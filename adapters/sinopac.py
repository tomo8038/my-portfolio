"""永豐金 Shioaji Adapter — P0 的主角。

需求:
  pip install shioaji   (Python >= 3.10)

憑證(放 .env,不進 git):
  SINOPAC_API_KEY / SINOPAC_SECRET_KEY     — 永豐 API 金鑰
  SINOPAC_CA_PATH / SINOPAC_CA_PASSWD      — 電子憑證(正式環境抓帳務必須)
  SINOPAC_PERSON_ID                        — 身分證字號(activate_ca 用)
  SINOPAC_SIMULATION=1                     — 模擬環境(可免 CA 先試跑)

注意:
  * 同一 person_id 最多 5 條連線,程式結束務必 logout()(本 adapter 已處理)。
  * list_positions 預設數量單位是「張」,這裡指定 Unit.Share 改用「股」,
    與均價(每股)的計算才一致。
"""
from datetime import datetime
from decimal import Decimal

from adapters.base import BrokerAdapter
from core.models import Position


class SinopacAdapter(BrokerAdapter):
    name = "sinopac"

    def __init__(self, creds: dict):
        """creds 來自 run.py 讀取的 .env,不要在程式碼寫死任何金鑰。"""
        self.creds = creds
        self.api = None

    # ---------- 連線生命週期 ----------

    def connect(self) -> None:
        if self.api is not None:
            return

        import shioaji as sj  # 延遲匯入:沒裝 shioaji 時,模擬模式仍可用

        simulation = self.creds.get("simulation", False)
        self.api = sj.Shioaji(simulation=simulation)

        print(f"[sinopac] 登入中({'模擬' if simulation else '正式'}環境)...")
        self.api.login(
            api_key=self.creds["api_key"],
            secret_key=self.creds["secret_key"],
        )

        # 正式環境抓「帳務資料」(持倉/餘額)需啟用電子憑證 CA
        if not simulation:
            if not self.creds.get("ca_path"):
                raise RuntimeError(
                    "正式環境需要電子憑證:請在 .env 設定 "
                    "SINOPAC_CA_PATH / SINOPAC_CA_PASSWD / SINOPAC_PERSON_ID"
                )
            print("[sinopac] 啟用電子憑證 CA...")
            self.api.activate_ca(
                ca_path=self.creds["ca_path"],
                ca_passwd=self.creds["ca_passwd"],
                person_id=self.creds["person_id"],
            )
        print("[sinopac] 連線完成")

    def disconnect(self) -> None:
        """務必登出,避免占用永豐 5 條連線額度。"""
        if self.api is not None:
            try:
                self.api.logout()
                print("[sinopac] 已登出")
            except Exception as e:  # 登出失敗不影響資料已寫入
                print(f"[sinopac] 登出時發生例外(可忽略):{e}")
            self.api = None

    # ---------- 資料抓取 ----------

    def list_positions(self) -> list[Position]:
        self.connect()
        import shioaji as sj

        raw = self.api.list_positions(
            self.api.stock_account,
            unit=sj.constant.Unit.Share,  # 用「股」,與每股均價一致
        )
        now = datetime.now()
        out: list[Position] = []
        for p in raw:
            qty = Decimal(str(p.quantity))
            if qty == 0:
                continue
            out.append(Position(
                broker=self.name,
                account_id="stock",
                symbol=p.code,
                name=self._lookup_name(p.code),
                asset_class=self._guess_asset_class(p.code),
                qty=qty,
                avg_cost=Decimal(str(p.price)),       # 每股均價
                last_price=Decimal(str(p.last_price)),
                ccy="TWD",
                industry=self._lookup_industry(p.code),
                as_of=now,
            ))
        return out

    def list_cash(self) -> dict[str, Decimal]:
        """銀行交割戶餘額。

        P0 先取 account_balance(銀行水位)。
        註:T+2 未交割款(api.settlements)會讓「銀行餘額」與
        「實際可動用」短暫不一致,P1 可再加上修正。
        """
        self.connect()
        bal = self.api.account_balance()
        return {"TWD": Decimal(str(bal.acc_balance))}

    def list_transactions(self) -> list:
        """今天的成交(盡力而為)。

        每次開機把「執行當天」的成交累積進交易表,讓回補引擎在
        有買賣的區間也能準確重播。查不到就回空清單、不中斷主流程
        (沒交易的日子本來就該是空的)。

        amount 慣例:買進為負現金流、賣出為正(估算含 0.1425% 手續費,
        賣出另含 0.3% 證交稅;與券商實際收費的微小差異,會在下一個
        真實快照被重新錨定,不會累積)。
        """
        self.connect()
        from core.models import Transaction
        today = datetime.now().strftime("%Y-%m-%d")
        out: list[Transaction] = []
        try:
            trades = self.api.list_trades(self.api.stock_account)
        except TypeError:
            try:
                trades = self.api.list_trades()
            except Exception as e:
                print(f"[sinopac] 今日成交查詢不可用(略過):{e}")
                return []
        except Exception as e:
            print(f"[sinopac] 今日成交查詢不可用(略過):{e}")
            return []

        for tr in trades or []:
            try:
                code = tr.contract.code
                action = str(tr.order.action)            # Buy / Sell
                is_buy = "Buy" in action
                deals = getattr(tr.status, "deals", None) or []
                for i, d in enumerate(deals):
                    qty = Decimal(str(d.quantity))
                    # 成交數量單位:整股交易以「張」回報 → 換算為股
                    if qty < 1000 and getattr(tr.order, "order_lot", "") != "IntradayOdd":
                        qty *= 1000
                    px = Decimal(str(d.price))
                    gross = qty * px
                    fee = (gross * Decimal("0.001425")).quantize(Decimal("1"))
                    tax = (gross * Decimal("0.003")).quantize(Decimal("1")) \
                        if not is_buy else Decimal(0)
                    amount = -(gross + fee) if is_buy else (gross - fee - tax)
                    out.append(Transaction(
                        broker=self.name,
                        external_id=f"{today}-{code}-{getattr(d, 'seq', i)}-{d.ts}",
                        symbol=code,
                        txn_type="BUY" if is_buy else "SELL",
                        qty=qty, price=px, amount=amount,
                        ccy="TWD", trade_date=today,
                    ))
            except Exception as e:
                print(f"[sinopac] 解析一筆成交失敗(略過):{e}")
        if out:
            print(f"[sinopac] 今日成交 {len(out)} 筆已記錄")
        return out

    def list_realized_pnl(self, begin: str, end: str) -> list:
        """已實現損益(P3):接 Shioaji list_profit_loss。

        查不到/不支援就回空清單、不中斷主流程。
        pnl 以券商回報金額為準;qty 單位依券商回報、僅供顯示。
        external_id 用「日期-代號-dseq」,跨次執行可去重。
        """
        self.connect()
        from core.models import RealizedPnl
        out: list[RealizedPnl] = []
        try:
            rows = self.api.list_profit_loss(
                self.api.stock_account, begin_date=begin, end_date=end)
        except Exception as e:
            print(f"[sinopac] 已實現損益查詢不可用(略過):{e}")
            return []

        for i, r in enumerate(rows or []):
            try:
                code = getattr(r, "code", "") or ""
                d = str(getattr(r, "date", "") or "")[:10]
                seq = getattr(r, "dseq", None) or getattr(r, "id", i)
                out.append(RealizedPnl(
                    broker=self.name,
                    external_id=f"{d}-{code}-{seq}",
                    symbol=code,
                    qty=Decimal(str(getattr(r, "quantity", 0) or 0)),
                    price=Decimal(str(getattr(r, "price", 0) or 0)),
                    pnl=Decimal(str(getattr(r, "pnl", 0) or 0)),
                    ccy="TWD", trade_date=d,
                ))
            except Exception as e:
                print(f"[sinopac] 解析一筆已實現損益失敗(略過):{e}")
        if out:
            print(f"[sinopac] 已實現損益 {len(out)} 筆({begin} ~ {end})")
        return out

    def subscribe_ticks(self, symbols: list[str], on_tick) -> bool:
        """訂閱台股即時成交 Tick(P3,watch.py 用)。

        注意:shioaji 的 callback 來自背景執行緒,on_tick 的實作
        (watch.py)以 queue 轉回主執行緒後才寫 SQLite。
        """
        self.connect()
        import shioaji as sj

        @self.api.on_tick_stk_v1()
        def _cb(exchange, tick):
            try:
                on_tick(str(tick.code), float(tick.close),
                        str(getattr(tick, "datetime", datetime.now())))
            except Exception as e:
                print(f"[sinopac] tick 處理失敗(略過):{e}")

        n = 0
        for sym in symbols:
            try:
                contract = self.api.Contracts.Stocks[sym]
                self.api.quote.subscribe(
                    contract,
                    quote_type=sj.constant.QuoteType.Tick,
                    version=sj.constant.QuoteVersion.v1,
                )
                n += 1
            except Exception as e:
                print(f"[sinopac] 訂閱 {sym} 失敗(略過):{e}")
        print(f"[sinopac] 已訂閱 {n}/{len(symbols)} 檔即時報價")
        return n > 0

    # ---------- 輔助 ----------

    # 證交所產業別代碼 → 名稱(P2 資產配置「產業」維度用)
    _INDUSTRY = {
        "01": "水泥", "02": "食品", "03": "塑膠", "04": "紡織纖維",
        "05": "電機機械", "06": "電器電纜", "08": "玻璃陶瓷", "09": "造紙",
        "10": "鋼鐵", "11": "橡膠", "12": "汽車", "14": "建材營造",
        "15": "航運", "16": "觀光餐旅", "17": "金融保險", "18": "貿易百貨",
        "19": "綜合", "20": "其他", "21": "化學", "22": "生技醫療",
        "23": "油電燃氣", "24": "半導體", "25": "電腦及週邊", "26": "光電",
        "27": "通信網路", "28": "電子零組件", "29": "電子通路",
        "30": "資訊服務", "31": "其他電子",
    }

    def _lookup_industry(self, code: str) -> str:
        """從合約主檔的 category 對映產業名;ETF 一律歸「ETF」。"""
        if code.startswith("00"):
            return "ETF"
        try:
            cat = str(getattr(self.api.Contracts.Stocks[code], "category", ""))
            return self._INDUSTRY.get(cat, "其他")
        except Exception:
            return "其他"

    def _lookup_name(self, code: str) -> str:
        """從合約主檔查中文名(登入時 shioaji 會下載 Contracts)。"""
        try:
            c = self.api.Contracts.Stocks[code]
            return getattr(c, "name", code) or code
        except Exception:
            return code

    def _guess_asset_class(self, code: str) -> str:
        """台股簡易分類:00 開頭多為 ETF,其餘視為個股。夠 P0 用。"""
        return "etf" if code.startswith("00") else "equity"
