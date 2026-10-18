"""Unit tests for the pure helpers in btc5m_sim.sim (no network)."""
import csv, os, tempfile, unittest

from btc5m_sim import sim


class VariantParsing(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(sim.parse_variant("w30_p98"), ("w30_p98", 30, 0.98))
        self.assertEqual(sim.parse_variant(" w45_p99 "), ("w45_p99", 45, 0.99))

    def test_defaults(self):
        names = [n for n, _, _ in sim.VARIANTS]
        self.assertEqual(names, ["w45_p98", "w30_p98", "w20_p98", "w45_p99", "w30_p99"])


class BookLogic(unittest.TestCase):
    def test_best_ask(self):
        book = {"asks": [{"price": "0.99", "size": "10"}, {"price": "0.98", "size": "3"}, {"price": "1", "size": "500"}]}
        ask, sh, usd = sim.best_ask(book, 0.99)
        self.assertEqual(ask, 0.98); self.assertEqual(sh, 13); self.assertAlmostEqual(usd, 0.98 * 3 + 0.99 * 10)
        self.assertEqual(sim.best_ask({"asks": []}, 0.99), (None, 0.0, 0.0))

    def test_evaluate_first_matching_side(self):
        asks = [(0.01, 1000, 10), (0.98, 20, 19.6)]
        self.assertEqual(sim.evaluate(asks, 0.98, 0.99, 5), (1, 0.98, 20, 19.6))
        self.assertIsNone(sim.evaluate(asks, 0.99, 0.99, 5))        # 99c-only variant: 98c is too cheap
        self.assertIsNone(sim.evaluate([(0.98, 4, 3.9), (None, 0, 0)], 0.98, 0.99, 5))  # not enough shares
        self.assertIsNone(sim.evaluate([(None, 0, 0), (None, 0, 0)], 0.98, 0.99, 5))

    def test_fill_and_score(self):
        row = dict(entered=0)
        sim.fill(row, (0, 0.98, 20, 19.6), ["Up", "Down"], [(0.98, 20, 19.6), (0.03, 50, 1.5)], 30.5, 340)
        self.assertEqual(row["side"], "Up"); self.assertEqual(row["other_side_ask"], 0.03)
        self.assertAlmostEqual(row["cost"], 0.98 * sim.SHARES, 4)
        sim.score(row, "Up"); self.assertEqual(row["won"], 1)
        self.assertAlmostEqual(row["pnl"], sim.SHARES - row["cost"] - row["fee"], 4)
        sim.score(row, "Down"); self.assertEqual(row["won"], 0)
        self.assertAlmostEqual(row["pnl"], -row["cost"] - row["fee"], 4)
        empty = dict(entered=0); sim.score(empty, "Up")
        self.assertEqual(empty["winner"], "Up"); self.assertNotIn("won", empty)


class SpotDistance(unittest.TestCase):
    def test_dist(self):
        self.assertEqual(sim.dist(81010.0, 81000.0), 10.0)
        self.assertIsNone(sim.dist(None, 81000.0)); self.assertIsNone(sim.dist(81000.0, None))

    def test_twap_dist_only_counts_samples_inside_window(self):
        end = 1000.0; ptb = 100.0
        samples = [(end - 70, 500.0), (end - 60, 110.0), (end - 30, 120.0), (end - 10, 130.0)]
        self.assertEqual(sim.twap_dist(samples, end, end - 20, ptb), 15.0)   # (110+120)/2 - 100
        self.assertEqual(sim.twap_dist(samples, end, end - 5, ptb), 20.0)    # (110+120+130)/3 - 100
        self.assertIsNone(sim.twap_dist([], end, end - 5, ptb)); self.assertIsNone(sim.twap_dist(samples, end, end - 5, None))

    def test_finalize_dists_backfills_with_late_ptb(self):
        end = 1000.0; samples = [(end - 60, 110.0), (end - 30, 120.0), (end - 10, 130.0)]
        v = dict(entered=1, spot_at_window=112.0, spot_at_entry=125.0, _t_entry=end - 20)
        sim.finalize_dists(v, samples, end, 100.0, 101.5)
        self.assertEqual((v["spot_at_start"], v["ptb_err"]), (101.5, 1.5))
        self.assertEqual((v["dist_at_window"], v["dist_at_entry"]), (12.0, 25.0))
        self.assertEqual(v["twap_dist_at_entry"], 15.0)          # samples at -60 and -30 only
        self.assertEqual((v["min_abs_dist"], v["spot_samples"]), (10.0, 3))
        nv = dict(entered=0, spot_at_window=112.0)
        sim.finalize_dists(nv, samples, end, None, None)         # ptb never published: everything blank
        self.assertTrue(all(nv[k] is None for k in ("ptb_err", "dist_at_window", "dist_at_entry", "twap_dist_at_entry", "min_abs_dist")))
        self.assertEqual(nv["spot_samples"], 3)

    def test_min_abs_dist(self):
        self.assertEqual(sim.min_abs_dist([(0, 105.0), (1, 97.5), (2, 110.0)], 100.0), 2.5)
        self.assertIsNone(sim.min_abs_dist([], 100.0))


class CsvWriter(unittest.TestCase):
    def test_append_rows_header_once_and_none_blank(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "v.csv")
            sim.append_rows(p, ["a", "b"], [{"a": 1, "b": None}]); sim.append_rows(p, ["a", "b"], [{"a": 2}])
            with open(p) as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(rows, [{"a": "1", "b": ""}, {"a": "2", "b": ""}])


if __name__ == "__main__":
    unittest.main()
