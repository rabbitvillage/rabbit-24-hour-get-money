"""Capital allocation across all city modules.

Every pass, the city modules hand in their opportunities; this module decides
how many dollars each one gets. Principles:

- Never all-in. Cash is split by hard caps: per bucket, per city-day (all buckets
  of one city and date fail together if that station's data is wrong), and
  total exposure, with a cash reserve kept aside.
- Capital is locked until the market resolves (about 1 to 2 days), so ranking
  is by return per day locked, not by raw return.
- Less certain signals get less money: a single report at the new high gets a
  reduced cap, a suspicious jump gets nothing.
- Orders are sized against the actual ask ladder, never more than is for sale
  at or below the price limit.
"""
import datetime as dt
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import config


@dataclass
class Limits:
    bankroll: float = 1000.0          # USDC the bot may use
    reserve_pct: float = 0.10         # always kept as cash
    max_total_pct: float = 0.80       # all open positions combined
    max_city_day_pct: float = 0.15    # one city + one date
    max_bucket_pct: float = 0.08      # one bucket
    single_report_factor: float = 0.5 # cap multiplier when only one report confirms
    min_roi: float = 0.005            # skip below 0.5% after fees
    max_price: float = config.MAX_NO_PRICE


@dataclass
class Position:
    token: str
    city: str
    date: str
    bucket: str
    side: str
    shares: float
    cost: float                       # including fees
    opened: str
    release_by: str                   # expected resolution, frees the cash


@dataclass
class Ledger:
    cash: float
    positions: list = field(default_factory=list)

    @classmethod
    def load(cls, path, bankroll):
        p = Path(path)
        if p.exists():
            d = json.loads(p.read_text())
            return cls(d["cash"], [Position(**x) for x in d["positions"]])
        return cls(bankroll)

    def save(self, path):
        Path(path).write_text(json.dumps(
            {"cash": self.cash, "positions": [asdict(x) for x in self.positions]},
            indent=1))

    def exposure(self, city=None, date=None, token=None):
        return sum(p.cost for p in self.positions
                   if (city is None or p.city == city)
                   and (date is None or p.date == str(date))
                   and (token is None or p.token == token))

    def settle_due(self, now):
        """Dry-run settlement: positions past release_by pay out 1.0 per share.
        A live executor must replace this with actual on-chain redemption."""
        keep = []
        for p in self.positions:
            if dt.datetime.fromisoformat(p.release_by) <= now:
                self.cash += p.shares
            else:
                keep.append(p)
        self.positions = keep


def _cost_for(asks, max_price, fee_rate, dollars):
    """Shares, cost incl. fee, worst price for spending up to `dollars`."""
    shares = cost = 0.0
    worst = None
    for p, s in asks:
        if p > max_price or cost >= dollars:
            break
        unit = p + fee_rate * p * (1 - p)
        take = min(s, (dollars - cost) / unit)
        if take <= 0:
            break
        shares += take
        cost += take * unit
        worst = p
    return shares, cost, worst


def plan(opps, ledger, limits=Limits(), now=None, fee_rate=config.DEFAULT_FEE_RATE):
    """Turn opportunities into orders [{token, shares, limit_price, dollars, ...}]."""
    now = now or dt.datetime.now(dt.timezone.utc)
    bank = limits.bankroll
    free_cash = ledger.cash - bank * limits.reserve_pct
    total_room = bank * limits.max_total_pct - ledger.exposure()

    def days_locked(o):
        # Resolves after the local day ends plus the source's next datapoint.
        end = dt.datetime.combine(o.date, dt.time(), dt.timezone.utc) + dt.timedelta(days=1.5)
        return max((end - now).total_seconds() / 86400, 0.25)

    ranked = sorted((o for o in opps if not o.suspicious and o.roi >= limits.min_roi),
                    key=lambda o: o.roi / days_locked(o), reverse=True)
    orders, planned_city, planned_tok = [], {}, {}
    for o in ranked:
        key = (o.city, str(o.date))
        bucket_cap = bank * limits.max_bucket_pct
        if o.single_report:
            bucket_cap *= limits.single_report_factor
        room = min(
            bucket_cap - ledger.exposure(token=o.token) - planned_tok.get(o.token, 0),
            bank * limits.max_city_day_pct - ledger.exposure(*key) - planned_city.get(key, 0),
            total_room, free_cash)
        if room < 1:
            continue
        shares, cost, worst = _cost_for(o.asks, limits.max_price, fee_rate, room)
        if shares < 1:
            continue
        orders.append({"city": o.city, "date": str(o.date), "bucket": o.bucket,
                       "side": o.side, "token": o.token, "shares": round(shares, 2),
                       "limit_price": worst, "dollars": round(cost, 2),
                       "expected_profit": round(shares - cost, 2),
                       "release_by": (now + dt.timedelta(days=days_locked(o))).isoformat()})
        planned_city[key] = planned_city.get(key, 0) + cost
        planned_tok[o.token] = planned_tok.get(o.token, 0) + cost
        total_room -= cost
        free_cash -= cost
    return orders


def apply_dry_run(orders, ledger, now=None):
    """Record planned orders as if fully filled (paper trading)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    for o in orders:
        ledger.cash -= o["dollars"]
        ledger.positions.append(Position(o["token"], o["city"], o["date"], o["bucket"],
                                         o["side"], o["shares"], o["dollars"],
                                         now.isoformat(), o["release_by"]))
