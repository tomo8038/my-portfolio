# 我的資產整合 — P0:永豐當下總資產

開一次程式,看到永豐帳戶的總資產淨值、持倉明細與未實現損益。
純 Python + SQLite 單檔,**不需要 Docker、不需要資料庫伺服器**。

---

## 一、安裝(只做一次)

需要 Python 3.10 以上(建議 3.11/3.12)。

```bash
pip install -r requirements.txt
```

## 二、先用模擬資料驗證流程(免帳號)

```bash
python run.py --mock          # 用假資料跑通:同步 → 寫入 SQLite
streamlit run viewer/app.py   # 瀏覽器自動開啟儀表板
```

看到「總資產淨值 NT$ 2,236,000」就代表整條管線是通的。

## 三、接上真實永豐帳戶

### 3.1 前置(在永豐官網完成,只做一次)

1. **開立永豐金證券帳戶**(已有可跳過)。
2. **簽署 API 使用條款**:到永豐「簽署中心」逐條閱讀並簽署(證券與期貨分開,先簽證券即可)。
3. **申請 API Key / Secret Key**:在永豐金證券理財網的 API 專區建立金鑰。
   - 申請時權限選「**帳務查詢**」即可,P0 不需要「交易」權限——唯讀最安全。
4. **下載電子憑證(CA)**:正式環境查帳務必須。下載 `.pfx` 憑證檔放到本機固定路徑,記下憑證密碼。

### 3.2 填入憑證

```bash
# 複製範本並編輯
cp .env.template .env
```

打開 `.env` 填入:

```
SINOPAC_API_KEY=你的APIKey
SINOPAC_SECRET_KEY=你的SecretKey
SINOPAC_CA_PATH=C:/sinopac/Sinopac.pfx     # 憑證檔實際路徑
SINOPAC_CA_PASSWD=憑證密碼
SINOPAC_PERSON_ID=身分證字號
SINOPAC_SIMULATION=0
```

> `.env`、`*.db`、`*.pfx` 都已列在 `.gitignore`,不會被版本控制帶走。
> 金鑰與憑證**永遠不要**貼進程式碼或聊天/雲端。

### 3.3 執行

```bash
python doctor.py              # 先診斷:檢查 .env 是否被正確讀到
python run.py                 # 連永豐,同步當下持倉與現金
streamlit run viewer/app.py   # 看儀表板
```

> **連不上或顯示讀不到憑證時,先跑 `python doctor.py`**,它會印出讀到了哪些設定
> (金鑰遮罩)、`.env` 的編碼、憑證檔是否存在。把輸出貼出來就能快速定位問題。

終端機也會直接印出摘要,不開儀表板也能看到總資產。

---

## 平常怎麼用

想看的時候:開電腦 → `python run.py` → `streamlit run viewer/app.py`。
每跑一次 `run.py` 就存一筆「真實淨值快照」;之後 P1 的回補引擎
會把兩次開機之間的每日淨值補齊,曲線就會連起來。

備份:整個資料就是 `portfolio.db` 一個檔,複製它即完成全量備份。

---

## 專案結構

```
my-portfolio/
├── run.py                # 主程式:同步 + 存快照
├── adapters/
│   ├── base.py           # 券商統一介面(日後加嘉信/盈透)
│   ├── sinopac.py        # 永豐 Shioaji
│   └── mock.py           # 模擬資料(--mock)
├── core/
│   ├── db.py             # SQLite 存取(portfolio.db 單檔)
│   └── models.py         # 統一資料模型
├── viewer/app.py         # Streamlit 儀表板
├── .env.template         # 憑證範本(複製為 .env)
└── requirements.txt
```

## 常見問題

- **`找不到永豐憑證`** → 還沒建立 `.env`,或想先試流程請用 `python run.py --mock`。
- **正式環境報 CA 相關錯誤** → 確認 `.pfx` 路徑正確、密碼正確、`SINOPAC_PERSON_ID` 已填。
- **連線數限制** → 永豐同一身分證最多 5 條連線;本程式結束時會自動登出,若異常中斷導致額度卡住,稍候再試或重新產生金鑰。
- **數量單位** → 程式已指定以「股」為單位抓持倉,與每股均價一致;1 張 = 1,000 股。

## P1 已內建:每日淨值曲線(回補引擎)

每次執行 `python run.py`,除了存「真實快照」,還會自動:

1. **記錄當天成交**(若有買賣)進交易史
2. **回補**上次快照到這次之間的**每日淨值**:抓區間每日收盤價
   (yfinance,含**拆股調整**——0050 在 2025 年 1 拆 4 已正確處理)、
   反向重播交易、逐日估值
3. 真實快照日永遠不會被估計值覆蓋;每段估計都被兩端真實快照錨定

也就是說:**就算一週只開一次,儀表板仍會是一條每日粒度的淨值曲線。**

額外指令:

```bash
python run.py --backfill-only   # 不連券商,只重跑回補(例:剛裝好 yfinance)
```

限制(誠實標示):App 啟用「之前」的歷史無法自動還原(交易回溯有限),
曲線從第一筆快照開始;區間內若有未被記錄到的買賣,該段為近似值,
但下一個真實快照會重新錨定,誤差不跨段累積。

## P2 已內建:完整視覺化儀表板

儀表板(`streamlit run viewer/app.py`)升級為四個分頁:

1. **總覽** — KPI、每日淨值曲線(實心點=真實快照)、持倉明細、快照紀錄
2. **資產配置** — 環圈圖,維度可切換:**類別 / 券商 / 幣別 / 產業**,可選含/不含現金
   (產業別由永豐合約主檔自動判別;ETF 自成一類)
3. **損益排行** — Top gainers / losers 橫條圖,可依**金額**或**報酬率%**排序
4. **股息現金流** — 配息行事曆、組合估算殖利率、近 12 個月月現金流長條圖
   - 資料來源:`run.py` 執行時自動抓持倉標的的除息紀錄(yfinance),存入本地
     `dividend_cache`,之後離線也能看
   - **估算口徑(誠實標示)**:「歷史每股配息 × 目前持股」,非帳上實收金額
     (實收對帳屬 P3 已實現損益範疇)

全域設定(側邊欄):**漲跌配色切換** — 台股「紅漲綠跌」↔ 美股「綠漲紅跌」,
影響所有損益數字與圖表。

舊資料庫免處理:首次以 P2 版開啟時自動遷移(補 `industry` 欄、建 `dividend_cache` 表)。

## P3 已內建:即時報價、已實現損益、TWR、React 專業版

### ⚡ 即時報價(盤中)

```bash
python watch.py          # 另開終端機:連永豐,訂閱目前持倉的即時成交
python watch.py --mock   # 免帳號:隨機漫步模擬報價(驗證流程/盤後體驗)
```

開著 watch.py,再到 Streamlit 儀表板側邊欄打開「⚡ 即時模式」,
KPI 每 2 秒以最新成交價重算市值(React 版則自動每 3 秒輪詢)。
Ctrl+C 結束會自動登出券商並清空即時報價,不佔永豐 5 連線額度。

### 💼 已實現損益

每次 `python run.py` 自動抓近 60 天平倉損益(永豐 `list_profit_loss`),
去重累積進本地資料庫 — 一旦入庫就永久保留,不受券商查詢區間限制。
儀表板新增「已實現損益」分頁:累計曲線、各標的排行、月別統計、勝率。

### 📐 TWR 時間加權報酬率

總覽分頁同時顯示兩個口徑:
- **期間變化(含出入金)**:資產規模變化,入金會放大它
- **TWR(不含出入金)**:剔除出入金影響的真實投資績效(基金淨值法)

出入金請用 flows.py 補登(同時讓回補曲線正確畫出入金跳階):

```bash
python flows.py add --date 2026-06-01 --amount 500000 --type DEPOSIT
python flows.py list
python run.py --backfill-only   # 補登後重算歷史
```

### 🖥 React 專業版儀表板(選用,存摺帳本風)

```bash
pip install fastapi uvicorn        # 第一次
uvicorn api.server:app --port 8787
# 瀏覽器開 http://127.0.0.1:8787
```

單一打包檔(viewer-react/dist),**免裝 Node、免 CDN、可離線**;
與 Streamlit 版讀同一個 portfolio.db,功能對齊(配置/排行/股息/已實現/即時)。
要改前端:`cd viewer-react && npm install && npm run build`。

## 下一步(P4)

- 嘉信 / 盈透 adapter(介面已留好,`run.py` 不用改)
- 多幣別實際換算(USD/TWD 歷史匯率回補,FXService 結構已備)
- 商品主檔跨券商去重、期權帳戶
