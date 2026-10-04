"""Which buckets are already decided, and is anyone still selling them cheap?"""
from dataclasses import dataclass, field

from . import config


@dataclass
class Opportunity:
    event: str
    bucket: str
    side: str                # "NO" (bucket dead) or "YES" (bucket certain)
    token: str
    observed: float
    best_ask: float
    shares: float            # fillable at <= MAX_NO_PRICE
    cost: float
    fee: float
    profit: float
    roi: float
    note: str
    city: str = ""
    date: object = None
    asks: list = field(default_factory=list)
    suspicious: bool = False     # max set by a sudden jump
    single_report: bool = False  # only one report at this level


def dead_buckets(ev, state, margin=config.MARGIN):
    """Buckets whose outcome is already fixed by observations.
    Returns [(bucket, side)]: side "NO" = cannot win, "YES" = must win."""
    if state.extreme is None:
        return []
    x = state.extreme
    out = []
    for b in ev.buckets:
        if b.closed:
            continue
        if ev.kind == "high":
            if b.hi is not None and x >= b.hi + margin:
                out.append((b, "NO"))
            elif b.hi is None and x >= b.lo + (margin - 1):
                out.append((b, "YES"))         # "N or higher" already reached
        else:
            if b.lo is not None and x <= b.lo - margin:
                out.append((b, "NO"))
            elif b.lo is None and x <= b.hi - (margin - 1):
                out.append((b, "YES"))
    if state.day_complete:                     # day over: everything is known
        fixed = {id(b) for b, _ in out}
        for b in ev.buckets:
            if b.closed or id(b) in fixed:
                continue
            inside = (b.lo is None or x >= b.lo) and (b.hi is None or x <= b.hi)
            out.append((b, "YES" if inside else "NO"))
    return out


def fill(asks, max_price=config.MAX_NO_PRICE, fee_rate=config.DEFAULT_FEE_RATE,
         budget=None):
    """Walk the ask side. Returns (shares, cost, fee)."""
    shares = cost = fee = 0.0
    for p, s in asks:
        if p > max_price:
            break
        if budget is not None:
            s = min(s, max(0.0, (budget - cost - fee) / (p + fee_rate * p * (1 - p))))
        if s <= 0:
            break
        shares += s
        cost += p * s
        fee += s * fee_rate * p * (1 - p)
    return shares, cost, fee


def evaluate(ev, state, books, budget=None):
    """books: token_id -> asks list."""
    opps = []
    for b, side in dead_buckets(ev, state):
        token = b.no_token if side == "NO" else b.yes_token
        asks = books.get(token) or []
        if not asks or asks[0][0] > config.MAX_NO_PRICE:
            continue
        shares, cost, fee = fill(asks, fee_rate=ev.fee_rate, budget=budget)
        if shares <= 0:
            continue
        profit = shares - cost - fee
        notes = []
        if state.spike:
            notes.append("max set by a sudden jump: check for sensor error")
        if state.confirmations < 2 and not state.day_complete:
            notes.append("only one report at this level")
        if state.day_complete:
            notes.append("local day complete")
        opps.append(Opportunity(ev.slug, b.title, side, token, state.extreme, asks[0][0],
                                round(shares, 2), round(cost, 2), round(fee, 4),
                                round(profit, 2), round(profit / (cost + fee), 4),
                                "; ".join(notes),
                                suspicious=state.spike,
                                single_report=state.confirmations < 2 and not state.day_complete))
    return opps
