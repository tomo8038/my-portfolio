# run.py 修改說明(需求 2:讓主程式同時看到台股 + 美股)

## 問題根因
現行 `run.py` 的 `main()` 只連一家券商(永豐),且彙總時:

```python
invested = sum((p.market_value for p in positions), Decimal(0))
cash_total = sum(cash.values(), Decimal(0))
```

**沒有匯率換算**(美股 USD 被當成 TWD),而且只用「永豐這次抓到的持倉」存快照。
所以即使把嘉信 / 盈透寫進 `positions_current`,下次 `run.py` 仍只更新永豐、
快照仍只反映台股 —— 這就是「只看得到台股」的原因。

## 修法(只動 `main()` 裡的「第 2、3 段」一個區塊)

把下面這段 **原始程式碼**:

```python
        # 2) 彙總(P0 全為 TWD,直接相加;P1 接美股後改走 FX 換算)
        invested = sum((p.market_value for p in positions), Decimal(0))
        cost = sum((p.cost_value for p in positions), Decimal(0))
        cash_total = sum(cash.values(), Decimal(0))
        net_worth = invested + cash_total
        unrealized = invested - cost

        breakdown = {
            "by_broker": {adapter.name: float(net_worth)},
            "by_asset_class": _group(positions, lambda p: p.asset_class),
            "by_ccy": {k: float(v) for k, v in cash.items()} | {"TWD_invested": float(invested)},
            "cash": {k: float(v) for k, v in cash.items()},
        }

        # 3) 寫入 SQLite
        db.replace_positions(adapter.name, positions)
        ts = db.save_snapshot(
            base_ccy=BASE_CCY,
            net_worth=float(net_worth), invested=float(invested),
            cash=float(cash_total), cost=float(cost),
            breakdown=breakdown,
        )
        db.save_snapshot_positions(ts, positions)        # P1:回補錨點
```

**替換成**:

```python
        # 2-3) 寫永豐持倉 + 現金,再「跨券商合併」算總資產(含美股,已換 TWD)
        from datetime import date, timedelta
        from core import aggregate
        from core.fxrate import FXBackfiller

        db.replace_positions(adapter.name, positions)          # 永豐持倉

        # 永豐現金(Shioaji 即時餘額,為真實值)記入 broker_cash,供合併採計
        aggregate.set_broker_cash(
            db, adapter.name, BASE_CCY,
            float(sum(cash.values(), Decimal(0))), date.today().isoformat())

        # 確保「今天」的 USD/TWD 匯率存在(美股換算用);抓不到就沿用既有快取
        fx = FXBackfiller(db, BASE_CCY)
        try:
            fx.backfill("USD", (date.today() - timedelta(days=7)).isoformat())
        except Exception as e:
            print(f"[fx] 今日匯率取得略過(用既有 fx_cache):{e}")

        # 跨券商合併快照:永豐(TWD)+ 嘉信/盈透(USD→TWD),
        # snapshot_positions 依代號合併(避免同代號跨券商互相覆蓋)
        ts = aggregate.write_combined_snapshot(db, fx, BASE_CCY)
        snap = aggregate.combined_snapshot(db, fx, BASE_CCY)
        net_worth = snap["net_worth"]
        invested = snap["invested"]
        cash_total = snap["cash"]
        cost = snap["cost"]
        unrealized = invested - cost
```

就這一個區塊。其餘(連線、讀 .env、回補、股息、已實現損益、`_print_summary`、
`disconnect`)**完全不動**。

## 修改後的行為
- `net_worth / invested / cash / 未實現損益` 這些 KPI(終端機摘要與儀表板)變成
  **永豐 + 嘉信 + 盈透合併、已換算成 TWD** 的值。
- `snapshot_positions` 依代號跨券商合併(例如 QLD 同時在嘉信與盈透 → 合併成一筆,
  股數相加、加權平均成本)。
- 終端機表格那一段(逐檔列)仍只列永豐持倉是正常的;美股逐檔請看儀表板的
  「持倉明細」(`positions_current` 已含三家)。

## 前置條件(只需做一次)
先把美股對帳單匯入,讓 `positions_current` 內有嘉信 / 盈透:

```bash
python import_statements.py schwab.csv
python import_statements.py U5529822_TRANSACTIONS.csv   # 盈透
python rebuild_history.py                                # 建立每日匯率 + 合併曲線
```

之後每次 `python run.py` 同步永豐時,就會自動把美股一起換算進總資產。
> 註:`aggregate.write_combined_snapshot` 與 `FXBackfiller` 已是專案內新模組
> (`core/aggregate.py`、`core/fxrate.py`),不需額外安裝套件。
