import sqlite3

DB_NAME = "cloth_stock_db.db"  # 実際のファイル名に合わせてね

conn = sqlite3.connect(DB_NAME)
cur = conn.cursor()

cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
tables = cur.fetchall()

print("このDBにあるテーブル一覧：")
for t in tables:
    print("-", t[0])

conn.close()
