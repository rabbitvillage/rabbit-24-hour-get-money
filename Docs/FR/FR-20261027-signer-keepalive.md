# FR-20261027-signer-keepalive：送單延遲——簽名 Worker 連線保持、身分預先取得

狀態：active（上線後要在接下來的真單 live_timing.csv 看到 sign_ms 下降才算 verified）

1. **改了什麼**：`btc5m_sim/live.py`
   - `worker()`（送意圖給簽名 Worker 的函式）改用一條保持連線的 HTTPS 連線（`_signer_pool`），socket 壞掉時換新連線重試一次；`SIGNER_KEEPALIVE=0` 可退回原本每次新建連線的 urllib 路徑。
   - 新背景執行緒 `_signer_keep_warm()`：啟動時先呼叫一次 `identity()`（whoami），之後每 `SIGNER_WARM_EVERY` 秒（預設 60）對簽名路徑發一個 GET（Worker 回 405，約 0.1 秒）讓連線不被收掉。只在 `EXEC_MODE=live` 啟動。
2. **為什麼**：fish 10-27「提升效能、降低延遲」。live_timing 顯示一筆單從看到盤口到交易所回覆中位數 1.0～1.3 秒：簽名 550～740 ms、送單 350 ms。量測主機到 Worker 邊緣：TCP 30 ms＋TLS 75 ms，每次簽名都重新握手；重啟後第一筆還多付一次 whoami（200 ms）。Worker 內部 whoami 83～198 ms、dry 驗證 174～206 ms，所以真正簽名時多出的 400～1,000 ms 在 Worker 內部（原始碼不在這台機器，無法從主機端改）。
3. **修改前**：每次簽名新建 TCP＋TLS 連線；第一筆單先打 whoami。
4. **修改後**：簽名走暖連線，預期每筆省約 100 ms；重啟後第一筆不再多付 200 ms。其他路徑（guard、tick、POST /order 的連線池）不變。
5. **影響檔案**：`btc5m_sim/live.py`（`worker()`、新增 `_signer_conn()`、`_signer_keep_warm()`、`init()` 多起一條執行緒）。主機四個 sim 服務要重啟才會載入。
6. **交易／下單／風控／資料判斷**：下單內容、價格、股數、規則、停損、冷卻都不變；只改傳輸方式。風險：Worker 邊緣若不支援長連線，第一次會在 socket 錯誤後自動換新連線（多一次握手，等於原本行為）。
7. **驗證**：本機 `python3 -m unittest discover -s tests`；主機先用新模組跑 whoami 三次看第二次起是否省掉握手；重啟後看 journal 的 `live: mode=live` 行與之後真單的 `live timing ... sign` 數字。順帶的診斷：`Docs/custom-queries/ws_vs_rest.py` 在 12:03～12:18 比對 websocket 盤口與 REST，最後 90 秒內領先方最佳賣價每秒相同（第一場 37/37），所以「找不到對手單」不是盤口舊，而是 0.81～0.92 的價位在 1～1.3 秒內跑掉了；降延遲是正解。
