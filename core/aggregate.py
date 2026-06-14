"""跨券商彙總 — P4d。

把 positions_current(各券商、各幣別)與各券商現金,用「當日匯率」換算成
基準幣別(TWD),算出整體淨值 / 投資市值 / 成本 / 未實現損益,
並產生 breakdown(by_broker / by_ccy)。run.py 與 rebuild 都用這個。

附帶:broker_cash 表(各券商現金餘額,原幣)。沒有就建。
"""
from __future__ import annotations

BROKER_CASH_DDL = """
CREATE TABLE IF NOT EXISTS broker_cash (
    broker TEXT PRIMARY KEY,
    ccy    TEXT NOT NULL,
    amount REAL NOT NULL,
    as_of  TEXT
);
"""


def ensure_broker_cash(db):
    db.con.executescript(BROKER_CASH_DDL)
    db.con.commit()


def set_broker_cash(db, broker: str, ccy: str, amount: float, as_of: str) -> None:
    ensure_broker_cash(db)
    db.con.execute(
        "INSERT OR REPLACE INTO broker_cash VALUES (?,?,?,?)",
        (broker, ccy, float(amount), as_of))
    db.con.commit()


def all_broker_cash(db) -> list[tuple[str, str, float]]:
    ensure_broker_cash(db)
    return [(b, c, a) for b, c, a in db.con.execute(
        "SELECT broker, ccy, amount FROM broker_cash")]


def combined_snapshot(db, fx, base_ccy: str = "TWD", today: str | None = None) -> dict:
    """彙總所有券商持倉 + 現金 → 基準幣別。回傳可直接餵 save_snapshot 的 dict。

    fx 需提供 rate_on(ccy, date) → 對 base_ccy 的匯率(base 本身回 1.0)。
    """
    from datetime import date
    today = today or date.today().isoformat()

    invested = cost = cash_total = 0.0
    by_broker: dict[str, float] = {}
    by_ccy: dict[str, float] = {}

    for p in db.all_positions():
        if p.get("asset_class") == "cash":
            continue   # 現金以 broker_cash 為準,避免重複計入
        rate = fx.rate_on(p["ccy"], today)
        mv = p["market_value_native"] * rate
        cst = p["qty"] * p["avg_cost"] * rate
        invested += mv
        cost += cst
        by_broker[p["broker"]] = by_broker.get(p["broker"], 0.0) + mv
        by_ccy[p["ccy"]] = by_ccy.get(p["ccy"], 0.0) + mv

    for broker, ccy, amount in all_broker_cash(db):
        rate = fx.rate_on(ccy, today)
        c = amount * rate
        cash_total += c
        by_broker[broker] = by_broker.get(broker, 0.0) + c

    net_worth = invested + cash_total
    breakdown = {
        "by_broker": by_broker,
        "by_ccy": by_ccy,
        "cash_twd": cash_total,
    }
    return {
        "base_ccy": base_ccy, "net_worth": net_worth, "invested": invested,
        "cash": cash_total, "cost": cost, "breakdown": breakdown,
    }


# ====================================================================
# 把 positions_current 依「代號」跨券商合併,做成可餵 save_snapshot_positions
# 的物件清單(snapshot_positions 主鍵是 ts+symbol,故同代號必須先合併,
# 否則跨券商同代號(例如 QLD 同時在嘉信與盈透)會互相覆蓋)。
# ====================================================================

class _SnapRow:
    """save_snapshot_positions 只取 symbol/qty/avg_cost/ccy/last_price 五個屬性。"""
    __slots__ = ("symbol", "qty", "avg_cost", "ccy", "last_price")

    def __init__(self, symbol, qty, avg_cost, ccy, last_price):
        self.symbol = symbol
        self.qty = qty
        self.avg_cost = avg_cost
        self.ccy = ccy
        self.last_price = last_price


def aggregated_snapshot_positions(db) -> list:
    """跨券商把同代號的持倉合併(股數相加、加權平均成本、取較新現價)。"""
    acc: dict[str, dict] = {}
    for p in db.all_positions():
        if p.get("asset_class") == "cash":
            continue
        sym = p["symbol"]
        a = acc.setdefault(sym, {"qty": 0.0, "cost": 0.0, "ccy": p["ccy"],
                                 "last": p["last_price"]})
        a["qty"] += p["qty"]
        a["cost"] += p["qty"] * p["avg_cost"]
        a["last"] = p["last_price"]          # 後者(任一)即可,顯示用
    rows = []
    for sym, a in acc.items():
        if a["qty"] <= 0:
            continue
        avg = a["cost"] / a["qty"] if a["qty"] else 0.0
        rows.append(_SnapRow(sym, a["qty"], avg, a["ccy"], a["last"]))
    return rows


def write_combined_snapshot(db, fx, base_ccy: str = "TWD",
                            today: str | None = None) -> str:
    """彙總全部券商 → 寫一筆「合併快照」(snapshots / daily_networth / 
    snapshot_positions)。回傳該快照 ts。run.py 同步完永豐後呼叫即可。"""
    snap = combined_snapshot(db, fx, base_ccy, today)
    ts = db.save_snapshot(
        base_ccy=snap["base_ccy"], net_worth=snap["net_worth"],
        invested=snap["invested"], cash=snap["cash"],
        cost=snap["cost"], breakdown=snap["breakdown"])
    db.save_snapshot_positions(ts, aggregated_snapshot_positions(db))
    return ts
