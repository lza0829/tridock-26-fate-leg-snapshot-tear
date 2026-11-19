from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.db import connect

router = APIRouter(tags=["settings"])

class DayIn(BaseModel):
    dock_day: str

@router.get("/settings")
def get_settings():
    conn = connect()
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}
    finally:
        conn.close()

@router.post("/settings/dock-day")
def set_dock_day(body: DayIn):
    if len(body.dock_day) != 10:
        raise HTTPException(400, "dock_day 格式应为 YYYY-MM-DD")
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES ('dock_day', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (body.dock_day,),
        )
        conn.commit()
        return {"dock_day": body.dock_day}
    finally:
        conn.close()
