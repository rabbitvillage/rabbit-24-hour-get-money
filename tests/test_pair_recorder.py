"""pair_sim.recorder pure helpers (no network)."""
import csv, gzip, json, os, tempfile, unittest
from pair_sim import recorder as R


class Window(unittest.TestCase):
    def test_starts_to_watch(self):
        # 10 s before an open: the market in progress and the next one (PRE 180)
        self.assertEqual(R.starts_to_watch(1000 * 300 - 10, pre=180), [999 * 300, 1000 * 300])
        # 200 s before the next open: only the market in progress
        self.assertEqual(R.starts_to_watch(1000 * 300 - 200, pre=180), [999 * 300])
        # right at an open: the new market only (the old one has just closed)
        self.assertEqual(R.starts_to_watch(1000 * 300, pre=180), [1000 * 300])


class Top(unittest.TestCase):
    def test_top_of_book(self):
        b = {"bids": [{"price": "0.49", "size": 100.0}, {"price": "0.48", "size": 50.0}],
             "asks": [{"price": "0.51", "size": 30.0}, {"price": "0.52", "size": 20.0}, {"price": "0.54", "size": 9.0}]}
        self.assertEqual(R.top(b), (0.49, 100.0, 0.51, 30.0, 50.0))
        self.assertEqual(R.top(None), (None, 0.0, None, 0.0, 0.0))
        self.assertEqual(R.top({"bids": [], "asks": []}), (None, 0.0, None, 0.0, 0.0))


class Trades(unittest.TestCase):
    def test_trade_prints_reach_the_callback(self):
        got = []
        f = R.TradeBookFeed(["111", "222"], lambda tok, side, p, s, now: got.append((tok, side, p, s)))
        f.on_message(json.dumps({"event_type": "last_trade_price", "asset_id": "222", "side": "BUY", "price": "0.47", "size": "12.5"}), now=1.0)
        f.on_message(json.dumps({"event_type": "book", "asset_id": "111", "bids": [], "asks": [{"price": "0.51", "size": "5"}]}), now=1.0)
        self.assertEqual(got, [("222", "BUY", 0.47, 12.5)])
        self.assertEqual(f.book_now("111", now=1.5)[0]["asks"], [{"price": "0.51", "size": 5.0}])


class WriterRollover(unittest.TestCase):
    def test_daily_files_and_gzip(self):
        with tempfile.TemporaryDirectory() as d:
            w = R.Writer(d)
            w.write("book_btc", ["a"], {"a": 1}, day="20261029")
            w.write("book_btc", ["a"], {"a": 2}, day="20261029")
            w.write("book_btc", ["a"], {"a": 3}, day="20261030")      # new day: yesterday gets gzipped
            self.assertTrue(os.path.exists(os.path.join(d, "book_btc_20261029.csv.gz")))
            with gzip.open(os.path.join(d, "book_btc_20261029.csv.gz"), "rt") as f:
                self.assertEqual([r["a"] for r in csv.DictReader(f)], ["1", "2"])
            w.write("markets", ["slug"], {"slug": "x"})
            self.assertTrue(os.path.exists(os.path.join(d, "markets.csv")))
