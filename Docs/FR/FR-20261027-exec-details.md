# FR-20261027-exec-details：執行面技術細節強化（決策輪詢、沒吃到診斷、手續費欄）

狀態：active（部署後觀察：sim_book 密度不變、live_miss.csv 有資料、live_results 的 fee 欄非 0）

fish 10-27：「強化我們所能做的任何技術上的細節」，策略與風控參數不動。先量測再改：

- 時鐘：主機 NTP 已同步（timedatectl NTPSynchronized=yes），secs_left 可信。
- websocket 盤口會不會被誤判成舊而退回 REST（350 ms）：10-26 18:00 起最後 60 秒的 26,290 次輪詢，72% 在 50 ms 內、24% 50～300 ms、只有 4% ≥ 300 ms；ws_vs_rest.py 三場最後 60 秒裡領先方 0.80～0.95 的秒數沒有任何一秒兩邊盤口超過 2.5 秒沒更新。結論：不值得改 feeds.py 的新鮮度判定，風險大於收益，不做。

1. **改了什麼**
   - `sim.py`：決策輪詢 `POLL` 預設 0.5 → 0.25 秒；新增 `BOOK_LOG_EVERY`（預設 0.5 秒）讓 sim_book.csv 仍然每 0.5 秒一列，紀錄檔不會變大。
   - `live.py`：FAK 沒成交（unfilled／rejected）後，背景執行緒立刻直連交易所 REST /book 抓那個 token 的盤口，寫進 `live_miss.csv`（看到的賣價、我們的上限、當時最佳賣價、上限以下還有幾股、前四層、距交易所回覆幾毫秒）。
   - `live.py` settle()：`fee` 欄改記真實手續費 7%×p×(1−p)×股數（鏈上核對過），`LIVE_FEE_RATE` 可調；**pnl 仍是毛利**，所以每日停損判定完全不變。
   - `review.py`：載入 fee，摘要行加「（扣手續費 ±x）」；review_log.csv 欄位不變。
2. **為什麼**：一筆單從看到盤口到決定平均落後 0.25 秒（0.5 秒輪詢），改 0.25 秒平均省 0.125 秒、最壞省 0.25 秒；沒吃到的單 10-27 已達 50%，需要知道是價格跑掉還是盤口錯，才能決定之後 slip 要不要調；手續費漏記讓 11-02 驗收數字偏高。
3. **修改前**：0.5 秒輪詢；沒吃到只有交易所一句 "no orders found to match"；fee 永遠 0。
4. **修改後**：0.25 秒輪詢、盤口紀錄密度不變；每筆沒吃到的單多一列診斷；fee 欄有值、review 摘要顯示淨值。
5. **影響檔案**：`btc5m_sim/sim.py`、`btc5m_sim/live.py`、`btc5m_sim/review.py`。
6. **交易／下單／風控／資料判斷**：規則、價位帶、股數、停損、冷卻、事件停單都不變。決策時間平均提早約 0.1 秒（同一規則、同一條件，只是更早看到）。pnl 毛利不變 → 停損不變。live_miss.csv 只是診斷。
7. **驗證**：92 個單元測試通過；部署後看 journal 的 `start [BTC] ... poll 0.25s`；隔天比 sim_book.csv 每場列數（應仍約 300 列）；有沒吃到的單時看 `live_miss.csv`；下一筆結算後 `live_results.csv` 最後一列 fee 非 0。
