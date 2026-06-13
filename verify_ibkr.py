"""
verify_ibkr.py — 盈透 IBKR 重建對帳驗證(對映嘉信 P4b 的 verify.py)

四項守恆檢查:
  1) 現金守恆     :所有現金事件 Net Amount 加總 == ibkr.db 記錄的現金
  2) 淨值守恆     :現金 + 持倉成本 == 外部投入 + 已實現 + 收益(獨立重算)
  3) 持股獨立重算 :不依賴 build 的狀態,從 CSV 直接重算每檔持股 == positions_current
  4) 每日淨值無缺日:daily_networth_native 連續、無跳日

附加:IBKR 個股 PIL 閉環(持股 × 季配 ≈ PIL),抓出「收益對不上持倉」這類缺漏。

用法: python verify_ibkr.py [ibkr.db] [IBKR_TRANSACTIONS.csv]
"""
from __future__ import annotations

import sqlite3
import sys
from collections import defaultdict
from datetime import date, timedelta

import parse_ibkr_csv as P

TOL = 0.01  # 1 美分容差


def _independent_replay(events: list[P.Event]):
    """完全獨立於 build_history 的第二套重算(交叉驗證)。"""
    cash = realized = ext = income = 0.0
    qty = defaultdict(float)
    cost = defaultdict(float)
    for e in events:
        s = e.symbol or ""
        if e.kind == "split":
            if qty[s] > 1e-12:
                qty[s] *= e.qty
        elif e.kind == "buy":
            cash += e.amount; qty[s] += e.qty; cost[s] += -e.amount
        elif e.kind == "sell":
            cash += e.amount; sq = -e.qty
            avg = cost[s] / qty[s] if qty[s] else 0
            realized += e.amount - avg * sq
            qty[s] -= sq; cost[s] -= avg * sq
        elif e.kind in ("dividend", "pil", "interest", "tax"):
            cash += e.amount; income += e.amount
        elif e.kind in ("cash_in", "cash_out"):
            cash += e.amount; ext += e.amount
        elif e.kind in ("transfer_in", "transfer_out"):
            qty[s] += e.qty; cost[s] += e.amount; ext += e.amount
        elif e.kind == "award":
            qty[s] += e.qty; cost[s] += e.amount; income += e.amount
    return cash, realized, ext, income, qty, cost


def verify(db_path: str, csv_path: str) -> bool:
    events = P.parse_csv(csv_path)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    db_cash = float(cur.execute("SELECT value FROM meta WHERE key='cash'").fetchone()[0])
    db_pos = {r["symbol"]: (r["qty"], r["cost_basis"])
              for r in cur.execute("SELECT * FROM positions_current WHERE broker='ibkr'")}
    dn = [r["date"] for r in cur.execute(
        "SELECT date FROM daily_networth_native WHERE broker='ibkr' ORDER BY date")]

    cash, realized, ext, income, qty, cost = _independent_replay(events)
    cash_events = sum(e.amount for e in events if e.is_cash)
    cost_total = sum(c for s, c in cost.items() if abs(qty[s]) > 1e-6)

    ok = True

    def check(name, lhs, rhs, unit="$"):
        nonlocal ok
        diff = lhs - rhs
        passed = abs(diff) < TOL
        ok = ok and passed
        mark = "✓" if passed else "✗ 失敗"
        print(f"  [{mark}] {name}")
        print(f"        {lhs:,.4f} vs {rhs:,.4f}   差異 {unit}{diff:+.4f}")

    print("=" * 64)
    print(f"IBKR 對帳驗證  ({db_path})")
    print("=" * 64)

    print("\n① 現金守恆(現金事件 Net 加總 == 記錄現金)")
    check("cash", cash_events, db_cash)

    print("\n② 淨值守恆(現金 + 持倉成本 == 外部投入 + 已實現 + 收益)")
    check("networth", cash + cost_total, ext + realized + income)

    print("\n③ 持股獨立重算 == positions_current")
    syms = sorted(set(list(db_pos) + [s for s in qty if abs(qty[s]) > 1e-6]))
    for s in syms:
        q_re = qty.get(s, 0.0)
        q_db = db_pos.get(s, (0.0, 0.0))[0]
        passed = abs(q_re - q_db) < 1e-4
        ok = ok and passed
        print(f"  [{'✓' if passed else '✗ 失敗'}] {s:6s} 重算 {q_re:.4f} vs DB {q_db:.4f}")

    print("\n④ 每日淨值無缺日")
    if dn:
        d0 = date.fromisoformat(dn[0]); d1 = date.fromisoformat(dn[-1])
        expected = (d1 - d0).days + 1
        passed = (len(dn) == expected)
        ok = ok and passed
        print(f"  [{'✓' if passed else '✗ 失敗'}] {dn[0]} ~ {dn[-1]}  "
              f"{len(dn)} 天 / 應有 {expected} 天")
    else:
        ok = False
        print("  [✗ 失敗] 無每日淨值")

    print("\n附加:IBKR 個股 PIL 閉環(借券補償 ≈ 持股 × 季配)")
    ibkr_q = qty.get("IBKR", 0.0)
    pil_total = sum(e.amount for e in events if e.kind == "pil")
    pil_cnt = sum(1 for e in events if e.kind == "pil")
    if pil_cnt:
        implied = ibkr_q * 0.08 * pil_cnt   # IBKR 2025-26 季配約 $0.08
        print(f"        IBKR {ibkr_q:.4f} 股 × $0.08 × {pil_cnt} 季 ≈ ${implied:.2f}"
              f"  vs 實際 PIL ${pil_total:.2f}  "
              f"{'✓ 對得上' if abs(implied - pil_total) < 0.15 else '⚠ 偏差大,可能仍缺持倉'}")

    con.close()
    print("\n" + ("✅ 全部通過" if ok else "❌ 有檢查未通過,請見上方") + "\n")
    return ok


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else "ibkr.db"
    csv = sys.argv[2] if len(sys.argv) > 2 else None
    if csv is None:
        print("用法: python verify_ibkr.py <ibkr.db> <IBKR_TRANSACTIONS.csv>")
        sys.exit(1)
    sys.exit(0 if verify(db, csv) else 1)
