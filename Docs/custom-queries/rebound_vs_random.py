"""fish 2026-10-30: 「核心原理是波動，出發點是 50」. Test it: after a side's ask first dips to D (from the 0.50 start), how often does
that side's BID later climb back to D + R (= the other side becoming cheap enough to pair) before the market ends?
Benchmark: a fair (martingale) price that starts at D hits D + R before 0 with probability D / (D + R). Empirical above the
benchmark = the swing mean-reverts (the strategy has an edge); below = it trends (the strategy bleeds). Split by when the dip
happens (seconds after the open). Read-only, /opt/pm-pair/data.  Usage: python3 -I rebound_vs_random.py
"""
import csv, glob
from collections import defaultdict
mk = defaultdict(list)
for asset in ("btc", "eth", "sol"):
    for p in sorted(glob.glob(f"book_{asset}_*.csv")):
        for r in csv.DictReader(open(p)):
            g = lambda k: float(r[k]) if r[k] else None
            mk[r["slug"]].append((float(r["s_from_open"]), g("up_ask"), g("dn_ask"), g("up_bid"), g("dn_bid")))
full = {s: [x for x in sorted(v) if x[0] >= 0] for s, v in mk.items() if min(x[0] for x in v) <= -5 and max(x[0] for x in v) >= 280}
print(f"{len(full)} complete markets; each side of each market is one sample (first dip to D within the window)")
print(f"{'dip to':>7} {'rebound':>8} {'window':>9} | {'n':>4} {'rebounded':>9} {'actual':>7} {'random':>7} {'diff':>6}")
for D in (0.45, 0.40, 0.35, 0.30):
    for R in (0.04, 0.06, 0.10):
        for lo, hi in ((0, 60), (0, 20), (20, 60), (60, 180)):
            n = ok = 0
            for rows in full.values():
                for ai, bi in ((1, 3), (2, 4)):          # Up: ask idx 1, bid idx 3; Down: ask 2, bid 4
                    k = next((i for i, x in enumerate(rows) if lo <= x[0] <= hi and x[ai] is not None and x[ai] <= D), None)
                    if k is None: continue
                    n += 1
                    if any(x[bi] is not None and x[bi] >= D + R for x in rows[k + 1:]): ok += 1
            if n:
                a = ok / n; b = D / (D + R)
                print(f"{D:>7.2f} {('+' + format(R, '.2f')):>8} {f'{lo}-{hi}s':>9} | {n:>4} {ok:>9} {a:>7.1%} {b:>7.1%} {(a - b) * 100:>+5.1f}")
