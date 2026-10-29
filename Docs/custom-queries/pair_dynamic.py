"""Dynamic version of fish's opening pair rule (fish 2026-10-30: 「不要死守一個範圍，要動態應變，能組成就出手」). Read-only replay
on /opt/pm-pair/data. All market (taker) orders, taker fee 7% x p x (1-p) on every buy and sell, equal shares on both legs.
Leg 1 trigger (first match within FIRST s of the open):
  abs  : a side's ask <= L1
  drop : a side's ask fell >= DROP within the last LOOK s (relative dump, as the open-source dump-and-hedge bots do)
Leg 2: the other side whenever 1 - p1 - p2 - fee(p1) - fee(p2) > MARGIN (the cap is computed from p1, no fixed 0.56)
Cut (before leg 2): sell leg 1 at its bid when the bid is <= p1 - STOP, or when CUT_T s passed since leg 1; else hold to the end.
Usage (in /opt/pm-pair/data): python3 -I pair_dynamic.py
"""
import csv, glob, itertools, os
from collections import defaultdict
FEE = 0.07; fee = lambda p: FEE * p * (1 - p); MIN = float(os.getenv("MIN", "60")); MARGIN = float(os.getenv("MARGIN", "0.001"))

def load():
    out = {}
    for asset in ("btc", "eth", "sol"):
        mk = defaultdict(list)
        for p in sorted(glob.glob(f"book_{asset}_*.csv")):
            for r in csv.DictReader(open(p)):
                g = lambda k: float(r[k]) if r[k] else None
                mk[r["slug"]].append((float(r["s_from_open"]), g("up_ask"), g("up_ask_sz") or 0, g("dn_ask"), g("dn_ask_sz") or 0, g("up_bid"), g("dn_bid")))
        for s, rows in mk.items():
            rows.sort()
            if rows[0][0] > -5 or rows[-1][0] < 280: continue
            win = "Up" if (rows[-1][5] or 0) >= 0.9 else "Down" if (rows[-1][6] or 0) >= 0.9 else None
            if win: out[s] = ([r for r in rows if r[0] >= 0], win)
    return out

def run(post, win, mode, l1, drop, look, first, stop, cut_t):
    hist = {"Up": [], "Down": []}; leg = None
    for i, (t, ua, us, da, ds, ub, db) in enumerate(post):
        if t > first: break
        for side, a, sz in (("Up", ua, us), ("Down", da, ds)):
            if a is None: continue
            hist[side].append((t, a))
            if sz < MIN: continue
            if mode == "abs" and a <= l1: leg = (i, side, a, t); break
            if mode == "drop":
                past = [x for tt, x in hist[side] if t - look <= tt < t]
                if past and max(past) - a >= drop and a < 0.5: leg = (i, side, a, t); break
        if leg: break
    if not leg: return None
    i, side, p1, t1 = leg
    for t, ua, us, da, ds, ub, db in post[i + 1:]:
        oth, osz = (da, ds) if side == "Up" else (ua, us); mybid = ub if side == "Up" else db
        if oth is not None and osz >= MIN and 1 - p1 - oth - fee(p1) - fee(oth) > MARGIN:
            return "pair", 1 - p1 - oth - fee(p1) - fee(oth), p1 + oth
        if mybid is not None and ((stop is not None and mybid <= p1 - stop) or (cut_t is not None and t - t1 >= cut_t)):
            return "cut", mybid - p1 - fee(p1) - fee(mybid), p1
    return ("held_won" if side == win else "held_lost"), ((1 - p1) if side == win else -p1) - fee(p1), p1

def summarize(mks, **kw):
    c = defaultdict(int); pnl = cost = 0.0
    for s, (post, win) in mks.items():
        r = run(post, win, **kw)
        if not r: c["none"] += 1; continue
        c[r[0]] += 1; pnl += r[1]; cost += r[2]
    n = sum(v for k, v in c.items() if k != "none")
    return n, c, pnl, (pnl / cost if cost else 0)

if __name__ == "__main__":
    mks = load(); print(f"{len(mks)} complete markets (BTC+ETH+SOL)")
    print(f"{'trigger':<22}{'stop':>6}{'cut s':>6} | {'n':>4} {'pair':>5} {'cut':>4} {'heldL':>5} {'heldW':>5} | {'net/sh':>7} {'per $1':>7}")
    rows = []
    for (mode, l1, drop, look) in [("abs", 0.40, 0, 0), ("abs", 0.45, 0, 0), ("drop", 0, 0.08, 3), ("drop", 0, 0.10, 5), ("drop", 0, 0.15, 10)]:
        for stop, cut_t in itertools.product([None, 0.05, 0.08, 0.12, 0.20], [None, 30, 60]):
            n, c, pnl, r = summarize(mks, mode=mode, l1=l1, drop=drop, look=look, first=60, stop=stop, cut_t=cut_t)
            trig = f"ask<= {l1:.2f}" if mode == "abs" else f"drop {drop:.2f}/{look}s"
            rows.append((r, trig, stop, cut_t, n, c, pnl))
    for r, trig, stop, cut_t, n, c, pnl in rows:
        print(f"{trig:<22}{('-' if stop is None else f'{stop:.2f}'):>6}{('-' if cut_t is None else cut_t):>6} | {n:>4} {c['pair']:>5} {c['cut']:>4} {c['held_lost']:>5} {c['held_won']:>5} | {pnl:>+7.2f} {r:>+7.1%}")
