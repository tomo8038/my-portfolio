# 修改紀錄 — 2026-06-14

本次共修正 **3 件事**:① 持倉類別/產業判讀錯誤、② 2025/11/18→11/19 淨值假跳階、
③ 執行 `merge_ibkr_into_portfolio.py` 崩潰。以下為根因、改動檔案與完整用法。

---

## 變更檔案一覽

| 檔案 | 路徑 | 性質 | 對應問題 |
|---|---|---|---|
| `fix_positions.py` | `my-portfolio/fix_positions.py` | 新增(單檔自足) | ① |
| `classify.py` | `my-portfolio/core/classify.py` | 新增(選用,可不放) | ① |
| `build_history.py` | `my-portfolio/build_history.py` | 覆蓋 | ② |
| `build_history_ibkr.py` | `my-portfolio/build_history_ibkr.py` | 覆蓋 | ② |
| `merge_into_portfolio.py` | `my-portfolio/merge_into_portfolio.py` | 覆蓋 | ② |
| `merge_ibkr_into_portfolio.py` | `my-portfolio/merge_ibkr_into_portfolio.py` | 覆蓋 | ③ |
| `rebuild_all.py` | `my-portfolio/rebuild_all.py` | 新增/更新(一鍵從零) | ④⑤ |
| `refresh_snapshot.py` | `my-portfolio/refresh_snapshot.py` | 新增 | ⑤ |

> 直接覆蓋同名檔即可,毋須手動編輯。`classify.py` 放在 `core/` 底下。

---

## ① 類別/產業判讀錯誤

**現象**:0050、QLD 被標成「個股(equity)」、產業全是「其他」。

**根因**
- 嘉信 `build_history.py`:`asset_class()` 用寫死的 ETF 白名單,名單外的 ETF 一律掉成
  equity;`industry` 寫死空字串。
- 盈透 `ibkr.db` 的 `positions_current` 沒有 asset_class/name/industry 欄;併入時也沒補,
  IBKR 每檔類別/產業皆空。
- 美股完全沒有產業別資料來源。

**修法**
- 新增 `core/classify.py`:跨券商共用分類器。美股 ETF 白名單 + 名稱關鍵字
  (SGOV 這種「債券型 ETF」也正確判成 etf)、台股 00 規則、美股產業對照
  (MSFT→軟體、IBKR→金融…),可選 `--yfinance` 補 sector。
- 新增 `fix_positions.py`(**單檔自足、零相依**,分類規則已內嵌):在「合併之後」直接
  正規化 `portfolio.db` 的 `asset_class`/`industry`(順便補 IBKR 漏掉的空 `ccy`),一次
  修好三家券商、冪等可重跑。放在 `my-portfolio/` 根目錄即可,**不需要 `core/classify.py`**。
  (`core/classify.py` 仍附上作為共用模組,但 `fix_positions.py` 已不依賴它。)

**驗證**:0050/QLD/SGOV/IBIT→etf‧ETF;MSFT→equity‧軟體;IBKR→equity‧金融;
公債 CUSIP→bond‧債券。重跑第二次更新 0 筆(冪等)。

---

## ② 2025/11/18→11/19 資產暴增(QLD 2:1 分割)

**現象**:單日淨值跳增約 NT$3.75M。

**根因**:分割的「股數加倍日」與 yfinance「價格減半日(ex-date)」對不齊。
- 兩支 build 都用 `auto_adjust=False`,故 `Close` 是未還原的原始成交價,只在真正
  ex-date 那天減半。
- 嘉信 `parse_schwab_csv.py` 取 CSV 的「as of 11/19」→ 股數在 11/19 加倍;
  盈透 `parse_ibkr_csv.py` 注入的 `SPLITS` 在 11/20 加倍。兩邊各記各的,
  頂多一邊對得上 yfinance 的 ex-date。
- 對不齊的那天 = 「分割後股數 × 分割前股價」,憑空多出約「新增股數市值」
  ≈ 1,772 × $67.6 ≈ NT$3.75M,正是該跳幅。
- 另:`build_history.py` 註解宣稱有 `_unadjust_splits` 還原步驟,實際並不存在。

**修法**:三支檔新增 `align_split_dates()` —— 把分割的股數加倍日直接挪到
yfinance 回報的真實 ex-date(`actions=True` 的 `Stock Splits` 欄),讓股數加倍與
價格減半永遠同一天,分割保證淨值中性。離線(抓不到 yfinance 分割)→ 保持原日期、不退步。
因 `merge_into_portfolio.py` 會自行重算嘉信曲線,故它也一併修改,否則修正不會生效。

**驗證**(離線 stub):修正前 11/18→11/19 = **+239,220 假跳階**;對齊後 = **+0**(中性)。
執行時若印出 `[split] QLD 分割日對齊 yfinance:2025-11-19 → 2025-11-20` 即代表生效。

**補充修正(2026-06-14 二修)**:首版把分割對齊到「yfinance 事件標記日」,假設 Close 是
未調整原始價。實測後發現 yfinance `auto_adjust=False` 的 **Close 其實是「已還原分割」的連續價**
(Yahoo 的 Close 欄本來就調分割、只有股息沒調),所以分割前的日子也會拿到「分割後的價」。
於是真正的症狀是:**分割前市值被低估一半**(分割前股數 1772 × 已調分割價 ≈ 只有一半),
而不是尖峰。

**最終修法(2026-06-14 三修)**:`fetch_prices` 會自動判斷每檔分割屬於哪種價:
* **連續價(已調分割,yfinance 常態)**:收盤序列在分割附近沒有掉 ratio 倍 → 估值時把
  **分割前的股數換算到分割後基準(×ratio)**,價格照用 → 分割前後市值連續、不再少一半。
* **原始價(會在 ex-date 掉一半,少見)**:序列偵測得到掉價日 → 把股數加倍對齊到掉價日、
  價格不調整 → 一樣連續。

兩條路都用 stub 驗過:連續價情境分割前不再低估、原始價情境不尖峰,全期間最大單日變化皆 0
(分割中性)。嘉信、盈透兩套 `daily_networth` 都已套用。
## ③ `merge_ibkr_into_portfolio.py` 執行崩潰

**現象**:`sqlite3.OperationalError: no such column: networth`。

**根因**(皆為 `merge_ibkr_into_portfolio.py` 既有欄位不相符,被「先嘉信、後 IBKR」順序觸發)
1. `_DN_VALUE = "networth"`:portfolio.db 的 `daily_networth` 欄位其實是
   `net_worth`(底線)。
2. 備份表 `stock_daily_backup`:與嘉信版「同名但欄位不同」而衝突——嘉信先建立了
   `(date,base_ccy,net_worth,is_real)`,IBKR 版卻去讀 `networth`。
3. `positions_current` / `realized_pnl` 直接套用 `ibkr.db` 的欄位(含 portfolio.db
   沒有的 `cost_basis`)→ 後續還會再炸。

**修法**
- `_DN_VALUE` 改為 `net_worth`。
- IBKR 專用基準備份改名 `ibkr_base_backup`,與嘉信版徹底分開、互不干擾。
- `positions_current` / `realized_pnl` / 早期日 `daily_networth` 插入改成
  「依目標 schema 自動對應欄位」(`_insert_adaptive`),只寫 portfolio.db 實際存在的欄位;
  並從 `ibkr.db` 的 `price_cache` 補上 IBKR 現價/市值/未實現損益,讓儀表板直接顯示。

**驗證**(合成 DB,含嘉信舊備份衝突情境):不再崩潰;`daily_networth = 基準 +
IBKR×當日匯率`(數字逐日吻合);重跑冪等;`--restore` 乾淨還原;IBKR 持倉帶現價市值。

> 你目前的 `portfolio.db` 仍是「台股+嘉信」狀態(IBKR 那次崩潰前未寫入任何資料),
> 直接覆蓋本檔後重跑即可,**不需要 reset 或還原**。

---

## ④ 一鍵從零重建(解決「步驟記不起來 / 漏建庫」)

**現象**:刪掉 `portfolio.db` 後只跑嘉信 merge → `no such table: fx_cache`,合併中止。

**根因**:`merge_*` 預設 `portfolio.db` 已存在(含 `fx_cache` 表)。正常情況下這張表
與台股資料、匯率是由 `run.py` 先建立的;少了那步,空庫裡沒有 `fx_cache`,嘉信 merge
要折算 TWD 時就找不到表而中止。

**修法**:新增 `rebuild_all.py` 總指揮,把固定順序包成**一個指令**,且預設「真正從零」
(先刪舊 `portfolio.db`/`ibkr.db`)——這也順帶避開合併備份表殘留造成的疊加/尖峰。

---

## ⑤ 6/13–6/14 淨值驟降、且總資產淨值與當日淨值對不上

**現象**:每日淨值在 6/13、6/14 突然從約 1,200 萬掉到 2–3 百萬;KPI「總資產淨值」
(只剩台股 ~136 萬)又和當日 daily_networth 不吻合。

**根因(同一個源頭)**:嘉信 `end_date = events[-1].date` = 最後一筆交易日(2026-06-12),
沒有延伸到今天。
- 每日曲線:嘉信只算到 6/12 → 6/13、6/14 缺整塊嘉信(~1,100 萬)→ 驟降。
- 今天的快照:嘉信 merge 會去找「日期 = 6/12」的快照來併入,但 `run.py` 寫的是「今天
  6/14」的快照 → 對不上 → 沒把嘉信加進 KPI(且 IBKR merge 本來就沒更新快照)。

**修法**
- **修A**(`merge_into_portfolio.py`、`build_history.py`):把嘉信每日序列**延伸到今天**
  ——最後一筆交易之後持股不變、逐日以收盤重估。6/13、6/14 補回嘉信,曲線不再驟降;
  快照日期也對上今天。
- **修B**(新增 `refresh_snapshot.py`):所有 merge 完成後,直接從 `positions_current`
  (三家)+ broker_cash + 當日匯率**重算今天的合併快照**(同步寫當日 daily_networth,
  is_real=1),確保「總資產淨值 = 當日淨值 = 台股+嘉信+盈透」。已併入 `rebuild_all.py`
  的最後一步,自動執行。

**驗證**(合成 DB):`refresh_snapshot` 重算後 net_worth = 台股 + 美股×匯率 + 現金,
by_broker 三家齊全,且 `daily[今天] == snapshot.net_worth`(is_real=1)。

---

# 使用方法(每次都一樣,一個指令)

```bash
cd my-portfolio
python rebuild_all.py
```

它會依序自動做完:**建庫 + 台股(run.py)→ 嘉信 → 盈透 → 校正分類**,然後你只要:

```bash
streamlit run viewer/app.py
```

選項(視情況):

```bash
python rebuild_all.py --mock-taiwan   # 台股用假資料(免永豐憑證,純測流程)
python rebuild_all.py --skip-taiwan   # 完全跳過台股,只建美股(免憑證)
python rebuild_all.py --keep          # 不刪舊檔(沿用既有 portfolio.db)
# 找不到 CSV 時可手動指定:
python rebuild_all.py --schwab-csv schwab.csv --ibkr-csv U5529822_TRANSACTIONS.csv
```

說明:
- 台股那步用 `run.py`(需 `.env` 永豐憑證)。若失敗,腳本會**自動降級**只建空庫表結構、
  繼續完成美股(US-only),並提示你補憑證或加 `--mock-taiwan`。
- 嘉信 merge 會**自行抓六年 USD/TWD 匯率**,不需另外處理。
- 跑 merge 時若看到 `[split] QLD 價格實際下跌日 … → 以下跌日對齊`,代表 ② 的分割修正生效,
  11/20 附近的尖峰會消失。

> 為什麼「從零」最省事:全部刪掉重建時,合併用的備份表(`stock_daily_backup` /
> `ibkr_base_backup`)也一起清空、重新擷取,天然不會有舊資料殘留或重複疊加。
