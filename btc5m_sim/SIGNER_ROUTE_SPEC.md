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

---

## 2026-10-21 補充：對照官方 Rust SDK（polymarket_client_sdk_v2 0.7.0，本機 cargo registry）與 rcss-sniper 現有 Worker 後的修正

**上面的「請求」格式要改成與 rcss／copy 路由一致的 V2 結構化欄位**（Worker 已有 `signOrder()`，domain `Polymarket CTF Exchange` v2、chainId 137、依 negRisk 選交易所合約）：

```json
{ "op": "btc5m_order", "idempotencyKey": "<variant>:<slug>", "dry": false,
  "order": { "salt": 479249096354, "maker": "<代理錢包>", "signer": "<EOA>", "tokenId": "1234…",
             "makerAmount": "4800000", "takerAmount": "5000000", "side": 0, "signatureType": 1,
             "timestamp": "1792570500123", "metadata": "0x00…00", "builder": "0x00…00", "negRisk": false } }
```

- `timestamp` 是**毫秒**（SDK `SystemTime::now().as_millis()`）；`salt` 是 ≤ 2^53 的整數；`metadata`／`builder` 固定 zero bytes32。
- 市價 FOK 買單的金額規則（交易所會拒絕不合規的）：`makerAmount` = USDC **以分為單位**（例：5 股 × 0.96 = $4.80 → `4800000`），`takerAmount` = `usdc ÷ price` 取到 (tick 小數位＋2) 位截尾（0.001 tick → 5 位）。不是「股數 × 價格」。
- Worker 回 `{ ok, signature, digest, signerAddress }`（與 copy_order 相同）。Worker 用 `btc5m_whoami` 回 `address`（EOA）與 `proxyWallet`（maker），主機據此填 maker／signer，不用自己推導。
- 主機 POST 的 wire 格式（SDK `OrderV2WithSignature`）：
  `{"order": {salt(數字), maker, signer, tokenId, makerAmount, takerAmount, "side": "BUY", "expiration": "0", signatureType(數字), timestamp(字串毫秒), metadata, builder, signature}, "orderType": "FOK", "owner": "<api key>"}`。
- L2 標頭：`POLY_ADDRESS`=EOA、`POLY_API_KEY`、`POLY_PASSPHRASE`、`POLY_TIMESTAMP`（秒）、`POLY_SIGNATURE`=base64url(HMAC-SHA256(base64url_decode(secret), ts+"POST"+"/order"+body))。
- 回應 `PostOrderResponse`：`success`、`status`（`matched` ＝ 成交）、`orderID`、`makingAmount`、`takingAmount`、`errorMsg`。

**L2 憑證怎麼來（不碰私鑰）**：加 `btc5m_auth` op，Worker 對 ClobAuth EIP-712 簽名（domain `ClobAuthDomain` v1 chainId 137；struct `{address, timestamp(string 秒), nonce(uint256)=0, message:"This message attests that I control the given wallet"}`），
回 `{address, signature}`；主機用 `POLY_ADDRESS/POLY_SIGNATURE/POLY_TIMESTAMP/POLY_NONCE` 四個標頭 GET `https://clob.polymarket.com/auth/derive-api-key` 取得 `{apiKey, secret, passphrase}`，自己寫進 `.live.env`（0600）。這等於 rcss 的 `derive-l2`，但由 Worker 代簽，主機與對話都不需要私鑰。

**Worker 程式位置與做法**：`~/Projects/rcss-sniper/cf-signer/src/signer.mjs` 照 `copy_*` 的樣子加 `btc5m_*`（獨立 `BTC5M_AUTH_TOKEN`、獨立 DO `Btc5mPolicy`：每日買入名目上限 $200、簽名 1,500 次、冪等鍵一生一次）；白名單 `verifyBtc5mToken`：gamma 查 slug 以 `btc-updown-5m-` 開頭、token 在 `clobTokenIds`、`acceptingOrders`；`validateBtc5mOrder`：只 BUY、signatureType 1、price 0.50～0.99、≤10 股、單筆 ≤ $10；`wrangler.jsonc` 加 `BTC5M_POLICY` 綁定與 `v3` 遷移；測試 `test/btc5m.test.mjs` 仿 `copy.test.mjs`。

**目前狀態（10-21 17:00）**：主機 `live.py` 仍是舊版意圖格式（dry 模式可用，真單路徑尚未改成 V2）。自動模式把「寫真實下單程式」擋下來了，V2 真單路徑與 Worker 路由要在允許的權限模式下完成，或由 fish 依本規格實作。
