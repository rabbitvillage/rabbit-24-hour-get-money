"""Opening-phase order-book recorder for the Polymarket 5-minute up/down markets. Read-only: never places an order.

fish 2026-10-29: 「盤口剛開的時候絕對是 50／50 開頭，因為會強制重設」— the plan is to buy both sides at two different moments
while the price swings around 0.50 after the open, so that the pair costs < $1. The old bot only kept the last 150 s of each
market, so the opening phase was never recorded. This records, for every market of every asset in ASSETS, from PRE seconds
before the open until the close:
  book_<asset>_<YYYYMMDD>.csv    one row whenever either side's top of book changes (at most every SAMPLE s, at least every
                                 HEARTBEAT s): best bid/ask, size at the best, size within 2c of the best, Coinbase mid,
                                 Chainlink price
  trades_<asset>_<YYYYMMDD>.csv  every trade print (last_trade_price events): side, price, size
  markets.csv                    one row per market seen (slug, start, token ids)
Markets are listed ~24 h ahead and quoted 0.50/0.51 until the open (checked 2026-10-29), so PRE=180 s is plenty.
One websocket per market (opened PRE s before the open, closed after the close), one Coinbase and one Chainlink feed per asset.
Usage: python3 -m pair_sim.recorder        env: ASSETS (btc,eth,sol)  DATA_DIR (data)  PRE (180)  SAMPLE (0.25)  HEARTBEAT (2)
"""
import csv, gzip, json, os, shutil, threading, time, urllib.request
from datetime import datetime, timezone

from . import feeds

ASSETS = [a.strip().lower() for a in os.getenv("ASSETS", "btc,eth,sol").split(",") if a.strip()]
DATA = os.getenv("DATA_DIR", "data")
PRE = float(os.getenv("PRE", "180"))
SAMPLE = float(os.getenv("SAMPLE", "0.25"))
HEARTBEAT = float(os.getenv("HEARTBEAT", "2"))
PERIOD = 300
CB = {"btc": "BTC-USD", "eth": "ETH-USD", "sol": "SOL-USD", "xrp": "XRP-USD"}
GAMMA = "https://gamma-api.polymarket.com/events?slug={}"
BFIELDS = ["t_ms", "slug", "s_from_open", "up_bid", "up_bid_sz", "up_ask", "up_ask_sz", "up_ask_sz2", "dn_bid", "dn_bid_sz",
           "dn_ask", "dn_ask_sz", "dn_ask_sz2", "cb_mid", "cl_price", "cl_sec"]
TFIELDS = ["t_ms", "slug", "s_from_open", "outcome", "side", "price", "size"]
MFIELDS = ["slug", "start_utc", "start", "up_token", "down_token"]


def log(*a):
    print(time.strftime("%m-%d %H:%M:%S"), *a, flush=True)


def starts_to_watch(now, pre=PRE):
    """Market starts whose window [start - pre, start + PERIOD] contains now."""
    cur = int(now // PERIOD) * PERIOD
    return [s for s in (cur - PERIOD, cur, cur + PERIOD) if s - pre <= now < s + PERIOD]


def top(book):
    """(best bid, size there, best ask, size there, size of asks within 2c of the best ask) from BookFeed.book_now()."""
    if not book:
        return None, 0.0, None, 0.0, 0.0
    bids = [(float(x["price"]), float(x["size"])) for x in book.get("bids", [])]
    asks = [(float(x["price"]), float(x["size"])) for x in book.get("asks", [])]
    bb = max(bids, default=(None, 0.0)); ba = min(asks, default=(None, 0.0))
    sz2 = sum(s for p, s in asks if ba[0] is not None and p <= ba[0] + 0.02 + 1e-9)
    return bb[0], round(bb[1], 2), ba[0], round(ba[1], 2), round(sz2, 2)


class TradeBookFeed(feeds.BookFeed):
    """BookFeed that also hands every trade print (last_trade_price) to on_trade(asset_id, side, price, size, now)."""
    def __init__(self, asset_ids, on_trade):
        super().__init__(asset_ids); self.on_trade = on_trade

    def on_message(self, raw, now=None):
        super().on_message(raw, now)
        try: d = json.loads(raw)
        except Exception: return
        for it in (d if isinstance(d, list) else [d]):
            if isinstance(it, dict) and it.get("event_type") == "last_trade_price":
                try: self.on_trade(str(it.get("asset_id")), str(it.get("side", "")), float(it["price"]), float(it.get("size") or 0), now or time.time())
                except (KeyError, TypeError, ValueError): pass


class Writer:
    """Append-only daily CSV files; yesterday's files are gzipped once the day rolls over."""
    def __init__(self, root):
        self.root = root; self.lock = threading.Lock(); self.day = None; os.makedirs(root, exist_ok=True)

    def _path(self, name, day):
        return os.path.join(self.root, f"{name}_{day}.csv")

    def write(self, name, fields, row, day=None):
        day = day or datetime.now(timezone.utc).strftime("%Y%m%d")
        with self.lock:
            if self.day and day != self.day:
                self._compress(self.day)
            self.day = day
            p = self._path(name, day) if name != "markets" else os.path.join(self.root, "markets.csv")
            new = not os.path.exists(p)
            with open(p, "a", newline="") as f:
                w = csv.DictWriter(f, fields)
                if new: w.writeheader()
                w.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in fields})

    def _compress(self, day):
        for fn in os.listdir(self.root):
            if fn.endswith(f"_{day}.csv"):
                src = os.path.join(self.root, fn)
                with open(src, "rb") as a, gzip.open(src + ".gz", "wb") as b: shutil.copyfileobj(a, b)
                os.remove(src)


def market(asset, start):
    try:
        d = json.load(urllib.request.urlopen(urllib.request.Request(GAMMA.format(f"{asset}-updown-5m-{start}"), headers={"User-Agent": "pair-recorder"}), timeout=10))
        if not d: return None
        m = d[0]["markets"][0]; toks = json.loads(m["clobTokenIds"]); outs = json.loads(m["outcomes"])
        up = toks[outs.index("Up")]; dn = toks[outs.index("Down")]
        return dict(slug=d[0]["slug"], start=start, up=str(up), down=str(dn))
    except Exception as e:
        log(f"gamma {asset} {start} failed {type(e).__name__}"); return None


class Recorder:
    def __init__(self, assets=ASSETS, root=DATA):
        self.assets = assets; self.w = Writer(root); self.live = {}   # (asset, start) -> dict(m, feed, last, t_last)
        self.spot = {}; self.cl = {}

    def start_feeds(self):
        for a in self.assets:
            try: self.spot[a] = feeds.SpotFeed(CB[a]); self.spot[a].start()
            except Exception as e: log(f"spot {a} failed {type(e).__name__}")
            try: self.cl[a] = feeds.ChainlinkFeed(f"{a}/usd"); self.cl[a].start()
            except Exception as e: log(f"chainlink {a} failed {type(e).__name__}")

    def _on_trade(self, asset, start, m):
        def f(tok, side, price, size, now):
            out = "Up" if tok == m["up"] else "Down" if tok == m["down"] else None
            if out is None: return
            self.w.write(f"trades_{asset}", TFIELDS, dict(t_ms=int(now * 1000), slug=m["slug"], s_from_open=round(now - start, 3),
                                                          outcome=out, side=side, price=price, size=size))
        return f

    def manage(self, now):
        want = {(a, s) for a in self.assets for s in starts_to_watch(now)}
        for k in list(self.live):
            if k not in want:
                try: self.live[k]["feed"].stop()
                except Exception: pass
                del self.live[k]
        for a, s in sorted(want):
            if (a, s) in self.live: continue
            m = market(a, s)
            if not m: continue
            fd = TradeBookFeed([m["up"], m["down"]], self._on_trade(a, s, m)); fd.start()
            self.live[(a, s)] = dict(m=m, feed=fd, last=None, t_last=0.0)
            self.w.write("markets", MFIELDS, dict(slug=m["slug"], start_utc=datetime.fromtimestamp(s, timezone.utc).isoformat(), start=s,
                                                  up_token=m["up"], down_token=m["down"]))
            log(f"watching {m['slug']} (opens in {s - now:.0f}s)")

    def sample(self, now):
        for (a, s), st in list(self.live.items()):
            m, fd = st["m"], st["feed"]
            ub = fd.book_now(m["up"])[0]; db = fd.book_now(m["down"])[0]
            u = top(ub); d = top(db)
            key = (u, d)
            if key == st["last"] and now - st["t_last"] < HEARTBEAT: continue
            sp = self.spot.get(a).get()[0] if self.spot.get(a) else None
            cs, cp, _ = self.cl[a].latest() if self.cl.get(a) else (None, None, None)
            self.w.write(f"book_{a}", BFIELDS, dict(t_ms=int(now * 1000), slug=m["slug"], s_from_open=round(now - s, 3),
                          up_bid=u[0], up_bid_sz=u[1], up_ask=u[2], up_ask_sz=u[3], up_ask_sz2=u[4],
                          dn_bid=d[0], dn_bid_sz=d[1], dn_ask=d[2], dn_ask_sz=d[3], dn_ask_sz2=d[4], cb_mid=sp, cl_price=cp, cl_sec=cs))
            st["last"] = key; st["t_last"] = now

    def run(self):
        self.start_feeds(); log(f"pair recorder: assets {self.assets}, {PRE:g}s before the open to the close, sample {SAMPLE:g}s, data {self.w.root}")
        last_manage = 0.0
        while True:
            now = time.time()
            if now - last_manage >= 1.0:
                try: self.manage(now)
                except Exception as e: log(f"manage failed {type(e).__name__}")
                last_manage = now
            try: self.sample(now)
            except Exception as e: log(f"sample failed {type(e).__name__}")
            time.sleep(max(0.0, SAMPLE - (time.time() - now)))


if __name__ == "__main__":
    Recorder().run()
