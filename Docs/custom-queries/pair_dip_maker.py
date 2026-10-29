"""fish 2026-10-30: first leg = market buy after a hard drop stops (as pair_dip_turn.py); second leg = a resting LIMIT bid
(maker: no fee, per docs.polymarket.com fees: taker fee = shares x 0.07 x p x (1-p), makers pay 0) at the price that leaves
at least MARGIN per share after the first leg's taker fee: p2 = 1 - p1 - fee(p1) - MARGIN. Filled when the other side's ask
reaches p2. Compared with a market (taker) second leg needing the same MARGIN after both fees. Optionally the target relaxes
to RELAX after EARLY seconds. Equal shares; hold to settlement. Read-only on /opt/pm-pair/data.
Usage: python3 -I ../pair_dip_maker.py
"""
import importlib.util, io, contextlib, os
spec = importlib.util.spec_from_file_location("dt", os.path.join(os.path.dirname(os.path.abspath(__file__)), "pair_dip_turn.py"))
dt = importlib.util.module_from_spec(spec)
with contextlib.redirect_stdout(io.StringIO()): spec.loader.exec_module(dt)
FEE = 0.07; fee = lambda p: FEE * p * (1 - p)

def leg1(post, drop=0.06, up=0.03, look=20, w=60):
    hist = {"Up": [], "Down": []}; low = {"Up": None, "Down": None}
    for i, (t, ua, us, da, ds, ub, db) in enumerate(post):
        if t > w: return None
        for side, a, sz in (("Up", ua, us), ("Down", da, ds)):
            if a is None: continue
            h = hist[side]; h.append((t, a))
            while h and h[0][0] < t - look: h.pop(0)
            hi = max(x for _, x in h)
            if hi - a >= drop: low[side] = a if low[side] is None else min(low[side], a)
            if low[side] is not None and a >= low[side] + up and hi - low[side] >= drop and a < 0.5 and sz >= dt.MIN: return i, side, a, t
            if low[side] is not None and hi - low[side] < drop: low[side] = None
    return None

def run(post, win, maker, margin, early, relax):
    L = leg1(post)
    if not L: return None
    i, side, p1, t1 = L
    for t, ua, us, da, ds, ub, db in post[i + 1:]:
        oth = da if side == "Up" else ua
        if oth is None: continue
        m = margin if t - t1 <= early else relax
        if maker:
            p2 = round(1 - p1 - fee(p1) - m, 2)            # resting bid, whole cents
            if oth <= p2 + 1e-9: return "pair", 1 - p1 - p2 - fee(p1), p1 + p2
        elif 1 - p1 - oth - fee(p1) - fee(oth) >= m: return "pair", 1 - p1 - oth - fee(p1) - fee(oth), p1 + oth
    return ("held_won" if side == win else "held_lost"), ((1 - p1) if side == win else -p1) - fee(p1), p1

print(f"{len(dt.mk)} complete markets | leg 1: fell >= 6c in 20 s then +3c off the low, first 60 s, market order")
print(f"{'second leg':<10}{'target':>8}{'relax':>16} | {'n':>4} {'pair':>5} {'lost':>5} | {'avg pair':>8} {'net/sh':>7} {'per $1':>7}")
for maker in (False, True):
    for margin in (0.005, 0.02, 0.03, 0.04):
        for early, relax in ((9999, None), (30, 0.005), (60, 0.005)):
            c = {}; pnl = cost = 0.0; pp = []
            for s, (post, win) in dt.mk.items():
                r = run(post, win, maker, margin, early, relax if relax is not None else margin)
                if not r: continue
                c[r[0]] = c.get(r[0], 0) + 1; pnl += r[1]; cost += r[2]
                if r[0] == "pair": pp.append(r[1])
            n = sum(c.values()); rl = "-" if relax is None else f"after {early}s>{relax:g}"
            print(f"{('limit' if maker else 'market'):<10}{margin:>8.3f}{rl:>16} | {n:>4} {c.get('pair', 0):>5} {c.get('held_lost', 0):>5} | {(sum(pp) / len(pp) * 100 if pp else 0):>6.1f}c {pnl:>+7.2f} {pnl / cost:>+7.1%}")
