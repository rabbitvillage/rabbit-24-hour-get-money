"""Two resting bids, one on each side, priced so the pair costs S < $1 (fish 2026-10-29: 「兩邊掛單」). Read-only replay.
Maker orders pay no fee. When the market is still undecided (|up ask - down ask| <= BAL), place a bid on each side at
(its ask - k), k = (up ask + down ask - S) / 2. A bid fills when that side's best ask falls to or under it (someone sells
into it). Both filled -> locked 1 - S. One filled -> held to settlement. None -> nothing. Data: sim_book.csv best asks,
last 150 s (5m) / 300 s (btc15). Usage (inside btc5m_sim/): python3 -I two_sided_maker.py data data-btc15
"""
import os, sys
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from twap_vs_coinbase import load
from replay_line_fix import book_rows
BAL = float(os.getenv("BAL", "0.10")); SUMS = [float(x) for x in os.getenv("SUMS", "0.98,0.96,0.94,0.90").split(",")]

FEE = 0.07
fee = lambda p: FEE * p * (1 - p)
HEDGE_T = float(os.getenv("HEDGE_T", "-1"))      # >=0: after one leg fills, if the other has not within T s, hedge at market
HEDGE_D = float(os.getenv("HEDGE_D", "-1"))      # >=0: ... or as soon as the filled side's ask is D under its fill price

def run(rows, win, S):
    bids = None; filled = {}; t_fill = None
    for sl, sp, ua, ush, da, dsh in rows:
        if bids is None:
            if ua is None or da is None or abs(ua - da) > BAL: continue
            k = max(0.0, (ua + da - S) / 2); bids = {"Up": round(ua - k, 3), "Down": round(da - k, 3)}; continue
        for side, a in (("Up", ua), ("Down", da)):
            if side not in filled and a is not None and a <= bids[side] + 1e-9:
                filled[side] = bids[side]
                if t_fill is None: t_fill = sl
        if len(filled) == 2: return "both", 1 - sum(filled.values()), sum(filled.values())
        if len(filled) == 1 and (HEDGE_T >= 0 or HEDGE_D >= 0):
            (side, p), = filled.items(); other = da if side == "Up" else ua; mine = ua if side == "Up" else da
            due = (HEDGE_T >= 0 and t_fill - sl >= HEDGE_T - 1e-9) or (HEDGE_D >= 0 and mine is not None and mine <= p - HEDGE_D + 1e-9)
            if due and other is not None and other > 0:        # buy the other side at its ask (taker): the pair is closed at a loss
                return "hedged", 1 - p - other - fee(other), p + other
    if bids is None: return None
    if not filled: return "none", 0.0, 0.0
    (side, p), = filled.items()
    return ("one_won" if side == win else "one_lost"), ((1 - p) if side == win else -p), p

def main(d):
    meta, _ = load(d); book = book_rows(d)
    mk = [(s, m["winner"]) for s, m in meta.items() if s in book and len(book[s]) > 20]
    print(f"== {d}: {len(mk)} markets, bids placed while |up-down| <= {BAL:g}; maker = no fee; per share")
    print(f"   {'pair S':>6} | {'placed':>6} {'both':>5} {'hedged':>6} {'one won':>7} {'one lost':>8} {'none':>5} | {'net':>8} {'per $1':>7} {'avg hedge':>9} {'worst day':>9}")
    for S in SUMS:
        res = []; byday = defaultdict(float); cost = 0.0
        for s, w in mk:
            r = run(book[s], w, S)
            if r is None: continue
            res.append(r); cost += r[2]; byday[int(s.rsplit("-", 1)[1]) // 86400] += r[1]
        c = defaultdict(int)
        for r in res: c[r[0]] += 1
        net = sum(r[1] for r in res)
        hg = [r[1] for r in res if r[0] == "hedged"]
        print(f"   {S:>6.2f} | {len(res):>6} {c['both']:>5} {c['hedged']:>6} {c['one_won']:>7} {c['one_lost']:>8} {c['none']:>5} | {net:>+8.2f} {net / cost if cost else 0:>+7.1%} {(sum(hg) / len(hg)) if hg else 0:>+9.3f} {min(byday.values()) if byday else 0:>+9.2f}")

if __name__ == "__main__":
    for d in sys.argv[1:] or ["data"]: main(d)
