"""Read-only structural checks on the host ledgers (+ data-api for the chain parts). Each part is independent."""
import csv, os, sys, time, statistics, collections, json
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

print("=== B. resolution latency (sim_trades resolved_after_s) and the cool-down gap")
for d in ("data", "data-sol"):
    ra = [f(r["resolved_after_s"]) for r in rows(f"{R}/{d}/sim_trades.csv") if f(r.get("resolved_after_s")) is not None]
    if ra: print(f"  {d}: n {len(ra)} median {med(ra):.0f}s p90 {sorted(ra)[int(0.9*len(ra))-1]:.0f}s >180s {pct(sum(x>180 for x in ra), len(ra)):.1f}% >240s {pct(sum(x>240 for x in ra), len(ra)):.1f}% max {max(ra):.0f}s")
# real losses: when were they booked vs the close, and did a real order go out in between?
for d in ("data", "data-sol"):
    orders = [o for o in rows(f"{R}/{d}/live_orders.csv") if o["mode"] == "live" and o["status"] in ("filled", "rejected", "unfilled", "aborted")]
    res = rows(f"{R}/{d}/live_results.csv")
    filled = {(o["variant"], o["slug"]) for o in orders if o["status"] == "filled"}
    gaps = []
    for r in res:
        if (r["variant"], r["slug"]) not in filled or r["won"] != "0": continue
        close = int(r["slug"].rsplit("-", 1)[1]) + 300
        booked = datetime.strptime(r["ts"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ).timestamp()
        between = [o for o in orders if close < datetime.strptime(o["ts"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ).timestamp() < booked]
        gaps.append((booked - close, len(between), r["slug"][-10:], [o["status"] for o in between]))
    if gaps:
        print(f"  {d}: {len(gaps)} real losses; booked after close median {med([g[0] for g in gaps]):.0f}s max {max(g[0] for g in gaps):.0f}s; "
              f"losses with a real order sent before the loss was booked: {sum(1 for g in gaps if g[1])} -> " + ", ".join(f"{g[2]} +{g[0]:.0f}s {g[3]}" for g in gaps if g[1]))

print("\n=== E. stale spot: longest run of an unchanged spot value inside the last 60 s (0.5 s rows)")
for d in ("data", "data-sol"):
    by = collections.defaultdict(list)
    for r in rows(f"{R}/{d}/sim_book.csv"):
        s = f(r["secs_left"]); sp = r.get("spot")
        if s is not None and s <= 60 and sp: by[r["slug"]].append((s, sp))
    longest = []
    for slug, pts in by.items():
        pts.sort(key=lambda x: -x[0]); run = best = 1
        for i in range(1, len(pts)):
            run = run + 1 if pts[i][1] == pts[i-1][1] else 1; best = max(best, run)
        longest.append(best * 0.5)
    if longest: print(f"  {d}: markets {len(longest)}; longest unchanged-spot run median {med(longest):.1f}s p90 {sorted(longest)[int(0.9*len(longest))-1]:.1f}s; >=3s in {pct(sum(x>=3 for x in longest), len(longest)):.1f}% of markets, >=10s in {pct(sum(x>=10 for x in longest), len(longest)):.1f}%")

print("\n=== F. distance source: Coinbase-vs-chain offset and which distance predicts the winner (w60/w45_m95_s20 shadow entries, BTC)")
ent = [r for r in rows(f"{R}/data/sim_variants_v2.csv") if r["variant"] in ("w60_m95_s20", "w45_m95_s20") and r.get("entered") == "1" and r.get("won") in ("0", "1")]
off_start = [f(r["spot_at_start"]) - f(r["chain_at_start"]) for r in ent if f(r.get("spot_at_start")) and f(r.get("chain_at_start"))]
ptb_err = [f(r["ptb_err"]) for r in ent if f(r.get("ptb_err")) is not None]
chain_err = [f(r["chain_ptb_err"]) for r in ent if f(r.get("chain_ptb_err")) is not None]
print(f"  entries {len(ent)}; Coinbase open - chain open: median {med(off_start):+.1f} USD, |.| p50 {med([abs(x) for x in off_start]):.1f} p90 {sorted(abs(x) for x in off_start)[int(0.9*len(off_start))-1]:.1f} max {max(abs(x) for x in off_start):.1f}")
print(f"  Coinbase open - official ptb: |.| p50 {med([abs(x) for x in ptb_err]):.1f} p90 {sorted(abs(x) for x in ptb_err)[int(0.9*len(ptb_err))-1]:.1f}; chain open - ptb: |.| p50 {med([abs(x) for x in chain_err]):.1f} p90 {sorted(abs(x) for x in chain_err)[int(0.9*len(chain_err))-1]:.1f}")
def passes(dl, side, thr=20.0): return dl is not None and abs(dl) >= thr and ((dl > 0) == (side == "Up"))
agree = collections.Counter()
for r in ent:
    sp, s0, c0, p0, side, won = f(r["spot_at_entry"]), f(r["spot_at_start"]), f(r["chain_at_start"]), f(r["price_to_beat"]), r["side"], r["won"] == "1"
    if sp is None: continue
    d_mixed = sp - c0 if c0 else (sp - s0 if s0 else None)
    d_cb = sp - s0 if s0 else None
    d_ptb = sp - p0 if p0 else None
    for name, dl in (("mixed(now-chain open)", d_mixed), ("coinbase(now-cb open)", d_cb), ("vs official ptb", d_ptb)):
        if dl is None: continue
        agree[(name, "pass")] += 1
        if passes(dl, side): agree[(name, "pass_ok")] += 1; agree[(name, "pass_ok_won")] += won
        else: agree[(name, "pass_fail")] += 1; agree[(name, "pass_fail_won")] += won
for name in ("mixed(now-chain open)", "coinbase(now-cb open)", "vs official ptb"):
    ok, okw, fl, flw = agree[(name, "pass_ok")], agree[(name, "pass_ok_won")], agree[(name, "pass_fail")], agree[(name, "pass_fail_won")]
    print(f"  {name:24s}: filter passes {ok:3d} (won {okw}, lost {ok-okw}); would have blocked {fl:3d} (won {flw}, lost {fl-flw})")

print("\n=== 3. rejected/error messages vs review.py HARD_ERRORS (restricted/signer/unauthorized/invalid/forbidden)")
HARD = ("restricted", "signer", "unauthorized", "invalid", "forbidden")
for d in ("data", "data-sol"):
    rej = [o for o in rows(f"{R}/{d}/live_orders.csv") if o["mode"] == "live" and o["status"] in ("rejected", "error")]
    hard = [o for o in rej if any(h in (o.get("msg") or "").lower() for h in HARD)]
    kinds = collections.Counter((o.get("msg") or "")[:70] for o in rej)
    print(f"  {d}: rejected/error {len(rej)}, matching HARD_ERRORS {len(hard)}; top messages: " + " | ".join(f"{k!r} x{v}" for k, v in kinds.most_common(3)))

print("\n=== A/N. chain vs ledger (data-api): winners vs REDEEM, and rejected orders that filled anyway")
try:
    from btc5m_sim import live as L
    from polyaudit_lib import fetch_activity
    W = L.identity()["proxyWallet"]
    acts = fetch_activity(W, stop_before=time.time() - 8 * 86400)
    trades = [a for a in acts if a.get("type") == "TRADE" and a.get("side") == "BUY"]
    redeems = collections.defaultdict(float)
    for a in acts:
        if a.get("type") == "REDEEM": redeems[a["slug"]] += float(a.get("size") or 0)
    print(f"  activity rows {len(acts)} (8 days): buys {len(trades)}, redeem markets {len(redeems)}")
    for d in ("data", "data-sol", "data-eth"):
        orders = [o for o in rows(f"{R}/{d}/live_orders.csv") if o["mode"] == "live" and o["status"] != "skipped"]
        res = {(r["variant"], r["slug"]): r for r in rows(f"{R}/{d}/live_results.csv")}
        mism = []; checked = 0
        for o in orders:
            if o["status"] != "filled": continue
            r = res.get((o["variant"], o["slug"]))
            if not r or r.get("won") not in ("0", "1"): continue
            t = datetime.strptime(o["ts"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ).timestamp()
            if t < time.time() - 8 * 86400: continue
            checked += 1
            got = redeems.get(o["slug"], 0.0) > 0
            if (r["won"] == "1") != got: mism.append((o["ts"], o["variant"], o["outcome"], r["won"], round(redeems.get(o["slug"], 0.0), 2)))
        print(f"  {d}: winners checked {checked}, ledger-vs-chain mismatches {len(mism)}" + (": " + str(mism[:5]) if mism else ""))
        # rejected/unfilled/error orders with an on-chain buy of the same slug+outcome within 120 s not explained by a filled order
        filled_keys = {(o["slug"], o["outcome"]) for o in orders if o["status"] == "filled"}
        ghost = []
        for o in orders:
            if o["status"] not in ("rejected", "unfilled", "error", "aborted"): continue
            t = datetime.strptime(o["ts"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ).timestamp()
            if t < time.time() - 8 * 86400 or (o["slug"], o["outcome"]) in filled_keys: continue
            hit = [a for a in trades if a.get("slug") == o["slug"] and a.get("outcome") == o["outcome"] and abs(a["timestamp"] - t) <= 120]
            if hit: ghost.append((o["ts"], o["variant"], o["status"], o["outcome"], [(round(a["usdcSize"], 2), a["price"]) for a in hit][:3]))
        print(f"  {d}: rejected/unfilled/error orders with an unexplained on-chain buy nearby: {len(ghost)}" + (": " + str(ghost[:5]) if ghost else ""))
except Exception as e:
    print("  chain part failed:", type(e).__name__, str(e)[:120])
