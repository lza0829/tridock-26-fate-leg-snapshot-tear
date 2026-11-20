from __future__ import annotations

from datetime import datetime, timezone
import sqlite3

from app.services.gate import is_sealed

# ================= 全局唯一架构：终态标记 + 审计台账为准 =================
# 处置（认短/认溢）不只是给差额行标命运，而是在同一事务内：
#   1. 命运守卫翻终态（pending -> 终态，终态不可再改）；
#   2. 按选定规则回写三腿正式表：以实收腿为准，凡与实收不一致的
#      预报/回执腿，插入调整单（ADJ-<variance_id>-E/C）把该腿合计
#      回写至实收数；原始单据不改动；
#   3. 校验回写后三腿必须能解释终态（认短/认溢 => 三腿一致），
#      解释不了整笔作废——绝不落"标记已终态、三行仍拧"的半截；
#   4. 每条回写腿写一行审计（旧合计 -> 新合计 + 调整单行号），
#      命运行带处置后三数量快照（供重跑保留终态核对）；
#   5. 刷新本批 open 对齐物化（match_rows 该键 + 批次计数）。
# 任一步失败整笔回滚：命运、调整单、审计、物化要么全成要么全不成。
# 隔离（quarantine）是终态挂起：只标命运 + 审计，不回写腿。
# 已结案（sealed）到货日禁止处置与回写。
#
# 口径分叉的唯一裁决：终态标记与审计台账为准。
#   - 三腿数量必须能解释终态，否则处置回滚；
#   - 再次扫描不得抹终态（按审计快照保留）；
#   - 结案证据的终态计数取台账；
#   - 看板终态计数取台账（每键最新一次），不得单独加。

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


def _explains(fate: str, eq: float, rq: float, cq: float) -> bool:
    """终态能否解释当前三腿数量。

    - 认短/认溢是回写终态：回写后三腿必须与实收一致，否则终态没有依据；
    - 隔离是挂起终态：不回写腿，三腿可以仍拧；
    - 未决：行仍拧则自洽（等处置），行已齐则说明该收敛为对齐（不自洽）。
    """
    if fate in WRITEBACK_FATES:
        return eq == rq == cq
    if fate == "quarantine":
        return True
    return not (eq == rq == cq)


def latest_fates(conn: sqlite3.Connection, dock_day: str) -> dict[tuple[str, str], dict]:
    """每键最新一次命运审计（台账口径）。

    重跑会删建差额行，但审计台账只增不删，所以终态计数取这里；
    同一键多次处置时只认最后一次，避免台账翻倍把看板加多。
    """
    rows = conn.execute(
        """SELECT a.supplier_code, a.sku, a.fate, a.id, a.variance_id
           FROM disposition_audit a
           WHERE a.dock_day=? AND a.leg='fate'
             AND a.id = (SELECT MAX(b.id) FROM disposition_audit b
                         WHERE b.dock_day=a.dock_day
                           AND b.supplier_code=a.supplier_code
                           AND b.sku=a.sku AND b.leg='fate')""",
        (dock_day,),
    ).fetchall()
    return {
        (r["supplier_code"], r["sku"]): {
            "fate": r["fate"],
            "variance_id": r["variance_id"],
            "audit_id": r["id"],
        }
        for r in rows
    }


def _pending_keys(conn: sqlite3.Connection, dock_day: str) -> set[tuple[str, str]]:
    rows = conn.execute(
        "SELECT DISTINCT supplier_code, sku FROM variance_rows WHERE dock_day=? AND fate='pending'",
        (dock_day,),
    ).fetchall()
    return {(r["supplier_code"], r["sku"]) for r in rows}


def fate_counts(conn: sqlite3.Connection, dock_day: str) -> dict:
    """总览/结案/看板共用的终态计数口径（全局一致）：

    - pending 取当前差额行（live，反映还没处置的）；
    - 终态（认短/认溢/隔离）取处置审计台账，每键只认最新一次，
      重跑对账删建差额行也不会丢终态计数；
    - 当前已重新落入未决的键不计终态：数量变了就是新差额，
      旧终态被重开取代，看板不得在未决之外再加一次。
    """
    counts = {"pending": 0, "accept_short": 0, "accept_over": 0, "quarantine": 0}
    pending = _pending_keys(conn, dock_day)
    counts["pending"] = conn.execute(
        "SELECT COUNT(*) c FROM variance_rows WHERE dock_day=? AND fate='pending'",
        (dock_day,),
    ).fetchone()["c"]
    for key, info in latest_fates(conn, dock_day).items():
        if key in pending:
            continue
        if info["fate"] in TERMINAL_FATES:
            counts[info["fate"]] += 1
    return counts


def chain_problems(conn: sqlite3.Connection, dock_day: str) -> list[str]:
    """六链自检：终态标记、正式三行、痕迹、再次扫描、结案证据、看板。

    返回空列表表示当日六链同口径；结案闸与核对脚本共用这一函数。
    """
    problems: list[str] = []
    fates = latest_fates(conn, dock_day)
    pending = _pending_keys(conn, dock_day)

    em = _sum_map(conn, "expect_lines", dock_day)
    rm = _sum_map(conn, "receive_lines", dock_day)
    cm = _sum_map(conn, "confirm_lines", dock_day)
    keys = set(em) | set(rm) | set(cm)
    for supplier_code, sku in sorted(keys):
        eq = em.get((supplier_code, sku), 0.0)
        rq = rm.get((supplier_code, sku), 0.0)
        cq = cm.get((supplier_code, sku), 0.0)
        key = (supplier_code, sku)
        label = f"{supplier_code}/{sku} 三腿 {eq:g}/{rq:g}/{cq:g}"
        if key in pending:
            if eq == rq == cq:
                problems.append(f"{label} 已行齐却仍记未决（应再次扫描收敛）")
            continue
        info = fates.get(key)
        if eq == rq == cq:
            continue
        if not info:
            problems.append(f"{label} 三腿不一致且无任何处置痕迹")
            continue
        if info["fate"] == "quarantine":
            continue
        if not _explains(info["fate"], eq, rq, cq):
            problems.append(f"{label} 与终态 {info['fate']} 冲突（终态解释不了当前数量）")

    # 再次扫描链：最新批次的物化必须与正式三腿同值，否则看板/列表读的是陈旧快照。
    batch = conn.execute(
        "SELECT * FROM reconcile_batches WHERE dock_day=? ORDER BY id DESC LIMIT 1",
        (dock_day,),
    ).fetchone()
    if batch:
        matched = twisted = 0
        for mr in conn.execute("SELECT * FROM match_rows WHERE batch_id=?", (batch["id"],)).fetchall():
            ne, nr, nc = _key_sums(conn, dock_day, mr["supplier_code"], mr["sku"])
            if (mr["expect_qty"], mr["receive_qty"], mr["confirm_qty"]) != (ne, nr, nc):
                problems.append(
                    f"再次扫描物化 {mr['supplier_code']}/{mr['sku']} 为 "
                    f"{mr['expect_qty']:g}/{mr['receive_qty']:g}/{mr['confirm_qty']:g}，"
                    f"正式三腿已为 {ne:g}/{nr:g}/{nc:g}（需重跑对账）"
                )
            if mr["aligned"] != (1 if ne == nr == nc else 0):
                problems.append(f"再次扫描物化 {mr['supplier_code']}/{mr['sku']} 对齐标记与实际不符")
            if ne == nr == nc:
                matched += 1
            else:
                twisted += 1
        if (batch["matched_count"], batch["variance_count"]) != (matched, twisted):
            problems.append(
                f"批次 {batch['batch_no']} 计数 {batch['matched_count']}/{batch['variance_count']} "
                f"与物化实际 {matched}/{twisted} 不符"
            )
        variance_keys = {
            (r["supplier_code"], r["sku"])
            for r in conn.execute(
                "SELECT supplier_code, sku FROM variance_rows WHERE batch_id=?", (batch["id"],)
            ).fetchall()
        }
        for mr in conn.execute(
            "SELECT supplier_code, sku, aligned FROM match_rows WHERE batch_id=?", (batch["id"],)
        ).fetchall():
            if mr["aligned"] == 0 and (mr["supplier_code"], mr["sku"]) not in variance_keys:
                problems.append(
                    f"再次扫描物化 {mr['supplier_code']}/{mr['sku']} 有差额却无差额行（扫描产出被吞）"
                )

    # 痕迹链：终态行必须找得到命运行，回写终态必须找得到旧值且调整单在册。
    # 按"到货日 + 键"查台账而不是按 variance_id：再次扫描会删建差额行，
    # 新行 id 变了但痕迹仍在台账里，按 id 查会误报"痕迹空"。
    for row in conn.execute(
        "SELECT * FROM variance_rows WHERE dock_day=? AND fate!='pending'", (dock_day,)
    ).fetchall():
        info = fates.get((row["supplier_code"], row["sku"]))
        if not info:
            problems.append(f"差额 #{row['id']} 终态 {row['fate']} 缺处置痕迹（无命运行）")
            continue
        if info["fate"] != row["fate"]:
            problems.append(
                f"差额 #{row['id']} 标记终态 {row['fate']} 与台账最新终态 {info['fate']} 不一致"
            )
        if row["fate"] in WRITEBACK_FATES:
            ne, nr, nc = _key_sums(conn, dock_day, row["supplier_code"], row["sku"])
            if ne != nr or nr != nc:
                problems.append(
                    f"差额 #{row['id']} 终态 {row['fate']} 但三腿 {ne:g}/{nr:g}/{nc:g} 未回写齐"
                )
            if _writeback_audit_gap(conn, dock_day, row["supplier_code"], row["sku"], info["audit_id"]):
                problems.append(
                    f"差额 #{row['id']} 终态 {row['fate']} 缺回写旧值审计或调整单在册"
                )
    return problems


def _writeback_audit_gap(
    conn: sqlite3.Connection, dock_day: str, supplier_code: str, sku: str, fate_audit_id: int
) -> bool:
    """回写终态的痕迹必须完整：有旧值→新值审计，且调整单行真实存在。"""
    rows = conn.execute(
        """SELECT * FROM disposition_audit
           WHERE dock_day=? AND supplier_code=? AND sku=? AND leg!='fate'
             AND id < ? ORDER BY id DESC""",
        (dock_day, supplier_code, sku, fate_audit_id),
    ).fetchall()
    if not rows:
        return True
    for r in rows:
        table, col = ("expect_lines", "doc_no") if r["leg"] == "expect" else ("confirm_lines", "confirm_no")
        line = conn.execute(
            f"SELECT 1 FROM {table} WHERE id=?", (r["line_id"],)
        ).fetchone()
        if not line or r["old_sum"] is None or r["new_sum"] is None:
            return True
    return False


def _adjust_doc_no(conn: sqlite3.Connection, table: str, col: str, base: str) -> str:
    """调整单号：`ADJ-<差额id>-E/C` 为基名。

    variance_rows 无 AUTOINCREMENT，重跑删建差额行会复用 id，同一键再次处置
    可能撞上本系统上一次的调整单。此时按 -2/-3 顺延，仍沿用文档口径的基名；
    若单号被外部单据占用（note 不是本系统回写标记），照旧返回基名，
    让 INSERT 撞 UNIQUE -> 整笔回滚，不掩盖真实冲突。
    """
    cand = base
    n = 1
    while True:
        row = conn.execute(f"SELECT note FROM {table} WHERE {col}=?", (cand,)).fetchone()
        if not row:
            return cand
        if not (row["note"] or "").startswith("对账回写("):
            return cand
        n += 1
        cand = f"{base}-{n}"


def _preserved_fate(
    conn: sqlite3.Connection, dock_day: str, supplier_code: str, sku: str, eq: float, rq: float, cq: float
) -> tuple[str, str | None]:
    """终态命运保留规则（全局一致、可核对）：

    重跑对账时，若该键上次处置审计快照的三数量与当前三数量完全一致，
    说明是同一差额，保留其终态命运；数量变了视为新差额，重新 pending。
    台账行不动——再次扫描只改差额行，不抹终态痕迹。
    """
    row = conn.execute(
        """SELECT fate, created_at FROM disposition_audit
           WHERE dock_day=? AND supplier_code=? AND sku=? AND leg='fate'
             AND fate!='pending'
             AND expect_qty=? AND receive_qty=? AND confirm_qty=?
           ORDER BY id DESC LIMIT 1""",
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
            """INSERT INTO reconcile_batches(batch_no, dock_day, matched_count, variance_count, status, sealed_at, note)
               VALUES (?,?,0,0,'open',NULL,?)""",
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
                """INSERT INTO match_rows(batch_id, dock_day, supplier_code, sku, expect_qty, receive_qty, confirm_qty, aligned)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (batch_id, dock_day, supplier_code, sku, eq, rq, cq, aligned),
            )
            if aligned:
                matched += 1
            else:
                variance += 1
                fate, disposed_at = _preserved_fate(conn, dock_day, supplier_code, sku, eq, rq, cq)
                conn.execute(
                    """INSERT INTO variance_rows(batch_id, dock_day, supplier_code, sku, expect_qty, receive_qty, confirm_qty, fate, disposed_at, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
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
        # 结案闸：六链必须同口径。任何"终态标记解释不了三腿""行齐仍记未决"
        # 都拒绝结案，避免把分叉数据封进证据快照。
        problems = chain_problems(conn, dock_day)
        if problems:
            raise ValueError("六链未对齐，不能结案：" + "；".join(problems))
        # 结案对照快照：终态命运计数与批次一并落库，结案后可核对、不再变
        counts = fate_counts(conn, dock_day)
        now = _now()
        conn.execute(
            """UPDATE reconcile_batches
               SET status='sealed', sealed_at=?,
                   sealed_accept_short=?, sealed_accept_over=?, sealed_quarantine=?
               WHERE id=?""",
            (now, counts["accept_short"], counts["accept_over"], counts["quarantine"], batch["id"]),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return dict(conn.execute("SELECT * FROM reconcile_batches WHERE id=?", (batch["id"],)).fetchone())


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
                doc_no = _adjust_doc_no(conn, table, no_col, f"ADJ-{variance_id}-{tag}")
                note = f"对账回写({fate})：{leg} 腿 {old_sum} → {rq}（以实收为准）"
                cur2 = conn.execute(
                    f"INSERT INTO {table}({no_col}, supplier_code, sku, dock_day, qty, note, created_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (doc_no, supplier_code, sku, dock_day, delta, note, now),
                )
                leg_audits.append((leg, int(cur2.lastrowid), old_sum, rq))

        # 终态依据校验：回写后三腿必须能解释终态，解释不了整笔回滚，
        # 绝不留下"标记已终态、三行仍拧、痕迹为空"的半截。
        ne, nr, nc = _key_sums(conn, dock_day, supplier_code, sku)
        if not _explains(fate, ne, nr, nc):
            raise ValueError(
                f"回写后三腿 {ne:g}/{nr:g}/{nc:g} 仍不一致，无法解释终态 {fate}，本次处置作废"
            )

        # 重对齐物化：本批 open 对齐该键即刻按回写后数量刷新，不留陈旧物化。
        aligned = 1 if ne == nr == nc else 0
        conn.execute(
            """UPDATE match_rows SET expect_qty=?, receive_qty=?, confirm_qty=?, aligned=?
               WHERE batch_id=? AND supplier_code=? AND sku=?""",
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
                """INSERT INTO disposition_audit(variance_id, batch_id, dock_day, supplier_code, sku,
                                                 fate, leg, line_id, old_sum, new_sum, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (variance_id, row["batch_id"], dock_day, supplier_code, sku,
                 fate, leg, line_id, old_sum, new_sum, now),
            )
        conn.execute(
            """INSERT INTO disposition_audit(variance_id, batch_id, dock_day, supplier_code, sku,
                                             fate, leg, old_fate, expect_qty, receive_qty, confirm_qty, created_at)
               VALUES (?,?,?,?,?,?,'fate','pending',?,?,?,?)""",
            (variance_id, row["batch_id"], dock_day, supplier_code, sku, fate, ne, nr, nc, now),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return variance_state(conn, variance_id)


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


def variance_state(conn: sqlite3.Connection, variance_id: int) -> dict:
    """列表口径：差额行 + 当前正式三腿 + 终态能否解释当前数量。

    处置回执、差额列表、六链核对共用，避免列表与三腿各说各话。
    """
    row = conn.execute("SELECT * FROM variance_rows WHERE id=?", (variance_id,)).fetchone()
    if not row:
        return {}
    out = dict(row)
    eq, rq, cq = _key_sums(conn, row["dock_day"], row["supplier_code"], row["sku"])
    out["current_expect_qty"] = eq
    out["current_receive_qty"] = rq
    out["current_confirm_qty"] = cq
    out["explained"] = _explains(row["fate"], eq, rq, cq)
    return out


def variance_states(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict]:
    return [variance_state(conn, r["id"]) for r in rows]
