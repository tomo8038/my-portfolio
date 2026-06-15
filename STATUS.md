# 專案現況與後續方向(2026-06-14)

多券商投資組合追蹤器:永豐(台股 TWD)+ 嘉信、盈透(美股 USD),
合併成單一 TWD 淨值曲線與每日 KPI。

---

## 一、已完成項目

### 資料管線(一鍵從零)
- `rebuild_all.py`:一個指令跑完「建庫 + 台股(run.py)→ 嘉信 → 盈透 → 校正分類 →
  重算快照」。預設真正從零(先刪舊檔),天然避開合併備份表殘留。
- 失敗自動降級:台股那步若無憑證/失敗,改建空庫續做美股(US-only)並提示。

### 五項修正(對應 CHANGELOG)
1. **分類/產業**:`fix_positions.py`(單檔自足)正規化三家的 asset_class / industry。
2. **股票分割估值**:正確處理「分割前後市值連續」。關鍵認知——yfinance
   `auto_adjust=False` 的 Close 是**已還原分割的連續價**,故:
   - 連續價(常態)→ 估值時把分割前股數換算到分割後基準(×ratio)。
   - 原始價(會在 ex-date 掉一半,少見)→ 偵測掉價日、股數對齊掉價日。
   兩條路皆驗證為分割中性(全期間最大單日變化 = 0)。
3. **IBKR 併入崩潰**:`merge_ibkr_into_portfolio.py` 改依目標 schema 自動對應欄位、
   獨立備份表 `ibkr_base_backup`,並補上 IBKR 現價/市值。
4. **建庫/匯率**:from-scratch 缺 `fx_cache` → `rebuild_all.py` 統一建庫;嘉信 merge
   會自行抓六年 USD/TWD。
5. **每日曲線與 KPI 一致**:
   - 嘉信每日序列**延伸到今天**(最後交易後持股不變、逐日收盤重估)→ 不再 6/13、6/14 驟降。
   - `refresh_snapshot.py` 在所有 merge 後從 positions_current 重算今天的合併快照 →
     「總資產淨值 = 當日淨值 = 台股+嘉信+盈透」一致。

### 分割處理覆蓋範圍(已盤點)
- 目前真實持股:Schwab(QLD/SGOV/MSFT/IBIT)、IBKR(QLD/IBKR)、台股(0050)。
- 跨分割持有者**只有 QLD**,已正確處理。NVDA 於 2021 分割前清倉、TQQQ/AAPL 於分割後才買,
  皆無影響。處理邏輯為通用,非寫死 QLD。

---

## 二、未來優化方向

### 正確性 / 穩健性
- **IBKR 分割自動化**:目前 IBKR 分割靠 `parse_ibkr_csv.py` 的 `SPLITS` 手動清單
  (僅 QLD)。可改成從 yfinance 已抓的 `actions` 自動補進,避免未來漏加;
  並加一道保險:若某檔在 yfinance 有分割、卻跨持有期且券商無對應事件 → 印警告。
- **資料完整性檢核**:自動比對「股息隱含持股 vs 交易可解釋持股」,抓出缺漏的轉撥
  (對應先前 IBKR ACATS 轉入沒有交易列的問題)。
- **回歸測試**:把目前一次性的 stub 驗證固化成 pytest(分割中性、合併冪等、
  快照=當日淨值…),改版時自動把關。
---

## 三、一鍵指令

```bash
cd my-portfolio
python rebuild_all.py
streamlit run viewer/app.py
```
