"""fish 2026-10-30 question: in the games where the second leg never filled, did the other side really never get cheap?
And would waiting for a deeper first-leg price (30c / 20c) change those games?
Part 1: every unfilled game of pair_dip_maker (limit second leg, target 3c): first leg, target, cheapest other-side ask
        before / after the first leg, winner.
Part 2: hindsight: cheapest buy-one-side-then-the-other sum per game (time order respected).
Part 3: first leg = market buy the first time either ask <= LEVEL inside the first W seconds; second leg = limit at
        1 - p1 - fee(p1) - 0.03; hold to settlement. Read-only on /opt/pm-pair/data.  Usage: python3 -I ../pair_unfilled_check.py
"""
import importlib.util, io, contextlib, os
here = os.path.dirname(os.path.abspath(__file__))
def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(here, name + ".py")); m = importlib.util.module_from_spec(spec)
    with contextlib.redirect_stdout(io.StringIO()): spec.loader.exec_module(m)
    return m
dm = load("pair_dip_maker"); dt = dm.dt; fee = dm.fee; mk = dt.mk
M = 0.03
other = lambda side, ua, da: da if side == "Up" else ua

print(f"{len(mk)} complete markets\n== Part 1: unfilled games (first leg = drop-then-turn, second leg = limit, target {M*100:.0f}c) ==")
unf = 0
for s, (post, win) in mk.items():
    r = dm.run(post, win, True, M, 9999, M)
    if not r or r[0] == "pair": continue
    unf += 1
    i, side, p1, t1 = dm.leg1(post); p2 = round(1 - p1 - fee(p1) - M, 2)
    after = [(other(side, ua, da), t) for t, ua, us, da, ds, ub, db in post[i + 1:] if other(side, ua, da) is not None]
    before = [(other(side, ua, da), t) for t, ua, us, da, ds, ub, db in post[:i + 1] if other(side, ua, da) is not None]
    own_after = min((ua if side == "Up" else da, t) for t, ua, us, da, ds, ub, db in post[i + 1:] if (ua if side == "Up" else da) is not None)
    ma, mb = min(after), min(before)
    print(f"{s:<26} bought {side:<4} {p1:.2f} at {t1:>3.0f}s | needed other <= {p2:.2f} | other cheapest after: {ma[0]:.2f} at {ma[1]:>3.0f}s,"
          f" before: {mb[0]:.2f} | own side later fell to {own_after[0]:.2f} at {own_after[1]:>3.0f}s | winner {win} ({r[0]})")
print(f"{unf} unfilled")

print("\n== Part 2: hindsight, cheapest 'buy one side, later buy the other' per game (asks, before fees) ==")
best = []
for s, (post, win) in mk.items():
    b = 9.0; mu = md = 9.0
    for t, ua, us, da, ds, ub, db in post:          # running min of each side, then pair with the other side now
        if ua is not None and md < 9: b = min(b, md + ua)
        if da is not None and mu < 9: b = min(b, mu + da)
        if ua is not None: mu = min(mu, ua)
        if da is not None: md = min(md, da)
    best.append(b)
for lim in (1.00, 0.99, 0.97, 0.95, 0.90, 0.80):
    print(f"  sum < {lim:.2f}: {sum(x < lim for x in best):>4} / {len(best)}")

print("\n== Part 3: first leg = market buy when either side first reaches LEVEL inside W s; second leg = limit, target 3c ==")
print(f"{'level':>6}{'window':>8} | {'games':>5} {'traded':>6} {'pair':>5} {'lost':>5} | {'net/sh':>7} {'per $1':>7}")
for level in (0.40, 0.35, 0.30, 0.25, 0.20, 0.15):
    for w in (60, 120, 200, 280):
        c = {}; pnl = cost = 0.0
        for s, (post, win) in mk.items():
            leg = None
            for i, (t, ua, us, da, ds, ub, db) in enumerate(post):
                if t > w: break
                for side, a, sz in (("Up", ua, us), ("Down", da, ds)):
                    if a is not None and a <= level and sz >= dt.MIN: leg = (i, side, a, t); break
                if leg: break
            if not leg: continue
            i, side, p1, t1 = leg; p2 = round(1 - p1 - fee(p1) - M, 2); res = None
            for t, ua, us, da, ds, ub, db in post[i + 1:]:
                o = other(side, ua, da)
                if o is not None and o <= p2 + 1e-9: res = ("pair", 1 - p1 - p2 - fee(p1), p1 + p2); break
            if not res: res = (("held_won" if side == win else "held_lost"), ((1 - p1) if side == win else -p1) - fee(p1), p1)
            c[res[0]] = c.get(res[0], 0) + 1; pnl += res[1]; cost += res[2]
        n = sum(c.values())
        print(f"{level:>6.2f}{w:>7}s | {len(mk):>5} {n:>6} {c.get('pair',0):>5} {c.get('held_lost',0):>5} | {pnl:>+7.2f} {(pnl/cost if cost else 0):>+7.1%}")

print("\n== Part 4: fish's 'choppy first' rule: enter only after Up's ask has crossed 50c at least K times since the open,"
      " then market buy the first side <= LEVEL (inside W s); second leg = limit, target 3c ==")
print(f"{'K':>3}{'level':>6}{'window':>8} | {'traded':>6} {'pair':>5} {'lost':>5} | {'net/sh':>7} {'per $1':>7}")
for K in (1, 2, 3, 4):
    for level in (0.40, 0.35, 0.30, 0.25):
        for w in (60, 120, 200):
            c = {}; pnl = cost = 0.0
            for s, (post, win) in mk.items():
                leg = None; cross = 0; prev = None
                for i, (t, ua, us, da, ds, ub, db) in enumerate(post):
                    if t > w: break
                    if ua is not None:
                        sgn = ua > 0.50
                        if prev is not None and sgn != prev: cross += 1
                        prev = sgn
                    if cross < K: continue
                    for side, a, sz in (("Up", ua, us), ("Down", da, ds)):
                        if a is not None and a <= level and sz >= dt.MIN: leg = (i, side, a, t); break
                    if leg: break
                if not leg: continue
                i, side, p1, t1 = leg; p2 = round(1 - p1 - fee(p1) - M, 2); res = None
                for t, ua, us, da, ds, ub, db in post[i + 1:]:
                    o = other(side, ua, da)
                    if o is not None and o <= p2 + 1e-9: res = ("pair", 1 - p1 - p2 - fee(p1), p1 + p2); break
                if not res: res = (("held_won" if side == win else "held_lost"), ((1 - p1) if side == win else -p1) - fee(p1), p1)
                c[res[0]] = c.get(res[0], 0) + 1; pnl += res[1]; cost += res[2]
            n = sum(c.values())
            print(f"{K:>3}{level:>6.2f}{w:>7}s | {n:>6} {c.get('pair',0):>5} {c.get('held_lost',0):>5} | {pnl:>+7.2f} {(pnl/cost if cost else 0):>+7.1%}")
