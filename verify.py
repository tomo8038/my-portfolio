"""schwab.db 對帳驗證 — P4b。

獨立於 build_history.py 重新核對數字,確認重建沒有漏算/重算。
本機抓到真實市價後,「報酬分解」也會吻合(成本遞補時未實現=0,該式不適用)。

用法:python verify.py schwab.db schwab.csv
"""
import csv
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path


def _money(s):
    s = (s or "").strip().replace("$", "").replace(",", "").replace('"', "")
    if not s:
        return Decimal(0)
    neg = s.startswith("-")
    s = s.lstrip("-")
    return (Decimal("-1") if neg else Decimal(1)) * Decimal(s) if s else Decimal(0)


def main():
    db = sys.argv[1] if len(sys.argv) > 1 else "schwab.db"
    csv_path = sys.argv[2] if len(sys.argv) > 2 else "schwab.csv"
    con = sqlite3.connect(db)
    ok = True

    # ---- 驗證 1:CSV 全部 Amount 加總 = 快照現金 ----
    with open(csv_path, encoding="utf-8-sig") as f:
        csv_cash = sum(_money(r["Amount"]) for r in csv.DictReader(f)
                       if r.get("Date"))
    db_cash = Decimal(str(con.execute(
        "SELECT cash FROM snapshots").fetchone()[0]))
    d1 = abs(csv_cash - db_cash)
    print(f"[1] CSV Amount 加總 ${csv_cash:,.2f}  vs  快照現金 ${db_cash:,.2f}"
          f"  → 差 ${d1:.4f}  {'✓' if d1 < Decimal('0.01') else '✗'}")
    ok &= d1 < Decimal("0.01")

    # ---- 驗證 2:現金 + 持倉成本 = 淨值(成本基礎守恆)----
    nw, inv, cost = con.execute(
        "SELECT net_worth, invested, cost FROM snapshots").fetchone()
    # 成本基礎下 invested==cost(現價=均價);市值口徑下兩者不同
    d2 = abs(Decimal(str(nw)) - (db_cash + Decimal(str(cost))))
    print(f"[2] 現金+持倉成本 ${db_cash + Decimal(str(cost)):,.2f}  vs  "
          f"淨值 ${nw:,.2f}  → 差 ${d2:.4f}  {'✓' if d2 < Decimal('1') else '✗'}")
    ok &= d2 < Decimal("1")

    # ---- 驗證 3:持股非負,且與 CSV 獨立重算一致 ----
    from collections import defaultdict
    pos = defaultdict(Decimal)
    with open(csv_path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            sym = r["Symbol"].strip()
            if not sym:
                continue
            q = Decimal((r["Quantity"] or "0").replace(",", "") or "0")
            a = r["Action"]
            if a in ("Buy", "Reinvest Shares", "Stock Split"):
                pos[sym] += q
            elif a == "Sell":
                pos[sym] -= q
            elif a in ("Journal", "Security Transfer"):
                pos[sym] += q
    csv_pos = {s: v for s, v in pos.items() if abs(v) > Decimal("0.0001")}
    db_pos = {s: Decimal(str(q)) for s, q in con.execute(
        "SELECT symbol, qty FROM positions_current")}
    mismatch = []
    for s in set(csv_pos) | set(db_pos):
        if abs(csv_pos.get(s, Decimal(0)) - db_pos.get(s, Decimal(0))) > Decimal("0.01"):
            mismatch.append(s)
    print(f"[3] 持股獨立重算:CSV {len(csv_pos)} 檔 vs DB {len(db_pos)} 檔"
          f"  → 不符 {mismatch or '無'}  {'✓' if not mismatch else '✗'}")
    ok &= not mismatch

    # ---- 驗證 4:每日淨值連續、無缺日 ----
    days = [r[0] for r in con.execute(
        "SELECT date FROM daily_networth ORDER BY date")]
    from datetime import date
    gaps = 0
    if days:
        a = date.fromisoformat(days[0]); b = date.fromisoformat(days[-1])
        expect = (b - a).days + 1
        gaps = expect - len(days)
    print(f"[4] 每日淨值 {len(days)} 天({days[0]}~{days[-1]})"
          f"  缺 {gaps} 天  {'✓' if gaps == 0 else '✗'}")
    ok &= gaps == 0

    # ---- 報酬分解(市值口徑才適用;成本遞補時僅供參考)----
    dep = con.execute("SELECT COALESCE(SUM(amount),0) FROM transactions "
                      "WHERE txn_type IN ('DEPOSIT','WITHDRAW')").fetchone()[0]
    realized = con.execute("SELECT COALESCE(SUM(pnl),0) "
                           "FROM realized_pnl").fetchone()[0]
    unreal = inv - cost
    print(f"\n[參考] 報酬分解(本機有真實市價時應吻合):")
    print(f"       淨入金 ${dep:,.2f} + 已實現 ${realized:,.2f} "
          f"+ 未實現 ${unreal:,.2f} + 配息淨額")
    print(f"       未實現 = ${unreal:,.2f}"
          f"{'(成本遞補,故為 0;本機抓價後會有值)' if abs(unreal) < 1 else ''}")

    con.close()
    print("\n" + ("✅ 全部守恆驗證通過" if ok else "❌ 有驗證未通過,請檢查"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
