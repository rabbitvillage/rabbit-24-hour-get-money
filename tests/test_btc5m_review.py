"""Self-review decisions on synthetic ledgers: continue / fix (rule, late fills) / stop / already stopped. No network."""
import csv, os, tempfile, time, unittest
from datetime import datetime, timedelta
from btc5m_sim import review

OF = ["ts", "mode", "variant", "slug", "outcome", "token_id", "ask", "price", "size", "tick", "neg_risk", "order_type", "status", "order_id", "filled_size", "cost", "secs_left", "msg"]
RF = ["ts", "variant", "slug", "outcome", "size", "cost", "fee", "winner", "won", "pnl", "day"]


def ledger(d, trades):
    """trades: (hours_ago, rule, won, ask, fill_price)"""
    now = datetime.now(review.TZ)
    with open(os.path.join(d, "live_orders.csv"), "w", newline="") as fo, open(os.path.join(d, "live_results.csv"), "w", newline="") as fr:
        wo, wr = csv.DictWriter(fo, OF), csv.DictWriter(fr, RF); wo.writeheader(); wr.writeheader()
        for i, (h, rule, won, ask, fill) in enumerate(trades):
            t = (now - timedelta(hours=h)).strftime("%Y-%m-%d %H:%M:%S"); slug = f"btc-updown-5m-{i}"; cost = 13.5; sh = cost / fill
            wo.writerow(dict(ts=t, mode="live", variant=rule, slug=slug, outcome="Up", ask=ask, price=ask + 0.02, size=15, status="filled", filled_size=sh, cost=cost, msg=""))
            wr.writerow(dict(ts=t, variant=rule, slug=slug, outcome="Up", size=sh, cost=cost, winner="Up" if won else "Down", won=int(won), pnl=(sh - cost) if won else -cost, day=t[:10]))


def base(losses=2, n=60):
    return [(30 + i, "w60_m95_s20", i >= losses, 0.90, 0.90) for i in range(n)]


class Review(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory(); self.d = self.t.name; self.saved = review.wallet_free; review.wallet_free = lambda: 100.0
    def tearDown(self): self.t.cleanup(); review.wallet_free = self.saved

    def test_low_wallet_asks_for_funds_using_the_wallet_now(self):
        ledger(self.d, base(2))
        old = review.FLOOR; review.FLOOR = 15.0; self.addCleanup(setattr, review, "FLOOR", old)   # production floor since 10-26
        review.wallet_free = lambda: 9.0
        o = self.run_once(); self.assertEqual(o["decision"], "fix"); self.assertIn("補資金", o["action"])
        review.wallet_free = lambda: 34.0                                   # above the floor again -> no false alarm
        self.assertNotEqual(self.run_once()["reason"].startswith("錢包"), True)
    def run_once(self, act=True): return review.review_coin("BTC", self.d, None, datetime.now(review.TZ), act=act)

    def test_binom_tail(self):
        self.assertEqual(review.binom_tail(10, 0, 0.05), 1.0)
        self.assertAlmostEqual(review.binom_tail(10, 10, 0.5), 0.5 ** 10, 9)

    def test_matches_expectation_continue(self):
        ledger(self.d, base(3) + [(1 + i * 0.5, "w60_m95_s20", i != 0, 0.9, 0.9) for i in range(30)])
        o = self.run_once(); self.assertEqual(o["decision"], "continue"); self.assertFalse(os.path.exists(os.path.join(self.d, "STOP")))

    def test_one_rule_failing_is_disabled(self):
        win = [(1 + i * 0.3, "w90_m99_s30", i >= 4, 0.9, 0.9) for i in range(6)] + [(1 + i * 0.3, "w60_m95_s20", True, 0.9, 0.9) for i in range(20)]
        ledger(self.d, base(2) + win)
        o = self.run_once(); self.assertEqual(o["decision"], "fix"); self.assertIn("w90_m99_s30", o["reason"])
        self.assertEqual(open(os.path.join(self.d, "DISABLED_RULES")).read().split(), ["w90_m99_s30"])

    def test_late_fills_pause_the_coin(self):
        win = []
        for rule in ("w90_m99_s30", "w60_m95_s20", "w45_p98_s10", "w45_m99_s20"):
            win += [(1 + j * 0.4, rule, j != 0, 0.90, 0.70 if j == 0 else 0.90) for j in range(5)]
        ledger(self.d, base(2) + win)
        o = self.run_once(); self.assertEqual(o["decision"], "fix"); self.assertIn("晚到", o["reason"])
        self.assertGreater(float(open(os.path.join(self.d, "PAUSE_UNTIL")).read()), time.time() + 3600)

    def test_unexplained_deviation_stops_and_says_so(self):
        win = []
        for rule in ("w90_m99_s30", "w60_m95_s20", "w45_p98_s10", "w45_m99_s20"):
            win += [(1 + j * 0.4, rule, j != 0, 0.90, 0.90) for j in range(5)]
        ledger(self.d, base(2) + win)
        o = self.run_once(); self.assertEqual(o["decision"], "stop"); self.assertTrue(os.path.exists(os.path.join(self.d, "STOP")))

    def test_dry_run_changes_nothing_and_stopped_coin_is_skipped(self):
        win = []
        for rule in ("w90_m99_s30", "w60_m95_s20", "w45_p98_s10", "w45_m99_s20"):
            win += [(1 + j * 0.4, rule, j != 0, 0.90, 0.90) for j in range(5)]
        ledger(self.d, base(2) + win)
        self.assertEqual(self.run_once(act=False)["decision"], "stop"); self.assertFalse(os.path.exists(os.path.join(self.d, "STOP")))
        open(os.path.join(self.d, "STOP"), "w").close()
        self.assertEqual(self.run_once()["decision"], "stopped")
