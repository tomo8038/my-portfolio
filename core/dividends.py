"""股息服務 — P2「股息現金流」的資料來源。

來源:yfinance 的 dividends(除息事件,每股配息金額)。
台股代號映射與 PriceService 一致(2330 → 2330.TW,查不到再試 .TWO)。

設計:
1) 快取優先:抓過的 (symbol, date) 每股配息存進 dividend_cache,
   之後開機/開儀表板直接讀本地,不重複打外部 API。
2) 估算口徑(誠實標示):月現金流與年化殖利率是
   「歷史每股配息 × 目前持股」的估算,不是帳上實收金額
   (實收需券商歷史對帳單,屬 P3 已實現損益範疇)。
3) 離線容錯:沒網路/沒裝 yfinance 時不中斷主流程,只用已快取資料。
"""
from datetime import date, datetime, timedelta

LOOKBACK_DAYS = 400          # 預設抓近 400 天(涵蓋完整 TTM + 緩衝)


class DividendService:
    def __init__(self, db):
        self.db = db
        self._mem: dict[str, dict[str, float]] = {}   # symbol -> {date: amt}

    # ---------- 對外 ----------

    def refresh(self, symbol: str, ccy: str,
                lookback_days: int = LOOKBACK_DAYS) -> int:
        """確保近 lookback_days 的除息事件已在快取;回傳新抓到的事件數。

        策略:若快取最新一筆已落在 90 天內,視為夠新、不重抓
        (台股多為季配/年配,90 天內必有更新機會時才上網)。
        """
        cached = self._load(symbol)
        today = date.today()
        if cached:
            newest = max(cached)
            if (today - _d(newest)).days < 90:
                return 0
        start = (today - timedelta(days=lookback_days)).isoformat()
        rows = self._fetch(symbol, ccy, start)
        if rows:
            self.db.put_dividends(symbol, ccy, rows)
            self._mem.pop(symbol, None)
        return len(rows)

    def history(self, symbol: str) -> dict[str, float]:
        """{date: 每股配息}(只讀快取,不上網)。"""
        return dict(self._load(symbol))

    def ttm_per_share(self, symbol: str, as_of: date | None = None) -> float:
        """近 12 個月每股配息合計(TTM)。"""
        as_of = as_of or date.today()
        cutoff = (as_of - timedelta(days=365)).isoformat()
        return sum(a for d, a in self._load(symbol).items()
                   if cutoff < d <= as_of.isoformat())

    # ---------- 內部 ----------

    def _load(self, symbol: str) -> dict[str, float]:
        if symbol not in self._mem:
            self._mem[symbol] = self.db.get_dividends(symbol)
        return self._mem[symbol]

    def _fetch(self, symbol: str, ccy: str,
               start: str) -> list[tuple[str, float]]:
        try:
            import yfinance as yf
        except ImportError:
            print("[div] 未安裝 yfinance,僅能使用已快取的股息資料")
            return []
        for ticker in self._candidates(symbol, ccy):
            try:
                s = yf.Ticker(ticker).dividends
            except Exception as e:
                print(f"[div] 抓 {ticker} 股息失敗:{e}")
                continue
            if s is None or len(s) == 0:
                continue
            rows = [(idx.strftime("%Y-%m-%d"), float(v))
                    for idx, v in s.items()
                    if idx.strftime("%Y-%m-%d") >= start and float(v) > 0]
            print(f"[div] {symbol} ← {ticker}:{len(rows)} 筆除息事件")
            return rows
        return []

    @staticmethod
    def _candidates(symbol: str, ccy: str) -> list[str]:
        if ccy == "TWD":
            return [f"{symbol}.TW", f"{symbol}.TWO"]
        return [symbol]


def _d(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()
