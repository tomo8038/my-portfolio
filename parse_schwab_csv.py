"""嘉信交易明細 CSV 解析器 — P4b(離線回放)。

把嘉信下載的 Transactions CSV(欄位:Date, Action, Symbol, Description,
Quantity, Price, Fees & Comm, Amount)逐筆解析成統一的事件流,供
build_history.py 反向/正向重播,重建每日持倉、現金、已實現損益與淨值。

為什麼用交易明細回放,而非接 API:
  嘉信 API 註冊限美國人、且授權需數個工作天。交易明細反而更完整——
  涵蓋開戶第一天到今天的每一筆,能還原 API 做不到的「深歷史」。

== 嘉信 22 種 Action 的語義對映 ==
(經實際資料核對:CSV 的 Amount 欄已是「該筆對現金帳戶的淨影響」,
 全部 Amount 加總 = 當前閒置現金餘額,本案為 $8.46,自洽。)

持股與現金都變動:
  Buy                       買進        股數+  現金-(Amount 為負)
  Sell                      賣出        股數-  現金+(Amount 為正)
  Reinvest Shares           股息再投入   股數+  現金-(配息買回零股)
  Cash In Lieu              拆股畸零換現 現金+(碎股不給,折現)

只變動持股(不動現金):
  Stock Split               股票分割     股數+(Quantity=新增股數)→ 見下方說明
  Journal                   內部調撥     成對 ±X,淨效果 0(過券/重分類)
  Security Transfer         證券轉移     股數±(ACAT 轉入/轉出他券商)

只變動現金(配息、利息、稅、費):
  Reinvest Dividend         再投資前的配息入帳(隨後 Reinvest Shares 買回)
  Qual Div Reinvest         合格股息(再投資型)
  Qualified Dividend        合格股息(現金)
  Cash Dividend             現金股息
  Special Dividend          特別股息
  Long Term Cap Gain Reinvest 長期資本利得分配(再投資)
  Credit Interest           利息收入
  NRA Tax Adj               非居民預扣稅調整(通常為負)
  Foreign Tax Paid          外國稅(ADR,如 TSM/BABA)
  ADR Mgmt Fee              ADR 管理費
  Service Fee               服務費(如電匯費)
  Misc Cash Entry           雜項現金(如 waive wire fee)
  Promotional Award         開戶/推廣獎勵金

外部現金流(出入金,TWR 用):
  Wire Received             電匯轉入     現金+   → DEPOSIT
  Wire Sent                 電匯轉出     現金-   → WITHDRAW
  Security Transfer(無Symbol 純現金 ACAT) → 視金額正負為 DEPOSIT/WITHDRAW

== Stock Split 的處理(關鍵)==
嘉信對「正向分割」(如 QLD 2025/11 的 2:1)記成一筆 Stock Split,
Quantity = 新增的股數(例:原持 1,772 股 → 記 +1,772,持股翻為 3,544)。
所以重播持股時,Stock Split 的 Quantity 直接「相加」即可,無需再乘比率。

但畫歷史市值曲線時要注意:yfinance 抓回的歷史價是「分割調整後」的連續
價格(分割前的價也被除以 2)。若我們用「今天的股數 × 分割調整價」,
分割前的市值會被低估。因此 build_history.py 採「反向重播當時的真實股數」
(分割前用分割前股數)配「未調整原始價」,兩者一致,市值才正確。
本解析器只負責產生事件;分割點與調整邏輯交給 build_history.py。
"""
import csv
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

# ---- 統一事件模型(比 Transaction 多帶 raw_action / split 標記)----


@dataclass
class Event:
    date: str            # 'YYYY-MM-DD'(trade date;'as of' 以結算日為準)
    action: str          # 正規化後類別:見下方 KIND_*
    raw_action: str      # 原始 Action 字串(保留供稽核)
    symbol: str
    name: str
    qty: Decimal         # 股數變化(帶正負:買+、賣-)
    price: Decimal       # 單價(原幣 USD;無則 0)
    fee: Decimal         # 手續費(正值)
    amount: Decimal      # 對現金的淨影響(原幣 USD,帶正負;CSV 原值)
    ccy: str = "USD"


# 正規化類別
KIND_BUY = "BUY"
KIND_SELL = "SELL"
KIND_SPLIT = "SPLIT"          # 持股+,不動現金
KIND_TRANSFER = "TRANSFER"    # 持股±(證券 ACAT),不動現金
KIND_JOURNAL = "JOURNAL"      # 成對 ±,淨 0
KIND_DIVIDEND = "DIVIDEND"    # 配息/利息類現金流入(不動持股)
KIND_FEE = "FEE"              # 稅費類現金流出(不動持股)
KIND_DEPOSIT = "DEPOSIT"      # 外部入金
KIND_WITHDRAW = "WITHDRAW"    # 外部出金
KIND_REINVEST_SHARES = "REINVEST_SHARES"  # 配息買回零股:持股+、現金-

# 原始 Action → 類別
_ACTION_KIND = {
    "Buy": KIND_BUY,
    "Sell": KIND_SELL,
    "Reinvest Shares": KIND_REINVEST_SHARES,
    "Stock Split": KIND_SPLIT,
    "Journal": KIND_JOURNAL,
    "Security Transfer": KIND_TRANSFER,     # 有 Symbol 時;無 Symbol 另判
    "Cash In Lieu": KIND_DIVIDEND,          # 碎股折現,當現金流入
    "Reinvest Dividend": KIND_DIVIDEND,
    "Qual Div Reinvest": KIND_DIVIDEND,
    "Qualified Dividend": KIND_DIVIDEND,
    "Cash Dividend": KIND_DIVIDEND,
    "Special Dividend": KIND_DIVIDEND,
    "Long Term Cap Gain Reinvest": KIND_DIVIDEND,
    "Credit Interest": KIND_DIVIDEND,
    "Promotional Award": KIND_DIVIDEND,
    "Misc Cash Entry": KIND_DIVIDEND,
    "NRA Tax Adj": KIND_FEE,
    "Foreign Tax Paid": KIND_FEE,
    "ADR Mgmt Fee": KIND_FEE,
    "Service Fee": KIND_FEE,
    "Wire Received": KIND_DEPOSIT,
    "Wire Sent": KIND_WITHDRAW,
}

# 代號隨時間更名 → 對映到 yfinance 現行代號(抓歷史價用)
# yfinance 以「現行代號」回傳改名前後的完整連續歷史,故映射到新代號即可。
SYMBOL_ALIASES = {
    "FB": "META",       # Facebook → Meta(2022/06)
    "SQ": "XYZ",        # Square → Block Inc(2025/01/20-21)
}


def _money(s: str) -> Decimal:
    s = (s or "").strip().replace("$", "").replace(",", "").replace('"', "")
    if not s:
        return Decimal(0)
    neg = s.startswith("-")
    s = s.lstrip("-").strip()
    if not s:
        return Decimal(0)
    return (Decimal("-1") if neg else Decimal(1)) * Decimal(s)


def _qty(s: str) -> Decimal:
    s = (s or "").strip().replace(",", "")
    return Decimal(s) if s else Decimal(0)


def _date(s: str) -> str:
    """'06/12/2026' 或 '11/20/2025 as of 11/19/2025' → 結算日 ISO。

    有 'as of' 時以結算日(as of 後者)為準,讓拆股/碎股對齊正確的日子。
    """
    s = s.strip()
    m = re.search(r"as of (\d{2}/\d{2}/\d{4})", s)
    raw = m.group(1) if m else s.split()[0]
    return datetime.strptime(raw, "%m/%d/%Y").strftime("%Y-%m-%d")


def is_cusip(symbol: str) -> bool:
    """9 碼英數且開頭含數字 → 美國公債 CUSIP(yfinance 無、以面額計值)。"""
    s = symbol.strip()
    return bool(re.fullmatch(r"[0-9A-Z]{9}", s)) and any(c.isdigit()
                                                         for c in s[:3])


def parse_csv(path: str | Path) -> list[Event]:
    """解析嘉信 CSV → 依日期由舊到新排序的事件流。"""
    path = Path(path)
    raw_rows = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if not r.get("Date"):
                continue
            raw_rows.append(r)

    # CSV 是新→舊;反轉成舊→新,並以結算日穩定排序
    raw_rows.reverse()

    events: list[Event] = []
    for r in raw_rows:
        action = r["Action"].strip()
        symbol = r["Symbol"].strip()
        kind = _ACTION_KIND.get(action)

        # 無 Symbol 的 Security Transfer 是純現金 ACAT → 依金額正負當出入金
        if action == "Security Transfer" and not symbol:
            amt = _money(r["Amount"])
            kind = KIND_DEPOSIT if amt >= 0 else KIND_WITHDRAW

        if kind is None:
            raise ValueError(f"未對映的 Action:{action!r}(請補進 _ACTION_KIND)")

        q = _qty(r["Quantity"])
        # 賣出的股數在 CSV 是正值,轉成負(持股減少)
        if kind == KIND_SELL:
            q = -abs(q)
        # 買進/再投資/拆股的股數為正(CSV 本就正)
        # Journal / Transfer 的 Quantity 已帶正負,原樣保留

        events.append(Event(
            date=_date(r["Date"]),
            action=kind,
            raw_action=action,
            symbol=symbol,
            name=r["Description"].strip(),
            qty=q,
            price=_money(r["Price"]),
            fee=_money(r["Fees & Comm"]),
            amount=_money(r["Amount"]),
        ))

    return events


def yf_symbol(symbol: str) -> str:
    """抓歷史價用的 yfinance 代號(處理更名)。"""
    return SYMBOL_ALIASES.get(symbol, symbol)


if __name__ == "__main__":
    import sys
    evs = parse_csv(sys.argv[1] if len(sys.argv) > 1 else
                    "Individual_XXX743_Transactions_20200825-20260612.csv")
    print(f"解析 {len(evs)} 筆事件,{evs[0].date} ~ {evs[-1].date}")
    from collections import Counter
    c = Counter(e.action for e in evs)
    for k, n in c.most_common():
        print(f"  {n:4d}  {k}")
