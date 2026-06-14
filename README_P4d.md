# P4d:多券商對帳單匯入 + 合併資產 + 每日匯率

這批檔案讓你的資產整合系統支援 **永豐(台股)+ 嘉信 + 盈透(美股)** 三家券商,
解決三件事:

1. **匯入永豐對帳單(sinopac.csv)並重建** —— 與嘉信 / 盈透同一套回放引擎。
2. **主程式 `run.py` 同時看到台股 + 美股** —— 跨券商合併、USD→TWD 換算後存快照。
3. **美股帳戶起始日起,每天記錄 USD/TWD 匯率** —— 資產曲線能反映匯率變動。

---

## 一、檔案清單

放進你的專案(`my-portfolio/`):

```
core/
  statements.py     # 三家對帳單解析 + 回放引擎(平均成本法、拆股、In-Kind 轉撥)
  importer.py       # 回放結果 → 持倉/交易/現金;順向逐日估值(產生每日淨值曲線)
  aggregate.py      # 跨券商合併(FX 換算)、合併快照、broker_cash 表
  fxrate.py         # 每日 USD/TWD 匯率回補(逐日一筆,非交易日前向填補)
  market.py         # yfinance:現價、拆股、原始收盤(auto_adjust=False)快取
import_statements.py  # CLI:匯入單一券商對帳單
rebuild_history.py    # CLI:一鍵重建合併每日淨值曲線(含每日匯率)
run_py_patch.md       # run.py 要改的「那一個區塊」(需求 2)
```

> `core/db.py`、`core/models.py` **沿用你現有的**,不需取代。
> `aggregate.py` 會用 `CREATE TABLE IF NOT EXISTS` 自動建一張 `broker_cash` 小表,
> 不更動既有 schema。

---

## 二、三步上手

```bash
# 1) 匯入美股對帳單(會自動抓拆股與現價、回補匯率;需網路)
python import_statements.py schwab.csv
python import_statements.py U5529822_TRANSACTIONS.csv      # 盈透

# 2)(可選)也把永豐對帳單匯入做核對;平常永豐走 run.py 即時同步
python import_statements.py sinopac.csv

# 3) 一鍵重建:每日匯率 + 跨券商每日淨值曲線 + 今日合併快照
python rebuild_history.py

streamlit run viewer/app.py    # 看儀表板
```

之後日常只要 `python run.py`(同步永豐),依 `run_py_patch.md` 改過後,
總資產就會自動把美股一起換算進來。

---

## 三、重要設計與正確性

- **回放已驗證**:嘉信 QLD 2,300 / SGOV 940 / IBIT 200 / MSFT 20、現金 8.46;
  盈透 QLD 799.3771 / IBKR 8.4249、現金 29.27;永豐 0050 13,159。皆與對帳單相符。
- **拆股兩條路**:嘉信對帳單「自帶」Stock Split 列(新增股數)→ 直接採用;
  盈透 / 永豐對帳單「沒有」拆股列 → 用 yfinance 自動補,離線可用
  `--split 代號:YYYY-MM-DD:倍數` 手動指定(例:`--split QLD:2025-11-19:2`)。
- **同代號跨券商合併**:QLD 同時在嘉信與盈透,合併快照的 `snapshot_positions`
  會合成一筆(股數相加、加權平均成本),避免主鍵(ts+symbol)互相覆蓋。
- **帳戶間轉撥不重複計入**:QLD 由嘉信轉到盈透(Security Transfer / In-Kind)
  在組合層級互相抵銷,故不計入現金流、也不影響曲線。
- **現金採計**:嘉信 / 盈透對帳單有現金結餘 → 採計;
  永豐對帳單「無出入金資料」→ 不採計(永豐現金由 `run.py` 的 Shioaji 即時餘額提供)。
- **曲線為何準(需求 3)**:逐日估值用「**當日股數 × 當日原始收盤價 × 當日匯率**」。
  當日股數與當日原始價同處「當期基礎」,所以跨拆股也正確,毋須做價格還原;
  匯率逐日一筆,美股的台幣表現因此反映匯率變動。
- **不蓋真實點**:今天寫入一筆「真實合併快照」(`is_real=1`);過去由逐日估值
  填補(`is_real=0`),真實快照日永不被估值覆蓋(沿用你既有 `upsert_daily_estimates` 規則)。

---

## 四、生產環境注意事項

- **需要網路 + yfinance** 來抓:拆股、現價、USD/TWD 歷史匯率。
  `pip install yfinance`。沒裝 / 沒網路時,程式不會崩,會印提示並:
  - 拆股 → 請用 `--split` 手動補;
  - 現價 → 以均價遞補(市值=成本,僅影響顯示,不影響股數/曲線結構);
  - 匯率 → `rebuild_history.py` 可用 `--usd-rate 32.5` 手動指定整段匯率離線測試。
- **離線完整重跑範例**:
  ```bash
  python rebuild_history.py sinopac.csv schwab.csv U5529822_TRANSACTIONS.csv \
      --no-prices --usd-rate 32.5 --split QLD:2025-11-19:2
  ```
- **盈透檔名**:`rebuild_history.py` 不帶參數時會自動尋找
  `sinopac.csv` / `schwab.csv` / `*TRANSACTIONS*.csv`;檔名不同請在參數明確指定。
- **冪等**:重複匯入安全 —— `positions_current` 以券商為單位整批覆蓋,
  `transactions` 以 `INSERT OR IGNORE` 去重。

---

## 五、CLI 參數速查

`import_statements.py FILE`
  `--broker auto|sinopac|schwab|ibkr`、`--db`、`--split 代號:日期:倍數`(可重複)、
  `--no-prices`、`--no-fx`

`rebuild_history.py [FILE ...]`
  `--db`、`--split`、`--no-prices`、`--no-fx`、`--usd-rate 匯率`、`--base TWD`
