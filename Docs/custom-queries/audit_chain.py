"""Read-only: the trading wallet's on-chain record vs the bot's ledgers.
  1. fee actually paid on the bot's fills (the ledger books fee = 0) and the ledger P&L after fees
  2. manual crypto trades (not explained by any bot order): P&L by day and by average buy price
Usage (on the host, data in <repo>/btc5m_sim/data*):
  python3 -I Docs/custom-queries/audit_chain.py <proxy-wallet-address> <repo-root> [max-rows=6000]
"""
import sys, os, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from polyaudit_lib import *

W, ROOT = sys.argv[1], sys.argv[2]; MAXROWS = int(sys.argv[3]) if len(sys.argv) > 3 else 6000
acts = fetch_activity(W, max_rows=MAXROWS)
orders = load_bot_orders(ROOT); winner = load_winners(ROOT); results = load_results(ROOT)
print("activity rows", len(acts), "oldest", datetime.fromtimestamp(min(a["timestamp"] for a in acts), TZ).strftime("%Y-%m-%d %H:%M"))
bot_fills, manual, unmatched = attribute(acts, orders)

# --- 1. fees on the bot's fills
fee = collections.Counter(); seen = set()
for a, o in bot_fills:
    fee["usdc"] += fee_of(a); seen.add(o["key"])
ledger = sum(results.get(k, 0.0) for k in seen)
print(f"\nbot filled orders {len(orders)}, explained on chain {len(seen)} ({len(bot_fills)} chain rows), not found on chain {len(unmatched)}")
print(f"fee paid on those fills ${fee['usdc']:.2f}; ledger pnl of the same orders {ledger:+.2f} -> after fees {ledger - fee['usdc']:+.2f}")
if unmatched:
    print("  not found (outside the fetched rows?):", ", ".join(f"{o['dir']} {o['variant']} {o['slug'][-10:]} {datetime.fromtimestamp(o['ts'], TZ):%m-%d %H:%M}" for o in unmatched[:8]))

# --- 2. manual crypto trades
mk = {}
for a in manual:
    m = mk.setdefault(a["slug"], dict(buy=0.0, sell=0.0, sh=collections.Counter(), first=a["timestamp"], prices=[], fee=0.0))
    u, s, p = float(a["usdcSize"]), float(a["size"]), float(a["price"])
    m["first"] = min(m["first"], a["timestamp"])
    if a["side"] == "BUY": m["buy"] += u; m["sh"][a["outcome"]] += s; m["prices"].append((p, s)); m["fee"] += u - s * p
    else: m["sell"] += u; m["sh"][a["outcome"]] -= s
redeem = collections.defaultdict(float)
for a in acts:
    if a.get("type") == "REDEEM": redeem[a["slug"]] += float(a["usdcSize"])
bot_payout = collections.defaultdict(float)
for o in orders:
    if winner.get(o["slug"]) == o["outcome"]: bot_payout[o["slug"]] += o["size"]
days = collections.defaultdict(collections.Counter); buckets = collections.defaultdict(collections.Counter); worst = []
for slug, m in mk.items():
    if not m["prices"]: continue
    w = winner.get(slug)
    payout = (m["sh"][w] if m["sh"][w] > 0 else 0.0) if w else max(0.0, redeem.get(slug, 0.0) - bot_payout.get(slug, 0.0))
    pnl = m["sell"] + payout - m["buy"]
    avgp = sum(p * s for p, s in m["prices"]) / sum(s for p, s in m["prices"])
    day = datetime.fromtimestamp(m["first"], TZ).strftime("%m-%d")
    for c, k in ((days[day], None), (buckets["<0.80" if avgp < 0.80 else "0.80-0.89" if avgp < 0.90 else "0.90-0.95" if avgp < 0.96 else "0.96-0.97" if avgp < 0.98 else "0.98" if avgp < 0.99 else "0.99+"], None)):
        c["n"] += 1; c["pnl"] += pnl; c["buy"] += m["buy"]; c["fee"] += m["fee"]; c["win"] += pnl > 0
    worst.append((pnl, datetime.fromtimestamp(m["first"], TZ).strftime("%m-%d %H:%M"), slug, avgp, m["buy"], w))
print(f"\nmanual crypto trade rows {len(manual)} in {len(mk)} markets")
print("### by day (Taiwan time): P&L = sells + payout - buys (fees already inside the buys)")
cum = 0.0
for day in sorted(days):
    x = days[day]; cum += x["pnl"]
    print(f"{day}: markets={x['n']:3d} spent=${x['buy']:7.0f} fee=${x['fee']:5.2f} won={x['win']:3d} pnl={x['pnl']:+8.2f} cumulative={cum:+8.2f}")
print("### by average buy price")
for b in ("<0.80", "0.80-0.89", "0.90-0.95", "0.96-0.97", "0.98", "0.99+"):
    if b in buckets:
        x = buckets[b]; print(f"{b:>9}: markets={x['n']:3d} won={x['win']:3d} ({100*x['win']/x['n']:.0f}%) spent=${x['buy']:7.0f} fee=${x['fee']:5.2f} pnl={x['pnl']:+8.2f} ret={100*x['pnl']/x['buy']:+.1f}%")
print("### 10 biggest manual losses")
for pnl, t, slug, avgp, buy, w in sorted(worst)[:10]:
    print(f"{t} {slug} avg {avgp:.2f} spent {buy:.0f} pnl {pnl:+.2f} winner {w}")
