"""嘉信歷史檢視器 — P4b。直接看 schwab.db,不需接 portfolio.db。

用法:
  streamlit run view_schwab.py

(本機抓過真實市價後,未實現損益、市值曲線才會反映真實行情;
 沙箱無網路時為成本遞補,曲線呈現「投入成本累積」形狀。)
"""
import sqlite3
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

DB = Path(__file__).parent / "schwab.db"

st.set_page_config(page_title="嘉信歷史(CSV 回放)", layout="wide")
st.title("嘉信證券 — 開戶至今(交易明細回放)")

con = sqlite3.connect(DB)

snap = con.execute(
    "SELECT ts, net_worth, invested, cash, cost FROM snapshots").fetchone()
ts, nw, inv, cash, cost = snap
unreal = inv - cost

c1, c2, c3, c4 = st.columns(4)
c1.metric("總資產淨值", f"${nw:,.0f}")
c2.metric("投資市值", f"${inv:,.0f}")
c3.metric("現金", f"${cash:,.2f}")
c4.metric("未實現損益", f"${unreal:+,.0f}",
          help="成本遞補時為 0;本機抓真實市價後會反映行情")
st.caption(f"資料截至 {ts[:10]} · 幣別 USD · 來源:嘉信交易明細 CSV 回放")

tab1, tab2, tab3, tab4 = st.tabs(
    ["📈 淨值曲線", "📋 持倉", "💼 已實現損益", "🧾 交易明細"])

with tab1:
    df = pd.read_sql("SELECT date, net_worth, is_real FROM daily_networth "
                     "ORDER BY date", con)
    st.subheader(f"每日淨值（{len(df)} 天）")
    line = alt.Chart(df).mark_line(color="#4c78a8").encode(
        x=alt.X("date:T", title=None),
        y=alt.Y("net_worth:Q", title="淨值 (USD)",
                scale=alt.Scale(zero=False)),
        tooltip=[alt.Tooltip("date:T", title="日期"),
                 alt.Tooltip("net_worth:Q", title="淨值", format=",.0f")])
    st.altair_chart(line.properties(height=360), width="stretch")
    st.caption("沙箱無網路時為成本遞補曲線（投入成本累積）；"
               "本機 `python build_history.py` 抓真實價後即為市值曲線。")

with tab2:
    pos = pd.read_sql(
        "SELECT symbol 代號, name 名稱, asset_class 類別, qty 股數, "
        "avg_cost 均價, last_price 現價, market_value_native 市值, "
        "unrealized_pnl_native 未實現損益 FROM positions_current "
        "ORDER BY market_value_native DESC", con)
    st.dataframe(pos.style.format({
        "股數": "{:,.4f}", "均價": "${:,.2f}", "現價": "${:,.2f}",
        "市值": "${:,.0f}", "未實現損益": "${:+,.0f}"}),
        width="stretch", hide_index=True)

with tab3:
    r = pd.read_sql("SELECT trade_date, symbol, qty, price, pnl "
                    "FROM realized_pnl ORDER BY trade_date", con)
    if r.empty:
        st.info("無已實現損益紀錄")
    else:
        r["cum"] = r["pnl"].cumsum()
        total = r["pnl"].sum()
        wins = (r["pnl"] > 0).sum()
        k1, k2, k3 = st.columns(3)
        k1.metric("累計已實現損益", f"${total:+,.0f}")
        k2.metric("平倉筆數", f"{len(r)}")
        k3.metric("勝率", f"{wins/len(r)*100:.0f}%")
        st.altair_chart(
            alt.Chart(r).mark_line(
                color="#54a24b" if total >= 0 else "#e45756",
                point=False).encode(
                x=alt.X("trade_date:T", title=None),
                y=alt.Y("cum:Q", title="累計已實現 (USD)"),
                tooltip=["trade_date:T", "symbol:N",
                         alt.Tooltip("pnl:Q", format=",.0f")]
            ).properties(height=280), width="stretch")
        # 各標的彙總
        agg = (r.groupby("symbol")["pnl"].sum().reset_index()
               .sort_values("pnl", ascending=False))
        agg.columns = ["標的", "已實現損益"]
        st.dataframe(agg.style.format({"已實現損益": "${:+,.0f}"}),
                     width="stretch", hide_index=True)

with tab4:
    t = pd.read_sql(
        "SELECT trade_date 日期, txn_type 類型, symbol 代號, qty 股數, "
        "price 價格, amount 金額 FROM transactions "
        "ORDER BY trade_date DESC, rowid DESC LIMIT 300", con)
    st.caption("最近 300 筆")
    st.dataframe(t.style.format({
        "股數": "{:,.4f}", "價格": "${:,.2f}", "金額": "${:,.2f}"}),
        width="stretch", hide_index=True)

con.close()
