"""Read-only: was today a good day for the market, or a normal one?  Per Taiwan day and per asset, two windows: the
same clock hours (default 05:00-16:30 Taiwan, i.e. the non-US hours a partial day has already covered) and all non-US hours.
For each: markets recorded, median |move| at the close (bps from price_to_beat), median spot range in the last 150 s,
how often the leader at -60 s lost ("flip60"), how often the bot's setup existed (leader ask 0.80-0.95 at -60 s) and how
often THAT flipped, the live rule's shadow ledger (entered / won / lost / return per $1 of cost, before fees), and the
real orders of the day (sent = filled+rejected+unfilled, filled, won, lost, gross pnl).  Nothing here is a signal.
Usage (on the host):  cd /opt/rabbit-24-hour-get-money && python3 -I Docs/custom-queries/day_regime.py [HH:MM-HH:MM] [rule]
  window defaults to 05:00-16:30; rule defaults to the first name in LIVE_VARIANTS (source btc5m_sim/.env) or w60_m95_s20.
"""
import csv, os, sys, collections, statistics
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
TZ = timezone(timedelta(hours=8)); NY = ZoneInfo("America/New_York")
ROOT = os.path.join(os.getcwd(), "btc5m_sim")
win = next((a for a in sys.argv[1:] if "-" in a and ":" in a), "05:00-16:30")
H0, H1 = [int(x[:2]) * 60 + int(x[3:5]) for x in win.split("-")]
RULE = next((a for a in sys.argv[1:] if a.startswith("w")), (os.getenv("LIVE_VARIANTS", "w60_m95_s20").split(",")[0]).strip())

US0, US1 = os.getenv("US_SESSION_START", "09:00"), os.getenv("US_SESSION_END", "17:00")   # the host .env pauses 09:00-17:00 New York
def us_open(ts):
    t = datetime.fromtimestamp(ts, NY)
    return t.weekday() < 5 and US0 <= t.strftime("%H:%M") < US1
def tw(ts): return datetime.fromtimestamp(ts, TZ)
def same_hours(ts):
    t = tw(ts); m = t.hour * 60 + t.minute
    return H0 <= m < H1
def f(v):
    try: return float(v)
    except (TypeError, ValueError): return None
def med(xs): return statistics.median(xs) if xs else float("nan")
def pct(a, b): return 100.0 * a / b if b else float("nan")

for d in ("data", "data-sol", "data-eth"):
    base = os.path.join(ROOT, d)
    if not os.path.exists(os.path.join(base, "sim_book.csv")): continue
    orders = [o for o in csv.DictReader(open(os.path.join(base, "live_orders.csv"))) if o["mode"] == "live" and o["status"] in ("filled", "rejected", "unfilled")] if os.path.exists(os.path.join(base, "live_orders.csv")) else []
    res = {(r["variant"], r["slug"]): r for r in csv.DictReader(open(os.path.join(base, "live_results.csv")))} if os.path.exists(os.path.join(base, "live_results.csv")) else {}
    winner, ptb, rule_rows = {}, {}, []
    for name in ("sim_variants.csv", "sim_variants_v2.csv"):           # BTC moved to the v2 file on 10-20; eth/sol were born v2
        p = os.path.join(base, name)
        if not os.path.exists(p): continue
        for r in csv.DictReader(open(p)):
            if r.get("winner"): winner[r["slug"]] = r["winner"]
            if f(r.get("price_to_beat")): ptb[r["slug"]] = f(r["price_to_beat"])
            if r["variant"] == RULE and r.get("entered") == "1" and r.get("won") in ("0", "1"): rule_rows.append(r)
    if os.path.exists(os.path.join(base, "sim_trades.csv")):
        for r in csv.DictReader(open(os.path.join(base, "sim_trades.csv"))):
            if r.get("winner"): winner.setdefault(r["slug"], r["winner"])
            if f(r.get("price_to_beat")): ptb.setdefault(r["slug"], f(r["price_to_beat"]))
    book = collections.defaultdict(list)
    for r in csv.DictReader(open(os.path.join(base, "sim_book.csv"))):
        s = f(r["secs_left"])
        if s is not None: book[r["slug"]].append((s, f(r["up_ask"]), f(r["down_ask"]), f(r["spot"])))
    mk = {}
    for slug, rows in book.items():
        rows.sort(key=lambda x: -x[0]); leader = None; flips = 0; lead60 = None; ask60 = None; last_spot = None; spots = []
        for secs, ua, da, sp in rows:
            pu = ua if ua is not None else (1 - da if da is not None else None)
            if pu is not None:
                L = "Up" if pu > 0.5 else "Down"
                if leader and L != leader: flips += 1
                leader = L
                if lead60 is None and secs <= 60:
                    lead60 = L; ask60 = (ua if ua is not None else 1 - da) if L == "Up" else (da if da is not None else 1 - ua)
            if sp: last_spot = sp; spots.append(sp)
        start = int(slug.rsplit("-", 1)[1]); p0 = ptb.get(slug)
        mk[slug] = dict(start=start, flips=flips, lead60=lead60, ask60=ask60, w=winner.get(slug),
                        move=(last_spot - p0) / p0 * 1e4 if last_spot and p0 else None,
                        rng=(max(spots) - min(spots)) / p0 * 1e4 if spots and p0 else None)
    days = sorted({tw(m["start"]).date() for m in mk.values()})
    print(f"\n=== {d}  rule {RULE}  window {win} ===")
    print("day        win   n  med|move| rng  flip60  setup% setupflip | shadow n won lost ret/$1 | real sent fill won lost  pnl")
    for day in days:
        for label, cond in ((win[:5], lambda ts: tw(ts).date() == day and same_hours(ts)), ("nonUS", lambda ts: tw(ts).date() == day and not us_open(ts))):
            ms = [m for m in mk.values() if cond(m["start"])]
            if not ms: continue
            mv = [abs(m["move"]) for m in ms if m["move"] is not None]; rg = [m["rng"] for m in ms if m["rng"] is not None]
            judged = [m for m in ms if m["lead60"] and m["w"]]
            fl = sum(m["lead60"] != m["w"] for m in judged)
            setup = [m for m in judged if m["ask60"] is not None and 0.80 <= m["ask60"] <= 0.95]
            sfl = sum(m["lead60"] != m["w"] for m in setup)
            rr = [r for r in rule_rows if cond(int(r["slug"].rsplit("-", 1)[1]))]
            rw = sum(r["won"] == "1" for r in rr); rl = len(rr) - rw
            ret = sum(((1 - f(r["ask"])) / f(r["ask"])) if r["won"] == "1" else -1.0 for r in rr if f(r["ask"]))
            oo = [o for o in orders if cond(datetime.strptime(o["ts"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ).timestamp())]
            fo = [o for o in oo if o["status"] == "filled"]
            won = lost = 0; pnl = 0.0
            for o in fo:
                r = res.get((o["variant"], o["slug"]))
                if r and r.get("won") in ("0", "1"): won += r["won"] == "1"; lost += r["won"] == "0"; pnl += f(r["pnl"]) or 0
            print(f"{day} {label:5s} {len(ms):4d}  {med(mv):6.1f} {med(rg):5.1f}  {pct(fl, len(judged)):5.1f}%  {pct(len(setup), len(judged)):5.1f}%  {pct(sfl, len(setup)):5.1f}% | "
                  f"{len(rr):3d} {rw:3d} {rl:3d} {pct(ret, len(rr)):+6.1f}% | {len(oo):3d} {len(fo):3d} {won:3d} {lost:3d} {pnl:+6.2f}")
