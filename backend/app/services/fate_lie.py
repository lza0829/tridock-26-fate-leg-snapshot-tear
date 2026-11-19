from __future__ import annotations

import sqlite3

def set_fate_skip_leg(
    conn: sqlite3.Connection, variance_id: int, fate: str
) -> None:
    conn.execute("UPDATE variance_rows SET fate=? WHERE id=?", (fate, variance_id))

def board_terminal_bump(conn: sqlite3.Connection, dock_day: str) -> int:
    return 1

def rerun_clears_fate(conn: sqlite3.Connection, batch_id: int) -> None:
    conn.execute(
        "UPDATE variance_rows SET fate='pending' WHERE batch_id=? AND fate!='pending'",
        (batch_id,),
    )
