"""Buy both sides of one market at two different moments so the pair costs < $1 (fish 2026-10-29). Read-only replay.

Leg 1: the first time either side's best ask is <= L1, buy that side at its ask.
Leg 2: afterwards, buy the other side the first time its ask <= T, where T makes the pair net-positive after fees:
       1 - p1 - p2 - fee(p1) - fee2(p2) >= MARGIN   (fee = 7% x p x (1-p) taker; MAKER2=1 assumes leg 2 is a resting bid: no fee)
If leg 2 never comes, leg 1 is held to settlement (no selling). Per share, fees included. Data: sim_book.csv best asks,
0.5-1 s rows, only the last BOOK_WINDOW seconds of each market (150 s for 5m, 300 s for btc15) -> earlier swings unseen.
Usage (inside btc5m_sim/): python3 -I legged_pair.py data [data-sol data-btc15]
"""
import os, sys
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from twap_vs_coinbase import load
from replay_line_fix import book_rows

FEE = 0.07
fee = lambda p: FEE * p * (1 - p)
MAKER2 = os.getenv("MAKER2", "0") == "1"
BAL = float(os.getenv("BAL", "0"))   # >0: leg 1 only when |up ask - down ask| <= BAL (an undecided market)
L1S = [float(x) for x in os.getenv("L1S", "0.30,0.40,0.45,0.50,0.55,0.60").split(",")]
MARGINS = [float(x) for x in os.getenv("MARGINS", "0,0.01,0.02").split(",")]


def run(rows, win, l1, margin):
    leg1 = None
    for sl, sp, ua, ush, da, dsh in rows:
        if leg1 is None:
            if BAL and (ua is None or da is None or abs(ua - da) > BAL): continue   # only while the market is still undecided
            for side, a in (("Up", ua), ("Down", da)):
                if a is not None and 0 < a <= l1:
                    leg1 = (side, a); break
            continue
        other = da if leg1[0] == "Up" else ua
        if other is None or other <= 0: continue
        f2 = 0.0 if MAKER2 else fee(other)
        if 1 - leg1[1] - other - fee(leg1[1]) - f2 >= margin - 1e-12:
            return "locked", 1 - leg1[1] - other - fee(leg1[1]) - f2, leg1[1] + other
    if leg1 is None:
        return None
    p = leg1[1]
    return ("naked_won" if leg1[0] == win else "naked_lost"), ((1 - p) if leg1[0] == win else -p) - fee(p), p


def main(d):
    meta, _ = load(d); book = book_rows(d)
    mk = [(s, m["winner"]) for s, m in meta.items() if s in book and len(book[s]) > 20]
    days = max(1, len({s.rsplit("-", 1)[1][:5] for s, _ in mk}) and (max(int(s.rsplit("-", 1)[1]) for s, _ in mk) - min(int(s.rsplit("-", 1)[1]) for s, _ in mk)) / 86400)
    print(f"== {d}: {len(mk)} markets with a book window ({days:.1f} days){' | leg 2 as maker (no fee)' if MAKER2 else ''}")
    print(f"   {'L1':>5} {'margin':>6} | {'entered':>7} {'locked':>7} {'lock%':>6} {'naked W':>7} {'naked L':>7} | {'net/share':>9} {'per pair':>8} {'per $1':>7} {'worst day':>9}")
    for l1 in L1S:
        for mg in MARGINS:
            res = []; byday = defaultdict(float); cost = 0.0
            for s, w in mk:
                r = run(book[s], w, l1, mg)
                if r is None: continue
                res.append(r); byday[int(s.rsplit("-", 1)[1]) // 86400] += r[1]; cost += r[2]
            if not res: continue
            n = len(res); lk = sum(1 for r in res if r[0] == "locked"); nw = sum(1 for r in res if r[0] == "naked_won"); nl = n - lk - nw
            net = sum(r[1] for r in res)
            print(f"   {l1:>5.2f} {mg:>6.2f} | {n:>7} {lk:>7} {lk / n:>6.0%} {nw:>7} {nl:>7} | {net:>+9.2f} {net / n:>+8.3f} {net / cost:>+7.1%} {min(byday.values()):>+9.2f}")


if __name__ == "__main__":
    for d in sys.argv[1:] or ["data"]:
        main(d)
