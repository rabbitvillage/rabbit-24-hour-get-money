"""How many real entries per day with the old line vs the official line (FR-20261028-official-line). Read-only.

Per Taiwan day, markets outside the US session (09:00-17:00 New York, Mon-Fri, as the host .env pauses), replaying the two
live rules (w60_m95_s20 then w45_m95_s20; one real order per market) with
  old line = on-chain aggregator at open (what the bot used before 10-28 11:20)
  new line = official open on Coinbase's scale (historically: Coinbase TWAP of [open-60, open); live: Chainlink stream - gap)
and, for markets after the deployment, the line the bot actually used (sim_line.csv line_last) plus what it actually did
(sim_decisions.csv), as a check that the replay matches the live bot.

Usage (inside btc5m_sim/):  BOOK_GAP_MAX=0.05 python3 -I entry_rate_line.py data [deploy_start_epoch]
"""
import csv, os, sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from twap_vs_coinbase import load, start_of, twap_est
from replay_line_fix import book_rows, replay

NY = ZoneInfo("America/New_York"); TW = timezone(timedelta(hours=8))
RULES = [r for r in os.getenv("RULES", "w60_m95_s20,w45_m95_s20").split(",") if r]
WIN = {"w60_m95_s20": 60, "w45_m95_s20": 45}


def us_paused(start):
    t = datetime.fromtimestamp(start, NY)
    return t.weekday() < 5 and 9 <= t.hour < 17


def first_entry(rows, ref):
    for r in RULES:
        e = replay(rows, ref, WIN[r])
        if e:
            return (r,) + e
    return None


def main(d, deploy=None):
    meta, _ = load(d); book = book_rows(d); by_start = {start_of(s): s for s in meta}
    lines = {}
    p = os.path.join(d, "sim_line.csv")
    if os.path.exists(p):
        for r in csv.DictReader(open(p)):
            lines[r["slug"]] = r
    did = {}
    p = os.path.join(d, "sim_decisions.csv")
    if os.path.exists(p):
        for r in csv.DictReader(open(p)):
            if r["variant"] in RULES and r["decision"] == "buy":
                did[r["slug"]] = r["variant"]
    day = defaultdict(lambda: dict(mk=0, old=0, new=0, both=0, old_win=0, old_lose=0, new_win=0, new_lose=0))
    post = []
    for s, m in sorted(meta.items(), key=lambda kv: start_of(kv[0])):
        st = start_of(s)
        if us_paused(st):
            continue
        rows = book.get(s); pv = by_start.get(st - 300)
        if not rows or pv not in book:
            continue
        te = twap_est(book[pv], 5)
        if not te:
            continue
        old_ref = m["chain0"] if m["chain0"] is not None else m["spot0"]
        old = first_entry(rows, old_ref); new = first_entry(rows, te[0])
        k = datetime.fromtimestamp(st, TW).strftime("%m-%d"); c = day[k]
        c["mk"] += 1; c["old"] += bool(old); c["new"] += bool(new); c["both"] += bool(old and new)
        if old: c["old_win" if old[1] == m["winner"] else "old_lose"] += 1
        if new: c["new_win" if new[1] == m["winner"] else "new_lose"] += 1
        if deploy and st >= deploy and s in lines and lines[s].get("line_last"):
            live_line = float(lines[s]["line_last"]); act = first_entry(rows, live_line)
            post.append((s, old, new, act, did.get(s), m["winner"]))
    print(f"== {d}: non-US markets per Taiwan day; entries = markets where a live rule would buy (one order per market); gap filter {os.getenv('BOOK_GAP_MAX', '0')}")
    print(f"   {'day':<6}{'markets':>8}{'old line':>10}{'new line':>10}{'both':>6}   {'old W-L':>8}  {'new W-L':>8}")
    tot = defaultdict(int)
    for k in sorted(day):
        c = day[k]
        for f, v in c.items(): tot[f] += v
        print(f"   {k:<6}{c['mk']:>8}{c['old']:>10}{c['new']:>10}{c['both']:>6}   {c['old_win']:>4}-{c['old_lose']:<3}  {c['new_win']:>4}-{c['new_lose']:<3}")
    days = len(day)
    print(f"   {'total':<6}{tot['mk']:>8}{tot['old']:>10}{tot['new']:>10}{tot['both']:>6}   {tot['old_win']:>4}-{tot['old_lose']:<3}  {tot['new_win']:>4}-{tot['new_lose']:<3}")
    print(f"   per day: old {tot['old'] / days:.1f}, new {tot['new'] / days:.1f}  ->  {(tot['new'] / max(tot['old'], 1) - 1):+.0%}"
          f"   (a Taiwan day has {tot['mk'] / days:.0f} non-US markets here; full days have ~192)")
    if post:
        print(f"\n   since the deployment ({len(post)} non-US markets with the bot's own line):")
        print(f"   {'market':<26}{'old line':>10}{'new(proxy)':>11}{'bot line':>10}{'bot did':>14}{'winner':>8}")
        for s, old, new, act, didv, w in post:
            f = lambda e: "-" if not e else f"{e[1]}@{e[2]:.2f}"
            print(f"   {s:<26}{f(old):>10}{f(new):>11}{f(act):>10}{(didv or '-'):>14}{w:>8}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data", int(sys.argv[2]) if len(sys.argv) > 2 else None)
