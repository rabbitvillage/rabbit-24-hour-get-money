"""Market discovery (Gamma API), rule parsing and order books (CLOB API)."""
import datetime as dt
import json
import re
from dataclasses import dataclass, field

from . import config
from .http import get

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1)}


@dataclass(eq=False)
class Bucket:
    title: str
    lo: float | None        # inclusive lower bound, None = "or below"
    hi: float | None        # inclusive upper bound, None = "or higher"
    yes_token: str
    no_token: str
    market_id: str
    closed: bool = False


@dataclass
class TempEvent:
    slug: str
    title: str
    kind: str                # "high" or "low"
    station: str             # e.g. RJTT, KLGA, HKO
    unit: str                # "C" or "F"
    date: dt.date            # observation date, station local time
    hourly_only: bool        # rules say "Hourly Data" -> ignore SPECI reports
    fee_rate: float
    source_url: str
    buckets: list = field(default_factory=list)


def discover_events(tag_slugs=("weather", "highest-temperature", "daily-temperature"),
                    days_back=1, page=100):
    """All open daily temperature events. Tag filters on Gamma are not fully
    consistent, so query several tags and de-duplicate by slug."""
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_back)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    seen = {}
    for tag in tag_slugs:
        offset = 0
        while True:
            batch = get(f"{config.GAMMA}/events", {
                "tag_slug": tag, "closed": "false", "end_date_min": since,
                "limit": page, "offset": offset})
            for ev in batch:
                t = (ev.get("title") or "").lower()
                if t.startswith(("highest temperature in", "lowest temperature in")):
                    seen[ev["slug"]] = ev
            if len(batch) < page:
                break
            offset += page
    return list(seen.values())


def event_by_slug(slug):
    evs = get(f"{config.GAMMA}/events", {"slug": slug})
    return evs[0] if evs else None


def station_from_source(url, description=""):
    url = url or ""
    m = re.search(r"[?&]site=([A-Za-z0-9]{3,5})", url)               # weather.gov timeseries
    if m:
        return m.group(1).upper()
    m = re.search(r"wunderground\.com/history/daily/.+/([A-Z0-9]{4})", url)
    if m:
        return m.group(1).upper()
    if "weather.gov.hk" in url or "Hong Kong Observatory" in description:
        return "HKO"
    m = re.search(r"timeseries\?site=([A-Za-z0-9]{4})", description)
    return m.group(1).upper() if m else None


_NUM = r"(-?\d+(?:\.\d+)?)"


def parse_bucket_title(title):
    """'17°C or below' / '18°C' / '62-63°F' / '80°F or higher' -> (lo, hi, unit)."""
    t = title.replace("º", "°").strip()
    unit = "F" if "°F" in t or t.endswith("F") else "C"
    if m := re.match(_NUM + r"\s*°?[CF]?\s*or (below|lower|less)", t):
        return None, float(m.group(1)), unit
    if m := re.match(_NUM + r"\s*°?[CF]?\s*or (higher|above|more)", t):
        return float(m.group(1)), None, unit
    if m := re.match(_NUM + r"\s*(?:-|–|to)\s*" + _NUM, t):
        return float(m.group(1)), float(m.group(2)), unit
    if m := re.match(_NUM + r"\s*°?[CF]?$", t):
        v = float(m.group(1))
        return v, v, unit
    raise ValueError(f"unrecognised bucket title: {title!r}")


def date_from_slug(slug):
    m = re.search(r"-on-([a-z]+)-(\d{1,2})-(\d{4})$", slug)
    if not m:
        raise ValueError(f"no date in slug {slug}")
    return dt.date(int(m.group(3)), MONTHS[m.group(1)], int(m.group(2)))


def parse_event(ev):
    desc = ev.get("description") or ""
    src = ev.get("resolutionSource") or ""
    kind = "low" if ev["title"].lower().startswith("lowest") else "high"
    fee_rate = config.DEFAULT_FEE_RATE
    buckets, unit = [], "C"
    for mk in ev.get("markets", []):
        if mk.get("feeSchedule"):
            fee_rate = float(mk["feeSchedule"].get("rate", fee_rate))
        title = mk.get("groupItemTitle") or mk.get("question")
        lo, hi, unit = parse_bucket_title(title)
        outcomes = json.loads(mk.get("outcomes") or '["Yes","No"]')
        tokens = json.loads(mk.get("clobTokenIds") or "[]")
        if len(tokens) != 2:
            continue
        tok = dict(zip(outcomes, tokens))
        buckets.append(Bucket(title, lo, hi, tok["Yes"], tok["No"], str(mk.get("id")),
                              bool(mk.get("closed"))))
    # Every description mentions both units ("To toggle between Fahrenheit and
    # Celsius"), so read the unit from the sentence that names what is measured.
    if m := re.search(r"(?:whole degrees|in degrees) (Celsius|Fahrenheit)", desc):
        unit = m.group(1)[0]
    return TempEvent(
        slug=ev["slug"], title=ev["title"], kind=kind,
        station=station_from_source(src, desc), unit=unit,
        date=date_from_slug(ev["slug"]),
        hourly_only="Hourly Data" in desc,
        fee_rate=fee_rate, source_url=src, buckets=buckets)


def order_book(token_id):
    """Asks sorted cheapest first: [(price, size), ...]."""
    book = get(f"{config.CLOB}/book", {"token_id": token_id})
    asks = sorted((float(a["price"]), float(a["size"])) for a in book.get("asks", []))
    bids = sorted(((float(b["price"]), float(b["size"])) for b in book.get("bids", [])),
                  reverse=True)
    return asks, bids
