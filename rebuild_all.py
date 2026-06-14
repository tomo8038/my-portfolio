"""rebuild_all.py — 一鍵從零重建 portfolio.db(台股 + 嘉信 + 盈透)。

為什麼要這支:
  各步驟有嚴格順序與相依(先建庫+台股+匯率,才能併美股;嘉信要在盈透之前)。
  手動逐步容易漏(例如漏了建庫那步,就會 no such table: fx_cache)。
  本腳本把整條流程固定下來,一個指令跑完,且「真正從零」(預設先刪舊檔),
  天然避開合併備份表殘留造成的疊加/尖峰問題。

用法(放在 my-portfolio 根目錄):
  python rebuild_all.py                 # 從零重建(會先刪 portfolio.db / ibkr.db)
  python rebuild_all.py --keep          # 不刪舊檔(沿用既有 DB)
  python rebuild_all.py --mock-taiwan   # 台股用假資料(免永豐憑證,純測流程)
  python rebuild_all.py --skip-taiwan   # 完全不做台股,只建美股(免憑證)
  python rebuild_all.py --schwab-csv schwab.csv --ibkr-csv U5529822_TRANSACTIONS.csv

跑完開儀表板:streamlit run viewer/app.py
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable


def run_step(cmd: list[str], desc: str, essential: bool = True) -> bool:
    print(f"\n▶ {desc}\n  $ {' '.join(cmd)}")
    rc = subprocess.run(cmd, cwd=str(HERE)).returncode
    if rc != 0:
        if essential:
            print(f"\n✗ 步驟失敗(return {rc}):{desc}")
            print("  已中止 — 請看上面的錯誤訊息修正後重跑。")
            sys.exit(rc)
        print(f"⚠ 步驟未成功(略過,繼續):{desc}")
        return False
    return True


def ensure_db_schema(db: str) -> None:
    """確保 portfolio.db 至少有完整表結構(含 fx_cache),否則 merge 會 no such table。"""
    sys.path.insert(0, str(HERE))
    from core.db import DB
    DB(db).close()
    print(f"  已確保 {db} 具備完整表結構(含 fx_cache)")


def find_csv(patterns: list[str], given: str | None) -> str | None:
    if given:
        return given
    for pat in patterns:
        hits = sorted(p.name for p in HERE.glob(pat))
        if hits:
            return hits[0]
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="一鍵從零重建 portfolio.db(台股+嘉信+盈透)")
    ap.add_argument("--db", default="portfolio.db")
    ap.add_argument("--schwab-csv", default=None, help="嘉信 CSV(預設自動找 schwab*.csv)")
    ap.add_argument("--ibkr-csv", default=None, help="盈透 CSV(預設自動找 *TRANSACTIONS*.csv)")
    ap.add_argument("--keep", action="store_true", help="不刪既有 portfolio.db/ibkr.db")
    ap.add_argument("--mock-taiwan", action="store_true", help="台股用 run.py --mock(假資料,免憑證)")
    ap.add_argument("--skip-taiwan", action="store_true", help="完全跳過台股,只建美股")
    args = ap.parse_args()

    db = args.db
    schwab = find_csv(["schwab*.csv", "*Schwab*.csv", "*嘉信*.csv"], args.schwab_csv)
    ibkr = find_csv(["*TRANSACTIONS*.csv", "U*_*.csv", "*ibkr*.csv", "*盈透*.csv"], args.ibkr_csv)

    print("=" * 64)
    print("  從零重建 portfolio.db(台股 + 嘉信 + 盈透)")
    print("=" * 64)
    print(f"  目標 DB   : {db}")
    print(f"  嘉信 CSV  : {schwab or '(找不到)'}")
    print(f"  盈透 CSV  : {ibkr or '(找不到)'}")
    print(f"  台股模式  : {'跳過' if args.skip_taiwan else ('假資料' if args.mock_taiwan else 'run.py(永豐憑證)')}")

    if not schwab:
        print("\n✗ 找不到嘉信 CSV。請用 --schwab-csv 指定檔名。")
        sys.exit(1)

    # 0) 真正從零:先刪舊檔(避免合併備份表殘留 → 疊加/尖峰)
    if not args.keep:
        for f in (db, "ibkr.db"):
            p = HERE / f
            if p.exists():
                p.unlink()
                print(f"  已刪除舊檔:{f}")

    # 1) 建庫 + 台股 + 匯率(run.py 會建立含 fx_cache 的完整表結構)
    if args.skip_taiwan:
        ensure_db_schema(db)
        print("  已跳過台股,僅建立空庫 + 表結構。")
    else:
        cmd = [PY, "run.py", "--db", db] + (["--mock"] if args.mock_taiwan else [])
        ok = run_step(cmd, "建立資料庫 + 台股 + 匯率(run.py)", essential=False)
        if not ok:
            print("  台股步驟未成功 → 改為只建空庫表結構,繼續做美股(US-only)。")
            print("  若要含台股:確認 .env 永豐憑證後重跑,或加 --mock-taiwan。")
            ensure_db_schema(db)

    # 2) 嘉信(merge 會自行抓六年 USD/TWD 匯率)
    run_step([PY, "merge_into_portfolio.py", schwab, db], "併入嘉信(Schwab)")

    # 3) 盈透:build → merge
    if ibkr:
        run_step([PY, "build_history_ibkr.py", ibkr, "ibkr.db"], "重建盈透 ibkr.db")
        run_step([PY, "merge_ibkr_into_portfolio.py", "ibkr.db", db], "併入盈透(IBKR)")
    else:
        print("\n⚠ 找不到盈透 CSV(*TRANSACTIONS*.csv)→ 略過 IBKR。用 --ibkr-csv 指定。")

    # 4) 校正分類/產業(三家一次修好)
    run_step([PY, "fix_positions.py", "--db", db], "校正持倉類別 / 產業")

    # 5) 重算今天的合併快照:讓 KPI 總資產淨值 = 當日淨值 = 台股+嘉信+盈透
    run_step([PY, "refresh_snapshot.py", "--db", db],
             "重算今天的合併快照(KPI 對齊三家)", essential=False)

    print("\n" + "=" * 64)
    print("  ✓ 全部完成!開啟儀表板:")
    print("      streamlit run viewer/app.py")
    print("=" * 64)


if __name__ == "__main__":
    main()
