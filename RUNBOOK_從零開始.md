# 從零開始:乾淨重建完整步驟(永豐 + 嘉信 + 盈透)

只保留「最新版程式碼 + 三家券商歷史交易 CSV」,把所有資料庫刪掉,重新建立。
從零匯入時美股一律正確標成 USD,先前那些髒資料(幣別錯、舊 TW-only 快照)都不會再出現。

---

## 第 0 步:確認檔案就定位

把這次交付的檔案放到你的專案 `my-portfolio/`,**覆蓋同名舊檔**:

| 檔案 | 覆蓋路徑 |
|---|---|
| `core/statements.py`   | `my-portfolio/core/statements.py` |
| `core/importer.py`     | `my-portfolio/core/importer.py` |
| `core/aggregate.py`    | `my-portfolio/core/aggregate.py` |
| `core/fxrate.py`       | `my-portfolio/core/fxrate.py` |
| `core/market.py`       | `my-portfolio/core/market.py` |
| `import_statements.py` | `my-portfolio/import_statements.py` |
| `rebuild_history.py`   | `my-portfolio/rebuild_history.py` |
| `run.py`               | `my-portfolio/run.py`(**已含修補的完整版,直接覆蓋**) |
| `apply_viewer_patch.py`| `my-portfolio/apply_viewer_patch.py`(自動修補 viewer) |

三個 CSV 放在 `my-portfolio/`(檔名保持):
`sinopac.csv`、`schwab.csv`、`U5529822_TRANSACTIONS.csv`

> `core/db.py`、`core/models.py` **沿用你現有的**,不需覆蓋。
> `fix_us_currency.py` 從零建庫**用不到**(那是修髒資料用的),可不放。

---

## 第 1 步:刪掉所有資料庫(回到最原始狀態)

在 `my-portfolio/` 目錄:

**Windows(PowerShell)**
```powershell
Remove-Item portfolio.db, portfolio.db-wal, portfolio.db-shm -ErrorAction SilentlyContinue
Remove-Item schwab.db, IBKR.db -ErrorAction SilentlyContinue   # 舊工具產生的,一併清掉
```

**macOS / Linux**
```bash
rm -f portfolio.db portfolio.db-wal portfolio.db-shm
rm -f schwab.db IBKR.db          # 舊工具產生的,一併清掉
```

> 所有快取(價格 / 匯率 / 持倉 / 交易 / 快照)都在 `portfolio.db` 裡,刪掉它就全清空。

---

## 第 2 步:套用程式修補(不需手動改)

兩處都已備好,**不用自己改 .md**:

1. **`run.py`** — 直接用交付的版本覆蓋 `my-portfolio/run.py` 即可(已內含「跨券商合併快照」修補)。
2. **`viewer/app.py`** — 跑一次自動修補(會自動建 `.bak` 備份,可重複執行):
   ```bash
   python apply_viewer_patch.py
   ```
   它只替換「讀取資料」那一個區塊,其餘儀表板程式完全不動。

> 仍想看修改內容對照,可參考 `run_py_patch.md` / `viewer_patch.md`(純說明,非必需)。

---

## 第 3 步:一鍵建庫(匯入三家 + 回補每日匯率 + 重畫每日淨值曲線)

**有網路(建議,會自動抓拆股、現價、USD/TWD 歷史匯率):**
```bash
python rebuild_history.py
```
就這一行。它會自動找到 `sinopac.csv` / `schwab.csv` / `*TRANSACTIONS*.csv`,
依序匯入、回補匯率、重算今天的合併快照、重畫整段每日淨值曲線。

**離線(沒網路 / 沒裝 yfinance)備案:**
```bash
python rebuild_history.py --no-prices --usd-rate 32.5 --split QLD:2025-11-19:2
```
`--usd-rate` 給一個目前的 USD/TWD;`--split` 手動補盈透 QLD 在 2025-11-19 的 2:1 拆股
(有網路時 yfinance 會自動抓,不必加)。

---

## 第 4 步:開儀表板

```bash
streamlit run viewer/app.py
```
檢查重點:
- 總覽「總資產淨值」與「資產配置(券商維度)」三家加總應一致(都已換 TWD)。
- 每日淨值曲線從 2020-08-25(嘉信起始)連續到今天,結尾不再有突然回落。

---

## 第 5 步(日常使用):同步永豐

之後想更新台股即時持倉時:
```bash
python run.py                 # 連永豐;因已套 run_py_patch,總資產會含美股
streamlit run viewer/app.py
```
美股部位不會每天變動(它們來自對帳單);要更新美股,重新匯出對帳單後再:
```bash
python import_statements.py schwab.csv
python import_statements.py U5529822_TRANSACTIONS.csv
python rebuild_history.py
```

---

## 一頁速查(有網路、從零)

```bash
cd my-portfolio
rm -f portfolio.db portfolio.db-wal portfolio.db-shm schwab.db IBKR.db   # 清空
# (覆蓋交付的 run.py;viewer 跑一次自動修補)
python apply_viewer_patch.py          # 修補 viewer/app.py(建 .bak)
python rebuild_history.py            # 建庫
streamlit run viewer/app.py          # 看結果
```

## 需求對應(驗證點)
- **需求 1**(永豐匯入/重建):`rebuild_history.py` 會匯入 sinopac.csv 並回放重建。
- **需求 2**(run.py 同時看到台股+美股):套 `run_py_patch.md` 後,合併快照含三家。
- **需求 3**(每日 USD/TWD 匯率、曲線反映匯率):`fxrate.py` 從美股起始日逐日回補匯率,
  曲線用「當日股數 × 當日原始價 × 當日匯率」估值。
