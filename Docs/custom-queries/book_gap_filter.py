"""Read-only: for the live rules' shadow entries (BTC, floor 0.80 era, non-US hours), how often each 'chaotic book' sign
was present, and what blocking on it would have cost in wins / saved in losses.  Signs:
  gap: ask + other side's ask - 1 > 0.05 (the two asks disagree: a hole after a sweep)
  crossed: |spot - line| came within 5 USD inside the window (min_abs_dist)
  twap: time-weighted distance in the window weaker than the threshold (|twap| < 20) or on the wrong side
  chain: on-chain feed's latest reading pointed the other way at entry
  flip: the leader's ask had been below 0.60 inside the window (lead_min from sim_decisions)"""
import csv, os, collections
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
TZ = timezone(timedelta(hours=8)); NY = ZoneInfo("America/New_York")
os.chdir(os.path.join(os.getenv("REPO", "/opt/rabbit-24-hour-get-money"), "btc5m_sim", "data"))
def f(v):
    try: return float(v)
    except (TypeError, ValueError): return None
US0, US1 = os.getenv("US_SESSION_START", "09:00"), os.getenv("US_SESSION_END", "17:00")   # the host .env pauses 09:00-17:00 New York
def us_open(ts):
    t = datetime.fromtimestamp(ts, NY)
    return t.weekday() < 5 and US0 <= t.strftime("%H:%M") < US1
lead_min = {}
for r in csv.DictReader(open("sim_decisions.csv")):
    if f(r.get("lead_min")) is not None: lead_min[(r["slug"], r["variant"])] = f(r["lead_min"])
rows = []
for r in csv.DictReader(open("sim_variants_v2.csv")):
    if r["variant"] not in ("w60_m95_s20", "w45_m95_s20") or r.get("entered") != "1" or r.get("won") not in ("0", "1"): continue
    start = int(r["slug"].rsplit("-", 1)[1])
    if start < 1761004800 or us_open(start): continue          # 2026-10-21 00:00 TW onwards (floor 0.80), non-US hours
    ask, oth = f(r["ask"]), f(r["other_side_ask"]); side = r["side"]
    dl, twap, mind, cdl = f(r["dist_at_entry"]), f(r["twap_dist_at_entry"]), f(r["min_abs_dist"]), f(r["chain_dist_at_entry"])
    sign = 1 if side == "Up" else -1
    flags = dict(
        gap=(ask is not None and oth is not None and ask + oth - 1 > 0.05),
        crossed=(mind is not None and mind < 5),
        twapw=(twap is not None and (twap * sign < 20)),
        chain=(cdl is not None and cdl * sign < 0),
        flip=(lead_min.get((r["slug"], r["variant"]), 1.0) < 0.60))
    rows.append(dict(slug=r["slug"], var=r["variant"], ts=start, won=r["won"] == "1", pnl=f(r["pnl"]) or 0.0, ask=ask, oth=oth, mind=mind, twap=twap, cdl=cdl, lm=lead_min.get((r["slug"], r["variant"])), **flags))
print(f"shadow entries (BTC, since 10-21, non-US): {len(rows)}, won {sum(r['won'] for r in rows)}, lost {sum(not r['won'] for r in rows)}, pnl(5sh) {sum(r['pnl'] for r in rows):+.2f}")
for flag in ("gap", "crossed", "twapw", "chain", "flip"):
    hit = [r for r in rows if r[flag]]
    print(f"  {flag:8s}: present in {len(hit):3d} entries -> would block {sum(not r['won'] for r in hit):2d} losses and {sum(r['won'] for r in hit):3d} wins; pnl of blocked {sum(r['pnl'] for r in hit):+.2f}")
anyf = [r for r in rows if r["gap"] or r["crossed"] or r["flip"]]
print(f"  gap|crossed|flip: {len(anyf)} entries -> blocks {sum(not r['won'] for r in anyf)} losses, {sum(r['won'] for r in anyf)} wins; pnl of blocked {sum(r['pnl'] for r in anyf):+.2f}")
print("\nall losses (time rule side ask other min|dist| twap chain lead_min | flags):")
for r in sorted(rows, key=lambda r: r["ts"]):
    if r["won"]: continue
    fl = " ".join(k for k in ("gap", "crossed", "twapw", "chain", "flip") if r[k])
    print(f"  {datetime.fromtimestamp(r['ts'], TZ):%m-%d %H:%M} {r['var']:12s} {r['slug'][-10:]} ask {r['ask']} other {r['oth']} min|d| {r['mind']} twap {r['twap']} chain {r['cdl']} lead_min {r['lm']} | {fl or '-'}")
