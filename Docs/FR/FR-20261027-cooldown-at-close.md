# FR-20261027-cooldown-at-close：收盤即判輸贏啟動冷卻、重啟補冷卻、執行層地板、盤口一致性開關

- 狀態：active（fish 10-27 裁示部署；20:00:35 主機 c52d2d3、四服務 active；等第一筆輸單看到 `closing book says ...` 先於 `live settle` 後轉 verified）
- 來源：`Docs/operations/structural-review-20261027.md`（fish：「用同樣的道理檢查整套系統」）

## 1. 這次改了什麼

1. `sim.py`：收盤後 `PROVISIONAL_AFTER`（預設 2）秒用 ws 盤口讀一次兩邊最佳賣價；一邊 ≤ `PROV_LOSER_ASK`（0.05）就是輸家（`provisional_winner`），立刻 `live.note_market(..., provisional=True)` 啟動或延長冷卻；正式結算照舊再呼叫一次。負值關閉。
2. `live.py` `_settle_when_resolved`：重啟後補結算的輸單也呼叫 `note_market`。
3. `live.py` `guard()` 多一個 `ask` 參數：`LIVE_ASK_FLOOR`（預設 0.80），規則決定的賣價低於它直接 skip（帳本記 skipped）。
4. `sim.py` `BOOK_GAP_MAX`（預設 0＝關）：領先方賣價＋另一邊賣價 −1 超過門檻就不進場（`gap_block`，決策紀錄 reason=gap，看板文字「盤口不一致」）。

## 2. 為什麼

- gamma 標記 `closed` 的時間：BTC 2,484 場中位數收盤後 301 秒、75% 超過 240 秒；下一場的進場窗口在收盤後 240～300 秒。冷卻靠結算啟動，等於輸一筆後的下一場永遠不受保護。28 筆 BTC 真單輸單有 9 筆在冷卻生效前又送了真單，SOL 3 筆有 2 筆。
- 重啟恢復路徑只記帳不冷卻，是程式漏洞。
- 執行層沒有地板：10-21 地板 0.50 時在 0.55 輸過一筆；fish 10-27：「80～97 可以接受，50 太離譜」。
- 18:34 決定時兩邊賣價和 1.11（Down 0.84、Up 0.27）：洞，不是價格。影子 183 筆：和 >1.05 的 18 筆 4 輸 14 贏。策略凍結中，所以做成開關。

## 3. 修改前的行為

冷卻在 gamma 標記結算後才開始；重啟補結算不冷卻；執行層接受 0.5～0.99 的任何限價；盤口不一致時照買。

## 4. 修改後的行為

收盤 2 秒後若盤口明確（一邊 ≤0.05），輸單的冷卻立刻生效，journal 有 `closing book says X won -> ... cool-down started before resolution`，通知文字多「收盤盤口判定」；盤口不明確（兩邊都不在地板、沒有 ws 盤口）就等正式結算，行為同以前。重啟補結算的輸單也冷卻。賣價 <0.80 的決定被 guard 擋下（現有真單規則地板本來就是 0.80，零影響）。`BOOK_GAP_MAX=0` 時 gap 過濾不動作。

## 5. 影響的檔案

`btc5m_sim/sim.py`（常數、`gap_block`、`provisional_winner`、watch() 收盤段、rule_state、summary why）、`btc5m_sim/live.py`（`ASK_FLOOR`、`guard(ask=)`、`note_market(provisional=)`、`_settle_when_resolved`）、`tests/test_btc5m_live.py`、`tests/test_btc5m_sim.py`、`Docs/custom-queries/struct_ledger_checks.py`、`struct_chain_checks.py`。

## 6. 對交易、下單、風控、資料判斷的影響

- 冷卻提早約 5 分鐘：輸一筆後的下一場不再送真單（以前會）。少做的量：每次輸單後最多一場。
- 誤判風險：收盤前最後 2 秒內翻盤且價格從 ≤0.05 翻到贏，提早的冷卻會白等 45 分鐘；估計每場 <1%，而且正式結算不會「取消」冷卻（設計上只延長）。
- 執行層地板、gap 開關：現況零影響。停損、每筆大小、價位帶不變。

## 7. 驗證與驗收邊界

- 本機 103 個測試通過（新增：執行層地板 ×2、provisional 冷卻、重啟補冷卻、provisional_winner ×2、gap_block）。
- 部署後：下一筆輸單的 journal 應先出現 `closing book says ...` 再出現 `live settle`，`COOLDOWN_UNTIL` 在收盤後幾秒內更新。
