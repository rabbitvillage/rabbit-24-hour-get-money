"""Shared helpers for the read-only wallet audits in this folder (standard library only, no secrets).

- fetch_activity(): Polymarket data-api /activity for a public wallet address, paced for the rate limit.
- load_bot_orders(): the bot's filled orders from btc5m_sim/data*/live_orders.csv.
- attribute(): match each on-chain BUY to one bot order by slug + outcome + time (+-300 s) + size; everything
  else on a crypto market is a manual trade. Matching by slug + outcome alone is NOT enough: on a day with manual
  trading the user may buy the same side the bot bought.
"""
import csv, json, os, time, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

TZ = timezone(timedelta(hours=8))
CRYPTO = ("btc-", "eth-", "sol-", "xrp-")
DATA_DIRS = ("data", "data-eth", "data-sol", "data-btc15")


def get(url, tries=6):
    for i in range(tries):
        try:
            return json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"user-agent": "btc5m-audit"}), timeout=30))
        except urllib.error.HTTPError as e:
            if e.code != 429: raise
            time.sleep(5 * (i + 1))
    raise RuntimeError("data-api rate limited")


def fetch_activity(wallet, stop_before=None, max_rows=6000):
    """All activity rows (TRADE / REDEEM / ...), newest first; stops once rows older than stop_before (epoch) appear."""
    acts = []
    for off in range(0, max_rows, 100):
        d = get(f"https://data-api.polymarket.com/activity?user={wallet}&limit=100&offset={off}")
        if not d: break
        acts += d
        if len(d) < 100 or (stop_before and min(a["timestamp"] for a in d) < stop_before): break
        time.sleep(1.5)
    return acts


def load_bot_orders(root):
    out = []
    for d in DATA_DIRS:
        p = os.path.join(root, "btc5m_sim", d, "live_orders.csv")
        if not os.path.exists(p): continue
        for o in csv.DictReader(open(p)):
            if o["status"] != "filled": continue
            ts = datetime.strptime(o["ts"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ).timestamp()
            out.append(dict(ts=ts, slug=o["slug"], outcome=o["outcome"], size=float(o["filled_size"] or o["size"] or 0),
                            cost=float(o["cost"] or 0), variant=o["variant"], dir=d, key=(d, o["variant"], o["slug"])))
    return out


def load_winners(root):
    w = {}
    for d in DATA_DIRS:
        p = os.path.join(root, "btc5m_sim", d, "sim_trades.csv")
        if not os.path.exists(p): continue
        for r in csv.DictReader(open(p)):
            if r["winner"] in ("Up", "Down"): w[r["slug"]] = r["winner"]
    return w


def load_results(root):
    """Ledger P&L per bot order key (dir, variant, slug) from live_results.csv."""
    res = {}
    for d in DATA_DIRS:
        p = os.path.join(root, "btc5m_sim", d, "live_results.csv")
        if not os.path.exists(p): continue
        for r in csv.DictReader(open(p)):
            if r["won"] in ("0", "1"): res[(d, r["variant"], r["slug"])] = float(r["pnl"])
    return res


def attribute(acts, orders, window=300):
    """Returns (bot_fills, manual, unmatched_orders). bot_fills: list of (chain_row, order). manual: crypto TRADE rows
    not explained by a bot order (BUY or SELL). A bot order may be explained by several chain rows (split fill)."""
    by_key = {}
    for a in acts:
        if a.get("type") == "TRADE" and a.get("side") == "BUY" and (a.get("slug") or "").startswith(CRYPTO):
            by_key.setdefault((a["slug"], a["outcome"]), []).append(a)
    used = set(); bot_fills = []; unmatched = []
    for o in sorted(orders, key=lambda o: o["ts"]):
        cands = [a for a in by_key.get((o["slug"], o["outcome"]), []) if id(a) not in used and abs(a["timestamp"] - o["ts"]) <= window]
        exact = [a for a in cands if abs(float(a["size"]) - o["size"]) <= max(0.02, 0.02 * o["size"])]
        if exact:
            a = min(exact, key=lambda a: abs(a["timestamp"] - o["ts"])); used.add(id(a)); bot_fills.append((a, o)); continue
        got = 0.0; take = []
        for a in sorted(cands, key=lambda a: abs(a["timestamp"] - o["ts"])):
            if got >= o["size"] - 0.02: break
            take.append(a); got += float(a["size"])
        if take and abs(got - o["size"]) <= max(0.02, 0.02 * o["size"]):
            for a in take: used.add(id(a)); bot_fills.append((a, o))
        else:
            unmatched.append(o)
    manual = [a for a in acts if a.get("type") == "TRADE" and (a.get("slug") or "").startswith(CRYPTO) and id(a) not in used]
    return bot_fills, manual, unmatched


def fee_of(a):
    """USDC fee on a chain BUY row = usdcSize - size x price (Polymarket crypto taker fee 7% x p x (1-p) x shares)."""
    return float(a["usdcSize"]) - float(a["size"]) * float(a["price"])
