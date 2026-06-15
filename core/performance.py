"""績效引擎 — P3 報酬率指標。

兩個口徑(同時呈現,各有用途):

1) 期間變化(簡單報酬,「含現金流」):
       (期末淨值 - 期初淨值) / 期初淨值
   入金會讓它變大、出金讓它變小 — 反映「資產規模」變化,不代表操盤績效。

2) TWR 時間加權報酬(「不含現金流」):
       將期間切成每日,逐日報酬 r_t = (NV_t - F_t) / NV_{t-1} - 1
       (F_t = 當日淨外部現金流,視為盤後發生),再連乘:
       TWR = Π(1 + r_t) - 1
   出入金不影響 TWR — 這才是衡量「投資表現」的標準口徑(基金淨值法)。

外部現金流的來源:transactions 表中 txn_type ∈ {DEPOSIT, WITHDRAW}
(amount 正=入金、負=出金)。買賣股票是內部調整,不是外部現金流。

誠實限制:若出入金「沒有被記錄」(可用 flows.py 手動補登),
TWR 會把該筆入金誤判成投資獲利(或出金誤判成虧損)。
有出入金時請務必用 flows.py 補登,TWR 才有意義。
"""
from datetime import datetime


def compute_performance(series: list[tuple[str, float]],
                        flows: dict[str, float]) -> dict | None:
    """series: [(date, net_worth)] 由舊到新(daily_networth 全序列)。
    flows:  {date: 淨外部現金流}(入金正、出金負)。

    回傳 {simple_return, twr, twr_annualized, days, net_flow,
          start_date, end_date, start_nv, end_nv};資料不足回 None。
    """
    if len(series) < 2:
        return None
    series = sorted(series)
    start_d, start_nv = series[0]
    end_d, end_nv = series[-1]
    if start_nv <= 0:
        return None

    growth = 1.0
    prev = start_nv
    valid = True
    for d, nv in series[1:]:
        f = flows.get(d, 0.0)
        if prev <= 0:           # 淨值歸零/負(理論上不會),TWR 無法定義
            valid = False
            break
        growth *= (nv - f) / prev
        prev = nv

    days = (_d(end_d) - _d(start_d)).days or 1
    net_flow = sum(a for d, a in flows.items() if start_d < d <= end_d)
    twr = (growth - 1.0) if valid else None
    twr_ann = None
    if twr is not None and days >= 30 and growth > 0:
        twr_ann = growth ** (365.0 / days) - 1.0

    return {
        "simple_return": end_nv / start_nv - 1.0,
        "twr": twr,
        "twr_annualized": twr_ann,
        "days": days,
        "net_flow": net_flow,
        "start_date": start_d, "end_date": end_d,
        "start_nv": start_nv, "end_nv": end_nv,
    }


def _d(s: str):
    return datetime.strptime(s, "%Y-%m-%d").date()


def compute_annual_performance(series: list[tuple[str, float]],
                               flows: dict[str, float]) -> list[dict]:
    """逐年度績效(由舊到新)。每筆:
        {year, start_date, end_date, start_nv, end_nv, net_flow, pnl,
         twr, annualized, days, partial}

    口徑說明
    --------
    * 起始金額 start_nv:進入該年度時的淨值 = 前一年最後一交易日淨值
      (首年無前值 → 取序列首日)。如此年與年首尾相接、不漏不重。
    * 淨流入 net_flow:該年度區間 (start_date, end_date] 的淨外部現金流
      (入金正、出金負)。
    * 損益 pnl = 期末 − 期初 − 淨流入:扣掉出入金後的「真實投資損益」。
      (若只看 期末−期初,入金會被誤算成獲利。)
    * 報酬率 twr:該年度的時間加權報酬,出入金不影響(基金淨值法)。
    * 年化 annualized:依該區間實際天數年化;整年≈twr,首年/當年(未滿一年)
      會外推為年化值。partial=True 標示「未滿整年」。
    """
    if not series:
        return []
    series = sorted(series)
    from collections import defaultdict
    by_year: dict[str, list] = defaultdict(list)
    for d, nv in series:
        by_year[d[:4]].append((d, nv))

    out: list[dict] = []
    prev_end: tuple[str, float] | None = None
    for y in sorted(by_year):
        ys = by_year[y]
        if prev_end is not None:
            sub = [prev_end] + ys
            start_d, start_nv = prev_end
        else:
            sub = ys
            start_d, start_nv = ys[0]
        end_d, end_nv = ys[-1]

        net_flow = sum(a for d, a in flows.items() if start_d < d <= end_d)
        pnl = end_nv - start_nv - net_flow
        days = (_d(end_d) - _d(start_d)).days or 1

        perf = compute_performance(sub, flows) if len(sub) >= 2 and start_nv > 0 \
            else None
        twr = perf["twr"] if perf else None
        ann = ((1 + twr) ** (365.0 / days) - 1.0) \
            if (twr is not None and (1 + twr) > 0) else None

        # 未滿整年:首年起點晚於 1/1,或當年尚未走到年底
        partial = not (start_d <= f"{y}-01-01" and end_d >= f"{y}-12-28")

        out.append({
            "year": int(y), "start_date": start_d, "end_date": end_d,
            "start_nv": start_nv, "end_nv": end_nv, "net_flow": net_flow,
            "pnl": pnl, "twr": twr, "annualized": ann, "days": days,
            "partial": partial,
        })
        prev_end = (end_d, end_nv)
    return out
