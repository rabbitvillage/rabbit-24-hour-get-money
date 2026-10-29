"""fish's manual pattern (2026-10-30): wait for one side to fall hard, buy only once the fall stops (first uptick off the low),
then lock the pair as soon as the other side is cheap enough for any profit after fees; equal shares; hold to settlement.
Leg 1: within WIN s of the open, a side's ask fell >= DROP below its high of the last LOOK s and has since ticked back up >= UP
from its low -> market buy at the current ask. Leg 2: the other side once 1 - p1 - p2 - fee(p1) - fee(p2) > MARGIN.
Read-only replay on /opt/pm-pair/data.  Usage: python3 -I ../pair_dip_turn.py
"""
import csv, glob, itertools
from collections import defaultdict
FEE = 0.07; fee = lambda p: FEE * p * (1 - p); MIN = 30
mk = {}
for asset in ("btc", "eth", "sol"):
    tmp = defaultdict(list)
    for p in sorted(glob.glob(f"book_{asset}_*.csv")):
        for r in csv.DictReader(open(p)):
            g = lambda k: float(r[k]) if r[k] else None
            tmp[r["slug"]].append((float(r["s_from_open"]), g("up_ask"), g("up_ask_sz") or 0, g("dn_ask"), g("dn_ask_sz") or 0, g("up_bid"), g("dn_bid")))
    for s, rows in tmp.items():
        rows.sort()
        if rows[0][0] > -5 or rows[-1][0] < 280: continue
        win = "Up" if (rows[-1][5] or 0) >= 0.9 else "Down" if (rows[-1][6] or 0) >= 0.9 else None
        if win: mk[s] = ([r for r in rows if r[0] >= 0], win)

def run(post, win, drop, up, look, w, margin):
    hist = {"Up": [], "Down": []}; low = {"Up": None, "Down": None}; leg = None
    for i, (t, ua, us, da, ds, ub, db) in enumerate(post):
        if t > w: break
        for side, a, sz in (("Up", ua, us), ("Down", da, ds)):
            if a is None: continue
            h = hist[side]; h.append((t, a))
            while h and h[0][0] < t - look: h.pop(0)
            hi = max(x for _, x in h)
            if hi - a >= drop: low[side] = a if low[side] is None else min(low[side], a)
            if low[side] is not None and a >= low[side] + up and hi - low[side] >= drop and a < 0.5 and sz >= MIN:
                leg = (i, side, a); break
            if low[side] is not None and hi - low[side] < drop: low[side] = None
        if leg: break
    if not leg: return None
    i, side, p1 = leg
    for t, ua, us, da, ds, ub, db in post[i + 1:]:
        oth = da if side == "Up" else ua
        if oth is not None and 1 - p1 - oth - fee(p1) - fee(oth) > margin: return "pair", 1 - p1 - oth - fee(p1) - fee(oth), p1 + oth
    return ("held_won" if side == win else "held_lost"), ((1 - p1) if side == win else -p1) - fee(p1), p1

print(f"{len(mk)} complete markets")
print(f"{'drop':>5}{'turn':>6}{'look':>6}{'win':>6}{'margin':>7} | {'n':>4} {'pair':>5} {'lost':>5} | {'net/sh':>7} {'per $1':>7}")
res = []
for drop, up, look, w, margin in itertools.product((0.06, 0.10, 0.15), (0.01, 0.02, 0.03), (10, 20), (60, 120, 290), (0.005, 0.02)):
    c = defaultdict(int); pnl = cost = 0.0
    for s, (post, win) in mk.items():
        r = run(post, win, drop, up, look, w, margin)
        if not r: continue
        c[r[0]] += 1; pnl += r[1]; cost += r[2]
    n = sum(c.values())
    if n: res.append((pnl / cost, drop, up, look, w, margin, n, c, pnl))
res.sort(reverse=True)
for row in res[:15] + [None] + res[-5:]:
    if row is None: print("   ..."); continue
    r, drop, up, look, w, margin, n, c, pnl = row
    print(f"{drop:>5.2f}{up:>6.2f}{look:>5}s{w:>5}s{margin:>7.3f} | {n:>4} {c['pair']:>5} {c['held_lost']:>5} | {pnl:>+7.2f} {r:>+7.1%}")
pos = sum(1 for x in res if x[0] > 0); print(f"{pos}/{len(res)} parameter sets positive")
