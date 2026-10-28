"""Replay the live rules with the old line and with the 2026-10-28 line fix, market by market, and compare the entries.
Read-only.

Old line: dl = Coinbase now - on-chain aggregator at open (chain_at_start; spot at open when missing).
New line: dl = Coinbase now - (official open - Chainlink/Coinbase gap). Historically the official open is priceToBeat and
the gap at open is priceToBeat - Coinbase 60 s TWAP of the same window, so the new line on Coinbase's scale is simply the
Coinbase TWAP of [open-60, open) (from the previous market's sim_book rows). Live, the bot uses the per-second Chainlink
stream and a 120 s median gap instead; this replay is the closest the recorded data allows.

Rules replayed from sim_book.csv (0.5-1 s rows): band 0.80-0.95, at least SHARES on the book, Up checked first, distance
>= 20 * DIST_SCALE on the right side, chop (price range since 150 s left >= CHOP_MAX_AMP_BPS of the line) blocks, book gap
(asks summing above 1 + BOOK_GAP_MAX) blocks when BOOK_GAP_MAX > 0, last entry 5 s before the close.

Usage (inside btc5m_sim/):  python3 -I replay_line_fix.py data            BTC
                            DIST_SCALE=0.004 python3 -I replay_line_fix.py data-sol
Env: SHARES (5), CHOP_MAX_AMP_BPS (8), BOOK_GAP_MAX (0), RULES (w60_m95_s20,w45_m95_s20)
"""
import csv, os, sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from twap_vs_coinbase import load, start_of, twap_est   # same loaders and Coinbase TWAP as the investigation

FLOOR = float(os.getenv("FLOOR", "0.80"))
SHARES = float(os.getenv("SHARES", "5")); CHOP = float(os.getenv("CHOP_MAX_AMP_BPS", "8")); GAP = float(os.getenv("BOOK_GAP_MAX", "0"))
THR = 20 * float(os.getenv("DIST_SCALE", "1")); FEE = 0.07
RULES = {"w60_m95_s20": 60, "w45_m95_s20": 45}
RULES = {k: v for k, v in RULES.items() if k in os.getenv("RULES", ",".join(RULES)).split(",")}


def book_rows(d):
    rows = defaultdict(list)
    for r in csv.DictReader(open(os.path.join(d, "sim_book.csv"))):
        try:
            sl = float(r["secs_left"]); sp = float(r["spot"]) if r["spot"] else None
        except ValueError:
            continue
        f = lambda k: float(r[k]) if r[k] else None
        rows[r["slug"]].append((sl, sp, f("up_ask"), f("up_sh"), f("down_ask"), f("down_sh")))
    for v in rows.values():
        v.sort(key=lambda x: -x[0])
    return rows


def replay(rows, ref, window):
    """First entry (side, ask, secs_left) of a w{window}_m95_s20 rule against line `ref`, or None."""
    lo = hi = None
    for sl, sp, ua, ush, da, dsh in rows:
        if sp is not None:
            lo = sp if lo is None else min(lo, sp); hi = sp if hi is None else max(hi, sp)
        if sl > window or sl <= 5:
            continue
        hit = None
        for side, ask, sh in (("Up", ua, ush), ("Down", da, dsh)):
            if ask is not None and FLOOR <= ask <= 0.95 and (sh or 0) >= SHARES:
                hit = (side, ask); break
        if not hit or sp is None or ref is None:
            continue
        dl = sp - ref
        if abs(dl) < THR or (dl > 0) != (hit[0] == "Up"):
            continue
        other = da if hit[0] == "Up" else ua
        if GAP and other is not None and hit[1] + other - 1 > GAP + 1e-9:
            continue
        if CHOP and lo is not None and (hi - lo) / ref * 1e4 >= CHOP:
            continue
        return hit[0], hit[1], sl
    return None


def pnl(ask, won):
    return (1 - ask if won else -ask) - FEE * ask * (1 - ask)


def main(d):
    meta, _ = load(d)
    book = book_rows(d)
    by_start = {start_of(s): s for s in meta}
    shadow = {}
    p2 = os.path.join(d, "sim_variants_v2.csv")
    if os.path.exists(p2):
        for r in csv.DictReader(open(p2)):
            if r["variant"] in RULES:
                shadow[(r["slug"], r["variant"])] = (r["side"], float(r["ask"])) if r["entered"] == "1" else None
    print(f"== {d}: distance ${THR:g}, SHARES {SHARES:g}, chop {CHOP:g} bps, book gap {GAP:g}")
    for rule, window in RULES.items():
        res = []; agree = n_cmp = 0
        for s, m in meta.items():
            rows = book.get(s); pv = by_start.get(start_of(s) - 300)
            if not rows or pv not in book:
                continue
            te = twap_est(book[pv], 5)
            if not te:
                continue
            old_ref = m["chain0"] if m["chain0"] is not None else m["spot0"]
            old = replay(rows, old_ref, window); new = replay(rows, te[0], window)
            exact = replay(rows, m["ptb"], window)
            res.append((s, m["winner"], old, new, exact))
            if (s, rule) in shadow:
                n_cmp += 1; sh = shadow[(s, rule)]
                agree += (sh is None and old is None) or (sh is not None and old is not None and sh[0] == old[0])
        print(f"\n   {rule}: {len(res)} markets; old-line replay agrees with the bot's own shadow ledger on {agree}/{n_cmp} "
              f"({agree / max(n_cmp, 1):.1%}) — the gap is the replay's 0.5-1 s rows vs the bot's 0.1-0.25 s loop")
        groups = {"both lines enter": [], "old only (fix removes)": [], "new only (fix adds)": [], "both, opposite side": []}
        for s, win, old, new, _ in res:
            if old and new:
                groups["both, opposite side" if old[0] != new[0] else "both lines enter"].append((s, win, old, new))
            elif old:
                groups["old only (fix removes)"].append((s, win, old, None))
            elif new:
                groups["new only (fix adds)"].append((s, win, None, new))
        for g in ("old", "new"):
            es = [(w, x) for _, w, *on in res for x in [on[0] if g == "old" else on[1]] if x]
            if es:
                lost = sum(x[0] != w for w, x in es); p = sum(pnl(x[1], x[0] == w) for w, x in es)
                print(f"      {g} line total: {len(es)} entries, lost {lost} ({lost / len(es):.1%}), net per share {p:+.2f} (after fee)")
        for name, es in groups.items():
            if not es:
                continue
            pick = (lambda e: e[3]) if name == "new only (fix adds)" else (lambda e: e[2])
            lost = sum(pick(e)[0] != e[1] for e in es); p = sum(pnl(pick(e)[1], pick(e)[0] == e[1]) for e in es)
            extra = ""
            if name == "both, opposite side":
                lost_new = sum(e[3][0] != e[1] for e in es)
                extra = f"; with the new line's side: lost {lost_new}, net {sum(pnl(e[3][1], e[3][0] == e[1]) for e in es):+.2f}"
            print(f"      {name:<24} n={len(es):>4}  lost {lost:>3} ({lost / len(es):5.1%})  net per share {p:+.2f}{extra}")
        print("      by side (net per share after fee):")
        for g, idx in (("old line (on-chain open)", 0), ("exact open, no gap", 2), ("exact open + gap (the fix)", 1)):
            parts = []
            for side in ("Up", "Down"):
                es = [(w, x) for _, w, *on in res for x in [on[idx]] if x and x[0] == side]
                if es:
                    lost = sum(x[0] != w for w, x in es)
                    parts.append(f"{side} {len(es)} lost {lost} ({lost / len(es):.1%}) {sum(pnl(x[1], x[0] == w) for w, x in es):+.2f}")
            print(f"         {g:<28} " + " | ".join(parts))


if __name__ == "__main__":
    for d in sys.argv[1:] or ["data"]:
        main(d)
