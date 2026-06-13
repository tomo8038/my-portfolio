"""
parse_ibkr_csv.py — 盈透 IBKR「Transaction History」CSV 解析器

把 IBKR 下載的交易明細 CSV 正規化成統一事件流(events),供 build_history_ibkr.py 重播。
對映嘉信 P4b 的 parse_schwab_csv.py 角色。

IBKR Transaction History 欄位(逗號分隔,每列前兩欄為 "Transaction History","Data"):
    Date, Account, Description, Transaction Type, Symbol, Quantity, Price,
    Gross Amount, Commission, Net Amount

支援的 Transaction Type(目前帳戶出現 9 種)→ 統一事件 kind:
    Buy                     -> buy        (動現金、加股、加成本)
    Sell                    -> sell       (動現金、減股、結已實現)
    Dividend                -> dividend   (動現金:收益)
    Payment in Lieu         -> pil        (動現金:收益,等同股息;借券補償)
    Credit Interest         -> interest   (動現金:收益)
    Foreign Tax Withholding -> tax        (動現金:支出,通常為股息/PIL 的 30% NRA 預扣)
    Deposit / Withdraw      -> cash_in / cash_out  (純現金出入金;外部資本)
    In-Kind                 -> transfer_in / transfer_out (帶 symbol+qty 的實物轉撥;外部資本,非現金)
    Promotional Award       -> award      (帶 symbol+qty 的贈股;收益,非現金,計入績效)

非現金事件(transfer / award)的 Net Amount 是「該批證券的價值」,不可當現金加總。

另注入「CSV 不會出現」的公司行動:
    SPLITS:股票分割(IBKR Transactions CSV 不含分割列,需自行注入)
    RENAMES:代號更名(抓歷史價時轉換)
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable


# ── 需手動維護的公司行動 ───────────────────────────────────────────────
# 分割:effective date(開盤生效日),ratio=每 1 股變成幾股(2:1 正向分割 → 2.0)
SPLITS: dict[str, list[tuple[date, float]]] = {
    "QLD": [(date(2025, 11, 20), 2.0)],   # ProShares 2:1 正向分割,2025/11/20 開盤生效
}

# 代號更名:抓歷史價時用新代號(舊 -> 新)
RENAMES: dict[str, str] = {
    # "FB": "META", "SQ": "XYZ",   # 範例;IBKR 此帳戶目前無
}

# 以面額計值、不抓股價的工具(公債 CUSIP 前綴等)。IBKR 此帳戶目前無。
FACE_VALUE_PREFIXES: tuple[str, ...] = ("912797", "912796")


@dataclass
class Event:
    date: date
    kind: str           # buy/sell/dividend/pil/interest/tax/cash_in/cash_out/
                        # transfer_in/transfer_out/award/split
    symbol: str | None  # 標的(現金事件為 None)
    qty: float          # 股數(非交易事件為 0;split 為比率)
    price: float        # 每股價(無則 0)
    amount: float       # 現金影響(Net Amount;非現金事件=該批證券價值或 0)
    description: str = ""
    is_cash: bool = True        # 是否實際動到現金餘額
    is_external: bool = False   # 是否為外部資本流(入金/實物轉撥;TWR 要排除)
    raw_type: str = ""          # 原始 Transaction Type


def _num(s: str | None) -> float:
    s = (s or "").strip()
    if s in ("", "-", "--"):
        return 0.0
    return float(s.replace(",", ""))


def _parse_date(s: str) -> date:
    s = s.strip()
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"無法解析日期: {s!r}")


# Transaction Type(小寫、去空白後)→ 事件分類
_DEPOSIT_TYPES = {"deposit", "cash transfer"}
_WITHDRAW_TYPES = {"withdraw", "withdrawal"}
_INKIND_TYPES = {"in-kind", "inkind", "in kind", "stock transfer", "security transfer"}


def classify(tt: str, symbol: str | None, qty: float) -> str:
    """把原始 Transaction Type 映射成統一事件 kind。"""
    t = tt.strip().lower()
    has_sec = bool(symbol) and symbol not in ("-", "") and abs(qty) > 1e-12

    if t == "buy":
        return "buy"
    if t == "sell":
        return "sell"
    if t == "dividend":
        return "dividend"
    if t in ("payment in lieu", "payment in lieu of dividend"):
        return "pil"
    if t == "credit interest":
        return "interest"
    if t in ("foreign tax withholding", "nra tax adj", "nra withholding"):
        return "tax"
    if t == "promotional award":
        return "award"
    if t in _INKIND_TYPES:
        return "transfer_in" if qty >= 0 else "transfer_out"
    if t in _DEPOSIT_TYPES:
        # 帶 symbol+qty 的 "Deposit" 其實是實物轉撥(舊版 IBKR 匯出會這樣標)
        return "transfer_in" if has_sec else "cash_in"
    if t in _WITHDRAW_TYPES:
        return "transfer_out" if has_sec else "cash_out"
    raise ValueError(f"未知的 Transaction Type: {tt!r}(請在 classify() 補對映)")


def _event_from_row(row: list[str]) -> Event | None:
    # 欄位索引:0 固定字串,1 Header/Data,2 Date,3 Account,4 Description,
    # 5 Transaction Type,6 Symbol,7 Quantity,8 Price,9 Gross,10 Commission,11 Net
    if len(row) < 12 or row[1].strip() != "Data":
        return None
    d = _parse_date(row[2])
    desc = row[4].strip()
    tt = row[5].strip()
    sym = row[6].strip() or None
    if sym in ("-", ""):
        sym = None
    qty = _num(row[7])
    price = _num(row[8])
    net = _num(row[11])
    kind = classify(tt, sym, qty)

    is_cash = kind not in ("transfer_in", "transfer_out", "award", "split")
    is_external = kind in ("cash_in", "cash_out", "transfer_in", "transfer_out")

    return Event(
        date=d, kind=kind, symbol=sym, qty=qty, price=price, amount=net,
        description=desc, is_cash=is_cash, is_external=is_external, raw_type=tt,
    )


def _split_events() -> list[Event]:
    out: list[Event] = []
    for sym, lst in SPLITS.items():
        for eff, ratio in lst:
            out.append(Event(date=eff, kind="split", symbol=sym, qty=ratio,
                             price=0.0, amount=0.0,
                             description=f"{sym} {ratio:g}:1 split",
                             is_cash=False, is_external=False, raw_type="Split"))
    return out


def parse_csv(path: str) -> list[Event]:
    """讀 IBKR CSV → 依日期排序的事件流(同日 split 排在交易之前)。"""
    rows: list[list[str]] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))

    events: list[Event] = []
    for row in rows:
        ev = _event_from_row(row)
        if ev is not None:
            events.append(ev)

    events.extend(_split_events())
    # 同日:split 先於其他事件(0),其餘維持原序(1)
    events.sort(key=lambda e: (e.date, 0 if e.kind == "split" else 1))
    return events


def price_symbol(symbol: str) -> str:
    """抓歷史價時使用的代號(套用更名映射)。"""
    return RENAMES.get(symbol, symbol)


def is_face_value(symbol: str) -> bool:
    return any(symbol.startswith(p) for p in FACE_VALUE_PREFIXES)


# ── CLI:盤點 ───────────────────────────────────────────────────────────
def _summary(events: list[Event]) -> None:
    from collections import Counter
    kinds = Counter(e.kind for e in events)
    syms = Counter(e.symbol for e in events if e.symbol)
    span = [e.date for e in events if e.kind != "split"]
    print(f"事件數: {len(events)}  期間: {min(span)} ~ {max(span)}")
    print("\n事件分類:")
    for k, v in kinds.most_common():
        print(f"  {k:14s} {v}")
    print("\n標的:")
    for s, v in syms.most_common():
        print(f"  {s:6s} {v}")
    print("\n注入的公司行動:")
    for e in events:
        if e.kind == "split":
            print(f"  {e.date}  {e.description}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法: python parse_ibkr_csv.py <IBKR_TRANSACTIONS.csv>")
        sys.exit(1)
    evs = parse_csv(sys.argv[1])
    _summary(evs)
