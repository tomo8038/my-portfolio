"""嘉信交易明細 → portfolio.db 結構的歷史重建引擎 — P4b。

從 parse_schwab_csv 的事件流:
  1. 正向重播,逐筆更新持股(加權平均成本)、現金、已實現損益
  2. 對每個「曾持有」的標的抓 yfinance 歷史價(market 口徑;抓不到以
     成本遞補),逐日估值 → daily_networth
  3. 寫入與既有系統相同的資料表:snapshots / daily_networth /
     positions_current / transactions / realized_pnl,讓 viewer 直接可讀

口徑:市值優先、抓不到價的標的當日以「持有成本」遞補(你選的混合口徑)。
已實現損益:賣出時 pnl =(賣出淨額 amount) -(賣出股數 × 加權平均成本)。
拆股:Stock Split 的 Quantity 是「新增股數」,直接加;成本不變(總成本
      不變、均價自動減半),歷史估值用「當時真實股數 × 當時未調整價」,
      故需把 yfinance 的分割調整價「還原」回未調整價(見 _unadjust_splits)。
基準幣:全部 USD;本檔輸出原幣 USD 的快照(broker='schwab')。若要與台股
        合併成 TWD,日後併入 portfolio.db 時再經 FXService 折算。
"""
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from parse_schwab_csv import (Event, parse_csv, is_cusip, yf_symbol,
                              KIND_BUY, KIND_SELL, KIND_SPLIT, KIND_TRANSFER,
                              KIND_JOURNAL, KIND_DIVIDEND, KIND_FEE,
                              KIND_DEPOSIT, KIND_WITHDRAW, KIND_REINVEST_SHARES)

BROKER = "schwab"
ACCOUNT = "individual"
ONE_DAY = timedelta(days=1)


# ====================================================================
# 1) 正向重播:逐筆更新持股 / 現金 / 已實現損益
# ====================================================================

class Lot:
    """單一標的的持股狀態(加權平均成本法)。"""
    __slots__ = ("qty", "cost", "name", "ccy")

    def __init__(self, name="", ccy="USD"):
        self.qty = Decimal(0)       # 目前股數
        self.cost = Decimal(0)      # 目前總成本(原幣)
        self.name = name
        self.ccy = ccy

    @property
    def avg(self) -> Decimal:
        return (self.cost / self.qty) if self.qty else Decimal(0)


def replay(events: list[Event]):
    """回傳 (positions_final, cash_final, realized_list, txn_list, daily_actions)

    daily_actions: {date: [Event,...]} 供逐日估值時反查當日持股變化。
    """
    lots: dict[str, Lot] = defaultdict(Lot)
    cash = Decimal(0)
    realized = []          # [(date, symbol, qty, price, pnl)]
    txns = []              # 統一交易(供寫入 transactions 表;含出入金)

    for e in events:
        sym = e.symbol
        lot = lots[sym] if sym else None
        if lot is not None and not lot.name:
            lot.name = e.name

        if e.action == KIND_BUY or e.action == KIND_REINVEST_SHARES:
            # 買進/再投資:股數+,總成本 +|amount|(amount 為負,取絕對值含費)
            spend = abs(e.amount) if e.amount != 0 else e.qty * e.price + e.fee
            lot.qty += e.qty
            lot.cost += spend
            cash += e.amount if e.amount != 0 else -spend
            txns.append(("BUY", e.date, sym, e.qty, e.price,
                         e.amount if e.amount != 0 else -spend))

        elif e.action == KIND_SELL:
            sell_qty = -e.qty           # e.qty 是負的
            proceeds = e.amount         # 正值(已扣費)
            avg = lot.avg
            cost_out = avg * sell_qty
            pnl = proceeds - cost_out
            lot.qty += e.qty            # 減少
            lot.cost -= cost_out
            if lot.qty < Decimal("0.0001"):
                lot.qty = Decimal(0); lot.cost = Decimal(0)
            cash += proceeds
            realized.append((e.date, sym, sell_qty, e.price, pnl))
            txns.append(("SELL", e.date, sym, sell_qty, e.price, proceeds))

        elif e.action == KIND_SPLIT:
            # 正向分割:Quantity = 新增股數,成本不變(均價自動下降)
            lot.qty += e.qty

        elif e.action == KIND_TRANSFER:
            # 證券 ACAT:股數±;轉入補成本(以當時市價估,缺價則 0),
            # 轉出按均價沖銷成本。不產生已實現損益(換券商不是賣出)。
            if e.qty >= 0:
                lot.qty += e.qty        # 轉入(本案無;保守處理)
            else:
                out = -e.qty
                lot.cost -= lot.avg * out
                lot.qty += e.qty
                if lot.qty < Decimal("0.0001"):
                    lot.qty = Decimal(0); lot.cost = Decimal(0)

        elif e.action == KIND_JOURNAL:
            # 成對 ±X,淨 0;逐筆套用即可(同日一進一出互相抵銷)
            lot.qty += e.qty

        elif e.action in (KIND_DIVIDEND, KIND_FEE):
            cash += e.amount            # 只動現金
            tt = "DIVIDEND" if e.action == KIND_DIVIDEND else "FEE"
            txns.append((tt, e.date, sym, Decimal(0), Decimal(0), e.amount))

        elif e.action == KIND_DEPOSIT:
            cash += e.amount
            txns.append(("DEPOSIT", e.date, sym, Decimal(0), Decimal(0),
                         e.amount))
        elif e.action == KIND_WITHDRAW:
            cash += e.amount            # amount 已為負
            txns.append(("WITHDRAW", e.date, sym, Decimal(0), Decimal(0),
                         e.amount))

    positions = {s: l for s, l in lots.items()
                 if abs(l.qty) > Decimal("0.0001")}
    return positions, cash, realized, txns


# ====================================================================
# 2) 歷史價格(market 口徑;抓不到以成本遞補)
# ====================================================================

def _holding_span(events):
    """每個標的「首次買入 ~ 今天」的日期範圍,決定抓價區間。"""
    first: dict[str, str] = {}
    for e in events:
        if e.symbol and e.action in (KIND_BUY, KIND_REINVEST_SHARES,
                                     KIND_TRANSFER, KIND_JOURNAL):
            first.setdefault(e.symbol, e.date)
    return first


def fetch_prices(symbols_spans: dict, end: str):
    """抓每個標的的歷史「未調整」收盤價 + yfinance 回報的分割 ex-date。

    回傳 (prices, yf_splits):
      prices    : {symbol: {date: close_未調整}}
      yf_splits : {symbol: [(date_iso, ratio), ...]}  yfinance 認定的分割生效日
    抓不到的標的不在 dict 中,由估值階段以成本遞補。
    """
    out: dict[str, dict] = {}
    yf_splits: dict[str, list] = {}
    factor_splits: dict[str, list] = {}
    try:
        import yfinance as yf
    except ImportError:
        print("[price] 未安裝 yfinance,全部標的將以成本遞補估值")
        return out, yf_splits, factor_splits

    for sym, start in symbols_spans.items():
        if is_cusip(sym):
            continue                    # 公債以面額計,不抓
        tk = yf_symbol(sym)
        try:
            df = yf.Ticker(tk).history(start=start, end=end,
                                       auto_adjust=False, actions=True)
        except Exception as ex:
            print(f"[price] 抓 {tk} 失敗(以成本遞補):{ex}")
            continue
        if df is None or df.empty:
            print(f"[price] {sym}({tk}) 無資料 → 以成本遞補")
            continue
        try:
            prices = {idx.strftime("%Y-%m-%d"): float(row["Close"])
                      for idx, row in df.iterrows()}
        except Exception as ex:
            print(f"[price] 解析 {tk} 價格失敗(以成本遞補):{ex}")
            continue
        out[sym] = prices
        if "Stock Splits" in df.columns:
            raw_sp = [(idx.strftime("%Y-%m-%d"), float(row["Stock Splits"]))
                      for idx, row in df.iterrows()
                      if float(row.get("Stock Splits", 0) or 0) not in (0.0, 1.0)]
            if raw_sp:
                closes_sorted = sorted(prices.items())
                aligned, factored = [], []
                for action_d, ratio in raw_sp:
                    drop_d = _detect_split_drop(closes_sorted, ratio, action_d)
                    if drop_d:
                        # 原始(未調整)價:在 drop_d 真的掉了 ratio 倍 →
                        # 把股數加倍對齊到掉價日,價格照用即連續。
                        aligned.append((drop_d, ratio))
                        print(f"[split] {sym} 原始價下跌日 {drop_d} → 股數對齊掉價日")
                    else:
                        # 連續價(yfinance 的 Close 已還原分割,常態如此)→
                        # 以事件日對齊股數加倍,並在估值時把『分割前股數』換算到
                        # 分割後基準(×ratio),否則分割前市值會少一半。
                        aligned.append((action_d, ratio))
                        factored.append((action_d, ratio))
                        print(f"[split] {sym} 連續價(已調分割)→ 分割前股數 ×{ratio:g} 換算到分割後基準")
                yf_splits[sym] = aligned
                if factored:
                    factor_splits[sym] = factored
        print(f"[price] {sym}({tk}):{len(prices)} 天")
    return out, yf_splits, factor_splits


def _detect_split_drop(closes_sorted, ratio: float, near_iso: str,
                       window: int = 7, tol: float = 0.15):
    """從『未調整原始收盤』序列找出價格真的掉了 ratio 倍的那一天(=真正 ex-date)。

    closes_sorted: [(date_iso, price)] 升冪。near_iso:yfinance 事件標記日(搜尋中心)。
    正向分割當天收盤約為前一交易日的 1/ratio;比對到就回該日,否則回 None。
    這能免疫「yfinance 事件日與原始價掉價日差一兩天」的情況。
    """
    near = datetime.strptime(near_iso, "%Y-%m-%d").date()
    best, best_gap = None, 10 ** 9
    for i in range(1, len(closes_sorted)):
        d_iso, px = closes_sorted[i]
        prev = closes_sorted[i - 1][1]
        if px <= 0 or prev <= 0:
            continue
        if abs((prev / px) / ratio - 1.0) <= tol:      # 前一日約為當日的 ratio 倍
            gap = abs((datetime.strptime(d_iso, "%Y-%m-%d").date() - near).days)
            if gap <= window and gap < best_gap:
                best, best_gap = d_iso, gap
    return best


def _sd(s: str):
    return datetime.strptime(s, "%Y-%m-%d").date()


def align_split_dates(events, yf_splits):
    """把 Stock Split 事件挪到 yfinance 回報的真實 ex-date,讓「股數加倍」與
    「價格減半」落在同一天 → 消除分割造成的單日假跳階(問題2)。
    無 yfinance 分割資料(離線)→ 保持原 CSV 日期,不退步。"""
    moved = 0
    for e in events:
        if e.action != KIND_SPLIT:
            continue
        cands = yf_splits.get(e.symbol) or []
        if not cands:
            continue
        best = min(cands, key=lambda dr: abs((_sd(dr[0]) - _sd(e.date)).days))
        if abs((_sd(best[0]) - _sd(e.date)).days) <= 7 and best[0] != e.date:
            print(f"[split] {e.symbol} 分割日對齊 yfinance:{e.date} → {best[0]}")
            e.date = best[0]
            moved += 1
    if moved:
        print(f"[split] 已對齊 {moved} 筆分割日(避免分割日淨值假跳階)")
    return events


def price_on(prices: dict, sym: str, d: str):
    """d 當天價;非交易日往前找最近 10 天;查無回 None。"""
    p = prices.get(sym)
    if not p:
        return None
    if d in p:
        return p[d]
    cur = datetime.strptime(d, "%Y-%m-%d").date()
    for _ in range(10):
        cur -= ONE_DAY
        if (k := cur.isoformat()) in p:
            return p[k]
    return None


# ====================================================================
# 3) 逐日估值 → 每日淨值(market 優先、成本遞補)
# ====================================================================

def _split_factor(sym: str, d_iso: str, factor_splits: dict) -> float:
    """連續價情境下,把『分割前(d < 分割日)』的股數換算到分割後基準:
    乘上該標的在 d 之後發生的所有分割比例。分割後的日子回 1.0。"""
    f = 1.0
    for sdate, ratio in factor_splits.get(sym, []):
        if sdate > d_iso:
            f *= ratio
    return f


def daily_networth(events, prices, end_date: str, factor_splits: dict | None = None):
    """正向逐日:重播到當日收盤後的持股/現金,估市值。

    回傳 [(date, networth, is_real)]。is_real=1 僅最後一天(今天,真實對帳),
    其餘為歷史估計(0)。factor_splits:連續價(已調分割)標的的分割表,估值時
    把分割前股數換算到分割後基準,避免分割前市值少一半。
    """
    factor_splits = factor_splits or {}
    # 預先把事件按日分組
    by_day: dict[str, list] = defaultdict(list)
    for e in events:
        by_day[e.date].append(e)

    start = events[0].date
    d = datetime.strptime(start, "%Y-%m-%d").date()
    last = datetime.strptime(end_date, "%Y-%m-%d").date()

    lots: dict[str, Lot] = defaultdict(Lot)
    cash = Decimal(0)
    series = []

    while d <= last:
        ds = d.isoformat()
        # 套用當天所有事件(到收盤後狀態)
        for e in by_day.get(ds, []):
            cash_ref = [cash]
            _apply(lots, e, cash_ref)
            cash = cash_ref[0]
        # 估值:市值優先,抓不到價的標的當日以持有成本遞補
        nv = cash
        for sym, lot in lots.items():
            if lot.qty <= Decimal("0.0001"):
                continue
            if is_cusip(sym):
                nv += lot.cost          # 公債以面額/成本計
                continue
            px = price_on(prices, sym, ds)
            if px is not None:
                f = _split_factor(sym, ds, factor_splits)   # 分割前股數換算到分割後基準
                nv += lot.qty * Decimal(str(px)) * Decimal(str(f))
            else:
                nv += lot.cost
        series.append((ds, float(nv), 1 if d == last else 0))
        d += ONE_DAY

    return series


def _apply(lots, e: Event, cash_ref):
    """估值用的輕量重播(與 replay 同邏輯,只更新持股與現金)。"""
    sym = e.symbol
    lot = lots[sym] if sym else None
    cash = cash_ref[0]
    if e.action in (KIND_BUY, KIND_REINVEST_SHARES):
        spend = abs(e.amount) if e.amount != 0 else e.qty * e.price + e.fee
        lot.qty += e.qty; lot.cost += spend
        cash += e.amount if e.amount != 0 else -spend
    elif e.action == KIND_SELL:
        sell_qty = -e.qty
        lot.cost -= lot.avg * sell_qty
        lot.qty += e.qty
        if lot.qty < Decimal("0.0001"):
            lot.qty = Decimal(0); lot.cost = Decimal(0)
        cash += e.amount
    elif e.action == KIND_SPLIT:
        lot.qty += e.qty
    elif e.action == KIND_TRANSFER:
        if e.qty < 0:
            lot.cost -= lot.avg * (-e.qty)
        lot.qty += e.qty
        if lot.qty < Decimal("0.0001"):
            lot.qty = Decimal(0); lot.cost = Decimal(0)
    elif e.action == KIND_JOURNAL:
        lot.qty += e.qty
    elif e.action in (KIND_DIVIDEND, KIND_FEE, KIND_DEPOSIT, KIND_WITHDRAW):
        cash += e.amount
    cash_ref[0] = cash


# ====================================================================
# 4) 寫入 schwab.db(與 portfolio.db 同結構)
# ====================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    ts TEXT PRIMARY KEY, base_ccy TEXT, net_worth REAL, invested REAL,
    cash REAL, cost REAL, breakdown TEXT);
CREATE TABLE IF NOT EXISTS daily_networth (
    date TEXT PRIMARY KEY, base_ccy TEXT, net_worth REAL, is_real INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS positions_current (
    broker TEXT, account_id TEXT, symbol TEXT, name TEXT, asset_class TEXT,
    qty REAL, avg_cost REAL, last_price REAL, ccy TEXT,
    market_value_native REAL, unrealized_pnl_native REAL, as_of TEXT,
    industry TEXT DEFAULT '', PRIMARY KEY (broker, account_id, symbol));
CREATE TABLE IF NOT EXISTS transactions (
    broker TEXT, external_id TEXT, symbol TEXT, txn_type TEXT, qty REAL,
    price REAL, amount REAL, ccy TEXT, trade_date TEXT,
    PRIMARY KEY (broker, external_id));
CREATE TABLE IF NOT EXISTS realized_pnl (
    broker TEXT, external_id TEXT, symbol TEXT, qty REAL, price REAL,
    pnl REAL, ccy TEXT, trade_date TEXT, PRIMARY KEY (broker, external_id));
CREATE TABLE IF NOT EXISTS snapshot_positions (
    ts TEXT, symbol TEXT, qty REAL, avg_cost REAL, ccy TEXT, last_price REAL,
    PRIMARY KEY (ts, symbol));
"""

_ETF = {"SGOV", "QLD", "QQQ", "QQQM", "TQQQ", "SSO", "VT", "VTI", "VXUS",
        "VGIT", "ARKK", "ARKF", "MCHI", "EWJ", "EWU", "IBIT"}


def asset_class(sym: str) -> str:
    if is_cusip(sym):
        return "bond"
    if sym in _ETF:
        return "etf"
    return "equity"


def build_db(csv_path: str, db_path: str, end_date: str | None = None):
    events = parse_csv(csv_path)
    end_date = end_date or max(events[-1].date, date.today().isoformat())
    print(f"[build] 解析 {len(events)} 筆事件,{events[0].date} ~ {end_date}")

    positions, cash, realized, txns = replay(events)
    print(f"[build] 重播完成:現金 ${cash:,.2f}、持倉 {len(positions)} 檔、"
          f"已實現 {len(realized)} 筆")

    spans = _holding_span(events)
    end_plus = (datetime.strptime(end_date, "%Y-%m-%d").date()
                + ONE_DAY).isoformat()
    print("[build] 抓歷史價(market 口徑,抓不到以成本遞補)...")
    prices, yf_splits, factor_splits = fetch_prices(spans, end_plus)
    # 分割日對齊(問題2修正):把 Stock Split 的股數加倍挪到 yfinance 的真實
    # ex-date,與價格減半同一天 → 消除 11/19 那種分割造成的單日假跳階。
    events = align_split_dates(events, yf_splits)

    # 當前持倉的現價(用最後一天的價;無則用均價→未實現損益顯示 0)
    today_px = {}
    for sym in positions:
        px = price_on(prices, sym, end_date)
        today_px[sym] = Decimal(str(px)) if px is not None else None

    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)

    # --- positions_current ---
    invested = Decimal(0)
    con.execute("DELETE FROM positions_current WHERE broker=?", (BROKER,))
    for sym, lot in sorted(positions.items()):
        last = today_px[sym] if today_px[sym] is not None else lot.avg
        mv = lot.qty * last
        invested += mv
        con.execute(
            "INSERT INTO positions_current VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (BROKER, ACCOUNT, sym, lot.name or sym, asset_class(sym),
             float(lot.qty), float(lot.avg), float(last), "USD",
             float(mv), float(mv - lot.cost),
             end_date + "T00:00:00", ""))

    cost_total = sum(l.cost for l in positions.values())
    net = invested + cash

    # --- snapshot(今天的真實對帳)---
    ts = end_date + "T00:00:00"
    breakdown = {
        "by_broker": {BROKER: float(net)},
        "by_asset_class": _group_mv(positions, today_px),
        "by_ccy": {"USD": float(net)},
        "cash": {"USD": float(cash)},
    }
    import json
    con.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?,?)",
                (ts, "USD", float(net), float(invested), float(cash),
                 float(cost_total), json.dumps(breakdown, ensure_ascii=False)))
    for sym, lot in positions.items():
        last = today_px[sym] if today_px[sym] is not None else lot.avg
        con.execute("INSERT OR REPLACE INTO snapshot_positions VALUES (?,?,?,?,?,?)",
                    (ts, sym, float(lot.qty), float(lot.avg), "USD",
                     float(last)))

    # --- transactions ---
    con.execute("DELETE FROM transactions WHERE broker=?", (BROKER,))
    seen = defaultdict(int)
    for tt, d, sym, q, px, amt in txns:
        key = f"{d}-{tt}-{sym}-{seen[(d,tt,sym)]}"
        seen[(d, tt, sym)] += 1
        con.execute("INSERT OR IGNORE INTO transactions VALUES (?,?,?,?,?,?,?,?,?)",
                    (BROKER, key, sym, tt, float(q), float(px), float(amt),
                     "USD", d))

    # --- realized_pnl ---
    con.execute("DELETE FROM realized_pnl WHERE broker=?", (BROKER,))
    rseen = defaultdict(int)
    for d, sym, q, px, pnl in realized:
        key = f"{d}-{sym}-{rseen[(d,sym)]}"
        rseen[(d, sym)] += 1
        con.execute("INSERT OR IGNORE INTO realized_pnl VALUES (?,?,?,?,?,?,?,?)",
                    (BROKER, key, sym, float(q), float(px), float(pnl),
                     "USD", d))

    # --- daily_networth(逐日估值)---
    print("[build] 逐日估值中...")
    series = daily_networth(events, prices, end_date, factor_splits)
    con.execute("DELETE FROM daily_networth")
    con.executemany("INSERT OR REPLACE INTO daily_networth VALUES (?,?,?,?)",
                    [(d, "USD", nv, r) for d, nv, r in series])

    con.commit()
    con.close()

    total_realized = sum(p for _, _, _, _, p in realized)
    print(f"[build] 寫入 {db_path}")
    print(f"        每日淨值 {len(series)} 天({series[0][0]} ~ {series[-1][0]})")
    print(f"        累計已實現損益 ${total_realized:,.2f}")
    return {
        "cash": cash, "invested": invested, "net": net,
        "cost": cost_total, "positions": positions,
        "realized_total": total_realized, "series": series,
        "prices_hit": [s for s in spans if s in prices],
        "prices_miss": [s for s in spans if s not in prices and not is_cusip(s)],
    }


def _group_mv(positions, today_px):
    out = defaultdict(float)
    for sym, lot in positions.items():
        last = today_px[sym] if today_px[sym] is not None else lot.avg
        out[asset_class(sym)] += float(lot.qty * last)
    return dict(out)


if __name__ == "__main__":
    csv_path = sys.argv[1] if len(sys.argv) > 1 else "schwab.csv"
    db_path = sys.argv[2] if len(sys.argv) > 2 else "schwab.db"
    build_db(csv_path, db_path)
