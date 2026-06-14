"""行情供應器 — P4d。封裝 yfinance,供匯入與歷史重建使用。

提供:
  * current_prices(symbols, ccy)         → {symbol: 最近收盤}
  * splits(symbol, ccy)                  → [(date, ratio)]  (自動偵測拆股)
  * RawPriceCache.price_on(sym, ccy, d)  → 當日「原始」收盤(auto_adjust=False)

原始收盤(非拆股調整)專供「順向逐日估值」:當日股數 × 當日原始價同處當期基礎,
跨拆股也正確。快取在 price_cache,key 加 'RAW:' 前綴,不汙染既有(調整後)價格。

台股代號自動映射 .TW / .TWO;離線/未裝 yfinance 時各方法安全回空。
"""
from __future__ import annotations

from datetime import datetime, timedelta

ONE_DAY = timedelta(days=1)


def _candidates(symbol: str, ccy: str) -> list[str]:
    if ccy == "TWD":
        return [f"{symbol}.TW", f"{symbol}.TWO"]
    return [symbol]


def _yf():
    try:
        import yfinance as yf
        return yf
    except ImportError:
        print("[market] 未安裝 yfinance(pip install yfinance);"
              "現價/歷史價/拆股將略過,以遞補值處理。")
        return None


def current_prices(symbols, ccy: str) -> dict[str, float]:
    yf = _yf()
    out: dict[str, float] = {}
    if not yf:
        return out
    for sym in symbols:
        for tk in _candidates(sym, ccy):
            try:
                h = yf.Ticker(tk).history(period="5d")
                if h is not None and not h.empty:
                    out[sym] = float(h["Close"].iloc[-1])
                    break
            except Exception:
                continue
    return out


def splits(symbol: str, ccy: str, since: str | None = None
           ) -> list[tuple[str, "Decimal"]]:
    """回傳 [(date, ratio)] 拆股事件(ratio>1 為分割,<1 為反分割)。"""
    from decimal import Decimal
    yf = _yf()
    if not yf:
        return []
    for tk in _candidates(symbol, ccy):
        try:
            s = yf.Ticker(tk).splits
            if s is None or len(s) == 0:
                continue
            out = []
            for idx, ratio in s.items():
                d = idx.strftime("%Y-%m-%d")
                if since and d < since:
                    continue
                if float(ratio) not in (0.0, 1.0):
                    out.append((d, Decimal(str(float(ratio)))))
            return sorted(out)
        except Exception:
            continue
    return []


class RawPriceCache:
    """當日原始收盤(auto_adjust=False),快取於 price_cache(key='RAW:'+sym)。"""

    def __init__(self, db):
        self.db = db
        self._mem: dict[str, dict[str, float]] = {}

    def _key(self, sym: str) -> str:
        return f"RAW:{sym}"

    def ensure_range(self, symbol: str, ccy: str, start: str, end: str) -> None:
        cached = self._load(symbol)
        if cached and min(cached) <= start and max(cached) >= end:
            return
        yf = _yf()
        if not yf:
            return
        end_plus = (datetime.strptime(end, "%Y-%m-%d").date() + ONE_DAY).isoformat()
        for tk in _candidates(symbol, ccy):
            try:
                df = yf.Ticker(tk).history(start=start, end=end_plus,
                                           auto_adjust=False, actions=False)
            except Exception:
                continue
            if df is None or df.empty:
                continue
            rows = [(i.strftime("%Y-%m-%d"), float(r["Close"]))
                    for i, r in df.iterrows() if r["Close"] == r["Close"]]
            if rows:
                self.db.put_prices(self._key(symbol), ccy, rows)
                self._mem.pop(symbol, None)
                print(f"[market] {symbol} ← {tk}:原始收盤 {len(rows)} 天已快取")
                return
        print(f"[market] 找不到 {symbol} 原始歷史價(該段以遞補處理)")

    def price_on(self, symbol: str, ccy: str, d: str) -> float | None:
        prices = self._load(symbol)
        if not prices:
            return None
        if d in prices:
            return prices[d]
        cur = datetime.strptime(d, "%Y-%m-%d").date()
        for _ in range(10):
            cur -= ONE_DAY
            if (k := cur.isoformat()) in prices:
                return prices[k]
        return None

    def _load(self, symbol: str) -> dict[str, float]:
        if symbol not in self._mem:
            self._mem[symbol] = self.db.get_prices(self._key(symbol))
        return self._mem[symbol]
