"""Real-time feeds (2026-10-26, fish: "幣價與 Polymarket 盤口改成 websocket 即時推送，縮短延遲").

Why: on 601 real fills the losing ones had already moved $14.5 against us within 5 s of the buy — with REST polling
(spot ticker every 1 s, two /book calls of ~0.35 s each) we were 1-2 s behind whoever was selling to us.

SpotFeed  — Coinbase Exchange websocket, "ticker" channel: mid of best bid/ask, updated on every tick.
BookFeed  — Polymarket CLOB websocket (/ws/market): a full "book" snapshot per asset on subscribe, then
            "price_change" deltas (each change = the new total size at that price level for that side).
ChainlinkFeed (2026-10-28) — Polymarket's public real-time data socket, topic crypto_prices_chainlink: the Chainlink price
            the up/down markets settle on, one point per second (see the class docstring).
Both keep the latest state in memory with a local timestamp; callers ask for (data, age_seconds) and fall back to REST
when the age exceeds their staleness limit. The websocket library is imported inside the threads so tests (and hosts
without it) can import this module. Reconnects with backoff; never raises into the caller."""
import bisect, collections, json, threading, time

COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"
POLY_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
RTDS_WS = "wss://ws-live-data.polymarket.com"


def _log(*a):
    print(time.strftime("%m-%d %H:%M:%S"), *a, flush=True)


class SpotFeed(threading.Thread):
    def __init__(self, product="BTC-USD", url=COINBASE_WS):
        super().__init__(daemon=True); self.product, self.url = product, url
        self.lock = threading.Lock(); self.price = None; self.ts = 0.0; self.msgs = 0; self.connected = False; self._stop = False
        self.hist = collections.deque(maxlen=20000)   # (local time, mid) for the last several minutes: lets a caller line
                                                      # Coinbase up with another source by time (ChainlinkFeed basis)

    def on_ticker(self, d, now=None):
        """Parse one ticker message; returns the mid or None."""
        if d.get("type") != "ticker" or d.get("product_id") != self.product: return None
        try: mid = (float(d["best_bid"]) + float(d["best_ask"])) / 2
        except (KeyError, TypeError, ValueError):
            try: mid = float(d["price"])
            except (KeyError, TypeError, ValueError): return None
        with self.lock:
            self.price = mid; self.ts = now or time.time(); self.msgs += 1; self.hist.append((self.ts, mid))
        return mid

    def get(self, now=None):
        """(price, age_seconds) or (None, inf)."""
        with self.lock:
            return (self.price, (now or time.time()) - self.ts) if self.price is not None else (None, float("inf"))

    def history(self, since):
        """[(local_time, mid)] received at or after `since`, oldest first."""
        with self.lock:
            return [x for x in self.hist if x[0] >= since]

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
        self.changed = threading.Event()     # set on every book update; the sim loop waits on it instead of sleeping a full poll

    # ---- pure state updates (unit-tested) ----
    def apply_snapshot(self, d, now=None):
        a = str(d.get("asset_id"))
        if a not in self.books: return
        with self.lock:
            self.books[a] = {"bids": {str(x["price"]): float(x["size"]) for x in d.get("bids", []) if float(x["size"]) > 0},
                             "asks": {str(x["price"]): float(x["size"]) for x in d.get("asks", []) if float(x["size"]) > 0}, "ts": now or time.time()}
        self.changed.set()

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
        if changes: self.changed.set()

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


class ChainlinkFeed(threading.Thread):
    """Chainlink reference price from Polymarket's public real-time data socket (no login). Topic crypto_prices_chainlink,
    filter {"symbol": "btc/usd"}: one point per second, {"timestamp": price time in ms, "value": price}, reaching the host
    ~1.45 s after its timestamp; a subscribe first returns the preceding ~60 s, so a reconnect refills the gap.
    The 5m/15m up/down markets settle on Chainlink's 60-second TWAP of this price: priceToBeat = mean of the 60 one-second
    prices in [open-60, open), finalPrice = the same over the last 60 s (2026-10-28: per-second mean vs gamma finalPrice
    within $0.04-$1.4 on 7 BTC markets, mean |error| $0.4). One symbol per connection (two symbols on one socket only
    streamed the first). Keeps the last KEEP seconds keyed by whole price-second."""
    KEEP = 900

    def __init__(self, symbol="btc/usd", url=RTDS_WS):
        super().__init__(daemon=True); self.symbol, self.url = symbol.lower(), url
        self.lock = threading.Lock(); self.px = {}; self.recv = 0.0; self.msgs = 0; self.connected = False; self._stop = False

    # ---- pure state updates (unit-tested) ----
    def _put(self, ts_ms, value):
        try: sec = int(round(float(ts_ms) / 1000)); v = float(value)
        except (TypeError, ValueError): return
        self.px[sec] = v

    def on_message(self, raw, now=None):
        if not raw or raw[0] != "{": return            # pongs / empty keep-alives
        try: d = json.loads(raw)
        except Exception: return
        p = d.get("payload") or {}
        if str(p.get("symbol", self.symbol)).lower() != self.symbol: return
        with self.lock:
            if isinstance(p.get("data"), list):          # snapshot on subscribe
                for x in p["data"]: self._put(x.get("timestamp"), x.get("value"))
            elif "value" in p:
                self._put(p.get("timestamp"), p.get("value"))
            else:
                return
            self.recv = now or time.time(); self.msgs += 1
            if len(self.px) > self.KEEP + 60:
                cut = max(self.px) - self.KEEP
                for k in [k for k in self.px if k < cut]: del self.px[k]

    def points(self, lo, hi):
        """{second: price} for lo <= second < hi."""
        with self.lock:
            return {k: v for k, v in self.px.items() if lo <= k < hi}

    def latest(self, now=None):
        """(price_second, price, seconds_since_last_message) or (None, None, inf)."""
        with self.lock:
            if not self.px: return None, None, float("inf")
            k = max(self.px); return k, self.px[k], (now or time.time()) - self.recv

    def window_mean(self, lo, hi, max_missing=3):
        """Mean of the one-second prices for lo <= second < hi (the settlement TWAP when hi - lo = 60). A missing second
        takes the previous second's price (the stream skips a second now and then). None when more than `max_missing`
        seconds are missing or the first second has nothing before it to carry."""
        with self.lock:
            prev = None; total = 0.0; missing = 0
            if lo not in self.px:
                before = [k for k in self.px if lo - 5 <= k < lo]
                prev = self.px[max(before)] if before else None
            for s in range(lo, hi):
                v = self.px.get(s)
                if v is None:
                    missing += 1
                    if prev is None or missing > max_missing: return None
                    v = prev
                total += v; prev = v
            return total / (hi - lo)

    def subscribe_msg(self):
        # The filter must be compact JSON: with json.dumps' default ", "/": " spacing the server sends the snapshot and then
        # no updates at all (host test 2026-10-28: compact 16 updates in 15 s, spaced 0).
        return json.dumps({"action": "subscribe", "subscriptions": [{"topic": "crypto_prices_chainlink", "type": "*",
                           "filters": json.dumps({"symbol": self.symbol}, separators=(",", ":"))}]})

    # ---- network ----
    SILENT = 20   # seconds without a message while connected -> reconnect (a socket can stay open and send nothing)

    def run(self):
        import websocket
        backoff = 1.0
        sub = self.subscribe_msg()
        def pinger(ws):
            t0 = time.time()
            while not self._stop and getattr(ws, "sock", None) and ws.sock and ws.sock.connected:
                try: ws.send("PING")
                except Exception: break
                if time.time() - max(self.recv, t0) > self.SILENT:
                    _log(f"chainlink ws silent {self.SILENT}s, reconnecting")
                    try: ws.close()
                    except Exception: pass
                    break
                time.sleep(5)
        while not self._stop:
            try:
                ws = websocket.WebSocketApp(self.url,
                    on_open=lambda w: (w.send(sub), setattr(self, "connected", True), threading.Thread(target=pinger, args=(w,), daemon=True).start()),
                    on_message=lambda w, m: self.on_message(m),
                    on_error=lambda w, e: _log(f"chainlink ws error {type(e).__name__}"),
                    on_close=lambda w, c, r: setattr(self, "connected", False))
                self.ws = ws; ws.run_forever(ping_interval=0); backoff = 1.0
            except Exception as e:
                _log(f"chainlink ws crashed {type(e).__name__}")
            if self._stop: break
            time.sleep(backoff); backoff = min(backoff * 2, 15)

    def stop(self):
        self._stop = True
        try: self.ws.close()
        except Exception: pass


def basis(cl_points, cb_hist, lead=0.375, min_pairs=30):
    """Median of Chainlink price - Coinbase mid, pairing each Chainlink second s with the latest Coinbase mid received at or
    before s - lead (Chainlink's price for second s matches Coinbase ~0.25-0.5 s earlier, 2026-10-28 recordings).
    cl_points {second: price}; cb_hist [(local_time, mid)] oldest first. None when fewer than min_pairs pairs."""
    if not cl_points or not cb_hist: return None
    ts = [t for t, _ in cb_hist]; diffs = []
    for s, p in cl_points.items():
        i = bisect.bisect_right(ts, s - lead) - 1
        if i >= 0 and s - lead - ts[i] <= 5:            # a Coinbase quote no older than 5 s at that moment
            diffs.append(p - cb_hist[i][1])
    if len(diffs) < min_pairs: return None
    diffs.sort(); n = len(diffs)
    return diffs[n // 2] if n % 2 else (diffs[n // 2 - 1] + diffs[n // 2]) / 2


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
