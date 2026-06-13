"""
combine_us.py — 美股帳戶小計合併與對帳(嘉信 schwab.db + 盈透 ibkr.db,USD)

把多個「USD 原幣」帳戶 db 合併成單一美股小計,不碰 portfolio.db、不折 TWD。
用來在併入台股前,先獨立確認美股那一半算得對不對。

自動辨識 schema(因各 db 由不同階段工具產生,欄位命名可能略有差異):
  - 持倉表:找含 symbol + qty + (cost_basis 或 avg_cost) 的表(通常 positions_current)
  - 現金  :先讀 meta['cash'];否則用最後一天 daily 的 cash 欄
  - 每日淨值:找同時含 date 與 networth 欄的表(daily_networth_native 或 daily_networth)
  - broker 標籤:讀 meta['broker'],否則用檔名

用法:
  python combine_us.py schwab.db ibkr.db
  python combine_us.py schwab.db ibkr.db --by-symbol   # 額外列出同檔跨券商合併
"""
from __future__ import annotations

import os
import sqlite3
import sys
from collections import defaultdict
from datetime import date, timedelta


def _tables(cur) -> list[str]:
    return [r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]


def _cols(cur, table) -> list[str]:
    return [r[1] for r in cur.execute(f"PRAGMA table_info({table})")]


def _find_positions_table(cur):
    for t in _tables(cur):
        c = set(_cols(cur, t))
        if "symbol" in c and "qty" in c and ("cost_basis" in c or "avg_cost" in c):
            return t
    return None


def _find_daily_table(cur):
    for t in _tables(cur):
        c = set(_cols(cur, t))
        if "date" in c and "networth" in c:
            return t
    return None


def _meta(cur) -> dict:
    if "meta" in _tables(cur):
        try:
            return dict(cur.execute("SELECT key, value FROM meta").fetchall())
        except sqlite3.OperationalError:
            return {}
    return {}


def load_account(db_path: str) -> dict:
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    meta = _meta(cur)
    broker = meta.get("broker") or os.path.splitext(os.path.basename(db_path))[0]

    ptab = _find_positions_table(cur)
    positions = []
    if ptab:
        cols = _cols(cur, ptab)
        cb = "cost_basis" if "cost_basis" in cols else "avg_cost"
        where = " WHERE broker=?" if "broker" in cols else ""
        rows = cur.execute(
            f"SELECT symbol, qty, {cb} FROM {ptab}{where}",
            ((broker,) if where else ())).fetchall()
        for s, q, c in rows:
            if abs(q) > 1e-6:
                positions.append((s, q, c if cb == "cost_basis" else c * q))

    dtab = _find_daily_table(cur)
    daily = {}
    cash_from_daily = None
    if dtab:
        cols = _cols(cur, dtab)
        where = " WHERE broker=?" if "broker" in cols else ""
        cashcol = ", cash" if "cash" in cols else ""
        rows = cur.execute(
            f"SELECT date, networth{cashcol} FROM {dtab}{where} ORDER BY date",
            ((broker,) if where else ())).fetchall()
        for r in rows:
            daily[r[0]] = r[1]
        if "cash" in cols and rows:
            cash_from_daily = rows[-1][2]

    cash = float(meta["cash"]) if "cash" in meta else (cash_from_daily or 0.0)
    realized = float(meta["realized"]) if "realized" in meta else None

    con.close()
    return dict(db=db_path, broker=broker, positions=positions,
                cash=cash, realized=realized, daily=daily,
                ptab=ptab, dtab=dtab)


def combine(dbs: list[str], by_symbol: bool):
    accts = [load_account(d) for d in dbs]

    print("=" * 66)
    print("美股帳戶小計(USD 原幣,未折 TWD)")
    print("=" * 66)

    # 各帳戶 schema 偵測結果 + 小計(方便逐一對券商)
    grand_cash = grand_mkt = 0.0
    per_broker_final = {}
    for a in accts:
        pos_cost = sum(c for _, _, c in a["positions"])
        last_day = max(a["daily"]) if a["daily"] else None
        nw = a["daily"][last_day] if last_day else (a["cash"] + pos_cost)
        grand_cash += a["cash"]
        grand_mkt += nw
        per_broker_final[a["broker"]] = (last_day, nw)
        print(f"\n● {a['broker']}  ({a['db']})")
        print(f"    schema: 持倉表={a['ptab']}  每日表={a['dtab']}")
        print(f"    持倉 {len(a['positions'])} 檔,成本合計 ${pos_cost:,.2f}")
        print(f"    現金 ${a['cash']:,.2f}"
              + (f" · 已實現 ${a['realized']:,.2f}" if a['realized'] is not None else ""))
        print(f"    期末淨值({last_day}) ${nw:,.2f}  ← 對這個數字跟券商 App 顯示的帳戶價值")

    # 合併持倉(同檔跨券商:股數相加、成本相加)
    if by_symbol:
        agg = defaultdict(lambda: [0.0, 0.0, []])  # sym -> [qty, cost, [brokers]]
        for a in accts:
            for s, q, c in a["positions"]:
                agg[s][0] += q
                agg[s][1] += c
                agg[s][2].append(a["broker"])
        print("\n── 合併持倉(同檔跨券商已加總)──")
        for s in sorted(agg, key=lambda x: -agg[x][1]):
            q, c, brs = agg[s]
            tag = "+".join(sorted(set(brs)))
            print(f"    {s:6s} {q:>11.4f} 股  成本 ${c:>11.2f}  [{tag}]")

    # 合併每日 USD 曲線(各帳戶外連 join,缺日沿用前值)
    all_days = sorted({d for a in accts for d in a["daily"]})
    if all_days:
        d0 = date.fromisoformat(all_days[0])
        d1 = date.fromisoformat(all_days[-1])
        carry = {a["broker"]: 0.0 for a in accts}
        curve = []
        d = d0
        while d <= d1:
            iso = d.isoformat()
            total = 0.0
            for a in accts:
                if iso in a["daily"]:
                    carry[a["broker"]] = a["daily"][iso]
                total += carry[a["broker"]]
            curve.append((iso, total))
            d += timedelta(days=1)
        print(f"\n── 合併每日 USD 淨值曲線 ──")
        print(f"    期間 {curve[0][0]} ~ {curve[-1][0]}  ({len(curve)} 天)")
        print(f"    起始 ${curve[0][1]:,.2f}  →  期末 ${curve[-1][1]:,.2f}")

    print("\n" + "=" * 66)
    print(f"美股合計現金   ${grand_cash:,.2f}")
    print(f"美股合計淨值   ${grand_mkt:,.2f}   ← 對券商各自帳戶價值「加總」")
    print("=" * 66)

    print("""
對帳清單(這才是『值對不對』的真正檢查):
  [1] 各帳戶期末淨值 → 各自跟 Schwab / IBKR App 顯示的「Account Value / NLV」對
  [2] 合併持倉股數   → 各檔跟兩家券商 Positions 頁逐檔對(同檔有沒有加對)
  [3] 現金           → 跟券商 Cash / Settled Cash 對(留意 T+2 未交割款短暫差異)
  [4] 若市值是『成本遞補』版(沒抓到真實價),數字一定對不上 →
      先本機 `pip install yfinance` 重跑 build_history 補真實收盤再對
""")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    by_sym = "--by-symbol" in sys.argv
    if len(args) < 1:
        print(__doc__); sys.exit(1)
    missing = [a for a in args if not os.path.exists(a)]
    if missing:
        print("找不到 db:", ", ".join(missing)); sys.exit(1)
    combine(args, by_sym)
