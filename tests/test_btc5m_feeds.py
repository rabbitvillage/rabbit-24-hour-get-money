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
