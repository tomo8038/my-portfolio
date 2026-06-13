"""
merge_ibkr_into_portfolio.py — 把 IBKR(ibkr.db)安全併入 portfolio.db
(對映嘉信 P4b 的 merge_into_portfolio.py;同一套三道安全保證)

每日淨值合併口徑:portfolio.db 的 daily_networth 變成
    台股 TWD + 嘉信 USD×當日匯率 + 盈透 IBKR USD×當日匯率   →  單一 TWD 曲線

三道安全保證:
  1) 不破壞既有資料:台股/嘉信原始每日淨值先備份到 *_daily_backup;
     合併值一律從各 broker 原始值重算,broker≠ibkr 的列完全不碰。
  2) 冪等可重跑:IBKR 以 broker='ibkr' 標記,每次先刪後寫;
     每日淨值從原始基準重算,重跑不疊加。
  3) 可還原:--restore 一鍵移除所有 IBKR 列、把 daily_networth 還原成併入前狀態。

用法:
  python merge_ibkr_into_portfolio.py --dry-run ibkr.db portfolio.db   # 只試算不寫入
  python merge_ibkr_into_portfolio.py          ibkr.db portfolio.db   # 正式併入
  python merge_ibkr_into_portfolio.py --restore           portfolio.db # 還原(移除 IBKR)

⚠ 注意:此工具依「專案進度總結」記載的 portfolio.db 結構撰寫。首次正式併入前,
   請先 `cp portfolio.db portfolio.db.bak`,並用 --dry-run 對照數字;若你的
   merge_into_portfolio.py(嘉信版)欄位命名略有差異,對齊 _FX/_DN 兩個常數即可。
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import date, datetime, timedelta

BROKER = "ibkr"

# 若你的 portfolio.db 欄位命名不同,改這裡即可 ───────────────────────────
_DN_TABLE = "daily_networth"       # 主每日淨值表(TWD)
_DN_DATE = "date"
_DN_VALUE = "networth"             # TWD 淨值欄
_FX_TABLE = "fx_cache"             # 匯率快取(USD->TWD)
_FX_DATE = "date"
_FX_RATE = "rate"
_FX_PAIR_COL = "pair"             # 若無此欄設為 None
_FX_PAIR_VAL = "USDTWD"


def _has_col(cur, table, col) -> bool:
    try:
        return col in [r[1] for r in cur.execute(f"PRAGMA table_info({table})")]
    except sqlite3.OperationalError:
        return False


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
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS ibkr_daily_native
        (date TEXT PRIMARY KEY, cash REAL, holdings REAL, networth REAL);
    CREATE TABLE IF NOT EXISTS stock_daily_backup
        (date TEXT PRIMARY KEY, networth REAL);
    CREATE TABLE IF NOT EXISTS merge_meta (key TEXT PRIMARY KEY, value TEXT);
    """)


def restore(portfolio_db: str):
    con = sqlite3.connect(portfolio_db)
    cur = con.cursor()
    print("還原:移除所有 IBKR 列,daily_networth 回到併入前 …")
    # 還原每日淨值:把備份覆蓋回主表
    rows = cur.execute("SELECT date, networth FROM stock_daily_backup").fetchall()
    for d, nw in rows:
        cur.execute(f"UPDATE {_DN_TABLE} SET {_DN_VALUE}=? WHERE {_DN_DATE}=?", (nw, d))
    # 移除 IBKR 痕跡
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
    txn = src.execute("SELECT * FROM transactions WHERE broker='ibkr'").fetchall()
    dn = src.execute("SELECT date, cash, holdings, networth "
                     "FROM daily_networth_native WHERE broker='ibkr' ORDER BY date").fetchall()
    src.close()

    con = sqlite3.connect(portfolio_db)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    _ensure_aux(cur)

    # 1) 備份既有(非 IBKR)每日淨值一次(僅在尚未備份時)
    have_backup = cur.execute("SELECT COUNT(*) FROM stock_daily_backup").fetchone()[0]
    if not have_backup:
        base = cur.execute(f"SELECT {_DN_DATE}, {_DN_VALUE} FROM {_DN_TABLE}").fetchall()
        cur.executemany("INSERT OR REPLACE INTO stock_daily_backup VALUES (?,?)",
                        [(r[0], r[1]) for r in base])
        print(f"  備份既有每日淨值 {len(base)} 天 → stock_daily_backup")

    # 2) 算 IBKR 每日 TWD,逐日加到 daily_networth(從備份基準重算 → 冪等)
    backup = dict(cur.execute("SELECT date, networth FROM stock_daily_backup").fetchall())
    last_fx = 32.0
    added_twd = {}
    for d, cash, hold, nw_usd in dn:
        day = date.fromisoformat(d)
        fx = _fx_on(cur, day, last_fx); last_fx = fx
        added_twd[d] = nw_usd * fx
    cur.executemany("INSERT OR REPLACE INTO ibkr_daily_native VALUES (?,?,?,?)",
                    [(d, c, h, nw) for (d, c, h, nw) in dn])

    # daily_networth = 備份(非IBKR原始) + IBKR_TWD ;IBKR 期間外的日子維持原值
    preview = []
    for d, twd in added_twd.items():
        base_twd = backup.get(d)
        if base_twd is None:
            # 該日原本沒有台股/嘉信淨值(IBKR 較早) → 直接以 IBKR 值建立
            new_twd = twd
        else:
            new_twd = base_twd + twd
        preview.append((d, base_twd or 0.0, twd, new_twd))

    if dry_run:
        print("\n── DRY-RUN 試算(不寫入)──")
        print(f"  IBKR 持倉 {len(pos)} 檔、平倉 {len(rp)} 筆、交易 {len(txn)} 列、每日 {len(dn)} 天")
        for d, b, i, n in preview[-5:]:
            print(f"   {d}  原TWD {b:,.0f} + IBKR {i:,.0f} = {n:,.0f}")
        print("  (僅顯示最後 5 天) → 加 --no-dry-run 或移除 --dry-run 正式併入")
        con.close(); return

    # 正式寫入
    for d, b, i, n in preview:
        cur.execute(f"UPDATE {_DN_TABLE} SET {_DN_VALUE}=? WHERE {_DN_DATE}=?", (n, d))
        if cur.rowcount == 0:
            cur.execute(f"INSERT INTO {_DN_TABLE} ({_DN_DATE}, {_DN_VALUE}) VALUES (?,?)", (d, n))

    # IBKR 明細(先刪後寫 → 冪等)
    def reinsert(table, rows, cols):
        if _has_col(cur, table, "broker"):
            cur.execute(f"DELETE FROM {table} WHERE broker=?", (BROKER,))
            placeholders = ",".join("?" * len(cols))
            cur.executemany(
                f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})",
                [tuple(r[c] for c in cols) for r in rows])

    reinsert("positions_current", pos, ["broker", "symbol", "qty", "avg_cost", "cost_basis"])
    reinsert("realized_pnl", rp, ["date", "broker", "symbol", "qty", "proceeds", "cost", "pnl"])

    cur.execute("INSERT OR REPLACE INTO merge_meta VALUES ('ibkr_merged_at', ?)",
                (datetime.now().isoformat(timespec="seconds"),))
    cur.execute("INSERT OR REPLACE INTO merge_meta VALUES ('ibkr_days', ?)", (str(len(dn)),))
    con.commit(); con.close()
    print(f"✓ 併入完成:IBKR {len(pos)} 檔持倉、{len(dn)} 天每日淨值已併入 {portfolio_db}")
    print("  by_broker 將分出 sinopac / schwab / ibkr。重跑本指令數字不變(冪等)。")
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
