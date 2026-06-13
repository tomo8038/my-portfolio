"""
build_history_ibkr.py — 盈透 IBKR 歷史重建引擎

正向重播 parse_ibkr_csv 的事件流:
  1) 逐筆更新持股(加權平均成本法)、現金、已實現損益、外部資本流、收益
  2) 抓 yfinance 歷史收盤價(抓不到 / 無網路 → 以加權平均成本遞補,帳務精確、市值為近似)
  3) 逐日估值(USD 原幣)寫入 ibkr.db(與系統同結構,之後可併入 portfolio.db)

對映嘉信 P4b 的 build_history.py。所有金額為 USD 原幣;併入時才乘當日匯率折 TWD。

用法:
  python build_history_ibkr.py <IBKR_TRANSACTIONS.csv> [ibkr.db]
  python build_history_ibkr.py <csv> ibkr.db --no-prices         # 跳過抓價(純成本遞補)
  python build_history_ibkr.py <csv> ibkr.db --as-of 2026-06-13  # 每日淨值補到指定日(預設今天)
"""
from __future__ import annotations

import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

import parse_ibkr_csv as P


# ── 重播狀態 ────────────────────────────────────────────────────────────
@dataclass
class Lot:
    qty: float = 0.0
    cost: float = 0.0   # 總成本(非每股)

    @property
    def avg(self) -> float:
        return self.cost / self.qty if abs(self.qty) > 1e-12 else 0.0


@dataclass
class Replay:
    cash: float = 0.0
    realized: float = 0.0
    ext_cash: float = 0.0       # 外部現金出入金
    ext_inkind: float = 0.0     # 外部實物轉撥(投資人自有資產)
    income: float = 0.0         # 股息+利息+PIL+稅(淨)+贈股
    lots: dict[str, Lot] = field(default_factory=lambda: defaultdict(Lot))
    realized_rows: list[tuple] = field(default_factory=list)   # (date,sym,qty,proceeds,cost,pnl)
    txn_rows: list[tuple] = field(default_factory=list)        # 正規化交易明細

    def external_total(self) -> float:
        return self.ext_cash + self.ext_inkind


def replay(events: list[P.Event]) -> Replay:
    st = Replay()
    for e in events:
        k = e.kind
        sym = e.symbol or ""
        if k == "split":
            lot = st.lots.get(sym)
            if lot and lot.qty > 1e-12:
                lot.qty *= e.qty   # 股數 × 比率;cost 不變 → 均價自動調整
            continue

        if k == "buy":
            st.cash += e.amount                     # amount 為負(含手續費)
            lot = st.lots[sym]
            lot.qty += e.qty
            lot.cost += -e.amount                   # 投入成本含手續費
        elif k == "sell":
            st.cash += e.amount                     # amount 為正(已扣手續費)
            lot = st.lots[sym]
            sell_qty = -e.qty                        # qty 為負
            cost_out = lot.avg * sell_qty
            pnl = e.amount - cost_out
            st.realized += pnl
            st.realized_rows.append((e.date.isoformat(), sym, sell_qty,
                                     e.amount, cost_out, pnl))
            lot.qty -= sell_qty
            lot.cost -= cost_out
        elif k in ("dividend", "pil", "interest", "tax"):
            st.cash += e.amount
            st.income += e.amount
        elif k == "cash_in":
            st.cash += e.amount
            st.ext_cash += e.amount
        elif k == "cash_out":
            st.cash += e.amount
            st.ext_cash += e.amount
        elif k in ("transfer_in", "transfer_out"):
            lot = st.lots[sym]
            lot.qty += e.qty
            lot.cost += e.amount                    # amount=該批證券價值(成本基礎)
            st.ext_inkind += e.amount
        elif k == "award":
            lot = st.lots[sym]
            lot.qty += e.qty
            lot.cost += e.amount
            st.income += e.amount                   # 贈股=收益(計入績效,非外部投入)
        else:
            raise ValueError(f"重播未處理的事件: {k}")

        st.txn_rows.append((
            e.date.isoformat(), "ibkr", k, sym or None, e.qty, e.price,
            e.amount, int(e.is_cash), int(e.is_external), e.description,
        ))
    return st


def positions(st: Replay) -> dict[str, Lot]:
    return {s: l for s, l in st.lots.items() if abs(l.qty) > 1e-6}


# ── 歷史價 ──────────────────────────────────────────────────────────────
def fetch_prices(symbols: list[str], start: date, end: date) -> dict[str, dict[date, float]]:
    """抓每日收盤;無網路 / 抓不到 → 回空 dict(交給成本遞補)。"""
    out: dict[str, dict[date, float]] = {}
    try:
        import yfinance as yf  # noqa
    except Exception:
        print("  [price] 未安裝 yfinance,跳過抓價(改以成本遞補)")
        return out
    for sym in symbols:
        psym = P.price_symbol(sym)
        if P.is_face_value(sym):
            continue
        try:
            df = yf.Ticker(psym).history(start=start.isoformat(),
                                         end=(end + timedelta(days=2)).isoformat(),
                                         auto_adjust=False)
            series = {d.date(): float(c) for d, c in df["Close"].items()}
            if series:
                out[sym] = series
                print(f"  [price] {sym} ({psym}): {len(series)} 日")
            else:
                print(f"  [price] {sym}: 無資料,成本遞補")
        except Exception as ex:
            print(f"  [price] {sym}: 抓取失敗 ({ex}),成本遞補")
    return out


def price_on(prices: dict[date, float], day: date, fallback: float) -> float:
    """當日無報價 → 往前找最近可得收盤;再無 → fallback(成本均價)。"""
    if not prices:
        return fallback
    d = day
    for _ in range(10):
        if d in prices:
            return prices[d]
        d -= timedelta(days=1)
    past = [p for dd, p in prices.items() if dd <= day]
    return past[-1] if past else fallback


# ── 每日淨值 ─────────────────────────────────────────────────────────────
def daily_networth(events: list[P.Event], price_map, as_of: date | None = None) -> list[tuple]:
    """逐日:現金 + Σ(持股 × 當日收盤),USD 原幣。回傳 [(date, cash, holdings, networth)]。

    as_of:把曲線延伸到該日(預設今天)。最後一筆交易之後持股不變,
    每日仍以「當日收盤」重估市值 → 補齊 4/1 至今的每日帳戶變化。
    """
    day_events: dict[date, list[P.Event]] = defaultdict(list)
    for e in events:
        day_events[e.date].append(e)

    start = min(e.date for e in events if e.kind != "split")
    last_event = max(e.date for e in events if e.kind != "split")
    end = max(last_event, as_of) if as_of else last_event   # 延伸到 as_of

    st = Replay()
    rows: list[tuple] = []
    d = start
    # 預先把每個事件套用到當天「收盤後」狀態,再以當天收盤估值
    cur = 0
    ordered = sorted(events, key=lambda e: (e.date, 0 if e.kind == "split" else 1))
    while d <= end:
        for e in [x for x in ordered if x.date == d]:
            _apply_one(st, e)
        cash = st.cash
        holdings = 0.0
        for sym, lot in st.lots.items():
            if abs(lot.qty) < 1e-9:
                continue
            px = price_on(price_map.get(sym, {}), d, lot.avg)
            holdings += lot.qty * px
        rows.append((d.isoformat(), cash, holdings, cash + holdings))
        d += timedelta(days=1)
    return rows


def _apply_one(st: Replay, e: P.Event) -> None:
    """daily_networth 用的單事件套用(與 replay 同邏輯,精簡版)。"""
    k = e.kind
    sym = e.symbol or ""
    if k == "split":
        lot = st.lots.get(sym)
        if lot and lot.qty > 1e-12:
            lot.qty *= e.qty
        return
    if k == "buy":
        st.cash += e.amount; st.lots[sym].qty += e.qty; st.lots[sym].cost += -e.amount
    elif k == "sell":
        st.cash += e.amount; lot = st.lots[sym]; sq = -e.qty
        co = lot.avg * sq; st.realized += e.amount - co
        lot.qty -= sq; lot.cost -= co
    elif k in ("dividend", "pil", "interest", "tax", "cash_in", "cash_out"):
        st.cash += e.amount
    elif k in ("transfer_in", "transfer_out", "award"):
        st.lots[sym].qty += e.qty; st.lots[sym].cost += e.amount


# ── 寫入 ibkr.db ─────────────────────────────────────────────────────────
DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS transactions (
    date TEXT, broker TEXT, kind TEXT, symbol TEXT, qty REAL, price REAL,
    amount REAL, is_cash INTEGER, is_external INTEGER, description TEXT);
CREATE TABLE IF NOT EXISTS positions_current (
    broker TEXT, symbol TEXT, qty REAL, avg_cost REAL, cost_basis REAL,
    PRIMARY KEY (broker, symbol));
CREATE TABLE IF NOT EXISTS realized_pnl (
    date TEXT, broker TEXT, symbol TEXT, qty REAL, proceeds REAL, cost REAL, pnl REAL);
CREATE TABLE IF NOT EXISTS daily_networth_native (
    date TEXT PRIMARY KEY, broker TEXT, cash REAL, holdings REAL, networth REAL);
CREATE TABLE IF NOT EXISTS price_cache (
    symbol TEXT, date TEXT, close REAL, PRIMARY KEY (symbol, date));
"""


def write_db(path: str, st: Replay, dn_rows: list[tuple], price_map) -> None:
    con = sqlite3.connect(path)
    con.executescript(DDL)
    cur = con.cursor()
    for t in ("transactions", "positions_current", "realized_pnl",
              "daily_networth_native", "price_cache", "meta"):
        cur.execute(f"DELETE FROM {t} WHERE 1=1"
                    + (" AND broker='ibkr'" if t in
                       ("transactions", "positions_current", "realized_pnl",
                        "daily_networth_native") else ""))

    cur.executemany("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?)", st.txn_rows)
    pos = positions(st)
    cur.executemany("INSERT OR REPLACE INTO positions_current VALUES (?,?,?,?,?)",
                    [("ibkr", s, l.qty, l.avg, l.cost) for s, l in pos.items()])
    cur.executemany("INSERT INTO realized_pnl VALUES (?,?,?,?,?,?,?)",
                    [(d, "ibkr", s, q, p, c, pl) for (d, s, q, p, c, pl) in st.realized_rows])
    cur.executemany("INSERT OR REPLACE INTO daily_networth_native VALUES (?,?,?,?,?)",
                    [(d, "ibkr", cash, h, nw) for (d, cash, h, nw) in dn_rows])
    for sym, series in price_map.items():
        cur.executemany("INSERT OR REPLACE INTO price_cache VALUES (?,?,?)",
                        [(sym, d.isoformat(), c) for d, c in series.items()])
    meta = {
        "broker": "ibkr", "currency": "USD",
        "cash": f"{st.cash:.6f}", "realized": f"{st.realized:.6f}",
        "ext_cash": f"{st.ext_cash:.6f}", "ext_inkind": f"{st.ext_inkind:.6f}",
        "income": f"{st.income:.6f}",
        "final_networth": f"{st.cash + sum(l.cost for l in pos.values()):.6f}",
    }
    cur.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", list(meta.items()))
    con.commit()
    con.close()


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    csv_path = sys.argv[1]
    db_path = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("--") else "ibkr.db"
    no_prices = "--no-prices" in sys.argv

    # --as-of YYYY-MM-DD:每日淨值補到該日(預設今天);最後一筆交易後持股不變、逐日重估
    as_of = date.today()
    for i, a in enumerate(sys.argv):
        if a == "--as-of" and i + 1 < len(sys.argv):
            as_of = date.fromisoformat(sys.argv[i + 1])

    print(f"解析 {csv_path} …")
    events = P.parse_csv(csv_path)
    st = replay(events)
    pos = positions(st)

    syms = sorted({e.symbol for e in events if e.symbol})
    start = min(e.date for e in events if e.kind != "split")
    last_event = max(e.date for e in events if e.kind != "split")
    end = max(last_event, as_of)
    price_map = {} if no_prices else fetch_prices(syms, start, end)
    print(f"計算每日淨值(延伸至 {as_of})…")
    dn_rows = daily_networth(events, price_map, as_of=as_of)

    print(f"寫入 {db_path} …")
    write_db(db_path, st, dn_rows, price_map)

    print("\n── 重建摘要(USD 原幣)──")
    print(f"  期間        {start} ~ {end}  ({len(dn_rows)} 天)")
    print(f"  現金        ${st.cash:,.4f}")
    for s, l in sorted(pos.items()):
        print(f"  {s:5s}       {l.qty:>11.4f} 股  成本 ${l.cost:>11.2f}  均價 ${l.avg:.4f}")
    print(f"  已實現損益  ${st.realized:,.2f}")
    print(f"  外部投入    現金 ${st.ext_cash:,.2f} + 實物 ${st.ext_inkind:,.2f} = ${st.external_total():,.2f}")
    nw = dn_rows[-1]
    note = "(市值含真實報價)" if price_map else "(市值=成本遞補,需本機 yfinance 補真實價)"
    print(f"  期末淨值    現金 ${nw[1]:,.2f} + 持倉 ${nw[2]:,.2f} = ${nw[3]:,.2f} {note}")
    if as_of > last_event:
        print(f"  注意:最後交易 {last_event},曲線已延伸至 {as_of}"
              f"(持股不變、逐日以收盤重估){'' if price_map else ';成本遞補時此段為平線'}")
    print(f"\n  → 已寫入 {db_path}。執行 `python verify_ibkr.py {db_path}` 做守恆驗證。")


if __name__ == "__main__":
    main()
