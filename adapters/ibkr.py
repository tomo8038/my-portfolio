"""盈透 IBKR Adapter — P4。

走 Client Portal Web API(本機 Gateway),只用唯讀端點:
帳戶清單、持倉、現金帳本。不下單、不佔 TWS 交易 session。

前置(每次要同步 IBKR 前):
  1. 下載 Client Portal Gateway:
     https://www.interactivebrokers.com/en/trading/ibgateway-stable.php
     (選 "Client Portal API Gateway",解壓即可,免安裝)
  2. 啟動:bin\\run.bat root\\conf.yaml(Windows)
           bin/run.sh  root/conf.yaml(macOS/Linux)
  3. 瀏覽器開 https://localhost:5000 → 登入 IBKR 帳號(含二階段驗證)。
     看到 "Client login succeeds" 即可。
  4. 之後執行 python run.py,本 adapter 會經 Gateway 抓資料。

.env 設定:
  IBKR_ENABLED=1
  IBKR_GATEWAY_URL=https://localhost:5000    # 可省略,此為預設

注意:
  * Gateway 用自簽憑證,本 adapter 對 localhost 關閉 TLS 驗證(僅本機迴路,
    流量不出網卡,風險可接受)。
  * Gateway session 閒置數分鐘會休眠;本 adapter 先打 /tickle 喚醒並
    檢查登入狀態,未登入會給出明確指引而不是悶錯。
"""
from datetime import datetime
from decimal import Decimal

from adapters.base import BrokerAdapter
from core.models import Position

# IBKR assetClass → 統一 asset_class
_ASSET_MAP = {
    "STK": "equity",
    "FUND": "fund",
    "BOND": "bond",
    "OPT": "option",
    "FOP": "option",
    "FUT": "future",
    "WAR": "warrant",
    "CASH": "cash",
}


class IbkrAdapter(BrokerAdapter):
    name = "ibkr"

    def __init__(self, creds: dict):
        self.base = (creds.get("gateway_url")
                     or "https://localhost:5000").rstrip("/")
        self._session = None
        self._accounts: list[str] = []

    # ---------- 連線生命週期 ----------

    def connect(self) -> None:
        if self._session is not None:
            return
        import requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        self._session = requests.Session()
        self._session.verify = False     # Gateway 自簽憑證(僅 localhost)

        # 喚醒 + 檢查登入狀態
        try:
            self._post("/tickle")
            status = self._post("/iserver/auth/status")
        except Exception as e:
            raise RuntimeError(
                "連不上 IBKR Client Portal Gateway。\n"
                f"  Gateway 位址:{self.base}\n"
                "  請先啟動 Gateway(bin/run.bat root/conf.yaml)並在瀏覽器\n"
                f"  開 {self.base} 完成登入。原始錯誤:{e}")

        if not (status.get("authenticated") and status.get("connected")):
            raise RuntimeError(
                "IBKR Gateway 已啟動但尚未登入(或 session 已失效)。\n"
                f"請用瀏覽器開 {self.base} 重新登入後再執行。")

        # 帳戶清單(portfolio 端點使用前必須先呼叫一次)
        accs = self._get("/portfolio/accounts")
        self._accounts = [a["accountId"] for a in accs if a.get("accountId")]
        print(f"[ibkr] 連線完成,帳戶:{', '.join(self._accounts)}")

    def disconnect(self) -> None:
        if self._session is not None:
            try:
                # 不登出 Gateway(讓你能連續執行);只關 HTTP session
                self._session.close()
            finally:
                self._session = None

    # ---------- HTTP ----------

    def _get(self, path: str, params: dict | None = None):
        r = self._session.get(self.base + "/v1/api" + path,
                              params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str):
        r = self._session.post(self.base + "/v1/api" + path, timeout=30)
        r.raise_for_status()
        return r.json() if r.text else {}

    # ---------- 資料抓取 ----------

    def list_positions(self) -> list[Position]:
        self.connect()
        now = datetime.now()
        out: list[Position] = []
        for acct in self._accounts:
            page = 0
            while True:    # 每頁最多 100 筆,翻頁到空為止
                try:
                    rows = self._get(f"/portfolio/{acct}/positions/{page}")
                except Exception as e:
                    print(f"[ibkr] 抓 {acct} 第 {page} 頁持倉失敗:{e}")
                    break
                if not rows:
                    break
                for p in rows:
                    try:
                        qty = Decimal(str(p.get("position", 0)))
                        if qty == 0:
                            continue
                        mv = Decimal(str(p.get("mktValue", 0)))
                        last = Decimal(str(p.get("mktPrice", 0)))
                        # avgPrice 是每股均價;avgCost 含合約乘數,優先用前者
                        avg = Decimal(str(p.get("avgPrice")
                                          or p.get("avgCost") or 0))
                        sym = (p.get("ticker") or p.get("contractDesc")
                               or str(p.get("conid", "?"))).strip()
                        out.append(Position(
                            broker=self.name,
                            account_id=acct,
                            symbol=sym,
                            name=(p.get("contractDesc") or sym).strip(),
                            asset_class=_ASSET_MAP.get(
                                (p.get("assetClass") or "").upper(),
                                "equity"),
                            qty=qty,
                            avg_cost=avg,
                            last_price=last if last else (
                                mv / qty if qty else Decimal(0)),
                            ccy=p.get("currency", "USD"),
                            industry=p.get("group", "") or "",
                            as_of=now,
                        ))
                    except Exception as e:
                        print(f"[ibkr] 持倉解析略過一筆:{e}")
                if len(rows) < 100:
                    break
                page += 1
        print(f"[ibkr] 取得 {len(out)} 筆持倉(含期權/期貨,若有)")
        return out

    def list_cash(self) -> dict[str, Decimal]:
        """各幣別現金 — 來自 ledger(略過 BASE 彙總列,避免重複計算)。"""
        self.connect()
        cash: dict[str, Decimal] = {}
        for acct in self._accounts:
            try:
                ledger = self._get(f"/portfolio/{acct}/ledger")
            except Exception as e:
                print(f"[ibkr] 抓 {acct} 現金帳本失敗:{e}")
                continue
            for ccy, row in (ledger or {}).items():
                if ccy.upper() == "BASE" or not isinstance(row, dict):
                    continue
                amt = Decimal(str(row.get("cashbalance", 0)))
                if amt:
                    cash[ccy.upper()] = cash.get(ccy.upper(),
                                                 Decimal(0)) + amt
        return cash
