"""Unit tests for the pure helpers in btc5m_sim.sim (no network)."""
import csv, os, tempfile, unittest

from btc5m_sim import sim


class VariantParsing(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(sim.parse_variant("w30_p98"), ("w30_p98", 30, 0.98, 0.99, None))
        self.assertEqual(sim.parse_variant(" w45_p99 "), ("w45_p99", 45, 0.99, 0.99, None))
        self.assertEqual(sim.parse_variant("w45_p98_s"), ("w45_p98_s", 45, 0.98, 0.99, 0.0))
        self.assertEqual(sim.parse_variant("w45_p98_s10"), ("w45_p98_s10", 45, 0.98, 0.99, 10.0))
        self.assertEqual(sim.parse_variant("w60_m95_s20"), ("w60_m95_s20", 60, 0.8, 0.95, 20.0))   # price cap: favourite at M_FLOOR(0.80)..95c

    def test_cap_variant_picks_favourite_only(self):
        asks = [(0.91, 300.0, 273.0), (0.10, 2000.0, 200.0)]
        self.assertEqual(sim.evaluate(asks, 0.8, 0.95, 5), (0, 0.91, 300.0, 273.0))      # Up at 91c qualifies
        self.assertIsNone(sim.evaluate([(0.97, 300.0, 291.0), (0.03, 10.0, 0.3)], 0.8, 0.95, 5))  # 97c is above the cap
        self.assertIsNone(sim.evaluate([(0.55, 300.0, 165.0), (0.44, 300.0, 132.0)], 0.8, 0.95, 5))  # 55c "favourite" is a coin flip: below the floor

    def test_defaults(self):
        names = [n for n, _, _, _, _ in sim.VARIANTS]
        self.assertEqual(names, ["w45_p98", "w30_p98", "w20_p98", "w45_p99", "w30_p99", "w45_p98_s", "w45_p98_s10", "w45_p98_s20", "w45_p98_s30", "w60_m95_s10", "w60_m95_s20", "w90_m99_s30", "w120_m99_s40", "w45_m99_s20"])

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


class DecisionLog(unittest.TestCase):
    def test_reasons(self):
        row = dict(winner="Up")
        base = dict(slug="btc-updown-5m-1", start_utc="2026-10-22T05:00:00+00:00", variant="w45_p98_s10", entered=0, _filt=10.0)
        self.assertEqual(sim.decision_row(dict(base, entered=1, ask=0.98, secs_left=44.2, won=1, pnl=0.3), row)["reason"], "entered")
        d = sim.decision_row(dict(base, _block=dict(reason="distance", ask=0.99, secs=44.2, dl=4.2, thr=10.0), _lead_min=0.98, _lead_max=0.99), row)
        self.assertEqual((d["decision"], d["reason"], d["dl"], d["threshold"], d["ask"]), ("skip", "distance", 4.2, 10.0, 0.99))
        self.assertEqual(sim.decision_row(dict(base, _block=dict(reason="direction", ask=0.98, secs=30.0, dl=-7.8, thr=0.0)), row)["reason"], "direction")
        self.assertEqual(sim.decision_row(dict(base, _lead_min=0.991, _lead_max=0.995), row)["reason"], "price_out_of_band")
        self.assertEqual(sim.decision_row(dict(base, _lead_min=0.98, _lead_max=0.99, _depth_short=True), row)["reason"], "depth")
        self.assertEqual(sim.decision_row(dict(base), row)["reason"], "no_ask")
        sim.live._last[("w45_p98_s10", "btc-updown-5m-1")] = ("filled", "ok")
        self.assertEqual(sim.decision_row(dict(base, entered=1, ask=0.98, secs_left=44.2), row)["live_status"], "filled")
        sim.live._last.clear()


if __name__ == "__main__":
    unittest.main()


class AssetParametrisation(unittest.TestCase):
    """ASSET=eth|sol switches market slug, spot feed, Chainlink aggregator and scales the distance thresholds.
    importlib.reload mutates the module in place, so each case reads its values before tearDown reloads BTC."""
    def _load(self, asset):
        import importlib, os
        os.environ["ASSET"] = asset
        importlib.reload(sim)
        return dict(gamma=sim.GAMMA, spot=sim.SPOT_URLS[0][1], feed=sim.CHAIN_FEED, scale=sim.DIST_SCALE,
                    s10=next(v for v in sim.VARIANTS if v[0] == "w45_p98_s10")[4])

    def tearDown(self):
        import importlib, os
        os.environ.pop("ASSET", None)
        importlib.reload(sim)

    def test_eth_and_sol(self):
        for asset, feed, ref in (("eth", "0xF9680D99D6C9589e2a93a78A04A279e509205945", 2435.0), ("sol", "0x10C8264C0935b3B9870013e057f330Ff3e9C56dC", 105.0)):
            m = self._load(asset)
            self.assertIn(f"slug={asset}-updown-5m-", m["gamma"])
            self.assertIn(f"{asset.upper()}-USD", m["spot"])
            self.assertEqual(m["feed"], feed)
            self.assertAlmostEqual(m["scale"], ref / 82000.0, places=6)
            self.assertAlmostEqual(m["s10"], round(10 * ref / 82000.0, 4), places=4)   # $10 at BTC -> same fraction of price

    def test_btc_unchanged(self):
        m = self._load("btc")
        self.assertEqual(m["scale"], 1.0)
        self.assertIn("slug=btc-updown-5m-", m["gamma"])
        self.assertEqual(m["s10"], 10.0)


class ChopFilter(unittest.TestCase):
    """Entries are refused when the market has already swung >= CHOP_MAX_AMP_BPS of the line before entry."""
    def test_amp_bps(self):
        self.assertIsNone(sim.amp_bps(None, None, 81000))
        self.assertAlmostEqual(sim.amp_bps(81300, 81381, 81000), 10.0, places=6)
    def test_block_and_pass(self):
        old = sim.CHOP_MAX_AMP_BPS; sim.CHOP_MAX_AMP_BPS = 8.0
        try:
            hit = (1, 0.9, 50.0, 45.0)
            b = sim.chop_block(hit, 21.9, 89.5)
            self.assertEqual((b["reason"], b["ask"], b["secs"], b["dl"], b["thr"]), ("chop", 0.9, 89.5, 21.9, 8.0))
            self.assertIsNone(sim.chop_block(hit, 7.9, 89.5))
            self.assertIsNone(sim.chop_block(hit, None, 89.5))      # no spot yet -> not blocked by chop
            self.assertIsNone(sim.chop_block(None, 50.0, 89.5))     # nothing to buy anyway
            sim.CHOP_MAX_AMP_BPS = 0.0
            self.assertIsNone(sim.chop_block(hit, 50.0, 89.5))      # 0 = off
        finally:
            sim.CHOP_MAX_AMP_BPS = old
    def test_decision_row_and_state_text(self):
        v = dict(slug="btc-updown-5m-1", start_utc="2026-10-22T13:50:00+00:00", variant="w90_m99_s30", window=90, min_price=0.8, entered=0, _filt=30.0, _max=0.99,
                 _block=dict(reason="chop", ask=0.88, secs=89.5, dl=21.9, thr=8.0))
        d = sim.decision_row(v, dict(winner="Up"))
        self.assertEqual((d["decision"], d["reason"], d["dl"], d["threshold"]), ("skip", "chop", 21.9, 8.0))
        old = sim.CHOP_MAX_AMP_BPS; sim.CHOP_MAX_AMP_BPS = 8.0
        try:
            txt = sim.rule_state(dict(v, _filt=None), [(0.05, 100, 5), (0.88, 100, 88)], 0.88, 89.5, lambda: -102.0, ["Up", "Down"], lambda: 21.9)
            self.assertIn("震盪盤", txt)
        finally:
            sim.CHOP_MAX_AMP_BPS = old


class TelegramChats(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(sim.tg_chats("555981860,5891236715"), ["555981860", "5891236715"])
        self.assertEqual(sim.tg_chats(" 555 ; 555 ,, 42 "), ["555", "42"])
        self.assertEqual(sim.tg_chats(""), []); self.assertEqual(sim.tg_chats(None), [])
    def test_send_to_each_chat_and_survive_one_failure(self):
        sent = []
        def fake_urlopen(req, timeout=10):
            body = req.data.decode()
            if "chat_id=bad" in body: raise OSError("403")
            sent.append(body); return None
        old = (sim.urllib.request.urlopen, sim.TG_TOKEN, sim.TG_CHAT)
        sim.urllib.request.urlopen = fake_urlopen; sim.TG_TOKEN = "t"; sim.TG_CHAT = "a,bad,b"
        try:
            sim.tg("hello")
        finally:
            sim.urllib.request.urlopen, sim.TG_TOKEN, sim.TG_CHAT = old
        self.assertEqual(len(sent), 2)
        self.assertTrue(any("chat_id=a" in s for s in sent) and any("chat_id=b" in s for s in sent))


class PassedTooExpensive(unittest.TestCase):
    def test_decision_row_and_summary(self):
        import tempfile, os, csv
        v = dict(slug="btc-updown-5m-1", start_utc="2026-10-26T05:15:00+00:00", variant="w60_m95_s20", window=60, min_price=0.8, entered=0, _filt=20.0, _max=0.95,
                 _lead_min=0.99, _lead_max=0.99, _expensive=dict(side="Down", ask=0.99, secs=60.0))
        d = sim.decision_row(v, dict(winner="Down"))
        self.assertEqual((d["decision"], d["reason"], d["ask"], d["secs_left"]), ("skip", "too_expensive:Down", 0.99, 60.0))
        with tempfile.TemporaryDirectory() as tmp:
            old = sim.DATA; sim.DATA = tmp
            try:
                with open(os.path.join(tmp, "sim_decisions.csv"), "w", newline="") as f:
                    w = csv.DictWriter(f, sim.DFIELDS); w.writeheader()
                    w.writerow(dict(d, winner="Down")); w.writerow(dict(d, slug="btc-updown-5m-2", winner="Up"))
                    w.writerow(dict(d, slug="btc-updown-5m-2", variant="w45_m95_s20", secs_left=45.0, winner="Up"))
                ps = sim.passed_summary("2026-10-26", variants={"w60_m95_s20", "w45_m95_s20"})
                self.assertEqual(ps[:3], (2, 1, 1)); self.assertAlmostEqual(ps[3], 15 * 0.01 - 15 * 0.99, 2)
                self.assertIsNone(sim.passed_summary("2026-10-25", variants={"w60_m95_s20"}))
            finally:
                sim.DATA = old


class PeriodParam(unittest.TestCase):
    def test_15m_slug_and_window(self):
        import importlib, os
        os.environ["PERIOD"] = "900"
        try:
            importlib.reload(sim)
            self.assertEqual(sim.PERIOD, 900); self.assertEqual(sim.SLUG_LABEL, "15m"); self.assertIn("-updown-15m-", sim.GAMMA)
        finally:
            os.environ.pop("PERIOD", None); importlib.reload(sim)
        self.assertEqual(sim.SLUG_LABEL, "5m")



class ProvisionalWinner(unittest.TestCase):
    """The closing book names the loser (ask <= 0.05) seconds after the close; gamma takes a median 5 minutes."""
    class FakeBook:
        def __init__(self, books): self.books = books
        def book_now(self, tok, now=None): return self.books.get(str(tok)), 0.1
    def test_loser_at_the_floor_names_the_winner(self):
        bf = self.FakeBook({"111": {"asks": [], "bids": []}, "222": {"asks": [{"price": "0.01", "size": "40000"}], "bids": []}})
        self.assertEqual(sim.provisional_winner(bf, ["111", "222"], ["Up", "Down"]), "Up")        # 10-27 18:35: Up had no asks left, Down at 0.01
        bf = self.FakeBook({"111": {"asks": [{"price": "0.02", "size": "5"}], "bids": []}, "222": {"asks": [{"price": "0.99", "size": "5"}], "bids": []}})
        self.assertEqual(sim.provisional_winner(bf, ["111", "222"], ["Up", "Down"]), "Down")
    def test_unclear_book_gives_none(self):
        bf = self.FakeBook({"111": {"asks": [{"price": "0.55", "size": "5"}], "bids": []}, "222": {"asks": [{"price": "0.47", "size": "5"}], "bids": []}})
        self.assertIsNone(sim.provisional_winner(bf, ["111", "222"], ["Up", "Down"]))
        self.assertIsNone(sim.provisional_winner(self.FakeBook({}), ["111", "222"], ["Up", "Down"]))
        bf = self.FakeBook({"111": {"asks": [{"price": "0.01", "size": "5"}], "bids": []}, "222": {"asks": [{"price": "0.03", "size": "5"}], "bids": []}})
        self.assertIsNone(sim.provisional_winner(bf, ["111", "222"], ["Up", "Down"]))              # both at the floor: nonsense, say nothing


class GapFilter(unittest.TestCase):
    def test_gap_block(self):
        old = sim.BOOK_GAP_MAX; sim.BOOK_GAP_MAX = 0.05
        try:
            asks = [(0.27, 100.0, 27.0), (0.84, 5053.0, 4244.0)]                   # 10-27 18:34: Up 0.27 + Down 0.84 = 1.11
            b = sim.gap_block((1, 0.84, 5053.0, 4244.0), asks, 36.6)
            self.assertEqual((b["reason"], b["dl"], b["thr"]), ("gap", 0.11, 0.05))
            self.assertIsNone(sim.gap_block((1, 0.84, 5.0, 4.2), [(0.17, 1.0, 0.17), (0.84, 5.0, 4.2)], 30))   # 1.01: a normal book
            self.assertIsNone(sim.gap_block((1, 0.84, 5.0, 4.2), [(None, 0.0, 0.0), (0.84, 5.0, 4.2)], 30))   # other side empty: nothing to compare
            self.assertIsNone(sim.gap_block(None, asks, 30))
            sim.BOOK_GAP_MAX = 0
            self.assertIsNone(sim.gap_block((1, 0.84, 5053.0, 4244.0), asks, 36.6))                           # off by default
        finally: sim.BOOK_GAP_MAX = old


class LineFix(unittest.TestCase):
    """2026-10-28: the distance line = official open (Chainlink 60 s mean) moved onto Coinbase's scale."""
    class _CL:
        def __init__(self, open_px, pts=None): self.open_px, self.pts, self.calls = open_px, pts or {}, 0
        def window_mean(self, lo, hi): self.calls += 1; return self.open_px
        def points(self, lo, hi): return self.pts
    class _Spot:
        def __init__(self, hist): self.hist = hist
        def history(self, since): return self.hist

    def test_official_open_with_gap(self):
        cl = self._CL(80000.0, {s: 80004.0 for s in range(900, 1000)})
        spot = self._Spot([(s - 0.4, 80000.0) for s in range(900, 1000)])       # Coinbase $4 below Chainlink
        ln = sim.LineRef(1000 - 60, fallback=79990.0, cl=cl, spot=spot)
        self.assertEqual(ln.value(now=941.0), 79990.0)                         # too early: old line
        self.assertEqual(ln.src, "fallback")
        self.assertAlmostEqual(ln.value(now=1000.0), 80000.0 - 4.0)            # open on Coinbase's scale
        self.assertEqual(ln.src, "chainlink_twap+gap")
        ln.value(now=1001.0); self.assertEqual(cl.calls, 1)                    # open computed once
        # a Coinbase quote of 80030 is +$30 over the official open in Chainlink terms ($34 raw minus the $4 gap)
        self.assertAlmostEqual(80030.0 - ln.value(now=1001.0), 34.0)

    def test_no_open_window_falls_back_and_gives_up(self):
        cl = self._CL(None)
        ln = sim.LineRef(1000, fallback=79990.0, cl=cl, spot=None)
        self.assertEqual(ln.value(now=1004.0), 79990.0); self.assertEqual(ln.value(now=1011.0), 79990.0)
        n = cl.calls; ln.value(now=1050.0); self.assertEqual(cl.calls, n)      # stops retrying once the window cannot arrive
        self.assertEqual(sim.LineRef(1000, 5.0, cl=None).value(now=2000.0), 5.0)

    def test_broken_stream_never_breaks_a_poll(self):
        class Boom:
            def window_mean(self, lo, hi): raise RuntimeError("feed bug")
        self.assertEqual(sim.LineRef(1000, 79990.0, cl=Boom()).value(now=1005.0), 79990.0)

    def test_glitchy_gap_ignored_and_switch_off(self):
        cl = self._CL(80000.0, {s: 80200.0 for s in range(900, 1000)})          # $200 = 25 bps: a glitch, not a gap
        ln = sim.LineRef(940, 79990.0, cl=cl, spot=self._Spot([(s - 0.4, 80000.0) for s in range(900, 1000)]))
        self.assertEqual(ln.value(now=1000.0), 80000.0); self.assertEqual(ln.src, "chainlink_twap")
        old = sim.LINE_TWAP
        try:
            sim.LINE_TWAP = False
            ln = sim.LineRef(940, 79990.0, cl=cl)
            self.assertEqual(ln.value(now=1000.0), 79990.0)                    # switch off: decisions keep the old line
            self.assertEqual(ln.official(now=1000.0), 80000.0)                 # but the official line is still there to log
            self.assertEqual(ln.src, "fallback")
        finally:
            sim.LINE_TWAP = old


class ShadowExit(unittest.TestCase):
    """2026-10-28: shadow exit signal — P(win) from the 60 s settlement average; logged only, never sells."""
    def test_win_prob_known_part_and_room(self):
        pts = {k: 80010.0 for k in range(940, 970)}                 # first 30 s of the window printed $10 above the open
        p, lean, sd = sim.win_prob("Up", 80000.0, 1000, 970.0, pts, 80010.0, 0.3)
        self.assertAlmostEqual(lean, 10 / 80000 * 1e4, 6); self.assertGreater(p, 0.9)
        p2, _, _ = sim.win_prob("Down", 80000.0, 1000, 970.0, pts, 80010.0, 0.3)
        self.assertAlmostEqual(p + p2, 1.0, 9)
        # price fell $30 below for the rest: known +$10 x 30 s, rest -$30 x 30 s -> mean -$10: Up is in trouble
        p3, lean3, _ = sim.win_prob("Up", 80000.0, 1000, 970.0, pts, 79970.0, 0.3)
        self.assertLess(lean3, 0); self.assertLess(p3, 0.1)
        # same lean with 5 s left is surer than with 30 s left
        pa, _, _ = sim.win_prob("Up", 80000.0, 1000, 995.0, {}, 79999.0, 0.3)
        pb, _, _ = sim.win_prob("Up", 80000.0, 1000, 970.0, {}, 79999.0, 0.3)
        self.assertLess(pa, pb)

    def test_sigma_and_exit_rows(self):
        self.assertEqual(sim.cl_sigma_bps({1: 1.0}, 1.0), 0.3)
        self.assertGreaterEqual(sim.cl_sigma_bps({k: 80000.0 for k in range(100)}, 80000.0), 0.15)
        v = dict(slug="s", variant="w60_m95_s20", entered=1, side="Up", ask=0.85, secs_left=55.0, winner="Down", won=0,
                 _exit_lag=dict(secs_left=30.0, p_win=0.25, lean_bps=-0.5, sd_bps=0.8, bid=0.55))
        (r,) = sim.exit_rows([v, dict(slug="s", variant="x", entered=0)])
        self.assertEqual(r["trigger"], "lag"); self.assertAlmostEqual(r["hold_pnl_sh"], -0.85 - sim.fee(0.85), 4)
        self.assertAlmostEqual(r["exit_pnl_sh"], 0.55 - 0.85 - sim.fee(0.85) - sim.fee(0.55), 4)
