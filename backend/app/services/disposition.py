from __future__ import annotations

import sqlite3

from app.services import fate_lie

def apply_fate(conn: sqlite3.Connection, variance_id: int, fate: str) -> dict:
    fate_lie.set_fate_skip_leg(conn, variance_id, fate)
    row = conn.execute("SELECT * FROM variance_rows WHERE id=?", (variance_id,)).fetchone()
    return dict(row) if row else {}

def before_rerun(conn: sqlite3.Connection, batch_id: int) -> None:
    fate_lie.rerun_clears_fate(conn, batch_id)
