"""模擬 Adapter — 不需任何券商帳號/憑證,用假資料驗證整條流程。

用途:python run.py --mock
先確認「同步 → 寫入 SQLite → 儀表板顯示」整條管線是通的,
再填入真實永豐憑證切換到正式模式。
"""
from datetime import datetime
from decimal import Decimal

from adapters.base import BrokerAdapter
from core.models import Position

_FAKE = [
    # (代號, 名稱, 類別, 股數, 均價, 現價, 產業)
    ("2330",  "台積電",         "equity", 1000, 580.0, 1050.0, "半導體"),
    ("2454",  "聯發科",         "equity", 300,  780.0, 1280.0, "半導體"),
    ("2882",  "國泰金",         "equity", 2000, 45.0,  41.2,   "金融保險"),
    ("0050",  "元大台灣50",     "etf",    2000, 130.0, 185.0,  "ETF"),
    ("00878", "國泰永續高股息", "etf",    5000, 19.5,  22.4,   "ETF"),
]


class MockAdapter(BrokerAdapter):
    name = "sinopac"  # 模擬「永豐」資料,讓儀表板顯示一致

    def connect(self) -> None:
        print("[mock] 模擬模式,不連任何券商")

    def list_positions(self) -> list[Position]:
        now = datetime.now()
        return [
            Position(
                broker=self.name, account_id="stock",
                symbol=code, name=name, asset_class=ac,
                qty=Decimal(qty), avg_cost=Decimal(str(avg)),
                last_price=Decimal(str(last)), ccy="TWD",
                industry=ind, as_of=now,
            )
            for code, name, ac, qty, avg, last, ind in _FAKE
        ]

    def list_cash(self) -> dict[str, Decimal]:
        return {"TWD": Decimal("320000")}

    def list_realized_pnl(self, begin: str, end: str) -> list:
        """假的已實現損益(P3)。external_id 固定 → 重跑可驗證去重。"""
        from datetime import date, timedelta
        from core.models import RealizedPnl
        today = date.today()
        fake = [   # (天數前, 代號, 股數, 賣價, 已實現損益)
            (45, "2603", 2000, 182.0,  61500.0),
            (30, "2330", 200,  995.0,  79800.0),
            (18, "0056", 5000, 38.2,  -10350.0),
            (9,  "2454", 100,  1310.0, 50200.0),
            (2,  "2891", 3000, 27.8,   -4400.0),
        ]
        out = []
        for days_ago, code, qty, px, pnl in fake:
            d = (today - timedelta(days=days_ago)).isoformat()
            if not (begin <= d <= end):
                continue
            out.append(RealizedPnl(
                broker=self.name, external_id=f"mock-{code}-{days_ago}",
                symbol=code, qty=Decimal(qty), price=Decimal(str(px)),
                pnl=Decimal(str(pnl)), ccy="TWD", trade_date=d))
        return out
