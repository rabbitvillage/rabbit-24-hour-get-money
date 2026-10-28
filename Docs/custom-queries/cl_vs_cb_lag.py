"""Live Chainlink (the settlement source) vs Coinbase: delivery delay, which one leads and by how much, the price gap,
and whether the per-second Chainlink stream averages exactly to the official finalPrice. Read-only.

Input: a JSONL file written by record_cl_cb.mjs (same folder). Each line:
  {"src":"cl","sym":"btc/usd","ts":<price time ms>,"v":<price>,"srv":<Polymarket send ms>,"recv":<our receive ms>}
  {"src":"cb","sym":"BTC-USD","ts":<Coinbase trade time ms>,"bid":..,"ask":..,"recv":<our receive ms>}
Usage: python3 -I cl_vs_cb_lag.py feeds.jsonl [btc|sol]
"""
import bisect, json, statistics as st, sys, urllib.request

path = sys.argv[1]; asset = (sys.argv[2] if len(sys.argv) > 2 else "btc").lower()
CL, CB = f"{asset}/usd", f"{asset.upper()}-USD"


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))] if xs else float("nan")


cl, cb = {}, []
for line in open(path):
    o = json.loads(line)
    if o["src"] == "cl" and o["sym"] == CL:
        cur = cl.get(o["ts"])
        if cur is None or (cur.get("snap") and not o.get("snap")):
            cl[o["ts"]] = o                         # keep the live copy over the snapshot copy
    elif o["src"] == "cb" and o["sym"] == CB and o.get("bid") and o.get("ask"):
        cb.append((o["ts"], (o["bid"] + o["ask"]) / 2, o["recv"]))
cb.sort(); cbt = [t for t, _, _ in cb]
cls = sorted(cl.values(), key=lambda o: o["ts"])
live = [o for o in cls if not o.get("snap")]
print(f"Chainlink {CL}: {len(cls)} seconds ({len(live)} live), Coinbase {CB}: {len(cb)} ticks, "
      f"span {(cls[-1]['ts'] - cls[0]['ts']) / 60000:.1f} min")


def cb_at(t):
    i = bisect.bisect_right(cbt, t) - 1
    return cb[i][1] if i >= 0 else None


# 1. delivery delay
d_cl = [(o["recv"] - o["ts"]) / 1000 for o in live]
d_srv = [(o["srv"] - o["ts"]) / 1000 for o in live if o.get("srv")]
d_cb = [(r - t) / 1000 for t, _, r in cb]
print("\n1) How late each price reaches us (seconds after the price's own timestamp)")
print(f"   Chainlink via Polymarket: p10 {pct(d_cl, .1):.2f}  p50 {pct(d_cl, .5):.2f}  p90 {pct(d_cl, .9):.2f}"
      f"   (Polymarket sends it at p50 {pct(d_srv, .5):.2f})")
print(f"   Coinbase ticker:          p10 {pct(d_cb, .1):.2f}  p50 {pct(d_cb, .5):.2f}  p90 {pct(d_cb, .9):.2f}")

# 2. level gap
gap = [o["v"] - cb_at(o["ts"]) for o in cls if cb_at(o["ts"]) is not None]
ref = st.median(o["v"] for o in cls)
print("\n2) Chainlink - Coinbase at the same timestamp")
print(f"   p10 {pct(gap, .1):+.2f}  p50 {pct(gap, .5):+.2f}  p90 {pct(gap, .9):+.2f}  USD  (= {pct(gap, .5) / ref * 1e4:+.2f} bps median)")

# 3. lead/lag: Chainlink at t vs Coinbase at t - d. d > 0 means Coinbase moves first.
print("\n3) Which one moves first: correlation of 1-second changes, Chainlink(t) vs Coinbase(t - d)")
best = None
for d in range(-3000, 5001, 250):
    xs, ys = [], []
    for a, b in zip(cls, cls[1:]):
        if b["ts"] - a["ts"] != 1000:
            continue
        c1, c0 = cb_at(b["ts"] - d), cb_at(a["ts"] - d)
        if c1 is None or c0 is None:
            continue
        xs.append(b["v"] - a["v"]); ys.append(c1 - c0)
    if len(xs) < 30:
        continue
    r = st.correlation(xs, ys)
    resid = st.pstdev([o["v"] - cb_at(o["ts"] - d) for o in cls if cb_at(o["ts"] - d) is not None])
    if best is None or r > best[1]:
        best = (d, r, resid)
    print(f"   d={d / 1000:+5.2f}s  corr {r:+.3f}  level wobble sd ${resid:.2f}")
print(f"   -> best match: Chainlink at t looks like Coinbase at t {'-' if best[0] >= 0 else '+'} {abs(best[0]) / 1000:.2f}s (corr {best[1]:.3f})")
lead = best[0] / 1000 + pct(d_cl, .5) - pct(d_cb, .5)
print(f"   -> head start we get from Coinbase: about {lead:.2f}s before Polymarket shows us the same Chainlink price")

# 4. official TWAP check
print("\n4) Is finalPrice the plain average of the per-second Chainlink prices? (markets fully inside the recording)")
ts_set = {o["ts"]: o["v"] for o in cls}
first, last = cls[0]["ts"] // 1000, cls[-1]["ts"] // 1000
for end in range((first // 300 + 1) * 300, last + 1, 300):
    if end - 61 < first:
        continue
    start = end - 300
    try:
        ev = json.load(urllib.request.urlopen(urllib.request.Request(
            f"https://gamma-api.polymarket.com/events?slug={asset}-updown-5m-{start}", headers={"User-Agent": "x"}), timeout=10))
        meta = ev[0].get("eventMetadata") or {}
    except Exception:
        meta = {}
    fin = meta.get("finalPrice")
    variants = {}
    for name, lo, hi in (("(end-60, end]", end - 59, end), ("[end-60, end)", end - 60, end - 1), ("[end-59, end-1]", end - 59, end - 1),
                         ("[end-60, end]", end - 60, end)):
        vals = [ts_set.get(t * 1000) for t in range(lo, hi + 1)]
        if None not in vals:
            variants[name] = sum(vals) / len(vals)
    cbw = [cb_at(t * 1000) for t in range(end - 59, end + 1)]
    cbavg = sum(cbw) / len(cbw) if None not in cbw else None
    if fin is None:
        print(f"   {start}: finalPrice not published yet")
        continue
    parts = "  ".join(f"{k} {v - fin:+.3f}" for k, v in variants.items())
    print(f"   {asset}-updown-5m-{start}: finalPrice {fin:.3f}   Chainlink average minus finalPrice: {parts}"
          + (f"   Coinbase avg minus finalPrice {cbavg - fin:+.2f}" if cbavg else ""))
