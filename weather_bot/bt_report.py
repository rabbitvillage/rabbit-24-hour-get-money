"""Post-process backtest output: check every 'dead' bucket against how the market
actually resolved, subtract taker fees, and write a per-city/per-day report.

    python3 -m weather_bot.bt_report results/bt14
"""
import csv, json, sys, datetime as dt
from collections import defaultdict, Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from . import polymarket as P
from .cities import MODULES

FEE = 0.05


def winners(city_dates):
    by_city = {m.city: m for m in MODULES}
    def one(cd):
        city, date = cd
        raw = P.event_by_slug(by_city[city].event_slugs(dt.date.fromisoformat(date))[0])
        win = [m["groupItemTitle"] for m in (raw or {}).get("markets", [])
               if (m.get("outcomePrices") or "").startswith('["1"')]
        return cd, (win[0] if win else None)
    with ThreadPoolExecutor(8) as pool:
        return dict(pool.map(one, city_dates))


def main(d):
    d = Path(d)
    days = list(csv.DictReader(open(d / "days.csv")))
    trades = list(csv.DictReader(open(d / "trades.csv")))
    win = winners({(r["city"], r["date"]) for r in trades})
    rows, bad_days = [], set()
    for r in trades:
        p, sh = float(r["price"]), float(r["shares"])
        no_price = p if r["kind"] == "buy_no" else 1 - p
        fee = sh * FEE * p * (1 - p)
        lost = win[(r["city"], r["date"])] == r["bucket"]   # 'dead' bucket actually won
        pnl = (-sh * no_price if lost else float(r["profit"])) - fee
        if lost:
            bad_days.add((r["city"], r["date"]))
        rows.append({**r, "no_price": round(no_price, 4), "fee": round(fee, 4),
                     "bucket_won": lost, "net": round(pnl, 4)})
    with open(d / "trades_checked.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader(); w.writerows(rows)

    good = [r for r in rows if not r["bucket_won"]]
    def agg(key, rs):
        a = defaultdict(lambda: [0.0, 0.0, 0])
        for r in rs:
            x = a[r[key]]; x[0] += r["net"]; x[1] += float(r["shares"]) * r["no_price"]; x[2] += 1
        return {k: {"net_usd": round(v[0], 2), "capital_usd": round(v[1], 2), "trades": v[2]}
                for k, v in sorted(a.items(), key=lambda kv: -kv[1][0])}
    band = lambda q: ("<0.90" if q < .9 else "0.90-0.95" if q < .95 else
                      "0.95-0.97" if q < .97 else "0.97-0.99")
    for r in rows:
        r["band"] = band(r["no_price"])
    out = {
        "calendar_days": len({r["date"] for r in days}), "city_days": len(days),
        "false_dead_city_days": sorted(f"{c} {dd} (won: {win[(c, dd)]})" for c, dd in bad_days),
        "loss_on_false_dead_usd": round(sum(r["net"] for r in rows if r["bucket_won"]), 2),
        "truly_dead": {
            "net_after_fee_usd": round(sum(r["net"] for r in good), 2),
            "fees_usd": round(sum(r["fee"] for r in good), 2),
            "capital_turned_usd": round(sum(float(r["shares"]) * r["no_price"] for r in good), 2),
            "by_band": agg("band", good), "by_date": agg("date", good), "by_city": agg("city", good)},
    }
    (d / "report.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
    print(json.dumps({k: v for k, v in out.items() if k != "truly_dead"} |
                     {"net": out["truly_dead"]["net_after_fee_usd"],
                      "band": out["truly_dead"]["by_band"],
                      "date": out["truly_dead"]["by_date"]}, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1])
