"""多券商同步協調 — P4。

run.py 用法:
    from adapters.registry import sync_all
    positions, cash_by_ccy, realized, txns, errors = sync_all(adapters)

設計原則:
  * 單家券商失敗「不中斷其他家」:該家當次略過並記入 errors,
    其餘券商照常同步(部分成功優於全部失敗)。
  * 是否要因為 errors 而跳過寫快照,由 run.py 決策(P4 規則:跳過,
    保護淨值曲線不出現假跳水)。
  * 無論成敗,每家都會在 finally 中 disconnect()(永豐 5 連線額度)。
"""


def sync_all(adapters: list):
    """逐一同步所有券商;單家失敗不影響其他家。

    回傳 (positions, cash_by_ccy, realized, txns, errors)
      positions    : list[Position]     成功券商的全部持倉
      cash_by_ccy  : dict[ccy, Decimal] 各幣別現金合計
      realized     : list[RealizedPnl]  近 60 天已實現損益(與 P3 口徑一致)
      txns         : list[Transaction]  今日成交(支援的券商)
      errors       : list[str]          失敗券商與原因
    """
    from datetime import date, timedelta
    from decimal import Decimal

    positions, realized, txns, errors = [], [], [], []
    cash_by_ccy: dict[str, Decimal] = {}
    begin = (date.today() - timedelta(days=60)).isoformat()
    end = date.today().isoformat()

    for ad in adapters:
        try:
            ad.connect()
            positions += ad.list_positions()
            for ccy, amt in ad.list_cash().items():
                cash_by_ccy[ccy] = cash_by_ccy.get(ccy, Decimal(0)) + amt
            try:
                txns += ad.list_transactions()
            except Exception as e:
                print(f"[{ad.name}] 抓今日成交失敗(略過):{e}")
            try:
                realized += ad.list_realized_pnl(begin, end)
            except Exception as e:
                print(f"[{ad.name}] 抓已實現損益失敗(略過):{e}")
        except Exception as e:
            msg = f"[{ad.name}] {e}"
            print(f"[{ad.name}] 同步失敗,本次略過:{e}")
            errors.append(msg)
        finally:
            try:
                ad.disconnect()
            except Exception:
                pass

    return positions, cash_by_ccy, realized, txns, errors
