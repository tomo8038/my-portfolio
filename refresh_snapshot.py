"""refresh_snapshot.py — 從 positions_current(三家)重算今天的合併快照與當日淨值。

為什麼需要:
  合併流程裡 run.py 先寫今天的快照(那時只有台股),美股是後面 merge 才進來的。
  若不重算,儀表板 KPI 的「總資產淨值」會只剩台股,且與當日 daily_networth 對不上。
  本工具在所有 merge 完成後,讀 positions_current 全部券商 + broker_cash + 當日
  USD/TWD 匯率,重算一筆今天的合併快照(snapshots / snapshot_positions / 當日
  daily_networth is_real=1),讓「總資產淨值 = 當日淨值 = 台股+嘉信+盈透」一致。

用法:
  python refresh_snapshot.py                 # 預設 portfolio.db
  python refresh_snapshot.py --db portfolio.db
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core.db import DB
from core import aggregate
from core.fxrate import FXBackfiller

BASE = "TWD"


def refresh(db_path: str) -> None:
    if not Path(db_path).exists():
        raise SystemExit(f"找不到資料庫:{db_path}")
    db = DB(db_path)
    fx = FXBackfiller(db, BASE)
    # 確保今天有 USD/TWD 匯率可換算(抓不到就沿用既有 fx_cache)
    try:
        fx.backfill("USD", (date.today() - timedelta(days=10)).isoformat())
    except Exception as e:
        print(f"[fx] 今日匯率取得略過(用既有 fx_cache):{e}")

    snap = aggregate.combined_snapshot(db, fx, BASE)
    ts = aggregate.write_combined_snapshot(db, fx, BASE)

    print(f"[snapshot] 已從 positions_current 重算合併快照 {ts}")
    print(f"  總資產淨值 NT$ {snap['net_worth']:,.0f}"
          f"(投資 {snap['invested']:,.0f} + 現金 {snap['cash']:,.0f})")
    for b, v in sorted(snap.get("breakdown", {}).get("by_broker", {}).items(),
                       key=lambda x: -x[1]):
        print(f"    {b:<8} NT$ {v:,.0f}")
    db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="portfolio.db")
    args = ap.parse_args()
    refresh(args.db)
