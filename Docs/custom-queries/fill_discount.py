"""Read-only: real filled orders bucketed by (fill avg - ask seen): are big discounts adverse (the side collapsing)?
Also a proxy for a 'pre-post re-check': the bot's own book row ~0.9 s after the decision (sim_book.csv, 0.5 s rows) --
how far had the side's best ask moved by the time the order would have been posted, and how did those orders end."""
import csv, os, collections
from datetime import datetime, timezone, timedelta
TZ = timezone(timedelta(hours=8))
os.chdir(os.path.join(os.getenv("REPO", "/opt/rabbit-24-hour-get-money"), "btc5m_sim"))
def f(v):
    try: return float(v)
    except (TypeError, ValueError): return None
out = []
for d in ("data", "data-sol", "data-eth"):
    if not os.path.exists(f"{d}/live_results.csv"): continue
    orders = {(o["variant"], o["slug"]): o for o in csv.DictReader(open(f"{d}/live_orders.csv")) if o["mode"] == "live" and o["status"] == "filled"}
    need = collections.defaultdict(list)
    for r in csv.DictReader(open(f"{d}/live_results.csv")):
        o = orders.get((r["variant"], r["slug"]))
        if not o or r["won"] not in ("0", "1") or not f(r["size"]): continue
        fill, ask = f(r["cost"]) / f(r["size"]), f(o["ask"])
        rec = dict(d=d, ts=o["ts"], var=r["variant"], slug=r["slug"], side=o["outcome"], ask=ask, fill=fill, disc=fill - ask, won=r["won"] == "1", pnl=f(r["pnl"]) or 0.0, secs=f(o["secs_left"]), ask_after=None)
        out.append(rec); need[r["slug"]].append(rec)
    rows = collections.defaultdict(list)
    for b in csv.DictReader(open(f"{d}/sim_book.csv")):
        if b["slug"] in need:
            rows[b["slug"]].append((f(b["secs_left"]), f(b["up_ask"]), f(b["down_ask"])))
    for slug, recs in need.items():
        for rec in recs:
            if rec["secs"] is None or not rows.get(slug): continue
            target = rec["secs"] - 0.9
            sec, ua, da = min(rows[slug], key=lambda x: abs((x[0] or 0) - target))
            if sec is not None and abs(sec - target) <= 0.6:
                a = ua if rec["side"] == "Up" else da
                rec["ask_after"] = a
print(f"real filled+settled orders: {len(out)}")
B = [(-9, -0.15, "fill <= ask-0.15"), (-0.15, -0.05, "ask-0.15..-0.05"), (-0.05, -0.004, "ask-0.05..-0.01"), (-0.004, 0.004, "= ask"), (0.004, 9, "> ask")]
print("\nby fill - ask seen:")
for lo, hi, name in B:
    s = [x for x in out if lo < x["disc"] <= hi]
    if s: print(f"  {name:18s} n {len(s):3d} won {sum(x['won'] for x in s):3d} lost {sum(not x['won'] for x in s):2d} pnl {sum(x['pnl'] for x in s):+7.2f}")
print("\nby the side's best ask ~0.9 s after the decision minus ask seen (the bot's own 0.5 s book rows):")
have = [x for x in out if x["ask_after"] is not None]
print(f"  rows with a book sample: {len(have)}")
for lo, hi, name in B:
    s = [x for x in have if lo < (x["ask_after"] - x["ask"]) <= hi]
    if s: print(f"  {name:18s} n {len(s):3d} won {sum(x['won'] for x in s):3d} lost {sum(not x['won'] for x in s):2d} pnl {sum(x['pnl'] for x in s):+7.2f}")
print("\nfills at least 0.15 below the ask seen:")
for x in sorted(out, key=lambda x: x["ts"]):
    if x["disc"] <= -0.15: print(f"  {x['ts']} {x['d']:8s} {x['var']:12s} {x['side']:4s} ask {x['ask']} fill {x['fill']:.3f} ask~0.9s-later {x['ask_after']} {'WIN' if x['won'] else 'LOSS'} {x['pnl']:+.2f}")
