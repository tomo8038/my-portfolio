/* 資產存摺 — React 專業版儀表板(P3)
 * 資料來源:FastAPI(api/server.py)的 /api/* 端點。
 * 即時:每 3 秒輪詢 /api/live(watch.py 寫入的報價),有資料即重算市值。
 * 打包:npm run build(esbuild → dist/app.js,單一檔、免 CDN、可離線)。
 */
import React, { useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  ResponsiveContainer, AreaChart, Area, LineChart, Line, BarChart, Bar,
  PieChart, Pie, Cell, XAxis, YAxis, Tooltip, CartesianGrid, ReferenceLine,
} from "recharts";

const fmt = (n, sign) =>
  (sign && n >= 0 ? "+" : "") +
  Math.round(n).toLocaleString("zh-TW");
const pct = (n, d = 2) => (n >= 0 ? "+" : "") + n.toFixed(d) + "%";

const PALETTE = ["#173B2C", "#C9342B", "#3D6B9E", "#B8860B",
                 "#6B4E8C", "#1E7A4E", "#8C5A3C", "#5A6B5D"];

function useApi(path, deps = []) {
  const [data, setData] = useState(null);
  useEffect(() => {
    let on = true;
    fetch(path).then(r => r.json()).then(d => on && setData(d))
      .catch(() => on && setData(undefined));
    return () => { on = false; };
  }, deps);
  return data;
}

/* ---------- 即時報價跑馬燈(signature) ---------- */
function Tape({ live, scheme, positions }) {
  const items = Object.entries(live || {});
  if (!items.length)
    return <div className="tape idle">⏸ 即時報價未啟動 — 另開終端機執行
      <code> python watch.py</code>(盤中)或<code> python watch.py --mock</code></div>;
  const base = {};
  (positions || []).forEach(p => { base[p.symbol] = p.last_price; });
  const cells = items.map(([s, q]) => {
    const chg = base[s] ? (q.price / base[s] - 1) * 100 : 0;
    const cls = chg >= 0 ? scheme.upCls : scheme.downCls;
    return <span key={s} className="tape-item">
      <b>{s}</b> {q.price.toLocaleString("zh-TW")}
      <i className={cls}> {pct(chg, 1)}</i>
    </span>;
  });
  return <div className="tape"><div className="tape-track">
    {cells}{cells /* 重複一輪讓跑馬燈無縫 */}
  </div></div>;
}

/* ---------- 存摺式 KPI ---------- */
function Passbook({ ov, liveCalc, scheme }) {
  const rows = [
    ["總資產淨值", liveCalc ? liveCalc.nw : ov.net_worth,
     liveCalc ? "⚡ 即時" : null],
    ["投資市值", liveCalc ? liveCalc.inv : ov.invested, null],
    ["現金", ov.cash, null],
    ["未實現損益", liveCalc ? liveCalc.unrl : ov.unrealized, "pnl"],
  ];
  return <div className="passbook">
    {rows.map(([label, v, tag]) => {
      const isPnl = tag === "pnl";
      const cls = isPnl ? (v >= 0 ? scheme.upCls : scheme.downCls) : "";
      return <div className="pb-row" key={label}>
        <span className="pb-label">{label}{tag === "⚡ 即時" || tag === null
          ? (tag ? <em className="live-dot"> {tag}</em> : null) : null}</span>
        <span className={"pb-num " + cls}>NT$ {fmt(v, isPnl)}</span>
      </div>;
    })}
    {ov.performance && <div className="pb-row perf">
      <span className="pb-label">TWR(不含出入金)
        {ov.performance.twr_annualized != null &&
          <em> · 年化 {pct(ov.performance.twr_annualized * 100)}</em>}</span>
      <span className={"pb-num " +
        (ov.performance.twr >= 0 ? scheme.upCls : scheme.downCls)}>
        {ov.performance.twr != null ? pct(ov.performance.twr * 100) : "—"}
      </span>
    </div>}
  </div>;
}

/* ---------- 區塊外框 ---------- */
const Card = ({ title, extra, children }) =>
  <section className="card">
    <header><h2>{title}</h2>{extra}</header>
    {children}
  </section>;

const DIMS = [["class", "類別"], ["broker", "券商"],
              ["ccy", "幣別"], ["industry", "產業"]];

function App() {
  const [schemeName, setSchemeName] = useState("tw");
  const scheme = schemeName === "tw"
    ? { up: "#C9342B", down: "#1E7A4E", upCls: "up-tw", downCls: "down-tw" }
    : { up: "#1E7A4E", down: "#C9342B", upCls: "down-tw", downCls: "up-tw" };

  const ov = useApi("/api/overview");
  const nw = useApi("/api/networth");
  const positions = useApi("/api/positions");
  const dividends = useApi("/api/dividends");
  const realized = useApi("/api/realized");
  const [dim, setDim] = useState("class");
  const alloc = useApi("/api/allocation?dim=" + dim, [dim]);

  const [live, setLive] = useState({});
  useEffect(() => {
    const tick = () => fetch("/api/live").then(r => r.json())
      .then(setLive).catch(() => {});
    tick();
    const id = setInterval(tick, 3000);
    return () => clearInterval(id);
  }, []);

  const liveCalc = useMemo(() => {
    if (!positions || !Object.keys(live).length || !ov) return null;
    let inv = 0, cost = 0;
    positions.forEach(p => {
      const px = live[p.symbol] ? live[p.symbol].price : p.last_price;
      inv += p.qty * px;
      cost += p.qty * p.avg_cost;
    });
    return { inv, nw: inv + ov.cash, unrl: inv - cost };
  }, [positions, live, ov]);

  if (!ov) return <div className="loading">讀取中…</div>;
  if (!ov.ready) return <div className="loading">{ov.hint}</div>;

  const pnlRank = (positions || [])
    .slice().sort((a, b) => b.pnl - a.pnl);

  return <>
    <header className="topbar">
      <div>
        <h1>資產存摺 <small>My Portfolio · P3</small></h1>
        <span className="ts">資料時間 {ov.ts} · 基準幣別 TWD</span>
      </div>
      <button className="scheme-btn"
        onClick={() => setSchemeName(s => s === "tw" ? "us" : "tw")}>
        {schemeName === "tw" ? "紅漲綠跌(台股)" : "綠漲紅跌(美股)"} ⇄
      </button>
    </header>

    <Tape live={live} scheme={scheme} positions={positions} />

    <main>
      <Passbook ov={ov} liveCalc={liveCalc} scheme={scheme} />

      <Card title="淨值走勢"
        extra={ov.performance && <span className="muted">
          {ov.performance.start_date} ~ {ov.performance.end_date} ·
          期間 {pct(ov.performance.simple_return * 100)}(含出入金)</span>}>
        <ResponsiveContainer width="100%" height={260}>
          <AreaChart data={nw || []}>
            <CartesianGrid stroke="#dde3dc" strokeDasharray="2 4" />
            <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={40} />
            <YAxis tick={{ fontSize: 11 }} width={86}
              domain={["auto", "auto"]}
              tickFormatter={v => (v / 10000).toFixed(0) + "萬"} />
            <Tooltip formatter={v => "NT$ " + fmt(v)} />
            <Area type="monotone" dataKey="net_worth" name="淨值"
              stroke="#173B2C" strokeWidth={2}
              fill="#173B2C" fillOpacity={0.08} />
          </AreaChart>
        </ResponsiveContainer>
        <p className="muted">真實快照與回補估計構成的每日曲線;出入金請以
          flows.py 補登,TWR 才準確。</p>
      </Card>

      <div className="grid2">
        <Card title="資產配置" extra={
          <nav className="dim-tabs">{DIMS.map(([k, label]) =>
            <button key={k} className={k === dim ? "on" : ""}
              onClick={() => setDim(k)}>{label}</button>)}</nav>}>
          <ResponsiveContainer width="100%" height={250}>
            <PieChart>
              <Pie data={alloc || []} dataKey="value" nameKey="label"
                innerRadius={58} outerRadius={95} paddingAngle={1}>
                {(alloc || []).map((_, i) =>
                  <Cell key={i} fill={PALETTE[i % PALETTE.length]} />)}
              </Pie>
              <Tooltip formatter={(v, _, e) =>
                ["NT$ " + fmt(v) + "(" + e.payload.pct.toFixed(1) + "%)",
                 e.payload.label]} />
            </PieChart>
          </ResponsiveContainer>
          <ul className="legend">{(alloc || []).map((a, i) =>
            <li key={a.label}><i style={{ background: PALETTE[i % 8] }} />
              {a.label} <b>{a.pct.toFixed(1)}%</b></li>)}</ul>
        </Card>

        <Card title="未實現損益排行">
          <ResponsiveContainer width="100%"
            height={Math.max(220, 38 * pnlRank.length)}>
            <BarChart data={pnlRank} layout="vertical"
              margin={{ left: 18, right: 24 }}>
              <CartesianGrid stroke="#dde3dc" strokeDasharray="2 4" />
              <XAxis type="number" tick={{ fontSize: 11 }}
                tickFormatter={v => (v / 10000).toFixed(0) + "萬"} />
              <YAxis type="category" width={108} tick={{ fontSize: 12 }}
                dataKey={p => p.symbol + " " + p.name} />
              <Tooltip formatter={(v, _, e) =>
                ["NT$ " + fmt(v, true) + "(" +
                 pct(e.payload.ret_pct, 1) + ")", "未實現損益"]} />
              <ReferenceLine x={0} stroke="#8a948c" />
              <Bar dataKey="pnl" radius={[0, 3, 3, 0]}>
                {pnlRank.map((p, i) =>
                  <Cell key={i} fill={p.pnl >= 0 ? scheme.up : scheme.down} />)}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </Card>
      </div>

      <div className="grid2">
        <Card title="股息現金流(近 12 個月,估算)"
          extra={dividends && <span className="muted">
            TTM NT$ {fmt(dividends.ttm_total)} ·
            殖利率 {dividends.yield_pct.toFixed(2)}%</span>}>
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={(dividends || { monthly: [] }).monthly}>
              <CartesianGrid stroke="#dde3dc" strokeDasharray="2 4" />
              <XAxis dataKey="month" tick={{ fontSize: 10 }} />
              <YAxis tick={{ fontSize: 11 }}
                tickFormatter={v => (v / 1000).toFixed(0) + "千"} />
              <Tooltip formatter={v => "NT$ " + fmt(v)} />
              <Bar dataKey="cash" name="估算股息" fill="#3D6B9E"
                radius={[3, 3, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
          <p className="muted">{dividends ? dividends.note : ""}</p>
        </Card>

        <Card title="已實現損益"
          extra={realized && realized.count > 0 && <span className="muted">
            累計 <b className={realized.total >= 0 ? scheme.upCls
              : scheme.downCls}>NT$ {fmt(realized.total, true)}</b> ·
            {realized.count} 筆 · 勝率 {realized.win_rate.toFixed(0)}%</span>}>
          {realized && realized.count > 0
            ? <ResponsiveContainer width="100%" height={220}>
                <LineChart data={realized.records}>
                  <CartesianGrid stroke="#dde3dc" strokeDasharray="2 4" />
                  <XAxis dataKey="date" tick={{ fontSize: 10 }} />
                  <YAxis tick={{ fontSize: 11 }}
                    tickFormatter={v => (v / 10000).toFixed(0) + "萬"} />
                  <Tooltip formatter={(v, n) => ["NT$ " + fmt(v, true),
                    n === "cum" ? "累計" : "單筆"]} />
                  <ReferenceLine y={0} stroke="#8a948c" strokeDasharray="4 4" />
                  <Line type="stepAfter" dataKey="cum" name="cum"
                    stroke={realized.total >= 0 ? scheme.up : scheme.down}
                    strokeWidth={2} dot={{ r: 3 }} />
                </LineChart>
              </ResponsiveContainer>
            : <p className="muted">尚無紀錄 — 每次 python run.py
                會抓近 60 天平倉損益並永久累積。</p>}
        </Card>
      </div>

      <Card title="持倉明細">
        <table className="ledger">
          <thead><tr>
            <th>代號</th><th>名稱</th><th>產業</th>
            <th className="num">股數</th><th className="num">均價</th>
            <th className="num">現價{Object.keys(live).length ? " ⚡" : ""}</th>
            <th className="num">市值</th><th className="num">未實現損益</th>
            <th className="num">報酬率</th>
          </tr></thead>
          <tbody>{(positions || []).map(p => {
            const px = live[p.symbol] ? live[p.symbol].price : p.last_price;
            const mv = p.qty * px, pnl = mv - p.qty * p.avg_cost;
            const rp = p.qty * p.avg_cost
              ? pnl / (p.qty * p.avg_cost) * 100 : 0;
            const cls = pnl >= 0 ? scheme.upCls : scheme.downCls;
            return <tr key={p.symbol}>
              <td>{p.symbol}</td><td>{p.name}</td><td>{p.industry}</td>
              <td className="num">{fmt(p.qty)}</td>
              <td className="num">{p.avg_cost.toFixed(2)}</td>
              <td className="num">{px.toFixed(2)}</td>
              <td className="num">{fmt(mv)}</td>
              <td className={"num " + cls}>{fmt(pnl, true)}</td>
              <td className={"num " + cls}>{pct(rp, 1)}</td>
            </tr>;
          })}</tbody>
        </table>
      </Card>

      <footer className="muted">
        my-portfolio P3 · 資料皆在本機 portfolio.db,不外送雲端 ·
        Streamlit 版:<code>streamlit run viewer/app.py</code>
      </footer>
    </main>
  </>;
}

createRoot(document.getElementById("root")).render(<App />);
