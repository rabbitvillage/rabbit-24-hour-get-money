# 簽名 Worker `btc5m` 路由規格（給 rcss-signer 部署用）

目的：BTC 5 分鐘盤的下單程式（racknerd 上的 `btc5m_sim/live.py`）**不持有私鑰**。它只送「下單意圖」給 Worker，Worker 用既有的代理錢包金鑰（signature type 1）建好並簽好 CLOB V2 的 Order，回傳簽名後的 order JSON；主機再用自己的 L2 憑證把它 POST 到 `https://clob.polymarket.com/order`（FOK）。做法與 2026-09-24 加的 `copy_*` 路由相同。

## 端點

`POST /btc5m/sign`
`Authorization: Bearer <BTC5M_AUTH_TOKEN>`（獨立 token，不共用 rcss／copy 的）
`Content-Type: application/json`

## 請求（主機送來的意圖）

```json
{
  "slug": "btc-updown-5m-1792480800",
  "token_id": "1234…",
  "outcome": "Down",
  "side": "BUY",
  "price": 0.99,
  "size": 5,
  "maker_amount": "4950000",
  "taker_amount": "5000000",
  "fee_rate_bps": "0",
  "tick": "0.001",
  "variant": "w45_p98_s",
  "secs_left": 30.5,
  "dry": false
}
```

## Worker 必須自己重新檢查（不信任主機送來的值）

1. `slug` 以 `btc-updown-5m-` 開頭；用 gamma 查 `slug` → 確認 `token_id` 真的屬於該市場、`outcome` 與 token 對得上。
2. `side` 只能是 `BUY`。
3. `0.50 ≤ price ≤ 0.99`，且 price 已對齊 `tick`。
4. `size ≤ 10` 股；`maker_amount == round(size × price × 1e6)`、`taker_amount == size × 1e6`，自己重算，不用主機的。
5. 速率：同一 `slug`＋`variant` 只簽一次；每分鐘最多 15 次；每日簽名總額（Σ maker_amount）≤ 設定上限（建議 200 USDC）。
6. `dry: true` 時只做以上檢查並回 `{"ok": true, "dry": true}`，**不簽名**。
7. 全部通過才建 Order：`maker` = 代理錢包、`signer` = EOA、`taker` = 0x0、`feeRateBps` = 主機給的值或 Worker 自查 `/fee-rate` 取 `base_fee`（取大者）、`nonce` = "0"、`expiration` = "0"、`signatureType` = 1、`salt` 隨機，EIP-712 domain 與 digest 由 Worker 自己重建（與 rcss 路由同一套，不接受任何預算雜湊）。

## 回應

成功：
```json
{ "ok": true, "order": { "salt": "…", "maker": "0x…", "signer": "0x…", "taker": "0x0000000000000000000000000000000000000000",
  "tokenId": "1234…", "makerAmount": "4950000", "takerAmount": "5000000", "expiration": "0", "nonce": "0",
  "feeRateBps": "0", "side": "BUY", "signatureType": 1, "signature": "0x…" } }
```
拒絕：HTTP 4xx，`{ "ok": false, "reason": "price out of range" }`。主機把 reason 原樣記進 `live_orders.csv`。

## 主機端之後怎麼送（已寫在 live.py，供對照）

```
body = {"order": <上面的 order>, "owner": <CLOB_API_KEY>, "orderType": "FOK", "postOnly": false}
POST https://clob.polymarket.com/order
POLY_ADDRESS=<EOA>  POLY_API_KEY  POLY_PASSPHRASE  POLY_TIMESTAMP=<unix 秒>
POLY_SIGNATURE = base64url( HMAC-SHA256( base64url_decode(secret), timestamp + "POST" + "/order" + body 原字串 ) )
```

## 主機要的設定（`btc5m_sim/.live.env`，0600，fish 自己用 heredoc 寫，Claude 只核對有沒有）

```
EXEC_MODE=live
SIGNER_URL=https://<worker>/btc5m/sign
SIGNER_TOKEN=<BTC5M_AUTH_TOKEN>
SIGNER_ADDRESS=<簽名 EOA 位址>
CLOB_API_KEY=…  CLOB_API_SECRET=…  CLOB_API_PASSPHRASE=…   （代理錢包的 L2 憑證，用 rcss derive-l2 衍生）
```

## 上線順序

1. Worker 部署 `btc5m/sign` 路由，先用 `dry: true` 的請求測通（主機 `EXEC_MODE=dry` 不會呼叫 Worker，要手動 curl 測）。
2. 主機 `.live.env` 填好 → `EXEC_MODE=live`、`LIVE_VARIANTS=` 留空（11 條全上）、`LIVE_SHARES=5` → `systemctl restart btc5m-sim`。
3. Telegram 會收到每一筆真單的成交／未成交／錯誤、每筆輸單、每條規則的停損；`touch /opt/rabbit-24-hour-get-money/btc5m_sim/data/STOP` 立刻全停。
