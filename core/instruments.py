"""商品主檔(Instrument Master)— P4。

解決「同一個標的在不同券商有不同代號」的問題,例如:
  * 台積電:永豐是 2330(TWD),嘉信是 TSM(ADR,USD,1 ADR = 5 普通股)
  * 同一支美股同時放在嘉信與盈透

主檔讓儀表板能用「經濟實體」彙總曝險,而不是被券商代號切碎。
這是輔助檢視層:不改變快照/回補的任何數字,只提供分組依據。

資料表:
  instrument_links(broker, symbol, canonical, ratio, note)
    canonical : 自訂的統一代號(建議用主上市代號,如 '2330'、'AAPL')
    ratio     : 1 單位此券商代號 = ratio 單位 canonical(ADR 換算用;
                一般同標的填 1,TSM→2330 填 5)

用法(CLI):
  python -m core.instruments list
  python -m core.instruments link sinopac 2330 2330
  python -m core.instruments link schwab  TSM  2330 --ratio 5 --note "ADR 1:5"
  python -m core.instruments unlink schwab TSM

程式內:
  from core.instruments import InstrumentMaster
  im = InstrumentMaster(db_path)
  groups = im.group_positions(positions)   # {canonical: [Position, ...]}
"""
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

# 內建種子:常見台股 ↔ ADR 對應(可用 CLI 增刪)
_SEED = [
    ("schwab", "TSM", "2330", 5.0, "台積電 ADR(1 ADR = 5 普通股)"),
    ("ibkr",   "TSM", "2330", 5.0, "台積電 ADR(1 ADR = 5 普通股)"),
    ("schwab", "UMC", "2303", 5.0, "聯電 ADR(1 ADR = 5 普通股)"),
    ("ibkr",   "UMC", "2303", 5.0, "聯電 ADR(1 ADR = 5 普通股)"),
    ("schwab", "ASX", "3711", 2.0, "日月光投控 ADR(1 ADR = 2 普通股)"),
    ("schwab", "CHT", "2412", 10.0, "中華電信 ADR(1 ADR = 10 普通股)"),
]


class InstrumentMaster:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        con = self._con()
        con.execute("""CREATE TABLE IF NOT EXISTS instrument_links (
                         broker    TEXT NOT NULL,
                         symbol    TEXT NOT NULL,
                         canonical TEXT NOT NULL,
                         ratio     REAL NOT NULL DEFAULT 1,
                         note      TEXT DEFAULT '',
                         PRIMARY KEY (broker, symbol))""")
        # 種子只在「該鍵不存在」時寫入,不覆蓋你的手動設定
        con.executemany(
            "INSERT OR IGNORE INTO instrument_links VALUES (?,?,?,?,?)",
            _SEED)
        con.commit()
        con.close()

    def _con(self):
        return sqlite3.connect(self.db_path)

    # ---------- 維護 ----------

    def link(self, broker: str, symbol: str, canonical: str,
             ratio: float = 1.0, note: str = "") -> None:
        con = self._con()
        con.execute(
            "INSERT OR REPLACE INTO instrument_links VALUES (?,?,?,?,?)",
            (broker, symbol, canonical, ratio, note))
        con.commit()
        con.close()

    def unlink(self, broker: str, symbol: str) -> None:
        con = self._con()
        con.execute("DELETE FROM instrument_links WHERE broker=? AND symbol=?",
                    (broker, symbol))
        con.commit()
        con.close()

    def all_links(self) -> list[tuple]:
        con = self._con()
        rows = con.execute(
            "SELECT broker, symbol, canonical, ratio, note "
            "FROM instrument_links ORDER BY canonical, broker").fetchall()
        con.close()
        return rows

    # ---------- 查詢 ----------

    def canonical_of(self, broker: str, symbol: str) -> tuple[str, Decimal]:
        """回傳 (統一代號, 換算比率)。沒設定關聯就用原代號、比率 1。"""
        con = self._con()
        row = con.execute(
            "SELECT canonical, ratio FROM instrument_links "
            "WHERE broker=? AND symbol=?", (broker, symbol)).fetchone()
        con.close()
        if row:
            return row[0], Decimal(str(row[1]))
        return symbol, Decimal(1)

    def group_positions(self, positions) -> dict[str, list]:
        """把跨券商持倉依統一代號分組:{canonical: [Position, ...]}。
        儀表板可據此顯示「合併曝險」(例:2330 普通股 + TSM ADR 折算)。"""
        groups: dict[str, list] = {}
        for p in positions:
            canon, _ = self.canonical_of(p.broker, p.symbol)
            groups.setdefault(canon, []).append(p)
        return groups

    def equivalent_qty(self, positions, canonical: str) -> Decimal:
        """某統一代號的「折算後總股數」(ADR 依 ratio 換算成普通股)。"""
        total = Decimal(0)
        for p in positions:
            canon, ratio = self.canonical_of(p.broker, p.symbol)
            if canon == canonical:
                total += p.qty * ratio
        return total


# ---------- CLI ----------

def _cli() -> None:
    db = Path(__file__).parent.parent / "portfolio.db"
    im = InstrumentMaster(db)
    args = sys.argv[1:]
    cmd = args[0] if args else "list"

    if cmd == "list":
        rows = im.all_links()
        if not rows:
            print("(尚無關聯)")
            return
        print(f"{'券商':<8}{'代號':<8}{'統一代號':<10}{'比率':<6}備註")
        for b, s, c, r, n in rows:
            print(f"{b:<8}{s:<8}{c:<10}{r:<6g}{n}")
    elif cmd == "link" and len(args) >= 4:
        ratio, note = 1.0, ""
        rest = args[4:]
        while rest:
            if rest[0] == "--ratio" and len(rest) >= 2:
                ratio, rest = float(rest[1]), rest[2:]
            elif rest[0] == "--note" and len(rest) >= 2:
                note, rest = rest[1], rest[2:]
            else:
                rest = rest[1:]
        im.link(args[1], args[2], args[3], ratio, note)
        print(f"已關聯:{args[1]}:{args[2]} → {args[3]}(ratio={ratio:g})")
    elif cmd == "unlink" and len(args) >= 3:
        im.unlink(args[1], args[2])
        print(f"已移除:{args[1]}:{args[2]}")
    else:
        print(__doc__)


if __name__ == "__main__":
    _cli()
