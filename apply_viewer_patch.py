"""自動修補 viewer/app.py — P4d(資產配置/持倉/損益金額換算 TWD)。

不需手動改檔。會在原檔旁建立 .bak 備份,然後把「讀取資料」那一個區塊替換成
依 fx_cache 最新匯率把市值/損益/成本換算成 TWD 的版本。可重複執行(冪等)。

用法:
  python apply_viewer_patch.py                 # 預設改 viewer/app.py
  python apply_viewer_patch.py path/to/app.py  # 指定路徑
"""
import sys
from pathlib import Path

MARKER = "# [P4d] 各幣別 → TWD 最新匯率"

OLD_LOAD = '''con.close()

if not pos.empty:
    pos["cost_value"] = pos["qty"] * pos["avg_cost"]
    pos["ret_pct"] = (pos["pnl"] / pos["cost_value"] * 100).where(
        pos["cost_value"] != 0, 0.0)
    pos["industry"] = pos["industry"].fillna("").replace("", "其他")'''

NEW_LOAD = '''# [P4d] 各幣別 → TWD 最新匯率(美股市值/損益換算用);TWD 自身為 1
fx_latest = {"TWD": 1.0}
for pair, rate in con.execute(
        "SELECT pair, rate FROM fx_cache f WHERE date = "
        "(SELECT MAX(date) FROM fx_cache WHERE pair = f.pair)"):
    if pair.endswith("TWD") and len(pair) >= 6:
        fx_latest[pair[:-3]] = rate          # 'USDTWD' -> 'USD'
con.close()

if not pos.empty:
    # market_value_native / unrealized_pnl_native 是「原幣」(美股為 USD)。
    # 統一換算成 TWD,讓配置圖、持倉明細、損益排行的金額正確。
    pos["fx"] = pos["ccy"].map(fx_latest).fillna(1.0)
    pos["mv"] = pos["mv"] * pos["fx"]
    pos["pnl"] = pos["pnl"] * pos["fx"]
    pos["cost_value"] = pos["qty"] * pos["avg_cost"] * pos["fx"]
    pos["ret_pct"] = (pos["pnl"] / pos["cost_value"] * 100).where(
        pos["cost_value"] != 0, 0.0)
    pos["industry"] = pos["industry"].fillna("").replace("", "其他")'''

# 即時模式(選用):把市值換算也帶上 fx
OLD_LIVE = '''                l_inv += r["qty"] * px
                l_cost += r["qty"] * r["avg_cost"]'''
NEW_LIVE = '''                l_inv += r["qty"] * px * r["fx"]
                l_cost += r["qty"] * r["avg_cost"] * r["fx"]'''


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("viewer/app.py")
    if not path.exists():
        raise SystemExit(f"找不到 {path}。請在專案根目錄執行,或指定路徑:"
                         f"python apply_viewer_patch.py viewer/app.py")

    src = path.read_text(encoding="utf-8")

    if MARKER in src:
        print(f"[skip] {path} 已套用過 P4d 匯率換算(偵測到標記),不重複修改。")
        return

    if OLD_LOAD not in src:
        raise SystemExit(
            f"[error] 在 {path} 找不到預期的『讀取資料』區塊,可能版本不同。\n"
            f"請改用 viewer_patch.md 手動比對,或把你的 viewer/app.py 提供出來。")

    # 備份
    bak = path.with_suffix(path.suffix + ".bak")
    bak.write_text(src, encoding="utf-8")

    out = src.replace(OLD_LOAD, NEW_LOAD)
    live_done = False
    if OLD_LIVE in out:
        out = out.replace(OLD_LIVE, NEW_LIVE)
        live_done = True

    path.write_text(out, encoding="utf-8")
    print(f"[ok] 已修補 {path}(備份:{bak.name})")
    print(f"     · 資產配置 / 持倉明細 / 損益排行 金額已改為依 fx_cache 換算 TWD")
    print(f"     · 即時模式換算:{'已套用' if live_done else '略過(未找到該段,可能你的版本不同)'}")
    print(f"\n提醒:請先 `python rebuild_history.py` 讓 fx_cache 有 USD/TWD 資料,再開儀表板。")


if __name__ == "__main__":
    main()
