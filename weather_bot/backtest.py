"""Backtest: after a bucket was ruled out by observations, how much money still
changed hands on it at pick-up prices, how long after, and who took it.

    python3 -m weather_bot.backtest --days 14                 # all cities
    python3 -m weather_bot.backtest --days 14 --city tokyo --city nyc
    python3 -m weather_bot.backtest --days 14 --out backtest_results

Writes <out>/trades.csv (one row per pick-up trade), <out>/days.csv (one row per
city-day) and <out>/summary.json, and prints the summary. Read-only.
"""
import argparse
import csv
import datetime as dt
import io
import json
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config, observations as O, polymarket as P
from .cities import BY_NAME, MODULES
from .http import get

DATA_API = "https://data-api.polymarket.com/trades"
MESONET = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
PUBLISH_LAG = dt.timedelta(minutes=5)   # observation time -> visible on the source
MAX_NO = 0.99                           # a No at or below this counts as a pick-up


# ---- historical observations (Iowa State Mesonet ASOS/METAR archive) --------

def mesonet_obs(icao, start, end):
    """Routine + special METARs for [start, end) UTC as a list of Obs."""
    params = [("station", icao), ("data", "tmpc"), ("data", "metar"),
              ("year1", start.year), ("month1", start.month), ("day1", start.day),
              ("year2", end.year), ("month2", end.month), ("day2", end.day),
              ("tz", "Etc/UTC"), ("format", "onlycomma"), ("latlon", "no"),
              ("elev", "no"), ("missing", "M"), ("trace", "T"), ("direct", "no"),
              ("report_type", "3"), ("report_type", "4")]
    text = get(MESONET + "?" + "&".join(f"{k}={v}" for k, v in params), as_json=False, timeout=60)
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        if row.get("tmpc") in (None, "", "M"):
            continue
        when = dt.datetime.strptime(row["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=dt.timezone.utc)
        raw = row.get("metar") or ""
        t, precise = O.parse_metar_temp(raw)
        if t is None:
            t, precise = float(row["tmpc"]), False
        out.append(O.Obs(when, float(t), precise, raw.startswith("SPECI"), raw))
    return sorted(out, key=lambda o: o.time)


# ---- Polymarket trade history -----------------------------------------------

def trades_for(condition_id, start_ts, end_ts, page=10000):
    """All taker trades on one market between two epoch seconds."""
    out, offset = [], 0
    while True:
        batch = get(DATA_API, {"market": condition_id, "limit": page, "offset": offset,
                               "start": start_ts, "end": end_ts, "takerOnly": "true"})
        out += batch
        if len(batch) < page:
            return out
        offset += page
        if offset >= 10000:          # API cap: narrow the window and continue
            last = min(t["timestamp"] for t in batch)
            return out + trades_for(condition_id, start_ts, last - 1, page)


# ---- the matching -----------------------------------------------------------

def death_times(ev, obs, tz, now=None, margin=config.MARGIN):
    """bucket -> UTC time from which the source must have shown it dead.
    Uses the same conservative value as the live detector; the last bucket to
    survive is never marked dead before the local day ends."""
    start, end = O.local_day_window(ev.date, tz)
    day = [o for o in obs if start <= o.time < end and not (ev.hourly_only and o.speci)]
    dead, running = {}, None
    pick = max if ev.kind == "high" else min
    for o in day:
        v = O.guaranteed_value(o, ev.unit, ev.kind)
        running = v if running is None else pick(running, v)
        for b in ev.buckets:
            if b in dead:
                continue
            if ev.kind == "high" and b.hi is not None and running >= b.hi + margin:
                dead[b] = o.time + PUBLISH_LAG
            if ev.kind == "low" and b.lo is not None and running <= b.lo - margin:
                dead[b] = o.time + PUBLISH_LAG
    return dead, running


def classify(trade):
    """Return (profit_per_share, kind) if this taker trade picked up certain money
    on a dead bucket, else None. Buying No cheap and selling Yes dear are the same
    position seen from either side."""
    p = float(trade["price"])
    if trade["outcome"] == "No" and trade["side"] == "BUY" and p <= MAX_NO:
        return round(1 - p, 6), "buy_no"
    if trade["outcome"] == "Yes" and trade["side"] == "SELL" and p >= 1 - MAX_NO:
        return p, "sell_yes"
    return None


def backtest_day(module, date, obs_cache):
    """One city, one date -> (day_row, pickup_rows) or (None, [])."""
    slug = module.event_slugs(date)[0]                 # highest only
    raw = P.event_by_slug(slug)
    if not raw:
        return None, []
    ev = P.parse_event(raw)
    if ev.station != module.station:
        return {"city": module.city, "date": str(date), "error": f"rules name {ev.station}"}, []
    start, end = O.local_day_window(date, module.tz)
    key = (module.station, date)
    if key not in obs_cache:
        obs_cache[key] = mesonet_obs(module.station, start - dt.timedelta(days=1),
                                     end + dt.timedelta(days=1))
    dead, day_max = death_times(ev, obs_cache[key], module.tz)
    rows, wallets = [], Counter()
    total_volume = sum(float(m.get("volumeNum") or m.get("volume") or 0)
                       for m in raw.get("markets", []))
    cond = {b: next((m["conditionId"] for m in raw["markets"]
                     if str(m.get("id")) == b.market_id), None) for b in ev.buckets}
    for b, t_dead in dead.items():
        if not cond[b]:
            continue
        for tr in trades_for(cond[b], int(t_dead.timestamp()), int(end.timestamp()) + 36 * 3600):
            c = classify(tr)
            if not c:
                continue
            per_share, kind = c
            when = dt.datetime.fromtimestamp(tr["timestamp"], dt.timezone.utc)
            rows.append({
                "city": module.city, "date": str(date), "bucket": b.title, "kind": kind,
                "price": float(tr["price"]), "shares": float(tr["size"]),
                "profit": round(float(tr["size"]) * per_share, 4),
                "min_after_death": round((when - t_dead).total_seconds() / 60, 1),
                "wallet": tr.get("proxyWallet", ""), "ts": when.isoformat()})
            wallets[tr.get("proxyWallet", "")] += float(tr["size"]) * per_share
    profit = sum(r["profit"] for r in rows)
    top = wallets.most_common(1)[0] if wallets else ("", 0)
    day = {"city": module.city, "date": str(date), "day_max": day_max,
           "buckets_dead": len(dead), "event_volume": round(total_volume, 2),
           "pickup_trades": len(rows), "pickup_shares": round(sum(r["shares"] for r in rows), 2),
           "pickup_profit": round(profit, 2),
           "within_5min": round(sum(r["profit"] for r in rows if r["min_after_death"] <= 5), 2),
           "within_30min": round(sum(r["profit"] for r in rows if r["min_after_death"] <= 30), 2),
           "wallets": len(wallets), "top_wallet_share": round(top[1] / profit, 3) if profit else 0}
    return day, rows


def run(modules, days, out_dir, workers=6):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    today = dt.datetime.now(dt.timezone.utc).date()
    dates = [today - dt.timedelta(days=i) for i in range(1, days + 1)]
    obs_cache, day_rows, trade_rows, errors = {}, [], [], []

    def one(args):
        m, d = args
        for attempt in range(3):
            try:
                return backtest_day(m, d, obs_cache)
            except Exception as err:            # rate limits etc: back off and retry
                if attempt == 2:
                    return {"city": m.city, "date": str(d), "error": str(err)}, []
                time.sleep(2 ** attempt)

    jobs = [(m, d) for m in modules for d in dates]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, (day, rows) in enumerate(pool.map(one, jobs), 1):
            if day is None:
                continue
            (errors if "error" in day else day_rows).append(day)
            trade_rows += rows
            if i % 20 == 0:
                print(f"  {i}/{len(jobs)} city-days done", file=sys.stderr)

    _write_csv(out / "days.csv", day_rows)
    _write_csv(out / "trades.csv", trade_rows)
    summary = summarize(day_rows, trade_rows, errors)
    (out / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False))
    return summary


def summarize(day_rows, trade_rows, errors):
    n_days = len(day_rows)
    profit = sum(d["pickup_profit"] for d in day_rows)
    volume = sum(d["event_volume"] for d in day_rows)
    by_city = defaultdict(float)
    for d in day_rows:
        by_city[d["city"]] += d["pickup_profit"]
    by_price = Counter()
    for r in trade_rows:
        band = ("<=0.95" if r["price"] <= 0.95 else "0.95-0.98" if r["price"] <= 0.98
                else "0.98-0.99") if r["kind"] == "buy_no" else (
                ">=0.05" if r["price"] >= 0.05 else "0.02-0.05" if r["price"] >= 0.02 else "0.01-0.02")
        by_price[f"{r['kind']} {band}"] += r["profit"]
    wallets = Counter()
    for r in trade_rows:
        wallets[r["wallet"]] += r["profit"]
    top5 = wallets.most_common(5)
    return {
        "city_days": n_days, "errors": len(errors),
        "calendar_days": len({d["date"] for d in day_rows}),
        "total_event_volume_usd": round(volume, 2),
        "pickup_profit_usd": round(profit, 2),
        "pickup_profit_per_calendar_day": round(profit / max(len({d["date"] for d in day_rows}), 1), 2),
        "pickup_share_of_volume": round(profit / volume, 4) if volume else None,
        "profit_within_5min": round(sum(d["within_5min"] for d in day_rows), 2),
        "profit_within_30min": round(sum(d["within_30min"] for d in day_rows), 2),
        "profit_by_price_band": {k: round(v, 2) for k, v in by_price.items()},
        "top_cities": sorted(((round(v, 2), c) for c, v in by_city.items()), reverse=True)[:10],
        "distinct_wallets": len(wallets),
        "top5_wallet_share": round(sum(v for _, v in top5) / profit, 3) if profit else None,
        "top5_wallets": [(w[:10] + "…", round(v, 2)) for w, v in top5],
        "error_samples": errors[:5],
    }


def _write_csv(path, rows):
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--city", action="append")
    ap.add_argument("--out", default="backtest_results")
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    modules = [BY_NAME[c.lower()] for c in a.city] if a.city else MODULES
    summary = run(modules, a.days, a.out, a.workers)
    print(json.dumps(summary, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
