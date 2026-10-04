"""One module per city.

Every city runs the same detection code; what differs is its station, timezone,
unit, observation source and rule quirks. Those live in one CityModule each, so
adding a city is one line, and a city with special rules (Hong Kong, Taipei)
can subclass and override a single method.

Station data verified against each market's rules on 2026-10-03 (Oct 4 events).
"""
import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from . import detector, observations, polymarket


@dataclass
class CityModule:
    city: str
    slug: str            # as used in Polymarket event slugs
    station: str         # station named in the resolution rules
    tz: str
    source: str          # "metar" or "hko"
    unit: str            # "C" or "F"
    hourly_only: bool    # rules count only the "Hourly Data" rows
    note: str = ""

    def event_slugs(self, date):
        d = f"{date:%B}".lower() + f"-{date.day}-{date.year}"
        return [f"highest-temperature-in-{self.slug}-on-{d}",
                f"lowest-temperature-in-{self.slug}-on-{d}"]

    def local_today(self, now):
        return now.astimezone(ZoneInfo(self.tz)).date()

    def observations(self):
        if self.source != "metar":
            raise NotImplementedError(f"{self.city}: {self.source} source not wired yet")
        return observations.metar_obs(self.station)

    def scan(self, now=None, budget=None):
        """Opportunities for local today and yesterday (yesterday is fully decided
        but may still be open). Returns (opportunities, warnings)."""
        now = now or dt.datetime.now(dt.timezone.utc)
        today = self.local_today(now)
        opps, warn, obs = [], [], None
        for date in (today - dt.timedelta(days=1), today):
            for slug in self.event_slugs(date):
                raw = polymarket.event_by_slug(slug)
                if not raw or raw.get("closed"):
                    continue
                ev = polymarket.parse_event(raw)
                if ev.station != self.station:
                    warn.append(f"{slug}: rules name {ev.station}, module expects "
                                f"{self.station}; skipped")
                    continue
                if obs is None:
                    obs = self.observations()
                state = observations.day_state(obs, ev.date, self.tz, ev.unit, ev.kind,
                                               ev.hourly_only or self.hourly_only, now)
                books = {}
                for b, side in detector.dead_buckets(ev, state):
                    tok = b.no_token if side == "NO" else b.yes_token
                    books[tok] = polymarket.order_book(tok)[0]
                for o in detector.evaluate(ev, state, books, budget):
                    o.city, o.date, o.asks = self.city, ev.date, books[o.token]
                    opps.append(o)
        return opps, warn


MODULES = [
    CityModule("Wellington", "wellington", "NZWN", "Pacific/Auckland", "metar", "C", False),
    CityModule("London", "london", "EGLC", "Europe/London", "metar", "C", False),
    CityModule("Singapore", "singapore", "WSSS", "Asia/Singapore", "metar", "C", False),
    CityModule("Shanghai", "shanghai", "ZSPD", "Asia/Shanghai", "metar", "C", False),
    CityModule("Warsaw", "warsaw", "EPWA", "Europe/Warsaw", "metar", "C", False),
    CityModule("Hong Kong", "hong-kong", "HKO", "Asia/Hong_Kong", "hko", "C", False, note="HKO Daily Extract, 0.1 C, published next day"),
    CityModule("Chongqing", "chongqing", "ZUCK", "Asia/Shanghai", "metar", "C", False),
    CityModule("Shenzhen", "shenzhen", "ZGSZ", "Asia/Shanghai", "metar", "C", False),
    CityModule("Ankara", "ankara", "LTAC", "Europe/Istanbul", "metar", "C", False),
    CityModule("Cape Town", "cape-town", "FACT", "Africa/Johannesburg", "metar", "C", False),
    CityModule("Wuhan", "wuhan", "ZHHH", "Asia/Shanghai", "metar", "C", False),
    CityModule("Munich", "munich", "EDDM", "Europe/Berlin", "metar", "C", False),
    CityModule("Madrid", "madrid", "LEMD", "Europe/Madrid", "metar", "C", False),
    CityModule("Seoul", "seoul", "RKSI", "Asia/Seoul", "metar", "C", False),
    CityModule("Guangzhou", "guangzhou", "ZGGG", "Asia/Shanghai", "metar", "C", False),
    CityModule("Los Angeles", "los-angeles", "KLAX", "America/Los_Angeles", "metar", "F", True),
    CityModule("Atlanta", "atlanta", "KATL", "America/New_York", "metar", "F", True),
    CityModule("Paris", "paris", "LFPB", "Europe/Paris", "metar", "C", False),
    CityModule("Tel Aviv", "tel-aviv", "LLBG", "Asia/Jerusalem", "metar", "C", False),
    CityModule("Qingdao", "qingdao", "ZSQD", "Asia/Shanghai", "metar", "C", False),
    CityModule("Chicago", "chicago", "KORD", "America/Chicago", "metar", "F", True),
    CityModule("Helsinki", "helsinki", "EFHK", "Europe/Helsinki", "metar", "C", False),
    CityModule("Tokyo", "tokyo", "RJTT", "Asia/Tokyo", "metar", "C", False),
    CityModule("Amsterdam", "amsterdam", "EHAM", "Europe/Amsterdam", "metar", "C", False),
    CityModule("NYC", "nyc", "KLGA", "America/New_York", "metar", "F", True),
    CityModule("Denver", "denver", "KBKF", "America/Denver", "metar", "F", True),
    CityModule("Busan", "busan", "RKPK", "Asia/Seoul", "metar", "C", False),
    CityModule("San Francisco", "san-francisco", "KSFO", "America/Los_Angeles", "metar", "F", True),
    CityModule("Milan", "milan", "LIMC", "Europe/Rome", "metar", "C", False),
    CityModule("Beijing", "beijing", "ZBAA", "Asia/Shanghai", "metar", "C", False),
    CityModule("Miami", "miami", "KMIA", "America/New_York", "metar", "F", True),
    CityModule("Kuala Lumpur", "kuala-lumpur", "WMKK", "Asia/Kuala_Lumpur", "metar", "C", False),
    CityModule("Manila", "manila", "RPLL", "Asia/Manila", "metar", "C", False),
    CityModule("Moscow", "moscow", "UUWW", "Europe/Moscow", "metar", "C", False),
    CityModule("Jeddah", "jeddah", "OEJN", "Asia/Riyadh", "metar", "C", False),
    CityModule("Houston", "houston", "KHOU", "America/Chicago", "metar", "F", True),
    CityModule("Lucknow", "lucknow", "VILK", "Asia/Kolkata", "metar", "C", False),
    CityModule("Austin", "austin", "KAUS", "America/Chicago", "metar", "F", True),
    CityModule("Dallas", "dallas", "KDAL", "America/Chicago", "metar", "F", True),
    CityModule("Mexico City", "mexico-city", "MMMX", "America/Mexico_City", "metar", "C", False),
    CityModule("Karachi", "karachi", "OPKC", "Asia/Karachi", "metar", "C", False),
    CityModule("Panama City", "panama-city", "MPMG", "America/Panama", "metar", "C", False),
    CityModule("Istanbul", "istanbul", "LTFM", "Europe/Istanbul", "metar", "C", False),
    CityModule("Buenos Aires", "buenos-aires", "SAEZ", "America/Argentina/Buenos_Aires", "metar", "C", False),
    CityModule("Taipei", "taipei", "RCSS", "Asia/Taipei", "metar", "C", False, note="resolves on Wunderground history page, not NOAA"),
    CityModule("Chengdu", "chengdu", "ZUUU", "Asia/Shanghai", "metar", "C", False),
    CityModule("Toronto", "toronto", "CYYZ", "America/Toronto", "metar", "C", False),
    CityModule("Sao Paulo", "sao-paulo", "SBGR", "America/Sao_Paulo", "metar", "C", False),
    CityModule("Seattle", "seattle", "KSEA", "America/Los_Angeles", "metar", "F", True),
]

BY_NAME = {m.city.lower(): m for m in MODULES}
