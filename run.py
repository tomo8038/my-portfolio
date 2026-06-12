"""P0 主程式 — 開一次,看到永豐當下總資產。

用法:
  python run.py          # 正式:連永豐(需 .env 憑證)
  python run.py --mock   # 模擬:用假資料驗證整條流程

流程:連線 → 抓持倉+現金 → 寫入 SQLite → 存真實快照 → 印出摘要 → 登出
之後執行 `streamlit run viewer/app.py` 開儀表板。
"""
import argparse
import os
import sys
from decimal import Decimal
from pathlib import Path

# 讓 core / adapters 可被匯入(以專案根目錄執行)
sys.path.insert(0, str(Path(__file__).parent))

from core.db import DB

BASE_CCY = "TWD"   # P0 全部是台股,基準幣別即 TWD;P1 加美股後才需要 FX


def _read_text_tolerant(path: Path) -> str:
    """容錯讀檔:依序嘗試 utf-8-sig(去 BOM)/ utf-8 / cp950(Big5)/ latin-1。

    金鑰與鍵名都是 ASCII,即使中文註解用了不同編碼也不影響解析。
    """
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp950", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def _parse_env(path: Path) -> dict:
    """手動解析 .env(不依賴 python-dotenv)。"""
    out: dict[str, str] = {}
    for line in _read_text_tolerant(path).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


def _mask(s: str) -> str:
    """遮罩金鑰,只露頭尾,用於診斷輸出。"""
    if not s:
        return "(空)"
    return (s[:4] + "…" + s[-2:]) if len(s) > 6 else (s[0] + "***")


def load_sinopac_creds() -> dict:
    """從專案目錄的 .env 讀永豐憑證。

    - 不依賴 python-dotenv(沒裝也能用,內建手動解析)。
    - 自動處理 Notepad++ 的 BOM / 編碼(utf-8-sig / utf-8 / cp950 / latin-1)。
    - 從 run.py 所在目錄找 .env,不受當前工作目錄(CWD)影響。
    - 會印出讀到了哪些鍵(金鑰遮罩),方便排查。
    絕不把金鑰寫死在程式裡。
    """
    project_dir = Path(__file__).parent
    env_path = project_dir / ".env"

    if not env_path.exists():
        # Windows 常見陷阱:存檔時被自動加上 .txt
        wrong = project_dir / ".env.txt"
        hint = ""
        if wrong.exists():
            hint = ("\n⚠ 偵測到 .env.txt — Windows 自動加了 .txt 副檔名。\n"
                    "  請先開啟檔案總管的「顯示副檔名」,再把 .env.txt 改名為 .env。")
        raise SystemExit(
            f"找不到 .env 檔。預期位置:\n  {env_path}{hint}\n"
            f"請把 .env.template 複製成 .env 並填入金鑰,"
            f"或先用模擬模式:python run.py --mock"
        )

    file_vals = _parse_env(env_path)

    def pick(key: str) -> str:
        return (os.getenv(key) or file_vals.get(key) or "").strip()

    creds = {
        "api_key": pick("SINOPAC_API_KEY"),
        "secret_key": pick("SINOPAC_SECRET_KEY"),
        "ca_path": pick("SINOPAC_CA_PATH"),
        "ca_passwd": pick("SINOPAC_CA_PASSWD"),
        "person_id": pick("SINOPAC_PERSON_ID"),
        "simulation": pick("SINOPAC_SIMULATION") == "1",
    }

    # 相對憑證路徑換算成相對「專案目錄」,避免受 CWD 影響
    if creds["ca_path"] and not Path(creds["ca_path"]).is_absolute():
        creds["ca_path"] = str((project_dir / creds["ca_path"]).resolve())

    # 診斷輸出:讓你一眼看出有沒有讀到
    print(f"[env] 讀取 {env_path}")
    print(f"[env] 解析到的鍵:{list(file_vals.keys()) or '(無)'}")
    print(f"[env] API_KEY={_mask(creds['api_key'])}  "
          f"SECRET_KEY={_mask(creds['secret_key'])}  "
          f"模式={'模擬' if creds['simulation'] else '正式'}")

    # 還沒替換範本佔位字
    if creds["api_key"].startswith("你的") or creds["secret_key"].startswith("你的"):
        raise SystemExit(
            ".env 裡仍是範本佔位字(例如「你的APIKey」)。\n"
            "請填入永豐實際的 API Key / Secret Key 後再執行。"
        )

    missing = [k for k in ("api_key", "secret_key") if not creds[k]]
    if missing:
        raise SystemExit(
            f"已讀到 .env,但必填欄位是空的:{', '.join(missing)}\n"
            f"請確認每行格式為  SINOPAC_API_KEY=你的金鑰  (等號兩側不要有空格或引號),且檔案已存檔。"
        )
    return creds


def make_adapter(mock: bool):
    if mock:
        from adapters.mock import MockAdapter
        return MockAdapter()
    from adapters.sinopac import SinopacAdapter
    return SinopacAdapter(load_sinopac_creds())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", help="用假資料跑通流程(免憑證)")
    ap.add_argument("--db", default="portfolio.db", help="SQLite 檔案路徑")
    ap.add_argument("--backfill-only", action="store_true",
                    help="不連券商,只重跑淨值回補(例如剛裝好 yfinance 後修補歷史)")
    args = ap.parse_args()

    db = DB(args.db)

    if args.backfill_only:
        from core.backfill import run_backfill
        from core.prices import FXService, PriceService
        stats = run_backfill(db, PriceService(db), FXService(db, BASE_CCY),
                             BASE_CCY)
        print(f"[backfill] {stats.get('note') or (str(stats['segments']) + ' 個區間、' + str(stats['days_written']) + ' 天已寫入')}")
        db.close()
        return

    adapter = make_adapter(args.mock)

    try:
        # 1) 抓資料
        adapter.connect()
        positions = adapter.list_positions()
        cash = adapter.list_cash()

        # 2) 彙總(P0 全為 TWD,直接相加;P1 接美股後改走 FX 換算)
        invested = sum((p.market_value for p in positions), Decimal(0))
        cost = sum((p.cost_value for p in positions), Decimal(0))
        cash_total = sum(cash.values(), Decimal(0))
        net_worth = invested + cash_total
        unrealized = invested - cost

        breakdown = {
            "by_broker": {adapter.name: float(net_worth)},
            "by_asset_class": _group(positions, lambda p: p.asset_class),
            "by_ccy": {k: float(v) for k, v in cash.items()} | {"TWD_invested": float(invested)},
            "cash": {k: float(v) for k, v in cash.items()},
        }

        # 3) 寫入 SQLite
        db.replace_positions(adapter.name, positions)
        ts = db.save_snapshot(
            base_ccy=BASE_CCY,
            net_worth=float(net_worth), invested=float(invested),
            cash=float(cash_total), cost=float(cost),
            breakdown=breakdown,
        )
        db.save_snapshot_positions(ts, positions)        # P1:回補錨點

        # 3.5) P1:累積今日成交 → 交易史
        txns = adapter.list_transactions()
        if txns:
            n = db.insert_transactions(txns)
            print(f"[txn] 新增 {n} 筆交易紀錄")

        # 3.6) P1:回補快照之間的每日淨值
        from core.backfill import run_backfill
        from core.prices import FXService, PriceService
        stats = run_backfill(db, PriceService(db), FXService(db, BASE_CCY),
                             BASE_CCY)
        if stats.get("note"):
            print(f"[backfill] {stats['note']}")
        else:
            print(f"[backfill] 已回補 {stats['segments']} 個區間、"
                  f"寫入 {stats['days_written']} 天的每日淨值")

        # 3.7) P2:刷新持倉的股息快取(供儀表板「股息現金流」分頁)
        try:
            from core.dividends import DividendService
            div = DividendService(db)
            n_div = sum(div.refresh(p.symbol, p.ccy) for p in positions)
            if n_div:
                print(f"[div] 更新 {n_div} 筆除息事件")
        except Exception as e:   # 沒網路/沒裝 yfinance 都不影響主流程
            print(f"[div] 股息更新略過:{e}")

        # 3.8) P3:抓近 60 天已實現損益(去重寫入,跨次執行自動累積)
        try:
            from datetime import date, timedelta
            begin = (date.today() - timedelta(days=60)).isoformat()
            realized = adapter.list_realized_pnl(begin, date.today().isoformat())
            if realized:
                n = db.insert_realized(realized)
                if n:
                    print(f"[realized] 新增 {n} 筆已實現損益")
        except Exception as e:
            print(f"[realized] 已實現損益略過:{e}")

        # 4) 終端機摘要(不開儀表板也看得到重點)
        _print_summary(ts, positions, net_worth, invested, cash_total,
                       unrealized, cost, args.db)

    finally:
        adapter.disconnect()   # 永豐 5 連線額度:無論成敗都登出
        db.close()


def _group(positions, key) -> dict:
    out: dict[str, float] = {}
    for p in positions:
        out[key(p)] = out.get(key(p), 0.0) + float(p.market_value)
    return out


def _print_summary(ts, positions, net_worth, invested, cash_total,
                   unrealized, cost, db_path) -> None:
    w = 62
    pct = (unrealized / cost * 100) if cost else Decimal(0)
    print("\n" + "=" * w)
    print(f"  同步完成  {ts}")
    print("=" * w)
    print(f"  總資產淨值      NT$ {net_worth:>14,.0f}")
    print(f"  投資市值        NT$ {invested:>14,.0f}")
    print(f"  現金            NT$ {cash_total:>14,.0f}")
    print(f"  未實現損益      NT$ {unrealized:>+14,.0f}  ({pct:+.1f}%)")
    print("-" * w)
    print(f"  {'代號':<8}{'名稱':<10}{'股數':>8}{'均價':>9}{'現價':>9}{'市值':>14}")
    for p in sorted(positions, key=lambda x: -x.market_value):
        print(f"  {p.symbol:<8}{p.name:<10}{p.qty:>8,.0f}"
              f"{p.avg_cost:>9,.1f}{p.last_price:>9,.1f}"
              f"{p.market_value:>14,.0f}")
    print("=" * w)
    print(f"  已寫入 {db_path}")
    print("  開啟儀表板:streamlit run viewer/app.py\n")


if __name__ == "__main__":
    main()
