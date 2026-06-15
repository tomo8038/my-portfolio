"""資產整合儀表板 — 亮色單頁版(V1.2 UI)。

執行:streamlit run viewer/app.py
(請先跑過一次 python rebuild_all.py 產生 / 更新 portfolio.db)

本檔只「讀」portfolio.db,不寫入。沿用既有資料表:
  snapshots / daily_networth / positions_current / transactions /
  dividend_cache / realized_pnl / live_quotes / fx_cache
所有讀取都做了容錯:某張表不存在或為空時,該區塊自動降級顯示,不崩。
版面:亮色暖奶油主題、單頁式,內容依重要性由上而下排列:
  總覽 → 年度報酬 → 資產配置 → 損益排行 → 已實現損益 →(股息現金流)
跨幣別一律換算為 TWD;TWR 與出入金皆以當日匯率換算後計算。
"""
import sqlite3
import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent.parent))
DB_PATH = Path(__file__).parent.parent / "portfolio.db"

try:
    from core.performance import (compute_annual_performance,
                                  compute_performance)
except Exception:                       # 萬一 import 失敗,年度分頁降級提示
    compute_annual_performance = compute_performance = None

st.set_page_config(page_title="資產整合儀表板", page_icon="◆",
                   layout="wide", initial_sidebar_state="collapsed")

# ======================================================================
# 設計 tokens(亮色 · 暖奶油)
# ======================================================================
BG     = "#f6f1e7"   # 暖奶油底
BG2    = "#efe7d6"   # 漸層用的深一階
PANEL  = "#fffdf9"   # 卡片(近白暖)
PANEL2 = "#fbf6ec"   # 次卡片
LINE   = "#e7ddc9"   # 暖色邊線
TXT    = "#332e25"   # 暖近黑
DIM    = "#8c8270"   # 暖灰(次要文字)
ACCENT = "#1f8a78"   # 主強調(沉穩深青綠)
ACCENT2 = "#c47f3d"  # 次強調(暖琥珀)
GRID   = "#ece3d2"   # 圖表格線(淺暖)

SCHEMES = {
    "台股 · 紅漲綠跌": {"up": "#d2453b", "down": "#1f9e6e", "metric": "inverse"},
    "美股 · 綠漲紅跌": {"up": "#1f9e6e", "down": "#d2453b", "metric": "normal"},
}

with st.sidebar:
    st.markdown("### 顯示設定")
    scheme_name = st.radio("漲跌配色", list(SCHEMES), index=0,
                           help="影響所有損益數字與圖表的紅綠方向")
    SCHEME = SCHEMES[scheme_name]
    st.caption("台股慣例紅漲綠跌;美股慣例綠漲紅跌。")
    st.divider()
    live_mode = st.toggle("⚡ 即時模式", value=False,
                          help="需另開終端機執行 python watch.py(盤中)"
                               "或 python watch.py --mock。KPI 每 2 秒刷新。")

UP, DOWN, METRIC_COLOR = SCHEME["up"], SCHEME["down"], SCHEME["metric"]

# ======================================================================
# 全域 CSS — 亮色暖奶油 · 柔和陰影 · 單頁式
# ======================================================================
st.markdown(f"""
<style>
:root {{
  --bg:{BG}; --bg2:{BG2}; --panel:{PANEL}; --panel2:{PANEL2}; --line:{LINE};
  --txt:{TXT}; --dim:{DIM}; --accent:{ACCENT}; --accent2:{ACCENT2};
  --shadow:0 1px 2px rgba(80,64,38,.05), 0 10px 30px rgba(80,64,38,.06);
}}
.stApp {{
  background:
    radial-gradient(1100px 560px at 88% -8%, rgba(196,127,61,.10), transparent 60%),
    radial-gradient(1000px 520px at -5% 0%, rgba(31,138,120,.10), transparent 55%),
    {BG};
  color: var(--txt);
}}
.block-container {{ padding-top: 2.2rem; padding-bottom: 4rem; max-width: 1240px; }}
#MainMenu, footer, header[data-testid="stHeader"] {{ visibility: hidden; }}

h1,h2,h3,h4 {{ color: var(--txt); letter-spacing:.2px; font-weight:700; }}
.stApp, .stMarkdown, p, span, div {{ font-feature-settings:"tnum" 1; }}
.stApp a {{ color: var(--accent); text-decoration:none; }}

/* ---- 頂部標題列 ---- */
.hero {{
  display:flex; align-items:flex-end; justify-content:space-between;
  border-bottom:1px solid var(--line); padding-bottom:14px; margin-bottom:20px;
}}
.hero .brand {{ display:flex; align-items:center; gap:12px; }}
.hero .mark {{
  width:34px; height:34px; border-radius:10px;
  background:linear-gradient(135deg,var(--accent),var(--accent2));
  box-shadow:0 6px 16px rgba(31,138,120,.30); position:relative;
}}
.hero .mark::after {{
  content:""; position:absolute; inset:8px; border-radius:5px; background:var(--panel);
}}
.hero h1 {{ font-size:1.45rem; margin:0; }}
.hero .sub {{ color:var(--dim); font-size:.8rem; margin-top:3px; }}
.pill {{
  font-size:.72rem; color:var(--accent); border:1px solid rgba(31,138,120,.30);
  background:rgba(31,138,120,.08); border-radius:999px; padding:4px 11px; font-weight:600;
}}

/* ---- 頁內導覽列 ---- */
.nav {{ display:flex; flex-wrap:wrap; gap:8px; margin:2px 0 18px; }}
.nav a {{
  font-size:.78rem; color:var(--dim); background:var(--panel);
  border:1px solid var(--line); border-radius:999px; padding:5px 13px;
  box-shadow:var(--shadow); transition:.15s;
}}
.nav a:hover {{ color:var(--accent); border-color:var(--accent); }}
.nav a b {{ color:var(--accent); font-weight:700; margin-right:5px; }}

/* ---- KPI 卡 ---- */
.kpis {{ display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin-bottom:6px; }}
.kpi {{
  background:var(--panel); border:1px solid var(--line); border-radius:16px;
  padding:16px 18px; position:relative; overflow:hidden; box-shadow:var(--shadow);
}}
.kpi::before {{
  content:""; position:absolute; left:0; top:0; bottom:0; width:4px;
  background:linear-gradient(var(--accent),var(--accent2));
}}
.kpi .label {{ color:var(--dim); font-size:.78rem; letter-spacing:.3px; }}
.kpi .value {{ font-size:1.6rem; font-weight:800; margin-top:6px; line-height:1.1;
  font-variant-numeric:tabular-nums; color:var(--txt); }}
.kpi .delta {{ font-size:.82rem; margin-top:5px; font-variant-numeric:tabular-nums; }}
.kpi.glow {{ box-shadow:0 0 0 1px rgba(31,138,120,.25), var(--shadow); }}

/* ---- 大區塊標題(單頁式) ---- */
.section-head {{
  display:flex; align-items:center; gap:12px; margin:30px 0 14px;
  padding-top:18px; border-top:1px solid var(--line);
}}
.section-head .idx {{
  width:30px; height:30px; flex:none; border-radius:9px; font-size:.92rem;
  font-weight:800; color:#fff; display:flex; align-items:center; justify-content:center;
  background:linear-gradient(135deg,var(--accent),var(--accent2));
  box-shadow:0 6px 14px rgba(31,138,120,.25);
}}
.section-head .t {{ font-size:1.18rem; font-weight:800; color:var(--txt); }}
.section-head .s {{ color:var(--dim); font-size:.78rem; margin-left:2px; }}

/* ---- 子標題 ---- */
.sect {{ font-size:.92rem; font-weight:700; color:var(--txt);
  margin:6px 0 10px; display:flex; align-items:center; gap:8px; }}
.sect::before {{ content:""; width:7px; height:7px; border-radius:2px; background:var(--accent); }}

/* ---- DataFrame 亮色微調 ---- */
[data-testid="stDataFrame"] {{ border:1px solid var(--line); border-radius:12px;
  box-shadow:var(--shadow); }}

.caption {{ color:var(--dim); font-size:.76rem; }}
hr {{ border-color:var(--line); }}

/* 側邊欄亮色 */
[data-testid="stSidebar"] {{ background:var(--panel2); border-right:1px solid var(--line); }}
</style>
""", unsafe_allow_html=True)


def chart_theme(c):
    """套用亮色圖表外觀(透明背景、淺暖格線、深色文字)。"""
    return (c.configure(background="transparent")
             .configure_view(stroke=None)
             .configure_axis(grid=True, gridColor=GRID, gridOpacity=1,
                             domainColor=LINE, tickColor=LINE,
                             labelColor=DIM, titleColor=DIM,
                             labelFontSize=11, titleFontSize=11)
             .configure_legend(labelColor=TXT, titleColor=DIM)
             .configure_arc(stroke=PANEL, strokeWidth=2))


def section_head(idx: int, title: str, subtitle: str = "", anchor: str = ""):
    a = f'<span id="{anchor}"></span>' if anchor else ""
    s = f'<span class="s">{subtitle}</span>' if subtitle else ""
    st.markdown(f'{a}<div class="section-head"><div class="idx">{idx}</div>'
                f'<div class="t">{title}</div>{s}</div>', unsafe_allow_html=True)


def fmt(v, signed=False):
    return f"{'+' if signed and v >= 0 else ''}{v:,.0f}"


# ======================================================================
# 讀取資料(全部容錯)
# ======================================================================
if not DB_PATH.exists():
    st.markdown('<div class="hero"><div class="brand"><div class="mark"></div>'
                '<div><h1>資產整合儀表板</h1></div></div></div>',
                unsafe_allow_html=True)
    st.warning("還沒有資料。請先在專案目錄執行:`python rebuild_all.py`")
    st.stop()


def q(con, sql, default=None, params=None):
    try:
        return pd.read_sql(sql, con, params=params)
    except Exception:
        return default if default is not None else pd.DataFrame()


con = sqlite3.connect(DB_PATH)
con.execute("PRAGMA busy_timeout=3000")

snap = con.execute(
    "SELECT ts, net_worth, invested, cash, cost FROM snapshots "
    "ORDER BY ts DESC LIMIT 1").fetchone()
if not snap:
    con.close()
    st.warning("資料庫存在但沒有快照。請先執行 `python rebuild_all.py`。")
    st.stop()

ts, net_worth, invested, cash, cost = snap
unrealized = invested - cost
pct = unrealized / cost * 100 if cost else 0.0

pos = q(con, "SELECT broker, symbol, name, asset_class, industry, qty, "
             "avg_cost, last_price, ccy, market_value_native AS mv, "
             "unrealized_pnl_native AS pnl FROM positions_current ORDER BY mv DESC")
series = q(con, "SELECT date, net_worth, is_real FROM daily_networth ORDER BY date")
snaps_table = q(con, "SELECT substr(ts,1,16) AS 時間, net_worth AS 淨值, "
                     "cash AS 現金 FROM snapshots ORDER BY ts DESC LIMIT 10")
div_cache = q(con, "SELECT symbol, date, amount FROM dividend_cache")
realized = q(con, "SELECT symbol, qty, price, pnl, trade_date FROM realized_pnl "
                  "ORDER BY trade_date")
# 外部現金流(出入金)— 年度報酬 / TWR 用;入金正、出金負。amount 為各券商原幣,
# 需逐筆用「當天匯率」換成 TWD(與每日淨值曲線同口徑),不能直接跨幣別相加。
flows_df = q(con, "SELECT trade_date AS d, ccy, SUM(amount) AS a FROM transactions "
                  "WHERE txn_type IN ('DEPOSIT','WITHDRAW') GROUP BY trade_date, ccy")
# 完整 USD/TWD 歷史(供逐日換算),由舊到新
usdtwd_df = q(con, "SELECT date, rate FROM fx_cache WHERE pair='USDTWD' "
                   "ORDER BY date")

# 各幣別→TWD 的最新匯率(美股市值/損益以原幣 USD 儲存,需換成 TWD 才能與 KPI、
# 以及彼此相加;否則把幾千 USD 當幾千 TWD 加總,配置/損益會嚴重失真)。
fx_rate = {"TWD": 1.0}
fx_missing: list[str] = []
if not pos.empty:
    for ccy in sorted(c for c in pos["ccy"].dropna().unique() if c != "TWD"):
        r = q(con, "SELECT rate FROM fx_cache WHERE pair = ? "
                   "ORDER BY date DESC LIMIT 1", params=(f"{ccy}TWD",))
        if r is not None and not r.empty:
            fx_rate[ccy] = float(r["rate"].iloc[0])
        else:
            fx_rate[ccy] = 1.0
            fx_missing.append(ccy)
con.close()

# 逐日 USD/TWD 查詢(forward-fill;早於最早一筆則用最早一筆)
_fx_dates = list(usdtwd_df["date"]) if not usdtwd_df.empty else []
_fx_vals = list(usdtwd_df["rate"]) if not usdtwd_df.empty else []


def usdtwd_on(d: str):
    if not _fx_dates:
        return None
    import bisect
    i = bisect.bisect_right(_fx_dates, d)
    return _fx_vals[i - 1] if i > 0 else _fx_vals[0]


# 把出入金換成 TWD,彙總成 {date: 淨流入(TWD)}
flows_twd: dict[str, float] = {}
flow_fx_missing = False
if not flows_df.empty:
    for _, fr in flows_df.iterrows():
        d, ccy, a = str(fr["d"]), (fr["ccy"] or "TWD"), float(fr["a"])
        if ccy == "TWD":
            amt = a
        else:                       # 非台幣(本系統即 USD)→ 當天 USD/TWD 換算
            rate = usdtwd_on(d)
            if rate is None:
                rate = 1.0
                flow_fx_missing = True
            amt = a * rate
        flows_twd[d] = flows_twd.get(d, 0.0) + amt

if not pos.empty:
    pos["fx"] = pos["ccy"].map(fx_rate).fillna(1.0)
    pos["mv_twd"] = pos["mv"] * pos["fx"]                 # 市值(換算成 TWD)
    pos["pnl_twd"] = pos["pnl"] * pos["fx"]               # 未實現損益(TWD)
    pos["cost_value"] = pos["qty"] * pos["avg_cost"]      # 成本(原幣,算報酬率用)
    pos["ret_pct"] = (pos["pnl"] / pos["cost_value"] * 100).where(
        pos["cost_value"] != 0, 0.0)                      # 報酬率與幣別無關
    pos["industry"] = pos["industry"].fillna("").replace("", "其他")

BROKER_NAMES = {"sinopac": "永豐金", "schwab": "嘉信", "ibkr": "盈透"}
CLASS_NAMES = {"equity": "個股", "etf": "ETF", "cash": "現金"}


def pnl_css(v):
    return f"color:{UP if v >= 0 else DOWN}"


# ======================================================================
# 頂部 + KPI
# ======================================================================
st.markdown(f"""
<div class="hero">
  <div class="brand">
    <div class="mark"></div>
    <div>
      <h1>資產整合儀表板</h1>
      <div class="sub">資料時間 {ts} · 跨永豐／嘉信／盈透 · 配色 {scheme_name}</div>
    </div>
  </div>
  <div class="pill">基準幣別 TWD</div>
</div>
""", unsafe_allow_html=True)

ucol = UP if unrealized >= 0 else DOWN


def kpi_card(label, value, delta_html="", glow=False):
    g = " glow" if glow else ""
    return (f'<div class="kpi{g}"><div class="label">{label}</div>'
            f'<div class="value">{value}</div>{delta_html}</div>')


if not live_mode:
    cards = "".join([
        kpi_card("總資產淨值", f"NT$ {net_worth:,.0f}", glow=True),
        kpi_card("投資市值", f"NT$ {invested:,.0f}"),
        kpi_card("現金", f"NT$ {cash:,.0f}"),
        kpi_card("未實現損益", f"NT$ {unrealized:+,.0f}",
                 f'<div class="delta" style="color:{ucol}">{pct:+.2f}%</div>'),
    ])
    st.markdown(f'<div class="kpis">{cards}</div>', unsafe_allow_html=True)
else:
    @st.fragment(run_every=2)
    def _live_kpis():
        lcon = sqlite3.connect(DB_PATH)
        lcon.execute("PRAGMA busy_timeout=3000")
        try:
            lq = {s: p for s, p, _ in
                  lcon.execute("SELECT symbol, price, ts FROM live_quotes")}
        except Exception:
            lq = {}
        lcon.close()
        if lq and not pos.empty:
            # 即時市值 = Σ 股數 × 即時(原幣)價 × 該幣別匯率 → TWD
            l_inv = float((pos.apply(
                lambda r: r["qty"] * lq.get(r["symbol"], r["last_price"])
                * r["fx"], axis=1)).sum())
            l_cost = float((pos["cost_value"] * pos["fx"]).sum())
        else:
            l_inv, l_cost = invested, cost
        l_unrl = l_inv - l_cost
        l_nw = l_inv + cash
        l_pct = l_unrl / l_cost * 100 if l_cost else 0.0
        uc = UP if l_unrl >= 0 else DOWN
        tag = "⚡" if lq else ""
        cards = "".join([
            kpi_card(f"總資產淨值 {tag}", f"NT$ {l_nw:,.0f}",
                     f'<div class="delta" style="color:{DIM}">'
                     f'較快照 {l_nw - net_worth:+,.0f}</div>', glow=True),
            kpi_card("投資市值", f"NT$ {l_inv:,.0f}"),
            kpi_card("現金", f"NT$ {cash:,.0f}"),
            kpi_card("未實現損益", f"NT$ {l_unrl:+,.0f}",
                     f'<div class="delta" style="color:{uc}">{l_pct:+.2f}%</div>'),
        ])
        st.markdown(f'<div class="kpis">{cards}</div>', unsafe_allow_html=True)
        if not lq:
            st.caption("⚡ 即時模式已開但收不到報價 — 請執行 "
                       "`python watch.py` 或 `python watch.py --mock`。"
                       "上方為最近一次快照。")
    _live_kpis()

st.write("")
# 頁內導覽(依重要性)
st.markdown(
    '<div class="nav">'
    '<a href="#s-ov"><b>1</b>總覽</a>'
    '<a href="#s-year"><b>2</b>年度報酬</a>'
    '<a href="#s-alloc"><b>3</b>資產配置</a>'
    '<a href="#s-pnl"><b>4</b>損益排行</a>'
    '<a href="#s-real"><b>5</b>已實現損益</a>'
    '<a href="#s-div">＋股息現金流</a>'
    '</div>', unsafe_allow_html=True)

# 單頁式:依「重要性」順序先建立各區塊容器(容器於建立處渲染),
# 之後原本的 with 區塊寫入對應容器即可 —— 視覺順序=重要性,邏輯不變。
section_head(1, "總覽", "資產全貌與每日淨值", anchor="s-ov")
tab_ov = st.container()
section_head(2, "年度報酬", "TWR 口徑,出入金不影響績效", anchor="s-year")
tab_year = st.container()
section_head(3, "資產配置", "市值已換算為 TWD", anchor="s-alloc")
tab_alloc = st.container()
section_head(4, "損益排行", "未實現損益(TWD)", anchor="s-pnl")
tab_pnl = st.container()
section_head(5, "已實現損益", "平倉實現的累計損益", anchor="s-real")
tab_real = st.container()
section_head(6, "股息現金流", "歷史每股配息 × 持股的估算", anchor="s-div")
tab_div = st.container()

# ======================================================================
# 分頁 1:總覽
# ======================================================================
with tab_ov:
    st.markdown('<div class="sect">每日淨值走勢</div>', unsafe_allow_html=True)
    if not series.empty and len(series) >= 2:
        s = series.copy()
        s["date"] = pd.to_datetime(s["date"])
        grad = alt.Gradient(
            gradient="linear",
            stops=[alt.GradientStop(color=BG, offset=0),
                   alt.GradientStop(color=ACCENT, offset=1)],
            x1=1, x2=1, y1=1, y2=0)
        area = alt.Chart(s).mark_area(
            line={"color": ACCENT, "strokeWidth": 2.4},
            color=grad, opacity=.38).encode(
            x=alt.X("date:T", title=None),
            y=alt.Y("net_worth:Q", title="淨值 (TWD)",
                    scale=alt.Scale(zero=False)),
            tooltip=[alt.Tooltip("date:T", title="日期"),
                     alt.Tooltip("net_worth:Q", title="淨值", format=",.0f")])
        dots = alt.Chart(s[s["is_real"] == 1]).mark_point(
            color=ACCENT, filled=True, size=55,
            stroke=PANEL, strokeWidth=1.5).encode(
            x="date:T", y="net_worth:Q",
            tooltip=[alt.Tooltip("date:T", title="真實快照"),
                     alt.Tooltip("net_worth:Q", title="淨值", format=",.0f")])
        st.altair_chart(chart_theme((area + dots).properties(height=320),),
                        width="stretch")
        st.markdown('<div class="caption">亮點＝真實快照(每次 rebuild_all.py);'
                    '其餘為每日回補估計,真實快照日永不被覆蓋。</div>',
                    unsafe_allow_html=True)
    else:
        st.info("淨值曲線需要至少 2 筆紀錄。每次執行 `python rebuild_all.py` 會更新快照。")

    st.write("")
    left, right = st.columns([1.5, 1])
    with left:
        st.markdown('<div class="sect">持倉明細</div>', unsafe_allow_html=True)
        if pos.empty:
            st.info("目前沒有持倉。")
        else:
            show = pos[["broker", "symbol", "name", "asset_class", "qty",
                        "avg_cost", "last_price", "mv_twd", "pnl_twd",
                        "ret_pct"]].copy()
            show["broker"] = show["broker"].map(lambda b: BROKER_NAMES.get(b, b))
            show["asset_class"] = show["asset_class"].map(
                lambda c: CLASS_NAMES.get(c, c))
            show.columns = ["券商", "代號", "名稱", "類別", "股數", "均價",
                            "現價", "市值(TWD)", "未實現損益(TWD)", "報酬率%"]
            st.dataframe(show.style.format({
                "股數": "{:,.0f}", "均價": "{:,.2f}", "現價": "{:,.2f}",
                "市值(TWD)": "{:,.0f}", "未實現損益(TWD)": "{:+,.0f}",
                "報酬率%": "{:+.1f}%",
            }).map(pnl_css, subset=["未實現損益(TWD)", "報酬率%"]),
                width="stretch", hide_index=True)
    with right:
        st.markdown('<div class="sect">快照紀錄</div>', unsafe_allow_html=True)
        st.dataframe(snaps_table.style.format(
            {"淨值": "{:,.0f}", "現金": "{:,.0f}"}),
            width="stretch", hide_index=True)

# ======================================================================
# 分頁 2:資產配置(環圈)
# ======================================================================
with tab_alloc:
    if pos.empty and cash <= 0:
        st.info("沒有持倉與現金,無法繪製配置圖。")
    else:
        c1, c2 = st.columns([3, 2])
        with c1:
            dim = st.radio("配置維度", ["類別", "券商", "幣別", "產業"],
                           horizontal=True)
        with c2:
            inc_cash = st.toggle("含現金", value=True)

        dim_col = {"類別": "asset_class", "券商": "broker",
                   "幣別": "ccy", "產業": "industry"}[dim]
        if pos.empty:
            agg = pd.DataFrame(columns=["key", "金額"])
        else:
            agg = (pos.groupby(dim_col)["mv_twd"].sum()
                   .reset_index().rename(columns={dim_col: "key", "mv_twd": "金額"}))
            if dim == "券商":
                agg["key"] = agg["key"].map(lambda b: BROKER_NAMES.get(b, b))
            if dim == "類別":
                agg["key"] = agg["key"].map(lambda c: CLASS_NAMES.get(c, c))
        if inc_cash and cash > 0:
            agg = pd.concat([agg, pd.DataFrame(
                [{"key": "現金", "金額": cash}])], ignore_index=True)
        agg = agg[agg["金額"] > 0]
        total = agg["金額"].sum()
        agg["占比"] = agg["金額"] / total * 100 if total else 0

        palette = [ACCENT, ACCENT2, "#ffb454", "#ff5470", "#5cc8ff",
                   "#b58cff", "#26d6a4", "#f78fb3", "#8ea0c0"]
        g1, g2 = st.columns([1, 1])
        with g1:
            donut = alt.Chart(agg).mark_arc(innerRadius=70, outerRadius=120).encode(
                theta=alt.Theta("金額:Q", stack=True),
                color=alt.Color("key:N", title=None,
                                scale=alt.Scale(range=palette),
                                legend=alt.Legend(orient="bottom")),
                tooltip=[alt.Tooltip("key:N", title=dim),
                         alt.Tooltip("金額:Q", format=",.0f"),
                         alt.Tooltip("占比:Q", format=".1f")],
            ).properties(height=320)
            st.altair_chart(chart_theme(donut), width="stretch")
        with g2:
            st.markdown('<div class="sect">配置明細</div>', unsafe_allow_html=True)
            tbl = agg.sort_values("金額", ascending=False)[["key", "金額", "占比"]]
            tbl.columns = [dim, "金額", "占比%"]
            st.dataframe(tbl.style.format({"金額": "NT$ {:,.0f}", "占比%": "{:.1f}%"}),
                         width="stretch", hide_index=True)
            usd = fx_rate.get("USD")
            if usd and usd != 1.0:
                st.markdown(f'<div class="caption">美股市值已用最新 USD/TWD '
                            f'≈ {usd:,.2f} 換算為 TWD,與上方 KPI 一致。</div>',
                            unsafe_allow_html=True)
            if fx_missing:
                st.markdown('<div class="caption" style="color:#ffb454">⚠ '
                            f'缺 {"/".join(fx_missing)} 對 TWD 匯率(fx_cache 無資料),'
                            '該幣別暫以原幣計入。請確認 rebuild_all.py 的匯率回補。</div>',
                            unsafe_allow_html=True)

# ======================================================================
# 分頁 3:損益排行
# ======================================================================
with tab_pnl:
    if pos.empty:
        st.info("目前沒有持倉。")
    else:
        mode = st.radio("排序依據", ["金額 (NT$)", "報酬率 (%)"], horizontal=True)
        col = "pnl_twd" if mode.startswith("金額") else "ret_pct"
        rank = pos[["symbol", "name", "broker", "pnl_twd", "ret_pct"]].copy()
        rank["標的"] = rank["symbol"] + " " + rank["name"].fillna("")
        rank = rank.sort_values(col, ascending=False)

        win = rank[rank[col] > 0]; los = rank[rank[col] < 0]
        cards = "".join([
            kpi_card("獲利檔數", f"{len(win)} 檔",
                     f'<div class="delta" style="color:{UP}">'
                     f'+NT$ {win["pnl_twd"].sum():,.0f}</div>'),
            kpi_card("虧損檔數", f"{len(los)} 檔",
                     f'<div class="delta" style="color:{DOWN}">'
                     f'-NT$ {abs(los["pnl_twd"].sum()):,.0f}</div>'),
        ])
        st.markdown(f'<div class="kpis" style="grid-template-columns:repeat(2,1fr);'
                    f'max-width:520px">{cards}</div>', unsafe_allow_html=True)
        st.write("")
        bars = alt.Chart(rank).mark_bar(cornerRadius=4, height=18).encode(
            x=alt.X(f"{col}:Q",
                    title="未實現損益 (NT$)" if col == "pnl_twd" else "報酬率 (%)"),
            y=alt.Y("標的:N", sort="-x", title=None),
            color=alt.condition(alt.datum[col] >= 0,
                                alt.value(UP), alt.value(DOWN)),
            tooltip=[alt.Tooltip("標的:N"),
                     alt.Tooltip("pnl_twd:Q", title="損益(TWD)", format="+,.0f"),
                     alt.Tooltip("ret_pct:Q", title="報酬率%", format="+.1f")],
        ).properties(height=max(220, 40 * len(rank)))
        st.altair_chart(chart_theme(bars), width="stretch")

# ======================================================================
# 分頁:年度報酬(TWR 口徑,出入金不計入績效)
# ======================================================================
with tab_year:
    s = series.copy()
    flows = flows_twd
    if compute_annual_performance is None:
        st.info("年度報酬引擎載入失敗(core/performance.py)。請確認檔案存在。")
    elif s.empty or len(s) < 2:
        st.info("每日淨值資料不足,無法計算年度報酬。先跑 `python rebuild_all.py`。")
    else:
        ser = list(zip(s["date"].astype(str), s["net_worth"].astype(float)))
        rows = compute_annual_performance(ser, flows)
        full = compute_performance(ser, flows)

        # 全期摘要 KPI
        tot_pnl = sum(r["pnl"] for r in rows)
        full_twr = full["twr"] if full else None
        full_ann = full["twr_annualized"] if full else None
        tc = UP if tot_pnl >= 0 else DOWN
        twr_c = UP if (full_twr or 0) >= 0 else DOWN
        cards = "".join([
            kpi_card("累計投資損益", f"NT$ {tot_pnl:+,.0f}",
                     '<div class="delta" style="color:#8c8270">'
                     '已扣除出入金</div>'),
            kpi_card("全期 TWR", f"{full_twr*100:+.2f}%" if full_twr is not None
                     else "—",
                     f'<div class="delta" style="color:{twr_c}">時間加權報酬</div>'),
            kpi_card("全期年化", f"{full_ann*100:+.2f}%" if full_ann is not None
                     else "—",
                     f'<div class="delta" style="color:#8c8270">'
                     f'{full["days"]} 天</div>' if full else ""),
        ])
        st.markdown(f'<div class="kpis" style="grid-template-columns:'
                    f'repeat(3,1fr)">{cards}</div>', unsafe_allow_html=True)
        st.write("")

        # 年度 TWR 長條圖
        st.markdown('<div class="sect">各年度報酬率(TWR)</div>',
                    unsafe_allow_html=True)
        bar_df = pd.DataFrame([{
            "年度": str(r["year"]),
            "報酬率": (r["twr"] * 100) if r["twr"] is not None else 0.0,
            "未滿年": "未滿整年" if r["partial"] else "整年",
        } for r in rows])
        ybars = alt.Chart(bar_df).mark_bar(cornerRadius=5).encode(
            x=alt.X("年度:N", sort=None, title=None),
            y=alt.Y("報酬率:Q", title="TWR (%)"),
            color=alt.condition(alt.datum["報酬率"] >= 0,
                                alt.value(UP), alt.value(DOWN)),
            opacity=alt.condition(alt.datum["未滿年"] == "整年",
                                  alt.value(1.0), alt.value(0.55)),
            tooltip=[alt.Tooltip("年度:N"),
                     alt.Tooltip("報酬率:Q", format="+.2f"),
                     alt.Tooltip("未滿年:N")],
        ).properties(height=240)
        st.altair_chart(chart_theme(ybars), width="stretch")
        st.markdown('<div class="caption">半透明長條代表未滿整年(首年自開戶日、'
                    '當年至今);其報酬率為該區間實際值,年化欄為外推年化值。</div>',
                    unsafe_allow_html=True)
        st.write("")

        # 年度明細表
        st.markdown('<div class="sect">年度明細</div>', unsafe_allow_html=True)
        tbl = pd.DataFrame([{
            "年度": f'{r["year"]}{"*" if r["partial"] else ""}',
            "起始金額": r["start_nv"],
            "最終金額": r["end_nv"],
            "淨流入": r["net_flow"],
            "損益": r["pnl"],
            "報酬率(TWR)": r["twr"] * 100 if r["twr"] is not None else None,
            "年化報酬": r["annualized"] * 100 if r["annualized"] is not None
            else None,
        } for r in reversed(rows)])
        st.dataframe(tbl.style.format({
            "起始金額": "NT$ {:,.0f}", "最終金額": "NT$ {:,.0f}",
            "淨流入": "{:+,.0f}", "損益": "{:+,.0f}",
            "報酬率(TWR)": "{:+.2f}%", "年化報酬": "{:+.2f}%",
        }, na_rep="—").map(pnl_css, subset=["損益", "報酬率(TWR)", "年化報酬"]),
            width="stretch", hide_index=True)
        st.markdown('<div class="caption">起始金額＝進入該年度時的淨值(前一年末);'
                    '損益＝期末−期初−淨流入(扣掉出入金的真實投資損益);'
                    '淨流入為各券商出入金以「當天匯率」換算後的 TWD 合計;'
                    '報酬率為 TWR(出入金不影響);* 表示未滿整年。</div>',
                    unsafe_allow_html=True)
        if flow_fx_missing:
            st.markdown('<div class="caption" style="color:#ffb454">⚠ '
                        '部分出入金日期早於匯率資料範圍,該筆暫以原幣計入。'
                        '請確認 rebuild_all.py 的 USD/TWD 匯率回補涵蓋最早交易日。'
                        '</div>', unsafe_allow_html=True)

# ======================================================================
# 分頁 4:股息現金流(估算)
# ======================================================================
with tab_div:
    if pos.empty or div_cache.empty:
        st.info("尚無股息資料。rebuild_all.py 會抓持倉標的的除息紀錄寫入 dividend_cache。")
    else:
        qty_map = dict(zip(pos["symbol"], pos["qty"]))
        d = div_cache.copy()
        d["持股"] = d["symbol"].map(qty_map).fillna(0)
        d = d[d["持股"] > 0].copy()
        if d.empty:
            st.info("目前持倉標的尚無歷史除息紀錄。")
        else:
            d["現金"] = d["amount"] * d["持股"]
            d["date"] = pd.to_datetime(d["date"])
            d["月份"] = d["date"].dt.to_period("M").astype(str)
            recent = d[d["date"] >= (pd.Timestamp.today() - pd.Timedelta(days=365))]
            est_year = float(recent["現金"].sum())
            yld = est_year / invested * 100 if invested else 0
            cards = "".join([
                kpi_card("近 12 月估算配息", f"NT$ {est_year:,.0f}"),
                kpi_card("估算殖利率", f"{yld:.2f}%",
                         '<div class="delta" style="color:#8c8270">'
                         '以歷史每股配息×目前持股</div>'),
            ])
            st.markdown(f'<div class="kpis" style="grid-template-columns:'
                        f'repeat(2,1fr);max-width:520px">{cards}</div>',
                        unsafe_allow_html=True)
            st.write("")
            monthly = (d.groupby("月份")["現金"].sum().reset_index()
                       .sort_values("月份").tail(18))
            bar = alt.Chart(monthly).mark_bar(
                cornerRadius=4, color=ACCENT2).encode(
                x=alt.X("月份:N", sort=None, title=None),
                y=alt.Y("現金:Q", title="配息 (NT$)"),
                tooltip=[alt.Tooltip("月份:N"),
                         alt.Tooltip("現金:Q", format=",.0f")],
            ).properties(height=260)
            st.altair_chart(chart_theme(bar), width="stretch")
            st.markdown('<div class="caption">估算口徑:歷史每股配息 × 目前持股,'
                        '非帳上實收金額(實收以永豐 CSV 的「配息」紀錄為準)。</div>',
                        unsafe_allow_html=True)

# ======================================================================
# 分頁 5:已實現損益
# ======================================================================
with tab_real:
    if realized.empty:
        st.info("尚無已實現損益資料。")
    else:
        r = realized.copy()
        r["trade_date"] = pd.to_datetime(r["trade_date"])
        r = r.sort_values("trade_date")
        r["累計"] = r["pnl"].cumsum()
        total_r = float(r["pnl"].sum())
        wins = int((r["pnl"] > 0).sum())
        rate = wins / len(r) * 100 if len(r) else 0
        rc = UP if total_r >= 0 else DOWN
        cards = "".join([
            kpi_card("累計已實現損益", f"NT$ {total_r:+,.0f}",
                     f'<div class="delta" style="color:{rc}">'
                     f'{len(r)} 筆平倉</div>'),
            kpi_card("勝率", f"{rate:.0f}%",
                     f'<div class="delta" style="color:{DIM}">'
                     f'{wins}/{len(r)} 筆獲利</div>'),
        ])
        st.markdown(f'<div class="kpis" style="grid-template-columns:'
                    f'repeat(2,1fr);max-width:520px">{cards}</div>',
                    unsafe_allow_html=True)
        st.write("")
        cum = alt.Chart(r).mark_line(color=ACCENT, strokeWidth=2.2).encode(
            x=alt.X("trade_date:T", title=None),
            y=alt.Y("累計:Q", title="累計已實現損益 (NT$)"),
            tooltip=[alt.Tooltip("trade_date:T", title="日期"),
                     alt.Tooltip("累計:Q", format="+,.0f")],
        ).properties(height=260)
        zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(
            strokeDash=[4, 4], color=DIM).encode(y="y:Q")
        st.altair_chart(chart_theme(cum + zero), width="stretch")

        st.markdown('<div class="sect">平倉明細</div>', unsafe_allow_html=True)
        det = r.sort_values("trade_date", ascending=False)[
            ["trade_date", "symbol", "qty", "price", "pnl"]].copy()
        det["trade_date"] = det["trade_date"].dt.strftime("%Y-%m-%d")
        det.columns = ["日期", "代號", "數量", "成交價", "已實現損益"]
        st.dataframe(det.style.format({
            "數量": "{:,.0f}", "成交價": "{:,.2f}", "已實現損益": "{:+,.0f}",
        }).map(pnl_css, subset=["已實現損益"]),
            width="stretch", hide_index=True)
