import sqlite3

DB_PATH = "gacha.db"

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

print("資料庫初始化或更新完成。")
