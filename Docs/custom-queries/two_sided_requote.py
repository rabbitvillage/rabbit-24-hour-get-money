"""Two resting bids that live only REQUOTE seconds (fish 2026-10-29: 「兩張都還沒成交才撤」). Read-only replay.
While nothing is filled: bids on both sides at (ask - k), k = (up ask + down ask - S) / 2, re-posted every REQUOTE s at the
current asks (only while |up - down| <= BAL). A bid fills when that side's ask falls to or under it. Once one side fills its
partner bid stays; it is hedged at market (buy the other side at its ask, taker fee) after HEDGE_T s or when the filled
side's ask is HEDGE_D under its fill (-1 = never: hold to settlement). Maker legs pay no fee. Per share.
Usage (inside btc5m_sim/): python3 -I two_sided_requote.py data data-btc15 data-sol
"""
import os, sys
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from twap_vs_coinbase import load
from replay_line_fix import book_rows
FEE = 0.07; fee = lambda p: FEE * p * (1 - p)
REQ = float(os.getenv("REQUOTE", "3")); BAL = float(os.getenv("BAL", "0.10"))
HT = float(os.getenv("HEDGE_T", "-1")); HD = float(os.getenv("HEDGE_D", "-1"))
SUMS = [float(x) for x in os.getenv("SUMS", "0.98,0.96,0.95,0.94").split(",")]

def run(rows, win, S):
    bids = None; t_q = None; filled = {}; t_fill = None; quoted = False
    for sl, sp, ua, ush, da, dsh in rows:
        if not filled:
            if bids is None or t_q - sl >= REQ - 1e-9:                      # (re)quote at the current asks
                if ua is None or da is None or abs(ua - da) > BAL: bids = None; continue
                k = max(0.0, (ua + da - S) / 2); bids = {"Up": round(ua - k, 3), "Down": round(da - k, 3)}; t_q = sl; quoted = True
                continue
        for side, a in (("Up", ua), ("Down", da)):
            if bids and side not in filled and a is not None and a <= bids[side] + 1e-9:
                filled[side] = bids[side]; t_fill = sl if t_fill is None else t_fill
        if len(filled) == 2: return "both", 1 - sum(filled.values()), sum(filled.values())
        if len(filled) == 1 and (HT >= 0 or HD >= 0):
            (side, p), = filled.items(); other = da if side == "Up" else ua; mine = ua if side == "Up" else da
            if ((HT >= 0 and t_fill - sl >= HT - 1e-9) or (HD >= 0 and mine is not None and mine <= p - HD + 1e-9)) and other:
                return "hedged", 1 - p - other - fee(other), p + other
    if not quoted: return None
    if not filled: return "none", 0.0, 0.0
    (side, p), = filled.items()
    return ("one_won" if side == win else "one_lost"), ((1 - p) if side == win else -p), p

def main(d):
    meta, _ = load(d); book = book_rows(d)
    mk = [(s, m["winner"]) for s, m in meta.items() if s in book and len(book[s]) > 20]
    print(f"== {d}: {len(mk)} markets | requote every {REQ:g}s while |up-down| <= {BAL:g} | hedge T={HT:g} D={HD:g}")
    print(f"   {'S':>5} | {'quoted':>6} {'both':>5} {'hedged':>6} {'1 won':>5} {'1 lost':>6} {'none':>5} | {'net':>8} {'per $1':>7} {'per mkt':>8} {'worst day':>9}")
    for S in SUMS:
        res = []; byday = defaultdict(float); cost = 0.0
        for s, w in mk:
            r = run(book[s], w, S)
            if r is None: continue
            res.append(r); cost += r[2]; byday[int(s.rsplit("-", 1)[1]) // 86400] += r[1]
        c = defaultdict(int)
        for r in res: c[r[0]] += 1
        net = sum(r[1] for r in res)
        print(f"   {S:>5.2f} | {len(res):>6} {c['both']:>5} {c['hedged']:>6} {c['one_won']:>5} {c['one_lost']:>6} {c['none']:>5} | {net:>+8.2f} {net / cost if cost else 0:>+7.1%} {net / max(len(res), 1):>+8.4f} {min(byday.values()) if byday else 0:>+9.2f}")

if __name__ == "__main__":
    for d in sys.argv[1:] or ["data"]: main(d)
