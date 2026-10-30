"""Feed state machines without any network."""
import json, unittest
from btc5m_sim import feeds


class Spot(unittest.TestCase):
    def test_ticker_mid_and_age(self):
        f = feeds.SpotFeed("BTC-USD")
        self.assertIsNone(f.on_ticker({"type": "subscriptions"}))
        self.assertIsNone(f.on_ticker({"type": "ticker", "product_id": "ETH-USD", "best_bid": "1", "best_ask": "2"}))
        self.assertAlmostEqual(f.on_ticker({"type": "ticker", "product_id": "BTC-USD", "best_bid": "80260.75", "best_ask": "80260.77", "price": "80260.75"}, now=1000.0), 80260.76, 6)
        p, age = f.get(now=1000.4); self.assertAlmostEqual(p, 80260.76, 6); self.assertAlmostEqual(age, 0.4, 6)
        self.assertEqual(feeds.SpotFeed("BTC-USD").get(), (None, float("inf")))


class Book(unittest.TestCase):
    def test_snapshot_then_deltas(self):
        A, B = "111", "222"; f = feeds.BookFeed([A, B])
        f.on_message(json.dumps({"event_type": "book", "asset_id": A, "bids": [{"price": "0.01", "size": "9654.1"}], "asks": [{"price": "0.98", "size": "50"}, {"price": "0.99", "size": "500"}]}), now=10.0)
        bk, age = f.book_now(A, now=10.5)
        self.assertEqual([x["price"] for x in bk["asks"]], ["0.98", "0.99"]); self.assertAlmostEqual(age, 0.5)
        f.on_message(json.dumps({"event_type": "price_change", "price_changes": [
            {"asset_id": A, "price": "0.98", "size": "0", "side": "SELL"},            # level removed
            {"asset_id": A, "price": "0.97", "size": "120", "side": "SELL"},          # new level
            {"asset_id": B, "price": "0.35", "size": "542", "side": "BUY"},           # other token's bid
            {"asset_id": "999", "price": "0.5", "size": "1", "side": "SELL"}]}), now=11.0)   # unknown token ignored
        bk, age = f.book_now(A, now=11.2)
        self.assertEqual([(x["price"], x["size"]) for x in bk["asks"]], [("0.97", 120.0), ("0.99", 500.0)]); self.assertAlmostEqual(age, 0.2)
        self.assertEqual(f.book_now(B, now=11.2)[0]["bids"], [{"price": "0.35", "size": 542.0}])
        self.assertEqual(f.book_now("999")[1], float("inf"))
    def test_old_change_format_and_ignored_events(self):
        f = feeds.BookFeed(["111"]); f.on_message(json.dumps({"event_type": "book", "asset_id": "111", "bids": [], "asks": []}), now=1.0)
        f.on_message(json.dumps({"event_type": "price_change", "asset_id": "111", "changes": [{"price": "0.90", "size": "7", "side": "SELL"}]}), now=2.0)
        f.on_message(json.dumps({"event_type": "last_trade_price", "asset_id": "111", "price": "0.9"}), now=3.0)
        f.on_message("PONG", now=4.0)
        self.assertEqual(f.book_now("111", now=2.5), ({"asks": [{"price": "0.90", "size": 7.0}], "bids": []}, 0.5))

    def test_changed_event_wakes_on_book_updates_only(self):
        f = feeds.BookFeed(["111"])
        self.assertFalse(f.changed.is_set())
        f.on_message(json.dumps({"event_type": "book", "asset_id": "111", "bids": [], "asks": [{"price": "0.98", "size": "5"}]}), now=1.0)
        self.assertTrue(f.changed.is_set()); f.changed.clear()
        f.on_message(json.dumps({"event_type": "last_trade_price", "asset_id": "111", "price": "0.9"}), now=2.0)
        f.on_message("PONG", now=2.5)
        self.assertFalse(f.changed.is_set())                     # nothing changed in the book -> the loop keeps waiting
        f.on_message(json.dumps({"event_type": "price_change", "asset_id": "111", "changes": [{"price": "0.97", "size": "3", "side": "SELL"}]}), now=3.0)
        self.assertTrue(f.changed.wait(timeout=0.01))


class Chainlink(unittest.TestCase):
    """Polymarket public stream, topic crypto_prices_chainlink (wire format recorded 2026-10-28)."""
    def _upd(self, ts, v, sym="btc/usd"):
        return json.dumps({"connection_id": "x", "payload": {"full_accuracy_value": "1", "symbol": sym, "timestamp": ts, "value": v},
                           "timestamp": ts + 1100, "topic": "crypto_prices_chainlink", "type": "update"})

    def test_snapshot_updates_and_window_mean(self):
        f = feeds.ChainlinkFeed("btc/usd")
        f.on_message(json.dumps({"payload": {"symbol": "btc/usd", "data": [{"timestamp": (1000 + i) * 1000, "value": 100.0 + i} for i in range(60)]},
                                 "topic": "crypto_prices_chainlink", "type": "subscribe"}), now=1061.0)
        self.assertAlmostEqual(f.window_mean(1000, 1060), 100.0 + 29.5)           # plain mean of 60 one-second prices
        f.on_message(self._upd(1060000, 200.0), now=1061.5)
        f.on_message(self._upd(1061000, 300.0, sym="sol/usd"), now=1062.0)        # other symbol ignored
        f.on_message("", now=1062.0); f.on_message("PONG", now=1062.0)
        self.assertEqual(f.latest(now=1062.0), (1060, 200.0, 0.5))
        self.assertEqual(f.points(1059, 1061), {1059: 159.0, 1060: 200.0})

    def test_subscribe_filter_is_compact_json(self):
        # spaced JSON ('{"symbol": "btc/usd"}') gets a snapshot and then nothing (host test 2026-10-28)
        sub = json.loads(feeds.ChainlinkFeed("SOL/USD").subscribe_msg())["subscriptions"][0]
        self.assertEqual(sub, {"topic": "crypto_prices_chainlink", "type": "*", "filters": '{"symbol":"sol/usd"}'})

    def test_missing_seconds_carry_forward_up_to_a_limit(self):
        f = feeds.ChainlinkFeed("btc/usd")
        for s in list(range(995, 1010)) + list(range(1012, 1060)):                 # 1010, 1011 missing
            f.on_message(self._upd(s * 1000, 10.0 if s < 1012 else 20.0), now=s + 1.5)
        self.assertAlmostEqual(f.window_mean(1000, 1060), (12 * 10.0 + 48 * 20.0) / 60)   # the 2 gaps carry 10.0
        self.assertIsNone(f.window_mean(1000, 1060, max_missing=1))
        self.assertIsNone(feeds.ChainlinkFeed("btc/usd").window_mean(1000, 1060))   # nothing buffered (process started late)

    def test_basis_median_with_lead(self):
        cl = {s: 105.0 for s in range(100, 140)}
        cb = [(s - 0.4, 100.0) for s in range(100, 140)]                           # Coinbase $5 below, quoted 0.4 s before
        self.assertAlmostEqual(feeds.basis(cl, cb), 5.0)
        self.assertIsNone(feeds.basis(cl, cb[:10]))                                # too few pairs
        self.assertIsNone(feeds.basis(cl, [(0.0, 100.0)]))                         # Coinbase quote too old to pair

    def test_spot_history(self):
        f = feeds.SpotFeed("BTC-USD")
        for t, b in ((1.0, 10), (2.0, 11), (3.0, 12)):
            f.on_ticker({"type": "ticker", "product_id": "BTC-USD", "best_bid": str(b), "best_ask": str(b)}, now=t)
        self.assertEqual(f.history(2.0), [(2.0, 11.0), (3.0, 12.0)])
