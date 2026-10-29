"""Replay fish's opening-phase pair idea on the pair_sim recorder's data (book_<asset>_<day>.csv). Read-only.
For each market (rows from the open to the close; winner read off the last rows: the side whose bid ends >= 0.9):
  hindsight: does an ordered pair exist (side X bought at t1, the other side at t2 > t1) with
             1 - askX - askY - fee(askX) - fee(askY) >= MARGIN, with >= MIN_SZ shares at each ask?  When is it first possible?
  rule A (leg 1 = the falling side): first side whose ask <= L1 is bought; leg 2 = the other side once the pair is net >= MARGIN.
  rule B (leg 1 = pre-open Up at the 0.50/0.51 quote, 5 s before the open); leg 2 = Down once the pair is net >= MARGIN.
  rule C (leg 1 = the rising side): first side whose ask >= H1 is bought; leg 2 = the other side once net >= MARGIN.
  Leg 2 never comes -> leg 1 held to settlement. Per share, taker fee 7% x p x (1-p) on both legs.
Usage (in /opt/pm-pair/data): python3 -I pair_open_replay.py btc [eth sol]   env: MARGIN (0.01) MIN_SZ (60) L1 (0.45) H1 (0.58)
"""
import csv, glob, os, sys
from collections import defaultdict
FEE = 0.07; fee = lambda p: FEE * p * (1 - p)
MARGIN = float(os.getenv("MARGIN", "0.01")); MIN_SZ = float(os.getenv("MIN_SZ", "60"))
L1 = float(os.getenv("L1", "0.45")); H1 = float(os.getenv("H1", "0.58"))
L1S = [float(x) for x in os.getenv("L1S", "0.48,0.46,0.44,0.42,0.40").split(",")]

def load(asset):
    mk = defaultdict(list)
    for p in sorted(glob.glob(f"book_{asset}_*.csv")):
        for r in csv.DictReader(open(p)):
            f = lambda k: float(r[k]) if r[k] else None
            mk[r["slug"]].append((float(r["s_from_open"]), f("up_ask"), f("up_ask_sz") or 0, f("dn_ask"), f("dn_ask_sz") or 0, f("up_bid"), f("dn_bid")))
    out = {}
    for s, rows in mk.items():
        rows.sort()
        if not rows or rows[0][0] > -5 or rows[-1][0] < 290: continue          # need the whole market recorded
        ub, db = rows[-1][5], rows[-1][6]
        win = "Up" if (ub or 0) >= 0.9 else "Down" if (db or 0) >= 0.9 else None
        if win: out[s] = (rows, win)
    return out

def ok(p1, p2): return 1 - p1 - p2 - fee(p1) - fee(p2) >= MARGIN - 1e-12

def leg2(rows, i0, side, p1):
    for t, ua, us, da, ds, _, _ in rows[i0 + 1:]:
        a, sz = (da, ds) if side == "Up" else (ua, us)
        if a and sz >= MIN_SZ and ok(p1, a): return t, a
    return None

def settle(side, p1, win, l2):
    if l2: return "pair", 1 - p1 - l2[1] - fee(p1) - fee(l2[1]), p1 + l2[1]
    return ("naked_won" if side == win else "naked_lost"), ((1 - p1) if side == win else -p1) - fee(p1), p1

def main(asset):
    mks = load(asset)
    print(f"== {asset}: {len(mks)} complete markets | margin {MARGIN:g} after both taker fees | >= {MIN_SZ:g} shares at each ask")
    hs = []; res = defaultdict(list)
    for s, (rows, win) in sorted(mks.items()):
        post = [r for r in rows if r[0] >= 0]
        # hindsight: earliest t2 such that some earlier t1 gives a net-positive ordered pair
        best = None; minU = minD = (9, None)
        for t, ua, us, da, ds, _, _ in post:
            if best is None:
                if ua and us >= MIN_SZ and minD[0] < 9 and ok(minD[0], ua): best = (minD[1], t, minD[0] + ua)
                elif da and ds >= MIN_SZ and minU[0] < 9 and ok(minU[0], da): best = (minU[1], t, minU[0] + da)
            if ua and us >= MIN_SZ and ua < minU[0]: minU = (ua, t)
            if da and ds >= MIN_SZ and da < minD[0]: minD = (da, t)
        hs.append((s, best))
        # rule A: falling side first
        for i, (t, ua, us, da, ds, _, _) in enumerate(post):
            pick = ("Up", ua) if ua and us >= MIN_SZ and ua <= L1 else ("Down", da) if da and ds >= MIN_SZ and da <= L1 else None
            if pick: res["A falling side first"].append(settle(pick[0], pick[1], win, leg2(post, i, pick[0], pick[1]))); break
        # rule B: pre-open Up ~5 s before the open
        pre = [r for r in rows if -6 <= r[0] < 0 and r[1]]
        if pre:
            i = rows.index(pre[-1]); p = pre[-1][1]
            res["B pre-open Up"].append(settle("Up", p, win, leg2(rows, i, "Up", p)))
        # rule E (fish 10-29: 「第一張絕對不可以用限價單」): leg 1 = market buy of the first side whose ask <= L1;
        # leg 2 = a resting bid on the other side at T = 1 - p1 - fee(p1) - MARGIN (maker, no fee), filled when that side's ask
        # reaches T, or a market buy of it (taker fee) as soon as the pair is net >= MARGIN -- whichever the book allows first.
        for l1 in L1S:
            for i, (t, ua, us, da, ds, _, _) in enumerate(post):
                pick = ("Up", ua) if ua and us >= MIN_SZ and ua <= l1 else ("Down", da) if da and ds >= MIN_SZ and da <= l1 else None
                if not pick: continue
                side, p1 = pick; T = 1 - p1 - fee(p1) - MARGIN; done = None
                for t2, ua2, us2, da2, ds2, _, _ in post[i + 1:]:
                    a = da2 if side == "Up" else ua2
                    if a is not None and a <= T + 1e-9: done = 1 - p1 - T - fee(p1); break          # the resting bid fills at T
                if done is not None: res[f"E mkt<= {l1:.2f} + bid"].append(("pair", done, p1 + T))
                else: res[f"E mkt<= {l1:.2f} + bid"].append(settle(side, p1, win, None))
                break
        # rule F (fish 10-29: 「只要每一場能夠獲利就撤退，大於零就是賺」): leg 1 = market buy of the first side whose ask <= L1;
        # then leave at the FIRST moment either exit is net > 0: (a) buy the other side (pair locked), or (b) sell leg 1 at its bid.
        for l1 in L1S:
            for i, (t, ua, us, da, ds, ub, db) in enumerate(post):
                pick = ("Up", ua) if ua and us >= MIN_SZ and ua <= l1 else ("Down", da) if da and ds >= MIN_SZ and da <= l1 else None
                if not pick: continue
                side, p1 = pick; out = None
                for t2, ua2, us2, da2, ds2, ub2, db2 in post[i + 1:]:
                    oth = da2 if side == "Up" else ua2; mybid = ub2 if side == "Up" else db2
                    if oth and 1 - p1 - oth - fee(p1) - fee(oth) > 0.001: out = ("pair", 1 - p1 - oth - fee(p1) - fee(oth), p1 + oth); break
                    if mybid and mybid - p1 - fee(p1) - fee(mybid) > 0.001: out = ("sold", mybid - p1 - fee(p1) - fee(mybid), p1); break
                res[f"F mkt<= {l1:.2f} exit>0"].append(out or settle(side, p1, win, None))
                break
        # rule C: rising side first
        for i, (t, ua, us, da, ds, _, _) in enumerate(post):
            pick = ("Up", ua) if ua and us >= MIN_SZ and ua >= H1 and ua < 0.9 else ("Down", da) if da and ds >= MIN_SZ and da >= H1 and da < 0.9 else None
            if pick: res["C rising side first"].append(settle(pick[0], pick[1], win, leg2(post, i, pick[0], pick[1]))); break
    n = len(hs); found = [b for _, b in hs if b]
    if n:
        ts = sorted(b[1] for b in found)
        print(f"   hindsight: a net-positive ordered pair existed in {len(found)}/{n} markets ({len(found) / n:.0%});"
              f" completed by second {ts[len(ts) // 2]:.0f} (median), best-pair cost median {sorted(b[2] for b in found)[len(found) // 2]:.2f}" if found else "   hindsight: none")
    for name, rs in res.items():
        if not rs: continue
        c = defaultdict(int)
        for r in rs: c[r[0]] += 1
        net = sum(r[1] for r in rs); cost = sum(r[2] for r in rs)
        print(f"   {name:<22} n={len(rs):>3}  paired {c['pair']:>3}  sold {c['sold']:>3}  naked won {c['naked_won']:>2}  naked lost {c['naked_lost']:>2}  net/share {net:+.2f}  per $1 {net / cost:+.1%}")

if __name__ == "__main__":
    for a in sys.argv[1:] or ["btc"]: main(a)
