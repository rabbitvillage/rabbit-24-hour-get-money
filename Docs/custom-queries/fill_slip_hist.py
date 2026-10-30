"""Read-only: how far the real fill landed from the ask the bot saw (live_timing.csv), in cents.
Negative = filled cheaper than quoted, +1 = one cent worse. Also the average book-to-exchange latency.
Usage:  python3 -I Docs/custom-queries/fill_slip_hist.py btc5m_sim/data [btc5m_sim/data-sol ...]
"""
import csv, sys, os, collections
h = collections.Counter(); byask = collections.defaultdict(collections.Counter); n = 0; ms = 0.0
for d in sys.argv[1:]:
    p = os.path.join(d, "live_timing.csv")
    if not os.path.exists(p): continue
    for r in csv.DictReader(open(p)):
        if r["status"] != "filled" or not r["fill_avg"]: continue
        diff = round((float(r["fill_avg"]) - float(r["ask"])) * 100)
        h[diff] += 1; byask["0.97-0.99" if float(r["ask"]) >= 0.97 else "<0.97"][diff] += 1
        n += 1; ms += float(r["total_ms"] or 0)
print("filled orders with timing:", n, "avg book->exchange ms", round(ms / n) if n else 0)
print("fill - quoted ask (cents): count", sorted(h.items()))
for k, c in byask.items(): print(k, sorted(c.items()))
