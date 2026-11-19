from __future__ import annotations
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from app.db import connect
from app.services.gate import assert_writable

router = APIRouter(tags=["lines"])

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

class ExpectIn(BaseModel):
    doc_no: str
    supplier_code: str
    sku: str
    dock_day: str
    qty: float = Field(gt=0)
    note: str | None = None

class ReceiveIn(BaseModel):
    receive_no: str
    supplier_code: str
    sku: str
    dock_day: str
    qty: float = Field(gt=0)
    occurred_at: str | None = None
    note: str | None = None

class ConfirmIn(BaseModel):
    confirm_no: str
    supplier_code: str
    sku: str
    dock_day: str
    qty: float = Field(gt=0)
    note: str | None = None

@router.get("/suppliers")
def list_suppliers():
    conn = connect()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM suppliers ORDER BY code").fetchall()]
    finally:
        conn.close()

@router.get("/expects")
def list_expects(dock_day: str | None = None):
    conn = connect()
    try:
        if dock_day:
            rows = conn.execute(
                "SELECT * FROM expect_lines WHERE dock_day=? ORDER BY id", (dock_day,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM expect_lines ORDER BY dock_day DESC, id").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

@router.post("/expects")
def add_expect(body: ExpectIn):
    conn = connect()
    try:
        assert_writable(conn, body.dock_day)
        conn.execute(
            '''INSERT INTO expect_lines(doc_no, supplier_code, sku, dock_day, qty, note, created_at)
               VALUES (?,?,?,?,?,?,?)''',
            (body.doc_no, body.supplier_code, body.sku, body.dock_day, body.qty, body.note, _now()),
        )
        conn.commit()
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(400, f"写入失败: {e}") from e
    finally:
        conn.close()

@router.get("/receives")
def list_receives(dock_day: str | None = None):
    conn = connect()
    try:
        if dock_day:
            rows = conn.execute(
                "SELECT * FROM receive_lines WHERE dock_day=? ORDER BY id", (dock_day,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM receive_lines ORDER BY dock_day DESC, id").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

@router.post("/receives")
def add_receive(body: ReceiveIn):
    conn = connect()
    try:
        assert_writable(conn, body.dock_day)
        occurred = body.occurred_at or _now()
        conn.execute(
            '''INSERT INTO receive_lines(receive_no, supplier_code, sku, dock_day, qty, occurred_at, note, created_at)
               VALUES (?,?,?,?,?,?,?,?)''',
            (
                body.receive_no,
                body.supplier_code,
                body.sku,
                body.dock_day,
                body.qty,
                occurred,
                body.note,
                _now(),
            ),
        )
        conn.commit()
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(400, f"写入失败: {e}") from e
    finally:
        conn.close()

@router.get("/confirms")
def list_confirms(dock_day: str | None = None):
    conn = connect()
    try:
        if dock_day:
            rows = conn.execute(
                "SELECT * FROM confirm_lines WHERE dock_day=? ORDER BY id", (dock_day,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM confirm_lines ORDER BY dock_day DESC, id").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

@router.post("/confirms")
def add_confirm(body: ConfirmIn):
    conn = connect()
    try:
        assert_writable(conn, body.dock_day)
        conn.execute(
            '''INSERT INTO confirm_lines(confirm_no, supplier_code, sku, dock_day, qty, note, created_at)
               VALUES (?,?,?,?,?,?,?)''',
            (body.confirm_no, body.supplier_code, body.sku, body.dock_day, body.qty, body.note, _now()),
        )
        conn.commit()
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(400, f"写入失败: {e}") from e
    finally:
        conn.close()
