from app.services import fate_lie
from __future__ import annotations
from datetime import datetime, timezone
import sqlite3

from app.services.gate import is_sealed

# ================= 全局唯一架构：回写腿数量 =================
# 处置（认短/认溢）不只是给差额行标命运，而是在同一事务内：
#   1. 命运守卫翻终态（pending -> 终态，终态不可再改）；
#   2. 按选定规则回写三腿正式表：以实收腿为准，凡与实收不一致的
#      预报/回执腿，插入调整单（ADJ-<variance_id>-E/C）把该腿合计
#      回写至实收数；原始单据不改动；
#   3. 每条回写腿写一行审计（旧合计 -> 新合计 + 调整单行号），
#      命运行带处置后三数量快照（供重跑保留终态核对）；
#   4. 刷新本批 open 对齐物化（match_rows 该键 + 批次计数）。
# 任一步失败整笔回滚：命运、调整单、审计、物化要么全成要么全不成。
# 隔离（quarantine）是终态挂起：只标命运 + 审计，不回写腿。
# 已结案（sealed）到货日禁止处置与回写。

TERMINAL_FATES = {"accept_short", "accept_over", "quarantine"}
WRITEBACK_FATES = {"accept_short", "accept_over"}

# 回写目标腿之外的待校正腿（实收腿是物理真值，永不回写）
_WRITEBACK_LEGS = (
    ("expect", "expect_lines", "doc_no", "E"),
    ("confirm", "confirm_lines", "confirm_no", "C"),
)

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _sum_map(conn: sqlite3.Connection, table: str, dock_day: str) -> dict[tuple[str, str], float]:
    rows = conn.execute(
        f"SELECT supplier_code, sku, SUM(qty) q FROM {table} WHERE dock_day=? GROUP BY supplier_code, sku",
        (dock_day,),
    ).fetchall()
    return {(r["supplier_code"], r["sku"]): float(r["q"]) for r in rows}

def _leg_sum(conn: sqlite3.Connection, table: str, dock_day: str, supplier_code: str, sku: str) -> float:
    row = conn.execute(
        f"SELECT COALESCE(SUM(qty),0) q FROM {table} WHERE dock_day=? AND supplier_code=? AND sku=?",
        (dock_day, supplier_code, sku),
    ).fetchone()
    return float(row["q"])

def _key_sums(conn: sqlite3.Connection, dock_day: str, supplier_code: str, sku: str) -> tuple[float, float, float]:
    return (
        _leg_sum(conn, "expect_lines", dock_day, supplier_code, sku),
        _leg_sum(conn, "receive_lines", dock_day, supplier_code, sku),
        _leg_sum(conn, "confirm_lines", dock_day, supplier_code, sku),
    )

def _preserved_fate(
    conn: sqlite3.Connection, dock_day: str, supplier_code: str, sku: str, eq: float, rq: float, cq: float
) -> tuple[str, str | None]:
    """终态命运保留规则（全局一致、可核对）：

    重跑对账时，若该键上次处置审计快照的三数量与当前三数量完全一致，
    说明是同一差额，保留其终态命运；数量变了视为新差额，重新 pending。
    """
    row = conn.execute(
        '''SELECT fate, created_at FROM disposition_audit
           WHERE dock_day=? AND supplier_code=? AND sku=? AND leg='fate'
             AND expect_qty=? AND receive_qty=? AND confirm_qty=?
           ORDER BY id DESC LIMIT 1''',
        (dock_day, supplier_code, sku, eq, rq, cq),
    ).fetchone()
    if row:
        return row["fate"], row["created_at"]
    return "pending", None

def run_reconcile(conn: sqlite3.Connection, dock_day: str) -> dict:
    conn.execute("BEGIN IMMEDIATE")
    try:
        if is_sealed(conn, dock_day):
            raise ValueError(f"到货日 {dock_day} 已结案，不能重跑对账")

        # drop previous open batch for same day
        old = conn.execute(
            "SELECT id FROM reconcile_batches WHERE dock_day=? AND status='open'",
            (dock_day,),
        ).fetchall()
        for r in old:
            conn.execute("DELETE FROM match_rows WHERE batch_id=?", (r["id"],))
            conn.execute("DELETE FROM variance_rows WHERE batch_id=?", (r["id"],))
            conn.execute("DELETE FROM reconcile_batches WHERE id=?", (r["id"],))

        expect_m = _sum_map(conn, "expect_lines", dock_day)
        receive_m = _sum_map(conn, "receive_lines", dock_day)
        confirm_m = _sum_map(conn, "confirm_lines", dock_day)
        keys = set(expect_m) | set(receive_m) | set(confirm_m)

        n = conn.execute("SELECT COUNT(*) c FROM reconcile_batches").fetchone()["c"] + 1
        batch_no = f"RB-{dock_day.replace('-', '')}-{n:03d}"
        cur = conn.execute(
            '''INSERT INTO reconcile_batches(batch_no, dock_day, matched_count, variance_count, status, sealed_at, note)
               VALUES (?,?,0,0,'open',NULL,?)''',
            (batch_no, dock_day, "三联对齐"),
        )
        batch_id = int(cur.lastrowid)

        matched = 0
        variance = 0
        now = _now()
        for supplier_code, sku in sorted(keys):
            eq = expect_m.get((supplier_code, sku), 0.0)
            rq = receive_m.get((supplier_code, sku), 0.0)
            cq = confirm_m.get((supplier_code, sku), 0.0)
            aligned = 1 if eq == rq == cq else 0
            conn.execute(
                '''INSERT INTO match_rows(batch_id, dock_day, supplier_code, sku, expect_qty, receive_qty, confirm_qty, aligned)
                   VALUES (?,?,?,?,?,?,?,?)''',
                (batch_id, dock_day, supplier_code, sku, eq, rq, cq, aligned),
            )
            if aligned:
                matched += 1
            else:
                variance += 1
                fate, disposed_at = _preserved_fate(conn, dock_day, supplier_code, sku, eq, rq, cq)
                conn.execute(
                    '''INSERT INTO variance_rows(batch_id, dock_day, supplier_code, sku, expect_qty, receive_qty, confirm_qty, fate, disposed_at, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)''',
                    (batch_id, dock_day, supplier_code, sku, eq, rq, cq, fate, disposed_at, now),
                )

        conn.execute(
            "UPDATE reconcile_batches SET matched_count=?, variance_count=? WHERE id=?",
            (matched, variance, batch_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "batch_id": batch_id,
        "batch_no": batch_no,
        "dock_day": dock_day,
        "matched_count": matched,
        "variance_count": variance,
        "status": "open",
    }

def seal_day(conn: sqlite3.Connection, dock_day: str) -> dict:
    conn.execute("BEGIN IMMEDIATE")
    try:
        batch = conn.execute(
            "SELECT * FROM reconcile_batches WHERE dock_day=? AND status='open' ORDER BY id DESC LIMIT 1",
            (dock_day,),
        ).fetchone()
        if not batch:
            raise ValueError("没有可结案的对账批次，请先跑三联对账")
        pending = conn.execute(
            "SELECT COUNT(*) c FROM variance_rows WHERE batch_id=? AND fate='pending'",
            (batch["id"],),
        ).fetchone()["c"]
        if pending:
            raise ValueError(f"仍有 {pending} 条差额未处置，不能结案")
        # 结案对照快照：终态命运计数与批次一并落库，结案后可核对、不再变
        counts = fate_counts(conn, dock_day)
        now = _now()
        conn.execute(
            '''UPDATE reconcile_batches
               SET status='sealed', sealed_at=?,
                   sealed_accept_short=?, sealed_accept_over=?, sealed_quarantine=?
               WHERE id=?''',
            (now, counts["accept_short"], counts["accept_over"], counts["quarantine"], batch["id"]),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return dict(conn.execute("SELECT * FROM reconcile_batches WHERE id=?", (batch["id"],)).fetchone())

def fate_counts(conn: sqlite3.Connection, dock_day: str) -> dict:
    """总览终态计数口径（全局一致）：

    - pending 取当前差额行（live，反映还没处置的）；
    - 终态（认短/认溢/隔离）取处置审计台账 disposition_audit，
      重跑对账删建差额行也不会丢终态计数。
    """
    counts = {"pending": 0, "accept_short": 0, "accept_over": 0, "quarantine": 0}
    counts["pending"] = conn.execute(
        "SELECT COUNT(*) c FROM variance_rows WHERE dock_day=? AND fate='pending'",
        (dock_day,),
    ).fetchone()["c"]
    for r in conn.execute(
        "SELECT fate, COUNT(*) c FROM disposition_audit WHERE dock_day=? AND leg='fate' GROUP BY fate",
        (dock_day,),
    ):
        counts[r["fate"]] = r["c"]
    return counts

def dispose_variance(conn: sqlite3.Connection, variance_id: int, fate: str) -> dict:
    if fate not in TERMINAL_FATES:
        raise ValueError("非法差额命运：只允许 accept_short / accept_over / quarantine")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM variance_rows WHERE id=?", (variance_id,)).fetchone()
        if not row:
            raise ValueError("差额行不存在")
        if row["fate"] != "pending":
            raise ValueError(f"差额已是终态 {row['fate']}，终态不可再改")
        dock_day = row["dock_day"]
        if is_sealed(conn, dock_day):
            raise ValueError(f"到货日 {dock_day} 已结案，禁止回写")
        batch = conn.execute(
            "SELECT * FROM reconcile_batches WHERE id=?", (row["batch_id"],)
        ).fetchone()
        if not batch or batch["status"] != "open":
            raise ValueError("差额所属批次不是 open 状态，禁止处置")

        now = _now()
        supplier_code = row["supplier_code"]
        sku = row["sku"]

        # 命运守卫：并发两笔只有一个能把 pending 翻成终态，
        # 另一笔 rowcount=0 -> 抛错 -> 整笔回滚，不会双写腿、不留半截命运。
        cur = conn.execute(
            "UPDATE variance_rows SET fate=?, disposed_at=? WHERE id=? AND fate='pending'",
            (fate, now, variance_id),
        )
        if cur.rowcount != 1:
            raise ValueError("差额已被并发处置，本次处置作废")

        # 回写腿数量：以实收腿为准，凡不一致的预报/回执腿插调整单校正。
        leg_audits: list[tuple[str, int, float, float]] = []
        if fate in WRITEBACK_FATES:
            eq, rq, cq = _key_sums(conn, dock_day, supplier_code, sku)
            current = {"expect": eq, "confirm": cq}
            for leg, table, no_col, tag in _WRITEBACK_LEGS:
                old_sum = current[leg]
                if old_sum == rq:
                    continue
                delta = rq - old_sum
                doc_no = f"ADJ-{variance_id}-{tag}"
                note = f"对账回写({fate})：{leg} 腿 {old_sum} → {rq}（以实收为准）"
                cur2 = conn.execute(
                    f"INSERT INTO {table}({no_col}, supplier_code, sku, dock_day, qty, note, created_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (doc_no, supplier_code, sku, dock_day, delta, note, now),
                )
                leg_audits.append((leg, int(cur2.lastrowid), old_sum, rq))

        # 重对齐物化：本批 open 对齐该键即刻按回写后数量刷新，不留陈旧物化。
        ne, nr, nc = _key_sums(conn, dock_day, supplier_code, sku)
        aligned = 1 if ne == nr == nc else 0
        conn.execute(
            '''UPDATE match_rows SET expect_qty=?, receive_qty=?, confirm_qty=?, aligned=?
               WHERE batch_id=? AND supplier_code=? AND sku=?''',
            (ne, nr, nc, aligned, row["batch_id"], supplier_code, sku),
        )
        matched = conn.execute(
            "SELECT COUNT(*) c FROM match_rows WHERE batch_id=? AND aligned=1", (row["batch_id"],)
        ).fetchone()["c"]
        twisted = conn.execute(
            "SELECT COUNT(*) c FROM match_rows WHERE batch_id=? AND aligned=0", (row["batch_id"],)
        ).fetchone()["c"]
        conn.execute(
            "UPDATE reconcile_batches SET matched_count=?, variance_count=? WHERE id=?",
            (matched, twisted, row["batch_id"]),
        )

        # 审计：每条回写腿一行（旧合计 -> 新合计 + 调整单行号）；
        # 命运行带处置后三数量快照（重跑保留终态的核对依据）。
        for leg, line_id, old_sum, new_sum in leg_audits:
            conn.execute(
                '''INSERT INTO disposition_audit(variance_id, batch_id, dock_day, supplier_code, sku,
                                                 fate, leg, line_id, old_sum, new_sum, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                (variance_id, row["batch_id"], dock_day, supplier_code, sku,
                 fate, leg, line_id, old_sum, new_sum, now),
            )
        conn.execute(
            '''INSERT INTO disposition_audit(variance_id, batch_id, dock_day, supplier_code, sku,
                                             fate, leg, old_fate, expect_qty, receive_qty, confirm_qty, created_at)
               VALUES (?,?,?,?,?,?,'fate','pending',?,?,?,?)''',
            (variance_id, row["batch_id"], dock_day, supplier_code, sku, fate, ne, nr, nc, now),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return dict(conn.execute("SELECT * FROM variance_rows WHERE id=?", (variance_id,)).fetchone())

def list_audits(
    conn: sqlite3.Connection, dock_day: str | None = None, variance_id: int | None = None
) -> list[dict]:
    sql = "SELECT * FROM disposition_audit"
    cond: list[str] = []
    args: list = []
    if variance_id is not None:
        cond.append("variance_id=?")
        args.append(variance_id)
    if dock_day:
        cond.append("dock_day=?")
        args.append(dock_day)
    if cond:
        sql += " WHERE " + " AND ".join(cond)
    sql += " ORDER BY id"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]
