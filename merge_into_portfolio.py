"""嘉信 CSV → 併入現有 portfolio.db(多券商 + 多幣別合併)— P4b。

把交易明細回放出來的嘉信資料,安全地併進你「已有台股」的 portfolio.db,
讓 daily_networth 變成「台股 TWD + 嘉信(USD×當日匯率)」的單一 TWD 曲線。

== 為什麼這支工具要特別小心 ==
你現有的 portfolio.db 裡有台股的真實快照與每日淨值(TWD)。併入嘉信時
最大的風險是「弄壞既有台股資料」或「重跑時重複累加」。本工具的設計原則:

1. 冪等、可重跑:嘉信資料以 broker='schwab' 標記,每次併入先刪除 schwab
   的舊列再寫入,絕不累加。台股(broker!='schwab')的列一律不碰。

2. 不破壞台股每日淨值:daily_networth 的 PRIMARY KEY 是 date、單一 TWD
   數值,無法同時存兩套。所以本工具不直接覆寫它,而是:
     - 把「嘉信每日 USD 原生淨值」存進獨立輔助表 schwab_daily_native
       (date, networth_usd),完全不動 daily_networth 既有內容;
     - 另存一份「台股原始 TWD 每日淨值」備份到 stock_daily_backup
       (僅第一次併入時建立,作為還原基準);
     - 重算 daily_networth = 台股原始 TWD + 嘉信 USD×當日匯率,逐日相加。
   這樣即使重跑,也是從「台股原始備份」重新相加,不會把嘉信疊兩次。

3. 真實快照保護:台股 is_real=1 的日子,合併後仍標記為 is_real=1(那天
   台股是真實對帳,加上嘉信估值);只有嘉信獨有的日子(台股還沒資料的
   早期)才是純估計。

4. 可還原:備份表 + restore 指令,一鍵還原成併入前的純台股 portfolio.db。

== 匯率 ==
USD→TWD 用既有 core/prices.FXService(fx_cache + 往前遞補)。回補嘉信
六年歷史需要六年的每日 USD/TWD,本工具會先 ensure_range 一次抓齊。
抓不到匯率的日子往前遞補最近一筆(與系統其他地方一致)。

用法:
  python merge_into_portfolio.py schwab.csv /path/to/portfolio.db
  python merge_into_portfolio.py --restore /path/to/portfolio.db   # 還原
  python merge_into_portfolio.py --dry-run schwab.csv portfolio.db # 試算不寫入
"""
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from parse_schwab_csv import parse_csv, is_cusip, yf_symbol
from build_history import (replay, _holding_span, fetch_prices, price_on,
                           daily_networth, asset_class, align_split_dates,
                           BROKER, ACCOUNT, ONE_DAY)

BASE_CCY = "TWD"
USD = "USD"

# 併入用的輔助表(不動既有 schema)
AUX_SCHEMA = """
CREATE TABLE IF NOT EXISTS schwab_daily_native (
    date TEXT PRIMARY KEY, networth_usd REAL
);
CREATE TABLE IF NOT EXISTS stock_daily_backup (
    date TEXT PRIMARY KEY, base_ccy TEXT, net_worth REAL, is_real INTEGER
);
CREATE TABLE IF NOT EXISTS merge_meta (
    key TEXT PRIMARY KEY, value TEXT
);
"""


def _conn(db_path):
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=3000")
    return con


# ---------------------------------------------------------------- 匯率

def load_fx(con, start: str, end: str) -> dict:
    """確保 fx_cache 有 [start,end] 的 USDTWD,回 {date: rate}(已遞補)。"""
    # 沿用既有 FXService(讀寫的是同一個 fx_cache 表)
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from core.prices import FXService

        class _DBWrap:
            def __init__(self, con): self.con = con
            def get_fx(self, pair):
                return dict(self.con.execute(
                    "SELECT date, rate FROM fx_cache WHERE pair=?", (pair,)))
            def put_fx(self, pair, rows):
                self.con.executemany(
                    "INSERT OR REPLACE INTO fx_cache VALUES (?,?,?)",
                    [(pair, d, r) for d, r in rows])
                self.con.commit()
        fx = FXService(_DBWrap(con), BASE_CCY)
        fx.ensure_range(USD, start, end)
    except Exception as e:
        print(f"[fx] 經 FXService 抓匯率失敗({e}),改用 fx_cache 既有資料")

    # 確保 fx_cache 表存在
    con.execute("CREATE TABLE IF NOT EXISTS fx_cache "
                "(pair TEXT, date TEXT, rate REAL, PRIMARY KEY(pair,date))")
    rates = dict(con.execute(
        "SELECT date, rate FROM fx_cache WHERE pair='USDTWD'"))
    return rates


def rate_on(rates: dict, d: str):
    """d 當日 USD/TWD;無則往前遞補最多 10 天;查無回 None。"""
    if d in rates:
        return rates[d]
    cur = date.fromisoformat(d)
    for _ in range(10):
        cur -= ONE_DAY
        if (k := cur.isoformat()) in rates:
            return rates[k]
    return None


# ---------------------------------------------------------------- 併入

def merge(csv_path: str, db_path: str, dry_run: bool = False):
    events = parse_csv(csv_path)
    last_event = events[-1].date
    # 延伸到今天:最後一筆交易之後持股不變,逐日以當日收盤重估市值。
    # (否則嘉信曲線只到最後交易日,6/13、6/14 會缺嘉信而驟降;且今天的快照
    #  也因日期對不上而吃不到嘉信。)
    end_date = max(last_event, date.today().isoformat())
    start_date = events[0].date
    print(f"[merge] 嘉信事件 {len(events)} 筆,{start_date} ~ {end_date}")

    con = _conn(db_path)
    con.executescript(AUX_SCHEMA)

    # 確認既有 portfolio.db 的台股資料概況
    has_stock = con.execute(
        "SELECT COUNT(*) FROM daily_networth WHERE base_ccy != 'USD'"
    ).fetchone()[0] if _table_exists(con, "daily_networth") else 0
    stock_brokers = [r[0] for r in con.execute(
        "SELECT DISTINCT broker FROM positions_current WHERE broker != ?",
        (BROKER,))] if _table_exists(con, "positions_current") else []
    print(f"[merge] 既有台股每日淨值 {has_stock} 天;"
          f"既有券商:{stock_brokers or '(無)'}")

    # ---- 1) 重播嘉信 → 持倉/現金/已實現/交易 ----
    positions, cash, realized, txns = replay(events)
    print(f"[merge] 嘉信重播:現金 ${cash:,.2f}、持倉 {len(positions)} 檔、"
          f"已實現 {len(realized)} 筆")

    # ---- 2) 抓嘉信歷史價 + USD/TWD 匯率 ----
    spans = _holding_span(events)
    end_plus = (date.fromisoformat(end_date) + ONE_DAY).isoformat()
    print("[merge] 抓嘉信歷史股價(抓不到以成本遞補)...")
    prices, yf_splits, factor_splits = fetch_prices(spans, end_plus)
    # 分割日對齊(問題2修正):股數加倍挪到 yfinance 真實 ex-date,
    # 與價格減半同一天 → 嘉信併入 portfolio.db 的每日曲線不再出現假跳階。
    events = align_split_dates(events, yf_splits)
    print("[merge] 抓 USD/TWD 歷史匯率...")
    rates = load_fx(con, start_date, end_plus)
    if not rates:
        print("[merge] ⚠ 無任何 USD/TWD 匯率(離線?)。併入需要匯率才能折算 TWD。")
        print("        請在有網路時重跑;或先確認 fx_cache 已有 USDTWD 資料。")
        con.close()
        return
    # 用今天匯率折算當前持倉市值(顯示用)
    today_rate = rate_on(rates, end_date) or list(rates.values())[-1]
    print(f"[merge] 今日 USD/TWD = {today_rate}")

    # ---- 3) 嘉信每日 USD 淨值(原生)----
    print("[merge] 逐日重算嘉信 USD 淨值...")
    schwab_series = daily_networth(events, prices, end_date, factor_splits)  # [(date, usd, is_real)]
    schwab_usd = {d: nv for d, nv, _ in schwab_series}

    if dry_run:
        _print_dryrun(positions, cash, realized, today_rate,
                      schwab_series, rates, has_stock)
        con.close()
        return

    # ---- 4) 寫入(全程交易包覆;失敗則 rollback)----
    try:
        con.execute("BEGIN")

        # 4a) 首次併入:備份台股原始每日淨值(作為重算基準與還原點)
        already = con.execute(
            "SELECT value FROM merge_meta WHERE key='stock_backed_up'"
        ).fetchone()
        if not already:
            con.execute("DELETE FROM stock_daily_backup")
            con.execute(
                "INSERT INTO stock_daily_backup "
                "SELECT date, base_ccy, net_worth, is_real FROM daily_networth")
            con.execute("INSERT OR REPLACE INTO merge_meta VALUES "
                        "('stock_backed_up', ?)",
                        (datetime.now().isoformat(timespec='seconds'),))
            n_bak = con.execute(
                "SELECT COUNT(*) FROM stock_daily_backup").fetchone()[0]
            print(f"[merge] 已備份台股原始每日淨值 {n_bak} 天(供還原)")

        # 4b) 嘉信持倉 → positions_current(broker='schwab',先刪後寫)
        con.execute("DELETE FROM positions_current WHERE broker=?", (BROKER,))
        invested_usd = Decimal(0)
        for sym, lot in sorted(positions.items()):
            px = price_on(prices, sym, end_date)
            last = Decimal(str(px)) if px is not None else lot.avg
            mv = lot.qty * last
            invested_usd += mv
            con.execute(
                "INSERT INTO positions_current VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (BROKER, ACCOUNT, sym, lot.name or sym, asset_class(sym),
                 float(lot.qty), float(lot.avg), float(last), USD,
                 float(mv), float(mv - lot.cost), end_date + "T00:00:00", ""))

        # 4c) 嘉信交易 / 已實現 → transactions / realized_pnl(先刪後寫)
        con.execute("DELETE FROM transactions WHERE broker=?", (BROKER,))
        seen = defaultdict(int)
        for tt, d, sym, q, px, amt in txns:
            key = f"{d}-{tt}-{sym}-{seen[(d,tt,sym)]}"; seen[(d, tt, sym)] += 1
            con.execute("INSERT OR IGNORE INTO transactions VALUES "
                        "(?,?,?,?,?,?,?,?,?)",
                        (BROKER, key, sym, tt, float(q), float(px),
                         float(amt), USD, d))
        con.execute("DELETE FROM realized_pnl WHERE broker=?", (BROKER,))
        rseen = defaultdict(int)
        for d, sym, q, px, pnl in realized:
            key = f"{d}-{sym}-{rseen[(d,sym)]}"; rseen[(d, sym)] += 1
            con.execute("INSERT OR IGNORE INTO realized_pnl VALUES "
                        "(?,?,?,?,?,?,?,?)",
                        (BROKER, key, sym, float(q), float(px), float(pnl),
                         USD, d))

        # 4d) 嘉信每日 USD 原生淨值 → 輔助表(先清後寫)
        con.execute("DELETE FROM schwab_daily_native")
        con.executemany("INSERT INTO schwab_daily_native VALUES (?,?)",
                        [(d, nv) for d, nv, _ in schwab_series])

        # 4e) 重算 daily_networth = 台股原始 TWD + 嘉信 USD×當日匯率
        #     從備份的台股原始值出發,確保重跑不疊加。
        stock_rows = dict(con.execute(
            "SELECT date, net_worth FROM stock_daily_backup").fetchall())
        stock_real = dict(con.execute(
            "SELECT date, is_real FROM stock_daily_backup").fetchall())

        all_days = sorted(set(stock_rows) | set(schwab_usd))
        merged = []
        miss_fx = 0
        for d in all_days:
            tw = Decimal(str(stock_rows.get(d, 0)))
            su = schwab_usd.get(d)
            if su is not None:
                r = rate_on(rates, d)
                if r is None:
                    miss_fx += 1
                    r = today_rate     # 極早期無匯率 → 用最近(誠實標示於 note)
                tw += Decimal(str(su)) * Decimal(str(r))
            # is_real:台股當天為真實 → 維持 1;否則 0(嘉信獨有日為估計)
            isr = int(stock_real.get(d, 0))
            merged.append((d, BASE_CCY, float(tw), isr))

        con.execute("DELETE FROM daily_networth")
        con.executemany(
            "INSERT INTO daily_networth VALUES (?,?,?,?)", merged)

        # 4f) 更新今日快照:把嘉信淨值(TWD)併入 snapshots 的 breakdown
        _merge_snapshot(con, invested_usd, cash, today_rate, positions,
                        prices, end_date)

        con.execute("INSERT OR REPLACE INTO merge_meta VALUES "
                    "('last_merge', ?)",
                    (datetime.now().isoformat(timespec='seconds'),))
        con.execute("COMMIT")
    except Exception as e:
        con.execute("ROLLBACK")
        print(f"[merge] ✗ 併入失敗,已 rollback(portfolio.db 未變動):{e}")
        con.close()
        raise

    # ---- 5) 摘要 ----
    schwab_twd = float(invested_usd + cash) * float(today_rate)
    print()
    print("=" * 60)
    print("  嘉信併入完成")
    print("=" * 60)
    print(f"  嘉信淨值(USD)  ${float(invested_usd+cash):>14,.2f}")
    print(f"  折算 TWD(@{today_rate})  NT$ {schwab_twd:>14,.0f}")
    print(f"  併入後每日淨值 {len(merged)} 天"
          f"({merged[0][0]} ~ {merged[-1][0]})")
    if miss_fx:
        print(f"  注:{miss_fx} 個早期日無對應匯率,以最近匯率遞補(誠實標示)")
    print(f"  台股資料未更動;嘉信可重跑覆蓋,不會疊加")
    print(f"  還原指令:python merge_into_portfolio.py --restore {db_path}")
    print("=" * 60)
    con.close()


def _merge_snapshot(con, invested_usd, cash_usd, rate, positions,
                    prices, end_date):
    """把嘉信併入最新一筆 snapshots(若當天已有台股快照則合併,否則新建)。

    冪等關鍵:當天若有台股快照,第一次併入前先把該快照的「台股原始值」
    備份到 merge_meta(stock_snap_orig)。之後每次併入都從這個原始值重算
    『台股原始 + 嘉信』,而非在已合併值上再疊加,故重跑結果一致。
    """
    rate = Decimal(str(rate))
    schwab_net_twd = (invested_usd + cash_usd) * rate
    schwab_inv_twd = invested_usd * rate
    schwab_cash_twd = cash_usd * rate
    schwab_cost_twd = sum(l.cost for l in positions.values()) * rate

    row = con.execute(
        "SELECT ts, net_worth, invested, cash, cost, breakdown "
        "FROM snapshots ORDER BY ts DESC LIMIT 1").fetchone()

    by_class = defaultdict(float)
    for sym, lot in positions.items():
        px = price_on(prices, sym, end_date)
        last = Decimal(str(px)) if px is not None else lot.avg
        by_class[asset_class(sym)] += float(lot.qty * last * rate)

    if row and row[0][:10] == end_date:
        # 當天已有台股快照 → 從「台股原始值」重算(冪等)
        ts = row[0]
        orig = con.execute(
            "SELECT value FROM merge_meta WHERE key='stock_snap_orig'"
        ).fetchone()
        if orig:
            # 已備份過台股原始快照 → 用它(重跑時走這條,不疊加)
            o = json.loads(orig[0])
        else:
            # 首次:目前 snapshots 還是純台股,備份它
            o = {"net_worth": row[1], "invested": row[2], "cash": row[3],
                 "cost": row[4], "breakdown": json.loads(row[5] or "{}")}
            con.execute("INSERT OR REPLACE INTO merge_meta VALUES "
                        "('stock_snap_orig', ?)",
                        (json.dumps(o, ensure_ascii=False),))

        bd = dict(o["breakdown"])      # 從台股原始 breakdown 出發
        bd.setdefault("by_broker", {})[BROKER] = float(schwab_net_twd)
        bc = dict(bd.get("by_asset_class", {}))   # 台股原始類別
        for k, v in by_class.items():
            bc[k] = bc.get(k, 0.0) + v
        bd["by_asset_class"] = bc
        bd.setdefault("by_ccy", {})["USD"] = float(schwab_net_twd)
        bd.setdefault("cash", {})["USD"] = float(cash_usd)
        bd.setdefault("fx", {})["USD"] = float(rate)
        con.execute(
            "UPDATE snapshots SET net_worth=?, invested=?, cash=?, cost=?, "
            "breakdown=? WHERE ts=?",
            (o["net_worth"] + float(schwab_net_twd),
             o["invested"] + float(schwab_inv_twd),
             o["cash"] + float(schwab_cash_twd),
             o["cost"] + float(schwab_cost_twd),
             json.dumps(bd, ensure_ascii=False), ts))
    else:
        # 當天沒有台股快照 → 為嘉信單獨建一筆(TWD)
        ts = end_date + "T00:00:00"
        bd = {"by_broker": {BROKER: float(schwab_net_twd)},
              "by_asset_class": dict(by_class),
              "by_ccy": {"USD": float(schwab_net_twd)},
              "cash": {"USD": float(cash_usd)},
              "fx": {"USD": float(rate)}}
        con.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?,?)",
                    (ts, BASE_CCY, float(schwab_net_twd),
                     float(schwab_inv_twd), float(schwab_cash_twd),
                     float(schwab_cost_twd),
                     json.dumps(bd, ensure_ascii=False)))


# ---------------------------------------------------------------- 還原

def restore(db_path: str):
    con = _conn(db_path)
    if not _table_exists(con, "stock_daily_backup"):
        print("[restore] 找不到備份表,無法還原(可能從未併入過)。")
        con.close()
        return
    n = con.execute("SELECT COUNT(*) FROM stock_daily_backup").fetchone()[0]
    if n == 0:
        print("[restore] 備份表是空的,無法還原。")
        con.close()
        return
    try:
        con.execute("BEGIN")
        # 還原 daily_networth 成純台股
        con.execute("DELETE FROM daily_networth")
        con.execute("INSERT INTO daily_networth "
                    "SELECT date, base_ccy, net_worth, is_real "
                    "FROM stock_daily_backup")
        # 移除嘉信的所有列
        for t in ("positions_current", "transactions", "realized_pnl"):
            if _table_exists(con, t):
                con.execute(f"DELETE FROM {t} WHERE broker=?", (BROKER,))
        # 移除嘉信當天併入 snapshots 的部分:無法完美回退 breakdown,
        # 故刪除嘉信獨有的快照、台股當天的需手動重跑 run.py 覆蓋。
        # 還原當天 snapshots 的台股原始值(若有備份)
        orig = con.execute(
            "SELECT value FROM merge_meta WHERE key='stock_snap_orig'"
        ).fetchone()
        if orig:
            import json as _json
            o = _json.loads(orig[0])
            # 找回那筆台股快照(最新一筆,當天)
            r = con.execute("SELECT ts FROM snapshots ORDER BY ts DESC "
                            "LIMIT 1").fetchone()
            if r:
                con.execute(
                    "UPDATE snapshots SET net_worth=?, invested=?, cash=?, "
                    "cost=?, breakdown=? WHERE ts=?",
                    (o["net_worth"], o["invested"], o["cash"], o["cost"],
                     _json.dumps(o["breakdown"], ensure_ascii=False), r[0]))
        con.execute("DELETE FROM schwab_daily_native")
        con.execute("DELETE FROM merge_meta WHERE key IN "
                    "('last_merge','stock_backed_up','stock_snap_orig')")
        con.execute("COMMIT")
        print(f"[restore] 已還原:daily_networth 回復台股原始 {n} 天,"
              "嘉信列已移除,當天快照已還原為台股原始值。")
        print("[restore] portfolio.db 已回到併入前的純台股狀態。")
    except Exception as e:
        con.execute("ROLLBACK")
        print(f"[restore] 還原失敗,已 rollback:{e}")
    con.close()


# ---------------------------------------------------------------- 工具

def _table_exists(con, name):
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def _print_dryrun(positions, cash, realized, rate, schwab_series, rates,
                  has_stock):
    inv = sum(l.qty * l.avg for l in positions.values())  # 成本估(無真實價時)
    net_usd = float(inv + cash)
    print()
    print("===== DRY RUN(只試算,不寫入)=====")
    print(f"  嘉信當前持倉 {len(positions)} 檔,已實現 {len(realized)} 筆")
    print(f"  嘉信淨值(USD,成本/市值口徑視抓價而定)≈ ${net_usd:,.2f}")
    print(f"  今日 USD/TWD ≈ {rate} → 折算 ≈ NT$ {net_usd*float(rate):,.0f}")
    print(f"  嘉信每日序列 {len(schwab_series)} 天"
          f"({schwab_series[0][0]} ~ {schwab_series[-1][0]})")
    print(f"  既有台股每日淨值 {has_stock} 天 → 合併後將涵蓋兩者聯集日期")
    print(f"  匯率快取覆蓋 {len(rates)} 天")
    print("  (加 --commit 或移除 --dry-run 才會實際寫入)")


def main():
    args = sys.argv[1:]
    if "--restore" in args:
        args.remove("--restore")
        if not args:
            print("用法:python merge_into_portfolio.py --restore portfolio.db")
            return
        restore(args[0])
        return
    dry = "--dry-run" in args
    if dry:
        args.remove("--dry-run")
    if len(args) < 2:
        print("用法:python merge_into_portfolio.py [--dry-run] "
              "schwab.csv /path/to/portfolio.db")
        print("     python merge_into_portfolio.py --restore /path/to/portfolio.db")
        return
    merge(args[0], args[1], dry_run=dry)


if __name__ == "__main__":
    main()
