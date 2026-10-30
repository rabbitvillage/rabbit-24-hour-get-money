# FR-20261028 第二帳號 acct2 獨立 btc5m（交易關閉）

1. 改了什麼：新增 systemd 實例 btc5m-sim@acct2，設定檔 btc5m_sim/.asset-acct2.env，資料夾 btc5m_sim/data-acct2/。沒改任何程式碼。
2. 為什麼：建構多機器人，讓新帳號之後能獨立運作；先用同一套策略模擬跑，確認環境可用。
3. 修改前：racknerd 上有 btc（實盤）、btc15、eth、sol 四套。
4. 修改後：多一套 acct2，策略規則與 btc 主實例相同（讀共用 .env），EXEC_MODE=off，不下單；
   主帳號金鑰（CLOB_API_*、SIGNER_*）在 acct2 設定中清空，Telegram 與儀表板推送也清空，避免重複通知。
5. 影響檔案：btc5m_sim/.asset-acct2.env（新）、btc5m_sim/data-acct2/（新）、/etc/systemd/system/multi-user.target.wants/btc5m-sim@acct2.service（啟用連結）。
6. 交易／風控影響：無。主實例 btc5m-sim 仍為 live，未改動。acct2 不下單。
   注意：共用 .env 裡的策略參數改動會同時影響 acct2。
   之後接新帳號時：在 .asset-acct2.env 填入新帳號自己的金鑰與錢包，再改 EXEC_MODE，需另行確認。
停用：systemctl disable --now btc5m-sim@acct2

## 追加 2026-10-28 23:20：接上簽名服務
- .asset-acct2.env 填入 SIGNER_URL（與主帳號同一個簽名服務）、SIGNER_TOKEN（acct2 專用）、SIGNER_ADDRESS=0x0BdD…9eA9；EXEC_MODE 仍為 off，CLOB_API_* 仍空。
- 新帳號是存款錢包（簽名類型 3），資金 14.95 pUSD 在 0x854e…87fb；現有 live.py 只支援類型 1，不可打開交易。
- 主實例與 btc15/eth/sol 未改動，重啟後皆 active。

## 追加 2026-10-28 深夜：live.py 支援存款錢包（簽名類型 3）
1. 改了什麼：
   - `identity()` 多讀簽名服務回傳的 `walletType`（沒有時當 1）。
   - 新增 `order_parties()`：類型 1 → maker = 代理錢包、signer = EOA（主帳號，原行為）；類型 3 → maker = signer = 存款錢包；其他類型拒絕。
   - 下單、`dry-sign`、餘額查詢（`signature_type`）都改用帳號自己的錢包類型。
   - `setup`（產生 API 金鑰）在目標檔已屬於別的帳號時拒絕寫入，避免 acct2 蓋掉主帳號 `.live.env`。
2. 為什麼：acct2（kuaao1）是 2026-05 後開的存款錢包，原程式寫死類型 1，無法下單。
3. 修改前：所有訂單都是類型 1。
4. 修改後：主帳號仍是類型 1（簽名服務對主帳號回 walletType 1，程式路徑不變）；acct2 用類型 3。
5. 影響檔案：`btc5m_sim/live.py`、`tests/test_btc5m_live.py`（新增 5 項）、本檔。
6. 交易／風控影響：主帳號下單路徑不變；風控（最低餘額、每日停損、股數上限）程式未改。acct2 是否交易仍由 `EXEC_MODE` 決定，需 fish 確認才改。
   測試：test_btc5m_live 55 項、sim 38、feeds 9、review 7 全部通過。

## 追加 2026-10-28 23:55：主機部署與驗證
- racknerd 已 pull 到 d2c4794，只重啟 btc5m-sim@acct2；主實例與 btc15／eth／sol 未重啟（記憶體中仍是舊碼，下次重啟載入新碼，類型 1 路徑不變）。
- .asset-acct2.env 加 `LIVE_ENV_FILE` 指向自己；`live setup` 產生 acct2 自己的 API 金鑰寫入該檔；主帳號 .live.env 未變。
- `live balance`：14.946103（類型 3 查詢成功，金鑰可用）；`live dry-sign`：真實市場預演通過（5 股 @0.95，$4.75）。
- 仍為 EXEC_MODE=off。開交易前需決定：最低餘額門檻（目前 $80，acct2 只有 $14.95）與 acct2 的股數／停損設定。

## 追加 2026-10-29 00:33：acct2 開啟交易（fish 同意）
- `.asset-acct2.env`：`EXEC_MODE=live`；acct2 專用風控 `MIN_BALANCE_USDC=5`、`MIN_BALANCE_WARN=8`、`LIVE_SIZE_MODE=fixed`、`LIVE_SHARES=5`、
  `MAX_DAILY_LOSS_VARIANT=5`、`MAX_DAILY_LOSS_TOTAL=10`。其餘（規則 w60_m95_s20／w45_m95_s20、美股時段暫停、每盤 1 單、FAK）沿用共用 .env。
- 修改前備份：主機 `.asset-acct2.env.bak-before-live-20261028`。
- acct2 沒有 Telegram 通知（TG_TOKEN 空），要看結果需查 `journalctl -u btc5m-sim@acct2` 或 `data-acct2/live_orders.csv`。
- 緊急停止：`touch /opt/rabbit-24-hour-get-money/btc5m_sim/data-acct2/STOP`（只停 acct2）；或 `EXEC_MODE=off` 後重啟 `btc5m-sim@acct2`。
- 對交易影響：acct2 開始用真錢下單（每筆 5 股，約 $4.75–4.95）；主帳號與其他實例不受影響。第一筆類型 3 真單尚待驗證。
