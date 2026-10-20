"""Unit tests for the pure helpers in btc5m_sim.sim (no network)."""
import csv, os, tempfile, unittest

from btc5m_sim import sim


class VariantParsing(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(sim.parse_variant("w30_p98"), ("w30_p98", 30, 0.98, 0.99, None))
        self.assertEqual(sim.parse_variant(" w45_p99 "), ("w45_p99", 45, 0.99, 0.99, None))
        self.assertEqual(sim.parse_variant("w45_p98_s"), ("w45_p98_s", 45, 0.98, 0.99, 0.0))
        self.assertEqual(sim.parse_variant("w45_p98_s10"), ("w45_p98_s10", 45, 0.98, 0.99, 10.0))
        self.assertEqual(sim.parse_variant("w60_m95_s20"), ("w60_m95_s20", 60, 0.5, 0.95, 20.0))   # price cap: favourite at 50..95c

    def test_cap_variant_picks_favourite_only(self):
        asks = [(0.91, 300.0, 273.0), (0.10, 2000.0, 200.0)]
        self.assertEqual(sim.evaluate(asks, 0.5, 0.95, 5), (0, 0.91, 300.0, 273.0))      # Up at 91c qualifies
        self.assertIsNone(sim.evaluate([(0.97, 300.0, 291.0), (0.03, 10.0, 0.3)], 0.5, 0.95, 5))  # 97c is above the cap

    def test_defaults(self):
        names = [n for n, _, _, _, _ in sim.VARIANTS]
        self.assertEqual(names, ["w45_p98", "w30_p98", "w20_p98", "w45_p99", "w30_p99", "w45_p98_s", "w45_p98_s10", "w45_p98_s20", "w45_p98_s30", "w60_m95_s10", "w60_m95_s20"])

    def test_dist_ok(self):
        self.assertTrue(sim.dist_ok("Up", 9.4, 0)); self.assertFalse(sim.dist_ok("Down", 9.4, 0))   # the 16:50 loss: Down bought while spot was +9.4
        self.assertTrue(sim.dist_ok("Down", -12.0, 10)); self.assertFalse(sim.dist_ok("Down", -9.9, 10))
        self.assertFalse(sim.dist_ok("Up", None, 0)); self.assertFalse(sim.dist_ok("Up", 0.0, 0))


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


class ChainFields(unittest.TestCase):
    def test_finalize_chain(self):
        v = dict(chain_at_entry=81580.0)
        sim.finalize_chain(v, [(1, 81570.0, 100, 1), (2, 81575.0, 130, 2)], 81586.5, 81570.0)
        self.assertEqual((v["chain_at_start"], v["chain_ptb_err"], v["chain_dist_at_entry"], v["chain_samples"]), (81570.0, -16.5, -6.5, 2))
        nv = {}; sim.finalize_chain(nv, [], None, None)
        self.assertEqual((nv["chain_ptb_err"], nv["chain_dist_at_entry"], nv["chain_samples"]), (None, None, 0))

    def test_versioned_path_rotates_on_header_change(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "v.csv")
            sim.append_rows(p, ["a", "b"], [{"a": 1, "b": 2}])
            self.assertEqual(sim.versioned_path(p, ["a", "b"]), p)                      # same header: keep appending
            p2 = sim.versioned_path(p, ["a", "b", "c"]); self.assertTrue(p2.endswith("v_v2.csv"))
            sim.append_rows(p, ["a", "b", "c"], [{"a": 1, "b": 2, "c": 3}])            # lands in v_v2.csv
            with open(p) as f: self.assertEqual(f.readline().strip(), "a,b")
            with open(p2) as f: self.assertEqual(f.readline().strip(), "a,b,c")
            self.assertEqual(sim.versioned_path(p, ["a", "b", "c"]), p2)               # reuses v2 next time


class CsvWriter(unittest.TestCase):
    def test_append_rows_header_once_and_none_blank(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "v.csv")
            sim.append_rows(p, ["a", "b"], [{"a": 1, "b": None}]); sim.append_rows(p, ["a", "b"], [{"a": 2}])
            with open(p) as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(rows, [{"a": "1", "b": ""}, {"a": "2", "b": ""}])


class DashboardCurve(unittest.TestCase):
    def test_favorite(self):
        from btc5m_sim import push_dashboard as pd
        self.assertEqual(pd.favorite(0.98, 500.0, 0.03, 10.0), (0.98, 500.0))
        self.assertEqual(pd.favorite(0.02, 10.0, 0.97, 300.0), (0.97, 300.0))
        self.assertEqual(pd.favorite(None, 0.0, 0.01, 5.0), (None, None))      # favourite (Up) has no ask
        self.assertEqual(pd.favorite(None, 0.0, 0.96, 50.0), (0.96, 50.0))     # only Down quoted, and it is the favourite
        self.assertEqual(pd.favorite(None, 0.0, None, 0.0), (None, None))

    def test_bucket(self):
        from btc5m_sim import push_dashboard as pd
        self.assertEqual([pd.bucket(x) for x in (44.6, 45.0, 45.1, 150.0, 150.4, 4.9, 0.0)], [45, 45, 50, 150, None, 5, None])

    def test_book_curve(self):
        from btc5m_sim import push_dashboard as pd
        rows = []
        def add(slug, secs, ua, da, uu=100.0, du=100.0):
            rows.append(dict(slug=slug, secs_left=str(secs), up_ask="" if ua is None else str(ua), up_usd=str(uu), down_ask="" if da is None else str(da), down_usd=str(du)))
        add("m1", 120.0, 0.95, 0.06); add("m1", 100.0, 0.98, 0.03); add("m1", 44.0, None, 0.01)   # asks vanish after 100s
        add("m2", 120.0, 0.90, 0.11); add("m2", 44.0, 0.99, 0.02); add("m2", 10.0, 0.99, 0.01)   # asks all the way down
        curve, d = pd.book_curve(rows)
        b120 = next(c for c in curve if c["secs"] == 120); b45 = next(c for c in curve if c["secs"] == 45)
        self.assertEqual((b120["markets"], b120["has_ask_pct"], b120["median_ask"]), (2, 100.0, 0.95))
        self.assertEqual((b45["markets"], b45["has_ask_pct"], b45["median_ask"]), (2, 50.0, 0.99))
        self.assertEqual((d["markets"], d["never"], d["s60_102"], d["under_30"], d["with_98"]), (2, 0, 1, 1, 2))
        self.assertEqual(d["vanish_median"], 100.0)


if __name__ == "__main__":
    unittest.main()
