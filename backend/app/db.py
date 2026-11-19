from __future__ import annotations
import sqlite3
from app.config import DATA_DIR, DB_PATH

def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    # 并发处置同一差额时，后到的写事务等待先到的提交，再由命运守卫判重，
    # 而不是立刻报 database is locked。
    conn.execute("PRAGMA busy_timeout=5000")
    return conn
