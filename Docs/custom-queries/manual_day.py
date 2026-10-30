"""Read-only: one day of manual trading, market by market, next to what the market was doing.
For every market the wallet traded by hand that day (trades not explained by a bot order): buys, sells, payout, P&L,
average buy price, favourite or underdog, sold early or held to settlement, and from the bot's own recordings the
market's move at the close (bps from price_to_beat) and how many times the leader changed in the last 150 s.
Usage:
  python3 -I Docs/custom-queries/manual_day.py <proxy-wallet> <repo-root> <YYYY-MM-DD Taiwan> [split HH:MM] [asset=btc]
"""
import sys, os, csv, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from polyaudit_lib import *

W, ROOT, DAY = sys.argv[1], sys.argv[2], sys.argv[3]
SPLIT = sys.argv[4] if len(sys.argv) > 4 and ":" in sys.argv[4] else None
ASSET = (sys.argv[5] if len(sys.argv) > 5 else "btc").lower()
d0 = datetime.strptime(DAY, "%Y-%m-%d").replace(tzinfo=TZ); t0, t1 = d0.timestamp(), d0.timestamp() + 86400
tsplit = datetime.strptime(f"{DAY} {SPLIT}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ).timestamp() if SPLIT else None

acts = fetch_activity(W, stop_before=t0 - 3600)
orders = load_bot_orders(ROOT); winner = load_winners(ROOT)
bot_fills, manual, _ = attribute(acts, orders)
redeem = collections.defaultdict(float)
for a in acts:
    if a.get("type") == "REDEEM": redeem[a["slug"]] += float(a["usdcSize"])

# --- market conditions from the bot's recordings (btc5m_sim/data*/sim_trades.csv + sim_book.csv)
ddir = os.path.join(ROOT, "btc5m_sim", "data" if ASSET == "btc" else f"data-{ASSET}")
ptb, start = {}, {}
for r in csv.DictReader(open(os.path.join(ddir, "sim_trades.csv"))):
    s = r["slug"]; ts = int(s.rsplit("-", 1)[1])
    if t0 - 300 <= ts < t1 and r["price_to_beat"]:
        ptb[s] = float(r["price_to_beat"]); start[s] = ts
book = collections.defaultdict(list)
for r in csv.DictReader(open(os.path.join(ddir, "sim_book.csv"))):
    if r["slug"] in start:
        try: book[r["slug"]].append((float(r["secs_left"]), r["up_ask"], r["down_ask"], r["spot"]))
        except ValueError: pass
cond = {}
for s, rows in book.items():
    rows.sort(key=lambda x: -x[0]); leader = None; flips = 0; lead120 = None; last_spot = None
    for secs, ua, da, spot in rows:
        pu = float(ua) if ua else (1 - float(da) if da else None)
        if pu is not None:
            L = "Up" if pu > 0.5 else "Down"
            if leader and L != leader: flips += 1
            leader = L
            if lead120 is None and secs <= 120: lead120 = L
        if spot: last_spot = float(spot)
    move = (last_spot - ptb[s]) / ptb[s] * 1e4 if last_spot and ptb.get(s) else None
    cond[s] = dict(flips=flips, lead120_won=(lead120 == winner.get(s)) if lead120 and winner.get(s) else None, move=move)

# --- the manual markets
mk = {}
for a in manual:
    if not (t0 <= a["timestamp"] < t1) or not a["slug"].startswith(ASSET + "-"): continue
    m = mk.setdefault(a["slug"], dict(buy=0.0, sell=0.0, sh=collections.Counter(), prices=[], n=0, first=a["timestamp"], last=a["timestamp"], fee=0.0))
    u, s, p = float(a["usdcSize"]), float(a["size"]), float(a["price"]); m["n"] += 1
    m["first"] = min(m["first"], a["timestamp"]); m["last"] = max(m["last"], a["timestamp"])
    if a["side"] == "BUY": m["buy"] += u; m["sh"][a["outcome"]] += s; m["prices"].append((p, s, a["timestamp"])); m["fee"] += u - s * p
    else: m["sell"] += u; m["sh"][a["outcome"]] -= s
rows = []
for s, m in mk.items():
    w = winner.get(s); held = sum(v for v in m["sh"].values() if v > 0.01)
    payout = (m["sh"][w] if m["sh"][w] > 0 else 0.0) if w else redeem.get(s, 0.0)
    pnl = m["sell"] + payout - m["buy"]
    avgp = sum(p * q for p, q, _ in m["prices"]) / sum(q for p, q, _ in m["prices"]) if m["prices"] else 0
    first_buy = min(m["prices"], key=lambda x: x[2]) if m["prices"] else None
    rows.append(dict(t=m["last"], slug=s, avgp=avgp, buy=m["buy"], sell=m["sell"], payout=payout, pnl=pnl, winner=w, n=m["n"], fee=m["fee"],
                     fav=(first_buy[0] >= 0.5) if first_buy else None, held=held > 0.01, both=len(m["sh"]) > 1,
                     entry_secs=(int(s.rsplit("-", 1)[1]) + 300 - first_buy[2]) if first_buy else None, **cond.get(s, dict(flips=None, lead120_won=None, move=None))))
rows.sort(key=lambda r: r["t"])
print(f"{DAY} manual {ASSET.upper()} markets: {len(rows)}; spent ${sum(r['buy'] for r in rows):.0f}; fees on buys ${sum(r['fee'] for r in rows):.2f}")
print("close(TW) slug         avgbuy  spent   sold payout    pnl      cum  win  fav held both entry_s flips lead120 move_bps")
cum = 0.0; peak = (0.0, "")
for r in rows:
    cum += r["pnl"]
    if cum > peak[0]: peak = (cum, datetime.fromtimestamp(r["t"], TZ).strftime("%H:%M"))
    move = "" if r["move"] is None else f"{r['move']:+.0f}"
    lead = "" if r["lead120_won"] is None else ("Y" if r["lead120_won"] else "N")
    print(f"{datetime.fromtimestamp(r['t'], TZ):%H:%M} {r['slug'][-10:]} {r['avgp']:6.2f} {r['buy']:6.1f} {r['sell']:6.1f} {r['payout']:6.1f} {r['pnl']:+7.2f} {cum:+8.2f}  {str(r['winner'])[:4]:4s} "
          f"{'fav' if r['fav'] else 'dog':3s} {'Y' if r['held'] else 'n':4s} {'Y' if r['both'] else 'n':4s} {str(r['entry_secs'] or ''):7s} {str(r['flips'] if r['flips'] is not None else ''):5s} {lead:7s} {move}")
print("peak cumulative", f"{peak[0]:+.2f} at {peak[1]}")

def summarize(name, rs, allmk):
    if not rs: print(f"\n[{name}] no manual markets"); return
    n = len(rs); won = sum(r["pnl"] > 0 for r in rs); buy = sum(r["buy"] for r in rs)
    print(f"\n[{name}] manual: markets={n} won={won} ({100*won/n:.0f}%) spent=${buy:.0f} avg/market=${buy/n:.1f} pnl={sum(r['pnl'] for r in rs):+.2f} "
          f"fav-buys={100*sum(bool(r['fav']) for r in rs)/n:.0f}% held-to-settle={100*sum(r['held'] for r in rs)/n:.0f}% both-sides={100*sum(r['both'] for r in rs)/n:.0f}% "
          f"median entry {sorted(r['entry_secs'] for r in rs if r['entry_secs'] is not None)[len([r for r in rs if r['entry_secs'] is not None])//2] if any(r['entry_secs'] is not None for r in rs) else '-'} s before close")
    c = [cond[s] for s in allmk if s in cond]
    if c:
        mv = [abs(x["move"]) for x in c if x["move"] is not None]; fl = [x["flips"] for x in c]; lw = [x["lead120_won"] for x in c if x["lead120_won"] is not None]
        print(f"[{name}] all {ASSET.upper()} markets in window: n={len(c)} median |move| {sorted(mv)[len(mv)//2] if mv else 0:.0f} bps, "
              f">=1 leader flip in last 150 s: {100*sum(f >= 1 for f in fl)/len(fl):.0f}%, leader at -120 s won: {100*sum(lw)/len(lw) if lw else 0:.0f}%")

allmk = [s for s, ts in start.items() if t0 <= ts < t1]
if tsplit:
    summarize(f"before {SPLIT}", [r for r in rows if r["t"] < tsplit], [s for s in allmk if start[s] < tsplit])
    summarize(f"after {SPLIT}", [r for r in rows if r["t"] >= tsplit], [s for s in allmk if start[s] >= tsplit])
summarize("whole day", rows, allmk)
