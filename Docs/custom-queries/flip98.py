"""Read-only: how often does the leader flip after its ask reaches 0.98-0.99 in the last seconds?
For every settled market: the first moment (per window) the leader's best ask is 0.98-0.99 with shares on it, as if
bought there; counts flips (that side lost) and P&L per share before fees; split US-session / other hours.
Usage:  python3 -I Docs/custom-queries/flip98.py btc5m_sim/data [btc5m_sim/data-eth ...]
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
def us_session(start_utc):
    t = ny(datetime.fromisoformat(start_utc))
    return t.weekday() < 5 and 9 * 60 + 30 <= t.hour * 60 + t.minute < 16 * 60
WINDOWS = (90, 60, 45, 30, 15)
for d in sys.argv[1:]:
    win, start = {}, {}
    for r in csv.DictReader(open(d + "/sim_trades.csv")):
        if r["winner"] in ("Up", "Down"): win[r["slug"]] = r["winner"]; start[r["slug"]] = r["start_utc"]
    first = {}
    for r in csv.DictReader(open(d + "/sim_book.csv")):
        s = r["slug"]
        if s not in win: continue
        try: secs = float(r["secs_left"])
        except ValueError: continue
        for side, k in (("Up", "up"), ("Down", "down")):
            try: a = float(r[k + "_ask"]); sh = float(r[k + "_sh"] or 0)
            except ValueError: continue
            if 0.98 <= a <= 0.99 and sh > 0:
                for w in WINDOWS:
                    if secs <= w and (s, w) not in first: first[(s, w)] = (side, a, secs)
    print("=====", d, "markets with result", len(win))
    for w in WINDOWS:
        tot = collections.Counter(); lost = collections.Counter(); pnl = collections.Counter()
        for (s, ww), (side, a, secs) in first.items():
            if ww != w: continue
            for key in ("all", "US" if us_session(start[s]) else "non-US"):
                tot[key] += 1
                if side != win[s]: lost[key] += 1; pnl[key] -= a
                else: pnl[key] += 1 - a
        print(f"window<={w}s " + "  ".join(f"{k}: n={tot[k]} flip={lost[k]} ({100*lost[k]/tot[k]:.2f}%) pnl/share={pnl[k]:+.2f}" for k in ("all", "non-US", "US") if tot[k]))
    for (s, ww), (side, a, secs) in sorted(first.items()):
        if ww == 45 and side != win[s]:
            print("  flip", start[s], s, side, a, f"{secs:.0f}s", "US" if us_session(start[s]) else "")
