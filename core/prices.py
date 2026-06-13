"""歷史價格服務 — 回補引擎的燃料。(測試重建版)"""
from datetime import date, datetime, timedelta


class PriceService:
    def __init__(self, db):
        self.db = db
        self._mem: dict[str, dict[str, float]] = {}

    def ensure_range(self, symbol: str, ccy: str, start: str, end: str) -> None:
        cached = self._load(symbol)
        if cached and min(cached) <= start and max(cached) >= end:
            return
        fetched = self._fetch(symbol, ccy, start, end)
        if fetched:
            self.db.put_prices(symbol, ccy, fetched)
            self._mem.pop(symbol, None)

    def close_on(self, symbol: str, d: str) -> float | None:
        prices = self._load(symbol)
        if not prices:
            return None
        if d in prices:
            return prices[d]
        cur = datetime.strptime(d, "%Y-%m-%d").date()
        for _ in range(10):
            cur -= timedelta(days=1)
            key = cur.isoformat()
            if key in prices:
                return prices[key]
        return None

    def _load(self, symbol: str) -> dict[str, float]:
        if symbol not in self._mem:
            self._mem[symbol] = self.db.get_prices(symbol)
        return self._mem[symbol]

    def _fetch(self, symbol: str, ccy: str,
               start: str, end: str) -> list[tuple[str, float]]:
        try:
            import yfinance as yf
        except ImportError:
            print("[prices] 未安裝 yfinance(pip install yfinance),"
                  "僅能使用已快取的價格")
            return []
        end_plus = (datetime.strptime(end, "%Y-%m-%d").date()
                    + timedelta(days=1)).isoformat()
        for ticker in self._candidates(symbol, ccy):
            try:
                df = yf.Ticker(ticker).history(
                    start=start, end=end_plus,
                    auto_adjust=False, actions=True)
            except Exception as e:
                print(f"[prices] 抓 {ticker} 失敗:{e}")
                continue
            if df is None or df.empty:
                continue
            closes = [(idx.strftime("%Y-%m-%d"), float(row["Close"]))
                      for idx, row in df.iterrows()]
            splits = [(idx.strftime("%Y-%m-%d"), float(row["Stock Splits"]))
                      for idx, row in df.iterrows()
                      if float(row.get("Stock Splits", 0) or 0) not in (0.0, 1.0)]
            adjusted = self._apply_splits(closes, splits)
            print(f"[prices] {symbol} ← {ticker}:{len(adjusted)} 天"
                  + (f"(含 {len(splits)} 次拆股調整)" if splits else ""))
            return adjusted
        print(f"[prices] 找不到 {symbol} 的歷史價格(網路或代號問題),"
              "該標的將以最近可得價遞補")
        return []

    @staticmethod
    def _candidates(symbol: str, ccy: str) -> list[str]:
        if ccy == "TWD":
            return [f"{symbol}.TW", f"{symbol}.TWO"]
        return [symbol]

    @staticmethod
    def _apply_splits(closes, splits):
        if not splits:
            return closes
        out = []
        for d, c in closes:
            factor = 1.0
            for sd, ratio in splits:
                if d < sd:
                    factor *= ratio
            out.append((d, c / factor))
        return out


class FXService:
    """歷史匯率(對基準幣別)。"""

    def __init__(self, db, base_ccy: str = "TWD"):
        self.db = db
        self.base = base_ccy

    def ensure_range(self, ccy: str, start: str, end: str) -> None:
        if ccy == self.base:
            return
        pair = f"{ccy}{self.base}"
        cached = self.db.get_fx(pair)
        if cached and min(cached) <= start and max(cached) >= end:
            return
        try:
            import yfinance as yf
            df = yf.Ticker(f"{pair}=X").history(start=start, end=end)
            rows = [(i.strftime("%Y-%m-%d"), float(r["Close"]))
                    for i, r in df.iterrows()]
            if rows:
                self.db.put_fx(pair, rows)
        except Exception as e:
            print(f"[fx] 抓 {pair} 失敗:{e}")

    def rate_on(self, ccy: str, d: str) -> float:
        if ccy == self.base:
            return 1.0
        pair = f"{ccy}{self.base}"
        rates = self.db.get_fx(pair)
        if d in rates:
            return rates[d]
        cur = datetime.strptime(d, "%Y-%m-%d").date()
        for _ in range(10):
            cur -= timedelta(days=1)
            if (k := cur.isoformat()) in rates:
                return rates[k]
        raise LookupError(f"無 {pair} 在 {d} 附近的匯率,請先 ensure_range")
