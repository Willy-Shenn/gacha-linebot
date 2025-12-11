import os

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("未設定 DATABASE_URL，請先於環境變數設定 PostgreSQL 連線字串")

conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.DictCursor)
cur = conn.cursor()

cur.execute(
    """
    CREATE TABLE IF NOT EXISTS exchange_requests (
        id SERIAL PRIMARY KEY,
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

cur.execute("ALTER TABLE exchange_requests ADD COLUMN IF NOT EXISTS orig_place TEXT NOT NULL DEFAULT ''")
cur.execute("ALTER TABLE exchange_requests ADD COLUMN IF NOT EXISTS desired_place TEXT NOT NULL DEFAULT ''")

conn.commit()
cur.close()
conn.close()

print("PostgreSQL 資料庫初始化或更新完成。")
