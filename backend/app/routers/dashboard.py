from __future__ import annotations

from fastapi import APIRouter

from app.db import connect
from app.services.gate import is_sealed
from app.services import reconcile_service as svc

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard")
def dashboard():
    conn = connect()
    try:
        day = conn.execute("SELECT value FROM settings WHERE key='dock_day'").fetchone()["value"]
        expect_n = conn.execute("SELECT COUNT(*) c FROM expect_lines WHERE dock_day=?", (day,)).fetchone()["c"]
        receive_n = conn.execute("SELECT COUNT(*) c FROM receive_lines WHERE dock_day=?", (day,)).fetchone()["c"]
        confirm_n = conn.execute("SELECT COUNT(*) c FROM confirm_lines WHERE dock_day=?", (day,)).fetchone()["c"]
        batch = conn.execute(
            "SELECT * FROM reconcile_batches WHERE dock_day=? ORDER BY id DESC LIMIT 1",
            (day,),
        ).fetchone()
        # 看板终态计数与处置/结案证据同一口径：未决取差额行，
        # 终态取处置审计台账每键最新一次，不另加任何"看板补偿"。
        counts = svc.fate_counts(conn, day)
        problems = svc.chain_problems(conn, day)
        return {
            "dock_day": day,
            "sealed": is_sealed(conn, day),
            "expect_count": expect_n,
            "receive_count": receive_n,
            "confirm_count": confirm_n,
            "latest_batch": dict(batch) if batch else None,
            "pending_variance": counts["pending"],
            "fate_counts": counts,
            "chain_problems": problems,
            "chains_aligned": not problems,
        }
    finally:
        conn.close()
