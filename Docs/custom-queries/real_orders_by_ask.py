"""Read-only: the bot's real (filled and settled) orders grouped by the ask it saw when it decided.
Per asset and ask price: orders, wins, losses, ledger P&L, cost, return on cost, and the average of
(fill price - quoted ask). Ledger P&L excludes the taker fee (see audit_chain.py for the fee).
Usage:  python3 -I Docs/custom-queries/real_orders_by_ask.py btc5m_sim/data [btc5m_sim/data-sol ...]
"""
import csv, sys, os, collections
for d in sys.argv[1:]:
    res = list(csv.DictReader(open(os.path.join(d, "live_results.csv"))))
    orders = list(csv.DictReader(open(os.path.join(d, "live_orders.csv"))))
    askof = {(o["variant"], o["slug"]): float(o["ask"]) for o in orders if o["status"] == "filled"}
    b = collections.defaultdict(lambda: dict(n=0, w=0, pnl=0.0, cost=0.0, fill=[]))
    for r in res:
        if r["won"] not in ("0", "1"): continue
        a = askof.get((r["variant"], r["slug"]))
        if a is None: continue
        size, cost = float(r["size"]), float(r["cost"])
        x = b[f"{a:.2f}"]; x["n"] += 1; x["w"] += int(r["won"]); x["pnl"] += float(r["pnl"]); x["cost"] += cost; x["fill"].append(cost / size - a if size else 0)
    print("==", d)
    for key in sorted(b):
        x = b[key]; n = x["n"]
        print(f"ask {key}: n={n:3d} win={x['w']:3d} ({100*x['w']/n:5.1f}%) lose={n-x['w']:2d} pnl={x['pnl']:+8.2f} cost={x['cost']:6.0f} ret={100*x['pnl']/x['cost']:+6.2f}% avg(fill-ask)={sum(x['fill'])/len(x['fill']):+.4f}")
