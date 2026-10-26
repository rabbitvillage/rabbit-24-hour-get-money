"""Read-only: could a rule that only buys the leader at exactly 0.97 or 0.98 in the last seconds make money?
For every settled market and window (60/45/30 s) the first moment the leader's best ask equals the price with at
least N shares on it, as if bought there. Reports flip rate, opportunities per day and expected value per share after
the Polymarket crypto taker fee (7% x p x (1-p)), also when the fill lands 1 cent worse (happens on ~16% of fills).
Usage:  python3 -I Docs/custom-queries/tail98.py btc5m_sim/data [btc5m_sim/data-eth ...]
"""
import csv, sys, collections
from datetime import datetime, timezone, timedelta
try:
    from zoneinfo import ZoneInfo; NY = ZoneInfo("America/New_York")
except Exception:
    NY = None
def ny(dt):
    if NY: return dt.astimezone(NY)
    return dt.astimezone(timezone(timedelta(hours=-4 if dt < datetime(2026, 11, 1, 6, tzinfo=timezone.utc) else -5)))
def us(start_utc):
    t = ny(datetime.fromisoformat(start_utc))
    return t.weekday() < 5 and 9 * 60 + 30 <= t.hour * 60 + t.minute < 16 * 60
FEE = 0.07
def ev(p, flip):
    fee = FEE * p * (1 - p); return (1 - flip) * (1 - p - fee) - flip * (p + fee)
def breakeven(p):
    return 1 - p - FEE * p * (1 - p)
PRICES, WINDOWS, DEPTHS = (0.97, 0.98), (60, 45, 30), (1, 20, 50)
for d in sys.argv[1:]:
    win, start = {}, {}
    for r in csv.DictReader(open(d + "/sim_trades.csv")):
        if r["winner"] in ("Up", "Down"): win[r["slug"]] = r["winner"]; start[r["slug"]] = r["start_utc"]
    days = len({s[:10] for s in start.values()})
    first = {}
    for r in csv.DictReader(open(d + "/sim_book.csv")):
        s = r["slug"]
        if s not in win: continue
        try: secs = float(r["secs_left"])
        except ValueError: continue
        for side, k in (("Up", "up"), ("Down", "down")):
            try: a = float(r[k + "_ask"]); sh = float(r[k + "_sh"] or 0)
            except ValueError: continue
            for P in PRICES:
                if abs(a - P) < 1e-9:
                    for w in WINDOWS:
                        if secs <= w:
                            for m in DEPTHS:
                                if sh >= m and (s, P, w, m) not in first: first[(s, P, w, m)] = (side, secs)
    print(f"===== {d}: {len(win)} settled markets over {days} days")
    for P in PRICES:
        print(f"-- best ask exactly {P:.2f}; break-even flip rate {100*breakeven(P):.2f}% after fee; if filled 1c worse: {100*breakeven(P+0.01):.2f}%")
        for w in WINDOWS:
            for m in DEPTHS:
                tot = collections.Counter(); flip = collections.Counter()
                for (s, p, ww, mm), (side, secs) in first.items():
                    if (p, ww, mm) != (P, w, m): continue
                    for key in ("all", "US" if us(start[s]) else "nonUS"):
                        tot[key] += 1; flip[key] += side != win[s]
                if not tot["all"]: continue
                def cell(k):
                    n, f = tot[k], flip[k]
                    if not n: return f"{k}: -"
                    return f"{k}: n={n} ({n/days:.1f}/day) flip={f} {100*f/n:.2f}% ev/share={100*ev(P, f/n):+.2f}c worse1c={100*ev(P+0.01, f/n):+.2f}c"
                print(f"  <= {w}s, depth>={m:2d} sh | " + " | ".join(cell(k) for k in ("all", "nonUS", "US")))
