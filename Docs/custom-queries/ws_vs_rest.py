"""Read-only diagnostic: is the websocket order book the bot decides on the same as the exchange's REST book at the
moments that matter (last 90 s of each market)? Runs its own BookFeed per market (no interaction with the bot), and
every second in the last 90 s compares, for both outcome tokens: best ask (ws vs REST), ws book age, number of ask
levels and USDC depth below 0.97 on each side. Mismatches on the favourite are what cause "no match" rejections
(phantom ask in ws) and fills far below the quoted ask (asks missing from ws).
Usage (on the host, inside the repo):  python3 -m Docs.custom-queries... no — run as a script:
  cd /opt/rabbit-24-hour-get-money && nohup python3 Docs/custom-queries/ws_vs_rest.py btc 3 > /tmp/ws_vs_rest.log 2>&1 &
Args: asset (btc/eth/sol), number of markets to observe (default 3). Summary lines start with "SUMMARY".
"""
import json, sys, time, urllib.request, os
sys.path.insert(0, os.getcwd())
from btc5m_sim import feeds

asset = (sys.argv[1] if len(sys.argv) > 1 else "btc").lower(); n_markets = int(sys.argv[2]) if len(sys.argv) > 2 else 3
UA = {"User-Agent": "btc5m-wsdiag"}

def get(url, timeout=6):
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout))

def rest_book(tok):
    b = get(f"https://clob.polymarket.com/book?token_id={tok}")
    asks = sorted(((float(x["price"]), float(x["size"])) for x in b.get("asks", []) if float(x["size"]) > 0))
    return asks

def summarize(asks):
    best = asks[0][0] if asks else None
    depth = sum(p * s for p, s in asks if p <= 0.97)
    return best, len(asks), depth

tot = dict(cmp=0, same=0, ws_lower=0, ws_higher=0, ws_none=0, rest_none=0, fav_cmp=0, fav_same=0, fav_phantom=0, fav_missing=0)
for i in range(n_markets):
    now = int(time.time()); start = now - now % 300
    slug = f"{asset}-updown-5m-{start}"
    try:
        e = get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=10)[0]
        toks = json.loads(e["markets"][0]["clobTokenIds"]); names = json.loads(e["markets"][0]["outcomes"])
    except Exception as ex:
        print("gamma failed", type(ex).__name__, ex); time.sleep(10); continue
    bf = feeds.BookFeed(toks); bf.start()
    end = start + 300
    print(f"=== {slug} {names} observing last 90 s", flush=True)
    while time.time() < end - 90: time.sleep(1)
    while time.time() < end:
        t0 = time.time(); secs = end - t0; line = []
        for tok, nm in zip(toks, names):
            try: ra = rest_book(tok)
            except Exception as ex: ra = None
            wsb, age = bf.book_now(tok)
            wa = sorted(((float(x["price"]), float(x["size"])) for x in wsb["asks"]), key=lambda x: x[0]) if wsb else None
            rb, rl, rd = summarize(ra) if ra is not None else (None, 0, 0)
            wb, wl, wd = summarize(wa) if wa is not None else (None, 0, 0)
            tot["cmp"] += 1
            if rb is None and wb is None: pass
            elif wb is None: tot["ws_none"] += 1
            elif rb is None: tot["rest_none"] += 1
            elif abs(rb - wb) < 1e-9: tot["same"] += 1
            elif wb < rb: tot["ws_lower"] += 1           # ws shows an ask that REST does not have any more (phantom)
            else: tot["ws_higher"] += 1                  # REST has a cheaper ask than ws knows (missing)
            fav = (rb is not None and 0.5 <= rb <= 0.99) or (wb is not None and 0.5 <= wb <= 0.99)
            if fav and rb is not None and wb is not None:
                tot["fav_cmp"] += 1; tot["fav_same"] += abs(rb - wb) < 1e-9; tot["fav_phantom"] += wb < rb - 1e-9; tot["fav_missing"] += wb > rb + 1e-9
            line.append(f"{nm}: ws {wb} ({wl} lv, ${wd:.0f}, age {age*1000:.0f}ms) rest {rb} ({rl} lv, ${rd:.0f}) {'=' if (rb == wb) else '≠'}")
        print(f"{secs:5.1f}s | " + " | ".join(line), flush=True)
        time.sleep(max(0, 1 - (time.time() - t0)))
    bf.stop()
    print(f"SUMMARY after {i+1} markets: " + " ".join(f"{k}={v}" for k, v in tot.items()), flush=True)
    time.sleep(2)
print("DONE", flush=True)
