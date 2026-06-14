"""重建合併淨值曲線 — P4d(永豐 + 嘉信 + 盈透,含每日 USD/TWD 匯率)。

一鍵把三家券商對帳單匯入 portfolio.db,回補「美股帳戶起始日 → 今天」每一天的
USD/TWD 匯率,並以「順向逐日估值」重畫一條跨券商、已換算成 TWD 的每日淨值曲線。
今天則寫入一筆真實的「合併快照」(snapshots / snapshot_positions),讓 run.py /
儀表板看到的總資產同時包含台股與美股。

用法:
  python rebuild_history.py                       # 自動找 sinopac.csv / schwab.csv / *TRANSACTIONS*.csv
  python rebuild_history.py a.csv b.csv c.csv     # 明確指定
  python rebuild_history.py --split QLD:2025-11-19:2   # 離線補拆股
  python rebuild_history.py --no-prices --no-fx        # 純離線(美股那段以遞補處理)

為什麼這樣才準(設計重點):
  逐日估值用「當日股數 × 當日原始收盤價 × 當日匯率」。當日股數與當日原始價
  同處『當期基礎』,所以跨拆股也正確,毋須做價格還原。匯率逐日一筆(非交易日
  前向填補),美股市值的台幣表現因此能反映匯率變動 —— 這正是需求 3。
"""
import argparse
import glob
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


def discover() -> list[str]:
    found = []
    for pat in ("sinopac.csv", "schwab.csv", "*TRANSACTIONS*.csv", "ibkr*.csv"):
        found += glob.glob(pat)
    # 去重、保序
    seen, out = set(), []
    for f in found:
        if f not in seen:
            seen.add(f); out.append(f)
    return out


def parse_split_args(items):
    out = {}
    for it in items or []:
        sym, d, ratio = it.split(":")
        out.setdefault(sym.upper(), []).append((d, Decimal(str(float(ratio)))))
    for s in out:
        out[s].sort()
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="重建跨券商每日淨值曲線(含每日匯率)")
    ap.add_argument("files", nargs="*", help="對帳單 CSV(留空則自動尋找)")
    ap.add_argument("--db", default="portfolio.db")
    ap.add_argument("--split", action="append", default=[])
    ap.add_argument("--no-prices", action="store_true")
    ap.add_argument("--no-fx", action="store_true")
    ap.add_argument("--usd-rate", type=float, default=None,
                    help="手動指定 USD/TWD 匯率(離線用,整段每日填同一值)")
    ap.add_argument("--keep-real", action="store_true",
                    help="保留既有真實快照日不被重畫(預設:整段重畫以修正舊的 TW-only 點)")
    ap.add_argument("--base", default=BASE_CCY)
    args = ap.parse_args()

    files = args.files or discover()
    if not files:
        raise SystemExit("找不到對帳單 CSV。請放在當前目錄,或在參數明確指定路徑。")
    print(f"[rebuild] 對帳單:{', '.join(files)}")

    override = parse_split_args(args.split)
    db = DB(args.db)
    today = date.today().isoformat()
    statements = []           # [(stmt, split_map)]
    us_start = None

    # ---- 1) 逐檔:解析 → 拆股 → 現價 → 回放 → 寫入 positions/txn/cash ----
    for f in files:
        path = Path(f)
        if not path.exists():
            print(f"[skip] 找不到 {f}"); continue
        stmt = detect_and_parse(path)
        names = collect_names(stmt, path)

        split_map = dict(override) if stmt.broker != "schwab" else {}
        if stmt.broker != "schwab" and not split_map and not args.no_prices:
            try:
                from core import market
                for s in sorted({e.symbol for e in stmt.events if e.symbol}):
                    sp = market.splits(s, stmt.ccy, since=stmt.start_date)
                    if sp:
                        split_map[s] = sp
            except Exception as e:
                print(f"[split] {stmt.broker} 略過 yfinance:{e}")

        res = replay(stmt, split_map, names)

        prices = {}
        if not args.no_prices:
            try:
                from core import market
                prices = market.current_prices(sorted(res.holdings), stmt.ccy)
            except Exception as e:
                print(f"[price] {stmt.broker} 略過現價:{e}")

        positions = importer.build_positions(stmt, res, prices, today)
        txns = importer.build_transactions(stmt)
        importer.write_to_db(db, stmt, positions, txns, float(res.cash),
                             record_cash=stmt.cash_is_real)
        print(f"[import] {stmt.broker}: 持倉 {len(positions)} 檔、交易 {len(txns)} 筆"
              f"、起始 {stmt.start_date}")

        statements.append((stmt, split_map))
        if stmt.ccy != args.base and stmt.start_date:
            us_start = stmt.start_date if us_start is None \
                else min(us_start, stmt.start_date)

    # ---- 2) 回補美股每日匯率(起始日 → 今天)----
    fx = FXBackfiller(db, args.base)
    if us_start:
        if args.usd_rate is not None:
            fx.backfill("USD", us_start, today,
                        fetch=lambda pair, s, e: [(s, args.usd_rate)])
            print(f"[fx] 使用手動匯率 USD/TWD={args.usd_rate}(整段每日)")
        elif not args.no_fx:
            try:
                fx.backfill("USD", us_start, today)
            except Exception as e:
                print(f"[fx] 匯率回補略過(無網路?):{e}")
        else:
            print(f"[fx] (--no-fx) 跳過匯率回補;美股換算將用既有 fx_cache")

    # ---- 3) 今天:寫一筆真實「合併快照」(含台股+美股,已換 TWD)----
    ts = aggregate.write_combined_snapshot(db, fx, args.base, today)
    snap = aggregate.combined_snapshot(db, fx, args.base, today)
    print(f"[snapshot] 合併快照 {ts}  總淨值 "
          f"{snap['net_worth']:,.0f} {args.base}  "
          f"(投資 {snap['invested']:,.0f} / 現金 {snap['cash']:,.0f})")
    for b, v in snap["breakdown"]["by_broker"].items():
        print(f"            {aggregate_broker_name(b)}: {v:,.0f} {args.base}")

    # ---- 4) 順向逐日估值 → daily_networth(整段重畫;今天保留真實合併快照)----
    price_on = _make_price_on(db, statements, us_start, today, args.no_prices)
    n = 0
    if statements:
        curve = importer.daily_history(
            statements, price_on, fx.rate_on, args.base, end=today)
        # 重建 = 整段重畫:用 FX 換算後的估值覆蓋過去每一天(含先前 TW-only 的
        # 舊真實點),只保留「今天」那筆真實合併快照不蓋。
        keep_real = set()
        if args.keep_real:
            keep_real = {d for (d,) in db.con.execute(
                "SELECT date FROM daily_networth WHERE is_real=1")}
        cur = db.con.cursor()
        for d, v in curve:
            if d == today or d in keep_real:
                continue
            cur.execute("INSERT OR REPLACE INTO daily_networth VALUES (?,?,?,0)",
                        (d, args.base, v))
            n += 1
        db.con.commit()
        if curve:
            print(f"[curve] 已重畫 {n} 天每日淨值"
                  f"({curve[0][0]} ~ {curve[-2][0] if len(curve)>1 else curve[0][0]});"
                  f"今天為真實合併快照"
                  + ("(--keep-real:保留既有真實點)" if args.keep_real else ""))

    db.close()
    print("\n完成。開啟儀表板:streamlit run viewer/app.py")


def aggregate_broker_name(b: str) -> str:
    return {"sinopac": "永豐金", "schwab": "嘉信", "ibkr": "盈透"}.get(b, b)


def _make_price_on(db, statements, us_start, today, no_prices):
    """回傳 price_on(symbol, ccy, date) → 當日原始收盤(快取於 price_cache RAW:)。"""
    from core.market import RawPriceCache
    cache = RawPriceCache(db)
    if not no_prices:
        # 先把每檔標的的原始歷史價抓滿(各券商各自起始日 → 今天)
        for stmt, _ in statements:
            syms = sorted({e.symbol for e in stmt.events if e.symbol})
            for s in syms:
                try:
                    cache.ensure_range(s, stmt.ccy, stmt.start_date, today)
                except Exception:
                    pass
    return cache.price_on


if __name__ == "__main__":
    main()
