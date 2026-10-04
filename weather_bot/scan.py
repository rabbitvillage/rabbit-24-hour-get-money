"""Runs every city module, then the allocator. Never places real orders.

    python3 -m weather_bot.scan                        # one pass, all 49 cities
    python3 -m weather_bot.scan --loop 30              # every 30 s
    python3 -m weather_bot.scan --city tokyo --city nyc
    python3 -m weather_bot.scan --bankroll 2000 --paper  # paper-trade into ledger.json
"""
import argparse
import dataclasses
import datetime as dt
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from . import allocator
from .cities import BY_NAME, MODULES


def run_modules(modules, now):
    def one(m):
        try:
            return m, *m.scan(now)
        except Exception as err:            # one city failing never stops the rest
            return m, [], [f"{m.city}: {err}"]
    with ThreadPoolExecutor(max_workers=12) as pool:
        return list(pool.map(one, modules))


def scan_once(modules, limits, ledger, paper, log):
    now = dt.datetime.now(dt.timezone.utc)
    ledger.settle_due(now)
    opps = []
    for m, found, warns in run_modules(modules, now):
        opps += found
        for w in warns:
            print(f"  warn {w}", file=sys.stderr)
    for o in opps:
        print(f"[{now:%H:%M:%SZ}] {o.city:<14} {o.date} {o.bucket:<14} {o.side} "
              f"ask {o.best_ask:.3f} obs={o.observed} depth={o.shares} "
              f"roi={o.roi:.1%} {o.note}")
    orders = allocator.plan(opps, ledger, limits, now)
    for od in orders:
        print(f"  PLAN {od['city']} {od['bucket']} {od['side']} {od['shares']} sh "
              f"@<= {od['limit_price']} ${od['dollars']} (+${od['expected_profit']})")
        log.write(json.dumps({"ts": now.isoformat(), **od}) + "\n")
    log.flush()
    if paper:
        allocator.apply_dry_run(orders, ledger, now)
    print(f"[{now:%H:%M:%SZ}] {len(modules)} cities, {len(opps)} opportunities, "
          f"{len(orders)} planned, cash ${ledger.cash:.2f}, "
          f"open ${ledger.exposure():.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", action="append", help="limit to these cities")
    ap.add_argument("--loop", type=int, help="seconds between passes")
    ap.add_argument("--bankroll", type=float, default=1000.0)
    ap.add_argument("--paper", action="store_true", help="record plans in the ledger")
    ap.add_argument("--ledger", default="ledger.json")
    ap.add_argument("--log", default="plans.jsonl")
    a = ap.parse_args()
    modules = [BY_NAME[c.lower()] for c in a.city] if a.city else MODULES
    limits = allocator.Limits(bankroll=a.bankroll)
    ledger = allocator.Ledger.load(a.ledger, a.bankroll)
    with open(a.log, "a") as log:
        while True:
            try:
                scan_once(modules, limits, ledger, a.paper, log)
                ledger.save(a.ledger)
            except Exception as err:
                print(f"pass failed: {err}", file=sys.stderr)
            if not a.loop:
                break
            time.sleep(a.loop)


if __name__ == "__main__":
    main()
