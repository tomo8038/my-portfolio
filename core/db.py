"""SQLite 存取層 — 整個系統的資料就是一個 portfolio.db 檔。

Python 內建 sqlite3,免安裝任何資料庫伺服器。
P0 用到:snapshots / daily_networth / positions_current。
其餘表(transactions / price_cache / fx_cache)先建好,P1 回補引擎會用。
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    ts            TEXT PRIMARY KEY,
    base_ccy      TEXT NOT NULL,
    net_worth     REAL NOT NULL,
    invested      REAL NOT NULL,
    cash          REAL NOT NULL,
    cost          REAL NOT NULL,
    breakdown     TEXT
);

CREATE TABLE IF NOT EXISTS daily_networth (
    date          TEXT PRIMARY KEY,
    base_ccy      TEXT NOT NULL,
    net_worth     REAL NOT NULL,
    is_real       INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS positions_current (
    broker        TEXT, account_id TEXT, symbol TEXT, name TEXT,
    asset_class   TEXT, qty REAL, avg_cost REAL, last_price REAL,
    ccy           TEXT, market_value_native REAL, unrealized_pnl_native REAL,
    as_of         TEXT, industry TEXT DEFAULT '',
    PRIMARY KEY (broker, account_id, symbol)
);

CREATE TABLE IF NOT EXISTS transactions (
    broker        TEXT, external_id TEXT, symbol TEXT, txn_type TEXT,
    qty REAL, price REAL, amount REAL, ccy TEXT, trade_date TEXT,
    PRIMARY KEY (broker, external_id)
);

CREATE TABLE IF NOT EXISTS sync_state (
    broker        TEXT PRIMARY KEY,
    last_sync     TEXT,
    last_txn_date TEXT
);

CREATE TABLE IF NOT EXISTS price_cache (
    symbol TEXT, date TEXT, close REAL, ccy TEXT,
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS fx_cache (
    pair TEXT, date TEXT, rate REAL,
    PRIMARY KEY (pair, date)
);

-- P2:股息快取(yfinance 除息事件,每股配息金額;抓過存本地)
CREATE TABLE IF NOT EXISTS dividend_cache (
    symbol TEXT, date TEXT, amount REAL, ccy TEXT,
    PRIMARY KEY (symbol, date)
);

-- P3:即時報價(watch.py 寫入、儀表板讀取;每檔只留最新一筆)
CREATE TABLE IF NOT EXISTS live_quotes (
    symbol TEXT PRIMARY KEY,
    price  REAL NOT NULL,
    ts     TEXT NOT NULL          -- 最後成交時間(ISO)
);

-- P3:已實現損益(append-only,去重靠 external_id)
CREATE TABLE IF NOT EXISTS realized_pnl (
    broker      TEXT, external_id TEXT, symbol TEXT,
    qty REAL, price REAL, pnl REAL, ccy TEXT, trade_date TEXT,
    PRIMARY KEY (broker, external_id)
);

-- P1:每筆快照「當下的持倉」。回補引擎的錨點:
-- 兩個快照夾住一段區間,兩端持倉已知,中間逐日估值。
CREATE TABLE IF NOT EXISTS snapshot_positions (
    ts     TEXT,            -- 對應 snapshots.ts
    symbol TEXT, qty REAL, avg_cost REAL, ccy TEXT, last_price REAL,
    PRIMARY KEY (ts, symbol)
);
"""


class DB:
    def __init__(self, path: str | Path = "portfolio.db"):
        self.path = Path(path)
        self.con = sqlite3.connect(self.path)
        # P3:WAL 模式 — 即時模式下 watch.py(寫)與儀表板(讀)並行不互鎖
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA busy_timeout=3000")
        self.con.executescript(SCHEMA)
        self._migrate()
        self.con.commit()

    def _migrate(self) -> None:
        """舊資料庫升級:P0/P1 建立的 positions_current 沒有 industry 欄位。"""
        cols = [r[1] for r in
                self.con.execute("PRAGMA table_info(positions_current)")]
        if "industry" not in cols:
            self.con.execute(
                "ALTER TABLE positions_current ADD COLUMN industry TEXT DEFAULT ''")

    # ---------- 寫入 ----------

    def replace_positions(self, broker: str, positions: list) -> None:
        """整批替換某券商的目前持倉(部位是「狀態」,不是流水)。"""
        cur = self.con.cursor()
        cur.execute("DELETE FROM positions_current WHERE broker = ?", (broker,))
        cur.executemany(
            """INSERT INTO positions_current
               (broker, account_id, symbol, name, asset_class, qty, avg_cost,
                last_price, ccy, market_value_native, unrealized_pnl_native,
                as_of, industry)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(p.broker, p.account_id, p.symbol, p.name, p.asset_class,
              float(p.qty), float(p.avg_cost), float(p.last_price), p.ccy,
              float(p.market_value), float(p.unrealized_pnl),
              p.as_of.isoformat(timespec="seconds"),
              getattr(p, "industry", "") or "") for p in positions],
        )
        self.con.commit()

    def save_snapshot(self, base_ccy: str, net_worth: float, invested: float,
                      cash: float, cost: float, breakdown: dict) -> str:
        """存一筆「真實淨值快照」,並同步更新當日 daily_networth (is_real=1)。"""
        ts = datetime.now().isoformat(timespec="seconds")
        today = ts[:10]
        cur = self.con.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?,?)",
            (ts, base_ccy, net_worth, invested, cash, cost,
             json.dumps(breakdown, ensure_ascii=False)),
        )
        cur.execute(
            "INSERT OR REPLACE INTO daily_networth VALUES (?,?,?,1)",
            (today, base_ccy, net_worth),
        )
        cur.execute(
            """INSERT INTO sync_state (broker, last_sync) VALUES ('_all', ?)
               ON CONFLICT(broker) DO UPDATE SET last_sync = excluded.last_sync""",
            (ts,),
        )
        self.con.commit()
        return ts

    # ---------- 讀取(給檢視器用) ----------

    def latest_snapshot(self) -> dict | None:
        row = self.con.execute(
            "SELECT ts, base_ccy, net_worth, invested, cash, cost, breakdown "
            "FROM snapshots ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return {
            "ts": row[0], "base_ccy": row[1], "net_worth": row[2],
            "invested": row[3], "cash": row[4], "cost": row[5],
            "breakdown": json.loads(row[6] or "{}"),
        }

    def all_positions(self) -> list[dict]:
        cols = ["broker", "account_id", "symbol", "name", "asset_class", "qty",
                "avg_cost", "last_price", "ccy", "market_value_native",
                "unrealized_pnl_native", "as_of", "industry"]
        rows = self.con.execute(
            f"SELECT {','.join(cols)} FROM positions_current "
            "ORDER BY market_value_native DESC"
        ).fetchall()
        return [dict(zip(cols, r)) for r in rows]

    # ---------- P2:股息快取 ----------

    def get_dividends(self, symbol: str) -> dict[str, float]:
        """{date: 每股配息(原幣)}"""
        return dict(self.con.execute(
            "SELECT date, amount FROM dividend_cache WHERE symbol = ?",
            (symbol,),
        ).fetchall())

    def put_dividends(self, symbol: str, ccy: str,
                      rows: list[tuple[str, float]]) -> None:
        self.con.executemany(
            "INSERT OR REPLACE INTO dividend_cache VALUES (?,?,?,?)",
            [(symbol, d, a, ccy) for d, a in rows],
        )
        self.con.commit()

    # ---------- P3:即時報價 ----------

    def upsert_live_quotes(self, rows: list[tuple[str, float, str]]) -> None:
        """rows: [(symbol, price, ts_iso)]。每檔只留最新一筆。"""
        self.con.executemany(
            "INSERT OR REPLACE INTO live_quotes VALUES (?,?,?)", rows)
        self.con.commit()

    def get_live_quotes(self) -> dict[str, tuple[float, str]]:
        """{symbol: (price, ts)}"""
        return {s: (p, t) for s, p, t in self.con.execute(
            "SELECT symbol, price, ts FROM live_quotes")}

    def clear_live_quotes(self) -> None:
        self.con.execute("DELETE FROM live_quotes")
        self.con.commit()

    # ---------- P3:已實現損益 ----------

    def insert_realized(self, rows: list) -> int:
        """寫入已實現損益(INSERT OR IGNORE 去重),回傳實際新增筆數。"""
        cur = self.con.cursor()
        before = cur.execute("SELECT COUNT(*) FROM realized_pnl").fetchone()[0]
        cur.executemany(
            "INSERT OR IGNORE INTO realized_pnl VALUES (?,?,?,?,?,?,?,?)",
            [(r.broker, r.external_id, r.symbol, float(r.qty), float(r.price),
              float(r.pnl), r.ccy, r.trade_date) for r in rows],
        )
        self.con.commit()
        return cur.execute(
            "SELECT COUNT(*) FROM realized_pnl").fetchone()[0] - before

    def realized_all(self) -> list[dict]:
        cols = ["broker", "external_id", "symbol", "qty", "price", "pnl",
                "ccy", "trade_date"]
        rows = self.con.execute(
            f"SELECT {','.join(cols)} FROM realized_pnl ORDER BY trade_date"
        ).fetchall()
        return [dict(zip(cols, r)) for r in rows]

    # ---------- P3:外部現金流(出入金)— TWR 用 ----------

    def external_flows(self) -> dict[str, float]:
        """{date: 淨外部現金流}(DEPOSIT 為正、WITHDRAW 為負,依 amount 正負號)。"""
        out: dict[str, float] = {}
        for d, a in self.con.execute(
            "SELECT trade_date, amount FROM transactions "
            "WHERE txn_type IN ('DEPOSIT','WITHDRAW')"
        ):
            out[d] = out.get(d, 0.0) + float(a)
        return out

    def networth_series(self) -> list[tuple[str, float, int]]:
        return self.con.execute(
            "SELECT date, net_worth, is_real FROM daily_networth ORDER BY date"
        ).fetchall()

    # ---------- P1:快照持倉(回補錨點) ----------

    def save_snapshot_positions(self, ts: str, positions: list) -> None:
        self.con.executemany(
            "INSERT OR REPLACE INTO snapshot_positions VALUES (?,?,?,?,?,?)",
            [(ts, p.symbol, float(p.qty), float(p.avg_cost), p.ccy,
              float(p.last_price)) for p in positions],
        )
        self.con.commit()

    def snapshots_with_positions(self) -> list[dict]:
        """全部快照(由舊到新),各自附上當時持倉。回補引擎的輸入。"""
        snaps = self.con.execute(
            "SELECT ts, net_worth, cash FROM snapshots ORDER BY ts"
        ).fetchall()
        out = []
        for ts, nw, cash in snaps:
            rows = self.con.execute(
                "SELECT symbol, qty, ccy FROM snapshot_positions WHERE ts = ?",
                (ts,),
            ).fetchall()
            out.append({
                "ts": ts, "date": ts[:10], "net_worth": nw, "cash": cash,
                "holdings": {sym: {"qty": q, "ccy": c} for sym, q, c in rows},
            })
        return out

    # ---------- P1:交易紀錄 ----------

    def insert_transactions(self, txns: list) -> int:
        """寫入交易(INSERT OR IGNORE 去重),回傳實際新增筆數。"""
        cur = self.con.cursor()
        before = cur.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        cur.executemany(
            "INSERT OR IGNORE INTO transactions VALUES (?,?,?,?,?,?,?,?,?)",
            [(t.broker, t.external_id, t.symbol, t.txn_type, float(t.qty),
              float(t.price), float(t.amount), t.ccy, t.trade_date)
             for t in txns],
        )
        self.con.commit()
        return cur.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] - before

    def transactions_between(self, start_date: str, end_date: str) -> list[dict]:
        """取 (start, end] 的交易,回補反向重播用。"""
        cols = ["broker", "external_id", "symbol", "txn_type", "qty",
                "price", "amount", "ccy", "trade_date"]
        rows = self.con.execute(
            f"SELECT {','.join(cols)} FROM transactions "
            "WHERE trade_date > ? AND trade_date <= ? ORDER BY trade_date",
            (start_date, end_date),
        ).fetchall()
        return [dict(zip(cols, r)) for r in rows]

    # ---------- P1:價格 / 匯率快取 ----------

    def get_prices(self, symbol: str) -> dict[str, float]:
        return dict(self.con.execute(
            "SELECT date, close FROM price_cache WHERE symbol = ?", (symbol,)
        ).fetchall())

    def put_prices(self, symbol: str, ccy: str,
                   rows: list[tuple[str, float]]) -> None:
        self.con.executemany(
            "INSERT OR REPLACE INTO price_cache VALUES (?,?,?,?)",
            [(symbol, d, c, ccy) for d, c in rows],
        )
        self.con.commit()

    def get_fx(self, pair: str) -> dict[str, float]:
        return dict(self.con.execute(
            "SELECT date, rate FROM fx_cache WHERE pair = ?", (pair,)
        ).fetchall())

    def put_fx(self, pair: str, rows: list[tuple[str, float]]) -> None:
        self.con.executemany(
            "INSERT OR REPLACE INTO fx_cache VALUES (?,?,?)",
            [(pair, d, r) for d, r in rows],
        )
        self.con.commit()

    # ---------- P1:寫入回補估計(絕不覆蓋真實快照日) ----------

    def upsert_daily_estimates(self, base_ccy: str,
                               rows: list[tuple[str, float]]) -> int:
        """寫入每日淨值估計值。is_real=1 的日子(真實快照)永遠不被覆蓋。"""
        cur = self.con.cursor()
        n = 0
        for d, nw in rows:
            cur.execute(
                """INSERT INTO daily_networth (date, base_ccy, net_worth, is_real)
                   VALUES (?,?,?,0)
                   ON CONFLICT(date) DO UPDATE SET
                     net_worth = excluded.net_worth,
                     base_ccy  = excluded.base_ccy
                   WHERE daily_networth.is_real = 0""",
                (d, base_ccy, nw),
            )
            n += cur.rowcount
        self.con.commit()
        return n

    def close(self) -> None:
        self.con.close()
