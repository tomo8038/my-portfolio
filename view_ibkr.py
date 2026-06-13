"""
view_ibkr.py — IBKR 獨立檢視器(對映嘉信 P4b 的 view_schwab.py)

直接讀 ibkr.db,不碰 portfolio.db,純粹檢查 IBKR 資料正確性。
四分頁:總覽 / 持倉 / 已實現損益 / 每日淨值。

用法: streamlit run view_ibkr.py -- ibkr.db
       (沒指定就預設讀同目錄 ibkr.db)
"""
from __future__ import annotations

import sqlite3
import sys

import pandas as pd
import streamlit as st

st.set_page_config(page_title="IBKR 檢視器", layout="wide")

# `streamlit run view_ibkr.py -- ibkr.db` → DB 路徑在 "--" 之後
args = sys.argv[1:]
DB = args[0] if args else "ibkr.db"


@st.cache_data
def load(db: str):
    con = sqlite3.connect(db)
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    pos = pd.read_sql("SELECT symbol, qty, avg_cost, cost_basis "
                      "FROM positions_current WHERE broker='ibkr' ORDER BY cost_basis DESC", con)
    rp = pd.read_sql("SELECT date, symbol, qty, proceeds, cost, pnl "
                     "FROM realized_pnl WHERE broker='ibkr' ORDER BY date", con)
    dn = pd.read_sql("SELECT date, cash, holdings, networth "
                     "FROM daily_networth_native WHERE broker='ibkr' ORDER BY date", con)
    txn = pd.read_sql("SELECT date, kind, symbol, qty, price, amount, description "
                      "FROM transactions WHERE broker='ibkr' ORDER BY date DESC", con)
    con.close()
    return meta, pos, rp, dn, txn


try:
    meta, pos, rp, dn, txn = load(DB)
except Exception as ex:
    st.error(f"讀不到 {DB}:{ex}\n請先 `python build_history_ibkr.py <csv> {DB}`")
    st.stop()

st.title("🟦 IBKR 帳戶檢視器(USD 原幣)")
st.caption(f"資料來源:{DB} · 幣別 {meta.get('currency', 'USD')} · 純檢查用,未併入 portfolio.db")

tab1, tab2, tab3, tab4 = st.tabs(["總覽", "持倉", "已實現損益", "每日淨值"])

with tab1:
    cash = float(meta.get("cash", 0))
    nw = float(meta.get("final_networth", 0))
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("淨值(成本基礎)", f"${nw:,.2f}")
    c2.metric("現金", f"${cash:,.2f}")
    c3.metric("已實現損益", f"${float(meta.get('realized', 0)):,.2f}")
    c4.metric("外部投入", f"${float(meta.get('ext_cash', 0)) + float(meta.get('ext_inkind', 0)):,.2f}")
    st.divider()
    st.subheader("交易明細(全)")
    st.dataframe(txn, use_container_width=True, hide_index=True)

with tab2:
    st.subheader("目前持倉")
    if not pos.empty:
        view = pos.copy()
        view.columns = ["標的", "股數", "均價", "成本基礎"]
        st.dataframe(view.style.format(
            {"股數": "{:.4f}", "均價": "${:.4f}", "成本基礎": "${:,.2f}"}),
            use_container_width=True, hide_index=True)
        st.bar_chart(pos.set_index("symbol")["cost_basis"])
    else:
        st.info("無持倉")

with tab3:
    st.subheader("已實現損益(平倉)")
    if not rp.empty:
        tot = rp["pnl"].sum()
        win = (rp["pnl"] > 0).mean() * 100
        c1, c2, c3 = st.columns(3)
        c1.metric("累計已實現", f"${tot:,.2f}")
        c2.metric("平倉筆數", f"{len(rp)}")
        c3.metric("勝率", f"{win:.0f}%")
        view = rp.copy()
        view.columns = ["日期", "標的", "股數", "收回", "成本", "損益"]
        st.dataframe(view.style.format(
            {"股數": "{:.4f}", "收回": "${:,.2f}", "成本": "${:,.2f}", "損益": "${:+,.2f}"}),
            use_container_width=True, hide_index=True)
    else:
        st.info("尚無平倉")

with tab4:
    st.subheader("每日淨值曲線(USD)")
    if not dn.empty:
        dn2 = dn.copy()
        dn2["date"] = pd.to_datetime(dn2["date"])
        st.line_chart(dn2.set_index("date")[["networth", "holdings", "cash"]])
        st.caption("注:沙箱交付版市值為成本遞補;本機跑 build_history_ibkr.py "
                   "(有 yfinance)即補真實收盤價。")
        st.dataframe(dn.iloc[::-1], use_container_width=True, hide_index=True)
    else:
        st.info("無每日淨值")
