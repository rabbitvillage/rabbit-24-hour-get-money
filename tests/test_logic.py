"""Offline tests: python -m unittest discover -s tests"""
import datetime as dt
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from weather_bot import allocator as A, backtest as B, cities, detector, observations as O, polymarket as P  # noqa: E402

UTC = dt.timezone.utc


def tokyo_event():
    titles = ["17°C or below"] + [f"{t}°C" for t in range(18, 27)] + ["27°C or higher"]
    return {
        "slug": "highest-temperature-in-tokyo-on-october-3-2026",
        "title": "Highest temperature in Tokyo on October 3?",
        "resolutionSource": "https://www.weather.gov/wrh/timeseries?site=rjtt",
        "description": "highest temperature recorded by NOAA at the Tokyo Haneda Airport "
                       "Station in degrees Celsius ... for all times on this day",
        "markets": [{"id": i, "groupItemTitle": t, "outcomes": '["Yes","No"]',
                     "clobTokenIds": json.dumps([f"y{i}", f"n{i}"]),
                     "feeSchedule": {"rate": 0.05}} for i, t in enumerate(titles)],
    }


def metar(hhmm, temp, day=3):
    t = dt.datetime(2026, 10, day, int(hhmm[:2]), int(hhmm[2:]), tzinfo=UTC)
    raw = f"METAR RJTT {day:02d}{hhmm}Z 02007KT 9999 BKN060 {temp:02d}/13 Q1021"
    return O.Obs(t, float(temp), False, False, raw)


class Parsing(unittest.TestCase):
    def test_titles(self):
        self.assertEqual(P.parse_bucket_title("17°C or below"), (None, 17.0, "C"))
        self.assertEqual(P.parse_bucket_title("62-63°F"), (62.0, 63.0, "F"))
        self.assertEqual(P.parse_bucket_title("80°F or higher"), (80.0, None, "F"))
        self.assertEqual(P.parse_bucket_title("22°C"), (22.0, 22.0, "C"))

    def test_station(self):
        self.assertEqual(P.station_from_source("https://www.weather.gov/wrh/timeseries?site=klga"), "KLGA")
        self.assertEqual(P.station_from_source("https://www.wunderground.com/history/daily/cn/jinan/ZSJN"), "ZSJN")

    def test_metar_tgroup(self):
        self.assertEqual(O.parse_metar_temp("KLGA 031351Z 19/10 A3024 RMK T01890100"), (18.9, True))
        self.assertEqual(O.parse_metar_temp("RJTT 031430Z 9999 M02/M05 Q1021"), (-2, False))


class Detection(unittest.TestCase):
    def setUp(self):
        self.ev = P.parse_event(tokyo_event())

    def test_user_example(self):
        # 25 °C observed at 05:00Z (14:00 JST): 24 and below are dead, 27+ still open.
        obs = [metar("0300", 22), metar("0400", 24), metar("0500", 25)]
        now = dt.datetime(2026, 10, 3, 5, 5, tzinfo=UTC)
        st = O.day_state(obs, self.ev.date, "Asia/Tokyo", "C", now=now)
        self.assertEqual(st.extreme, 25)
        dead = {b.title: s for b, s in detector.dead_buckets(self.ev, st)}
        self.assertEqual(dead["24°C"], "NO")
        self.assertEqual(dead["17°C or below"], "NO")
        self.assertNotIn("25°C", dead)
        self.assertNotIn("26°C", dead)
        books = {"n7": [(0.96, 500), (0.98, 300), (0.995, 1000)]}   # bucket "24°C"
        opps = detector.evaluate(self.ev, st, books)
        o = [x for x in opps if x.bucket == "24°C"][0]
        self.assertEqual(o.shares, 800)
        self.assertGreater(o.profit, 0)

    def test_local_day_boundary(self):
        # 2 Oct 16:00Z is 3 Oct 01:00 JST and counts; 2 Oct 14:00Z (23:00 JST 2 Oct) does not.
        obs = [metar("1400", 30, day=2), metar("1600", 15, day=2)]
        st = O.day_state(obs, self.ev.date, "Asia/Tokyo", "C",
                         now=dt.datetime(2026, 10, 2, 17, tzinfo=UTC))
        self.assertEqual(st.extreme, 15)

    def test_fahrenheit_is_conservative(self):
        o = O.Obs(dt.datetime(2026, 10, 3, tzinfo=UTC), 21.1, True, False)   # 69.98 °F
        self.assertEqual(O.guaranteed_value(o, "F", "high"), 70)
        o = O.Obs(dt.datetime(2026, 10, 3, tzinfo=UTC), 21.0, False, False)  # 21 ±0.5 °C
        self.assertEqual(O.guaranteed_value(o, "F", "high"), 69)


class Cities(unittest.TestCase):
    def test_table(self):
        self.assertEqual(len(cities.MODULES), 49)
        self.assertEqual(len({m.station for m in cities.MODULES}), 49)
        for m in cities.MODULES:
            O.local_day_window(dt.date(2026, 10, 4), m.tz)   # valid timezone
        self.assertEqual(cities.BY_NAME["paris"].station, "LFPB")


def opp(city, token, roi, asks, single=False, suspicious=False):
    return detector.Opportunity("e", "24°C", "NO", token, 25, asks[0][0], 0, 0, 0, 0, roi, "",
                                city=city, date=dt.date(2026, 10, 3), asks=asks,
                                single_report=single, suspicious=suspicious)


class Allocation(unittest.TestCase):
    now = dt.datetime(2026, 10, 3, 5, tzinfo=UTC)

    def test_caps(self):
        lim = A.Limits(bankroll=1000)
        big = [(0.96, 10000)]
        orders = A.plan([opp("Tokyo", "a", 0.04, big), opp("Tokyo", "b", 0.03, big),
                         opp("NYC", "c", 0.02, big)], A.Ledger(1000), lim, self.now)
        by = {o["token"]: o["dollars"] for o in orders}
        self.assertAlmostEqual(by["a"], 80, delta=1)          # 8% bucket cap
        self.assertAlmostEqual(by["b"], 70, delta=1)          # Tokyo city-day cap 15%
        self.assertAlmostEqual(by["c"], 80, delta=1)

    def test_depth_and_flags(self):
        orders = A.plan([opp("Tokyo", "a", 0.04, [(0.96, 20), (0.995, 999)]),
                         opp("NYC", "s", 0.04, [(0.96, 999)], suspicious=True),
                         opp("London", "h", 0.04, [(0.96, 999)], single=True)],
                        A.Ledger(1000), A.Limits(bankroll=1000), self.now)
        by = {o["token"]: o for o in orders}
        self.assertEqual(by["a"]["shares"], 20)               # only depth <= 0.99
        self.assertNotIn("s", by)                             # suspicious jump skipped
        self.assertAlmostEqual(by["h"]["dollars"], 40, delta=1)   # half cap

    def test_reserve(self):
        led = A.Ledger(150)
        orders = A.plan([opp(c, c, 0.04, [(0.96, 999)]) for c in "ABCDE"], led,
                        A.Limits(bankroll=1000), self.now)
        self.assertLessEqual(sum(o["dollars"] for o in orders), 50.01)


class Backtest(unittest.TestCase):
    def test_death_times_and_classify(self):
        ev = P.parse_event(tokyo_event())
        obs = [metar("0300", 22), metar("0400", 24), metar("0500", 25), metar("0600", 24)]
        dead, day_max = B.death_times(ev, obs, "Asia/Tokyo")
        self.assertEqual(day_max, 25)
        titles = {b.title: t for b, t in dead.items()}
        self.assertEqual(titles["24°C"], dt.datetime(2026, 10, 3, 5, 5, tzinfo=UTC))
        self.assertEqual(titles["23°C"], dt.datetime(2026, 10, 3, 4, 5, tzinfo=UTC))
        self.assertNotIn("25°C", titles)
        self.assertEqual(B.classify({"outcome": "No", "side": "BUY", "price": 0.96}), (0.04, "buy_no"))
        self.assertEqual(B.classify({"outcome": "Yes", "side": "SELL", "price": 0.03}), (0.03, "sell_yes"))
        self.assertIsNone(B.classify({"outcome": "No", "side": "BUY", "price": 0.995}))
        self.assertIsNone(B.classify({"outcome": "No", "side": "SELL", "price": 0.96}))

    def test_summary(self):
        days = [{"city": "Tokyo", "date": "2026-10-02", "pickup_profit": 40.0, "event_volume": 1000.0,
                 "within_5min": 30.0, "within_30min": 40.0}]
        trades = [{"kind": "buy_no", "price": 0.96, "profit": 40.0, "wallet": "0xabc"}]
        s = B.summarize(days, trades, [])
        self.assertEqual(s["pickup_share_of_volume"], 0.04)
        self.assertEqual(s["top5_wallet_share"], 1.0)


if __name__ == "__main__":
    unittest.main()
