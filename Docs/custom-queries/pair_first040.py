"""fish 2026-10-29 rule: in the first 60 s after the open, market-buy whichever side first trades down to L1 (0.40); then
market-buy the other side the first time its ask is <= L2 (0.56 = the most that still leaves a profit after both taker
fees); hold both to settlement ($1). Requires >= MIN shares at each ask. Read-only replay on /opt/pm-pair/data.
Usage (in /opt/pm-pair/data): python3 -I pair_first040.py   env: L1 0.40  L2 0.56  MIN 60  FIRST 60
"""
import csv, glob, os
from collections import defaultdict
FEE = 0.07; fee = lambda p: FEE * p * (1 - p)
L1 = float(os.getenv("L1", "0.40")); L2 = float(os.getenv("L2", "0.56")); MIN = float(os.getenv("MIN", "60")); FIRST = float(os.getenv("FIRST", "60"))
tot = dict(trade=0, paired=0, naked=0, none=0, pnl=0.0, cost=0.0)
for asset in ("btc", "eth", "sol"):
    mk = defaultdict(list); span = defaultdict(list)
    for p in sorted(glob.glob(f"book_{asset}_*.csv")):
        for r in csv.DictReader(open(p)):
            t = float(r["s_from_open"]); span[r["slug"]].append(t)
            g = lambda k: float(r[k]) if r[k] else None
            mk[r["slug"]].append((t, g("up_ask"), g("up_ask_sz") or 0, g("dn_ask"), g("dn_ask_sz") or 0, g("up_bid"), g("dn_bid")))
    full = {s: sorted(mk[s]) for s in span if min(span[s]) <= -5 and max(span[s]) >= 280}
    a_tr = a_pair = 0; a_pnl = 0.0; per = []
    for s, rows in sorted(full.items()):
        post = [r for r in rows if r[0] >= 0]
        win = "Up" if (rows[-1][5] or 0) >= 0.9 else "Down" if (rows[-1][6] or 0) >= 0.9 else None
        leg = None
        for i, (t, ua, us, da, ds, _, _) in enumerate(post):
            if t > FIRST: break
            if ua is not None and ua <= L1 and us >= MIN: leg = (i, "Up", ua, t); break
            if da is not None and da <= L1 and ds >= MIN: leg = (i, "Down", da, t); break
        if not leg:
            tot["none"] += 1; continue
        i, side, p1, t1 = leg; a_tr += 1; tot["trade"] += 1
        got = None
        for t, ua, us, da, ds, _, _ in post[i + 1:]:
            a, sz = (da, ds) if side == "Up" else (ua, us)
            if a is not None and a <= L2 and sz >= MIN: got = (t, a); break
        if got:
            pnl = 1 - p1 - got[1] - fee(p1) - fee(got[1]); a_pair += 1; tot["paired"] += 1; tot["cost"] += p1 + got[1]
            per.append(f"{pnl * 100:+.1f}c")
        else:
            pnl = ((1 - p1) if side == win else -p1) - fee(p1); tot["naked"] += 1; tot["cost"] += p1
            per.append(f"NAKED {pnl * 100:+.0f}c")
        a_pnl += pnl; tot["pnl"] += pnl
    print(f"== {asset}: {len(full)} markets, traded {a_tr}, paired {a_pair}, net {a_pnl * 100:+.1f} cents/share  [{', '.join(per)}]")
c = tot["cost"] or 1
print(f"== all: traded {tot['trade']}, paired {tot['paired']}, naked {tot['naked']}, no trigger {tot['none']}; "
      f"net {tot['pnl']:+.3f} $/share = {tot['pnl'] / c * 100:+.1f}% per $1; at 60 shares per leg = ${tot['pnl'] * 60:+.2f}")
