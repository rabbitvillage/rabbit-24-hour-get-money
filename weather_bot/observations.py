"""Live observations per station, reduced to what the resolution source would show.

Only *observed* readings are used. A forecast can never make a "No" certain;
the day's running maximum can only go up, so once a reading is in, every
bucket entirely below it is dead.
"""
import csv
import datetime as dt
import io
import math
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from . import config
from .http import get


@dataclass
class Obs:
    time: dt.datetime        # UTC
    temp_c: float            # best precision available (T-group if present)
    precise: bool            # True if temp_c came from a 0.1 °C T-group
    speci: bool              # special (non-routine) report
    raw: str = ""


_TGROUP = re.compile(r"\bT([01])(\d{3})([01])(\d{3})\b")
_MAIN = re.compile(r"\s(M?\d{2})/(M?\d{2})?\s")


def parse_metar_temp(raw):
    """(temp_c, precise) from a raw METAR string, or (None, False)."""
    if m := _TGROUP.search(raw):
        v = int(m.group(2)) / 10
        return (-v if m.group(1) == "1" else v), True
    if m := _MAIN.search(" " + raw + " "):
        s = m.group(1)
        return (-int(s[1:]) if s.startswith("M") else int(s)), False
    return None, False


def metar_obs(icao, hours=36):
    rows = get(config.AWC_METAR, {"ids": icao, "format": "json", "hours": hours})
    out = []
    for r in rows:
        raw = r.get("rawOb", "")
        t, precise = parse_metar_temp(raw)
        if t is None and r.get("temp") is not None:
            t, precise = float(r["temp"]), False
        if t is None:
            continue
        when = dt.datetime.fromtimestamp(int(r["obsTime"]), dt.timezone.utc)
        speci = (r.get("metarType") == "SPECI") or raw.startswith("SPECI")
        out.append(Obs(when, float(t), precise, speci, raw))
    return sorted(out, key=lambda o: o.time)


def hko_since_midnight():
    """HKO 'since midnight' max/min at the Observatory (unverified column layout:
    check against the live CSV before trusting)."""
    text = get(config.HKO_MAXMIN, as_json=False)
    for row in csv.reader(io.StringIO(text.lstrip("﻿"))):
        if row and "Hong Kong Observatory" in row[1:2] + row[0:1]:
            nums = [float(x) for x in row if re.fullmatch(r"-?\d+(\.\d+)?", x.strip())]
            if len(nums) >= 2:
                return {"max": nums[-2], "min": nums[-1]}
    return None


def local_day_window(date, tz):
    z = ZoneInfo(tz)
    start = dt.datetime.combine(date, dt.time(0), z)
    return start.astimezone(dt.timezone.utc), (start + dt.timedelta(days=1)).astimezone(dt.timezone.utc)


def _c_to_f(c):
    return c * 9 / 5 + 32


def _round_half_up(x):
    return math.floor(x + 0.5)


def guaranteed_value(o, unit, kind):
    """The value the resolution source must show for this report, taken at the
    least favourable end of the measurement's rounding uncertainty.
    kind="high": smallest value it could display; "low": largest."""
    if unit == "C":
        # Source displays whole °C, same integer the METAR main group carries.
        if o.precise:
            return _round_half_up(o.temp_c)
        return o.temp_c
    # °F: source converts °C -> °F and shows whole degrees.
    half = 0.05 if o.precise else 0.5
    edge = o.temp_c - half if kind == "high" else o.temp_c + half
    return _round_half_up(_c_to_f(edge))


@dataclass
class DayState:
    extreme: float | None    # guaranteed running max (or min) in market units
    n_reports: int
    confirmations: int       # reports at that extreme
    spike: bool              # extreme set by a suspicious jump
    day_complete: bool
    last_report: dt.datetime | None


def day_state(obs, date, tz, unit, kind="high", hourly_only=False, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    start, end = local_day_window(date, tz)
    day = [o for o in obs if start <= o.time < end and not (hourly_only and o.speci)]
    if not day:
        return DayState(None, 0, 0, False, now >= end, None)
    vals = [guaranteed_value(o, unit, kind) for o in day]
    pick = max if kind == "high" else min
    ext = pick(vals)
    idx = vals.index(ext)
    spike = idx > 0 and abs(day[idx].temp_c - day[idx - 1].temp_c) >= config.SPIKE_GUARD
    return DayState(ext, len(day), vals.count(ext), spike,
                    now >= end and day[-1].time >= end - dt.timedelta(hours=2),
                    day[-1].time)
