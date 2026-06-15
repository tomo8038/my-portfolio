"""對帳單解析 + 持倉重播引擎 — P4d。

把三家券商的 CSV 對帳單(永豐 / 嘉信 / 盈透)轉成:
  1. 統一 Transaction 流水(寫進 transactions 表,供回補引擎反向重播)
  2. 重建「目前持倉」(forward replay:由最舊一筆往今天逐筆累積)
  3. 各券商現金餘額、帳戶起始日

設計重點
--------
* 平均成本法:買進/再投入/轉入累加成本與股數;賣出/轉出按比例沖銷成本,
  並算出已實現損益(賣出金額 − 沖銷成本)。
* 拆股(split)兩條路:
    - 嘉信對帳單「Stock Split」列已直接給「新增股數」→ 重播時 qty += 新增股數
      (總成本不變,均價自動減半),不需再向行情要倍數。
    - 盈透 / 永豐對帳單沒有拆股列 → 向 yfinance 取該標的的歷史分割事件,
      在分割日把當時持股 × 倍數(總成本不變)。沒網路時可傳入 split_map 覆寫。
* In-Kind 實物轉撥:轉出端當「無現金、無損益的減量」、轉入端當「帶成本基礎的加量」。
* 幣別:永豐 TWD、嘉信 / 盈透 USD。
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

# ====================================================================
# 標準化事件:解析層輸出、重播層輸入
# ====================================================================

# 影響「股數」的事件
SHARE_TYPES = {"BUY", "SELL", "REINVEST_BUY", "TRANSFER_IN",
               "TRANSFER_OUT", "SPLIT_ADD", "AWARD"}
# 只影響「現金」的事件(寫進 transactions 供 TWR / 回補用)
CASH_TYPES = {"DIVIDEND", "FEE", "INTEREST", "DEPOSIT", "WITHDRAW",
              "CASH_IN_LIEU"}


@dataclass
class Event:
    date: str            # YYYY-MM-DD
    etype: str           # 見上方類型
    symbol: str          # 空字串代表純現金事件
    qty: Decimal
    price: Decimal
    amount: Decimal      # 含費稅的「淨現金流」:流出為負、流入為正
    raw: str             # 原始 Action / Transaction Type(診斷用)
    ext_id: str          # 去重用唯一鍵


@dataclass
class Statement:
    broker: str
    ccy: str
    events: list[Event] = field(default_factory=list)
    # 對帳單是否含出入金 → 推得的現金是否為「真實帳戶現金」。
    # 嘉信/盈透含 Wire/Deposit ⇒ True;永豐純成交流水 ⇒ False(真實現金走 Shioaji)。
    cash_is_real: bool = True

    @property
    def start_date(self) -> str | None:
        ds = [e.date for e in self.events]
        return min(ds) if ds else None


# ====================================================================
# 共用小工具
# ====================================================================

def _money(s: str) -> Decimal:
    """'-$94,488.61' / '$1,234' / '' → Decimal。空字串為 0。"""
    s = (s or "").strip().replace("$", "").replace(",", "")
    if s in ("", "-", "--"):
        return Decimal(0)
    if s.startswith("(") and s.endswith(")"):   # 括號表負數
        s = "-" + s[1:-1]
    try:
        return Decimal(s)
    except Exception:
        return Decimal(0)


def _num(s: str) -> Decimal:
    s = (s or "").strip().replace(",", "")
    if s in ("", "-", "--"):
        return Decimal(0)
    try:
        return Decimal(s)
    except Exception:
        return Decimal(0)


def _read_tolerant(path: Path) -> list[list[str]]:
    """容錯讀 CSV:utf-8-sig / utf-8 / big5 / cp950 / latin-1 依序嘗試。"""
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-8", "big5", "cp950", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="ignore")
    return list(csv.reader(text.splitlines()))


def _iso(d: str, fmts: tuple[str, ...]) -> str:
    d = d.split(" as of ")[-1].strip() if " as of " in d else d.strip()
    for f in fmts:
        try:
            return datetime.strptime(d, f).strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise ValueError(f"無法解析日期:{d!r}")


# ====================================================================
# 永豐(Sinopac)— 純成交流水
# 欄位:成交日,商品,買賣,數量,成交價,價金,手續費,交易稅,應付金額,應收金額,...,委託單號,幣別
# ====================================================================

def parse_sinopac(path) -> Statement:
    rows = _read_tolerant(path)
    out: list[Event] = []
    has_deposit = False
    for i, r in enumerate(rows[1:], 1):
        if len(r) < 10 or not r[0].strip():
            continue
        date = _iso(r[0], ("%Y/%m/%d", "%Y-%m-%d"))
        product = r[1].strip()
        sym = product.split()[0] if product else ""
        side = r[2].strip()                      # 現買 / 現賣 / 入金 / 配息
        qty = _num(r[3])
        price = _num(r[4])
        gross = _num(r[5])                       # 價金(入金:即入金金額)
        payable = _num(r[8])                     # 應付金額(買:含手續費)
        receivable = _num(r[9])                  # 應收金額(賣/配息:扣費稅後)
        order_no = (r[14].strip() if len(r) > 14 else "") or f"row{i}"
        ext = f"sinopac-{date}-{order_no}-{i}"
        if "入金" in side:
            # 入金:純現金流入(無標的、無股數);價金欄即入金金額
            out.append(Event(date, "DEPOSIT", "", Decimal(0), Decimal(0),
                             gross, side, ext))
            has_deposit = True
        elif "配息" in side:
            # 配息:應收金額為實收現金(已扣手續費);掛在該標的下
            amt = receivable if receivable else gross
            out.append(Event(date, "DIVIDEND", sym, Decimal(0), Decimal(0),
                             amt, side, ext))
        elif "買" in side:
            out.append(Event(date, "BUY", sym, qty, price, -payable,
                             side, ext))
        elif "賣" in side:
            out.append(Event(date, "SELL", sym, qty, price, receivable,
                             side, ext))
    # 對帳單一旦含「入金」,回放推得的現金即為真實帳戶現金 → 可採計、寫入 broker_cash。
    # 純成交流水(無入金)時維持 False:那時的「現金」只是買賣淨額,並非帳戶餘額。
    return Statement("sinopac", "TWD", out, cash_is_real=has_deposit)


# ====================================================================
# 嘉信(Schwab)
# 欄位:Date,Action,Symbol,Description,Quantity,Price,Fees & Comm,Amount
# ====================================================================

_SCHWAB_MAP = {
    "Buy": "BUY", "Sell": "SELL",
    "Reinvest Shares": "REINVEST_BUY",
    "Stock Split": "SPLIT_ADD",
    "Security Transfer": "_TRANSFER",          # 依正負號 / 有無現金再判
    "Journal": "_JOURNAL",                     # 多為成對沖銷,淨值為 0
    # 純現金:
    "Credit Interest": "INTEREST",
    "Reinvest Dividend": "DIVIDEND",
    "Qualified Dividend": "DIVIDEND",
    "Cash Dividend": "DIVIDEND",
    "Special Dividend": "DIVIDEND",
    "Qual Div Reinvest": "DIVIDEND",
    "Long Term Cap Gain Reinvest": "DIVIDEND",
    "NRA Tax Adj": "FEE", "Foreign Tax Paid": "FEE",
    "ADR Mgmt Fee": "FEE", "Service Fee": "FEE",
    "Wire Received": "DEPOSIT", "Wire Sent": "WITHDRAW",
    "Promotional Award": "DEPOSIT", "Misc Cash Entry": "DEPOSIT",
    "Cash In Lieu": "CASH_IN_LIEU",
}


def parse_schwab(path) -> Statement:
    rows = _read_tolerant(path)
    out: list[Event] = []
    for i, r in enumerate(rows[1:], 1):
        if len(r) < 8 or not r[0].strip():
            continue
        date = _iso(r[0], ("%m/%d/%Y",))
        action = r[1].strip()
        sym = r[2].strip()
        qty = _num(r[4])
        price = _money(r[5])
        amount = _money(r[7])
        etype = _SCHWAB_MAP.get(action)
        if etype is None:
            continue
        ext = f"schwab-{date}-{action}-{sym}-{i}"

        if etype == "_TRANSFER":
            if sym and qty != 0:                 # 證券實物轉撥
                t = "TRANSFER_IN" if qty > 0 else "TRANSFER_OUT"
                out.append(Event(date, t, sym, abs(qty), price, amount,
                                 action, ext))
            else:                                # 現金轉撥(ACAT 現金)
                t = "DEPOSIT" if amount >= 0 else "WITHDRAW"
                out.append(Event(date, t, "", Decimal(0), Decimal(0),
                                 amount, action, ext))
        elif etype == "_JOURNAL":
            # 成對證券 journal(±同量)→ 當作股數調整,自然互相抵銷
            if sym and qty != 0:
                t = "TRANSFER_IN" if qty > 0 else "TRANSFER_OUT"
                out.append(Event(date, t, sym, abs(qty), Decimal(0),
                                 Decimal(0), action, ext))
        else:
            out.append(Event(date, etype, sym, qty, price, amount, action, ext))
    return Statement("schwab", "USD", out)


# ====================================================================
# 盈透(IBKR)
# 欄位:...,Date,Account,Description,Transaction Type,Symbol,Quantity,Price,
#       Gross Amount,Commission,Net Amount
# ====================================================================

_IBKR_MAP = {
    "Buy": "BUY", "Sell": "SELL",
    "In-Kind": "TRANSFER_IN",
    "Promotional Award": "AWARD",
    "Dividend": "DIVIDEND",
    "Payment in Lieu": "DIVIDEND",
    "Foreign Tax Withholding": "FEE",
    "Credit Interest": "INTEREST",
    "Deposit": "DEPOSIT", "Withdrawal": "WITHDRAW",
}


def parse_ibkr(path) -> Statement:
    rows = _read_tolerant(path)
    header = rows[0]
    idx = {name.strip(): k for k, name in enumerate(header)}

    def col(r, name, default=""):
        k = idx.get(name)
        return r[k] if k is not None and k < len(r) else default

    out: list[Event] = []
    for i, r in enumerate(rows[1:], 1):
        if col(r, "Header").strip() != "Data":
            continue
        date = _iso(col(r, "Date"), ("%Y/%m/%d", "%Y/%-m/%-d", "%Y-%m-%d"))
        ttype = col(r, "Transaction Type").strip()
        sym = col(r, "Symbol").strip()
        qty = _num(col(r, "Quantity"))
        price = _num(col(r, "Price"))
        net = _num(col(r, "Net Amount"))
        etype = _IBKR_MAP.get(ttype)
        if etype is None:
            continue
        ext = f"ibkr-{date}-{ttype}-{sym}-{i}"
        # IBKR 賣出 Quantity 為負;BUY/AWARD/TRANSFER_IN 用絕對值
        out.append(Event(date, etype, sym, abs(qty), price, net, ttype, ext))
    return Statement("ibkr", "USD", out)


# ====================================================================
# 自動辨識格式
# ====================================================================

def detect_and_parse(path) -> Statement:
    rows = _read_tolerant(path)
    head = ",".join(rows[0]).lower() if rows else ""
    if "transaction history" in head or "transaction type" in head:
        return parse_ibkr(path)
    if "action" in head and "fees & comm" in head:
        return parse_schwab(path)
    if "成交日" in head or "委託單號" in head or "buy" not in head and "現" in "".join(
            r[2] if len(r) > 2 else "" for r in rows[1:3]):
        return parse_sinopac(path)
    # 後備:看欄數
    n = len(rows[0]) if rows else 0
    if n >= 12:
        return parse_ibkr(path)
    if n == 8:
        return parse_schwab(path)
    return parse_sinopac(path)


# ====================================================================
# 重播引擎:事件流 → 目前持倉 + 現金 + 已實現損益
# ====================================================================

@dataclass
class Holding:
    qty: Decimal = Decimal(0)
    cost: Decimal = Decimal(0)        # 總成本(原幣,平均成本法)

    @property
    def avg_cost(self) -> Decimal:
        return (self.cost / self.qty) if self.qty else Decimal(0)


@dataclass
class ReplayResult:
    holdings: dict[str, Holding]
    cash: Decimal
    realized: list[dict]              # [{date, symbol, qty, proceeds, pnl}]
    names: dict[str, str]


def replay(stmt: Statement,
           split_map: dict[str, list[tuple[str, Decimal]]] | None = None,
           names: dict[str, str] | None = None) -> ReplayResult:
    """forward replay。split_map: {symbol: [(date, ratio), ...]}(用於無拆股列的券商)。"""
    split_map = split_map or {}
    names = names or {}
    hold: dict[str, Holding] = {}
    cash = Decimal(0)
    realized: list[dict] = []

    # 把分割事件併入時間軸:用 (date, order, payload) 排序,確保同日分割先於買賣後處理
    pending_splits = {sym: sorted(lst) for sym, lst in split_map.items()}

    def apply_splits_upto(sym: str, date: str):
        lst = pending_splits.get(sym)
        if not lst:
            return
        keep = []
        for sd, ratio in lst:
            if sd <= date:
                h = hold.get(sym)
                if h and h.qty:
                    h.qty *= ratio          # 總成本不變、股數 × 倍數 → 均價自動調整
            else:
                keep.append((sd, ratio))
        pending_splits[sym] = keep

    # In-Kind 轉撥 / 贈股 / 拆股的 amount 代表「股票價值」而非現金,不可入帳
    NON_CASH = {"TRANSFER_IN", "TRANSFER_OUT", "AWARD", "SPLIT_ADD"}

    for e in sorted(stmt.events, key=lambda x: x.date):
        if e.etype not in NON_CASH:
            cash += e.amount                 # 僅真正的現金流入帳
        if e.symbol:
            apply_splits_upto(e.symbol, e.date)

        if e.etype in ("BUY", "REINVEST_BUY", "TRANSFER_IN", "AWARD"):
            h = hold.setdefault(e.symbol, Holding())
            h.qty += e.qty
            h.cost += abs(e.amount) if e.amount != 0 else (e.qty * e.price)
        elif e.etype == "SELL":
            h = hold.setdefault(e.symbol, Holding())
            sold = min(e.qty, h.qty) if h.qty > 0 else e.qty
            cost_out = h.avg_cost * sold
            realized.append({
                "date": e.date, "symbol": e.symbol, "qty": float(sold),
                "proceeds": float(e.amount), "pnl": float(e.amount - cost_out),
            })
            h.qty -= e.qty
            h.cost -= cost_out
            if h.qty <= Decimal("0.0000001"):
                h.qty = Decimal(0)
                h.cost = Decimal(0)
        elif e.etype == "TRANSFER_OUT":
            h = hold.setdefault(e.symbol, Holding())
            out_cost = h.avg_cost * min(e.qty, h.qty) if h.qty else Decimal(0)
            h.qty -= e.qty
            h.cost -= out_cost
            if h.qty <= Decimal("0.0000001"):
                h.qty = Decimal(0); h.cost = Decimal(0)
        elif e.etype == "SPLIT_ADD":
            # 嘉信:對帳單已給「新增股數」,總成本不變
            h = hold.setdefault(e.symbol, Holding())
            h.qty += e.qty
        # DIVIDEND / FEE / INTEREST / DEPOSIT / WITHDRAW / CASH_IN_LIEU 只動現金

    # 收尾:把分割日在最後一筆交易「之後」的(例如清倉後才分割)忽略;
    # 仍持有的標的若還有未套用分割,套用之
    for sym, lst in pending_splits.items():
        h = hold.get(sym)
        for _sd, ratio in lst:
            if h and h.qty:
                h.qty *= ratio

    hold = {s: h for s, h in hold.items() if h.qty > Decimal("0.0000001")}
    return ReplayResult(hold, cash, realized, names)


# ====================================================================
# 從事件流取每檔標的中文 / 英文名稱(顯示用)
# ====================================================================

def collect_names(stmt: Statement, path) -> dict[str, str]:
    """嘉信 / 永豐 對帳單第 4 欄是名稱;回傳 {symbol: name}。"""
    rows = _read_tolerant(path)
    out: dict[str, str] = {}
    if stmt.broker == "schwab":
        for r in rows[1:]:
            if len(r) >= 4 and r[2].strip():
                out.setdefault(r[2].strip(), r[3].strip().title())
    elif stmt.broker == "sinopac":
        for r in rows[1:]:
            if len(r) >= 2 and r[1].strip():
                parts = r[1].strip().split(None, 1)
                if len(parts) == 2:
                    out.setdefault(parts[0], parts[1])
    elif stmt.broker == "ibkr":
        for r in rows[1:]:
            # Description 含公司名;Symbol 在固定欄
            pass
    return out
