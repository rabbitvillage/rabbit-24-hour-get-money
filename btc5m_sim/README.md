# btc5m_sim — BTC 5 分鐘盤「最後 45 秒買 98¢」即時模擬（階段 0，不下單）

不需要錢包、不下任何訂單，只讀 Polymarket 公開資料。Python 3.9+，只用標準庫。

每一場 5 分鐘盤：
1. 從剩 45 秒到剩 5 秒，每秒讀一次兩邊的訂單簿。
2. 第一次看到某一邊最低賣價在 0.98~0.99、而且至少有 5 股 → 記一筆「假想買進 5 股」，記下剩幾秒、賣單深度、抓訂單簿花了幾毫秒。
3. 收盤後等結算，記錄輸贏、損益、結算花了多久。

結果寫在 `btc5m_sim/data/sim_trades.csv`。Telegram 會播：啟動、每小時摘要、每天摘要，以及每一筆模擬輸掉的單。

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
