"""FastAPI 伺服器 — React 專業版儀表板的資料來源(P3)。

執行(在 my-portfolio 目錄):
  pip install fastapi uvicorn          # 第一次
  uvicorn api.server:app --port 8787
  → 瀏覽器開 http://127.0.0.1:8787    (React 儀表板)
  → JSON 端點在 http://127.0.0.1:8787/api/...

端點一覽:
  GET /api/overview     KPI:淨值/市值/現金/未實現損益 + TWR 績效
  GET /api/networth     每日淨值序列(含 is_real 標記)
  GET /api/positions    目前持倉明細
  GET /api/allocation   資產配置(?dim=class|broker|ccy|industry,含現金)
  GET /api/dividends    股息:TTM 統計、月現金流、配息事件
  GET /api/realized     已實現損益:累計、各標的、明細
  GET /api/live         即時報價(watch.py 寫入的 live_quotes)

設計:每個 request 各開一條 SQLite 連線(讀取輕量、避免跨執行緒問題);
資料庫是 WAL 模式,與 run.py / watch.py 並行不互鎖。仍然不需要 Docker。
"""
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from core.performance import compute_performance  # noqa: E402

ROOT = Path(__file__).parent.parent
DB_PATH = ROOT / "portfolio.db"
STATIC_DIR = ROOT / "viewer-react" / "dist"

app = FastAPI(title="my-portfolio API", version="P3")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


def _con() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA busy_timeout=3000")
    con.row_factory = sqlite3.Row
    return con


# ---------- 端點 ----------

@app.get("/api/overview")
def overview():
    con = _con()
    try:
        s = con.execute("SELECT ts, net_worth, invested, cash, cost "
                        "FROM snapshots ORDER BY ts DESC LIMIT 1").fetchone()
        if not s:
            return {"ready": False,
                    "hint": "請先執行 python run.py(或 --mock)"}
        series = con.execute("SELECT date, net_worth FROM daily_networth "
                             "ORDER BY date").fetchall()
        flows = {}
        for r in con.execute(
            "SELECT trade_date, SUM(amount) AS a FROM transactions "
            "WHERE txn_type IN ('DEPOSIT','WITHDRAW') GROUP BY trade_date"):
            flows[r["trade_date"]] = float(r["a"])
        perf = compute_performance(
            [(r["date"], r["net_worth"]) for r in series], flows)
        unrl = s["invested"] - s["cost"]
        return {
            "ready": True, "ts": s["ts"], "base_ccy": "TWD",
            "net_worth": s["net_worth"], "invested": s["invested"],
            "cash": s["cash"], "cost": s["cost"],
            "unrealized": unrl,
            "unrealized_pct": unrl / s["cost"] * 100 if s["cost"] else 0,
            "performance": perf,
        }
    finally:
        con.close()


@app.get("/api/networth")
def networth():
    con = _con()
    try:
        return [{"date": r["date"], "net_worth": r["net_worth"],
                 "is_real": r["is_real"]}
                for r in con.execute("SELECT date, net_worth, is_real "
                                     "FROM daily_networth ORDER BY date")]
    finally:
        con.close()


@app.get("/api/positions")
def positions():
    con = _con()
    try:
        out = []
        for r in con.execute(
            "SELECT broker, symbol, name, asset_class, industry, qty, "
            "avg_cost, last_price, ccy, market_value_native AS mv, "
            "unrealized_pnl_native AS pnl FROM positions_current "
                "ORDER BY mv DESC"):
            d = dict(r)
            cost_v = d["qty"] * d["avg_cost"]
            d["ret_pct"] = d["pnl"] / cost_v * 100 if cost_v else 0.0
            d["industry"] = d["industry"] or "其他"
            out.append(d)
        return out
    finally:
        con.close()


_DIM_COL = {"class": "asset_class", "broker": "broker",
            "ccy": "ccy", "industry": "industry"}
_CLASS_LABEL = {"equity": "個股", "etf": "ETF", "cash": "現金"}
_BROKER_LABEL = {"sinopac": "永豐金", "schwab": "嘉信", "ibkr": "盈透"}


@app.get("/api/allocation")
def allocation(dim: str = "class"):
    col = _DIM_COL.get(dim, "asset_class")
    con = _con()
    try:
        rows = con.execute(
            f"SELECT COALESCE(NULLIF({col}, ''), '其他') AS k, "
            "SUM(market_value_native) AS v FROM positions_current "
            "GROUP BY k ORDER BY v DESC").fetchall()
        out = []
        for r in rows:
            label = r["k"]
            if dim == "class":
                label = _CLASS_LABEL.get(label, label)
            elif dim == "broker":
                label = _BROKER_LABEL.get(label, label)
            out.append({"label": label, "value": r["v"]})
        s = con.execute("SELECT cash FROM snapshots "
                        "ORDER BY ts DESC LIMIT 1").fetchone()
        if s and s["cash"] > 0:
            out.append({"label": "TWD 現金" if dim == "ccy" else "現金",
                        "value": s["cash"]})
        total = sum(o["value"] for o in out) or 1
        for o in out:
            o["pct"] = o["value"] / total * 100
        return out
    finally:
        con.close()


@app.get("/api/dividends")
def dividends():
    con = _con()
    try:
        today = date.today()
        ttm_cut = (today - timedelta(days=365)).isoformat()
        rows = con.execute(
            "SELECT d.symbol, p.name, d.date, d.amount, p.qty, p.last_price "
            "FROM dividend_cache d JOIN positions_current p "
            "ON d.symbol = p.symbol ORDER BY d.date DESC").fetchall()
        events, monthly, ttm_total = [], {}, 0.0
        for r in rows:
            est = r["amount"] * r["qty"]
            events.append({"date": r["date"], "symbol": r["symbol"],
                           "name": r["name"], "per_share": r["amount"],
                           "qty": r["qty"], "est_cash": est})
            if ttm_cut < r["date"] <= today.isoformat():
                ttm_total += est
                monthly[r["date"][:7]] = monthly.get(r["date"][:7], 0) + est
        inv = con.execute("SELECT invested FROM snapshots "
                          "ORDER BY ts DESC LIMIT 1").fetchone()
        invested = inv["invested"] if inv else 0
        months = []
        cur = today.replace(day=1)
        for _ in range(12):
            months.append(cur.strftime("%Y-%m"))
            cur = (cur - timedelta(days=1)).replace(day=1)
        months.reverse()
        return {
            "ttm_total": ttm_total,
            "yield_pct": ttm_total / invested * 100 if invested else 0,
            "monthly": [{"month": m, "cash": monthly.get(m, 0.0)}
                        for m in months],
            "events": events[:40],
            "note": "估算口徑:歷史每股配息 × 目前持股,非帳上實收",
        }
    finally:
        con.close()


@app.get("/api/realized")
def realized():
    con = _con()
    try:
        rows = con.execute(
            "SELECT symbol, qty, price, pnl, trade_date FROM realized_pnl "
            "ORDER BY trade_date").fetchall()
        cum, out = 0.0, []
        by_sym: dict[str, float] = {}
        for r in rows:
            cum += r["pnl"]
            by_sym[r["symbol"]] = by_sym.get(r["symbol"], 0) + r["pnl"]
            out.append({"date": r["trade_date"], "symbol": r["symbol"],
                        "qty": r["qty"], "price": r["price"],
                        "pnl": r["pnl"], "cum": cum})
        wins = sum(1 for r in rows if r["pnl"] > 0)
        return {
            "total": cum, "count": len(rows),
            "win_rate": wins / len(rows) * 100 if rows else 0,
            "by_symbol": sorted(
                [{"symbol": k, "pnl": v} for k, v in by_sym.items()],
                key=lambda x: -x["pnl"]),
            "records": out,
        }
    finally:
        con.close()


@app.get("/api/live")
def live():
    con = _con()
    try:
        return {r["symbol"]: {"price": r["price"], "ts": r["ts"]}
                for r in con.execute("SELECT symbol, price, ts "
                                     "FROM live_quotes")}
    finally:
        con.close()


# ---------- React 儀表板靜態檔(瀏覽器開 http://127.0.0.1:8787) ----------
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True),
              name="dashboard")
