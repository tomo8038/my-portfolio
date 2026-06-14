"""對帳單匯入核心 — P4d。

職責:
  1. build_positions(): 重播結果 → Position 物件(含現價、資產類別判別)
  2. write_to_db(): 寫 positions_current / transactions / broker_cash / snapshot_positions
  3. daily_history(): 順向、逐日估值(當日原始收盤 × 當日匯率),產生整體淨值曲線
     —— 因為「當日股數 × 當日原始價」同處當期基礎,跨拆股也正確,毋須調整價。

需要 yfinance 的部分(現價、歷史價、匯率)都做成可注入,離線可測。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from core.models import Position, Transaction
from core.statements import Statement, ReplayResult, replay

ONE_DAY = timedelta(days=1)

# 寫進 transactions 表的類型(回補/ TWR 需要的「真現金流 + 股數變動」)
_TXN_TYPE = {
    "BUY": "BUY", "REINVEST_BUY": "BUY", "SELL": "SELL",
    "DIVIDEND": "DIVIDEND", "FEE": "FEE", "INTEREST": "DIVIDEND",
    "DEPOSIT": "DEPOSIT", "WITHDRAW": "WITHDRAW", "CASH_IN_LIEU": "DIVIDEND",
    # TRANSFER_*/AWARD/SPLIT_ADD 屬「自有帳戶間搬移/非現金」,不入 transactions
}

_ETF_HINT = ("ETF", "TRUST", "FUND", "正2", "反1", "BITCOIN", "TREASURY")


def _asset_class(symbol: str, name: str) -> str:
    up = f"{symbol} {name}".upper()
    if any(h.upper() in up for h in _ETF_HINT):
        return "etf"
    return "equity"


def build_positions(stmt: Statement, res: ReplayResult,
                    prices: dict[str, float] | None = None,
                    as_of: str | None = None) -> list[Position]:
    """重播持倉 → Position 清單。prices: {symbol: 現價};缺則用均價遞補。"""
    prices = prices or {}
    asof_dt = datetime.fromisoformat(as_of) if as_of else datetime.now()
    out: list[Position] = []
    for sym, h in res.holdings.items():
        name = res.names.get(sym, sym)
        last = Decimal(str(prices.get(sym, float(h.avg_cost))))
        out.append(Position(
            broker=stmt.broker, account_id="stmt", symbol=sym, name=name,
            asset_class=_asset_class(sym, name),
            qty=h.qty, avg_cost=h.avg_cost, last_price=last,
            ccy=stmt.ccy, as_of=asof_dt,
        ))
    return out


def build_transactions(stmt: Statement) -> list[Transaction]:
    out: list[Transaction] = []
    for e in stmt.events:
        tt = _TXN_TYPE.get(e.etype)
        if tt is None:
            continue
        out.append(Transaction(
            broker=stmt.broker, external_id=e.ext_id, symbol=e.symbol,
            txn_type=tt, qty=e.qty, price=e.price, amount=e.amount,
            ccy=stmt.ccy, trade_date=e.date,
        ))
    return out


def write_to_db(db, stmt: Statement, positions: list[Position],
                txns: list[Transaction], cash: float,
                record_cash: bool) -> dict:
    """整批寫入單一券商的持倉 / 交易 / 現金。"""
    from core import aggregate
    db.replace_positions(stmt.broker, positions)
    n_txn = db.insert_transactions(txns)
    if record_cash:
        aggregate.set_broker_cash(db, stmt.broker, stmt.ccy, cash,
                                  date.today().isoformat())
    return {"positions": len(positions), "txns_new": n_txn,
            "cash": cash if record_cash else None}


# ====================================================================
# 順向、逐日歷史估值 — 產生整體(含美股)每日淨值曲線
# ====================================================================

def _holdings_by_day(events, splits) -> tuple[list[str], dict]:
    """回傳(排序後日期, {date: {symbol: qty}})。每天為「當日收盤後」持股(當期基礎)。"""
    # 逐筆套用,記錄每個「有變動的日子」收盤後的持股快照
    from collections import defaultdict
    pend = {s: sorted(l) for s, l in (splits or {}).items()}
    qty: dict[str, float] = defaultdict(float)

    def apply_splits(sym, d):
        keep = []
        for sd, ratio in pend.get(sym, []):
            if sd <= d and qty[sym]:
                qty[sym] *= float(ratio)
            elif sd > d:
                keep.append((sd, ratio))
        if sym in pend:
            pend[sym] = keep

    timeline: dict[str, dict[str, float]] = {}
    for e in sorted(events, key=lambda x: x.date):
        if e.symbol:
            apply_splits(e.symbol, e.date)
        if e.etype in ("BUY", "REINVEST_BUY", "TRANSFER_IN", "AWARD"):
            qty[e.symbol] += float(e.qty)
        elif e.etype in ("SELL", "TRANSFER_OUT"):
            qty[e.symbol] -= float(e.qty)
        elif e.etype == "SPLIT_ADD":
            qty[e.symbol] += float(e.qty)
        timeline[e.date] = {s: q for s, q in qty.items() if abs(q) > 1e-9}
    days = sorted(timeline)
    return days, timeline


def daily_history(statements: list[tuple[Statement, dict]],
                  price_on, fx_on, base_ccy: str = "TWD",
                  start: str | None = None, end: str | None = None
                  ) -> list[tuple[str, float]]:
    """多券商合併的每日淨值(基準幣別)。

    statements: [(stmt, split_map), ...]
    price_on(symbol, ccy, date) -> 當日原始收盤價(原幣)或 None
    fx_on(ccy, date) -> 對 base 的匯率
    回傳 [(date, net_worth)],涵蓋 start..end 每一天。
    """
    # 每個券商各自算出「每天的持股」與「每天的累積現金」
    per_broker = []
    all_start = None
    for stmt, splits in statements:
        days, timeline = _holdings_by_day(stmt.events, splits)
        # 累積現金(逐日)
        cash_by_day: dict[str, float] = {}
        running = 0.0
        from core.statements import replay as _r  # 借用 NON_CASH 規則
        NON_CASH = {"TRANSFER_IN", "TRANSFER_OUT", "AWARD", "SPLIT_ADD"}
        for e in sorted(stmt.events, key=lambda x: x.date):
            if e.etype not in NON_CASH:
                running += float(e.amount)
            cash_by_day[e.date] = running
        if not stmt.cash_is_real:
            cash_by_day = {}            # 對帳單無出入金 → 不採計推得現金
        per_broker.append((stmt, timeline, cash_by_day,
                           sorted(timeline), sorted(cash_by_day)))
        s0 = stmt.start_date
        all_start = s0 if all_start is None else min(all_start, s0)

    start = start or all_start
    end = end or date.today().isoformat()
    if not start:
        return []

    out: list[tuple[str, float]] = []
    cur = datetime.strptime(start, "%Y-%m-%d").date()
    endd = datetime.strptime(end, "%Y-%m-%d").date()
    # 快取「截至某日」的最後持股/現金,避免每天重掃
    while cur <= endd:
        d = cur.isoformat()
        nv = 0.0
        for stmt, timeline, cash_by_day, days, cash_days in per_broker:
            # 取 <= d 的最後一個有紀錄日
            hold = _asof(timeline, days, d)
            cash = _asof(cash_by_day, cash_days, d)
            rate = fx_on(stmt.ccy, d)
            if cash:
                nv += cash * rate
            if hold:
                for sym, q in hold.items():
                    px = price_on(sym, stmt.ccy, d)
                    if px:
                        nv += q * px * rate
        out.append((d, round(nv, 2)))
        cur += ONE_DAY
    return out


def _asof(mapping: dict, sorted_keys: list[str], d: str):
    """回傳 mapping 中 key <= d 的最後一筆值;無則 None/0。"""
    import bisect
    i = bisect.bisect_right(sorted_keys, d)
    if i == 0:
        return None if mapping and isinstance(
            next(iter(mapping.values())), dict) else 0
    return mapping[sorted_keys[i - 1]]
