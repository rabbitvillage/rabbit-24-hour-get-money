"""Python twin of record_cl_cb.mjs (for the host, which has websocket-client). Read-only. Usage: python3 record_cl_cb.py <minutes> <out>"""
import json, sys, threading, time
import websocket

mins, out = float(sys.argv[1]), open(sys.argv[2], "w"); lock = threading.Lock()
def w(o):
    with lock: out.write(json.dumps(o) + "\n"); out.flush()
def rtds():
    while True:
        try:
            ws = websocket.create_connection("wss://ws-live-data.polymarket.com", timeout=20)
            ws.send(json.dumps({"action": "subscribe", "subscriptions": [{"topic": "crypto_prices_chainlink", "type": "*", "filters": "{\"symbol\":\"btc/usd\"}"}]}))
            last = time.time()
            while True:
                if time.time() - last > 5: ws.send("PING"); last = time.time()
                s = ws.recv(); r = int(time.time() * 1000)
                if not s or s[0] != "{": continue
                m = json.loads(s); p = m.get("payload") or {}
                if p.get("data"):
                    for d in p["data"]: w(dict(src="cl", sym=p.get("symbol", "btc/usd"), ts=d["timestamp"], v=d["value"], snap=1, recv=r))
                elif "value" in p:
                    w(dict(src="cl", sym=p["symbol"], ts=p["timestamp"], v=p["value"], srv=m.get("timestamp"), recv=r))
        except Exception:
            time.sleep(1)
def coinbase():
    while True:
        try:
            ws = websocket.create_connection("wss://ws-feed.exchange.coinbase.com", timeout=20)
            ws.send(json.dumps({"type": "subscribe", "product_ids": ["BTC-USD"], "channels": ["ticker"]}))
            while True:
                m = json.loads(ws.recv()); r = int(time.time() * 1000)
                if m.get("type") != "ticker": continue
                from datetime import datetime
                ts = int(datetime.fromisoformat(m["time"].replace("Z", "+00:00")).timestamp() * 1000)
                w(dict(src="cb", sym=m["product_id"], ts=ts, bid=float(m["best_bid"]), ask=float(m["best_ask"]), px=float(m["price"]), recv=r))
        except Exception:
            time.sleep(1)
for f in (rtds, coinbase): threading.Thread(target=f, daemon=True).start()
time.sleep(mins * 60)
