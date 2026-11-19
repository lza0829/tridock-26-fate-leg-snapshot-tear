from __future__ import annotations
import sqlite3

def is_sealed(conn: sqlite3.Connection, dock_day: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM reconcile_batches WHERE dock_day=? AND status='sealed' LIMIT 1",
        (dock_day,),
    ).fetchone()
    return row is not None

def assert_writable(conn: sqlite3.Connection, dock_day: str) -> None:
    if is_sealed(conn, dock_day):
        raise ValueError(f"到货日 {dock_day} 已结案，禁止再改预报/实收/回执")
