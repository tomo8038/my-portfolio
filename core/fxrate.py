"""USD/TWD(及任意對 TWD)每日歷史匯率回補 — P4d 需求 3。

* 從「美股帳戶起始日」起,逐日把 USD/TWD 匯率寫進 fx_cache,
  非交易日(週末/假日)以前一個交易日收盤前向填補 → 真的「每天」都有一筆。
* 資料源 yfinance 'USDTWD=X';抓過存本地,離線可重用。
* rate_on(ccy, date):base(TWD)回 1.0;否則回該日(或往前最近)的匯率。

這支與 core/prices.py 的 FXService 互通(同一張 fx_cache 表),
但多了「逐日前向填補」與「指定起始日整段回補」,讓資產曲線在含美股後也準確。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

ONE_DAY = timedelta(days=1)


class FXBackfiller:
    def __init__(self, db, base_ccy: str = "TWD"):
        self.db = db
        self.base = base_ccy

    # ---------- 回補 ----------

    def backfill(self, ccy: str, start: str, end: str | None = None,
                 fetch=None) -> int:
        """把 [start, end] 每一天的 ccy/base 匯率寫滿 fx_cache。回傳寫入天數。

        fetch(pair, start, end) -> list[(date, rate)]:可注入(測試用);
        預設用 yfinance。
        """
        if ccy == self.base:
            return 0
        end = end or date.today().isoformat()
        pair = f"{ccy}{self.base}"

        cached = self.db.get_fx(pair)
        # 已完整覆蓋就不重抓
        need = not (cached and min(cached) <= start and max(cached) >= end)
        market: dict[str, float] = dict(cached)
        if need:
            fetched = (fetch or self._yf_fetch)(pair, start, end)
            for d, r in fetched:
                market[d] = r

        if not market:
            print(f"[fx] 取不到 {pair} 匯率(無網路/yfinance?)。"
                  f"資產曲線的美股部位暫時無法換算。")
            return 0

        # 逐日前向填補:每個日曆日都給一筆
        rows: list[tuple[str, float]] = []
        cur = datetime.strptime(start, "%Y-%m-%d").date()
        last = None
        endd = datetime.strptime(end, "%Y-%m-%d").date()
        keys = sorted(market)
        while cur <= endd:
            k = cur.isoformat()
            if k in market:
                last = market[k]
            elif last is None:
                # 起始日早於第一個有報價的交易日 → 用最早可得值回填
                last = market[keys[0]]
            rows.append((k, last))
            cur += ONE_DAY

        self.db.put_fx(pair, rows)
        print(f"[fx] {pair}:已回補 {rows[0][0]} ~ {rows[-1][0]} 共 {len(rows)} 天"
              f"(每日一筆,非交易日前向填補)")
        return len(rows)

    # ---------- 查詢 ----------

    def rate_on(self, ccy: str, d: str) -> float:
        if ccy == self.base:
            return 1.0
        pair = f"{ccy}{self.base}"
        rates = self.db.get_fx(pair)
        if d in rates:
            return rates[d]
        cur = datetime.strptime(d, "%Y-%m-%d").date()
        for _ in range(15):
            cur -= ONE_DAY
            if (k := cur.isoformat()) in rates:
                return rates[k]
        # 還是找不到 → 取最近可得(避免整段美股市值掉成 0)
        if rates:
            return rates[max(rates)]
        raise LookupError(f"fx_cache 無 {pair} 資料,請先 backfill('{ccy}', ...)")

    # ---------- 資料源 ----------

    @staticmethod
    def _yf_fetch(pair: str, start: str, end: str) -> list[tuple[str, float]]:
        try:
            import yfinance as yf
        except ImportError:
            print("[fx] 未安裝 yfinance(pip install yfinance),無法抓匯率")
            return []
        end_plus = (datetime.strptime(end, "%Y-%m-%d").date() + ONE_DAY).isoformat()
        try:
            df = yf.Ticker(f"{pair}=X").history(start=start, end=end_plus)
        except Exception as e:
            print(f"[fx] 抓 {pair} 失敗:{e}")
            return []
        return [(i.strftime("%Y-%m-%d"), float(r["Close"]))
                for i, r in df.iterrows() if r["Close"] == r["Close"]]
