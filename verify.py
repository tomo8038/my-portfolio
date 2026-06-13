"""schwab.db 對帳驗證 — P4b(v4)。

獨立於 build_history.py 重新核對,確認重建沒有漏算/重算。

關卡(全部與市價無關,且不依賴券商的賣出批次法)──
  [1] 現金     :CSV 全部 Amount 加總 == 快照現金
  [2] 市值自洽 :淨值 == 現金 + 持倉市值(invested)
  [3] 持股     :CSV 獨立重算 == positions_current
  [4] 每日無缺日
  [5] 持倉成本 :CSV 獨立重播(加權平均)之「持倉總成本」== DB cost
               ← 剩餘持倉總成本與賣出批次法無關,故穩健

修正歷程:
  v1 [2] 用「成本==淨值」→ 本機真實價後差一個未實現,誤報。
  v2 [2] 改市值自洽;新增 [5] 收益分類 → 漏算實物轉撥,誤報。
  v3 [5] 改獨立重播比 成本+已實現 → 但「已實現」依券商賣出批次法(指定批次/FIFO)
     而定,用均價獨立重算必然不同,誤報。
  v4 [5] 只比「持倉成本」(與批次法無關);已實現一律以 DB 為準,僅參考呈現;
     [分解] 的實物轉撥改殘差推算,恆等式恆成立。
"""
import csv
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal

D0 = Decimal(0)

SHARE_TRADE_ACTIONS = {"Buy", "Sell", "Reinvest Shares", "Stock Split"}
TRANSFER_ACTIONS = {"Journal", "Security Transfer", "Internal Transfer",
                    "Journaled Shares"}
EXTERNAL_CASH_ACTIONS = {"Wire Received", "Wire Sent", "MoneyLink Deposit",
                         "MoneyLink Transfer", "Deposit", "Withdrawal",
                         "Withdraw", "Cash Transfer"}


def _money(s):
    s = (s or "").strip().replace("$", "").replace(",", "").replace('"', "")
    if not s:
        return D0
    neg = s.startswith("-")
    s = s.lstrip("-")
    return (Decimal("-1") if neg else Decimal(1)) * Decimal(s) if s else D0


def _qty(s):
    s = (s or "0").replace(",", "").strip()
    return Decimal(s) if s else D0


def _parse_date(s):
    s = (s or "").split(" as of ")[0].strip()[:10]
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _read_rows(csv_path):
    with open(csv_path, encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if r.get("Date")]
    rows.sort(key=lambda r: (_parse_date(r["Date"]) or date.max))
    return rows


def main():
    db = sys.argv[1] if len(sys.argv) > 1 else "schwab.db"
    csv_path = sys.argv[2] if len(sys.argv) > 2 else "schwab.csv"
    con = sqlite3.connect(db)
    ok = True
    rows = _read_rows(csv_path)

    # [1] 現金
    csv_cash = sum((_money(r["Amount"]) for r in rows), D0)
    db_cash = Decimal(str(con.execute("SELECT cash FROM snapshots").fetchone()[0]))
    d1 = abs(csv_cash - db_cash)
    print(f"[1] CSV Amount 加總 ${csv_cash:,.2f}  vs  快照現金 ${db_cash:,.2f}"
          f"  → 差 ${d1:.4f}  {'✓' if d1 < Decimal('0.01') else '✗'}")
    ok &= d1 < Decimal("0.01")

    # [2] 市值自洽
    nw, inv, cost = con.execute(
        "SELECT net_worth, invested, cost FROM snapshots").fetchone()
    nw, inv, cost = Decimal(str(nw)), Decimal(str(inv)), Decimal(str(cost))
    d2 = abs(nw - (db_cash + inv))
    print(f"[2] 淨值 ${nw:,.2f}  vs  現金+持倉市值 ${db_cash + inv:,.2f}"
          f"  → 差 ${d2:.4f}  {'✓' if d2 < Decimal('1') else '✗'}")
    ok &= d2 < Decimal("1")

    # 獨立重播(加權平均)— 供 [3][5] 與分解
    qty = defaultdict(lambda: D0)
    cost_lot = defaultdict(lambda: D0)
    realized_avg = D0          # 僅參考(均價法)
    income = D0

    def avg(sym):
        return cost_lot[sym] / qty[sym] if qty[sym] else D0

    for r in rows:
        a = (r.get("Action") or "").strip()
        sym = (r.get("Symbol") or "").strip()
        q = _qty(r.get("Quantity"))
        amt = _money(r.get("Amount"))
        if a in ("Buy", "Reinvest Shares"):
            qty[sym] += q; cost_lot[sym] += -amt
        elif a == "Stock Split":
            qty[sym] += q
        elif a == "Sell":
            removed = avg(sym) * q
            realized_avg += amt - removed
            qty[sym] -= q; cost_lot[sym] -= removed
        elif a in TRANSFER_ACTIONS:
            basis = amt if amt != 0 else (avg(sym) * q if q else D0)
            qty[sym] += q; cost_lot[sym] += basis
        elif a in EXTERNAL_CASH_ACTIONS:
            pass
        else:
            income += amt

    re_cost = sum((c for s, c in cost_lot.items()
                   if abs(qty[s]) > Decimal("0.0001")), D0)

    # [3] 持股
    csv_pos = {s: v for s, v in qty.items() if abs(v) > Decimal("0.0001")}
    db_pos = {s: Decimal(str(qd)) for s, qd in con.execute(
        "SELECT symbol, qty FROM positions_current")}
    mismatch = [s for s in set(csv_pos) | set(db_pos)
                if abs(csv_pos.get(s, D0) - db_pos.get(s, D0)) > Decimal("0.01")]
    print(f"[3] 持股獨立重算:CSV {len(csv_pos)} 檔 vs DB {len(db_pos)} 檔"
          f"  → 不符 {mismatch or '無'}  {'✓' if not mismatch else '✗'}")
    ok &= not mismatch

    # [4] 每日無缺日
    days = [r[0] for r in con.execute(
        "SELECT date FROM daily_networth ORDER BY date")]
    gaps = 0
    if days:
        a0 = date.fromisoformat(days[0]); b0 = date.fromisoformat(days[-1])
        gaps = (b0 - a0).days + 1 - len(days)
    print(f"[4] 每日淨值 {len(days)} 天({days[0]}~{days[-1]})"
          f"  缺 {gaps} 天  {'✓' if gaps == 0 else '✗'}")
    ok &= gaps == 0

    # [5] 持倉成本(與賣出批次法無關)
    dc = abs(re_cost - cost)
    p5 = dc < Decimal("1")
    print(f"[5] 獨立重播 持倉成本 ${re_cost:,.2f}  vs  DB ${cost:,.2f}"
          f"  → 差 ${dc:.4f}  {'✓' if p5 else '✗'}")
    ok &= p5

    # 已實現:以 DB 為準(歷史事實);均價法僅參考
    db_realized = Decimal(str(con.execute(
        "SELECT COALESCE(SUM(pnl),0) FROM realized_pnl").fetchone()[0]))
    if abs(realized_avg - db_realized) >= Decimal("1"):
        print(f"    ⚠ 已實現參考:均價法重算 ${realized_avg:,.2f} ≠ DB ${db_realized:,.2f}"
              f"(差 ${abs(realized_avg - db_realized):,.2f})——屬賣出批次法差異,非錯誤,不列入判定")

    # [分解] 純呈現:實物轉撥以殘差推算 → 恆等式恆成立
    dep = Decimal(str(con.execute(
        "SELECT COALESCE(SUM(amount),0) FROM transactions "
        "WHERE txn_type IN ('DEPOSIT','WITHDRAW')").fetchone()[0]))
    cost_basis_nw = db_cash + cost
    inkind = cost_basis_nw - dep - db_realized - income   # 殘差=淨實物轉撥
    unreal = inv - cost
    print(f"\n[分解] 現金+成本 ${cost_basis_nw:,.2f} = 淨入金 ${dep:,.2f} + 已實現 "
          f"${db_realized:,.2f} + 收益淨額 ${income:,.2f} + 實物轉撥(推算) ${inkind:,.2f}")
    print(f"       淨值 ${nw:,.2f} = 上式 + 未實現 ${unreal:,.2f}"
          + ("(成本遞補:未實現=0)" if abs(unreal) < 1 else ""))
    if abs(inkind) > Decimal("1"):
        print(f"       註:實物轉撥推算 ${inkind:,.2f}"
              f"(負值=淨轉出,如 QLD 由嘉信實物轉去 IBKR 的成本基礎)")

    con.close()
    print("\n" + ("✅ 全部守恆驗證通過" if ok else "❌ 有驗證未通過,請檢查"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
