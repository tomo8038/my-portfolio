"""跨券商資產分類 + 產業別 — 台股 / 美股共用,集中一處維護。

回傳值:
  asset_class : 'etf' / 'equity' / 'bond' / 'cash'
  industry    : 中文產業/類股;ETF 一律 'ETF'、債券 '債券'、查不到 '其他'。

設計原則(離線優先、可預期、可選增強):
  1) 先用「內建規則 + 清單 + 名稱啟發式」判斷 —— 離線、即時、結果穩定。
  2) 只有在呼叫端明確開啟 use_yfinance=True 時,才對「規則判不出產業」的
     美股上網查 sector(yfinance),並寫入本地 JSON 快取,之後離線可重用。
     沒網路 / 沒裝 yfinance 一律安全略過,不影響主流程。

為什麼用「規則 + 清單」而非全靠 API:
  使用者的標的數量有限且固定,白名單 + 名稱關鍵字即可 100% 命中,
  且結果不隨外部資料源變動(API 偶爾把槓桿型 ETF 回成 EQUITY)。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────
# 美股 ETF 白名單(已知、常見;不在名單者再走名稱啟發式)
# ─────────────────────────────────────────────────────────────────────
US_ETFS = {
    "QLD", "QQQ", "QQQM", "TQQQ", "SSO", "SPY", "VOO", "IVV",
    "VT", "VTI", "VXUS", "VEA", "VWO", "VGIT", "VGSH", "BND", "AGG",
    "SGOV", "BIL", "SHV", "SHY", "TLT", "IEF",
    "ARKK", "ARKF", "ARKG", "MCHI", "EWJ", "EWU", "EWY", "EWT",
    "IBIT", "FBTC", "GLD", "IAU", "SLV", "USO",
    "SCHD", "SCHB", "SCHG", "SCHX", "DIA", "IWM", "EFA", "EEM",
    "SOXL", "SOXX", "SMH", "XLK", "XLF", "XLE", "XLV", "XLY",
}

# 美股個股 → 中文產業(使用者實際持有 + 常見)
US_INDUSTRY = {
    "MSFT": "軟體", "AAPL": "消費電子", "GOOGL": "網路服務",
    "GOOG": "網路服務", "AMZN": "電商雲端", "META": "網路服務",
    "NVDA": "半導體", "AMD": "半導體", "TSM": "半導體", "AVGO": "半導體",
    "INTC": "半導體", "QCOM": "半導體", "MU": "半導體", "ASML": "半導體",
    "TSLA": "汽車", "NFLX": "串流媒體", "DIS": "娛樂",
    "IBKR": "金融", "JPM": "金融", "BAC": "金融", "GS": "金融",
    "V": "金融", "MA": "金融", "PYPL": "金融", "BRK.B": "金融",
    "KO": "食品飲料", "PEP": "食品飲料", "PG": "民生消費",
    "JNJ": "生技醫療", "PFE": "生技醫療", "LLY": "生技醫療",
    "XOM": "油氣能源", "CVX": "油氣能源",
    "BABA": "電商", "NIO": "汽車",
}

# 台股代號 → 中文產業(少量常見;台股 ETF 由 00 規則處理)
TW_INDUSTRY = {
    "2330": "半導體", "2454": "半導體", "2308": "電子零組件",
    "2317": "電腦及週邊", "2382": "電腦及週邊", "2412": "通信網路",
    "2881": "金融保險", "2882": "金融保險", "2891": "金融保險",
    "2886": "金融保險", "2884": "金融保險",
    "1301": "塑膠", "1303": "塑膠", "2002": "鋼鐵", "2603": "航運",
    "2609": "航運", "2615": "航運", "1216": "食品",
}

# 名稱關鍵字 → 視為 ETF(白名單之外的保險絲)
_ETF_NAME_HINTS = ("ETF", "FUND", " TRUST", "ISHARES", "PROSHARES",
                   "VANGUARD", "INVESCO", "SPDR", "INDEX", "正2", "反1")
# 名稱關鍵字 → 視為債券(以面額計值的工具)
_BOND_NAME_HINTS = ("TREASURY", "T BILL", "T-BILL", "TBILL", "U S T BILL",
                    "TREAS", "GOVT BOND", "UST NOTE", "BOND DUE")

_CACHE_PATH = Path(__file__).resolve().parent.parent / "classify_cache.json"


# ─────────────────────────────────────────────────────────────────────
# 工具
# ─────────────────────────────────────────────────────────────────────
def _is_tw(symbol: str, ccy: str | None) -> bool:
    if ccy and ccy.upper() == "TWD":
        return True
    if ccy and ccy.upper() == "USD":
        return False
    # 無幣別時以代號形狀判斷:純數字(可帶 L/R/B 等台股後綴)→ 台股
    return bool(re.fullmatch(r"\d{4,6}[A-Z]?", symbol.strip()))


def _is_cusip(symbol: str) -> bool:
    s = symbol.strip()
    return bool(re.fullmatch(r"[0-9A-Z]{9}", s)) and any(c.isdigit()
                                                         for c in s[:3])


# ─────────────────────────────────────────────────────────────────────
# 對外:資產類別
# ─────────────────────────────────────────────────────────────────────
def asset_class(symbol: str, name: str = "", ccy: str | None = None) -> str:
    sym = (symbol or "").strip().upper()
    nm = (name or "").upper()

    # 1) CUSIP(直接持有的美國公債)→ 債券。ETF 不會是 CUSIP。
    if _is_cusip(sym):
        return "bond"

    # 2) 台股:00 開頭一律 ETF(含 0050 / 00640L / 00675L ...)
    if _is_tw(sym, ccy):
        return "etf" if sym.startswith("00") else "equity"

    # 3) 美股 ETF:白名單 → 名稱啟發式(務必在「債券名稱」之前判,
    #    否則 SGOV「…TREASURY BOND ETF」這種債券型 ETF 會被誤判成 bond)
    if sym in US_ETFS or any(h in nm for h in _ETF_NAME_HINTS):
        return "etf"

    # 4) 直接持有的債券(名稱含公債字樣、且非 ETF)→ 債券
    if any(h in nm for h in _BOND_NAME_HINTS):
        return "bond"

    # 5) 其餘視為個股
    return "equity"


# ─────────────────────────────────────────────────────────────────────
# 對外:產業別
# ─────────────────────────────────────────────────────────────────────
def industry(symbol: str, name: str = "", ccy: str | None = None,
             ac: str | None = None, use_yfinance: bool = False) -> str:
    sym = (symbol or "").strip().upper()
    ac = ac or asset_class(sym, name, ccy)

    if ac == "etf":
        return "ETF"
    if ac == "bond":
        return "債券"
    if ac == "cash":
        return "現金"

    if _is_tw(sym, ccy):
        return TW_INDUSTRY.get(sym, "其他")

    # 美股個股
    if sym in US_INDUSTRY:
        return US_INDUSTRY[sym]
    if use_yfinance:
        s = _yf_sector(sym)
        if s:
            return s
    return "其他"


# ─────────────────────────────────────────────────────────────────────
# 一次取得 (asset_class, industry, 補完的名稱)
# ─────────────────────────────────────────────────────────────────────
def classify(symbol: str, name: str = "", ccy: str | None = None,
             use_yfinance: bool = False) -> dict:
    ac = asset_class(symbol, name, ccy)
    ind = industry(symbol, name, ccy, ac, use_yfinance)
    return {"asset_class": ac, "industry": ind,
            "name": (name or symbol or "").strip() or symbol}


# ─────────────────────────────────────────────────────────────────────
# 選用:yfinance sector(英→中),本地 JSON 快取;離線/失敗安全略過
# ─────────────────────────────────────────────────────────────────────
_SECTOR_ZH = {
    "Technology": "科技", "Financial Services": "金融", "Healthcare": "生技醫療",
    "Consumer Cyclical": "非必需消費", "Consumer Defensive": "民生消費",
    "Communication Services": "通訊服務", "Industrials": "工業",
    "Energy": "能源", "Utilities": "公用事業", "Real Estate": "不動產",
    "Basic Materials": "原物料",
}


def _load_cache() -> dict:
    try:
        return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(c: dict) -> None:
    try:
        _CACHE_PATH.write_text(json.dumps(c, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    except Exception:
        pass


def _yf_sector(symbol: str) -> str | None:
    cache = _load_cache()
    if symbol in cache:
        return cache[symbol] or None
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).info or {}
        sec = info.get("sector") or ""
        zh = _SECTOR_ZH.get(sec, sec) or None
    except Exception:
        zh = None
    cache[symbol] = zh or ""
    _save_cache(cache)
    return zh


if __name__ == "__main__":
    samples = [
        ("0050", "元大台灣50", "TWD"),
        ("00640L", "富邦日本正2", "TWD"),
        ("00675L", "富邦臺灣加權正2", "TWD"),
        ("QLD", "PROSHARES ULTRA QQQ", "USD"),
        ("SGOV", "ISHARES 0-3 MONTH TREASURY BOND ETF", "USD"),
        ("MSFT", "MICROSOFT CORP", "USD"),
        ("IBIT", "ISHARES BITCOIN TRUST ETF", "USD"),
        ("IBKR", "INTERACTIVE BROKERS GRO-CL A", "USD"),
        ("912797KB2", "US TREASURY BILL DUE 08/15/24", "USD"),
    ]
    for s, n, c in samples:
        r = classify(s, n, c)
        print(f"  {s:10s} {c:4s} -> {r['asset_class']:7s} / {r['industry']}")
