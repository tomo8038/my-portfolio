"""對帳單匯入 CLI — P4d(支援 永豐 / 嘉信 / 盈透)。

把券商對帳單(CSV)解析、回放成「目前持倉 + 現金」,寫進 portfolio.db 的
positions_current / transactions / broker_cash,並回補該幣別每日匯率。

用法:
  python import_statements.py sinopac.csv
  python import_statements.py schwab.csv
  python import_statements.py U5529822_TRANSACTIONS.csv
  python import_statements.py xxx.csv --broker ibkr          # 強制指定券商
  python import_statements.py xxx.csv --split QLD:2025-11-19:2  # 手動補拆股(離線/無 yfinance 時)
  python import_statements.py xxx.csv --no-prices --no-fx     # 純離線:不抓現價、不回補匯率

流程:辨識 → (yfinance 抓拆股 + 現價) → 回放 → 寫入 → 回補匯率 → 印摘要
之後執行 `python rebuild_history.py` 重建合併淨值曲線,或 `python run.py` 同步永豐
即會把美股一起換算進總資產。

說明:
  * 嘉信對帳單「本身」含 Stock Split 列(已是新增股數),不需 yfinance 補拆股。
  * 盈透對帳單「沒有」拆股列 → 預設用 yfinance 補;離線時請用 --split 指定。
  * 對帳單推得的現金:永豐無出入金資料 → 不採計;嘉信/盈透有 → 採計。
"""
import argparse
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from core.db import DB
from core.statements import detect_and_parse, replay, collect_names
from core import importer, aggregate
from core.fxrate import FXBackfiller

BASE_CCY = "TWD"


def parse_split_args(items: list[str]) -> dict:
    """--split QLD:2025-11-19:2 → {'QLD': [('2025-11-19', Decimal('2'))]}"""
    out: dict[str, list] = {}
    for it in items or []:
        try:
            sym, d, ratio = it.split(":")
            out.setdefault(sym.upper(), []).append((d, Decimal(str(float(ratio)))))
        except ValueError:
            raise SystemExit(f"--split 格式錯誤:{it}(應為 代號:YYYY-MM-DD:倍數)")
    for sym in out:
        out[sym].sort()
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="匯入券商對帳單到 portfolio.db")
    ap.add_argument("file", help="對帳單 CSV 路徑")
    ap.add_argument("--broker", choices=["auto", "sinopac", "schwab", "ibkr"],
                    default="auto", help="預設自動辨識")
    ap.add_argument("--db", default="portfolio.db", help="SQLite 路徑")
    ap.add_argument("--split", action="append", default=[],
                    help="手動補拆股 代號:YYYY-MM-DD:倍數(可重複)")
    ap.add_argument("--no-prices", action="store_true", help="不抓現價(離線)")
    ap.add_argument("--no-fx", action="store_true", help="不回補匯率(離線)")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        raise SystemExit(f"找不到檔案:{path}")

    # 1) 解析
    stmt = detect_and_parse(path) if args.broker == "auto" else \
        detect_and_parse(path)            # detect 已足夠;保留 --broker 供未來強制
    if args.broker not in ("auto", stmt.broker):
        stmt.broker = args.broker
    names = collect_names(stmt, path)
    print(f"[parse] {stmt.broker}  幣別={stmt.ccy}  事件數={len(stmt.events)}  "
          f"起始日={stmt.start_date}")

    # 2) 拆股:嘉信已在 CSV(SPLIT_ADD),其餘券商用 yfinance 補,或 --split 覆寫
    split_map = parse_split_args(args.split)
    if stmt.broker == "schwab":
        split_map = {}        # 嘉信拆股已在對帳單,絕不可再套 split_map(會重複計算)
    elif not split_map and not args.no_prices:
        try:
            from core import market
            syms = sorted({e.symbol for e in stmt.events if e.symbol})
            for s in syms:
                sp = market.splits(s, stmt.ccy, since=stmt.start_date)
                if sp:
                    split_map[s] = sp
                    print(f"[split] {s}:{[(d, str(r)) for d, r in sp]}")
        except Exception as e:
            print(f"[split] 略過 yfinance 拆股查詢:{e}")

    # 3) 現價
    prices = {}
    if not args.no_prices:
        try:
            from core import market
            syms = sorted(replay(stmt, split_map, names).holdings.keys())
            prices = market.current_prices(syms, stmt.ccy)
            if prices:
                print(f"[price] 取得 {len(prices)} 檔現價")
        except Exception as e:
            print(f"[price] 略過現價查詢(用均價遞補):{e}")

    # 4) 回放 → 持倉 / 交易
    res = replay(stmt, split_map, names)
    asof = date.today().isoformat()
    positions = importer.build_positions(stmt, res, prices, asof)
    txns = importer.build_transactions(stmt)

    # 5) 寫入 DB(現金是否採計依對帳單性質)
    db = DB(args.db)
    info = importer.write_to_db(db, stmt, positions, txns,
                               float(res.cash), record_cash=stmt.cash_is_real)
    print(f"[db] 寫入持倉 {info['positions']} 檔、新增交易 {info['txns_new']} 筆"
          + (f"、現金 {info['cash']:,.2f} {stmt.ccy}"
             if info["cash"] is not None else "、(對帳單無出入金,現金不採計)"))

    # 6) 回補該幣別每日匯率(美股帳戶起始日 → 今天)
    if not args.no_fx and stmt.ccy != BASE_CCY and stmt.start_date:
        fx = FXBackfiller(db, BASE_CCY)
        try:
            fx.backfill(stmt.ccy, stmt.start_date, asof)
        except Exception as e:
            print(f"[fx] 匯率回補略過(無網路?):{e}")

    # 7) 摘要
    _summary(stmt, res, positions, prices)
    db.close()
    print("\n下一步:python rebuild_history.py   # 重建含美股的每日淨值曲線\n"
          "        (或 python run.py 同步永豐,會自動把美股一起換算進總資產)")


def _summary(stmt, res, positions, prices) -> None:
    w = 72
    print("\n" + "=" * w)
    print(f"  {stmt.broker.upper()}  目前持倉(回放結果)")
    print("-" * w)
    print(f"  {'代號':<8}{'股數':>14}{'均價':>12}{'現價':>12}{'市值(原幣)':>16}")
    tot = 0.0
    for p in sorted(positions, key=lambda x: -float(x.market_value)):
        mv = float(p.market_value)
        tot += mv
        print(f"  {p.symbol:<8}{float(p.qty):>14,.4f}{float(p.avg_cost):>12,.2f}"
              f"{float(p.last_price):>12,.2f}{mv:>16,.2f}")
    print("-" * w)
    print(f"  市值合計:{tot:,.2f} {stmt.ccy}"
          + (f"  +  現金 {float(res.cash):,.2f} {stmt.ccy}"
             if stmt.cash_is_real else "  (現金未採計)"))
    print("=" * w)


if __name__ == "__main__":
    main()
