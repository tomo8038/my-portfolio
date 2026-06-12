"""歷史價格服務 — 回補引擎的燃料。

來源:yfinance(免費、免認證)。台股代號自動映射:2330 → 2330.TW(上市),
查不到再試 .TWO(上櫃)。

兩個關鍵設計:

1) 快取優先:抓過的 (symbol, date) 收盤價存進 SQLite price_cache,
   之後開機直接讀本地,離線也能回補已快取的區間。

2) 拆股調整(split-only):回補是「今天的股數 × 當天價格」,
   若中間發生過股票分割(例:0050 在 2025 年 1 拆 4),
   分割前的原始價格是分割後的 4 倍,直接相乘會把淨值灌水 4 倍。
   修正:把分割「之前」的價格除以之後發生的分割倍數
   (今天股數 × 調整價 = 當時股數 × 當時原始價 = 正確市值)。
   注意只調拆股、不調股息 — 股息調整會把過去價格壓低,低估歷史淨值。
"""
from datetime import date, datetime, timedelta


class PriceService:
    def __init__(self, db):
        self.db = db
        self._mem: dict[str, dict[str, float]] = {}   # symbol -> {date: close}

    # ---------- 對外 ----------

    def ensure_range(self, symbol: str, ccy: str, start: str, end: str) -> None:
        """確保 [start, end] 的每日收盤已在快取;缺的才上網抓。"""
        cached = self._load(symbol)
        if cached and min(cached) <= start and max(cached) >= end:
            return  # 區間已涵蓋
        fetched = self._fetch(symbol, ccy, start, end)
        if fetched:
            self.db.put_prices(symbol, ccy, fetched)
            self._mem.pop(symbol, None)  # 失效,下次重讀

    def close_on(self, symbol: str, d: str) -> float | None:
        """d 當天收盤價;非交易日/無資料 → 往前找最近一個(最多 10 天)。"""
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

    # ---------- 內部 ----------

    def _load(self, symbol: str) -> dict[str, float]:
        if symbol not in self._mem:
            self._mem[symbol] = self.db.get_prices(symbol)
        return self._mem[symbol]

    def _fetch(self, symbol: str, ccy: str,
               start: str, end: str) -> list[tuple[str, float]]:
        """從 yfinance 抓原始收盤 + 拆股事件,做 split-only 調整。"""
        try:
            import yfinance as yf
        except ImportError:
            print("[prices] 未安裝 yfinance(pip install yfinance),"
                  "僅能使用已快取的價格")
            return []

        end_plus = (datetime.strptime(end, "%Y-%m-%d").date()
                    + timedelta(days=1)).isoformat()   # yfinance end 不含當日

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
        """代號 → yfinance ticker 候選清單。"""
        if ccy == "TWD":
            return [f"{symbol}.TW", f"{symbol}.TWO"]   # 上市 → 上櫃
        return [symbol]                                 # 美股原樣

    @staticmethod
    def _apply_splits(closes: list[tuple[str, float]],
                      splits: list[tuple[str, float]]) -> list[tuple[str, float]]:
        """split-only 調整:某日價格 ÷(該日「之後」所有拆股倍數的乘積)。"""
        if not splits:
            return closes
        out = []
        for d, c in closes:
            factor = 1.0
            for sd, ratio in splits:
                if d < sd:          # 拆股發生在 d 之後 → d 的價格要除
                    factor *= ratio
            out.append((d, c / factor))
        return out


class FXService:
    """歷史匯率(對基準幣別)。P1 全為 TWD,先以恆等為主;
    結構已備好,P4 接美股時 USD/TWD 走 yfinance 'USDTWD=X' + fx_cache。"""

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
