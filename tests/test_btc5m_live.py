"""Tests for the phase-1 executor: guards, ledger, stops, header signing. No network."""
import base64, csv, hashlib, hmac, os, tempfile, unittest
from importlib import reload

os.environ["EXEC_MODE"] = "dry"
from btc5m_sim import live


def market(slug="btc-updown-5m-1792480800"):
    return dict(slug=slug, outcomes=["Up", "Down"], tokens=["111", "222"])


class Guards(unittest.TestCase):
    def setUp(self):
        live._orders.clear(); live._daily.clear(); live._stopped.clear()
        self.d = tempfile.TemporaryDirectory(); live.DATA = self.d.name; live.KILL_FILE = os.path.join(self.d.name, "STOP")
    def tearDown(self): self.d.cleanup()

    def test_mode_off(self):
        self.assertEqual(live.guard("main", "btc-updown-5m-1", 0.98, 5, mode="off"), (False, "mode off"))

    def test_basic_guards(self):
        self.assertEqual(live.guard("main", "btc-updown-5m-1", 0.98, 5)[0], True)
        self.assertFalse(live.guard("main", "weather-xyz", 0.98, 5)[0])
        self.assertFalse(live.guard("main", "btc-updown-5m-1", 0.995, 5)[0])
        self.assertFalse(live.guard("main", "btc-updown-5m-1", 0.4, 5)[0])
        self.assertFalse(live.guard("main", "btc-updown-5m-1", 0.98, 11)[0])
        open(live.KILL_FILE, "w").close()
        self.assertEqual(live.guard("main", "btc-updown-5m-1", 0.98, 5), (False, "kill file"))

    def test_rule_filter(self):
        live.LIVE_VARIANTS = {"w45_p98_s"}
        self.assertFalse(live.guard("main", "btc-updown-5m-1", 0.98, 5)[0]); self.assertTrue(live.guard("w45_p98_s", "btc-updown-5m-1", 0.98, 5)[0])
        live.LIVE_VARIANTS = set()

    def test_daily_stops(self):
        day = live.today()
        live._daily[day] = {"w60_m95_s10": -15.0}
        self.assertIn("rule daily loss", live.guard("w60_m95_s10", "btc-updown-5m-1", 0.9, 5)[1])
        self.assertTrue(live.guard("w45_p98_s", "btc-updown-5m-1", 0.9, 5)[0])          # other rules keep going
        live._daily[day] = {"a": -30.0, "b": -20.5}
        self.assertIn("total daily loss", live.guard("w45_p98_s", "btc-updown-5m-1", 0.9, 5)[1])

    def test_one_attempt_per_rule_per_market(self):
        live._orders[("main", "btc-updown-5m-1")] = {}
        self.assertEqual(live.guard("main", "btc-updown-5m-1", 0.98, 5)[1], "already ordered this market")
        self.assertTrue(live.guard("w20_p98", "btc-updown-5m-1", 0.98, 5)[0])


class Amounts(unittest.TestCase):
    def test_amounts_and_tick(self):
        # V2 market BUY: USDC floored to cents, shares = usdc/price truncated to tick decimals + 2
        self.assertEqual(live.amounts(0.98, 5, "0.001"), (4900000, 5000000, 5.0, 4.9))
        self.assertEqual(live.amounts(0.96, 5, "0.001"), (4800000, 5000000, 5.0, 4.8))
        maker, taker, shares, usdc = live.amounts(0.913, 5, "0.001")     # 5 x 0.913 = 4.565 -> $4.56 -> 4.99452 sh
        self.assertEqual((maker, usdc), (4560000, 4.56)); self.assertEqual(shares, 4.99452); self.assertEqual(taker, 4994520)
        self.assertEqual(live.tick_round(0.9876, "0.001"), 0.988); self.assertEqual(live.tick_round(0.9876, "0.01"), 0.99)
        self.assertLess(live.new_salt(), 2 ** 53)

    def test_l2_headers(self):
        live.API_SECRET = base64.urlsafe_b64encode(b"secret-bytes").decode(); live.API_KEY = "k"; live.API_PASSPHRASE = "p"; live.SIGNER_ADDRESS = "0xabc"
        h = live.l2_headers("POST", "/order", '{"a": 1}')
        msg = h["POLY_TIMESTAMP"] + "POST" + "/order" + '{"a": 1}'
        want = base64.urlsafe_b64encode(hmac.new(b"secret-bytes", msg.encode(), hashlib.sha256).digest()).decode()
        self.assertEqual(h["POLY_SIGNATURE"], want); self.assertEqual(h["POLY_ADDRESS"], "0xabc"); self.assertEqual(h["POLY_API_KEY"], "k")


class DryAndSettle(unittest.TestCase):
    def setUp(self):
        live._orders.clear(); live._daily.clear(); live._stopped.clear()
        self.d = tempfile.TemporaryDirectory(); live.DATA = self.d.name; live.KILL_FILE = os.path.join(self.d.name, "STOP")
        self.notes = []; live._notify = self.notes.append; live._log = lambda *a: None
    def tearDown(self): self.d.cleanup()

    def rows(self, name):
        p = os.path.join(self.d.name, name)
        if not os.path.exists(p): return []
        with open(p, newline="") as f: return list(csv.DictReader(f))

    def test_dry_order_is_written_not_sent(self):
        row = live._submit("w45_p98_s", market(), 1, 0.98, 30.5, mode="dry")
        self.assertEqual((row["status"], row["outcome"], row["price"], row["size"], row["cost"]), ("dry", "Down", 0.99, 5.0, 4.95))   # ask + slip
        o = self.rows("live_orders.csv"); self.assertEqual(len(o), 1); self.assertEqual(o[0]["status"], "dry"); self.assertEqual(o[0]["token_id"], "222")
        # second attempt on the same market by the same rule is skipped and recorded
        row2 = live._submit("w45_p98_s", market(), 1, 0.98, 20.0, mode="dry")
        self.assertEqual((row2["status"], row2["msg"]), ("skipped", "already ordered this market"))
        self.assertEqual(len(self.rows("live_orders.csv")), 2)

    def test_settle_books_pnl_and_trips_stops(self):
        live._submit("w60_m95_s10", market(), 0, 0.80, 55.0, mode="dry")
        live._orders[("w60_m95_s10", market()["slug"])]["status"] = "filled"      # pretend the exchange filled it
        live._orders[("w60_m95_s10", market()["slug"])]["filled_size"] = 5
        live.settle(market()["slug"], "Down")                                       # we bought Up at 0.81 -> lost the $4.05 paid
        r = self.rows("live_results.csv"); self.assertEqual(len(r), 1)
        self.assertEqual((r[0]["won"], r[0]["outcome"]), ("0", "Up")); self.assertAlmostEqual(float(r[0]["pnl"]), -4.05, 3)
        self.assertTrue(any("真單輸" in n for n in self.notes)); self.assertFalse(any("已停到明天" in n for n in self.notes))
        live._daily[live.today()]["w60_m95_s10"] = -14.9
        live._submit("w60_m95_s10", market("btc-updown-5m-2"), 0, 0.80, 55.0, mode="dry")
        live._orders[("w60_m95_s10", "btc-updown-5m-2")]["status"] = "filled"; live._orders[("w60_m95_s10", "btc-updown-5m-2")]["filled_size"] = 5
        live.settle("btc-updown-5m-2", "Down")
        self.assertTrue(any("已停到明天" in n for n in self.notes))
        self.assertIn("rule daily loss", live.guard("w60_m95_s10", "btc-updown-5m-3", 0.9, 5)[1])
        self.assertEqual(live._orders, {})                                           # settled slots are released

    def test_dry_results_do_not_count_toward_stops(self):
        live._submit("main", market(), 0, 0.98, 30.0, mode="dry"); live.settle(market()["slug"], "Down")
        self.assertEqual(live.realised("main"), 0.0); self.assertEqual(len(self.rows("live_results.csv")), 1)


class WireFormat(unittest.TestCase):
    def test_live_env_writer_keeps_other_lines(self):
        with tempfile.TemporaryDirectory() as d:
            live.LIVE_ENV = os.path.join(d, ".live.env")
            with open(live.LIVE_ENV, "w") as f: f.write("SIGNER_URL=https://x/\nSIGNER_TOKEN=t\nCLOB_API_KEY=old\n")
            live.write_live_env("k1", "s1", "p1", "0xabc")
            with open(live.LIVE_ENV) as f: lines = f.read().split()
            self.assertEqual(oct(os.stat(live.LIVE_ENV).st_mode & 0o777), "0o600")
            self.assertIn("SIGNER_URL=https://x/", lines); self.assertIn("CLOB_API_KEY=k1", lines); self.assertNotIn("CLOB_API_KEY=old", lines)
            self.assertIn("SIGNER_ADDRESS=0xabc", lines)


if __name__ == "__main__":
    unittest.main()
