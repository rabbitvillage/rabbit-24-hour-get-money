# FR-20261028-official-line：距離過濾的基準線改用官方開盤價（Chainlink 60 秒平均），兩邊換成同一個尺度

- 狀態：active（本機完成、112 個測試通過；主機尚未部署；fish 要選部署方式：直接生效，或 `LINE_TWAP=0` 先只記錄）
- 來源：fish 10-28：「這不是策略的一環，這是結構問題的修正，所以我們可以修它。」調查見 `Docs/operations/todo.md` 第 14 項

## 1. 這次改了什麼

1. `feeds.py` 新增 `ChainlinkFeed`：訂閱 Polymarket 公開串流 `wss://ws-live-data.polymarket.com`，topic 是 `crypto_prices_chainlink`，不用登入，每秒一筆官方結算來源的價格。一條連線只訂一個幣（同一條連線訂兩個幣，只會收到第一個）。**訂閱的 filters 必須是不含空格的 JSON**（`{"symbol":"btc/usd"}`）：Python 預設的 `{"symbol": "btc/usd"}` 會收到快照，之後一筆更新都沒有（主機冒煙測試抓到：不含空格 15 秒收到 16 筆，含空格 0 筆），已加測試鎖住格式。連著卻 20 秒沒收到資料就自動重連。另外新增 `window_mean(lo, hi)`：算 60 秒平均，缺 ≤3 秒時用前一秒的價格補。也新增 `basis()`：取 Chainlink 減 Coinbase 的中位數，兩邊對時時 Coinbase 往前 0.375 秒。`SpotFeed` 多記最近的 Coinbase 報價歷史。
2. `sim.py` 新增 `LineRef`。每場的基準線 = 開盤前 60 秒（[open−60, open)）的 Chainlink 平均，也就是官方 priceToBeat 的算法，再減掉最近 120 秒的 Chainlink−Coinbase 價差，換成 Coinbase 的尺度。距離 `dl = Coinbase 現價 − 基準線`，等於「(Coinbase 現價 + 價差) − 官方開盤價」。
3. 退回舊基準線的情況：串流裡沒有完整的開盤窗口（例如程式在開盤後才啟動），或者 `LINE_TWAP=0`。舊基準線是開盤時讀的 Polygon 鏈上喂價。價差超過 10 bps 視為資料異常，不採用。
4. 新增 `sim_line.csv`，每場一列：用了哪條線、官方算法的開盤價、和 priceToBeat 的誤差、舊鏈上開盤價和它的誤差、價差、官方基準線。**`LINE_TWAP=0` 時照樣計算並記錄**，只是決策還用舊線。
5. 看板文字：「鏈上開盤價」改為「官方開盤價（Chainlink 60 秒平均）」。

門檻 $20、60／45 秒窗口、0.80～0.95 價位帶、震盪、冷卻、盤口一致性，全部不變。

## 2. 為什麼

- 官方規則（gamma 市場說明）：結算是 Chainlink 的 60 秒 TWAP。priceToBeat 是開盤前 60 秒平均，finalPrice 是最後 60 秒平均。
- 舊基準線和官方開盤價的差距：BTC |p50| 0.58、|p90| 2.32 bps（約 $19，幾乎等於整個 $20 門檻）；SOL |p90| 3.17 bps。
- 舊做法拿 Coinbase 減 Chainlink 系的價格，兩個不同尺度相減。Coinbase 平均比 Chainlink 低約 $3（10-28 錄製 $2.4～4.5），所以 `dl` 一直偏向 Down 約 $3。
- 驗證：公開串流每秒價格的 60 秒平均，和 gamma 的 finalPrice 差 −$0.9～+$1.4（7 場，平均絕對誤差 $0.4）。15 分鐘盤的收盤價和同時間收盤的 5 分鐘盤完全相同，同一個算法兩種盤都適用。

## 3. 修改前的行為

開盤時讀一次 Polygon 鏈上喂價當基準線（讀不到就用開盤時的 Coinbase 價），之後一律拿 Coinbase 現價去減它。

## 4. 修改後的行為

開盤 3 秒後，用串流裡開盤前 60 秒的價格算出官方開盤價。之後每 5 秒更新一次 Chainlink−Coinbase 價差，基準線 = 官方開盤價 − 價差。開盤 10 秒後還算不出來（串流缺資料），這一場就用舊基準線，`sim_line.csv` 的 `line_src` 記 `fallback`。

## 5. 影響的檔案

`btc5m_sim/feeds.py`、`btc5m_sim/sim.py`、`tests/test_btc5m_feeds.py`（Chainlink ×4、Spot 歷史 ×1）、`tests/test_btc5m_sim.py`（LineRef ×4，含串流出錯時退回舊線）、`Docs/custom-queries/replay_line_fix.py`（新舊基準線重放）。

## 6. 對交易、下單、風控、資料判斷的影響（**會改變進場**）

這是量測修正，但它會改變哪些場次通過距離過濾。歷史重放（`replay_line_fix.py`，BTC 2,424 場，影子規則，扣手續費每股淨利；舊線重放和機器人自己的影子帳 98～99.8% 一致）：

| 規則 | 舊基準線 | 新基準線 |
|---|---|---|
| w60_m95_s20 | 238 筆，輸 6.3%，+7.64 | 192 筆，輸 6.2%，+3.02 |
| w45_m95_s20 | 85 筆，輸 5.9%，+2.34 | 43 筆，輸 2.3%，+2.62 |

- 分邊（w60）：Up 從 90 筆、輸 6.7%、+2.90，變成 101 筆、輸 5.0%、+2.90。Down 從 148 筆、輸 6.1%、+4.74，變成 91 筆、輸 7.7%、+0.12。舊線因為偏 Down 約 $3，放進比較多 Down 單；這段期間 BTC 從 82k 跌到 80.5k，那些 Down 單剛好是賺的。
- 只有新線會進的 61 筆、和只有舊線會進的 107 筆，每股淨利都只有 +1 分左右，差距大多來自 5 場兩條線方向相反的場次（新線那邊輸 4 場）。樣本小，不能下「新線比較差」的結論，但也**沒有證據顯示新線會減少輸單**。
- 每筆大小、價位帶、停損、冷卻都不變。SOL 這兩條規則歷史上幾乎不進場（2 筆），影響可忽略。
- 驗收期影響：直接生效會改變約 1/4 的進場組合，11-02 的驗收數字會混入兩套基準線。

## 7. 驗證與驗收邊界

- 本機 `python3 -m unittest discover -s tests` 112 個測試通過；主機 Python 3.12 跑 btc5m 的測試也全過（在 /tmp 的複本，不碰正式目錄）。
- 主機冒煙測試（只讀，新 feeds.py 在 /tmp 跑，不碰機器人）：10-28 開盤 1793153400 那場，我們算的官方開盤價 80,819.390，gamma priceToBeat 80,819.002，差 **+$0.39**；Chainlink−Coinbase 價差 +$4.95；3 分鐘收到 193 筆 Chainlink 更新。
- 部署後要確認：啟動行出現 `line for the distance filter: ...`；每場 `sim_line.csv` 一列，`line_src` 是 `chainlink_twap+gap`（或關閉時是 `fallback`），`cl_open_err` 大多在 ±$1.5 以內；看板顯示「官方開盤價」。
- 轉為 verified 的條件：部署後一天的 `sim_line.csv` 中，`cl_open_err` 的 |p90| < $2，`fallback` 只出現在重啟的那一場。
