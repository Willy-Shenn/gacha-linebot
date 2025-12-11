import sqlite3

conn = sqlite3.connect("gacha.db")
c = conn.cursor()

c.execute("""
CREATE TABLE IF NOT EXISTS exchange_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    line_user_id TEXT NOT NULL,
    contact TEXT NOT NULL,
    order_no TEXT NOT NULL,
    phone TEXT NOT NULL,
    email TEXT NOT NULL,
    orig_date TEXT NOT NULL,
    orig_slot TEXT NOT NULL,
    desired_date TEXT NOT NULL,
    desired_slot TEXT NOT NULL,
    verif_code TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    match_id INTEGER
)
""")

conn.commit()
conn.close()

print("DB initialized.")
