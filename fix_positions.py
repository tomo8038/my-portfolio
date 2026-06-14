"""fix_positions.py — 正規化 portfolio.db 的持倉分類與產業別。

為什麼需要:
  三家券商寫進 positions_current 的 asset_class / industry 各有問題——
  嘉信用寫死的 ETF 白名單(名單外的 ETF 變 equity)、產業恆為空;
  盈透併入時 asset_class/industry 留空。本工具在「合併之後」統一以
  core/classify 重算 asset_class + industry(並補空白 name / ccy),
  一次修好三家,且冪等可重跑。

用法(在 my-portfolio 目錄):
  python fix_positions.py                      # 修 portfolio.db(離線規則)
  python fix_positions.py --db portfolio.db    # 指定資料庫
  python fix_positions.py --yfinance           # 規則查不到產業的美股,上網補 sector

每次重跑 build/merge 之後再跑一次即可(它只改 asset_class/industry/補空欄,
不動股數、成本、市值)。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import classify as C


def _broker_default_ccy(broker: str) -> str:
    return "TWD" if (broker or "").lower() == "sinopac" else "USD"


def fix(db_path: str, use_yfinance: bool = False) -> None:
    if not Path(db_path).exists():
        raise SystemExit(f"找不到資料庫:{db_path}")
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT broker, account_id, symbol, name, asset_class, ccy, industry "
        "FROM positions_current").fetchall()
    if not rows:
        print("positions_current 沒有資料,無需處理。")
        con.close()
        return

    changed = 0
    print(f"{'券商':<8}{'代號':<10}{'類別(舊→新)':<22}{'產業(舊→新)'}")
    print("-" * 70)
    for r in rows:
        broker = r["broker"]
        sym = r["symbol"]
        name = (r["name"] or "").strip()
        ccy = (r["ccy"] or "").strip() or _broker_default_ccy(broker)

        res = C.classify(sym, name, ccy, use_yfinance=use_yfinance)
        new_ac = res["asset_class"]
        new_ind = res["industry"]
        new_name = name or res["name"]

        old_ac = r["asset_class"] or ""
        old_ind = r["industry"] or ""

        if (new_ac != old_ac or new_ind != old_ind
                or new_name != name or (r["ccy"] or "") != ccy):
            con.execute(
                "UPDATE positions_current SET asset_class=?, industry=?, "
                "name=?, ccy=? WHERE broker=? AND account_id=? AND symbol=?",
                (new_ac, new_ind, new_name, ccy,
                 broker, r["account_id"], sym))
            changed += 1
            print(f"{broker:<8}{sym:<10}"
                  f"{(old_ac or '∅')+' → '+new_ac:<22}"
                  f"{(old_ind or '∅')+' → '+new_ind}")

    con.commit()
    con.close()
    print("-" * 70)
    print(f"完成:檢視 {len(rows)} 檔,更新 {changed} 檔。"
          f"{'(已含 yfinance sector 補強)' if use_yfinance else ''}")
    print("開儀表板查看:streamlit run viewer/app.py")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="portfolio.db")
    ap.add_argument("--yfinance", action="store_true",
                    help="規則查不到產業的美股,額外上網查 sector(需網路)")
    args = ap.parse_args()
    fix(args.db, args.yfinance)
