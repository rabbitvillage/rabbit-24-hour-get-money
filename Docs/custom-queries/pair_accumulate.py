"""Accumulate both sides in small market buys so that avg(Up) + avg(Down) < $1 (the 'buy both sides cheap' bots; fish
2026-10-29). Read-only replay on pair_sim recorder data (book_<asset>_<day>.csv, complete markets only).
Every STEP seconds, for the side whose buy keeps the pair cheap: buy LOT shares at its ask (taker fee) when
   ask + fee(ask) + (avg cost per share of the other side, or its current ask if none yet) <= CAP
and this side would not hold more than IMB shares more than the other; at most MAXQ shares per side. Winner from the
last rows. Payout = shares of the winning side. Per market P&L in $ (LOT shares per buy).
Usage (in /opt/pm-pair/data): python3 -I pair_accumulate.py btc eth sol    env: LOT 5 STEP 1 IMB 10 MAXQ 60 CAP 0.98 UNTIL 300
"""
import csv, glob, os, sys
from collections import defaultdict
FEE = 0.07; fee = lambda p: FEE * p * (1 - p)
LOT = float(os.getenv("LOT", "5")); STEP = float(os.getenv("STEP", "1")); IMB = float(os.getenv("IMB", "10"))
MAXQ = float(os.getenv("MAXQ", "60")); CAP = float(os.getenv("CAP", "0.98")); UNTIL = float(os.getenv("UNTIL", "300"))

def load(asset):
    mk = defaultdict(list)
    for p in sorted(glob.glob(f"book_{asset}_*.csv")):
        for r in csv.DictReader(open(p)):
            f = lambda k: float(r[k]) if r[k] else None
            mk[r["slug"]].append((float(r["s_from_open"]), f("up_ask"), f("up_ask_sz") or 0, f("dn_ask"), f("dn_ask_sz") or 0, f("up_bid"), f("dn_bid")))
    out = {}
    for s, rows in mk.items():
        rows.sort()
        if not rows or rows[0][0] > -5 or rows[-1][0] < 290: continue
        win = "Up" if (rows[-1][5] or 0) >= 0.9 else "Down" if (rows[-1][6] or 0) >= 0.9 else None
        if win: out[s] = (rows, win)
    return out

def run(rows, win):
    q = {"Up": 0.0, "Down": 0.0}; c = {"Up": 0.0, "Down": 0.0}; last = -1e9
    for t, ua, us, da, ds, _, _ in rows:
        if t < 0 or t > UNTIL or t - last < STEP: continue
        asks = {"Up": (ua, us), "Down": (da, ds)}
        cands = []
        for side, other in (("Up", "Down"), ("Down", "Up")):
            a, sz = asks[side]
            if not a or sz < LOT or a >= 0.99 or q[side] + LOT > MAXQ or q[side] + LOT - q[other] > IMB: continue
            ref = c[other] / q[other] if q[other] else (asks[other][0] or 1)
            if a + fee(a) + ref <= CAP: cands.append((a, side))
        if cands:
            a, side = min(cands); q[side] += LOT; c[side] += LOT * (a + fee(a)); last = t
    cost = c["Up"] + c["Down"]
    return q[win] - cost, cost, q["Up"], q["Down"]

def main(asset):
    mks = load(asset); res = []
    for s, (rows, win) in sorted(mks.items()):
        pnl, cost, qu, qd = run(rows, win); res.append((s, pnl, cost, qu, qd, win))
    if not res: print(f"== {asset}: no complete markets"); return
    tr = [r for r in res if r[2] > 0]; pos = sum(1 for r in tr if r[1] > 0)
    net = sum(r[1] for r in res); cost = sum(r[2] for r in res)
    print(f"== {asset}: {len(res)} markets | lot {LOT:g} every {STEP:g}s, imbalance <= {IMB:g}, <= {MAXQ:g}/side, pair cap {CAP}")
    print(f"   traded {len(tr)}  profitable {pos}  net ${net:+.2f}  per $1 {net / cost if cost else 0:+.1%}  worst ${min(r[1] for r in res):+.2f}  best ${max(r[1] for r in res):+.2f}")
    for s, pnl, cst, qu, qd, w in res:
        print(f"     {s[-10:]}  Up {qu:>4.0f} sh  Down {qd:>4.0f} sh  cost ${cst:6.2f}  winner {w:<4}  pnl ${pnl:+.2f}")

if __name__ == "__main__":
    for a in sys.argv[1:] or ["btc"]: main(a)
