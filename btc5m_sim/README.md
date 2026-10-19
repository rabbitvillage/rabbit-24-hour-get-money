# btc5m_sim — BTC 5 分鐘盤「最後 45 秒買 98¢」即時模擬（階段 0，不下單）

不需要錢包、不下任何訂單，只讀 Polymarket 公開資料。Python 3.9+，只用標準庫。

每一場 5 分鐘盤：
1. 從剩 45 秒到剩 5 秒，每秒讀一次兩邊的訂單簿。
2. 第一次看到某一邊最低賣價在 0.98~0.99、而且至少有 5 股 → 記一筆「假想買進 5 股」，記下剩幾秒、賣單深度、抓訂單簿花了幾毫秒。
3. 收盤後等結算，記錄輸贏、損益、結算花了多久。

結果寫在 `btc5m_sim/data/sim_trades.csv`。Telegram 會播：啟動、每小時摘要、每天摘要，以及每一筆模擬輸掉的單。

## 影子變體與 BTC 距離（同一場盤同時評分，仍不下單）

同一次訂單簿輪詢也拿來評分其他進場規則（`VARIANTS`，預設 `w45_p98,w30_p98,w20_p98,w45_p99,w30_p99,w45_p98_s,w45_p98_s10`，
意思是「最後 45／30／20 秒、最低買價 98¢ 或 99¢」；`_s` ＝ 加距離過濾：BTC 現價相對「開盤時我們自己抓的現價」要站在買的那一邊，
`_s10` ＝ 而且距離至少 10 美元。這是針對 2026-10-19 16:50 那場「貼線翻盤」加的），每場每個變體各寫一列到 `btc5m_sim/data/sim_variants.csv`。
`sim_trades.csv` 的欄位不變，仍是主規則（ENTRY_WINDOW／MIN_PRICE）。

另外從收盤前 `SPOT_WINDOW`（預設同 `BOOK_WINDOW`＝150）秒起，每秒抓一次 BTC 現價（Coinbase 交易所即時買賣價中點，失敗改 Kraken 最後成交價），用來記錄：

- `spot_at_start`／`ptb_err`：開盤那一刻我們自己抓的現價，以及它和真正 priceToBeat 的差。
  gamma 的 priceToBeat **要到收盤後才會出現**，所以盤中只能用 `spot_at_start` 當基準價的替代值；`ptb_err` 告訴你這個替代值差多少。
  所有距離欄位都是結算後拿到 priceToBeat 再回填的。
- `spot_at_window`／`dist_at_window`：該變體窗口打開那一刻的現價與「現價 − priceToBeat」（美元，正值偏 Up）
- `spot_at_entry`／`dist_at_entry`：假想進場那一刻的現價與距離
- `twap_dist_at_entry`：進場時「已經落在最後 60 秒結算窗口內的現價平均」− priceToBeat。
  翻盤需要剩餘時間的均價反向偏離約 `twap_dist × (60 − secs_left) / secs_left`
- `min_abs_dist`：這場最後 60 秒內現價離 priceToBeat 最近的距離；`spot_samples`：取到幾個樣本

Telegram 每小時／每日摘要會多一段各變體的進場數、輸的筆數與損益。每小時／每日摘要只算程序啟動後的場次，
`systemctl restart` 會歸零摘要；CSV 不受影響。

    python3 -m unittest tests.test_btc5m_sim   # 純邏輯測試，不需要網路

## 部署到 VPS（一行）

    git clone https://github.com/rabbitvillage/rabbit-24-hour-get-money.git && cd rabbit-24-hour-get-money && git checkout claude/project-thread-27gj0j && sudo bash btc5m_sim/deploy.sh

會問一次 Telegram bot token（輸入時不顯示），存在 `btc5m_sim/.env`（權限 600、不進 git）。
裝好後先在 Telegram 對 bot 傳一次 `/start`，下一則播報就會送到那個聊天室。

    journalctl -u btc5m-sim -f        # 看即時紀錄
    systemctl restart btc5m-sim       # 改 .env 後重啟
    systemctl disable --now btc5m-sim # 停掉

需要能連到：gamma-api.polymarket.com、clob.polymarket.com、api.telegram.org。

## 本機直接跑

    set -a; . btc5m_sim/.env; set +a; python3 -m btc5m_sim.sim
