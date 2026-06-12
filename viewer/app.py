"""P2 檢視器 — 讀 portfolio.db,完整視覺化儀表板。

執行:streamlit run viewer/app.py
(請先跑過一次 python run.py 或 python run.py --mock)

P2 新增:
  * 分頁:總覽 / 資產配置 / 損益排行 / 股息現金流
  * 資產配置環圈圖,維度可切換(類別 / 券商 / 幣別 / 產業)
  * 損益貢獻排行(Top gainers / losers,金額或報酬率)
  * 股息現金流(配息行事曆、估算年化殖利率、月現金流長條圖)
  * 全域漲跌配色切換:台股「紅漲綠跌」↔ 美股「綠漲紅跌」
"""
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent.parent))

DB_PATH = Path(__file__).parent.parent / "portfolio.db"

st.set_page_config(page_title="資產整合儀表板", page_icon="📊", layout="wide")

# ---------- 全域設定:漲跌配色(P2) ----------
SCHEMES = {
    "台股(紅漲綠跌)": {"up": "#d23b3b", "down": "#2a9d5c", "metric": "inverse"},
    "美股(綠漲紅跌)": {"up": "#2a9d5c", "down": "#d23b3b", "metric": "normal"},
}
with st.sidebar:
    st.header("顯示設定")
    scheme_name = st.radio("漲跌配色", list(SCHEMES), index=0,
                           help="影響所有損益數字與圖表的紅綠方向")
    SCHEME = SCHEMES[scheme_name]
    st.caption("台股慣例:紅漲綠跌;美股慣例:綠漲紅跌。")
    st.divider()
    live_mode = st.toggle(
        "⚡ 即時模式", value=False,
        help="需先在另一個終端機執行 python watch.py(盤中)或 "
             "python watch.py --mock(模擬報價);KPI 每 2 秒自動刷新")

UP, DOWN, METRIC_COLOR = SCHEME["up"], SCHEME["down"], SCHEME["metric"]


def pnl_css(v: float) -> str:
    return f"color: {UP if v >= 0 else DOWN}"


# ---------- 讀取資料 ----------
if not DB_PATH.exists():
    st.warning("還沒有資料。請先在專案目錄執行:`python run.py` 或 `python run.py --mock`")
    st.stop()

con = sqlite3.connect(DB_PATH)
snap = con.execute(
    "SELECT ts, net_worth, invested, cash, cost FROM snapshots ORDER BY ts DESC LIMIT 1"
).fetchone()
if not snap:
    con.close()
    st.warning("資料庫存在但沒有快照。請先執行 `python run.py`。")
    st.stop()

ts, net_worth, invested, cash, cost = snap
unrealized = invested - cost
pct = unrealized / cost * 100 if cost else 0.0

pos = pd.read_sql(
    "SELECT broker, symbol, name, asset_class, industry, qty, avg_cost, "
    "last_price, ccy, market_value_native AS mv, "
    "unrealized_pnl_native AS pnl "
    "FROM positions_current ORDER BY mv DESC", con)
series = pd.read_sql(
    "SELECT date, net_worth, is_real FROM daily_networth ORDER BY date", con)
snaps_table = pd.read_sql(
    "SELECT substr(ts,1,16) AS 時間, net_worth AS 淨值, cash AS 現金 "
    "FROM snapshots ORDER BY ts DESC LIMIT 10", con)
div_cache = pd.read_sql("SELECT symbol, date, amount FROM dividend_cache", con)
realized = pd.read_sql(
    "SELECT symbol, qty, price, pnl, trade_date FROM realized_pnl "
    "ORDER BY trade_date", con)
flows = {d: a for d, a in con.execute(
    "SELECT trade_date, SUM(amount) FROM transactions "
    "WHERE txn_type IN ('DEPOSIT','WITHDRAW') GROUP BY trade_date")}
con.close()

if not pos.empty:
    pos["cost_value"] = pos["qty"] * pos["avg_cost"]
    pos["ret_pct"] = (pos["pnl"] / pos["cost_value"] * 100).where(
        pos["cost_value"] != 0, 0.0)
    pos["industry"] = pos["industry"].fillna("").replace("", "其他")

BROKER_NAMES = {"sinopac": "永豐金", "schwab": "嘉信", "ibkr": "盈透"}

# ---------- 標題 + KPI ----------
st.title("資產整合儀表板")
st.caption(f"資料時間:{ts} · 基準幣別 TWD · 配色:{scheme_name}")

if not live_mode:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("總資產淨值", f"NT$ {net_worth:,.0f}")
    c2.metric("投資市值", f"NT$ {invested:,.0f}")
    c3.metric("現金", f"NT$ {cash:,.0f}")
    c4.metric("未實現損益", f"NT$ {unrealized:+,.0f}", f"{pct:+.1f}%",
              delta_color=METRIC_COLOR)
else:
    @st.fragment(run_every=2)
    def _live_kpis():
        """⚡ 即時 KPI:每 2 秒讀 live_quotes(watch.py 寫入),
        以即時價重算市值;沒有報價的標的以快照現價遞補。"""
        lcon = sqlite3.connect(DB_PATH)
        lcon.execute("PRAGMA busy_timeout=3000")
        lq = {s: (p, t) for s, p, t in
              lcon.execute("SELECT symbol, price, ts FROM live_quotes")}
        lcon.close()

        if not lq:
            st.warning("⚡ 即時模式已開,但收不到報價 — 請在另一個終端機執行 "
                       "`python watch.py`(盤中)或 `python watch.py --mock`。"
                       "以下為最近一次快照的數字。")
            l_inv, l_unrl, hit = invested, unrealized, 0
        else:
            l_inv = l_cost = 0.0
            hit = 0
            for _, r in pos.iterrows():
                px = r["last_price"]
                if r["symbol"] in lq:
                    px = lq[r["symbol"]][0]
                    hit += 1
                l_inv += r["qty"] * px
                l_cost += r["qty"] * r["avg_cost"]
            l_unrl = l_inv - l_cost
        l_nw = l_inv + cash
        l_pct = l_unrl / cost * 100 if cost else 0.0

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("總資產淨值 ⚡", f"NT$ {l_nw:,.0f}",
                  f"{l_nw - net_worth:+,.0f} vs 快照", delta_color=METRIC_COLOR)
        c2.metric("投資市值 ⚡", f"NT$ {l_inv:,.0f}")
        c3.metric("現金", f"NT$ {cash:,.0f}")
        c4.metric("未實現損益 ⚡", f"NT$ {l_unrl:+,.0f}", f"{l_pct:+.1f}%",
                  delta_color=METRIC_COLOR)

        if lq:
            from datetime import datetime as _dt
            last_ts = max(t for _, t in lq.values())
            stale = ""
            try:
                age = (_dt.now() - _dt.fromisoformat(last_ts[:19])).total_seconds()
                if age > 60:
                    stale = f" · ⚠ 報價已 {age:,.0f} 秒未更新(watch.py 是否還在跑?非交易時段亦無成交)"
            except ValueError:
                pass
            st.caption(f"⚡ 即時報價 {hit}/{len(pos)} 檔 · 最後成交 {last_ts}{stale}"
                       " · 現金為快照值(盤中不變)")

    _live_kpis()

tab_main, tab_alloc, tab_pnl, tab_div, tab_real = st.tabs(
    ["📈 總覽", "🧩 資產配置", "🏆 損益排行", "💰 股息現金流", "💼 已實現損益"])

# ======================================================================
# 分頁 1:總覽(P1 的淨值曲線 + 持倉明細)
# ======================================================================
with tab_main:
    st.subheader("淨值走勢")
    if len(series) >= 2:
        n_real = int(series["is_real"].sum())
        n_est = len(series) - n_real

        from core.performance import compute_performance
        perf = compute_performance(
            list(series[["date", "net_worth"]].itertuples(index=False,
                                                          name=None)),
            flows)

        a, b, c, d = st.columns(4)
        a.metric("期間變化(含出入金)",
                 f"NT$ {perf['end_nv'] - perf['start_nv']:+,.0f}",
                 f"{perf['simple_return']:+.2%}", delta_color=METRIC_COLOR)
        b.metric("TWR 報酬率(不含出入金)",
                 f"{perf['twr']:+.2%}" if perf["twr"] is not None else "—",
                 help="時間加權報酬:剔除出入金影響的真實投資績效(基金淨值法)")
        c.metric("年化 TWR",
                 f"{perf['twr_annualized']:+.2%}"
                 if perf["twr_annualized"] is not None else "—",
                 help="期間滿 30 天才顯示年化值,避免短期數字誤導")
        d.metric("資料點", f"{len(series)} 天",
                 f"真實 {n_real} · 回補 {n_est}", delta_color="off")

        if perf["net_flow"]:
            st.caption(f"期間淨出入金 NT$ {perf['net_flow']:+,.0f}"
                       "(已自 TWR 剔除)· 出入金請用 flows.py 補登,"
                       "TWR 與曲線才準確")

        line = alt.Chart(series).mark_line(color="#4c78a8").encode(
            x=alt.X("date:T", title=None),
            y=alt.Y("net_worth:Q", title="淨值 (TWD)",
                    scale=alt.Scale(zero=False)),
            tooltip=[alt.Tooltip("date:T", title="日期"),
                     alt.Tooltip("net_worth:Q", title="淨值", format=",.0f")],
        )
        dots = alt.Chart(series[series["is_real"] == 1]).mark_point(
            color="#4c78a8", filled=True, size=60).encode(
            x="date:T", y="net_worth:Q",
            tooltip=[alt.Tooltip("date:T", title="真實快照"),
                     alt.Tooltip("net_worth:Q", title="淨值", format=",.0f")])
        st.altair_chart((line + dots).properties(height=320),
                        width="stretch")
        st.caption("實心點=真實快照(每次執行 run.py);線段其餘部分為回補估計。"
                   "真實快照日永不被估計值覆蓋。")
    else:
        st.info("淨值曲線需要至少 2 筆紀錄。每次執行 `python run.py` 會新增一筆真實快照,"
                "兩筆之後、回補引擎會自動把中間的每日空白補齊。")

    st.divider()
    left, right = st.columns([1.4, 1])

    with left:
        st.subheader("持倉明細")
        if pos.empty:
            st.info("目前沒有持倉。")
        else:
            show = pos[["symbol", "name", "asset_class", "industry", "qty",
                        "avg_cost", "last_price", "mv", "pnl", "ret_pct"]]
            show.columns = ["代號", "名稱", "類別", "產業", "股數", "均價",
                            "現價", "市值", "未實現損益", "報酬率%"]
            styled = show.style.format({
                "股數": "{:,.0f}", "均價": "{:,.2f}", "現價": "{:,.2f}",
                "市值": "NT$ {:,.0f}", "未實現損益": "NT$ {:+,.0f}",
                "報酬率%": "{:+.1f}%",
            }).map(pnl_css, subset=["未實現損益", "報酬率%"])
            st.dataframe(styled, width="stretch", hide_index=True)

    with right:
        st.subheader("快照紀錄")
        st.dataframe(
            snaps_table.style.format({"淨值": "NT$ {:,.0f}",
                                      "現金": "NT$ {:,.0f}"}),
            width="stretch", hide_index=True)

# ======================================================================
# 分頁 2:資產配置(P2)— 環圈圖,維度可切換
# ======================================================================
with tab_alloc:
    if pos.empty and cash <= 0:
        st.info("目前沒有持倉與現金,無法繪製配置圖。")
    else:
        dim = st.radio("配置維度", ["類別", "券商", "幣別", "產業"],
                       horizontal=True)
        include_cash = st.toggle("含現金", value=True,
                                 help="現金在「券商/產業」維度以獨立「現金」區塊呈現")

        if pos.empty:
            alloc = pd.DataFrame(columns=["label", "value"])
        elif dim == "類別":
            g = pos.groupby("asset_class")["mv"].sum()
            label_map = {"equity": "個股", "etf": "ETF", "cash": "現金"}
            alloc = pd.DataFrame({
                "label": [label_map.get(k, k) for k in g.index],
                "value": g.values})
        elif dim == "券商":
            g = pos.groupby("broker")["mv"].sum()
            alloc = pd.DataFrame({
                "label": [BROKER_NAMES.get(k, k) for k in g.index],
                "value": g.values})
        elif dim == "幣別":
            g = pos.groupby("ccy")["mv"].sum()
            alloc = pd.DataFrame({"label": g.index, "value": g.values})
        else:  # 產業
            g = pos.groupby("industry")["mv"].sum()
            alloc = pd.DataFrame({"label": g.index, "value": g.values})

        if include_cash and cash > 0:
            cash_label = "TWD 現金" if dim == "幣別" else "現金"
            alloc = pd.concat([alloc, pd.DataFrame(
                [{"label": cash_label, "value": float(cash)}])],
                ignore_index=True)

        alloc = alloc[alloc["value"] > 0].sort_values("value", ascending=False)
        total = alloc["value"].sum()
        alloc["pct"] = alloc["value"] / total * 100

        cL, cR = st.columns([1.2, 1])
        with cL:
            donut = alt.Chart(alloc).mark_arc(
                innerRadius=70, outerRadius=130, cornerRadius=3, padAngle=0.01,
            ).encode(
                theta=alt.Theta("value:Q"),
                color=alt.Color("label:N", title=dim,
                                scale=alt.Scale(scheme="tableau10"),
                                sort=alt.EncodingSortField(
                                    "value", op="sum", order="descending")),
                tooltip=[alt.Tooltip("label:N", title=dim),
                         alt.Tooltip("value:Q", title="金額", format=",.0f"),
                         alt.Tooltip("pct:Q", title="占比", format=".1f")],
            ).properties(height=340)
            st.altair_chart(donut, width="stretch")
        with cR:
            tbl = alloc.rename(columns={
                "label": dim, "value": "金額", "pct": "占比%"})
            st.dataframe(
                tbl.style.format({"金額": "NT$ {:,.0f}", "占比%": "{:.1f}%"}),
                width="stretch", hide_index=True)
            if dim == "券商":
                st.caption("註:現金未拆分到各券商(單一券商階段以總額呈現),"
                           "P4 多券商接入後改依券商歸屬。")

# ======================================================================
# 分頁 3:損益貢獻排行(P2)— Top gainers / losers
# ======================================================================
with tab_pnl:
    if pos.empty:
        st.info("目前沒有持倉。")
    else:
        mode = st.radio("排序依據", ["金額(NT$)", "報酬率(%)"], horizontal=True)
        col = "pnl" if mode.startswith("金額") else "ret_pct"

        rank = pos[["symbol", "name", "broker", "pnl", "ret_pct"]].copy()
        rank["顯示名"] = rank["symbol"] + " " + rank["name"]
        rank["券商"] = rank["broker"].map(lambda b: BROKER_NAMES.get(b, b))
        rank = rank.sort_values(col, ascending=False)

        g1, g2 = st.columns(2)
        winners = rank[rank[col] > 0]
        losers = rank[rank[col] < 0]
        g1.metric("獲利檔數", f"{len(winners)} 檔",
                  f"+NT$ {winners['pnl'].sum():,.0f}",
                  delta_color=METRIC_COLOR)
        g2.metric("虧損檔數", f"{len(losers)} 檔",
                  f"-NT$ {abs(losers['pnl'].sum()):,.0f}",
                  delta_color=METRIC_COLOR)

        fmt = ",.0f" if col == "pnl" else "+.1f"
        bars = alt.Chart(rank).mark_bar(cornerRadius=2).encode(
            x=alt.X(f"{col}:Q",
                    title="未實現損益 (NT$)" if col == "pnl" else "報酬率 (%)"),
            y=alt.Y("顯示名:N", sort="-x", title=None),
            color=alt.condition(alt.datum[col] >= 0,
                                alt.value(UP), alt.value(DOWN)),
            tooltip=[alt.Tooltip("顯示名:N", title="標的"),
                     alt.Tooltip("券商:N"),
                     alt.Tooltip("pnl:Q", title="損益", format="+,.0f"),
                     alt.Tooltip("ret_pct:Q", title="報酬率%", format="+.1f")],
        ).properties(height=max(220, 44 * len(rank)))
        st.altair_chart(bars, width="stretch")
        st.caption("依「目前持倉的未實現損益」排序;已實現損益分析屬 P3 範疇。")

# ======================================================================
# 分頁 4:股息現金流(P2)— 行事曆、殖利率、月現金流
# ======================================================================
with tab_div:
    if pos.empty:
        st.info("目前沒有持倉。")
    elif div_cache.empty:
        st.info("尚無股息資料。執行 `python run.py`(需網路)會自動抓取"
                "持倉標的近 400 天的除息紀錄並存入本地快取。")
    else:
        today = date.today()
        ttm_cut = (today - timedelta(days=365)).isoformat()
        div = div_cache.merge(
            pos[["symbol", "name", "qty", "last_price"]], on="symbol")
        div["est_cash"] = div["amount"] * div["qty"]   # 每股配息 × 目前持股

        # --- 摘要 KPI(TTM 口徑) ---
        ttm = div[(div["date"] > ttm_cut) & (div["date"] <= today.isoformat())]
        ttm_total = ttm["est_cash"].sum()
        yld = ttm_total / invested * 100 if invested else 0

        k1, k2, k3 = st.columns(3)
        k1.metric("近 12 個月股息(估算)", f"NT$ {ttm_total:,.0f}")
        k2.metric("組合估算殖利率", f"{yld:.2f}%",
                  help="近 12 個月每股配息 × 目前持股 ÷ 投資市值")
        k3.metric("月均現金流(估算)", f"NT$ {ttm_total / 12:,.0f}")

        st.caption("⚠ 估算口徑:以「歷史每股配息 × **目前**持股」推算,"
                   "非帳上實收金額(當時持股可能不同;實收對帳屬 P3)。")

        # --- 月現金流長條圖(近 12 個月) ---
        st.subheader("月現金流(近 12 個月)")
        months = pd.period_range(end=pd.Timestamp(today), periods=12, freq="M")
        m = ttm.copy()
        m["month"] = pd.to_datetime(m["date"]).dt.to_period("M")
        monthly = (m.groupby("month")["est_cash"].sum()
                   .reindex(months, fill_value=0.0).reset_index())
        monthly.columns = ["month", "cash"]
        monthly["month"] = monthly["month"].astype(str)
        bar = alt.Chart(monthly).mark_bar(color="#4c78a8", cornerRadius=2).encode(
            x=alt.X("month:N", title=None, sort=None),
            y=alt.Y("cash:Q", title="估算股息 (NT$)"),
            tooltip=[alt.Tooltip("month:N", title="月份"),
                     alt.Tooltip("cash:Q", title="估算股息", format=",.0f")],
        ).properties(height=260)
        st.altair_chart(bar, width="stretch")

        st.divider()
        cA, cB = st.columns([1.2, 1])

        # --- 配息行事曆(最近事件在前) ---
        with cA:
            st.subheader("配息行事曆")
            cal = div.sort_values("date", ascending=False)[
                ["date", "symbol", "name", "amount", "qty", "est_cash"]].head(30)
            cal.columns = ["除息日", "代號", "名稱", "每股配息",
                           "目前持股", "估算金額"]
            st.dataframe(cal.style.format({
                "每股配息": "{:,.2f}", "目前持股": "{:,.0f}",
                "估算金額": "NT$ {:,.0f}"}),
                width="stretch", hide_index=True)

        # --- 各標的殖利率 ---
        with cB:
            st.subheader("各標的估算年化殖利率")
            per = (ttm.groupby(["symbol", "name"])
                   .agg(年配息每股=("amount", "sum")).reset_index()
                   .merge(pos[["symbol", "last_price", "qty"]], on="symbol"))
            per["殖利率%"] = per["年配息每股"] / per["last_price"] * 100
            per["年估算股息"] = per["年配息每股"] * per["qty"]
            per = per.sort_values("殖利率%", ascending=False)[
                ["symbol", "name", "年配息每股", "殖利率%", "年估算股息"]]
            per.columns = ["代號", "名稱", "TTM每股配息", "殖利率%", "年估算股息"]
            st.dataframe(per.style.format({
                "TTM每股配息": "{:,.2f}", "殖利率%": "{:.2f}%",
                "年估算股息": "NT$ {:,.0f}"}),
                width="stretch", hide_index=True)

# ======================================================================
# 分頁 5:已實現損益(P3)— 累計曲線、標的排行、月別統計
# ======================================================================
with tab_real:
    if realized.empty:
        st.info("尚無已實現損益紀錄。每次執行 `python run.py` 會自動抓取"
                "近 60 天的平倉損益(永豐 list_profit_loss)並累積進本地資料庫;"
                "之後即使超過券商查詢區間,歷史紀錄仍會保留。")
    else:
        r = realized.copy()
        r["cum"] = r["pnl"].cumsum()
        total = r["pnl"].sum()
        wins = (r["pnl"] > 0).sum()

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("累計已實現損益", f"NT$ {total:+,.0f}",
                  delta_color=METRIC_COLOR)
        k2.metric("平倉筆數", f"{len(r)} 筆")
        k3.metric("勝率", f"{wins / len(r) * 100:.0f}%",
                  f"獲利 {wins} · 虧損 {(r['pnl'] < 0).sum()}",
                  delta_color="off")
        k4.metric("區間", f"{r['trade_date'].iloc[0]} ~ "
                          f"{r['trade_date'].iloc[-1]}")

        cL, cR = st.columns([1.3, 1])
        with cL:
            st.subheader("累計已實現損益")
            cum_line = alt.Chart(r).mark_line(
                color=UP if total >= 0 else DOWN, point=True,
            ).encode(
                x=alt.X("trade_date:T", title=None),
                y=alt.Y("cum:Q", title="累計損益 (NT$)"),
                tooltip=[alt.Tooltip("trade_date:T", title="日期"),
                         alt.Tooltip("symbol:N", title="標的"),
                         alt.Tooltip("pnl:Q", title="該筆損益", format="+,.0f"),
                         alt.Tooltip("cum:Q", title="累計", format="+,.0f")],
            ).properties(height=280)
            zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(
                strokeDash=[4, 4], color="#888").encode(y="y:Q")
            st.altair_chart(cum_line + zero, width="stretch")

        with cR:
            st.subheader("各標的已實現損益")
            by_sym = (r.groupby("symbol")["pnl"].sum()
                      .sort_values(ascending=False).reset_index())
            sym_bar = alt.Chart(by_sym).mark_bar(cornerRadius=2).encode(
                x=alt.X("pnl:Q", title="已實現損益 (NT$)"),
                y=alt.Y("symbol:N", sort="-x", title=None),
                color=alt.condition(alt.datum.pnl >= 0,
                                    alt.value(UP), alt.value(DOWN)),
                tooltip=[alt.Tooltip("symbol:N", title="標的"),
                         alt.Tooltip("pnl:Q", title="損益", format="+,.0f")],
            ).properties(height=280)
            st.altair_chart(sym_bar, width="stretch")

        st.subheader("月別已實現損益")
        m = r.copy()
        m["month"] = pd.to_datetime(m["trade_date"]).dt.to_period("M").astype(str)
        monthly_r = m.groupby("month")["pnl"].sum().reset_index()
        mon_bar = alt.Chart(monthly_r).mark_bar(cornerRadius=2).encode(
            x=alt.X("month:N", title=None, sort=None),
            y=alt.Y("pnl:Q", title="已實現損益 (NT$)"),
            color=alt.condition(alt.datum.pnl >= 0,
                                alt.value(UP), alt.value(DOWN)),
            tooltip=[alt.Tooltip("month:N", title="月份"),
                     alt.Tooltip("pnl:Q", title="損益", format="+,.0f")],
        ).properties(height=220)
        st.altair_chart(mon_bar, width="stretch")

        st.subheader("平倉明細")
        detail = r.sort_values("trade_date", ascending=False)[
            ["trade_date", "symbol", "qty", "price", "pnl"]]
        detail.columns = ["日期", "代號", "數量", "成交價", "已實現損益"]
        st.dataframe(detail.style.format({
            "數量": "{:,.0f}", "成交價": "{:,.2f}",
            "已實現損益": "NT$ {:+,.0f}",
        }).map(pnl_css, subset=["已實現損益"]),
            width="stretch", hide_index=True)
        st.caption("來源:券商平倉損益查詢(損益金額以券商回報為準,含費稅;"
                   "數量單位依券商回報)。每次 run.py 抓近 60 天並去重累積,"
                   "歷史紀錄一旦入庫即永久保留。")
