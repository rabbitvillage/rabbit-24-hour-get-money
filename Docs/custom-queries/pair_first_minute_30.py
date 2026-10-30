"""fish 2026-10-30: "in the first minute, when it swings hard and one side is at 30c or below, I can almost always
complete a pair under 100". Part 1: one game's first-minute path and every buy-one-then-the-other chance.
Part 2: all recorded games: first time a side's ask <= LEVEL inside the first W s -> buy it (market); does the other side's
ask later reach sum < 1.00 / net >= 0 after fees / net >= 3c (second leg limit, no fee)? Read-only on /opt/pm-pair/data.
Usage: python3 -I ../pair_first_minute_30.py [slug]
"""
import importlib.util, io, contextlib, os, sys
here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("dt", os.path.join(here, "pair_dip_turn.py")); dt = importlib.util.module_from_spec(spec)
with contextlib.redirect_stdout(io.StringIO()): spec.loader.exec_module(dt)
fee = dt.fee; mk = dt.mk

slug = sys.argv[1] if len(sys.argv) > 1 else None
if slug:
    if slug not in mk: print(slug, "not in complete markets (still open or not recorded)")
    else:
        post, win = mk[slug]
        print(f"== {slug}  winner {win} ==\n  s   Up ask  Down ask  sum")
        nxt = 0
        for t, ua, us, da, ds, ub, db in post:
            if t >= nxt and t <= 120:
                print(f"{t:>4.0f}  {ua if ua is not None else '-':>6}  {da if da is not None else '-':>8}  {(ua + da) if ua and da else '-'}"); nxt = t + 3
        # best completion for every first-leg moment in the first 60 s, both sides
        print("\nfirst-leg moments (first 60 s) where the other side later made sum < 1:")
        seen = set()
        for i, (t, ua, us, da, ds, ub, db) in enumerate(post):
            if t > 60: break
            for side, a in (("Up", ua), ("Down", da)):
                if a is None or a > 0.45 or (side, a) in seen: continue
                o = [((r[3] if side == "Up" else r[1]), r[0]) for r in post[i + 1:] if (r[3] if side == "Up" else r[1]) is not None]
                if not o: continue
                m = min(o)
                if a + m[0] < 1: seen.add((side, a)); print(f"  buy {side} {a:.2f} at {t:>3.0f}s -> other cheapest {m[0]:.2f} at {m[1]:>3.0f}s, sum {a + m[0]:.2f}, net/share {1 - a - m[0] - fee(a):+.3f} (2nd leg limit)")

print(f"\n== all {len(mk)} complete games: buy the first side to reach LEVEL in the first W s; can the other side complete it later? ==")
print(f"{'level':>6}{'W':>5} | {'entered':>7} | {'sum<1':>6} {'net>0':>6} {'net>=3c':>7} | {'never':>5} {'never&lost':>10}")
for level in (0.40, 0.35, 0.30, 0.25, 0.20):
    for w in (30, 60):
        n = s1 = s0 = s3 = nev = nevl = 0
        for s, (post, win) in mk.items():
            leg = None
            for i, (t, ua, us, da, ds, ub, db) in enumerate(post):
                if t > w: break
                for side, a in (("Up", ua), ("Down", da)):
                    if a is not None and a <= level: leg = (i, side, a); break
                if leg: break
            if not leg: continue
            i, side, p1 = leg; n += 1
            o = [r[3] if side == "Up" else r[1] for r in post[i + 1:]]; o = [x for x in o if x is not None]
            m = min(o) if o else 1
            s1 += p1 + m < 1; s0 += 1 - p1 - m - fee(p1) > 0; s3 += 1 - p1 - m - fee(p1) >= 0.03
            if p1 + m >= 1: nev += 1; nevl += side != win
        print(f"{level:>6.2f}{w:>4}s | {n:>7} | {s1:>6} {s0:>6} {s3:>7} | {nev:>5} {nevl:>10}")

print("\n== money: same entry, second leg = limit order at 1 - p1 - fee(p1) - TARGET (no fee), else hold to settlement ==")
print(f"{'level':>6}{'W':>5}{'target':>8} | {'n':>4} {'pair':>5} {'lost':>5} | {'net/sh':>7} {'per $1':>7}")
for level in (0.40, 0.35, 0.30, 0.25):
    for w in (30, 60):
        for tg in (0.01, 0.03, 0.05, 0.07, 0.10):
            c = {}; pnl = cost = 0.0
            for s, (post, win) in mk.items():
                leg = None
                for i, (t, ua, us, da, ds, ub, db) in enumerate(post):
                    if t > w: break
                    for side, a in (("Up", ua), ("Down", da)):
                        if a is not None and a <= level: leg = (i, side, a); break
                    if leg: break
                if not leg: continue
                i, side, p1 = leg; p2 = int((1 - p1 - fee(p1) - tg) * 100 + 1e-9) / 100; r = None
                for row in post[i + 1:]:
                    o = row[3] if side == "Up" else row[1]
                    if o is not None and o <= p2 + 1e-9: r = ("pair", 1 - p1 - p2 - fee(p1), p1 + p2); break
                if not r: r = ("won" if side == win else "lost", ((1 - p1) if side == win else -p1) - fee(p1), p1)
                c[r[0]] = c.get(r[0], 0) + 1; pnl += r[1]; cost += r[2]
            n = sum(c.values())
            print(f"{level:>6.2f}{w:>4}s{tg:>8.2f} | {n:>4} {c.get('pair',0):>5} {c.get('lost',0):>5} | {pnl:>+7.2f} {pnl/cost:>+7.1%}")
