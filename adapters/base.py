"""BrokerAdapter 統一介面 — 核心程式只依賴這個介面,不碰各券商原始 SDK。

日後加嘉信/盈透,只需各自實作這個介面,run.py 完全不用改。
"""
from abc import ABC, abstractmethod
from decimal import Decimal

from core.models import Position


class BrokerAdapter(ABC):
    name: str = "base"

    @abstractmethod
    def connect(self) -> None:
        """建立連線/登入。重複呼叫應為 no-op。"""

    @abstractmethod
    def list_positions(self) -> list[Position]:
        """回傳目前全部持倉(統一模型)。"""

    @abstractmethod
    def list_cash(self) -> dict[str, Decimal]:
        """回傳各幣別現金餘額,例如 {'TWD': Decimal('320000')}。"""

    def list_transactions(self) -> list:
        """回傳「今天」的成交(統一 Transaction 模型)。
        P1 用來累積交易史,讓回補在有買賣的區間也準確。
        預設回空清單,各 adapter 視能力覆寫;失敗不應拋出例外。"""
        return []

    def list_realized_pnl(self, begin: str, end: str) -> list:
        """回傳 [begin, end] 區間的已實現損益(統一 RealizedPnl 模型)。
        P3 用。預設回空清單,各 adapter 視能力覆寫;失敗不應拋出例外。"""
        return []

    def subscribe_ticks(self, symbols: list[str], on_tick) -> bool:
        """訂閱即時成交報價(P3,watch.py 用)。
        on_tick(symbol: str, price: float, ts_iso: str) 由 adapter 在
        每筆成交時呼叫(可能來自其他執行緒,呼叫端需自行處理)。
        回傳是否支援/訂閱成功;預設不支援。"""
        return False

    def disconnect(self) -> None:
        """登出/釋放連線。預設 no-op,各 adapter 視需要覆寫。"""
