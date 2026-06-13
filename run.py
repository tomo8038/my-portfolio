"""P4 主程式 — 多券商(永豐 + 嘉信 + 盈透)同步、多幣別折算、快照與回補。

用法:
  python run.py                  # 正式:同步 .env 啟用的所有券商
  python run.py --mock           # 模擬:永豐(TWD)+ 嘉信(USD)假資料驗證全流程
  python run.py --backfill-only  # 不連券商,只重跑淨值回補

流程:逐家連線 → 抓持倉+現金 → 全部折算 TWD → 寫入 SQLite → 存真實快照
      → 記交易 → 回補 → 更新股息/已實現 → 印摘要 → 登出
之後執行 `streamlit run viewer/app.py` 開儀表板。

P4 重要規則:
  * 任何一家券商同步失敗 → 該家略過、其他家照常,但「不寫快照、不回補」,
    避免少算一家造成淨值曲線假跳水(快照是回補錨點,必須百分百準確)。
  * 快照錨點(snapshot_positions)主鍵為 (ts, symbol):同一檔出現在多家
    券商時(例:嘉信與盈透都有 AAPL),先按 symbol 合併再存。
  * 外幣折算使用既有 core/prices.FXService(fx_cache 表,週末/假日往前
    遞補最近匯率);回補引擎本來就會逐日乘當日匯率,無需改動。
"""
import argparse
import os
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

# 讓 core / adapters 可被匯入(以專案根目錄執行)
sys.path.insert(0, str(Path(__file__).parent))

from core.db import DB

BASE_CCY = "TWD"   # 基準幣別:所有快照合計皆以 TWD 計


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


# ---------------------------------------------------------------- P4:多券商


def load_env_all() -> dict:
    """讀整份 .env(環境變數優先);沒有 .env 也回空 dict(--mock 可用)。"""
    env_path = Path(__file__).parent / ".env"
    file_vals = _parse_env(env_path) if env_path.exists() else {}
    merged = dict(file_vals)
    for k in list(file_vals) + [
        "SINOPAC_ENABLED", "SCHWAB_ENABLED", "IBKR_ENABLED",
        "SCHWAB_APP_KEY", "SCHWAB_APP_SECRET", "SCHWAB_CALLBACK_URL",
        "SCHWAB_TOKEN_PATH", "IBKR_GATEWAY_URL",
    ]:
        if os.getenv(k):
            merged[k] = os.getenv(k)
    return merged


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "y", "on")


def make_adapters(mock: bool) -> list:
    """依 .env 開關組出本次要同步的券商清單(P4)。

    永豐:預設啟用(SINOPAC_ENABLED=0 可關),沿用 load_sinopac_creds()
          的完整診斷;若未填金鑰但啟用了其他券商,跳過永豐而非報錯。
    嘉信:SCHWAB_ENABLED=1(需先跑過 python schwab_auth.py)
    盈透:IBKR_ENABLED=1(需先啟動 Client Portal Gateway 並登入)
    """
    if mock:
        from adapters.mock import MockAdapter
        from adapters.mock_us import MockUSAdapter
        return [MockAdapter(), MockUSAdapter()]

    env = load_env_all()
    schwab_on = _truthy(env.get("SCHWAB_ENABLED"))
    ibkr_on = _truthy(env.get("IBKR_ENABLED"))
    sinopac_on = env.get("SINOPAC_ENABLED", "1").strip() != "0"

    adapters = []
    if sinopac_on:
        if env.get("SINOPAC_API_KEY") or not (schwab_on or ibkr_on):
            from adapters.sinopac import SinopacAdapter
            adapters.append(SinopacAdapter(load_sinopac_creds()))
        else:
            print("[run] 永豐:.env 未填 SINOPAC_API_KEY,本次跳過"
                  "(要永久停用可設 SINOPAC_ENABLED=0)")
    if schwab_on:
        from adapters.schwab import SchwabAdapter
        adapters.append(SchwabAdapter({
            "app_key": env.get("SCHWAB_APP_KEY", ""),
            "app_secret": env.get("SCHWAB_APP_SECRET", ""),
            "callback_url": env.get("SCHWAB_CALLBACK_URL",
                                    "https://127.0.0.1:8182"),
            "token_path": str(Path(__file__).parent /
                              env.get("SCHWAB_TOKEN_PATH",
                                      "schwab_token.json")),
        }))
    if ibkr_on:
        from adapters.ibkr import IbkrAdapter
        adapters.append(IbkrAdapter({
            "gateway_url": env.get("IBKR_GATEWAY_URL",
                                   "https://localhost:5000"),
        }))

    if not adapters:
        raise SystemExit("沒有任何券商被啟用:請檢查 .env 的 SINOPAC_ENABLED / "
                         "SCHWAB_ENABLED / IBKR_ENABLED,或用 --mock 試跑。")
    return adapters


def _fx_rates_today(db: DB, ccys: set[str]) -> dict[str, Decimal]:
    """今天各幣別 → TWD 匯率(用既有 FXService:fx_cache + 10 天遞補)。"""
    from core.prices import FXService
    fx = FXService(db, BASE_CCY)
    today = date.today()
    start = (today - timedelta(days=14)).isoformat()
    end = (today + timedelta(days=1)).isoformat()

    rates: dict[str, Decimal] = {BASE_CCY: Decimal(1)}
    for ccy in sorted(c for c in ccys if c != BASE_CCY):
        fx.ensure_range(ccy, start, end)
        try:
            r = Decimal(str(fx.rate_on(ccy, today.isoformat())))
        except LookupError:
            raise SystemExit(
                f"取不到 {ccy}/TWD 匯率(近 10 天皆無)。\n"
                "首次同步外幣部位需要網路抓匯率(之後會走本地 fx_cache);\n"
                "請確認網路與 yfinance(pip install yfinance)後重試。")
        rates[ccy] = r
        print(f"[fx] {ccy}/TWD = {r}")
    return rates


def _merge_anchor(positions: list) -> list:
    """快照錨點合併(P4):snapshot_positions 主鍵是 (ts, symbol),
    同一檔出現在多家券商時,股數相加、成本加權平均,避免互相覆蓋。
    回補引擎的 holdings 本來就以 symbol 為鍵,合併後語義完全一致。
    """
    class _A:                      # save_snapshot_positions 只取這五個屬性
        __slots__ = ("symbol", "qty", "avg_cost", "ccy", "last_price")

        def __init__(self, p):
            self.symbol, self.qty = p.symbol, p.qty
            self.avg_cost, self.ccy = p.avg_cost, p.ccy
            self.last_price = p.last_price

    agg: dict[str, _A] = {}
    for p in positions:
        a = agg.get(p.symbol)
        if a is None:
            agg[p.symbol] = _A(p)
            continue
        if a.ccy != p.ccy:
            print(f"[warn] {p.symbol} 在不同券商幣別不一致"
                  f"({a.ccy} vs {p.ccy}),錨點以先出現者為準")
            continue
        total_cost = a.qty * a.avg_cost + p.qty * p.avg_cost
        a.qty += p.qty
        a.avg_cost = (total_cost / a.qty) if a.qty else Decimal(0)
        a.last_price = p.last_price
    return list(agg.values())


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

    adapters = make_adapters(args.mock)
    print(f"[run] 本次同步券商:{', '.join(a.name for a in adapters)}")

    try:
        # 1) 逐家抓資料(單家失敗不中斷其他家;失敗清單供後續決策)
        from adapters.registry import sync_all
        positions, cash_by_ccy, realized, txns, errors = sync_all(adapters)

        # 1.5) 累積今日成交與已實現損益(成功的券商照常入庫,append-only 無害)
        if txns:
            n = db.insert_transactions(txns)
            if n:
                print(f"[txn] 新增 {n} 筆交易紀錄")
        if realized:
            n = db.insert_realized(realized)
            if n:
                print(f"[realized] 新增 {n} 筆已實現損益")

        # 2) 更新成功券商的目前持倉(replace_positions 只動該券商的列)
        for broker in sorted({p.broker for p in positions}):
            db.replace_positions(
                broker, [p for p in positions if p.broker == broker])

        # 2.5) P4 防呆:任一啟用券商失敗 → 不寫快照、不回補。
        #      快照是回補錨點,少一家會讓淨值曲線出現假跳水。
        if errors:
            print("\n" + "!" * 62)
            print("  本次有券商同步失敗,已略過「寫入快照與回補」以保護淨值曲線:")
            for e in errors:
                print(f"   - {e}")
            print("  成功券商的持倉已更新;排除問題後重跑 python run.py 即可。")
            print("!" * 62)
            return

        # 2.6) 提醒:資料庫裡有「本次未同步」券商的舊持倉(例如剛停用某家)
        synced = {a.name for a in adapters}
        stale = sorted({r["broker"] for r in db.all_positions()} - synced)
        if stale:
            print(f"[warn] positions_current 仍有未同步券商的舊資料:{stale};"
                  "若已不再使用該券商,快照將不含它(舊明細仍會顯示在儀表板)")

        # 3) 彙總(P4:全部折算 TWD 再合計;原幣值仍存於 positions_current)
        ccys = {p.ccy for p in positions} | set(cash_by_ccy)
        rates = _fx_rates_today(db, ccys)

        def to_twd(amount: Decimal, ccy: str) -> Decimal:
            return amount * rates[ccy]

        invested = sum((to_twd(p.market_value, p.ccy) for p in positions),
                       Decimal(0))
        cost = sum((to_twd(p.cost_value, p.ccy) for p in positions),
                   Decimal(0))
        cash_total = sum((to_twd(a, c) for c, a in cash_by_ccy.items()),
                         Decimal(0))
        net_worth = invested + cash_total
        unrealized = invested - cost

        breakdown = {
            "by_broker": _group(positions, lambda p: p.broker, to_twd),
            "by_asset_class": _group(positions, lambda p: p.asset_class, to_twd),
            "by_ccy": _group(positions, lambda p: p.ccy, to_twd),
            "cash": {k: float(v) for k, v in cash_by_ccy.items()},
            "fx": {k: float(v) for k, v in rates.items() if k != BASE_CCY},
        }

        # 4) 寫入快照(TWD 合計)與回補錨點(symbol 合併)
        ts = db.save_snapshot(
            base_ccy=BASE_CCY,
            net_worth=float(net_worth), invested=float(invested),
            cash=float(cash_total), cost=float(cost),
            breakdown=breakdown,
        )
        db.save_snapshot_positions(ts, _merge_anchor(positions))

        # 5) 回補快照之間的每日淨值(FXService 會逐日乘當日匯率)
        from core.backfill import run_backfill
        from core.prices import FXService, PriceService
        stats = run_backfill(db, PriceService(db), FXService(db, BASE_CCY),
                             BASE_CCY)
        if stats.get("note"):
            print(f"[backfill] {stats['note']}")
        else:
            print(f"[backfill] 已回補 {stats['segments']} 個區間、"
                  f"寫入 {stats['days_written']} 天的每日淨值")

        # 6) 刷新持倉的股息快取(供儀表板「股息現金流」分頁)
        try:
            from core.dividends import DividendService
            div = DividendService(db)
            n_div = sum(div.refresh(p.symbol, p.ccy) for p in positions)
            if n_div:
                print(f"[div] 更新 {n_div} 筆除息事件")
        except Exception as e:   # 沒網路/沒裝 yfinance 都不影響主流程
            print(f"[div] 股息更新略過:{e}")

        # 7) 終端機摘要(不開儀表板也看得到重點)
        _print_summary(ts, positions, net_worth, invested, cash_total,
                       unrealized, cost, args.db)

        # 8) P4 強化:同步成功後自動備份(失敗不影響主流程)
        try:
            from tools.backup import auto_backup
            auto_backup(Path(args.db))
        except Exception as e:
            print(f"[backup] 備份略過:{e}")

    finally:
        db.close()   # 各券商已在 sync_all 內 finally 登出


def _group(positions, key, to_twd) -> dict:
    """各分組的 TWD 市值合計(P4:原幣先折算,跨幣別才能相加)。"""
    out: dict[str, float] = {}
    for p in positions:
        out[key(p)] = out.get(key(p), 0.0) + float(to_twd(p.market_value, p.ccy))
    return out


def _print_summary(ts, positions, net_worth, invested, cash_total,
                   unrealized, cost, db_path) -> None:
    w = 78
    pct = (unrealized / cost * 100) if cost else Decimal(0)
    print("\n" + "=" * w)
    print(f"  同步完成  {ts}")
    print("=" * w)
    print(f"  總資產淨值      NT$ {net_worth:>14,.0f}")
    print(f"  投資市值        NT$ {invested:>14,.0f}")
    print(f"  現金            NT$ {cash_total:>14,.0f}")
    print(f"  未實現損益      NT$ {unrealized:>+14,.0f}  ({pct:+.1f}%)")
    print("-" * w)
    print(f"  {'券商':<9}{'代號':<7}{'名稱':<12}{'股數':>9}{'均價':>10}"
          f"{'現價':>10}{'市值(原幣)':>15}")
    for p in sorted(positions, key=lambda x: -x.market_value):
        print(f"  {p.broker:<9}{p.symbol:<7}{p.name[:10]:<12}{p.qty:>9,.0f}"
              f"{p.avg_cost:>10,.1f}{p.last_price:>10,.1f}"
              f"{p.ccy + format(p.market_value, ',.0f'):>15}")
    print("=" * w)
    print(f"  已寫入 {db_path}(快照合計為 TWD;明細市值為原幣)")
    print("  開啟儀表板:streamlit run viewer/app.py\n")


if __name__ == "__main__":
    main()
