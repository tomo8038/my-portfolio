"""淨值回補引擎 — 把「每次開機才一個點」補成「每日一點」的曲線。

演算法(設計文件 §3.3 的反向重播):

  對每一對相鄰的真實快照 (A 較舊, B 較新):
    1. 從 B 的「已知且準確」持倉與現金出發
    2. 由 B 往回逐日走到 A:
       - 每天用「當天收盤價(拆股調整後)× 當天匯率」估值 → 該日淨值
       - 跨日時把「當天的交易」反做(undo),還原前一日收盤後的狀態
    3. 寫入 daily_networth(is_real=0);真實快照日(is_real=1)絕不覆蓋

  錨點性質:每段都被兩個真實快照夾住,誤差不會跨段累積。
  區間內若沒有交易 → 估值完全精確;有交易且已記錄 → 重播後仍準確;
  有交易但未記錄(P1 只能累積「執行當天」的成交)→ 該段為近似值,
  但下一個真實快照會把曲線重新錨定,不會一路歪下去。
"""
from datetime import datetime, timedelta

ONE_DAY = timedelta(days=1)


def run_backfill(db, prices, fx, base_ccy: str = "TWD") -> dict:
    """對所有相鄰快照之間的空白做回補。回傳統計摘要。"""
    snaps = db.snapshots_with_positions()
    # 同一天多筆快照只留最後一筆(當日最終狀態)
    by_day: dict[str, dict] = {}
    for s in snaps:
        by_day[s["date"]] = s
    anchors = sorted(by_day.values(), key=lambda s: s["date"])

    if len(anchors) < 2:
        return {"segments": 0, "days_written": 0,
                "note": "快照少於 2 筆,尚無區間可回補(每次執行 run.py 會累積)"}

    total_days = 0
    segments = 0
    for a, b in zip(anchors, anchors[1:]):
        gap = (_d(b["date"]) - _d(a["date"])).days
        if gap <= 1:
            continue  # 相鄰兩天,沒有空白
        rows = _backfill_segment(db, prices, fx, a, b, base_ccy)
        total_days += db.upsert_daily_estimates(base_ccy, rows)
        segments += 1

    return {"segments": segments, "days_written": total_days}


def _backfill_segment(db, prices, fx, a: dict, b: dict,
                      base_ccy: str) -> list[tuple[str, float]]:
    """回補 (a.date, b.date) 之間的每日淨值(不含兩端,端點是真實值)。"""
    start, end = a["date"], b["date"]

    # 1) 先確保區間價格/匯率已在快取(聯集 a、b 兩端出現過的標的)
    symbols: dict[str, str] = {}
    for s in (a, b):
        for sym, h in s["holdings"].items():
            symbols[sym] = h["ccy"]
    for sym, ccy in symbols.items():
        prices.ensure_range(sym, ccy, start, end)
        fx.ensure_range(ccy, start, end)

    # 2) 從 B(較新、已知)的狀態出發,反向逐日重播
    holdings = {sym: dict(h) for sym, h in b["holdings"].items()}
    state = {"cash": float(b["cash"])}
    txs = _tx_by_day(db.transactions_between(start, end))
    warned: set[str] = set()

    out: list[tuple[str, float]] = []
    day = _d(end) - ONE_DAY
    first = _d(start)
    while day > first:
        dstr = day.isoformat()

        # 估值 dstr 之前,先反做「dstr 之後那天(=dstr+1)」發生的交易,
        # 使 holdings/cash 回到 dstr 收盤後的狀態
        nxt = (day + ONE_DAY).isoformat()
        for tx in txs.get(nxt, []):
            _undo(holdings, state, tx)

        nv = state["cash"]
        for sym, h in holdings.items():
            if h["qty"] == 0:
                continue
            px = prices.close_on(sym, dstr)
            if px is None:
                if sym not in warned:
                    print(f"[backfill] ⚠ {sym} 在 {dstr} 附近無歷史價,"
                          "此區間以 0 估值(裝 yfinance / 檢查網路後重跑可修正)")
                    warned.add(sym)
                px = 0.0
            nv += h["qty"] * px * fx.rate_on(h["ccy"], dstr)
        out.append((dstr, round(nv, 2)))
        day -= ONE_DAY

    return out


def _undo(holdings: dict, state: dict, tx: dict) -> None:
    """把一筆交易反做,回到交易前狀態。
    amount 慣例:含費稅的「淨現金流」,買進為負、賣出/股息/入金為正。"""
    t = tx["txn_type"]
    sym, qty, amount = tx["symbol"], float(tx["qty"]), float(tx["amount"])
    state["cash"] -= amount                    # 反做現金流
    if t == "BUY":
        h = holdings.setdefault(sym, {"qty": 0.0, "ccy": tx["ccy"]})
        h["qty"] -= qty
    elif t == "SELL":
        h = holdings.setdefault(sym, {"qty": 0.0, "ccy": tx["ccy"]})
        h["qty"] += qty
    # DIVIDEND / FEE / DEPOSIT / WITHDRAW 只動現金,上面已處理


def _tx_by_day(txns: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for t in txns:
        out.setdefault(t["trade_date"], []).append(t)
    return out


def _d(s: str):
    return datetime.strptime(s, "%Y-%m-%d").date()
