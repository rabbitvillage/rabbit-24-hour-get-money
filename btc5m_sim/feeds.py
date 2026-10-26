"""Real-time feeds (2026-10-26, fish: "幣價與 Polymarket 盤口改成 websocket 即時推送，縮短延遲").

Why: on 601 real fills the losing ones had already moved $14.5 against us within 5 s of the buy — with REST polling
(spot ticker every 1 s, two /book calls of ~0.35 s each) we were 1-2 s behind whoever was selling to us.

SpotFeed  — Coinbase Exchange websocket, "ticker" channel: mid of best bid/ask, updated on every tick.
BookFeed  — Polymarket CLOB websocket (/ws/market): a full "book" snapshot per asset on subscribe, then
            "price_change" deltas (each change = the new total size at that price level for that side).
Both keep the latest state in memory with a local timestamp; callers ask for (data, age_seconds) and fall back to REST
when the age exceeds their staleness limit. The websocket library is imported inside the threads so tests (and hosts
without it) can import this module. Reconnects with backoff; never raises into the caller."""
import json, threading, time

COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"
POLY_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def _log(*a):
    print(time.strftime("%m-%d %H:%M:%S"), *a, flush=True)


class SpotFeed(threading.Thread):
    def __init__(self, product="BTC-USD", url=COINBASE_WS):
        super().__init__(daemon=True); self.product, self.url = product, url
        self.lock = threading.Lock(); self.price = None; self.ts = 0.0; self.msgs = 0; self.connected = False; self._stop = False

    def on_ticker(self, d, now=None):
        """Parse one ticker message; returns the mid or None."""
        if d.get("type") != "ticker" or d.get("product_id") != self.product: return None
        try: mid = (float(d["best_bid"]) + float(d["best_ask"])) / 2
        except (KeyError, TypeError, ValueError):
            try: mid = float(d["price"])
            except (KeyError, TypeError, ValueError): return None
        with self.lock:
            self.price = mid; self.ts = now or time.time(); self.msgs += 1
        return mid

    def get(self, now=None):
        """(price, age_seconds) or (None, inf)."""
        with self.lock:
            return (self.price, (now or time.time()) - self.ts) if self.price is not None else (None, float("inf"))

    def run(self):
        import websocket
        backoff = 1.0
        while not self._stop:
            try:
                ws = websocket.WebSocketApp(self.url,
                    on_open=lambda w: (w.send(json.dumps({"type": "subscribe", "product_ids": [self.product], "channels": ["ticker"]})), setattr(self, "connected", True)),
                    on_message=lambda w, m: self.on_ticker(json.loads(m)),
                    on_error=lambda w, e: _log(f"spot ws error {type(e).__name__}"),
                    on_close=lambda w, c, r: setattr(self, "connected", False))
                self.ws = ws; ws.run_forever(ping_interval=20, ping_timeout=10); backoff = 1.0
            except Exception as e:
                _log(f"spot ws crashed {type(e).__name__}")
            if self._stop: break
            time.sleep(backoff); backoff = min(backoff * 2, 30)

    def stop(self):
        self._stop = True
        try: self.ws.close()
        except Exception: pass


class BookFeed(threading.Thread):
    """One connection per market (the two outcome tokens). Call .stop() when the market is over."""
    def __init__(self, asset_ids, url=POLY_WS):
        super().__init__(daemon=True); self.assets = [str(a) for a in asset_ids]; self.url = url
        self.lock = threading.Lock(); self.books = {a: {"bids": {}, "asks": {}, "ts": 0.0} for a in self.assets}
        self.msgs = 0; self.connected = False; self._stop = False

    # ---- pure state updates (unit-tested) ----
    def apply_snapshot(self, d, now=None):
        a = str(d.get("asset_id"))
        if a not in self.books: return
        with self.lock:
            self.books[a] = {"bids": {str(x["price"]): float(x["size"]) for x in d.get("bids", []) if float(x["size"]) > 0},
                             "asks": {str(x["price"]): float(x["size"]) for x in d.get("asks", []) if float(x["size"]) > 0}, "ts": now or time.time()}

    def apply_change(self, d, now=None):
        changes = d.get("price_changes") or d.get("changes") or []
        top_asset = str(d.get("asset_id")) if d.get("asset_id") else None
        with self.lock:
            for c in changes:
                a = str(c.get("asset_id") or top_asset)
                b = self.books.get(a)
                if b is None: continue
                side = "bids" if str(c.get("side", "")).upper() == "BUY" else "asks"
                try: size = float(c["size"]); price = str(c["price"])
                except (KeyError, TypeError, ValueError): continue
                if size <= 0: b[side].pop(price, None)
                else: b[side][price] = size
                b["ts"] = now or time.time()

    def on_message(self, raw, now=None):
        try: d = json.loads(raw)
        except Exception: return
        for it in (d if isinstance(d, list) else [d]):
            et = it.get("event_type") if isinstance(it, dict) else None
            if et == "book": self.apply_snapshot(it, now)
            elif et == "price_change": self.apply_change(it, now)
            self.msgs += 1

    def book_now(self, asset_id, now=None):
        """({'asks': [{'price','size'}, ...] sorted, 'bids': [...]}, age_seconds)"""
        a = str(asset_id)
        with self.lock:
            b = self.books.get(a)
            if not b or not b["ts"]: return None, float("inf")
            asks = sorted(({"price": p, "size": s} for p, s in b["asks"].items()), key=lambda x: float(x["price"]))
            bids = sorted(({"price": p, "size": s} for p, s in b["bids"].items()), key=lambda x: -float(x["price"]))
            return {"asks": asks, "bids": bids}, (now or time.time()) - b["ts"]

    # ---- network ----
    def run(self):
        import websocket
        backoff = 1.0
        def pinger(ws):
            while not self._stop and getattr(ws, "sock", None) and ws.sock and ws.sock.connected:
                try: ws.send("PING")
                except Exception: break
                time.sleep(10)
        while not self._stop:
            try:
                ws = websocket.WebSocketApp(self.url,
                    on_open=lambda w: (w.send(json.dumps({"assets_ids": self.assets, "type": "market"})), setattr(self, "connected", True),
                                       threading.Thread(target=pinger, args=(w,), daemon=True).start()),
                    on_message=lambda w, m: self.on_message(m),
                    on_error=lambda w, e: _log(f"book ws error {type(e).__name__}"),
                    on_close=lambda w, c, r: setattr(self, "connected", False))
                self.ws = ws; ws.run_forever(ping_interval=0); backoff = 1.0
            except Exception as e:
                _log(f"book ws crashed {type(e).__name__}")
            if self._stop: break
            time.sleep(backoff); backoff = min(backoff * 2, 15)

    def stop(self):
        self._stop = True
        try: self.ws.close()
        except Exception: pass


if __name__ == "__main__":
    # Verification: python3 -m btc5m_sim.feeds [asset] [seconds] — compares the ws-maintained book with REST /book every second.
    import sys, urllib.request
    asset = (sys.argv[1] if len(sys.argv) > 1 else "btc").lower(); secs = int(sys.argv[2]) if len(sys.argv) > 2 else 40
    cb = {"btc": "BTC-USD", "eth": "ETH-USD", "sol": "SOL-USD", "xrp": "XRP-USD"}[asset]
    now = int(time.time()); start = now - now % 300
    e = json.load(urllib.request.urlopen(urllib.request.Request(f"https://gamma-api.polymarket.com/events?slug={asset}-updown-5m-{start}", headers={"User-Agent": "x"}), timeout=10))[0]
    toks = json.loads(e["markets"][0]["clobTokenIds"]); names = json.loads(e["markets"][0]["outcomes"])
    sf = SpotFeed(cb); sf.start(); bf = BookFeed(toks); bf.start(); time.sleep(2)
    same = diff = 0
    for i in range(secs):
        t0 = time.time(); line = []
        for tok, nm in zip(toks, names):
            try:
                rest = json.load(urllib.request.urlopen(urllib.request.Request(f"https://clob.polymarket.com/book?token_id={tok}", headers={"User-Agent": "x"}), timeout=5))
                ra = min((float(x["price"]) for x in rest.get("asks", [])), default=None)
            except Exception: ra = None
            wsb, age = bf.book_now(tok)
            wa = float(wsb["asks"][0]["price"]) if wsb and wsb["asks"] else None
            ok = (ra is not None and wa is not None and abs(ra - wa) < 1e-9); same += ok; diff += (not ok)
            line.append(f"{nm} ws {wa} ({age*1000:.0f}ms) rest {ra} {'=' if ok else '≠'}")
        sp, sage = sf.get(); rest_sp = None
        try:
            d = json.load(urllib.request.urlopen(urllib.request.Request(f"https://api.exchange.coinbase.com/products/{cb}/ticker", headers={"User-Agent": "x"}), timeout=5)); rest_sp = (float(d["bid"]) + float(d["ask"])) / 2
        except Exception: pass
        print(f"{time.strftime('%H:%M:%S')} spot ws {sp} ({sage*1000:.0f}ms) rest {rest_sp} | " + " | ".join(line), flush=True)
        time.sleep(max(0, 1 - (time.time() - t0)))
    print(f"最佳賣價 ws 與 REST 相同 {same} 次、不同 {diff} 次；ws 訊息數 spot {sf.msgs} book {bf.msgs}")
    sf.stop(); bf.stop()
