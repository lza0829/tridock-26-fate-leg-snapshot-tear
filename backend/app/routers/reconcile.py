from app.services.disposition import apply_fate, before_rerun
from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.db import connect
from app.services import reconcile_service as svc

router = APIRouter(tags=["reconcile"])

class RunIn(BaseModel):
    dock_day: str

class SealIn(BaseModel):
    dock_day: str

class FateIn(BaseModel):
    fate: str

@router.post("/reconcile/run")
def run_reconcile(body: RunIn):
    conn = connect()
    try:
        return svc.run_reconcile(conn, body.dock_day)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    finally:
        conn.close()

@router.post("/reconcile/seal")
def seal(body: SealIn):
    conn = connect()
    try:
        return svc.seal_day(conn, body.dock_day)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    finally:
        conn.close()

@router.get("/reconcile/batches")
def batches(dock_day: str | None = None):
    conn = connect()
    try:
        if dock_day:
            rows = conn.execute(
                "SELECT * FROM reconcile_batches WHERE dock_day=? ORDER BY id DESC", (dock_day,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM reconcile_batches ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

@router.get("/reconcile/matches")
def matches(batch_id: int):
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM match_rows WHERE batch_id=? ORDER BY supplier_code, sku", (batch_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

@router.get("/reconcile/variances")
def variances(batch_id: int | None = None, dock_day: str | None = None):
    conn = connect()
    try:
        if batch_id is not None:
            rows = conn.execute(
                "SELECT * FROM variance_rows WHERE batch_id=? ORDER BY id", (batch_id,)
            ).fetchall()
        elif dock_day:
            rows = conn.execute(
                "SELECT * FROM variance_rows WHERE dock_day=? ORDER BY id", (dock_day,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM variance_rows ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

@router.post("/reconcile/variances/{variance_id}/fate")
def set_fate(variance_id: int, body: FateIn):
    conn = connect()
    try:
        return svc.dispose_variance(conn, variance_id, body.fate)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    finally:
        conn.close()

@router.get("/reconcile/audits")
def audits(dock_day: str | None = None, variance_id: int | None = None):
    conn = connect()
    try:
        return svc.list_audits(conn, dock_day=dock_day, variance_id=variance_id)
    finally:
        conn.close()

@router.get("/reconcile/fate-counts")
def fate_counts(dock_day: str):
    conn = connect()
    try:
        return svc.fate_counts(conn, dock_day)
    finally:
        conn.close()
