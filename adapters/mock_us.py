"""美股模擬 Adapter — P4 驗證「多券商 + 多幣別」整條流程用。

用途:python run.py --mock(P4 版會同時掛 MockAdapter + MockUSAdapter)
免任何嘉信/盈透帳號,即可驗證:
  * 多 adapter 聚合(永豐 TWD + 嘉信 USD 出現在同一張快照)
  * USD → TWD 匯率換算(core/fx.py)
  * 儀表板「券商 / 幣別」配置維度
  * 商品主檔跨券商關聯(2330 ↔ TSM)
"""
from datetime import datetime
from decimal import Decimal

from adapters.base import BrokerAdapter
from core.models import Position

_FAKE_US = [
    # (代號, 名稱, 類別, 股數, 均價USD, 現價USD, 產業)
    ("TSM",  "Taiwan Semi ADR",   "equity", 60,  95.0,  210.0, "半導體"),
    ("AAPL", "Apple Inc",         "equity", 30,  165.0, 235.0, "消費電子"),
    ("VOO",  "Vanguard S&P 500",  "etf",    25,  380.0, 560.0, "ETF"),
    ("MSFT", "Microsoft",         "equity", 15,  310.0, 470.0, "軟體"),
]


class MockUSAdapter(BrokerAdapter):
    name = "schwab"   # 模擬「嘉信」,讓儀表板的券商維度看得到第二家

    def connect(self) -> None:
        print("[mock-us] 模擬嘉信(USD 持倉),不連任何券商")

    def list_positions(self) -> list[Position]:
        now = datetime.now()
        return [
            Position(
                broker=self.name, account_id="brokerage",
                symbol=code, name=name, asset_class=ac,
                qty=Decimal(qty), avg_cost=Decimal(str(avg)),
                last_price=Decimal(str(last)), ccy="USD",
                industry=ind, as_of=now,
            )
            for code, name, ac, qty, avg, last, ind in _FAKE_US
        ]

    def list_cash(self) -> dict[str, Decimal]:
        return {"USD": Decimal("4200")}
