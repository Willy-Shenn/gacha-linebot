import os
import random
import re
import sqlite3
import string
from datetime import datetime
from typing import Dict, Optional, Tuple

from dotenv import load_dotenv
from flask import Flask, abort, request
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

# 讀取 .env
load_dotenv()

app = Flask(__name__)

CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")

if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("請先設定 LINE_CHANNEL_SECRET 與 LINE_CHANNEL_ACCESS_TOKEN")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

DB_PATH = os.path.join(os.path.dirname(__file__), "gacha.db")

PLACE_ALLOWED = {"MAYDAY LAND": "MAYDAY LAND", "洲際棒球場": "洲際棒球場", "皆可": "皆可"}
PLACE_OPTIONS_TEXT = "地點僅接受：1. MAYDAY LAND  2. 洲際棒球場  3. 皆可，可直接輸入代號"
DISCLAIMER = "本系統僅提供扭蛋交換配對功能，不負責任合金流活動，亦不負任何法律責任"

FIELD_FLOW: Tuple[Tuple[str, str], ...] = (
    ("contact", "聯繫方式"),
    ("order_no", "扭蛋訂單編號"),
    ("phone", "手機號碼"),
    ("email", "E-mail"),
    ("orig_date", "原登記日期"),
    ("orig_slot", "原登記時段"),
    ("orig_place", "原登記地點"),
    ("desired_date", "希望交換日期"),
    ("desired_slot", "希望交換時段"),
    ("desired_place", "希望交換地點"),
)

FIELD_HINTS = {
    "order_no": "(9碼)",
    "orig_date": "(月/日，僅單日，限 12 或 01 月)",
    "orig_slot": "(24小時制，如:14:00~15:00)",
    "orig_place": f"({PLACE_OPTIONS_TEXT}，原登記僅接受 1 或 2)",
    "desired_date": "(月/日，可多日以逗號或頓號分隔，限 12 或 01 月)",
    "desired_slot": "(24小時制，如:14:00~15:00，可多段以逗號或頓號分隔，需與日期數量一致)",
    "desired_place": f"({PLACE_OPTIONS_TEXT})",
}

FIELD_LABEL_MAP = {label: key for key, label in FIELD_FLOW}

# key = line_user_id, value = {"mode": str}
user_states: Dict[str, Dict[str, str]] = {}


# ===== 輔助函式 =====

def label_with_hint(key: str) -> str:
    label = next(label for k, label in FIELD_FLOW if k == key)
    hint = FIELD_HINTS.get(key)
    return f"{label}{hint}" if hint else label


def canonicalize_label(label: str) -> str:
    # 移除括號提示字串，取得欄位本名
    return re.sub(r"\s*（.*?）|\s*\(.*?\)", "", label).strip()


def label_to_key(label: str) -> Optional[str]:
    canonical = canonicalize_label(label)
    for key, base_label in FIELD_FLOW:
        if canonicalize_label(base_label) == canonical:
            return key
    return None


# ===== 資料庫工具 =====

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS exchange_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            line_user_id TEXT NOT NULL,
            contact TEXT NOT NULL,
            order_no TEXT NOT NULL,
            phone TEXT NOT NULL,
            email TEXT NOT NULL,
            orig_date TEXT NOT NULL,
            orig_slot TEXT NOT NULL,
            orig_place TEXT NOT NULL DEFAULT '',
            desired_date TEXT NOT NULL,
            desired_slot TEXT NOT NULL,
            desired_place TEXT NOT NULL DEFAULT '',
            verif_code TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            match_id INTEGER
        )
        """
    )

    existing_cols = {row[1] for row in c.execute("PRAGMA table_info(exchange_requests)").fetchall()}
    migrations = {
        "orig_place": "ALTER TABLE exchange_requests ADD COLUMN orig_place TEXT NOT NULL DEFAULT ''",
        "desired_place": "ALTER TABLE exchange_requests ADD COLUMN desired_place TEXT NOT NULL DEFAULT ''",
    }
    for col, ddl in migrations.items():
        if col not in existing_cols:
            c.execute(ddl)

    conn.commit()
    conn.close()


def get_db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def has_pending_request(line_user_id: str) -> bool:
    conn = get_db_conn()
    c = conn.cursor()
    c.execute(
        "SELECT COUNT(*) FROM exchange_requests WHERE line_user_id = ? AND status = 'pending'",
        (line_user_id,),
    )
    count = c.fetchone()[0]
    conn.close()
    return count > 0


def cancel_pending_request(line_user_id: str) -> int:
    conn = get_db_conn()
    c = conn.cursor()
    c.execute(
        "DELETE FROM exchange_requests WHERE line_user_id = ? AND status = 'pending'",
        (line_user_id,),
    )
    deleted = c.rowcount
    conn.commit()
    conn.close()
    return deleted


def order_no_exists(line_user_id: str, order_no: str) -> bool:
    conn = get_db_conn()
    c = conn.cursor()
    c.execute(
        "SELECT COUNT(*) FROM exchange_requests WHERE line_user_id = ? AND order_no = ?",
        (line_user_id, order_no),
    )
    exists = c.fetchone()[0] > 0
    conn.close()
    return exists


def get_request_by_order_and_code(line_user_id: str, order_no: str, verif_code: str):
    conn = get_db_conn()
    c = conn.cursor()
    c.execute(
        """
        SELECT * FROM exchange_requests
        WHERE line_user_id = ? AND order_no = ? AND verif_code = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (line_user_id, order_no, verif_code),
    )
    row = c.fetchone()
    conn.close()
    return row


def delete_pending_by_id(req_id: int) -> int:
    conn = get_db_conn()
    c = conn.cursor()
    c.execute("DELETE FROM exchange_requests WHERE id = ? AND status = 'pending'", (req_id,))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    return deleted


def insert_request(data: Dict[str, str], line_user_id: str) -> int:
    verif_code = "".join(random.choices(string.digits, k=6))

    conn = get_db_conn()
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO exchange_requests (
            line_user_id, contact, order_no, phone, email,
            orig_date, orig_slot, orig_place,
            desired_date, desired_slot, desired_place,
            verif_code, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
        """,
        (
            line_user_id,
            data["contact"],
            data["order_no"],
            data["phone"],
            data["email"],
            data["orig_date"],
            data["orig_slot"],
            data["orig_place"],
            data["desired_date"],
            data["desired_slot"],
            data["desired_place"],
            verif_code,
        ),
    )
    new_id = c.lastrowid
    conn.commit()
    conn.close()
    return new_id


def get_request_by_id(req_id: int):
    conn = get_db_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM exchange_requests WHERE id = ?", (req_id,))
    row = c.fetchone()
    conn.close()
    return row


def get_partner(req) -> Optional[sqlite3.Row]:
    if req is None or req["match_id"] is None:
        return None
    conn = get_db_conn()
    c = conn.cursor()
    c.execute(
        "SELECT * FROM exchange_requests WHERE match_id = ? AND id != ? LIMIT 1",
        (req["match_id"], req["id"]),
    )
    partner = c.fetchone()
    conn.close()
    return partner


def normalize_place(raw: str) -> Optional[str]:
    cleaned = raw.strip()
    if not cleaned:
        return None

    normalized = cleaned.upper().replace(" ", "")
    if normalized in {"1", "1.", "1、", "1)", "MAYDAYLAND"}:
        return "MAYDAY LAND"
    if normalized in {"2", "2.", "2、", "2)", "洲際棒球場"}:
        return "洲際棒球場"
    if normalized in {"3", "3.", "3、", "3)", "皆可"}:
        return "皆可"

    # 使用者直接輸入名稱時保留原大小寫，英文比對後轉為標準格式
    if cleaned.upper() in PLACE_ALLOWED:
        return PLACE_ALLOWED[cleaned.upper()]
    if cleaned in PLACE_ALLOWED:
        return PLACE_ALLOWED[cleaned]
    return None


def normalize_order_no(raw: str) -> Optional[str]:
    cleaned = raw.strip()
    if not cleaned.isdigit() or len(cleaned) != 9:
        return None
    return cleaned


def normalize_date(raw: str) -> Optional[str]:
    cleaned = raw.strip().replace("-", "/")
    match = re.match(r"^\s*(?:(\d{4})/)?(\d{1,2})/(\d{1,2})\s*$", cleaned)
    if not match:
        return None

    _year_str, month_str, day_str = match.groups()
    month, day = int(month_str), int(day_str)
    if month not in {1, 12}:
        return None
    if not (1 <= day <= 31):
        return None

    try:
        dt = datetime(2024 if month == 12 else 2025, month, day)
    except ValueError:
        return None
    return dt.strftime("%m/%d")


def normalize_slot(raw: str) -> Tuple[Optional[str], Optional[str]]:
    cleaned = raw.strip()
    pattern = re.compile(r"^\s*(\d{1,2}):(\d{1,2})\s*[~\-]\s*(\d{1,2}):(\d{1,2})\s*$")
    match = pattern.match(cleaned)
    if not match:
        return None, "格式需為 hh:mm~hh:mm（24小時制）。"

    h1, m1, h2, m2 = map(int, match.groups())
    if not (0 <= h1 < 24 and 0 <= h2 < 24 and 0 <= m1 < 60 and 0 <= m2 < 60):
        return None, "時段需為 24 小時制，分鐘需為 00~59。"
    if (h1, m1) >= (h2, m2):
        return None, "開始時間需早於結束時間，請重新輸入。"

    return f"{h1:02d}:{m1:02d}~{h2:02d}:{m2:02d}", None


def split_multi_values(raw: str) -> list:
    return [part.strip() for part in re.split(r"[、,]", raw) if part.strip()]


def normalize_desired_dates(raw: str) -> Tuple[Optional[list], Optional[str]]:
    parts = split_multi_values(raw)
    if not parts:
        return None, "希望交換日期不可空白，格式為月/日，可多筆以逗號或頓號分隔。"

    normalized = []
    for part in parts:
        date_str = normalize_date(part)
        if date_str is None:
            return None, f"日期「{part}」格式錯誤，僅接受 12/xx 或 01/xx。"
        normalized.append(date_str)

    return normalized, None


def normalize_desired_slots(raw: str, expected_count: Optional[int] = None) -> Tuple[Optional[list], Optional[str]]:
    parts = split_multi_values(raw)
    if not parts:
        return None, "希望交換時段不可空白，格式為 hh:mm~hh:mm，可多筆以逗號或頓號分隔。"

    normalized = []
    for part in parts:
        slot, err = normalize_slot(part)
        if err:
            return None, f"時段「{part}」格式錯誤，需為 24 小時制 hh:mm~hh:mm，且開始早於結束。"
        normalized.append(slot)

    if expected_count is not None and len(normalized) != expected_count:
        return None, "希望交換日期與時段數量需一致，請檢查後重新輸入。"

    return normalized, None


def validate_field(key: str, value: str, data: Optional[Dict[str, str]] = None) -> Tuple[Optional[str], Optional[str]]:
    if key == "order_no":
        normalized = normalize_order_no(value)
        if normalized is None:
            return None, "扭蛋訂單編號需為 9 碼數字。"
        return normalized, None

    if key == "orig_date":
        if any(sep in value for sep in [",", "、"]):
            return None, "原登記日期僅能填寫單一天，請勿輸入區間。"
        normalized = normalize_date(value)
        if normalized is None:
            return None, "原登記日期格式需為 12/xx 或 01/xx，請重新輸入。"
        return normalized, None

    if key == "orig_slot":
        normalized, err = normalize_slot(value)
        if err:
            return None, err
        return normalized, None

    if key == "desired_date":
        normalized_list, err = normalize_desired_dates(value)
        if err:
            return None, err
        return ",".join(normalized_list), None

    if key == "desired_slot":
        expected = None
        if data and data.get("desired_date"):
            expected = len(split_multi_values(data["desired_date"]))
        normalized_list, err = normalize_desired_slots(value, expected_count=expected)
        if err:
            return None, err
        return ",".join(normalized_list), None

    if key in {"orig_place", "desired_place"}:
        normalized = normalize_place(value)
        if normalized is None:
            return None, f"{label_with_hint(key)}，{PLACE_OPTIONS_TEXT}。"
        if key == "orig_place" and normalized == "皆可":
            return None, "原登記地點不可選擇「皆可」，請輸入 1 或 2。"
        return normalized, None

    if not value.strip():
        return None, f"{label_with_hint(key)} 不可空白。"

    return value.strip(), None


def build_form_template() -> str:
    lines = [f"{idx + 1}. {label_with_hint(key)}: " for idx, (key, _label) in enumerate(FIELD_FLOW)]
    return "\n".join(lines)


def format_summary(data: Dict[str, str]) -> str:
    lines = []
    for idx, (key, _label) in enumerate(FIELD_FLOW, start=1):
        value = data.get(key, "")
        if key == "desired_date":
            value = format_desired_pairs_text(data) or value
        if key == "desired_slot":
            continue
        lines.append(f"{idx}. {label_with_hint(key)}: {value}")
    return "\n".join(lines)


def build_desired_pairs(record) -> list:
    dates = split_multi_values(record["desired_date"])
    slots = split_multi_values(record["desired_slot"])
    pairs = []
    for idx in range(min(len(dates), len(slots))):
        pairs.append((dates[idx], slots[idx]))
    return pairs


def format_desired_pairs_text(record) -> str:
    pairs = build_desired_pairs(record)
    if not pairs:
        return ""
    return "、".join(f"{d}:{s}" for d, s in pairs)


def parse_form_input(text: str) -> Tuple[Dict[str, str], list]:
    data: Dict[str, str] = {}
    errors: list = []

    chunks = []
    current = []
    for line in text.splitlines():
        if re.match(r"^\s*\d+\.", line):
            if current:
                chunks.append("\n".join(current))
                current = []
        if line.strip():
            current.append(line.strip())
    if current:
        chunks.append("\n".join(current))

    for chunk in chunks:
        m = re.match(
            r"^\s*\d+\.\s*(?P<label>(?:[^:：()]|\([^)]*\))+)\s*[:：]\s*(?P<value>.*)$",
            chunk,
            flags=re.S,
        )
        if not m:
            continue
        label, value = m.group("label").strip(), m.group("value").strip()
        key = label_to_key(label)
        if key is None:
            continue

        normalized, err = validate_field(key, value, data)
        if err:
            errors.append(f"{label_with_hint(key)}：{err}")
            continue

        if normalized is not None:
            data[key] = normalized

    if "desired_date" in data and "desired_slot" in data:
        if len(split_multi_values(data["desired_date"])) != len(split_multi_values(data["desired_slot"])):
            errors.append("希望交換日期與時段數量需一致，請重新確認。")

    for key, _label in FIELD_FLOW:
        if key not in data:
            errors.append(f"缺少欄位：{label_with_hint(key)}")

    return data, errors


def parse_single_field_input(key: str, text: str, data: Optional[Dict[str, str]] = None) -> Tuple[Optional[str], Optional[str]]:
    stripped = text.strip()
    if not stripped:
        return None, f"{label_with_hint(key)} 不可空白。"

    pattern = re.compile(
        r"^\s*\d+\.\s*(?P<label>(?:[^:：()]|\([^)]*\))+)\s*[:：]\s*(?P<value>.*)$",
        re.S,
    )
    match = pattern.match(stripped)
    if match:
        label = match.group("label").strip()
        value = match.group("value").strip()
        parsed_key = label_to_key(label)
        if parsed_key and parsed_key != key:
            return None, f"目前需更新「{label_with_hint(key)}」，請不要更換欄位。"
        stripped = value

    return validate_field(key, stripped, data)


def build_match_message(me, partner) -> str:
    return (
        "【扭蛋交換配對成功】\n"
        f"對方聯繫方式：{partner['contact']}\n"
        f"對方訂單編號：{partner['order_no']}\n"
        f"對方手機號碼：{partner['phone']}\n"
        f"對方 E-mail：{partner['email']}\n"
        f"對方原登記：{partner['orig_date']} {partner['orig_slot']} / {partner['orig_place']}\n"
        f"對方希望交換：{format_desired_pairs_text(partner)} / {partner['desired_place']}\n"
        f"對方驗證碼（請互相核對）：{partner['verif_code']}\n"
        "請盡快互相聯繫並先核對驗證碼以保障安全。\n\n"
        f"{DISCLAIMER}"
    )


def try_match_and_notify(new_id: int):
    conn = get_db_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM exchange_requests WHERE id = ?", (new_id,))
    me = c.fetchone()
    if not me or me["status"] != "pending":
        conn.close()
        return False

    me_pairs = build_desired_pairs(me)

    c.execute(
        """
        SELECT * FROM exchange_requests
        WHERE status = 'pending' AND id != ?
        """,
        (me["id"],),
    )
    fetched = c.fetchall()
    candidates = [row for row in fetched if row["line_user_id"] != me["line_user_id"]]

    other = None
    def place_ok(orig_place: str, desired_place: str) -> bool:
        return desired_place == "皆可" or orig_place == desired_place

    for cand in candidates:
        if not place_ok(cand["orig_place"], me["desired_place"]):
            continue
        if not place_ok(me["orig_place"], cand["desired_place"]):
            continue

        cand_pairs = build_desired_pairs(cand)
        me_to_other_ok = any(
            cand["orig_date"] == d and cand["orig_slot"] == s for d, s in me_pairs
        )
        other_to_me_ok = any(
            me["orig_date"] == d and me["orig_slot"] == s for d, s in cand_pairs
        )

        if me_to_other_ok and other_to_me_ok:
            other = cand
            break

    if not other:
        conn.close()
        return False

    match_id = min(me["id"], other["id"])
    c.execute(
        "UPDATE exchange_requests SET status = 'matched', match_id = ? WHERE id IN (?, ?)",
        (match_id, me["id"], other["id"]),
    )
    conn.commit()
    conn.close()

    try:
        msg_to_me = build_match_message(me, other)
        msg_to_other = build_match_message(other, me)
        line_bot_api.push_message(me["line_user_id"], TextSendMessage(text=msg_to_me))
        line_bot_api.push_message(other["line_user_id"], TextSendMessage(text=msg_to_other))
    except Exception as exc:
        print("push_message 發送失敗：", exc)

    return True


def build_confirm_message(req) -> str:
    data = {key: req[key] for key, _ in FIELD_FLOW}
    summary = format_summary(data)
    return (
        "登記完成！以下是你的資料，請確認：\n"
        f"{summary}\n"
        f"驗證碼: {req['verif_code']}\n\n"
        "系統會自動為你尋找互相需要的交換對象，配對成功時將主動通知。\n"
        "若要重新登記，可先輸入「取消」刪除待配對資料。\n\n"
        f"{DISCLAIMER}"
    )


def build_help_message() -> str:
    return (
        "目前提供的指令：\n"
        "- 輸入「登記」開始扭蛋交換登記流程（一次填寫 10 個欄位）。\n"
        "- 輸入「取消 訂單編號 驗證碼」，例如:取消 查詢 987654321 793921（配對成功後不可取消）。\n"
        "- 輸入「查詢 訂單編號 驗證碼」，例如:查詢 987654321 793921。\n"
        "同一 LINE 使用者可登記多筆，但每個扭蛋訂單編號不得重複。\n"
        "完成登記後系統會自動嘗試配對，成功時將主動推播通知。\n\n"
        "填寫格式範例：\n"
        f"{build_form_template()}\n\n"
        f"{DISCLAIMER}"
    )


init_db()


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK"


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()

    if text.startswith("取消"):
        user_states.pop(user_id, None)
        parts = text.split()
        if len(parts) < 3:
            reply = "取消請輸入：取消 訂單編號 驗證碼，例如:取消 查詢 987654321 793921\n配對成功後不可取消。"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            return

        order_no, code = parts[1], parts[2]
        req = get_request_by_order_and_code(user_id, order_no, code)
        if not req:
            reply = "查無此訂單或驗證碼，請確認後再試。"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            return
        if req["status"] == "matched":
            reply = "該筆登記已配對成功，無法取消。"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            return

        deleted = delete_pending_by_id(req["id"])
        if deleted:
            reply = "已為你取消該筆待配對登記。"
        else:
            reply = "目前此筆登記已非待配對狀態，無法取消。"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    if text.startswith("查詢"):
        user_states.pop(user_id, None)
        parts = text.split()
        if len(parts) < 3:
            reply = "查詢請輸入：查詢 訂單編號 驗證碼，例如:查詢 987654321 793921"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            return

        order_no, code = parts[1], parts[2]
        req = get_request_by_order_and_code(user_id, order_no, code)
        if not req:
            reply = "查無此訂單或驗證碼，請確認後再試。"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            return

        partner = get_partner(req) if req["status"] == "matched" else None
        status_text = "已配對" if req["status"] == "matched" else "待配對"
        summary = format_summary({key: req[key] for key, _ in FIELD_FLOW})
        base_msg = (
            f"訂單查詢結果（狀態：{status_text}）\n"
            f"{summary}\n"
            f"驗證碼: {req['verif_code']}"
        )
        if partner:
            partner_msg = (
                "\n\n配對對象資訊：\n"
                f"聯繫方式：{partner['contact']}\n"
                f"訂單編號：{partner['order_no']}\n"
                f"手機號碼：{partner['phone']}\n"
                f"E-mail：{partner['email']}\n"
                f"原登記：{partner['orig_date']} {partner['orig_slot']} / {partner['orig_place']}\n"
                f"希望交換：{format_desired_pairs_text(partner)} / {partner['desired_place']}\n"
                f"驗證碼：{partner['verif_code']}"
            )
            reply = base_msg + partner_msg + f"\n\n{DISCLAIMER}"
        else:
            reply = base_msg + f"\n\n{DISCLAIMER}"

        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    if text == "登記":
        user_states[user_id] = {"mode": "await_form"}
        intro = (
            "將為你進行扭蛋交換登記，請一次填寫以下 10 個欄位並直接回覆：\n"
            "注意：同一扭蛋訂單編號不可重複登記；配對成功後不可取消。\n"
            f"{build_form_template()}\n\n"
            f"{DISCLAIMER}"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=intro))
        return

    state = user_states.get(user_id)
    if state is not None and state.get("mode") == "await_form":
        data, errors = parse_form_input(text)
        if order_no_exists(user_id, data.get("order_no", "")):
            errors.append("此扭蛋訂單編號已登記，請使用不同的 9 碼編號。")

        if errors:
            prompt = "以下欄位需修正：\n" + "\n".join(errors) + f"\n\n請依下列格式重新輸入：\n{build_form_template()}"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=prompt))
            return

        user_states.pop(user_id, None)
        new_id = insert_request(data, user_id)
        req = get_request_by_id(new_id)
        confirm_msg = build_confirm_message(req)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=confirm_msg))
        try_match_and_notify(new_id)
        return

    help_msg = build_help_message()
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=help_msg))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
