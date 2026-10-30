"""Early exit on reversal: after an entry, how much reversal means the trade is lost, and would selling early save money?
Read-only research (fish 2026-10-28: 「設計一個多少反轉量 = 一定會輸的測量機制，提早離開保本」).

Settlement is the Chainlink 60 s TWAP of the last minute, so with s seconds left (s <= 60) the result is
    final = ((60 - s) * known_avg + s * future_avg) / 60
and to still win, the side we bought needs   future_avg  beyond  line + (line - known part) * (60 - s) / s.
We estimate it on Coinbase's scale (open line = Coinbase TWAP of [open-60, open), as in replay_line_fix.py):
    lean  E(t) = TWAP-so-far-with-current-price-for-the-rest - line, signed toward the side we hold (bps)
    room  sd(t) = sqrt( ((s/60) * sigma * sqrt(s/3))^2 + BASIS_SD^2 )       sigma = per-second volatility (bps) of this market
    P(win) = Phi(E / sd)
The exit price is our side's best bid = 1 - the other side's best ask (Polymarket mirrors the two books), read EXIT_LAG seconds
after the trigger (time to send a sell). Fees: 7% x p x (1-p) on the buy and on the sell.

For each shadow entry of the live rules: hold to settlement vs exit at the first moment P(win) < theta (or a plain price stop).
Usage (inside btc5m_sim/): python3 -I exit_on_reversal.py data      SOL: DIST_SCALE irrelevant here; python3 -I exit_on_reversal.py data-sol
Env: RULES (w60_m95_s20,w45_m95_s20), EXIT_LAG (1.0 s), BASIS_SD (0.45 bps), SINCE (slug start, optional)
"""
import csv, math, os, statistics as st, sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from twap_vs_coinbase import load, start_of, twap_est
from replay_line_fix import book_rows

RULES = [r for r in os.getenv("RULES", "w60_m95_s20,w45_m95_s20").split(",") if r]
LAG = float(os.getenv("EXIT_LAG", "1.0")); BSD = float(os.getenv("BASIS_SD", "0.45")); FEE = 0.07
SINCE = int(os.getenv("SINCE", "0"))


def ncdf(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def fee(p):
    return FEE * p * (1 - p)


def sigma_bps(rows, upto_sl, line):
    """Per-second volatility (bps) from the Coinbase samples seen so far (secs_left >= upto_sl)."""
    pts = [(sl, sp) for sl, sp, *_ in rows if sp is not None and sl >= upto_sl]
    d = [(b[1] - a[1]) / math.sqrt(max(a[0] - b[0], 0.25)) for a, b in zip(pts, pts[1:]) if a[0] > b[0]]
    if len(d) < 10:
        return 0.3
    return max(0.15, st.pstdev(d) / line * 1e4)


def path(rows, entry_sl, side, line):
    """[(secs_left, P(win), lean_bps, exit_bid)] for every book row after the entry."""
    out = []
    for i, (sl, sp, ua, ush, da, dsh) in enumerate(rows):
        if sl >= entry_sl or sl < 1 or sl > 60:
            continue
        te = twap_est(rows, sl)
        if not te:
            continue
        sgn = 1 if side == "Up" else -1
        E = sgn * (te[0] - line) / line * 1e4
        s = sl; sig = sigma_bps(rows, sl, line)
        sd = math.sqrt(((s / 60) * sig * math.sqrt(s / 3)) ** 2 + BSD ** 2)
        other = da if side == "Up" else ua
        bid = None if other is None else round(1 - other, 3)
        out.append((sl, ncdf(E / sd), E, bid))
    return out


def bid_after(p, k):
    """Bid EXIT_LAG seconds after the trigger at index k (first row at least LAG later); None if the market ended."""
    sl0 = p[k][0]
    for sl, _, _, bid in p[k:]:
        if sl <= sl0 - LAG:
            return bid
    return None


def main(d):
    meta, _ = load(d)
    book = book_rows(d)
    by_start = {start_of(s): s for s in meta}
    ents = []
    vf = next((os.path.join(d, f) for f in ("sim_variants_v2.csv", "sim_variants.csv") if os.path.exists(os.path.join(d, f))), None)
    for r in (csv.DictReader(open(vf)) if vf else []):
        if r["variant"] not in RULES or r["entered"] != "1" or r.get("won") not in ("0", "1"):
            continue
        s = r["slug"]
        if start_of(s) < SINCE or s not in book:
            continue
        pv = by_start.get(start_of(s) - 300)
        te = twap_est(book.get(pv, []), 5) if pv else None
        if not te:
            continue
        p = path(book[s], float(r["secs_left"]), r["side"], te[0])
        if p:
            ents.append(dict(rule=r["variant"], slug=s, side=r["side"], ask=float(r["ask"]), sl=float(r["secs_left"]), won=r["won"] == "1", path=p))
    print(f"== {d}: {len(ents)} shadow entries with a full path (rules {RULES}); exit lag {LAG:g}s; prices per share, fees included")

    hold = lambda e: (1.0 if e["won"] else 0.0) - e["ask"] - fee(e["ask"])
    base = sum(hold(e) for e in ents); lost = [e for e in ents if not e["won"]]
    print(f"   hold to settlement: {len(ents)} entries, lost {len(lost)} ({len(lost) / max(len(ents), 1):.1%}), net {base:+.2f}")

    def test(name, trig):
        n_exit = saved = killed = 0; net = 0.0; miss = 0
        for e in ents:
            k = next((i for i, x in enumerate(e["path"]) if trig(x, e)), None)
            if k is None:
                net += hold(e); continue
            b = bid_after(e["path"], k)
            if b is None or b <= 0:          # the market ended or nobody to sell to: we hold
                miss += 1; net += hold(e); continue
            n_exit += 1; net += b - e["ask"] - fee(e["ask"]) - fee(b)
            if e["won"]: killed += 1
            else: saved += 1
        print(f"   {name:<34} exits {n_exit:>3} (losers saved {saved:>2}, winners cut {killed:>3}, no bid {miss:>2})  net {net:+.2f}  vs hold {net - base:+.2f}")

    print("\n   exit when the model says the trade is probably lost:")
    for th in (0.5, 0.3, 0.2, 0.1, 0.05, 0.02):
        test(f"P(win) < {th:g}", lambda x, e, th=th: x[1] < th)
    print("\n   plain price stops (sell when our side's bid falls to):")
    for b in (0.6, 0.5, 0.4, 0.3, 0.2):
        test(f"bid <= {b:g}", lambda x, e, b=b: x[3] is not None and x[3] <= b)
    print("\n   exit when the TWAP lean turns against us by at least (bps):")
    for t in (0.0, -0.5, -1.0, -2.0):
        test(f"lean <= {t:g} bps", lambda x, e, t=t: x[2] <= t)

    print("\n   exit only while the market has not caught up (model says lost, our bid still high):")
    for th, b in ((0.3, 0.5), (0.2, 0.5), (0.1, 0.4), (0.1, 0.6), (0.05, 0.5), (0.02, 0.5)):
        test(f"P(win) < {th:g} and bid >= {b:g}", lambda x, e, th=th, b=b: x[1] < th and x[3] is not None and x[3] >= b)
    # the losers one by one: when did the trade become lost, and what was the bid then?
    print("\n   losers: entry -> first time P(win) < 0.5 / < 0.1 / < 0.01, with our bid at that moment")
    for e in sorted(lost, key=lambda e: e["slug"]):
        def first(th):
            k = next((i for i, x in enumerate(e["path"]) if x[1] < th), None)
            return "never" if k is None else f"{e['path'][k][0]:>4.1f}s left bid {e['path'][k][3]}"
        print(f"     {e['slug'][-10:]} {e['rule']:<12} {e['side']:<4} @{e['ask']:.2f} {e['sl']:>4.1f}s  | <0.5 {first(0.5):<22} | <0.1 {first(0.1):<22} | <0.01 {first(0.01)}")
    # winners that dipped: how often does a trade that looks lost come back?
    print("\n   comebacks: entries whose P(win) fell below theta at some point, and how many still won")
    for th in (0.5, 0.3, 0.1, 0.05, 0.02, 0.01):
        dip = [e for e in ents if any(x[1] < th for x in e["path"])]
        w = sum(e["won"] for e in dip)
        print(f"     P(win) < {th:<5g} at some point: {len(dip):>3} entries, still won {w:>3} ({w / max(len(dip), 1):.0%})")


if __name__ == "__main__":
    for d in sys.argv[1:] or ["data"]:
        main(d)
