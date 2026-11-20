"""回写腿数量架构的端到端核对脚本（不依赖 fastapi，直接打服务层）。

用法: python3 -I tests/verify_disposition.py   （在 backend/ 下运行）

覆盖核对清单：
  1. 认短/认溢回写后三腿一致，旧值入审计可查
  2. 隔离只标命运、不回写腿
  3. 已结案后回写被拒
  4. 处置与总览终态计数一致（含重跑后）
  5. 回写失败整笔命运回滚（无半截命运、无孤儿调整单/审计）
  6. 并发处置同一差额：只有一笔成功，不双写腿
  7. 结案快照带命运计数；重跑按数量快照保留终态
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.db as db  # noqa: E402
from app.seed import SCHEMA, _migrate  # noqa: E402
from app.services import reconcile_service as svc  # noqa: E402
from app.services.gate import assert_writable, is_sealed  # noqa: E402

FAILURES: list[str] = []

def check(name: str, cond: bool, detail: str = "") -> None:
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)

def fresh_db() -> object:
    tmp = tempfile.mkdtemp(prefix="tridock-test-")
    db.DATA_DIR = Path(tmp)
    db.DB_PATH = Path(tmp) / "test.db"
    conn = db.connect()
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn

def leg(conn, table, no_col, no, sup, sku, day, qty):
    if table == "receive_lines":
        conn.execute(
            "INSERT INTO receive_lines(receive_no, supplier_code, sku, dock_day, qty, occurred_at, note, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (no, sup, sku, day, qty, "2026-11-19T09:00:00Z", None, "2026-11-19T09:00:00Z"),
        )
    else:
        col = "doc_no" if table == "expect_lines" else "confirm_no"
        conn.execute(
            f"INSERT INTO {table}({col}, supplier_code, sku, dock_day, qty, note, created_at) VALUES (?,?,?,?,?,?,?)",
            (no, sup, sku, day, qty, None, "2026-11-19T09:00:00Z"),
        )

def sums(conn, day, sup, sku):
    out = []
    for t in ("expect_lines", "receive_lines", "confirm_lines"):
        out.append(
            conn.execute(
                f"SELECT COALESCE(SUM(qty),0) q FROM {t} WHERE dock_day=? AND supplier_code=? AND sku=?",
                (day, sup, sku),
            ).fetchone()["q"]
        )
    return tuple(out)

def audits_of(conn, variance_id):
    return conn.execute(
        "SELECT * FROM disposition_audit WHERE variance_id=? ORDER BY id", (variance_id,)
    ).fetchall()

def variance_of(conn, batch_id, sku):
    return conn.execute(
        "SELECT * FROM variance_rows WHERE batch_id=? AND sku=?", (batch_id, sku)
    ).fetchone()

SUP = "SUP-A"
RICE = "SKU-米-25"
TOWEL = "SKU-巾-卷"

# ---------- S1+S2+S3+S7：认短/认溢回写、结案快照、封后拒写、总览一致 ----------
print("== S1/S2 认短认溢回写 + S3 结案快照与封后拒写 + S7 总览一致 ==")
conn = fresh_db()
D1 = "2026-11-18"
leg(conn, "expect_lines", "doc_no", "EX-1", SUP, RICE, D1, 40)
leg(conn, "receive_lines", "receive_no", "RC-1", SUP, RICE, D1, 38)
leg(conn, "confirm_lines", "confirm_no", "CF-1", SUP, RICE, D1, 38)
leg(conn, "expect_lines", "doc_no", "EX-2", SUP, TOWEL, D1, 60)
leg(conn, "receive_lines", "receive_no", "RC-2", SUP, TOWEL, D1, 66)
leg(conn, "confirm_lines", "confirm_no", "CF-2", SUP, TOWEL, D1, 60)
conn.commit()

b = svc.run_reconcile(conn, D1)
check("S1 对账出 2 条差额", b["variance_count"] == 2, str(b))

v_short = variance_of(conn, b["batch_id"], RICE)
r = svc.dispose_variance(conn, v_short["id"], "accept_short")
check("S1 认短后命运为终态且有处置时间", r["fate"] == "accept_short" and r["disposed_at"])
check("S1 回写后三腿一致（38/38/38）", sums(conn, D1, SUP, RICE) == (38, 38, 38), str(sums(conn, D1, SUP, RICE)))
adj = conn.execute("SELECT * FROM expect_lines WHERE doc_no=?", (f"ADJ-{v_short['id']}-E",)).fetchone()
check("S1 预报腿生成调整单（-2）", adj is not None and adj["qty"] == -2)
au = audits_of(conn, v_short["id"])
leg_au = [a for a in au if a["leg"] == "expect"]
fate_au = [a for a in au if a["leg"] == "fate"]
check("S1 审计含回写腿旧值→新值（40→38）",
      len(leg_au) == 1 and leg_au[0]["old_sum"] == 40 and leg_au[0]["new_sum"] == 38
      and leg_au[0]["line_id"] == adj["id"])
check("S1 审计命运行带三数量快照（38/38/38）",
      len(fate_au) == 1 and (fate_au[0]["expect_qty"], fate_au[0]["receive_qty"], fate_au[0]["confirm_qty"]) == (38, 38, 38)
      and fate_au[0]["old_fate"] == "pending")
m = conn.execute("SELECT * FROM match_rows WHERE batch_id=? AND sku=?", (b["batch_id"], RICE)).fetchone()
check("S1 open 对齐物化已刷新为对齐", m["aligned"] == 1 and (m["expect_qty"], m["receive_qty"], m["confirm_qty"]) == (38, 38, 38))
b1 = conn.execute("SELECT * FROM reconcile_batches WHERE id=?", (b["batch_id"],)).fetchone()
check("S1 批次计数联动（对齐1/未对齐1）", b1["matched_count"] == 1 and b1["variance_count"] == 1)

v_over = variance_of(conn, b["batch_id"], TOWEL)
svc.dispose_variance(conn, v_over["id"], "accept_over")
check("S2 认溢后三腿一致（66/66/66）", sums(conn, D1, SUP, TOWEL) == (66, 66, 66), str(sums(conn, D1, SUP, TOWEL)))
au2 = audits_of(conn, v_over["id"])
legs2 = {a["leg"]: (a["old_sum"], a["new_sum"]) for a in au2 if a["leg"] != "fate"}
check("S2 认溢回写预报+回执双腿且旧值可查", legs2.get("expect") == (60, 66) and legs2.get("confirm") == (60, 66), str(legs2))
adj_e = conn.execute("SELECT 1 FROM expect_lines WHERE doc_no=?", (f"ADJ-{v_over['id']}-E",)).fetchone()
adj_c = conn.execute("SELECT 1 FROM confirm_lines WHERE confirm_no=?", (f"ADJ-{v_over['id']}-C",)).fetchone()
check("S2 双腿调整单均落库（+6）", adj_e and adj_c)

counts = svc.fate_counts(conn, D1)
check("S7 总览终态计数与处置一致", counts == {"pending": 0, "accept_short": 1, "accept_over": 1, "quarantine": 0}, str(counts))

sealed = svc.seal_day(conn, D1)
check("S3 结案快照带命运计数",
      sealed["status"] == "sealed"
      and sealed["sealed_accept_short"] == 1 and sealed["sealed_accept_over"] == 1 and sealed["sealed_quarantine"] == 0,
      str(sealed))
check("S3 结案后当日写闸生效", is_sealed(conn, D1))
try:
    assert_writable(conn, D1)
    check("S3 结案后三腿写闸拒绝", False)
except ValueError:
    check("S3 结案后三腿写闸拒绝", True)
# 结案后回写被拒：人为把一条差额翻回 pending 模拟闸内拦截
conn.execute("UPDATE variance_rows SET fate='pending' WHERE id=?", (v_short["id"],))
conn.commit()
try:
    svc.dispose_variance(conn, v_short["id"], "accept_over")
    check("S3 已封后回写被拒", False)
except ValueError as e:
    check("S3 已封后回写被拒", "已结案" in str(e), str(e))
check("S3 拒写后三腿未被改", sums(conn, D1, SUP, RICE) == (38, 38, 38))
conn.execute("UPDATE variance_rows SET fate='accept_short' WHERE id=?", (v_short["id"],))
conn.commit()
try:
    svc.run_reconcile(conn, D1)
    check("S3 结案后禁止重跑", False)
except ValueError:
    check("S3 结案后禁止重跑", True)
try:
    svc.dispose_variance(conn, v_short["id"], "quarantine")
    check("S3 终态不可再改", False)
except ValueError as e:
    check("S3 终态不可再改", "终态" in str(e), str(e))
conn.close()

# ---------- S4：回写失败整笔命运回滚 ----------
print("== S4 回写失败整笔回滚 ==")
conn = fresh_db()
D2 = "2026-11-19"
leg(conn, "expect_lines", "doc_no", "EX-9", SUP, RICE, D2, 40)
leg(conn, "receive_lines", "receive_no", "RC-9", SUP, RICE, D2, 38)
leg(conn, "confirm_lines", "confirm_no", "CF-9", SUP, RICE, D2, 38)
conn.commit()
b2 = svc.run_reconcile(conn, D2)
v = variance_of(conn, b2["batch_id"], RICE)
# 预置冲突调整单号，让回写 INSERT 在事务中段炸掉
leg(conn, "expect_lines", "doc_no", f"ADJ-{v['id']}-E", SUP, RICE, D2, 0)
conn.commit()
try:
    svc.dispose_variance(conn, v["id"], "accept_short")
    check("S4 冲突应抛错", False)
except Exception:
    check("S4 冲突应抛错", True)
v2 = conn.execute("SELECT * FROM variance_rows WHERE id=?", (v["id"],)).fetchone()
check("S4 命运整笔回滚仍为 pending", v2["fate"] == "pending" and v2["disposed_at"] is None)
check("S4 无孤儿审计", len(audits_of(conn, v["id"])) == 0)
check("S4 腿数量未被半截改写", sums(conn, D2, SUP, RICE) == (40, 38, 38), str(sums(conn, D2, SUP, RICE)))
m2 = conn.execute("SELECT * FROM match_rows WHERE batch_id=? AND sku=?", (b2["batch_id"], RICE)).fetchone()
check("S4 物化未半截刷新", m2["aligned"] == 0 and m2["expect_qty"] == 40)
# 冲突源解除（删掉预置占位单）后可正常重试并成功
conn.execute("DELETE FROM expect_lines WHERE doc_no=?", (f"ADJ-{v['id']}-E",))
conn.commit()
svc.dispose_variance(conn, v["id"], "accept_short")
check("S4 失败重试后三腿一致", sums(conn, D2, SUP, RICE) == (38, 38, 38))
conn.close()

# ---------- S5：并发处置同一差额 ----------
print("== S5 并发处置同一差额 ==")
conn = fresh_db()
D3 = "2026-11-20"
leg(conn, "expect_lines", "doc_no", "EX-5", SUP, RICE, D3, 40)
leg(conn, "receive_lines", "receive_no", "RC-5", SUP, RICE, D3, 38)
leg(conn, "confirm_lines", "confirm_no", "CF-5", SUP, RICE, D3, 38)
conn.commit()
b3 = svc.run_reconcile(conn, D3)
vid = variance_of(conn, b3["batch_id"], RICE)["id"]
conn.close()

barrier = threading.Barrier(2)
results: list[tuple[str, object]] = []

def race(fate):
    c = db.connect()
    barrier.wait()
    try:
        svc.dispose_variance(c, vid, fate)
        results.append(("ok", fate))
    except Exception as e:  # noqa: BLE001
        results.append(("err", str(e)))
    finally:
        c.close()

t1 = threading.Thread(target=race, args=("accept_short",))
t2 = threading.Thread(target=race, args=("accept_over",))
t1.start(); t2.start(); t1.join(); t2.join()

conn = db.connect()
oks = [r for r in results if r[0] == "ok"]
check("S5 并发只有一笔成功", len(oks) == 1 and len(results) == 2, str(results))
v5 = conn.execute("SELECT * FROM variance_rows WHERE id=?", (vid,)).fetchone()
check("S5 命运为唯一终态", v5["fate"] in ("accept_short", "accept_over"))
adj_n = conn.execute("SELECT COUNT(*) c FROM expect_lines WHERE doc_no=?", (f"ADJ-{vid}-E",)).fetchone()["c"]
check("S5 调整单不双写", adj_n == 1, f"adj={adj_n}")
check("S5 审计命运行唯一", len([a for a in audits_of(conn, vid) if a["leg"] == "fate"]) == 1)
check("S5 并发后三腿一致", sums(conn, D3, SUP, RICE) == (38, 38, 38), str(sums(conn, D3, SUP, RICE)))
conn.close()

# ---------- S6：隔离不回写 + 重跑终态保留规则 ----------
print("== S6 隔离不回写 + 重跑终态保留 ==")
conn = fresh_db()
D4 = "2026-11-21"
leg(conn, "expect_lines", "doc_no", "EX-6", SUP, RICE, D4, 40)
leg(conn, "receive_lines", "receive_no", "RC-6", SUP, RICE, D4, 38)
leg(conn, "confirm_lines", "confirm_no", "CF-6", SUP, RICE, D4, 38)
conn.commit()
b4 = svc.run_reconcile(conn, D4)
v6 = variance_of(conn, b4["batch_id"], RICE)
svc.dispose_variance(conn, v6["id"], "quarantine")
check("S6 隔离只标命运、腿数量不变", sums(conn, D4, SUP, RICE) == (40, 38, 38))
check("S6 隔离无调整单", conn.execute("SELECT COUNT(*) c FROM expect_lines WHERE doc_no LIKE 'ADJ-%'").fetchone()["c"] == 0)
check("S6 隔离也有审计命运行", len([a for a in audits_of(conn, v6["id"]) if a["leg"] == "fate"]) == 1)

b4b = svc.run_reconcile(conn, D4)  # 数量未变重跑：终态保留
v6b = variance_of(conn, b4b["batch_id"], RICE)
check("S6 重跑保留隔离终态", v6b["fate"] == "quarantine" and v6b["disposed_at"], v6b["fate"])
check("S6 重跑后未决为 0", svc.fate_counts(conn, D4)["pending"] == 0)

leg(conn, "receive_lines", "receive_no", "RC-6B", SUP, RICE, D4, 2)  # 数量变了：视为新差额
conn.commit()
b4c = svc.run_reconcile(conn, D4)
v6c = variance_of(conn, b4c["batch_id"], RICE)
check("S6 数量变更后重跑回到 pending", v6c["fate"] == "pending", v6c["fate"])
conn.close()

# ---------- S6b：认短后重跑不复活差额、终态计数不丢 ----------
print("== S6b 回写后重跑：差额不复活、终态计数不丢、可结案 ==")
conn = fresh_db()
D5 = "2026-11-22"
leg(conn, "expect_lines", "doc_no", "EX-7", SUP, RICE, D5, 40)
leg(conn, "receive_lines", "receive_no", "RC-7", SUP, RICE, D5, 38)
leg(conn, "confirm_lines", "confirm_no", "CF-7", SUP, RICE, D5, 38)
conn.commit()
b5 = svc.run_reconcile(conn, D5)
v7 = variance_of(conn, b5["batch_id"], RICE)
svc.dispose_variance(conn, v7["id"], "accept_short")
b5b = svc.run_reconcile(conn, D5)
check("S6b 回写后重跑差额不复活", b5b["variance_count"] == 0 and b5b["matched_count"] == 1)
check("S6b 重跑后终态计数仍在", svc.fate_counts(conn, D5)["accept_short"] == 1)
s5 = svc.seal_day(conn, D5)
check("S6b 重跑后可结案且快照带命运计数", s5["status"] == "sealed" and s5["sealed_accept_short"] == 1, str(s5))
conn.close()

# ---------- S8：六链夹具（终态行拧 / 行齐未决 / 扫描吞终态 / 交错半截） ----------
# 四条夹具都必须让 chain_problems 报出问题并被结案闸挡下；
# 走完正确处置 + 再次扫描后，六链必须归零且结案证据/看板/列表同口径。
print("== S8 六链夹具：分叉必须被识别，收口后六链归零 ==")

def seed_day(conn, day, eq, rq, cq):
    leg(conn, "expect_lines", "doc_no", f"EX-{day}", SUP, RICE, day, eq)
    leg(conn, "receive_lines", "receive_no", f"RC-{day}", SUP, RICE, day, rq)
    leg(conn, "confirm_lines", "confirm_no", f"CF-{day}", SUP, RICE, day, cq)
    conn.commit()

def batch_of(conn):
    return conn.execute(
        "SELECT * FROM reconcile_batches ORDER BY id DESC LIMIT 1"
    ).fetchone()

# S8a 终态行拧：标记已终态，但三行数量没回写（历史半截数据）
conn = fresh_db()
D6 = "2026-11-23"
seed_day(conn, D6, 40, 38, 38)
svc.run_reconcile(conn, D6)
v = variance_of(conn, batch_of(conn)["id"], RICE)
conn.execute("UPDATE variance_rows SET fate='accept_short', disposed_at='x' WHERE id=?", (v["id"],))
conn.commit()
p = svc.chain_problems(conn, D6)
check("S8a 终态行拧被识别为分叉", any("无任何处置痕迹" in x for x in p), str(p))
try:
    svc.seal_day(conn, D6)
    check("S8a 终态行拧禁止结案", False)
except ValueError as e:
    check("S8a 终态行拧禁止结案", "六链未对齐" in str(e), str(e))
# 现行架构下：把这条半截标记作废重开未决，再走正常处置，六链才收口
conn.execute("UPDATE variance_rows SET fate='pending', disposed_at=NULL WHERE id=?", (v["id"],))
conn.commit()
svc.dispose_variance(conn, v["id"], "accept_short")
check("S8a 重开后正常处置六链归零", svc.chain_problems(conn, D6) == [], str(svc.chain_problems(conn, D6)))
s = svc.seal_day(conn, D6)
check("S8a 收口后可结案且计数与看板一致",
      s["sealed_accept_short"] == svc.fate_counts(conn, D6)["accept_short"] == 1)
conn.close()

# S8b 行齐标记未决：三行已一致，差额行仍待处置
conn = fresh_db()
D7 = "2026-11-24"
seed_day(conn, D7, 40, 38, 38)
svc.run_reconcile(conn, D7)
v = variance_of(conn, batch_of(conn)["id"], RICE)
# 有人把预报腿补成实收数，但没重跑：三行齐了，差额行还 pending
leg(conn, "expect_lines", "doc_no", "EX-ADJ", SUP, RICE, D7, -2)
conn.commit()
p = svc.chain_problems(conn, D7)
check("S8b 行齐未决被识别", any("已行齐却仍记未决" in x for x in p), str(p))
try:
    svc.seal_day(conn, D7)
    check("S8b 行齐未决禁止结案", False)
except ValueError as e:
    check("S8b 行齐未决禁止结案", "未处置" in str(e) or "六链未对齐" in str(e), str(e))
# 再次扫描：数量已齐 => 新批次无差额，未决清零，六链归零
b = svc.run_reconcile(conn, D7)
p = svc.chain_problems(conn, D7)
check("S8b 再次扫描后差额消失且六链归零", b["variance_count"] == 0 and p == [], str(p))
s = svc.seal_day(conn, D7)
check("S8b 收口后可结案且看板未决为 0", s["status"] == "sealed" and svc.fate_counts(conn, D7)["pending"] == 0)
conn.close()

# S8c 扫描吞终态：先终态，再数量变更重跑 -> 旧实现会 fate 抹成 pending 且台账不认
conn = fresh_db()
D8 = "2026-11-25"
seed_day(conn, D8, 40, 38, 38)
svc.run_reconcile(conn, D8)
v = variance_of(conn, batch_of(conn)["id"], RICE)
svc.dispose_variance(conn, v["id"], "accept_short")
# 数量未变重跑：终态必须保留、台账不动、看板终态不重复加。
# 认短已把三腿回写齐，重跑不再产出差额行——这本身就是终态被保住的表现，
# 若不保住会被重新打成 pending 并复活差额行。
svc.run_reconcile(conn, D8)
vf_after = variance_of(conn, batch_of(conn)["id"], RICE)
check("S8c 数量未变重跑终态不被抹（不复活差额行）", vf_after is None)
check("S8c 重跑后看板终态仍为 1（不翻倍）", svc.fate_counts(conn, D8)["accept_short"] == 1,
      str(svc.fate_counts(conn, D8)))
# 数量变更重跑：旧终态被重开取代，看板不得在未决之外再加一次
leg(conn, "receive_lines", "receive_no", "RC-LATE", SUP, RICE, D8, 2)
conn.commit()
# 重跑之前的窗口：晚到货已改三腿，台账仍记终态 -> 必须报分叉并禁止结案，
# 而不是让看板把这条终态和拧着的三行一起封进证据。
p_pre = svc.chain_problems(conn, D8)
check("S8c 晚到货未重跑时六链报分叉", len(p_pre) > 0, str(p_pre))
try:
    svc.seal_day(conn, D8)
    check("S8c 晚到货未重跑禁止结案", False)
except ValueError as e:
    check("S8c 晚到货未重跑禁止结案", "六链未对齐" in str(e), str(e))
svc.run_reconcile(conn, D8)
vf = variance_of(conn, batch_of(conn)["id"], RICE)
c = svc.fate_counts(conn, D8)
check("S8c 数量变更后终态被未决取代", vf["fate"] == "pending" and c["pending"] == 1 and c["accept_short"] == 0, str(c))
p = svc.chain_problems(conn, D8)
check("S8c 数量变更后六链不分叉（未决不计终态）", p == [], str(p))
# 把新差额处置掉，六链收口
svc.dispose_variance(conn, vf["id"], "accept_short")
check("S8c 新差额处置后终态回到 1 且六链归零",
      svc.fate_counts(conn, D8)["accept_short"] == 1 and svc.chain_problems(conn, D8) == [])
conn.close()

# S8d 交错半截：处置中途失败 -> 命运/三行/痕迹/物化全回滚，再次扫描产出与原一致
conn = fresh_db()
D9 = "2026-11-26"
seed_day(conn, D9, 40, 38, 38)
svc.run_reconcile(conn, D9)
v = variance_of(conn, batch_of(conn)["id"], RICE)
leg(conn, "expect_lines", "doc_no", f"ADJ-{v['id']}-E", SUP, RICE, D9, 0)  # 占位制造冲突
conn.commit()
try:
    svc.dispose_variance(conn, v["id"], "accept_short")
    check("S8d 交错应抛错", False)
except Exception:
    check("S8d 交错应抛错", True)
check("S8d 半截后命运仍 pending", conn.execute("SELECT fate FROM variance_rows WHERE id=?", (v["id"],)).fetchone()["fate"] == "pending")
check("S8d 半截后三行未被改", sums(conn, D9, SUP, RICE) == (40, 38, 38), str(sums(conn, D9, SUP, RICE)))
check("S8d 半截后无痕迹", audits_of(conn, v["id"]) == [])
check("S8d 半截后物化未刷新", conn.execute(
    "SELECT expect_qty FROM match_rows WHERE batch_id=?", (batch_of(conn)["id"],)
).fetchone()["expect_qty"] == 40)
# 解除冲突再重试 -> 六链收口
conn.execute("DELETE FROM expect_lines WHERE doc_no=?", (f"ADJ-{v['id']}-E",))
conn.commit()
svc.dispose_variance(conn, v["id"], "accept_short")
check("S8d 重试后六链归零", svc.chain_problems(conn, D9) == [], str(svc.chain_problems(conn, D9)))
conn.close()

# ---------- 汇总 ----------
print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} 项 -> {FAILURES}")
    sys.exit(1)
print("ALL CHECKS PASSED")
