"""Tests for the phase-1 executor: guards, ledger, stops, header signing. No network."""
import base64, csv, hashlib, hmac, json, os, tempfile, unittest, urllib.request
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


class BalanceFloor(unittest.TestCase):
    def setUp(self):
        live._orders.clear(); live._daily.clear(); live._balance.update(usdc=None, at=0.0, alerted=None)
        self.d = tempfile.TemporaryDirectory(); live.DATA = self.d.name; live.KILL_FILE = os.path.join(self.d.name, "STOP")
        self.notes = []; live._notify = self.notes.append; live._log = lambda *a: None
    def tearDown(self): self.d.cleanup(); live._balance.update(usdc=None, at=0.0, alerted=None)

    def test_below_floor_blocks_live_only_and_alerts_once(self):
        live._balance.update(usdc=42.0, at=live.time.time())          # cached reading below the $50 floor
        ok, why = live.guard("main", "btc-updown-5m-1", 0.98, 5, mode="live")
        self.assertFalse(ok); self.assertIn("below floor", why); self.assertEqual(len(self.notes), 1)
        live.guard("w20_p98", "btc-updown-5m-1", 0.98, 5, mode="live"); self.assertEqual(len(self.notes), 1)   # one alert per day
        self.assertTrue(live.guard("main", "btc-updown-5m-1", 0.98, 5, mode="dry")[0])                       # dry never checks the wallet
        live._balance.update(usdc=133.7, at=live.time.time())
        self.assertTrue(live.guard("main", "btc-updown-5m-2", 0.98, 5, mode="live")[0])

    def test_unreadable_balance_does_not_block(self):
        live.API_KEY = ""                                               # no credentials -> wallet_balance() returns None
        self.assertTrue(live.guard("main", "btc-updown-5m-1", 0.98, 5, mode="live")[0])


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


class ProxyRouting(unittest.TestCase):
    def test_only_authenticated_calls_use_the_proxy(self):
        live.ORDER_PROXY = "http://127.0.0.1:10809"
        def proxies(opener): return {k: v for h in opener.handlers for k, v in getattr(h, "proxies", {}).items()}
        self.assertEqual(proxies(live._opener(True)).get("https"), "http://127.0.0.1:10809")
        self.assertEqual(proxies(live._opener(False)), {})
        live.ORDER_PROXY = ""
        self.assertEqual(proxies(live._opener(True)), {})


class FakPartialFill(unittest.TestCase):
    """Live submit with every network call stubbed: FAK partial fills are booked at what was really spent."""
    def setUp(self):
        live._orders.clear(); live._daily.clear(); live._ticks.clear(); live._balance.update(usdc=200.0, at=live.time.time(), alerted=None)
        self.d = tempfile.TemporaryDirectory(); live.DATA = self.d.name; live.KILL_FILE = os.path.join(self.d.name, "STOP")
        self.notes = []; live._notify = self.notes.append; live._log = lambda *a: None
        self.saved = (live._get, live.identity, live.worker, live.clob_request, live.l2_headers, live.API_KEY, live.API_SECRET, live.API_PASSPHRASE, live.SIGNER_ADDRESS)
        live.API_KEY = "k"; live.API_SECRET = base64.urlsafe_b64encode(b"s").decode(); live.API_PASSPHRASE = "p"; live.SIGNER_ADDRESS = "0xabc"
        self.sizes = (live.LIVE_SHARES, live.MAX_SHARES); live.LIVE_SHARES, live.MAX_SHARES = 15.0, 15.0
        live._get = lambda url, **kw: {"minimum_tick_size": "0.001"}
        live.identity = lambda: {"address": "0xabc", "proxyWallet": "0xdef"}
        live.worker = lambda op, payload=None, timeout=8: (200, {"ok": True, "signature": "0xsig", "digest": "0xd"})
        live.l2_headers = lambda m, p, b: {}
    def tearDown(self):
        (live._get, live.identity, live.worker, live.clob_request, live.l2_headers, live.API_KEY, live.API_SECRET, live.API_PASSPHRASE, live.SIGNER_ADDRESS) = self.saved
        live.LIVE_SHARES, live.MAX_SHARES = self.sizes
        self.d.cleanup(); live._balance.update(usdc=None, at=0.0, alerted=None)

    def test_partial_fill_books_actual_spend(self):
        sent = {}
        def fake_post(method, path, body=None, headers=None, _retry=True):
            sent["body"] = json.loads(body); return 200, {"success": True, "status": "matched", "orderID": "o1", "makingAmount": "6.2", "takingAmount": "6.4"}
        live.clob_request = fake_post
        row = live._submit("w45_p98_s10", market(), 1, 0.98, 30.0, mode="live")          # asked 15 sh -> got 6.4 for $6.20
        self.assertEqual(sent["body"]["orderType"], live.ORDER_TYPE); self.assertEqual(sent["body"]["order"]["side"], "BUY")
        self.assertEqual((row["status"], row["filled_size"], row["cost"]), ("filled", 6.4, 6.2)); self.assertIn("partial", row["msg"])
        live.settle(market()["slug"], "Up")                                                  # bought Down -> lost only what was spent
        r = csv.DictReader(open(os.path.join(self.d.name, "live_results.csv"))); row2 = list(r)[0]
        self.assertAlmostEqual(float(row2["pnl"]), -6.2, 3)

    def test_exchange_500_is_retried_once_with_the_same_signed_order(self):
        calls = []
        def fake_post(method, path, body=None, headers=None, _retry=True):
            calls.append((json.loads(body)["order"]["salt"], _retry))
            return (500, {"error": "internal server error"}) if len(calls) == 1 else (200, {"success": True, "status": "matched", "orderID": "o3", "makingAmount": "14.85", "takingAmount": "15.1"})
        live.clob_request = fake_post
        row = live._submit("w60_m95_s20", market(), 0, 0.90, 50.0, mode="live")
        self.assertEqual(row["status"], "filled"); self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], calls[1][0]); self.assertEqual(calls[1][1], False)     # same order, fresh connection
        self.assertIn("retry after 500", row["msg"])

    def test_timing_row_records_each_step(self):
        import time as _t
        def slow_worker(op, payload=None, timeout=8): _t.sleep(0.03); return 200, {"ok": True, "signature": "0xsig", "digest": "0xd"}
        def slow_post(method, path, body=None, headers=None, _retry=True):
            _t.sleep(0.02); return 200, {"success": True, "status": "matched", "orderID": "o9", "makingAmount": "13.5", "takingAmount": "35.128206"}
        live.worker = slow_worker; live.clob_request = slow_post
        seen = _t.time() - 0.1; called = _t.time()
        live._submit("w60_m95_s20", market(), 1, 0.88, 56.6, mode="live", seen_at=seen, book_ms=210.4, t_call=called)
        rows = list(csv.DictReader(open(os.path.join(self.d.name, "live_timing.csv"))))
        self.assertEqual(len(rows), 1); r = rows[0]
        self.assertEqual((r["variant"], r["status"], r["book_ms"], r["retried"]), ("w60_m95_s20", "filled", "210", "0"))
        self.assertAlmostEqual(float(r["fill_avg"]), 0.3843, 3)                    # 13.5 / 35.128 — the 10-23 21:39 SOL fill
        self.assertGreaterEqual(int(r["book_age_ms"]), 95); self.assertGreaterEqual(int(r["sign_ms"]), 25); self.assertGreaterEqual(int(r["post_ms"]), 15)
        parts = sum(int(r[k]) for k in ("book_age_ms", "queue_ms", "guard_ms", "tick_ms", "prep_ms", "sign_ms", "post_ms"))
        self.assertLessEqual(abs(int(r["total_ms"]) - parts), 5)                    # the steps add up to the total

    def test_timing_never_breaks_and_skips_are_not_timed(self):
        live._orders[("w45_p98_s10", market()["slug"])] = dict(status="filled", outcome="Up")   # opposite side held -> skipped by guard
        row = live._submit("w45_p98_s20", market(), 1, 0.98, 30.0, mode="live")
        self.assertEqual(row["status"], "skipped")
        self.assertFalse(os.path.exists(os.path.join(self.d.name, "live_timing.csv")))
        live._record_timing({"status": "filled", "cost": "x", "filled_size": "0"}, {}, None, None, None)   # bad input: logged, not raised

    def test_zero_fill_is_unfilled(self):
        live.clob_request = lambda m, p, body=None, headers=None, _retry=True: (200, {"success": True, "status": "unmatched", "orderID": "o2", "makingAmount": "0", "takingAmount": "0"})
        row = live._submit("w45_p98_s20", market(), 1, 0.98, 30.0, mode="live")
        self.assertEqual(row["status"], "unfilled")


class Recovery(unittest.TestCase):
    def test_filled_unsettled_orders_are_reloaded_after_restart(self):
        live._orders.clear(); live._daily.clear()
        with tempfile.TemporaryDirectory() as d:
            live.DATA = d; live._log = lambda *a: None
            with open(os.path.join(d, "live_orders.csv"), "w") as f:
                f.write(",".join(live.OFIELDS) + "\n")
                f.write("2026-10-21 22:19:25,live,w45_p98,btc-updown-5m-1792593000,Down,222,0.98,0.99,5.0,0.001,False,FOK,filled,abc,5.15625,4.95,44.6,ok\n")
                f.write("2026-10-21 22:19:25,live,w30_p98,btc-updown-5m-1792593000,Down,222,0.98,0.99,5.0,0.001,False,FOK,unfilled,,0,0,44.6,fok\n")
                f.write("2026-10-21 22:14:25,live,main,btc-updown-5m-1792592700,Up,111,0.98,0.99,5.0,0.001,False,FOK,filled,def,5.0,4.95,40.0,ok\n")
            with open(os.path.join(d, "live_results.csv"), "w") as f:
                f.write(",".join(live.RFIELDS) + "\n")
                f.write("2026-10-21 22:25:00,main,btc-updown-5m-1792592700,Up,5.0,4.95,0,Up,1,0.05,2026-10-21\n")   # already booked
            started = []
            orig = live.threading.Thread
            class FakeThread:
                def __init__(self, target=None, args=(), daemon=None): started.append(args)
                def start(self): pass
            live.threading.Thread = FakeThread
            try: live._recover_pending()
            finally: live.threading.Thread = orig
            self.assertEqual(list(live._orders), [("w45_p98", "btc-updown-5m-1792593000")])     # filled + unbooked only
            self.assertEqual(live._orders[("w45_p98", "btc-updown-5m-1792593000")]["filled_size"], 5.15625)
            self.assertEqual(started, [("btc-updown-5m-1792593000",)])
            live._orders.clear()


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


class OneDirectionPerMarket(unittest.TestCase):
    """Once a rule holds one side of a market, no other rule may buy the other side (no hedging against ourselves)."""
    def setUp(self):
        live._orders.clear()
    def tearDown(self):
        live._orders.clear()
    def test_opposite_side_blocked_same_side_allowed(self):
        live._orders[("w120_m99_s40", "btc-updown-5m-1")] = dict(status="filled", outcome="Up")
        ok, why = live.guard("w60_m95_s20", "btc-updown-5m-1", 0.9, 5, mode="dry", outcome="Down")
        self.assertFalse(ok); self.assertIn("opposite side", why)
        self.assertTrue(live.guard("w60_m95_s20", "btc-updown-5m-1", 0.9, 5, mode="dry", outcome="Up")[0])
        self.assertTrue(live.guard("w60_m95_s20", "btc-updown-5m-2", 0.9, 5, mode="dry", outcome="Down")[0])   # other market
    def test_pending_reservation_counts(self):
        live._orders[("w120_m99_s40", "btc-updown-5m-1")] = dict(status="", outcome="Up")   # reserved, network call in flight
        self.assertFalse(live.guard("w45_p98_s10", "btc-updown-5m-1", 0.98, 5, mode="dry", outcome="Down")[0])


class OrdersPerMarketCap(unittest.TestCase):
    def setUp(self):
        live._orders.clear(); self.old = live.MAX_ORDERS_PER_MARKET; live.MAX_ORDERS_PER_MARKET = 2
    def tearDown(self):
        live._orders.clear(); live.MAX_ORDERS_PER_MARKET = self.old
    def test_third_order_same_market_blocked(self):
        live._orders[("w120_m99_s40", "btc-updown-5m-1")] = dict(status="filled", outcome="Up")
        self.assertTrue(live.guard("w90_m99_s30", "btc-updown-5m-1", 0.9, 5, mode="dry", outcome="Up")[0])
        live._orders[("w90_m99_s30", "btc-updown-5m-1")] = dict(status="", outcome="Up")
        ok, why = live.guard("w60_m95_s20", "btc-updown-5m-1", 0.9, 5, mode="dry", outcome="Up")
        self.assertFalse(ok); self.assertIn("cap 2", why)
        self.assertTrue(live.guard("w60_m95_s20", "btc-updown-5m-2", 0.9, 5, mode="dry", outcome="Up")[0])
    def test_skipped_rows_do_not_count(self):
        live._orders[("a", "btc-updown-5m-1")] = dict(status="skipped", outcome="Up"); live._orders[("b", "btc-updown-5m-1")] = dict(status="rejected", outcome="Up")
        self.assertTrue(live.guard("c", "btc-updown-5m-1", 0.9, 5, mode="dry", outcome="Up")[0])


class OrderSpeedups(unittest.TestCase):
    """10-24: balance refreshed in the background, tick size looked up once per market — the order path waits for neither."""
    def setUp(self):
        live._orders.clear(); live._daily.clear(); live._ticks.clear()
        self.d = tempfile.TemporaryDirectory(); live.DATA = self.d.name; live.KILL_FILE = os.path.join(self.d.name, "STOP")
        self.notes = []; live._notify = self.notes.append; live._log = lambda *a: None
        self.saved = (live._get, live.identity, live.worker, live.clob_request, live.l2_headers, live.API_KEY, live.API_SECRET, live.API_PASSPHRASE, live.SIGNER_ADDRESS)
        live.API_KEY = "k"; live.API_SECRET = base64.urlsafe_b64encode(b"s").decode(); live.API_PASSPHRASE = "p"; live.SIGNER_ADDRESS = "0xabc"
        self.sizes = (live.LIVE_SHARES, live.MAX_SHARES); live.LIVE_SHARES, live.MAX_SHARES = 15.0, 15.0
        live.identity = lambda: {"address": "0xabc", "proxyWallet": "0xdef"}
        live.worker = lambda op, payload=None, timeout=8: (200, {"ok": True, "signature": "0xsig", "digest": "0xd"})
        live.l2_headers = lambda m, p, b: {}
        live.clob_request = lambda m, p, body=None, headers=None, _retry=True: (200, {"success": True, "status": "matched", "orderID": "o", "makingAmount": "13.5", "takingAmount": "15.2"})
        self.calls = []
    def tearDown(self):
        (live._get, live.identity, live.worker, live.clob_request, live.l2_headers, live.API_KEY, live.API_SECRET, live.API_PASSPHRASE, live.SIGNER_ADDRESS) = self.saved
        live.LIVE_SHARES, live.MAX_SHARES = self.sizes
        live._ticks.clear(); self.d.cleanup(); live._balance.update(usdc=None, at=0.0, alerted=None)

    def _no_network(self, url, **kw):
        raise AssertionError("order path must not hit the network: " + url)

    def test_guard_uses_the_cached_balance_even_past_the_old_60s_ttl(self):
        live._get = self._no_network
        live._balance.update(usdc=42.0, at=live.time.time() - 120, alerted=None)        # 2 min old: used to trigger an inline fetch
        ok, why = live.guard("main", "btc-updown-5m-1", 0.98, 5, mode="live")
        self.assertFalse(ok); self.assertIn("below floor", why)                          # the $50 floor still bites

    def test_guard_fetches_inline_only_when_the_cache_is_stale(self):
        live._get = lambda url, **kw: (self.calls.append(url), {"balance": "60000000"})[1]
        live._balance.update(usdc=42.0, at=live.time.time() - 400, alerted=None)        # background thread dead for > 5 min
        self.assertTrue(live.guard("main", "btc-updown-5m-1", 0.98, 5, mode="live")[0])
        self.assertEqual(live._balance["usdc"], 60.0); self.assertEqual(len(self.calls), 1)

    def test_background_refresh_updates_the_cache_and_pauses_on_kill_file(self):
        live._get = lambda url, **kw: (self.calls.append(url), {"balance": "171790000"})[1]
        self.assertAlmostEqual(live._balance_once(), 171.79, 2)
        open(live.KILL_FILE, "w").close()
        self.assertIsNone(live._balance_once()); self.assertEqual(len(self.calls), 1)

    def test_prefetched_tick_is_used_without_a_lookup(self):
        live._get = lambda url, **kw: (self.calls.append(url), {"minimum_tick_size": "0.01"})[1]
        live._prefetch_tick_now(market())
        self.assertEqual(len(self.calls), 2); self.assertEqual(live._ticks, {"111": "0.01", "222": "0.01"})
        live._balance.update(usdc=200.0, at=live.time.time(), alerted=None)
        live._get = self._no_network
        row = live._submit("w60_m95_s20", market(), 0, 0.90, 50.0, mode="live")
        self.assertEqual((row["status"], row["tick"]), ("filled", "0.01"))

    def test_tick_is_still_looked_up_when_not_prefetched(self):
        live._get = lambda url, **kw: (self.calls.append(url), {"minimum_tick_size": "0.001"})[1]
        live._balance.update(usdc=200.0, at=live.time.time(), alerted=None)
        row = live._submit("w60_m95_s20", market(), 0, 0.90, 50.0, mode="live")
        self.assertEqual(row["tick"], "0.001"); self.assertTrue(any("tick-size" in c for c in self.calls))
        self.assertEqual(live._ticks.get("111"), "0.001")                                # cached for the rest of the market


class UsSessionPause(unittest.TestCase):
    """Mon-Fri 09:30-16:00 New York time, daylight saving via the tz database, holidays and early closes."""
    from datetime import datetime as _dt, timezone as _tz
    def at(self, iso): return self._dt.fromisoformat(iso).replace(tzinfo=self._tz.utc)

    def test_session_hours_follow_new_york_time(self):
        o = live.us_session_open
        self.assertTrue(o(self.at("2026-10-23T17:26")))     # Fri 13:26 EDT = Sat 01:26 Taiwan: still Friday in New York
        self.assertFalse(o(self.at("2026-10-23T20:00")))    # Fri 16:00 EDT = Sat 04:00 Taiwan: closed (end exclusive)
        self.assertFalse(o(self.at("2026-10-23T13:29")))    # 09:29 EDT
        self.assertTrue(o(self.at("2026-10-23T13:30")))     # 09:30 EDT = 21:30 Taiwan
        self.assertFalse(o(self.at("2026-10-24T15:00")))    # Saturday
        self.assertFalse(o(self.at("2026-10-25T15:00")))    # Sunday
        self.assertTrue(o(self.at("2026-10-26T13:30")))     # Mon 09:30 EDT = 21:30 Taiwan

    def test_daylight_saving_shift(self):
        o = live.us_session_open
        self.assertFalse(o(self.at("2026-11-02T13:30")))    # Mon 08:30 EST (would be 09:30 under EDT)
        self.assertTrue(o(self.at("2026-11-02T14:30")))     # Mon 09:30 EST = 22:30 Taiwan
        self.assertTrue(o(self.at("2026-11-02T20:59")))     # 15:59 EST = 04:59 Taiwan
        self.assertFalse(o(self.at("2026-11-02T21:00")))    # 16:00 EST = 05:00 Taiwan
        self.assertEqual(live.us_session_close_local(self.at("2026-10-23T17:26")), "04:00")
        self.assertEqual(live.us_session_close_local(self.at("2026-11-02T15:00")), "05:00")

    def test_holidays_and_early_close(self):
        o = live.us_session_open
        self.assertFalse(o(self.at("2026-11-26T15:00")))    # Thanksgiving
        self.assertTrue(o(self.at("2026-11-27T17:30")))     # day after: 12:30 EST open
        self.assertFalse(o(self.at("2026-11-27T18:00")))    # 13:00 EST early close
        self.assertFalse(o(self.at("2026-12-25T15:00")))    # Christmas

    def test_guard_pauses_live_orders_only_when_enabled(self):
        live._orders.clear(); live._daily.clear()
        d = tempfile.TemporaryDirectory(); live.DATA = d.name; live.KILL_FILE = os.path.join(d.name, "STOP")
        saved = (live.PAUSE_US_SESSION, live.us_session_open)
        try:
            live.us_session_open = lambda now=None: True
            live.PAUSE_US_SESSION = False
            self.assertTrue(live.guard("w90_m99_s30", "btc-updown-5m-1", 0.9, 5, mode="dry")[0])
            live.PAUSE_US_SESSION = True
            self.assertEqual(live.guard("w90_m99_s30", "btc-updown-5m-1", 0.9, 5, mode="dry"), (False, "US stock session pause"))
            live.us_session_open = lambda now=None: False
            self.assertTrue(live.guard("w90_m99_s30", "btc-updown-5m-1", 0.9, 5, mode="dry")[0])
        finally:
            live.PAUSE_US_SESSION, live.us_session_open = saved; d.cleanup()


class ReviewBlocks(unittest.TestCase):
    """DISABLED_RULES / PAUSE_UNTIL written by the self-review stop real orders without a restart."""
    def setUp(self):
        live._orders.clear(); live._daily.clear()
        self.d = tempfile.TemporaryDirectory(); live.DATA = self.d.name; live.KILL_FILE = os.path.join(self.d.name, "STOP")
    def tearDown(self): self.d.cleanup()
    def test_disabled_rule_and_pause(self):
        open(os.path.join(self.d.name, "DISABLED_RULES"), "w").write("w90_m99_s30\n")
        self.assertEqual(live.guard("w90_m99_s30", "btc-updown-5m-1", 0.9, 5, mode="dry"), (False, "rule disabled by review"))
        self.assertTrue(live.guard("w60_m95_s20", "btc-updown-5m-1", 0.9, 5, mode="dry")[0])
        open(os.path.join(self.d.name, "PAUSE_UNTIL"), "w").write(str(int(live.time.time()) + 600))
        ok, why = live.guard("w60_m95_s20", "btc-updown-5m-1", 0.9, 5, mode="dry"); self.assertFalse(ok); self.assertIn("paused by review", why)
        open(os.path.join(self.d.name, "PAUSE_UNTIL"), "w").write(str(int(live.time.time()) - 10))
        self.assertTrue(live.guard("w60_m95_s20", "btc-updown-5m-2", 0.9, 5, mode="dry")[0])

