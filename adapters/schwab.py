"""嘉信 Schwab Adapter — P4。

使用 Schwab Trader API(Individual / Market Data + Accounts and Trading
Production),唯讀:只抓持倉與現金,不下單。

需求:
  pip install requests

前置(在 https://developer.schwab.com 完成,只做一次):
  1. 註冊開發者帳號,建立 App(選 Accounts and Trading Production)。
  2. Callback URL 填 https://127.0.0.1:8182(要與 .env 一致)。
  3. 取得 App Key / Secret,等待 App 狀態變成 "Ready For Use"。
  4. 執行 `python schwab_auth.py` 完成一次性 OAuth 授權,
     產生 schwab_token.json(已列入 .gitignore)。

.env 設定:
  SCHWAB_ENABLED=1
  SCHWAB_APP_KEY=...
  SCHWAB_APP_SECRET=...
  SCHWAB_CALLBACK_URL=https://127.0.0.1:8182
  SCHWAB_TOKEN_PATH=schwab_token.json        # 可省略,預設專案根目錄

Token 生命週期(Schwab 規則):
  * access token  約 30 分鐘 → 本 adapter 會用 refresh token 自動換新。
  * refresh token 約 7 天    → 到期需重跑 `python schwab_auth.py`。
    本 adapter 會在剩不到 2 天時於終端機提醒,過期則丟出明確錯誤訊息。
"""
import base64
import json
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from adapters.base import BrokerAdapter
from core.models import Position

TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
TRADER_BASE = "https://api.schwabapi.com/trader/v1"

REFRESH_TOKEN_LIFETIME = 7 * 86400          # 7 天
REFRESH_WARN_BEFORE = 2 * 86400             # 剩 2 天開始提醒

# Schwab assetType → 統一 asset_class
_ASSET_MAP = {
    "EQUITY": "equity",
    "ETF": "etf",
    "COLLECTIVE_INVESTMENT": "etf",
    "MUTUAL_FUND": "fund",
    "FIXED_INCOME": "bond",
    "OPTION": "option",
    "FUTURE": "future",
    "CASH_EQUIVALENT": "cash",
}


class SchwabAdapter(BrokerAdapter):
    name = "schwab"

    def __init__(self, creds: dict):
        self.creds = creds
        self.token_path = Path(creds.get("token_path") or "schwab_token.json")
        self._token: dict | None = None
        self._session = None

    # ---------- 連線生命週期 ----------

    def connect(self) -> None:
        if self._session is not None:
            return
        import requests
        self._session = requests.Session()
        self._load_token()
        self._ensure_access_token()
        print("[schwab] 連線就緒(OAuth token 有效)")

    def disconnect(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

    # ---------- Token 管理 ----------

    def _load_token(self) -> None:
        if not self.token_path.exists():
            raise RuntimeError(
                f"找不到 Schwab token 檔({self.token_path})。\n"
                "請先執行一次性授權:python schwab_auth.py"
            )
        self._token = json.loads(self.token_path.read_text(encoding="utf-8"))

        # refresh token 7 天壽命檢查與提醒
        issued = self._token.get("refresh_issued_at", 0)
        age = time.time() - issued
        remain = REFRESH_TOKEN_LIFETIME - age
        if remain <= 0:
            raise RuntimeError(
                "Schwab refresh token 已過期(7 天上限)。\n"
                "請重新執行:python schwab_auth.py"
            )
        if remain <= REFRESH_WARN_BEFORE:
            hours = int(remain // 3600)
            print(f"[schwab] ⚠ refresh token 約 {hours} 小時後過期,"
                  f"建議近期重跑 python schwab_auth.py")

    def _save_token(self) -> None:
        self.token_path.write_text(
            json.dumps(self._token, indent=2), encoding="utf-8")

    def _basic_auth_header(self) -> dict:
        raw = f"{self.creds['app_key']}:{self.creds['app_secret']}"
        return {"Authorization": "Basic " +
                base64.b64encode(raw.encode()).decode()}

    def _ensure_access_token(self) -> None:
        """access token 過期(30 分)就用 refresh token 換新。"""
        expires_at = self._token.get("access_expires_at", 0)
        if time.time() < expires_at - 60:   # 留 60 秒餘裕
            return
        print("[schwab] access token 到期,自動續期中...")
        resp = self._session.post(
            TOKEN_URL,
            headers={**self._basic_auth_header(),
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "refresh_token",
                  "refresh_token": self._token["refresh_token"]},
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Schwab token 續期失敗(HTTP {resp.status_code})。"
                "若 refresh token 已過期,請重跑 python schwab_auth.py。\n"
                f"回應:{resp.text[:300]}")
        data = resp.json()
        self._token["access_token"] = data["access_token"]
        self._token["access_expires_at"] = time.time() + data.get("expires_in", 1800)
        # Schwab 續期時可能換發新的 refresh token
        if data.get("refresh_token"):
            self._token["refresh_token"] = data["refresh_token"]
        self._save_token()

    def _get(self, path: str, params: dict | None = None):
        self._ensure_access_token()
        resp = self._session.get(
            TRADER_BASE + path,
            headers={"Authorization": f"Bearer {self._token['access_token']}"},
            params=params, timeout=30)
        if resp.status_code == 401:
            # access token 被提前撤銷 → 強制續期重試一次
            self._token["access_expires_at"] = 0
            self._ensure_access_token()
            resp = self._session.get(
                TRADER_BASE + path,
                headers={"Authorization":
                         f"Bearer {self._token['access_token']}"},
                params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    # ---------- 資料抓取 ----------

    def list_positions(self) -> list[Position]:
        self.connect()
        now = datetime.now()
        out: list[Position] = []
        accounts = self._get("/accounts", params={"fields": "positions"})
        for acc in accounts:
            sec = acc.get("securitiesAccount", {}) or {}
            acct_id = str(sec.get("accountNumber", "schwab"))
            for p in sec.get("positions", []) or []:
                try:
                    inst = p.get("instrument", {}) or {}
                    qty = (Decimal(str(p.get("longQuantity", 0)))
                           - Decimal(str(p.get("shortQuantity", 0))))
                    if qty == 0:
                        continue
                    mv = Decimal(str(p.get("marketValue", 0)))
                    last = mv / qty if qty else Decimal(0)
                    out.append(Position(
                        broker=self.name,
                        account_id=acct_id,
                        symbol=inst.get("symbol", "?"),
                        name=inst.get("description",
                                      inst.get("symbol", "?")) or "?",
                        asset_class=_ASSET_MAP.get(
                            (inst.get("assetType") or "").upper(), "equity"),
                        qty=qty,
                        avg_cost=Decimal(str(p.get("averagePrice", 0))),
                        last_price=last,
                        ccy="USD",
                        industry="",            # 可由 instrument master 補
                        as_of=now,
                    ))
                except Exception as e:      # 單筆解析失敗不中斷整體
                    print(f"[schwab] 持倉解析略過一筆:{e}")
        print(f"[schwab] 取得 {len(out)} 筆持倉")
        return out

    def list_cash(self) -> dict[str, Decimal]:
        self.connect()
        total = Decimal(0)
        accounts = self._get("/accounts")
        for acc in accounts:
            sec = acc.get("securitiesAccount", {}) or {}
            bal = sec.get("currentBalances", {}) or {}
            # 現金 + 貨幣市場基金(Schwab 把閒置現金掃進 MMF)
            for key in ("cashBalance", "moneyMarketFund"):
                v = bal.get(key)
                if v is not None:
                    total += Decimal(str(v))
        return {"USD": total}
