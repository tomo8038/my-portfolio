"""
merge_ibkr_into_portfolio.py — 把 IBKR(ibkr.db)安全併入 portfolio.db
(對映嘉信版 merge_into_portfolio.py;同一套三道安全保證)

每日淨值合併口徑:portfolio.db 的 daily_networth 變成
    台股 TWD + 嘉信 USD×當日匯率 + 盈透 IBKR USD×當日匯率   →  單一 TWD 曲線

三道安全保證:
  1) 不破壞既有資料:併入前的(非 IBKR)每日淨值先備份到 ibkr_base_backup;
     合併值一律從該基準重算,broker≠ibkr 的列完全不碰。
  2) 冪等可重跑:IBKR 以 broker='ibkr' 標記,每次先刪後寫;
     每日淨值從基準重算,重跑不疊加。
  3) 可還原:--restore 一鍵移除所有 IBKR 列、把 daily_networth 還原成併入前狀態。

⚠ 併入順序:請先跑嘉信 merge_into_portfolio.py、再跑本工具(IBKR 最後)。
   這樣 ibkr_base_backup 擷取到的基準才是「台股+嘉信」,合併總額才正確。

用法:
  python merge_ibkr_into_portfolio.py --dry-run ibkr.db portfolio.db   # 只試算不寫入
  python merge_ibkr_into_portfolio.py          ibkr.db portfolio.db   # 正式併入
  python merge_ibkr_into_portfolio.py --restore           portfolio.db # 還原(移除 IBKR)

【本版修正(對映你回報的 merge 崩潰)】
  * net_worth 欄名:portfolio.db 的 daily_networth 欄位是 net_worth(底線),
    舊版誤用 networth → 已修正 _DN_VALUE。
  * 備份表改名 ibkr_base_backup:舊版叫 stock_daily_backup,與嘉信版「同名但
    欄位不同」而衝突("no such column: networth")→ 改用獨立表,徹底分開。
  * positions_current / realized_pnl 改「依目標 schema 自動對應欄位」寫入:
    舊版直接套 ibkr.db 的欄位(含 portfolio.db 沒有的 cost_basis)→ 會炸。
    現在只寫 portfolio.db 實際存在的欄位,並補上 last_price/市值/未實現損益,
    讓儀表板的 IBKR 持倉直接有現價與市值。
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import date, datetime, timedelta

BROKER = "ibkr"
ACCOUNT_ID = "U5529822"          # IBKR 帳號(positions_current 主鍵需要)

# 若你的 portfolio.db 欄位命名不同,改這裡即可 ───────────────────────────
_DN_TABLE = "daily_networth"     # 主每日淨值表(TWD)
_DN_DATE = "date"
_DN_VALUE = "net_worth"          # ★ 修正:portfolio.db 是 net_worth(底線)
_BK_TABLE = "ibkr_base_backup"   # ★ IBKR 專用基準備份(與嘉信版分開,避免衝突)
_FX_TABLE = "fx_cache"           # 匯率快取(USD->TWD)
_FX_DATE = "date"
_FX_RATE = "rate"
_FX_PAIR_COL = "pair"            # 若無此欄設為 None
_FX_PAIR_VAL = "USDTWD"


def _cols(cur, table) -> list[str]:
    try:
        return [r[1] for r in cur.execute(f"PRAGMA table_info({table})")]
    except sqlite3.OperationalError:
        return []


def _has_col(cur, table, col) -> bool:
    return col in _cols(cur, table)


def _insert_adaptive(cur, table: str, value: dict) -> None:
    """只寫入『目標表實際存在』的欄位,避免 schema 不一致時 no such column。"""
    have = _cols(cur, table)
    cols = [c for c in value if c in have]
    if not cols:
        return
    ph = ",".join("?" * len(cols))
    cur.execute(f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({ph})",
                [value[c] for c in cols])


def _fx_on(cur, day: date, last: float) -> float:
    """取當日 USD→TWD;無則往前遞補(最多 10 天);再無沿用上次。"""
    d = day
    for _ in range(10):
        q = f"SELECT {_FX_RATE} FROM {_FX_TABLE} WHERE {_FX_DATE}=?"
        params = [d.isoformat()]
        if _FX_PAIR_COL and _has_col(cur, _FX_TABLE, _FX_PAIR_COL):
            q += f" AND {_FX_PAIR_COL}=?"
            params.append(_FX_PAIR_VAL)
        row = cur.execute(q, params).fetchone()
        if row and row[0]:
            return float(row[0])
        d -= timedelta(days=1)
    return last


def _ensure_aux(cur):
    cur.executescript(f"""
    CREATE TABLE IF NOT EXISTS ibkr_daily_native
        (date TEXT PRIMARY KEY, cash REAL, holdings REAL, networth REAL);
    CREATE TABLE IF NOT EXISTS {_BK_TABLE}
        (date TEXT PRIMARY KEY, net_worth REAL);
    CREATE TABLE IF NOT EXISTS merge_meta (key TEXT PRIMARY KEY, value TEXT);
    """)


def restore(portfolio_db: str):
    con = sqlite3.connect(portfolio_db)
    cur = con.cursor()
    print("還原:移除所有 IBKR 列,daily_networth 回到併入前 …")
    if _has_col(cur, _BK_TABLE, "net_worth"):
        rows = cur.execute(f"SELECT date, net_worth FROM {_BK_TABLE}").fetchall()
        for d, nw in rows:
            cur.execute(f"UPDATE {_DN_TABLE} SET {_DN_VALUE}=? WHERE {_DN_DATE}=?", (nw, d))
        print(f"  已用基準備份還原 {len(rows)} 天每日淨值")
    for t in ("transactions", "positions_current", "realized_pnl", "snapshots",
              "snapshot_positions"):
        if _has_col(cur, t, "broker"):
            cur.execute(f"DELETE FROM {t} WHERE broker=?", (BROKER,))
    cur.execute("DROP TABLE IF EXISTS ibkr_daily_native")
    cur.execute("DELETE FROM merge_meta WHERE key LIKE 'ibkr_%'")
    con.commit(); con.close()
    print("✓ 已還原(IBKR 全數移除)")


def merge(ibkr_db: str, portfolio_db: str, dry_run: bool):
    src = sqlite3.connect(ibkr_db); src.row_factory = sqlite3.Row
    pos = src.execute("SELECT * FROM positions_current WHERE broker='ibkr'").fetchall()
    rp = src.execute("SELECT * FROM realized_pnl WHERE broker='ibkr'").fetchall()
    dn = src.execute("SELECT date, cash, holdings, networth "
                     "FROM daily_networth_native WHERE broker='ibkr' ORDER BY date").fetchall()
    # 每檔最近一日收盤(供 portfolio.db 顯示現價 / 市值)
    last_px: dict[str, float] = {}
    try:
        for r in src.execute("SELECT symbol, date, close FROM price_cache ORDER BY date"):
            last_px[r["symbol"]] = float(r["close"])
    except sqlite3.OperationalError:
        pass
    src.close()

    con = sqlite3.connect(portfolio_db)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    _ensure_aux(cur)

    # 1) 擷取「併入前(非 IBKR)」每日淨值為基準,僅在尚未擷取時做一次
    have_backup = cur.execute(f"SELECT COUNT(*) FROM {_BK_TABLE}").fetchone()[0]
    if not have_backup:
        base = cur.execute(f"SELECT {_DN_DATE}, {_DN_VALUE} FROM {_DN_TABLE}").fetchall()
        cur.executemany(f"INSERT OR REPLACE INTO {_BK_TABLE} VALUES (?,?)",
                        [(r[0], r[1]) for r in base])
        print(f"  擷取基準(台股+嘉信)每日淨值 {len(base)} 天 → {_BK_TABLE}")

    # 2) IBKR 每日 USD → TWD,逐日加到基準
    backup = dict(cur.execute(f"SELECT date, net_worth FROM {_BK_TABLE}").fetchall())
    last_fx = 32.0
    added_twd: dict[str, float] = {}
    for d, cash, hold, nw_usd in dn:
        day = date.fromisoformat(d)
        fx = _fx_on(cur, day, last_fx); last_fx = fx
        added_twd[d] = nw_usd * fx
    cur.executemany("INSERT OR REPLACE INTO ibkr_daily_native VALUES (?,?,?,?)",
                    [(d, c, h, nw) for (d, c, h, nw) in dn])

    preview = []
    for d, twd in added_twd.items():
        base_twd = backup.get(d)
        new_twd = twd if base_twd is None else base_twd + twd
        preview.append((d, base_twd or 0.0, twd, new_twd))

    if dry_run:
        print("\n── DRY-RUN 試算(不寫入)──")
        print(f"  IBKR 持倉 {len(pos)} 檔、平倉 {len(rp)} 筆、每日 {len(dn)} 天")
        for d, b, i, n in preview[-5:]:
            print(f"   {d}  基準TWD {b:,.0f} + IBKR {i:,.0f} = {n:,.0f}")
        print("  (僅顯示最後 5 天) → 移除 --dry-run 正式併入")
        con.close(); return

    # 3) 正式寫入每日淨值(IBKR 較早、基準沒有的日子 → 直接建立)
    for d, b, i, n in preview:
        cur.execute(f"UPDATE {_DN_TABLE} SET {_DN_VALUE}=? WHERE {_DN_DATE}=?", (n, d))
        if cur.rowcount == 0:
            _insert_adaptive(cur, _DN_TABLE,
                             {"date": d, "base_ccy": "TWD",
                              "net_worth": n, "is_real": 0})

    # 4) IBKR 持倉 → positions_current(依目標 schema 自動對應欄位)
    as_of = (max(added_twd) if added_twd else date.today().isoformat()) + "T00:00:00"
    if _has_col(cur, "positions_current", "broker"):
        cur.execute("DELETE FROM positions_current WHERE broker=?", (BROKER,))
        for r in pos:
            sym = r["symbol"]
            qty = float(r["qty"])
            avg = float(r["avg_cost"] or 0)
            cost_basis = float(r["cost_basis"]) if "cost_basis" in r.keys() else qty * avg
            last = last_px.get(sym, avg)
            mv = qty * last
            _insert_adaptive(cur, "positions_current", {
                "broker": BROKER, "account_id": ACCOUNT_ID, "symbol": sym,
                "name": sym,                       # IBKR 以代號為名;fix_positions 不覆寫
                "asset_class": "equity",           # 佔位;由 fix_positions.py 校正
                "qty": qty, "avg_cost": avg, "last_price": last, "ccy": "USD",
                "market_value_native": mv,
                "unrealized_pnl_native": mv - cost_basis,
                "as_of": as_of, "industry": "",
            })

    # 5) IBKR 已實現損益 → realized_pnl(依目標 schema 自動對應欄位)
    if _has_col(cur, "realized_pnl", "broker"):
        cur.execute("DELETE FROM realized_pnl WHERE broker=?", (BROKER,))
        seen: dict[tuple, int] = {}
        for r in rp:
            d = r["date"] if "date" in r.keys() else r["trade_date"]
            sym = r["symbol"]
            q = float(r["qty"] or 0)
            proceeds = float(r["proceeds"]) if "proceeds" in r.keys() else 0.0
            pnl = float(r["pnl"] or 0)
            k = (d, sym); seen[k] = seen.get(k, 0) + 1
            _insert_adaptive(cur, "realized_pnl", {
                "broker": BROKER,
                "external_id": f"{d}-{sym}-{seen[k]}",
                "symbol": sym, "qty": q,
                "price": (proceeds / q) if q else 0.0,
                "pnl": pnl, "ccy": "USD", "trade_date": d,
            })

    cur.execute("INSERT OR REPLACE INTO merge_meta VALUES ('ibkr_merged_at', ?)",
                (datetime.now().isoformat(timespec="seconds"),))
    cur.execute("INSERT OR REPLACE INTO merge_meta VALUES ('ibkr_days', ?)", (str(len(dn)),))
    con.commit(); con.close()
    print(f"✓ 併入完成:IBKR {len(pos)} 檔持倉、{len(dn)} 天每日淨值已併入 {portfolio_db}")
    print("  by_broker 將分出 sinopac / schwab / ibkr。重跑本指令數字不變(冪等)。")
    print("  之後請跑:python fix_positions.py(校正 IBKR 類別/產業)")
    print("  還原:python merge_ibkr_into_portfolio.py --restore " + portfolio_db)


def main():
    a = sys.argv[1:]
    dry = "--dry-run" in a
    a = [x for x in a if x not in ("--dry-run", "--no-dry-run")]
    if "--restore" in a:
        a.remove("--restore")
        if not a:
            print("用法: python merge_ibkr_into_portfolio.py --restore <portfolio.db>"); sys.exit(1)
        restore(a[0]); return
    if len(a) < 2:
        print(__doc__); sys.exit(1)
    merge(a[0], a[1], dry)


if __name__ == "__main__":
    main()
