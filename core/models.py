"""統一資料模型 — 所有券商 adapter 的輸出都轉成這裡的型別。

P0 只需要 Position(持倉)與現金餘額 dict。
Transaction 留待 P1 回補引擎使用。
"""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass
class Position:
    broker: str            # 'sinopac' / 'schwab' / 'ibkr'
    account_id: str        # 帳戶代號(自訂,例如 'stock')
    symbol: str            # '2330'
    name: str              # '台積電'
    asset_class: str       # 'equity' / 'etf' / 'cash' ...
    qty: Decimal           # 持有股數
    avg_cost: Decimal      # 平均成本(原幣)
    last_price: Decimal    # 現價(原幣)
    ccy: str               # 'TWD'
    industry: str = ""     # 產業別(P2 配置圖用;查不到留空,顯示為「其他」)
    as_of: datetime = field(default_factory=datetime.now)

    @property
    def market_value(self) -> Decimal:
        """原幣市值"""
        return self.qty * self.last_price

    @property
    def cost_value(self) -> Decimal:
        """原幣投入成本"""
        return self.qty * self.avg_cost

    @property
    def unrealized_pnl(self) -> Decimal:
        """原幣未實現損益"""
        return self.market_value - self.cost_value


@dataclass
class Transaction:
    """統一交易模型(P1)。

    amount 慣例:含費稅後的「淨現金流」——
    買進為負(現金流出)、賣出/股息/入金為正(現金流入)。
    回補引擎的反向重播依賴這個正負號慣例。
    """
    broker: str
    external_id: str       # 券商端唯一代號,去重用
    symbol: str
    txn_type: str          # BUY / SELL / DIVIDEND / FEE / DEPOSIT / WITHDRAW
    qty: Decimal
    price: Decimal
    amount: Decimal        # 淨現金流(見上)
    ccy: str
    trade_date: str        # 'YYYY-MM-DD'


@dataclass
class RealizedPnl:
    """已實現損益(P3)。

    來源:券商的平倉損益查詢(永豐 list_profit_loss)。
    pnl 為券商回報的已實現損益金額(含費稅,原幣),以此為準;
    qty/price 單位依券商回報,僅供參考顯示。
    """
    broker: str
    external_id: str       # 券商端唯一代號,去重用
    symbol: str
    qty: Decimal
    price: Decimal
    pnl: Decimal           # 已實現損益(原幣)
    ccy: str
    trade_date: str        # 'YYYY-MM-DD'
