import os
import random
import re
import sqlite3
import string
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

PLACE_ALLOWED = {"MAYDAY LAND": "MAYDAY LAND", "洲際棒球場": "洲際棒球場"}
PLACE_OPTIONS_TEXT = "地點僅接受：1. MAYDAY LAND  2. 洲際棒球場"

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

FIELD_LABEL_MAP = {label: key for key, label in FIELD_FLOW}

# key = line_user_id, value = {"mode": str}
user_states: Dict[str, Dict[str, str]] = {}


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


def normalize_place(raw: str) -> Optional[str]:
    cleaned = raw.strip()
    if not cleaned:
        return None

    normalized = cleaned.upper().replace(" ", "")
    if normalized in {"1", "1.", "1、", "1)", "MAYDAYLAND"}:
        return "MAYDAY LAND"
    if normalized in {"2", "2.", "2、", "2)", "洲際棒球場"}:
        return "洲際棒球場"

    # 使用者直接輸入名稱時保留原大小寫，英文比對後轉為標準格式
    if cleaned.upper() in PLACE_ALLOWED:
        return PLACE_ALLOWED[cleaned.upper()]
    if cleaned in PLACE_ALLOWED:
        return PLACE_ALLOWED[cleaned]
    return None


def build_form_template() -> str:
    lines = [f"{idx + 1}. {label}: " for idx, (_key, label) in enumerate(FIELD_FLOW)]
    lines.append(PLACE_OPTIONS_TEXT)
    return "\n".join(lines)


def format_summary(data: Dict[str, str]) -> str:
    lines = []
    for idx, (key, label) in enumerate(FIELD_FLOW, start=1):
        value = data.get(key, "")
        lines.append(f"{idx}. {label}: {value}")
    return "\n".join(lines)


def parse_form_input(text: str) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < len(FIELD_FLOW):
        return None, "欄位數量不足，請依格式填寫所有 10 個欄位。"

    data: Dict[str, str] = {}
    pattern = re.compile(r"^\s*\d+\.\s*([^:：]+)\s*[:：]\s*(.*)$")

    for line in lines:
        match = pattern.match(line)
        if not match:
            return None, f"無法解析此行：{line}"

        label = match.group(1).strip()
        value = match.group(2).strip()
        key = FIELD_LABEL_MAP.get(label)
        if key is None:
            return None, f"無法辨識欄位「{label}」，請確認欄位名稱。"
        if not value:
            return None, f"「{label}」不可空白，請補齊。"

        if key in {"orig_place", "desired_place"}:
            place_value = normalize_place(value)
            if place_value is None:
                return None, f"「{label}」格式錯誤，{PLACE_OPTIONS_TEXT}"
            value = place_value

        data[key] = value

    missing = [label for key, label in FIELD_FLOW if key not in data]
    if missing:
        return None, f"缺少欄位：{', '.join(missing)}"

    return data, None


def build_match_message(me, partner) -> str:
    return (
        "【扭蛋交換配對成功】\n"
        f"對方聯繫方式：{partner['contact']}\n"
        f"對方訂單編號：{partner['order_no']}\n"
        f"對方手機號碼：{partner['phone']}\n"
        f"對方 E-mail：{partner['email']}\n"
        f"對方原登記：{partner['orig_date']} {partner['orig_slot']} / {partner['orig_place']}\n"
        f"對方希望交換：{partner['desired_date']} {partner['desired_slot']} / {partner['desired_place']}\n"
        f"對方驗證碼（請互相核對）：{partner['verif_code']}\n"
        "請盡快互相聯繫並先核對驗證碼以保障安全。"
    )


def try_match_and_notify(new_id: int):
    conn = get_db_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM exchange_requests WHERE id = ?", (new_id,))
    me = c.fetchone()
    if not me or me["status"] != "pending":
        conn.close()
        return False

    c.execute(
        """
        SELECT * FROM exchange_requests
        WHERE status = 'pending'
          AND id != ?
          AND orig_date    = ?
          AND orig_slot    = ?
          AND orig_place   = ?
          AND desired_date = ?
          AND desired_slot = ?
          AND desired_place = ?
        ORDER BY id ASC
        LIMIT 1
        """,
        (
            me["id"],
            me["desired_date"],
            me["desired_slot"],
            me["desired_place"],
            me["orig_date"],
            me["orig_slot"],
            me["orig_place"],
        ),
    )
    other = c.fetchone()

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
        "若要重新登記，可先輸入「取消」刪除待配對資料。"
    )


def build_help_message() -> str:
    return (
        "目前提供的指令：\n"
        "- 輸入「登記」開始扭蛋交換登記流程（一次填寫 10 個欄位）。\n"
        "- 輸入「取消」刪除你尚未配對的登記資料。\n"
        "完成登記後系統會自動嘗試配對，成功時將主動推播通知。\n\n"
        "填寫格式範例：\n"
        f"{build_form_template()}"
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

    if text == "取消":
        user_states.pop(user_id, None)
        deleted = cancel_pending_request(user_id)
        if deleted:
            reply = "已為你取消尚未配對的登記資料。若要重新登記，請輸入「登記」。"
        else:
            reply = "目前查無你的待配對登記資料。若要新增，請輸入「登記」。"

        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    if text == "登記":
        if has_pending_request(user_id):
            reply = (
                "你目前已有一筆待配對的登記資料。\n"
                "若要重新登記，請先輸入「取消」刪除原紀錄。"
            )
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            return

        user_states[user_id] = {"mode": "await_form"}
        intro = (
            "將為你進行扭蛋交換登記，請一次填寫以下 10 個欄位並直接回覆：\n"
            f"{build_form_template()}"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=intro))
        return

    state = user_states.get(user_id)
    if state is not None and state.get("mode") == "await_form":
        data, err = parse_form_input(text)
        if err:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"{err}\n\n請依下列格式重新輸入：\n{build_form_template()}"),
            )
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
