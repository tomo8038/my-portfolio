"""即時報價監看 — P3。開著它,儀表板的「⚡ 即時模式」就會動起來。

用法(在 my-portfolio 目錄,另開一個終端機):
  python watch.py           # 正式:連永豐,訂閱「目前持倉」的即時成交
  python watch.py --mock    # 模擬:以隨機漫步產生報價(免帳號,驗證流程)

流程:
  1. 讀 positions_current 取得目前持倉清單
  2. 訂閱各檔即時成交 Tick(shioaji callback 來自背景執行緒,
     先丟進 queue,由主執行緒批次寫入 SQLite live_quotes — 避免
     sqlite 跨執行緒問題,也把高頻 tick 合併成每秒一次的寫入)
  3. 儀表板開「⚡ 即時模式」即每 2 秒讀 live_quotes 重算市值
  4. Ctrl+C 結束:登出券商(不佔永豐 5 連線額度)、清空 live_quotes

注意:與 run.py 不同,watch.py 會「常駐」直到你手動停止;
台股盤後沒有成交,畫面靜止是正常的(可用 --mock 看效果)。
"""
import argparse
import queue
import random
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from core.db import DB

FLUSH_SEC = 1.0          # 主執行緒每秒批次寫一次(合併高頻 tick)
STOP = False


def _sigint(*_):
    global STOP
    STOP = True


def run_mock(db: DB) -> None:
    """隨機漫步報價:以 positions_current 的現價為起點,每秒跳動一次。"""
    pos = db.all_positions()
    if not pos:
        raise SystemExit("positions_current 是空的。請先執行 python run.py --mock")
    px = {p["symbol"]: float(p["last_price"]) for p in pos}
    print(f"[watch:mock] 模擬 {len(px)} 檔報價(Ctrl+C 結束):"
          f"{', '.join(px)}")
    rng = random.Random()
    while not STOP:
        ts = datetime.now().isoformat(timespec="seconds")
        rows = []
        for sym in px:
            px[sym] *= 1 + rng.uniform(-0.0015, 0.0015)
            rows.append((sym, round(px[sym], 2), ts))
        db.upsert_live_quotes(rows)
        print(f"\r[watch:mock] {ts} " +
              "  ".join(f"{s}={p:,.1f}" for s, p, _ in rows[:4]),
              end="", flush=True)
        time.sleep(1)
    print()


def run_real(db: DB) -> None:
    """連永豐訂閱即時 Tick。callback → queue → 主執行緒寫 SQLite。"""
    # 沿用 run.py 的 .env 讀取(編碼容錯、遮罩診斷都一致)
    from run import load_sinopac_creds
    from adapters.sinopac import SinopacAdapter

    pos = db.all_positions()
    if not pos:
        raise SystemExit("positions_current 是空的。請先執行 python run.py")
    symbols = sorted({p["symbol"] for p in pos})

    q: queue.Queue = queue.Queue()
    adapter = SinopacAdapter(load_sinopac_creds())
    try:
        adapter.connect()
        ok = adapter.subscribe_ticks(
            symbols, lambda sym, price, ts: q.put((sym, price, ts)))
        if not ok:
            raise SystemExit("一檔都沒訂閱成功,請檢查連線/合約代號。")
        print(f"[watch] 監看中(Ctrl+C 結束)。提醒:非交易時段不會有成交,"
              f"可先用 python watch.py --mock 驗證流程。")

        n_ticks = 0
        while not STOP:
            time.sleep(FLUSH_SEC)
            latest: dict[str, tuple[float, str]] = {}
            while True:                      # 清空 queue,同檔只留最新
                try:
                    sym, price, ts = q.get_nowait()
                    latest[sym] = (price, str(ts)[:19])
                    n_ticks += 1
                except queue.Empty:
                    break
            if latest:
                db.upsert_live_quotes(
                    [(s, p, t) for s, (p, t) in latest.items()])
                print(f"\r[watch] 累計 {n_ticks} ticks,最新:" +
                      "  ".join(f"{s}={p:,.1f}"
                                for s, (p, _) in list(latest.items())[:4]),
                      end="", flush=True)
        print()
    finally:
        adapter.disconnect()    # 永豐 5 連線額度:無論成敗都登出


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true",
                    help="隨機漫步模擬報價(免帳號)")
    ap.add_argument("--db", default="portfolio.db")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _sigint)
    signal.signal(signal.SIGTERM, _sigint)   # 被 kill/排程終止也要清理、登出
    db = DB(args.db)
    try:
        if args.mock:
            run_mock(db)
        else:
            run_real(db)
    finally:
        db.clear_live_quotes()   # 結束即清空,儀表板不會讀到殘留舊價
        db.close()
        print("[watch] 已結束,live_quotes 已清空。")


if __name__ == "__main__":
    main()
