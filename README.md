# IBKR CSV 回放工具組(P4c)

把盈透 IBKR 下載的 **Transaction History CSV** 回放重建成資產歷史,
與嘉信 P4b 同結構、同流程。所有金額為 **USD 原幣**,併入 `portfolio.db` 時才折 TWD。

## 檔案

| 檔案 | 角色 | 對應嘉信版 |
|---|---|---|
| `parse_ibkr_csv.py` | CSV → 統一事件流(9 種 Action + 分割注入) | `parse_schwab_csv.py` |
| `build_history_ibkr.py` | 正向重播 → 抓價 → 每日淨值 → 寫 `ibkr.db` | `build_history.py` |
| `verify_ibkr.py` | 4 項守恆驗證 + PIL 閉環 | `verify.py` |
| `view_ibkr.py` | 獨立 Streamlit 檢視器(看 `ibkr.db`) | `view_schwab.py` |
| `merge_ibkr_into_portfolio.py` | 安全併入 `portfolio.db`(冪等 / 可還原) | `merge_into_portfolio.py` |

---

## A. 只想「單獨檢查 IBKR 資料正確性」(不碰 portfolio.db)← 你要的

完全不需要動 `portfolio.db`。IBKR 資料是寫進**獨立的 `ibkr.db`**,跟主庫互不干擾:

```bash
pip install -r requirements.txt

# 1) 重建(有網路會抓真實收盤;沒網路自動成本遞補)
#    預設把每日淨值補到「今天」:最後一筆交易(2026/4/1)之後持股不變、逐日以收盤重估
python build_history_ibkr.py U5529822_TRANSACTIONS.csv ibkr.db
#    要指定補到哪一天:--as-of 2026-06-13

# 2) 守恆驗證(4 項全綠才算對)
python verify_ibkr.py ibkr.db U5529822_TRANSACTIONS.csv

# 3) 開檢視器看持倉 / 已實現 / 每日淨值
streamlit run view_ibkr.py -- ibkr.db
```

要重檢查只要刪掉 `ibkr.db` 重跑即可:`rm ibkr.db && python build_history_ibkr.py ...`。
**這條路完全不影響 portfolio.db。**

---

## B. 確認無誤後,才併入 portfolio.db

```bash
cp portfolio.db portfolio.db.bak               # 先備份(務必)
python merge_ibkr_into_portfolio.py --dry-run ibkr.db portfolio.db   # 試算
python merge_ibkr_into_portfolio.py           ibkr.db portfolio.db   # 正式併入
streamlit run viewer/app.py                    # 看三券商(台股+嘉信+IBKR)
```

- **冪等**:重跑併入不疊加。
- **可還原**:`python merge_ibkr_into_portfolio.py --restore portfolio.db`
  會移除所有 IBKR 列、daily_networth 回到併入前。

---

## C. 如何「重置」portfolio.db

依你的目的選一種,**由輕到重**:

1. **只想移除 IBKR**(最常見,推薦):
   ```bash
   python merge_ibkr_into_portfolio.py --restore portfolio.db
   ```
   不影響台股/嘉信,只把 IBKR 拆掉、淨值還原。

2. **回到併入前的整個狀態**:用併入前的備份覆蓋回去
   ```bash
   cp portfolio.db.bak portfolio.db
   ```

3. **完全清空、重建空庫**(連台股/嘉信都不要了):
   ```bash
   mv portfolio.db portfolio.db.old     # 保險起見先改名,不要直接刪
   python run.py                        # 主程式會自動建立全新的空 portfolio.db
   ```
   `run.py` 偵測不到 `portfolio.db` 會重新建表;下次同步/回補即重新累積。

> ⚠ 重置/還原前一律先 `cp portfolio.db portfolio.db.bak`。SQLite 是單檔,
> 備份就是複製這一個檔案,零風險。

---

## 注意事項

- **沙箱交付版 `ibkr.db` 市值為「成本遞補」**(沙箱連不到 Yahoo Finance)。
  在你本機跑 `build_history_ibkr.py`(已 `pip install yfinance`)即補上真實收盤價,
  帳務數字本來就精確,只有每日「市值」會從成本變成市價。
- **QLD 2:1 分割**(2025/11/20 生效)CSV 不含此列,已在 `parse_ibkr_csv.py`
  的 `SPLITS` 內注入。未來若有新分割/更名,改 `SPLITS` / `RENAMES` 兩個常數即可。
- **`merge_ibkr_into_portfolio.py`** 依「專案進度總結」記載的 portfolio.db 欄位撰寫;
  首次正式併入前用 `--dry-run` 對一次,若欄位命名與你的嘉信 merge 略有不同,
  對齊檔頭的 `_DN_*` / `_FX_*` 常數即可。
