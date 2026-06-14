"""修正美股幣別 + 重算合併快照 — P4d hotfix。

症狀:儀表板「資產配置 / 持倉明細」金額過小;券商維度顯示嘉信/盈透只有數十萬。
原因:positions_current(及 broker_cash)裡,嘉信 / 盈透的幣別被標成 TWD,
      導致換匯時被當台幣(匯率=1),美股市值低估約 32 倍。

本程式做兩件事:
  1. 把 schwab / ibkr 的 ccy 一律更正為 USD(positions_current 與 broker_cash)。
  2. 重算「今天」的合併快照(snapshots / daily_networth / snapshot_positions),
     讓總覽 KPI 與資產配置立刻一致。

用法:
  python fix_us_currency.py                      # 需網路抓今日 USD/TWD
  python fix_us_currency.py --usd-rate 32.5       # 離線:手動指定匯率
  python fix_us_currency.py --db portfolio.db --brokers schwab,ibkr

之後若要把「歷史每日曲線」也一併用正確幣別重畫,再跑:
  python rebuild_history.py
"""
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from core.db import DB
from core import aggregate
from core.fxrate import FXBackfiller

BASE_CCY = "TWD"


def main() -> None:
    ap = argparse.ArgumentParser(description="修正美股幣別並重算合併快照")
    ap.add_argument("--db", default="portfolio.db")
    ap.add_argument("--brokers", default="schwab,ibkr",
                    help="要更正為 USD 的券商(逗號分隔)")
    ap.add_argument("--usd-rate", type=float, default=None,
                    help="手動 USD/TWD 匯率(離線用)")
    args = ap.parse_args()

    db = DB(args.db)
    brokers = [b.strip() for b in args.brokers.split(",") if b.strip()]
    qmarks = ",".join("?" * len(brokers))

    # 1) 更正幣別
    cur = db.con.cursor()
    n_pos = cur.execute(
        f"UPDATE positions_current SET ccy='USD' "
        f"WHERE broker IN ({qmarks}) AND ccy<>'USD'", brokers).rowcount
    aggregate.ensure_broker_cash(db)
    n_cash = cur.execute(
        f"UPDATE broker_cash SET ccy='USD' "
        f"WHERE broker IN ({qmarks}) AND ccy<>'USD'", brokers).rowcount
    db.con.commit()
    print(f"[fix] positions_current 更正 {n_pos} 列、broker_cash 更正 {n_cash} 列 → USD")

    # 2) 確保今日匯率存在
    fx = FXBackfiller(db, BASE_CCY)
    start = (date.today() - timedelta(days=7)).isoformat()
    today = date.today().isoformat()
    if args.usd_rate is not None:
        fx.backfill("USD", start, today,
                    fetch=lambda pair, s, e: [(s, args.usd_rate)])
        print(f"[fx] 使用手動匯率 USD/TWD={args.usd_rate}")
    else:
        try:
            fx.backfill("USD", start, today)
        except Exception as e:
            print(f"[fx] 取匯率失敗({e});請改用 --usd-rate 指定後重跑。")
            db.close(); raise SystemExit(1)

    # 3) 重算合併快照
    ts = aggregate.write_combined_snapshot(db, fx, BASE_CCY, today)
    snap = aggregate.combined_snapshot(db, fx, BASE_CCY, today)
    print(f"[snapshot] 合併快照 {ts}  總淨值 {snap['net_worth']:,.0f} {BASE_CCY}")
    for b, v in snap["breakdown"]["by_broker"].items():
        nm = {"sinopac": "永豐金", "schwab": "嘉信", "ibkr": "盈透"}.get(b, b)
        print(f"            {nm}: {v:,.0f} {BASE_CCY}")
    print("  by_ccy:", {k: round(v, 0) for k, v in snap["breakdown"]["by_ccy"].items()})

    db.close()
    print("\n完成。重開儀表板即可看到正確金額;若要重畫歷史曲線:python rebuild_history.py")


if __name__ == "__main__":
    main()
