"""Read-only, part 2: chain vs ledger (winners vs REDEEM; rejected orders that filled anyway), stale spot at the decision
of real orders, and the distance filter split into direction vs magnitude."""
import csv, os, sys, time, statistics, collections
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.path.join(os.getenv("REPO", "/opt/rabbit-24-hour-get-money"), "Docs", "custom-queries")); sys.path.insert(0, os.getenv("REPO", "/opt/rabbit-24-hour-get-money"))
TZ = timezone(timedelta(hours=8))
R = os.path.join(os.getenv("REPO", "/opt/rabbit-24-hour-get-money"), "btc5m_sim")
def f(v):
    try: return float(v)
    except (TypeError, ValueError): return None
def med(xs): return statistics.median(xs) if xs else float("nan")
def pct(a, b): return 100.0 * a / b if b else float("nan")
def rows(path): return list(csv.DictReader(open(path))) if os.path.exists(path) else []
def ts(s): return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ).timestamp()

print("=== E2. real filled orders: was the spot value unchanged for >= 3 s / >= 10 s at the decision? win rate by that")
for d in ("data", "data-sol"):
    orders = [o for o in rows(f"{R}/{d}/live_orders.csv") if o["mode"] == "live" and o["status"] == "filled"]
    res = {(r["variant"], r["slug"]): r for r in rows(f"{R}/{d}/live_results.csv")}
    need = {o["slug"] for o in orders}
    book = collections.defaultdict(list)
    for r in rows(f"{R}/{d}/sim_book.csv"):
        if r["slug"] in need and r.get("spot"): book[r["slug"]].append((f(r["secs_left"]), r["spot"]))
    buckets = collections.defaultdict(lambda: [0, 0, 0.0])
    for o in orders:
        r = res.get((o["variant"], o["slug"]))
        if not r or r.get("won") not in ("0", "1") or not book.get(o["slug"]): continue
        pts = sorted(book[o["slug"]], key=lambda x: -x[0]); secs = f(o["secs_left"]) or 0
        idx = min(range(len(pts)), key=lambda i: abs(pts[i][0] - secs))
        run = 1
        while idx - run >= 0 and pts[idx - run][1] == pts[idx][1]: run += 1
        stale = run * 0.5
        b = ">=10s" if stale >= 10 else (">=3s" if stale >= 3 else "<3s")
        buckets[b][0] += 1; buckets[b][1] += r["won"] == "1"; buckets[b][2] += f(r["pnl"]) or 0
    print(f"  {d}: " + "; ".join(f"{k}: n {v[0]} won {v[1]} lost {v[0]-v[1]} pnl {v[2]:+.2f}" for k, v in sorted(buckets.items())))

print("\n=== F2. distance filter (w60/w45_m95_s20 shadow entries, BTC): direction vs magnitude under the Coinbase-consistent measure")
ent = [r for r in rows(f"{R}/data/sim_variants_v2.csv") if r["variant"] in ("w60_m95_s20", "w45_m95_s20") and r.get("entered") == "1" and r.get("won") in ("0", "1")]
c = collections.Counter()
for r in ent:
    sp, s0, side, won = f(r["spot_at_entry"]), f(r["spot_at_start"]), r["side"], r["won"] == "1"
    if sp is None or not s0: continue
    d = sp - s0; right = (d > 0) == (side == "Up")
    k = "wrong direction" if not right else ("right, |d|<20" if abs(d) < 20 else "right, |d|>=20")
    c[(k, "n")] += 1; c[(k, "won")] += won
for k in ("right, |d|>=20", "right, |d|<20", "wrong direction"):
    print(f"  {k:18s}: n {c[(k,'n')]:3d} won {c[(k,'won')]:3d} lost {c[(k,'n')]-c[(k,'won')]:2d} loss rate {pct(c[(k,'n')]-c[(k,'won')], c[(k,'n')]):.1f}%")

print("\n=== A/N. chain vs ledger (data-api, last 8 days)")
from btc5m_sim import live as L
from polyaudit_lib import fetch_activity
W = L.identity()["proxyWallet"]
acts = fetch_activity(W, stop_before=time.time() - 8 * 86400)
trades = [a for a in acts if a.get("type") == "TRADE" and a.get("side") == "BUY"]
redeems = collections.defaultdict(float)
for a in acts:
    if a.get("type") == "REDEEM": redeems[a["slug"]] += float(a.get("size") or 0)
print(f"  activity rows {len(acts)}: buys {len(trades)}, markets with a redeem {len(redeems)}")
for d in ("data", "data-sol", "data-eth"):
    orders = [o for o in rows(f"{R}/{d}/live_orders.csv") if o["mode"] == "live" and o["status"] != "skipped"]
    res = {(r["variant"], r["slug"]): r for r in rows(f"{R}/{d}/live_results.csv")}
    mism = []; checked = 0
    for o in orders:
        if o["status"] != "filled": continue
        r = res.get((o["variant"], o["slug"]))
        if not r or r.get("won") not in ("0", "1") or ts(o["ts"]) < time.time() - 8 * 86400: continue
        checked += 1
        if (r["won"] == "1") != (redeems.get(o["slug"], 0.0) > 0): mism.append((o["ts"], o["variant"], o["outcome"], "won" if r["won"] == "1" else "lost", round(redeems.get(o["slug"], 0.0), 2)))
    print(f"  {d}: winners checked {checked}, ledger-vs-chain mismatches {len(mism)}" + (": " + str(mism[:6]) if mism else ""))
    filled_keys = {(o["slug"], o["outcome"]) for o in orders if o["status"] == "filled"}
    ghost = []
    for o in orders:
        if o["status"] not in ("rejected", "unfilled", "error", "aborted") or (o["slug"], o["outcome"]) in filled_keys or ts(o["ts"]) < time.time() - 8 * 86400: continue
        hit = [a for a in trades if a.get("slug") == o["slug"] and a.get("outcome") == o["outcome"] and abs(a["timestamp"] - ts(o["ts"])) <= 120]
        if hit: ghost.append((o["ts"], o["variant"], o["status"], o["outcome"], [(round(float(a["usdcSize"]), 2), a["price"]) for a in hit][:3]))
    print(f"  {d}: rejected/unfilled/error orders with an unexplained on-chain buy within 120 s: {len(ghost)}" + (": " + str(ghost[:6]) if ghost else ""))
