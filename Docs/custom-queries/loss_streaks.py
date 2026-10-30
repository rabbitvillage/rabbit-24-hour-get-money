"""Read-only: the bot's real losing streaks, and whether today's rule set would still have taken each losing order.
For every settled real order (live_orders filled x live_results) in time order, finds runs of >= MIN_STREAK losses
(all assets together and per asset). For each loss in a run it replays the CURRENT configuration:
  asset stopped (ETH)  /  US-session pause  /  price band + window of the live rules  /  direction + distance filter
  /  chop filter (spot amplitude since the book window opened)  /  loss cool-down  /  one order per market  /  events calendar
and reports which of them would have blocked it now. Nothing here is a trading signal; it is a post-mortem.
Usage (on the host, inside the repo):  python3 Docs/custom-queries/loss_streaks.py [min_streak=3]
Current parameters are read from the environment like the bot does (source btc5m_sim/.env first) or fall back to the
values in force on 2026-10-27 (price 0.80-0.95, window 60 s, dist 20 BTC / 0.08 SOL, chop 20 bps, cool-down 9/6 markets).
"""
import csv, os, sys, collections
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.getcwd())
TZ = timezone(timedelta(hours=8))
MIN_STREAK = int(sys.argv[1]) if len(sys.argv) > 1 else 3
PRICE_MIN, PRICE_MAX, WINDOW = float(os.getenv("M_FLOOR", "0.80")), 0.95, 60.0
CHOP = float(os.getenv("CHOP_MAX_AMP_BPS", "20"))
DIST = {"btc": 20.0, "sol": 20.0 * float(os.getenv("DIST_SCALE_SOL", "0.004")), "eth": 20.0 * 0.0297}
COOLDOWN = {"btc": int(os.getenv("LIVE_LOSS_COOLDOWN_MARKETS", "9")), "sol": 6, "eth": 9}
STOPPED = {"eth"}
try:
    from zoneinfo import ZoneInfo; NY = ZoneInfo("America/New_York")
except Exception: NY = None
try:
    from btc5m_sim import live as _live
    def event_pause(ts): return _live.event_pause(now=datetime.fromtimestamp(ts, timezone.utc))
except Exception:
    def event_pause(ts): return None

def us_session(ts):
    t = datetime.fromtimestamp(ts, NY) if NY else datetime.fromtimestamp(ts, timezone(timedelta(hours=-4)))
    return t.weekday() < 5 and 9 * 60 <= t.hour * 60 + t.minute < 17 * 60

def f(v):
    try: return float(v)
    except (TypeError, ValueError): return None

trades = []
books_needed = collections.defaultdict(set)
for d, asset in (("data", "btc"), ("data-eth", "eth"), ("data-sol", "sol")):
    base = os.path.join("btc5m_sim", d)
    if not os.path.exists(os.path.join(base, "live_results.csv")): continue
    orders = {(o["variant"], o["slug"]): o for o in csv.DictReader(open(os.path.join(base, "live_orders.csv"))) if o["mode"] == "live" and o["status"] == "filled"}
    var, ref_of = {}, {}
    for name in ("sim_variants.csv", "sim_variants_v2.csv"):      # BTC moved to the v2 file on 10-20; eth/sol were born v2
        p = os.path.join(base, name)
        if not os.path.exists(p): continue
        for r in csv.DictReader(open(p)):
            var[(r["variant"], r["slug"])] = r
            rf = f(r.get("chain_at_start")) or f(r.get("spot_at_start"))
            if rf and r["slug"] not in ref_of: ref_of[r["slug"]] = rf
    for r in csv.DictReader(open(os.path.join(base, "live_results.csv"))):
        o = orders.get((r["variant"], r["slug"]))
        if not o or r["won"] not in ("0", "1"): continue
        v = var.get((r["variant"], r["slug"]), {})
        ts = datetime.strptime(o["ts"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ).timestamp()
        start = int(r["slug"].rsplit("-", 1)[1])
        trades.append(dict(ts=ts, start=start, asset=asset, rule=r["variant"], slug=r["slug"], side=r["outcome"], ask=f(o["ask"]), secs=f(o["secs_left"]),
                           fill=(f(r["cost"]) / f(r["size"])) if f(r["size"]) else f(o["ask"]), cost=f(r["cost"]), pnl=f(r["pnl"]), won=r["won"] == "1",
                           dl=f(v.get("dist_at_entry")), ref=ref_of.get(r["slug"]), base=base))
        books_needed[base].add(r["slug"])
trades.sort(key=lambda t: t["ts"])

# from sim_book.csv: spot amplitude since the book window opened (150 s) up to the entry second, and the distance at entry
# (spot at entry - open reference) when the variant row did not record it
amp = {}
for base, slugs in books_needed.items():
    rows = collections.defaultdict(list)
    for r in csv.DictReader(open(os.path.join(base, "sim_book.csv"))):
        if r["slug"] in slugs and r["spot"]:
            try: rows[r["slug"]].append((float(r["secs_left"]), float(r["spot"])))
            except ValueError: pass
    for t in trades:
        if t["base"] != base or t["slug"] not in rows: continue
        pts = [s for sec, s in rows[t["slug"]] if sec >= (t["secs"] or 0)]
        if pts and t["ref"]: amp[(base, t["slug"], t["rule"])] = (max(pts) - min(pts)) / t["ref"] * 1e4
        if t["dl"] is None and t["ref"] and rows[t["slug"]]:
            sec, sp = min(rows[t["slug"]], key=lambda x: abs(x[0] - (t["secs"] or 0)))
            if abs(sec - (t["secs"] or 0)) <= 3: t["dl"] = sp - t["ref"]; t["dl_src"] = "book"

def blockers(t, prev_losses, same_market_before):
    out = []
    if t["asset"] in STOPPED: out.append("ETH 已停")
    if us_session(t["start"]): out.append("美股時段停單")
    if t["ask"] is None or not (PRICE_MIN <= t["ask"] <= PRICE_MAX): out.append(f"賣價 {t['ask']} 不在 0.80～0.95")
    if t["secs"] is not None and t["secs"] > WINDOW: out.append(f"剩 {t['secs']:.0f} 秒 > 60 秒窗口")
    dl, thr = t["dl"], DIST[t["asset"]]
    if dl is None: out.append("沒有距離資料（方向過濾會擋）")
    elif abs(dl) < thr: out.append(f"距離 {dl:+.2f} < {thr:g}")
    elif (dl > 0) != (t["side"] == "Up"): out.append(f"方向不對（{t['side']}，距離 {dl:+.2f}）")
    a = amp.get((t["base"], t["slug"], t["rule"]))
    if a is not None and a >= CHOP: out.append(f"震盪 {a:.0f} bps ≥ {CHOP:g}")
    cd = COOLDOWN[t["asset"]] * 300
    recent = [p for p in prev_losses if p["asset"] == t["asset"] and 0 < t["start"] - p["start"] <= cd]
    if recent: out.append(f"冷卻中（{t['start'] - recent[-1]['start']} 秒前剛輸）")
    if same_market_before: out.append("同場已有一筆")
    ev = event_pause(t["start"])
    if ev: out.append(f"事件停單：{str(ev)[:30]}")
    return out

# streaks
def streaks(seq):
    runs, run = [], []
    for t in seq:
        if not t["won"]: run.append(t)
        else:
            if len(run) >= MIN_STREAK: runs.append(run)
            run = []
    if len(run) >= MIN_STREAK: runs.append(run)
    return runs

losses = [t for t in trades if not t["won"]]
print(f"real settled orders {len(trades)}, losses {len(losses)} ({100*len(losses)/len(trades):.1f}%)")
all_runs = streaks(trades)
per_asset = {a: streaks([t for t in trades if t["asset"] == a]) for a in ("btc", "eth", "sol")}
print(f"losing streaks of >= {MIN_STREAK} (all assets in time order): {len(all_runs)}; per asset: " + ", ".join(f"{a} {len(r)}" for a, r in per_asset.items()))

# replay in time order with TODAY's rules: an order counts as "taken now" only when nothing blocks it; only taken-now
# losses start a cool-down and only taken-now orders occupy the one slot per market (orders of rules that no longer exist do not)
taken_market = collections.Counter()
taken_losses = []
for t in trades:
    key = (t["asset"], t["slug"]); t["blk"] = blockers(t, taken_losses, taken_market[key] > 0)
    if not t["blk"]:
        taken_market[key] += 1
        if not t["won"]: taken_losses.append(t)

def show(run, title):
    total = sum(t["pnl"] for t in run); blocked = sum(1 for t in run if t["blk"])
    print(f"\n### {title}: {len(run)} 連輸，合計 {total:+.2f}，現在的設定會擋掉 {blocked}/{len(run)} 筆")
    for t in run:
        a = amp.get((t["base"], t["slug"], t["rule"]))
        print(f"  {datetime.fromtimestamp(t['ts'], TZ):%m-%d %H:%M} {t['asset'].upper():3s} {t['rule']:13s} {t['side']:4s} 賣價 {t['ask']} 成交 {t['fill']:.3f} 剩 {t['secs'] or 0:.0f}s "
              f"距離 {'' if t['dl'] is None else f'{t[chr(100)+chr(108)]:+.1f}'} 振幅 {'' if a is None else f'{a:.0f}'} {t['pnl']:+.2f} | "
              + ("；".join(t["blk"]) if t["blk"] else "★ 現在也會進場"))

for run in all_runs: show(run, f"跨幣 {datetime.fromtimestamp(run[0]['ts'], TZ):%m-%d %H:%M}")
for a, runs in per_asset.items():
    for run in runs:
        if not any(run[0] is r[0] and len(run) == len(r) for r in all_runs): show(run, f"{a.upper()} {datetime.fromtimestamp(run[0]['ts'], TZ):%m-%d %H:%M}")

still = [t for t in losses if not t["blk"]]
print(f"\n=== 全部 {len(losses)} 筆輸單中，用現在的設定仍會進場的：{len(still)} 筆，合計 {sum(t['pnl'] for t in still):+.2f}")
by_reason = collections.Counter()
for t in losses:
    for b in t["blk"]: by_reason[b.split("（")[0].split(" ")[0]] += 1
print("擋掉的原因（一筆可能多個）：" + ", ".join(f"{k} {v}" for k, v in by_reason.most_common()))
for t in still:
    print(f"  ★ {datetime.fromtimestamp(t['ts'], TZ):%m-%d %H:%M} {t['asset'].upper()} {t['rule']} {t['side']} 賣價 {t['ask']} 成交 {t['fill']:.3f} 剩 {t['secs'] or 0:.0f}s 距離 {t['dl']} {t['pnl']:+.2f}")
