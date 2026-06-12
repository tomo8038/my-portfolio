"""手動出入金補登工具 — P3 TWR 報酬率的資料基礎。

為什麼需要:程式目前只會自動累積「買賣成交」,出入金(銀行轉入交割戶/
提領)券商 API 不易自動取得。沒補登的入金會被 TWR 誤判成投資獲利,
也會讓回補引擎把入金當天的淨值跳階畫錯。補登後兩者都會正確。

用法(在 my-portfolio 目錄):
  python flows.py add --date 2026-06-01 --amount 500000 --type DEPOSIT
  python flows.py add --date 2026-06-20 --amount 200000 --type WITHDRAW
  python flows.py list
  python flows.py rm --id flow-2026-06-01-DEPOSIT-1717... (list 會列出 id)

補登/刪除後,執行 python run.py --backfill-only 重算歷史曲線。
"""
import argparse
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from core.db import DB
from core.models import Transaction


def cmd_add(db: DB, args) -> None:
    t = args.type.upper()
    if t not in ("DEPOSIT", "WITHDRAW"):
        raise SystemExit("--type 只接受 DEPOSIT(入金)或 WITHDRAW(出金)")
    amt = abs(Decimal(str(args.amount)))
    signed = amt if t == "DEPOSIT" else -amt   # 模型慣例:入金正、出金負
    ext_id = f"flow-{args.date}-{t}-{int(time.time())}"
    n = db.insert_transactions([Transaction(
        broker="manual", external_id=ext_id, symbol="",
        txn_type=t, qty=Decimal(0), price=Decimal(0),
        amount=signed, ccy=args.ccy, trade_date=args.date,
    )])
    if n:
        print(f"已補登:{args.date} {t} {args.ccy} {amt:,.0f}(id={ext_id})")
        print("提醒:執行 python run.py --backfill-only 重算歷史曲線。")
    else:
        print("未新增(可能重複)。")


def cmd_list(db: DB) -> None:
    rows = db.con.execute(
        "SELECT external_id, trade_date, txn_type, amount, ccy "
        "FROM transactions WHERE txn_type IN ('DEPOSIT','WITHDRAW') "
        "ORDER BY trade_date").fetchall()
    if not rows:
        print("尚無手動補登的出入金紀錄。")
        return
    print(f"{'日期':<12}{'類型':<10}{'金額':>14}  id")
    for ext, d, t, a, c in rows:
        print(f"{d:<12}{t:<10}{c} {abs(a):>10,.0f}  {ext}")


def cmd_rm(db: DB, args) -> None:
    cur = db.con.execute(
        "DELETE FROM transactions WHERE external_id = ? "
        "AND txn_type IN ('DEPOSIT','WITHDRAW')", (args.id,))
    db.con.commit()
    if cur.rowcount:
        print(f"已刪除 {args.id}。提醒:執行 python run.py --backfill-only 重算。")
    else:
        print(f"找不到 id = {args.id}(用 python flows.py list 查看)。")


def main() -> None:
    ap = argparse.ArgumentParser(description="手動出入金補登(TWR/回補用)")
    ap.add_argument("--db", default="portfolio.db")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="補登一筆出入金")
    a.add_argument("--date", required=True, help="YYYY-MM-DD")
    a.add_argument("--amount", required=True, type=float, help="金額(取絕對值)")
    a.add_argument("--type", required=True, help="DEPOSIT / WITHDRAW")
    a.add_argument("--ccy", default="TWD")

    sub.add_parser("list", help="列出所有出入金紀錄")

    r = sub.add_parser("rm", help="刪除一筆(id 由 list 取得)")
    r.add_argument("--id", required=True)

    args = ap.parse_args()
    db = DB(args.db)
    try:
        if args.cmd == "add":
            cmd_add(db, args)
        elif args.cmd == "list":
            cmd_list(db)
        else:
            cmd_rm(db, args)
    finally:
        db.close()


if __name__ == "__main__":
    main()
