"""資產整合儀表板 — 深色科技感版(V1.1 UI 改版)。

執行:streamlit run viewer/app.py
(請先跑過一次 python rebuild_all.py 產生 / 更新 portfolio.db)

本檔只「讀」portfolio.db,不寫入。沿用既有資料表:
  snapshots / daily_networth / positions_current / transactions /
  dividend_cache / realized_pnl / live_quotes
所有讀取都做了容錯:某張表不存在或為空時,該區塊自動降級顯示,
不會讓整頁崩潰。視覺層全面重做為深色科技風,資料邏輯與 V1.0 一致。
"""
import sqlite3
import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent.parent))
DB_PATH = Path(__file__).parent.parent / "portfolio.db"

st.set_page_config(page_title="資產整合儀表板", page_icon="◆",
                   layout="wide", initial_sidebar_state="collapsed")

# ======================================================================
# 設計 tokens(深色科技風)
# ======================================================================
BG     = "#0a0e1a"
PANEL  = "#111726"
PANEL2 = "#161d2e"
LINE   = "#222b42"
TXT    = "#e8eef9"
DIM    = "#7e8aa3"
ACCENT = "#29e0c4"   # 主強調(青)
ACCENT2 = "#6c7bff"  # 次強調(靛)
GRID   = "#1c2438"

SCHEMES = {
    "台股 · 紅漲綠跌": {"up": "#ff5470", "down": "#26d6a4", "metric": "inverse"},
    "美股 · 綠漲紅跌": {"up": "#26d6a4", "down": "#ff5470", "metric": "normal"},
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
# 全域 CSS — 把 Streamlit 染成深色科技風
# ======================================================================
st.markdown(f"""
<style>
:root {{
  --bg:{BG}; --panel:{PANEL}; --panel2:{PANEL2}; --line:{LINE};
  --txt:{TXT}; --dim:{DIM}; --accent:{ACCENT}; --accent2:{ACCENT2};
}}
.stApp {{
  background:
    radial-gradient(1200px 600px at 80% -10%, rgba(108,123,255,.10), transparent 60%),
    radial-gradient(1000px 500px at 0% 0%, rgba(41,224,196,.08), transparent 55%),
    {BG};
  color: var(--txt);
}}
.block-container {{ padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1280px; }}
#MainMenu, footer, header[data-testid="stHeader"] {{ visibility: hidden; }}

h1,h2,h3,h4 {{ color: var(--txt); letter-spacing:.3px; font-weight:700; }}
.stApp, .stMarkdown, p, span, div {{ font-feature-settings:"tnum" 1, "cv01" 1; }}

/* ---- 頂部標題列 ---- */
.hero {{
  display:flex; align-items:flex-end; justify-content:space-between;
  border-bottom:1px solid var(--line); padding-bottom:14px; margin-bottom:22px;
}}
.hero .brand {{ display:flex; align-items:center; gap:12px; }}
.hero .mark {{
  width:34px; height:34px; border-radius:9px;
  background:linear-gradient(135deg,var(--accent),var(--accent2));
  box-shadow:0 0 18px rgba(41,224,196,.45); position:relative;
}}
.hero .mark::after {{
  content:""; position:absolute; inset:8px; border-radius:5px;
  background:var(--bg);
}}
.hero h1 {{ font-size:1.45rem; margin:0; }}
.hero .sub {{ color:var(--dim); font-size:.8rem; margin-top:3px; }}
.pill {{
  font-size:.72rem; color:var(--accent); border:1px solid rgba(41,224,196,.35);
  background:rgba(41,224,196,.07); border-radius:999px; padding:4px 11px;
}}

/* ---- KPI 卡 ---- */
.kpis {{ display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin-bottom:8px; }}
.kpi {{
  background:linear-gradient(180deg,var(--panel2),var(--panel));
  border:1px solid var(--line); border-radius:16px; padding:16px 18px;
  position:relative; overflow:hidden;
}}
.kpi::before {{
  content:""; position:absolute; left:0; top:0; bottom:0; width:3px;
  background:linear-gradient(var(--accent),var(--accent2)); opacity:.85;
}}
.kpi .label {{ color:var(--dim); font-size:.78rem; letter-spacing:.4px; }}
.kpi .value {{ font-size:1.62rem; font-weight:700; margin-top:6px; line-height:1.1;
  font-variant-numeric:tabular-nums; }}
.kpi .delta {{ font-size:.82rem; margin-top:5px; font-variant-numeric:tabular-nums; }}
.kpi.glow::before {{ box-shadow:0 0 16px rgba(41,224,196,.6); }}

/* ---- Tabs ---- */
.stTabs [data-baseweb="tab-list"] {{ gap:4px; border-bottom:1px solid var(--line); }}
.stTabs [data-baseweb="tab"] {{
  background:transparent; color:var(--dim); border-radius:10px 10px 0 0;
  padding:9px 16px; font-weight:600;
}}
.stTabs [aria-selected="true"] {{
  color:var(--txt); background:var(--panel);
  border:1px solid var(--line); border-bottom:2px solid var(--accent);
}}

/* ---- Section 標題 ---- */
.sect {{ font-size:.95rem; font-weight:700; color:var(--txt);
  margin:6px 0 10px; display:flex; align-items:center; gap:8px; }}
.sect::before {{ content:""; width:6px; height:6px; border-radius:2px;
  background:var(--accent); box-shadow:0 0 8px var(--accent); }}

/* ---- DataFrame 深色微調 ---- */
[data-testid="stDataFrame"] {{ border:1px solid var(--line); border-radius:12px; }}
.stDataFrame, .stTable {{ background:var(--panel); }}

/* ---- 區塊卡片包裝 ---- */
.panel {{ background:var(--panel); border:1px solid var(--line);
  border-radius:16px; padding:16px 18px; }}
.caption {{ color:var(--dim); font-size:.76rem; }}
hr {{ border-color:var(--line); }}
[data-testid="stMetricValue"] {{ font-variant-numeric:tabular-nums; }}
</style>
""", unsafe_allow_html=True)


def chart_theme(c):
    """套用深色圖表外觀(transparent 背景、暗格線)。"""
    return (c.configure(background="transparent")
             .configure_view(stroke=None)
             .configure_axis(grid=True, gridColor=GRID, gridOpacity=.7,
                             domainColor=LINE, tickColor=LINE,
                             labelColor=DIM, titleColor=DIM,
                             labelFontSize=11, titleFontSize=11)
             .configure_legend(labelColor=DIM, titleColor=DIM)
             .configure_arc(stroke=BG, strokeWidth=2))


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


def q(con, sql, default=None):
    try:
        return pd.read_sql(sql, con)
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
con.close()

if not pos.empty:
    pos["cost_value"] = pos["qty"] * pos["avg_cost"]
    pos["ret_pct"] = (pos["pnl"] / pos["cost_value"] * 100).where(
        pos["cost_value"] != 0, 0.0)
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
            l_inv = float((pos.apply(
                lambda r: r["qty"] * lq.get(r["symbol"], r["last_price"]),
                axis=1)).sum())
            l_cost = float(pos["cost_value"].sum())
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
tab_ov, tab_alloc, tab_pnl, tab_div, tab_real = st.tabs(
    ["　總覽　", "　資產配置　", "　損益排行　", "　股息現金流　", "　已實現損益　"])

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
            stops=[alt.GradientStop(color="#0a0e1a", offset=0),
                   alt.GradientStop(color=ACCENT, offset=1)],
            x1=1, x2=1, y1=1, y2=0)
        area = alt.Chart(s).mark_area(
            line={"color": ACCENT, "strokeWidth": 2.4},
            color=grad, opacity=.55).encode(
            x=alt.X("date:T", title=None),
            y=alt.Y("net_worth:Q", title="淨值 (TWD)",
                    scale=alt.Scale(zero=False)),
            tooltip=[alt.Tooltip("date:T", title="日期"),
                     alt.Tooltip("net_worth:Q", title="淨值", format=",.0f")])
        dots = alt.Chart(s[s["is_real"] == 1]).mark_point(
            color=ACCENT, filled=True, size=55,
            stroke="#0a0e1a", strokeWidth=1.5).encode(
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
                        "avg_cost", "last_price", "mv", "pnl", "ret_pct"]].copy()
            show["broker"] = show["broker"].map(lambda b: BROKER_NAMES.get(b, b))
            show["asset_class"] = show["asset_class"].map(
                lambda c: CLASS_NAMES.get(c, c))
            show.columns = ["券商", "代號", "名稱", "類別", "股數", "均價",
                            "現價", "市值", "未實現損益", "報酬率%"]
            st.dataframe(show.style.format({
                "股數": "{:,.0f}", "均價": "{:,.2f}", "現價": "{:,.2f}",
                "市值": "{:,.0f}", "未實現損益": "{:+,.0f}", "報酬率%": "{:+.1f}%",
            }).map(pnl_css, subset=["未實現損益", "報酬率%"]),
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
            agg = (pos.groupby(dim_col)["mv"].sum()
                   .reset_index().rename(columns={dim_col: "key", "mv": "金額"}))
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
            if dim in ("券商", "幣別"):
                st.markdown('<div class="caption">註:跨幣別市值以各標的原幣計,'
                            '完整 TWD 換算以淨值快照為準。</div>',
                            unsafe_allow_html=True)

# ======================================================================
# 分頁 3:損益排行
# ======================================================================
with tab_pnl:
    if pos.empty:
        st.info("目前沒有持倉。")
    else:
        mode = st.radio("排序依據", ["金額 (NT$)", "報酬率 (%)"], horizontal=True)
        col = "pnl" if mode.startswith("金額") else "ret_pct"
        rank = pos[["symbol", "name", "broker", "pnl", "ret_pct"]].copy()
        rank["標的"] = rank["symbol"] + " " + rank["name"].fillna("")
        rank = rank.sort_values(col, ascending=False)

        win = rank[rank[col] > 0]; los = rank[rank[col] < 0]
        cards = "".join([
            kpi_card("獲利檔數", f"{len(win)} 檔",
                     f'<div class="delta" style="color:{UP}">'
                     f'+NT$ {win["pnl"].sum():,.0f}</div>'),
            kpi_card("虧損檔數", f"{len(los)} 檔",
                     f'<div class="delta" style="color:{DOWN}">'
                     f'-NT$ {abs(los["pnl"].sum()):,.0f}</div>'),
        ])
        st.markdown(f'<div class="kpis" style="grid-template-columns:repeat(2,1fr);'
                    f'max-width:520px">{cards}</div>', unsafe_allow_html=True)
        st.write("")
        bars = alt.Chart(rank).mark_bar(cornerRadius=4, height=18).encode(
            x=alt.X(f"{col}:Q",
                    title="未實現損益 (NT$)" if col == "pnl" else "報酬率 (%)"),
            y=alt.Y("標的:N", sort="-x", title=None),
            color=alt.condition(alt.datum[col] >= 0,
                                alt.value(UP), alt.value(DOWN)),
            tooltip=[alt.Tooltip("標的:N"),
                     alt.Tooltip("pnl:Q", title="損益", format="+,.0f"),
                     alt.Tooltip("ret_pct:Q", title="報酬率%", format="+.1f")],
        ).properties(height=max(220, 40 * len(rank)))
        st.altair_chart(chart_theme(bars), width="stretch")

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
                         '<div class="delta" style="color:#7e8aa3">'
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
