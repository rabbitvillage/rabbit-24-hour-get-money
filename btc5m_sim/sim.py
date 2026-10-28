"""Phase 0: watch every BTC 5m market live, pretend to buy the favorite at
98-99c in the last 45s, record what was really on the book, and score it
when the market resolves. Read-only: no wallet, no orders.

Shadow variants: every market is also scored under other entry rules
(window / min price, see VARIANTS) from the same order-book polls, and BTC
spot is sampled once a second during the last SPOT_WINDOW seconds so the
distance between spot and priceToBeat can be studied later.

Output: data/sim_trades.csv  (main rule, schema unchanged)
        data/sim_variants.csv (one row per market per variant, incl. spot/distance)

Run:  python3 -m btc5m_sim.sim            (env: see .env.example)
"""
import csv, math, glob, json, os, sys, threading, time, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta

from . import live   # phase-1 executor; EXEC_MODE=off keeps it inert
from . import feed   # live commentary for the dashboard
from . import feeds  # websocket spot + order book (2026-10-26); REST stays as the fallback

UA = {"User-Agent": "Mozilla/5.0 btc5m-sim"}
# One process per asset (ASSET=btc|eth|sol|xrp): same rules, same files layout under its own DATA_DIR.
ASSET = os.getenv("ASSET", "btc").strip().lower()
ASSETS = {  # coinbase product, kraken pair + result key, Chainlink aggregator on Polygon, reference price for scaling distance thresholds
    "btc": dict(cb="BTC-USD", kr=("XBTUSD", "XXBTZUSD"), feed="0xc907E116054Ad103354f2D350FD2514433D57F6f", ref=82000.0),
    "eth": dict(cb="ETH-USD", kr=("ETHUSD", "XETHZUSD"), feed="0xF9680D99D6C9589e2a93a78A04A279e509205945", ref=2435.0),
    "sol": dict(cb="SOL-USD", kr=("SOLUSD", "SOLUSD"), feed="0x10C8264C0935b3B9870013e057f330Ff3e9C56dC", ref=105.0),
    "xrp": dict(cb="XRP-USD", kr=("XRPUSD", "XXRPZUSD"), feed="0x785ba89291f676b5386652eB12b30cF361020694", ref=1.34),
}
A = ASSETS.get(ASSET, ASSETS["btc"])
# Market length (fish 2026-10-26: also collect the 15-minute markets). PERIOD=900 -> slug "<asset>-updown-15m-<start>".
PERIOD = int(os.getenv("PERIOD", "300"))
SLUG_LABEL = f"{PERIOD // 60}m"
GAMMA = "https://gamma-api.polymarket.com/events?slug=" + ASSET + "-updown-" + SLUG_LABEL + "-{}"
BOOK = "https://clob.polymarket.com/book?token_id={}"
SPOT_URLS = [("coinbase", f"https://api.exchange.coinbase.com/products/{A['cb']}/ticker"),   # real-time best bid/ask
             ("kraken", f"https://api.kraken.com/0/public/Ticker?pair={A['kr'][0]}")]
KRAKEN_KEY = A["kr"][1]
# Distance thresholds in the variant names are BTC dollars (s10 = $10 at ~$82k). For other assets they scale with price:
# DIST_SCALE = asset price / 82000, so "s20" means 0.024% of price everywhere. Override with DIST_SCALE env.
DIST_SCALE = float(os.getenv("DIST_SCALE", "0")) or (A["ref"] / ASSETS["btc"]["ref"])

ENTRY_WINDOW = int(os.getenv("ENTRY_WINDOW", "45"))   # seconds before close to start looking
LAST_ENTRY = int(os.getenv("LAST_ENTRY", "5"))        # stop looking this many seconds before close
MIN_PRICE = float(os.getenv("MIN_PRICE", "0.98"))
MAX_PRICE = float(os.getenv("MAX_PRICE", "0.99"))
SHARES = float(os.getenv("SHARES", "5"))
POLL = float(os.getenv("POLL", "0.25"))                # 1.0 -> 0.5 s once the websocket feeds made each poll free of network calls;
                                                       # 0.5 -> 0.25 s 2026-10-27 (fish: 降低延遲): a decision now lags the book by <= 0.25 s
BOOK_LOG_EVERY = float(os.getenv("BOOK_LOG_EVERY", "0.5"))   # sim_book.csv keeps one row per 0.5 s per market whatever POLL is
# 2026-10-27 (fish: 再提升效能): a websocket book update wakes the decision loop at once instead of waiting for the next poll;
# POLL stays as the backstop (REST fallback, quiet books). WAKE_MIN_GAP keeps a very busy book from running the loop more
# than ~10 times a second. WAKE_ON_CHANGE=0 restores pure polling.
WAKE_ON_CHANGE = os.getenv("WAKE_ON_CHANGE", "1").strip().lower() in ("1", "true", "yes", "on")
WAKE_MIN_GAP = float(os.getenv("WAKE_MIN_GAP", "0.1"))
USE_WS = os.getenv("USE_WS", "1").strip().lower() in ("1", "true", "yes", "on")
WS_STALE = float(os.getenv("WS_STALE", "2.5"))          # seconds; older websocket data -> use REST for that poll
_spot_feed = None                                        # feeds.SpotFeed, started in main()
_ws_stats = {"book_ws": 0, "book_rest": 0, "spot_ws": 0, "spot_rest": 0}
DATA = os.getenv("DATA_DIR", "data")
TG_TOKEN = os.getenv("TG_TOKEN", "")
TG_CHAT = os.getenv("TG_CHAT_ID", "")
TZ = timezone(timedelta(hours=int(os.getenv("TZ_OFFSET", "8"))))
FEE_RATE = 0.07
# Chainlink BTC/USD aggregator on Polygon (public, no key). Polymarket resolves on the Chainlink BTC/USD 60s-TWAP *Data Stream*
# (credentialed, off-chain); the on-chain aggregator is the closest free reference, updated on deviation/heartbeat, not every second.
CHAIN_RPCS = [u for u in os.getenv("CHAIN_RPCS", "https://polygon-bor-rpc.publicnode.com,https://polygon.drpc.org").split(",") if u.strip()]
CHAIN_FEED = os.getenv("CHAIN_FEED", A["feed"])
BOOK_WINDOW = int(os.getenv("BOOK_WINDOW", "150"))    # record both sides' best ask every poll from this many seconds before close
SPOT_WINDOW = int(os.getenv("SPOT_WINDOW", str(BOOK_WINDOW)))  # sample BTC spot from this many seconds before close
TWAP_WINDOW = 60                                       # resolution = Chainlink TWAP of the last 60s


M_FLOOR = float(os.getenv("M_FLOOR", "0.80"))   # price floor for 'm' (price-cap) variants: below this the "favourite" is a coin flip


def parse_variant(s):
    """Variant spec -> (name, window_s, min_price, max_price, dist_filter):
       'w30_p98'      buy the favourite only at 0.98..MAX_PRICE in the last 30s (p = price floor, the original rule)
       'w60_m95'      buy the favourite at any ask M_FLOOR(0.80)..0.95 in the last 60s (m = price cap: earlier, cheaper entries;
                      2026-10-21: floor raised from 0.50 after a live loss at 0.55 — backtest: 0.50-0.70 entries lose 34%, 0.80-0.95 lose 8%)
       '..._s' / '..._s10'  distance filter: spot must favour the side we buy relative to the line (on-chain open price),
                     and |spot - line| >= 0 / 10 USD."""
    s = s.strip(); parts = s.split("_")
    w, pr = parts[0], parts[1]
    lo, hi = (int(pr[1:]) / 100, MAX_PRICE) if pr.startswith("p") else (M_FLOOR, int(pr[1:]) / 100)
    filt = None
    if len(parts) > 2 and parts[2].startswith("s"):
        filt = float(parts[2][1:] or 0)
    return s, int(w[1:]), lo, hi, filt


VARIANTS_RAW = [parse_variant(v) for v in os.getenv("VARIANTS", "w45_p98,w30_p98,w20_p98,w45_p99,w30_p99,w45_p98_s,w45_p98_s10,w45_p98_s20,w45_p98_s30,w60_m95_s10,w60_m95_s20,w90_m99_s30,w120_m99_s40,w45_m99_s20").split(",") if v.strip()]
VARIANTS = [(n, w, lo, hi, None if f is None else round(f * DIST_SCALE, 4)) for n, w, lo, hi, f in VARIANTS_RAW]


# Choppy-market filter (2026-10-22 22:30, fish: "I can't keep losing like this until tomorrow"). Losses cluster in markets where the
# price swung widely before entry: BTC 10-19..10-22, 513 rule entries, blocking entries whose spot range since 150s-left was >= 8 bps
# of the line removed 11 of 22 losses and lifted net from +$65 to +$121 (15 sh); per day it cost $3-4 on calm days and saved $52 on
# the choppy one. Measured only from data already seen in this market (no look-ahead). 0 = off.
CHOP_MAX_AMP_BPS = float(os.getenv("CHOP_MAX_AMP_BPS", "8"))
# Book-consistency filter (2026-10-27 18:34: the rule saw Down at 0.84 while Up's ask was 0.27 — the asks summed to 1.11, a
# hole left by a sweep, not a price; one second later Down was back at 0.5). With the loop waking on every book update such
# instants are always seen. BOOK_GAP_MAX: refuse an entry when favourite ask + other side's ask - 1 exceeds it. Shadow replay
# (w60/w45_m95_s20, BTC since 10-21, non-US): > 0.05 in 18 of 183 entries, 4 losses / 14 wins, blocked pnl negative.
# 0 = off (default while the acceptance week's rules are frozen; fish decides).
BOOK_GAP_MAX = float(os.getenv("BOOK_GAP_MAX", "0"))
# The line distances are measured from (2026-10-28 structural fix, fish: 「這不是策略的一環，這是結構問題的修正」).
# Markets settle on Chainlink's 60 s TWAP: priceToBeat = mean of the per-second Chainlink prices in [open-60, open). Before
# this fix the line was the Polygon on-chain aggregator read at open (|p90| $19 off priceToBeat on BTC, ~ the whole $20
# distance threshold) and spot was Coinbase, which sits ~$3 below Chainlink — two different scales in one subtraction.
# Now: line = that exact 60 s mean from Polymarket's public Chainlink stream (feeds.ChainlinkFeed), moved onto Coinbase's
# scale by the live Chainlink-Coinbase gap (median over the last BASIS_WINDOW s), so  dl = Coinbase now - (open - gap)
# = (Coinbase now + gap) - open. Threshold, windows, price band are unchanged. Falls back to the old line when the stream
# has no full open window (e.g. process started mid-market). LINE_TWAP=0 restores the old line.
LINE_TWAP = os.getenv("LINE_TWAP", "1").strip().lower() in ("1", "true", "yes", "on")
CL_SYMBOL = os.getenv("CL_SYMBOL", A["cb"].split("-")[0].lower() + "/usd")
BASIS_WINDOW = int(os.getenv("BASIS_WINDOW", "120"))      # seconds of Chainlink/Coinbase pairs behind the gap estimate
BASIS_EVERY = float(os.getenv("BASIS_EVERY", "5"))        # re-estimate the gap this often (seconds)
BASIS_MAX_BPS = float(os.getenv("BASIS_MAX_BPS", "10"))   # a gap wider than this (bps of price) is a feed glitch: ignored
_cl_feed = None                                           # feeds.ChainlinkFeed, started in main()
# Shadow exit signal (2026-10-28, fish: 「只記錄、不真的賣的離場訊號」). Never sells anything. After an entry, every poll in the
# last 60 s: P(win) from the settlement rule itself — final = mean of the last 60 one-second Chainlink prices; the seconds
# already printed are known, the rest is (Coinbase now + gap) plus noise sd (s/60)*sigma*sqrt(s/3). Logged to sim_exit.csv:
# 'lag' = first moment P(win) < EXIT_P while our bid (1 - other side's ask) is still >= EXIT_MIN_BID (the market has not
# caught up — the one rule that was positive in Docs/custom-queries/exit_on_reversal.py); 'p10' = first P(win) < 0.10.
EXIT_SHADOW = os.getenv("EXIT_SHADOW", "1").strip().lower() in ("1", "true", "yes", "on")
EXIT_P = float(os.getenv("EXIT_P", "0.3"))
EXIT_MIN_BID = float(os.getenv("EXIT_MIN_BID", "0.5"))
EXIT_BASIS_SD = float(os.getenv("EXIT_BASIS_SD", "0.45"))   # bps: error left in "Coinbase + gap" as a Chainlink estimate
# Provisional result from the closing book (2026-10-27 structural review): gamma marks a market closed a median 301 s after
# the close (75% later than 240 s), so a loss cool-down started at resolution never covered the next market. PROVISIONAL_AFTER
# seconds after the close the book is read once: a side whose best ask is <= PROV_LOSER_ASK has lost. Negative = off.
PROVISIONAL_AFTER = float(os.getenv("PROVISIONAL_AFTER", "2"))
PROV_LOSER_ASK = float(os.getenv("PROV_LOSER_ASK", "0.05"))


def amp_bps(spot_min, spot_max, ref):
    """Price range seen so far in this market, in basis points of the reference line. None when nothing to measure."""
    if spot_min is None or spot_max is None or not ref:
        return None
    return (spot_max - spot_min) / ref * 1e4


def chop_block(hit, amp, secs_left):
    """The _block record for a would-be entry refused because the market is choppy, or None when it may proceed."""
    if hit is None or not CHOP_MAX_AMP_BPS or amp is None or amp < CHOP_MAX_AMP_BPS:
        return None
    return dict(reason="chop", ask=hit[1], secs=secs_left, dl=round(amp, 1), thr=CHOP_MAX_AMP_BPS)


def gap_block(hit, asks, secs_left):
    """The _block record for a would-be entry refused because the two sides' asks do not add up (see BOOK_GAP_MAX), else None."""
    if hit is None or not BOOK_GAP_MAX:
        return None
    other = asks[1 - hit[0]][0]
    if other is None:
        return None
    gap = hit[1] + other - 1
    if gap <= BOOK_GAP_MAX + 1e-9:
        return None
    return dict(reason="gap", ask=hit[1], secs=secs_left, dl=round(gap, 3), thr=BOOK_GAP_MAX)


def provisional_winner(bf, tokens, outcomes, loser_ask=None):
    """Winner read off the closing order book: the side whose best ask is <= PROV_LOSER_ASK is worthless, so the other side
    won. None when the book does not say so clearly (both sides or neither at the floor, no book)."""
    loser_ask = PROV_LOSER_ASK if loser_ask is None else loser_ask
    dead = []
    for tok in tokens:
        b, _ = bf.book_now(tok)
        best = best_ask(b, MAX_PRICE)[0] if b else None
        dead.append(best is not None and best <= loser_ask)
    if dead[0] != dead[1]:
        return outcomes[1] if dead[0] else outcomes[0]
    return None


def dist_ok(side, dl, min_abs):
    """Distance filter: dl = spot now - spot at market open (proxy for priceToBeat). Up needs dl > 0, Down needs dl < 0, and |dl| >= min_abs."""
    if dl is None:
        return False
    if abs(dl) < min_abs:
        return False
    return dl > 0 if side == "Up" else dl < 0

FIELDS = ["slug", "start_utc", "price_to_beat", "entered", "secs_left", "side", "ask",
          "ask_shares_le_max", "ask_usd_le_max", "other_side_ask", "fetch_ms",
          "cost", "fee", "winner", "won", "pnl", "resolved_after_s", "note"]
VFIELDS = ["slug", "start_utc", "price_to_beat", "variant", "window", "min_price", "entered", "secs_left",
           "side", "ask", "ask_shares_le_max", "ask_usd_le_max", "other_side_ask", "fetch_ms", "cost", "fee",
           "spot_at_start", "ptb_err", "spot_at_window", "dist_at_window", "spot_at_entry", "dist_at_entry",
           "twap_dist_at_entry", "min_abs_dist", "spot_samples",
           "chain_at_start", "chain_ptb_err", "chain_at_entry", "chain_dist_at_entry", "chain_age_at_entry", "chain_samples",
           "winner", "won", "pnl", "note"]
CFIELDS = ["slug", "secs_left", "price", "updated_at", "round_id"]  # sim_chain.csv: one row per on-chain feed update
# sim_line.csv: one row per market — which line the distance filter used and how far each candidate sat from priceToBeat
# sim_exit.csv: shadow exit signals (no order is ever sent) — one row per entered rule per trigger
EFIELDS = ["slug", "variant", "trigger", "side", "entry_ask", "entry_secs", "secs_left", "p_win", "lean_bps", "sd_bps", "bid",
           "bid_05s", "bid_1s", "bid_2s",
           "winner", "won", "hold_pnl_sh", "exit_pnl_sh"]
LFIELDS = ["slug", "start_utc", "price_to_beat", "line_src", "cl_open", "cl_open_err", "chain_at_start", "chain_open_err",
           "spot_at_start", "gap_first", "gap_last", "line_official", "line_last"]
# sim_decisions.csv: one row per market per rule — did it buy, and if not, why (shown on the dashboard)
DFIELDS = ["slug", "start_utc", "variant", "decision", "reason", "ask", "secs_left", "dl", "threshold", "lead_min", "lead_max",
           "live_status", "live_msg", "winner", "won", "pnl"]
# one row per poll per market (sim_book.csv): the raw "curve" of both sides' best ask until close
BFIELDS = ["slug", "secs_left", "up_ask", "up_sh", "up_usd", "down_ask", "down_sh", "down_usd", "spot", "fetch_ms"]


def get(url, timeout=10):
    t0 = time.time()
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return json.load(r), (time.time() - t0) * 1000


def log(*a):
    print(datetime.now(TZ).strftime("%m-%d %H:%M:%S"), *a, flush=True)


# ---------------- Telegram ----------------
def chat_file():
    return os.path.join(DATA, "tg_chat_id")


def tg(text):
    global TG_CHAT
    if not TG_TOKEN:
        return
    try:
        if not TG_CHAT and os.path.exists(chat_file()):   # Telegram forgets /start after 24h; remember the chat ourselves
            TG_CHAT = open(chat_file()).read().strip()
        if not TG_CHAT:
            ups, _ = get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates")
            chats = [u.get("message", u.get("channel_post", {})).get("chat", {}).get("id") for u in ups.get("result", [])]
            chats = [c for c in chats if c]
            if not chats:
                log("TG: no chat yet - send /start to the bot (or set TG_CHAT_ID)")
                return
            TG_CHAT = str(chats[-1])
            log("TG: using chat", TG_CHAT)
            try:
                os.makedirs(DATA, exist_ok=True); open(chat_file(), "w").write(TG_CHAT)
            except OSError:
                pass
        # TG_CHAT_ID may list several chats, comma separated (2026-10-23 fish: also broadcast to a second user). Each chat
        # is sent separately so one failure (a user who has not pressed /start) never blocks the others.
        for chat in tg_chats(TG_CHAT):
            try:
                data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
                urllib.request.urlopen(urllib.request.Request(
                    f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data, headers=UA), timeout=10)
            except Exception as e:
                log(f"TG send failed for chat …{str(chat)[-4:]}:", type(e).__name__)
    except Exception as e:  # never let TG kill the sim; never print the token
        log("TG send failed:", type(e).__name__)


def tg_chats(value):
    """'555,5891' -> ['555', '5891']; blanks and duplicates dropped, order kept."""
    out = []
    for c in str(value or "").replace(";", ",").split(","):
        c = c.strip()
        if c and c not in out:
            out.append(c)
    return out


# ---------------- market helpers ----------------
def market(start):
    ev, _ = get(GAMMA.format(start))
    if not ev:
        return None
    e = ev[0]; m = e["markets"][0]
    return dict(slug=e["slug"], cond=m["conditionId"], closed=m.get("closed"),
                prices=json.loads(m["outcomePrices"]), tokens=json.loads(m["clobTokenIds"]),
                outcomes=json.loads(m["outcomes"]),
                ptb=(e.get("eventMetadata") or {}).get("priceToBeat"))


def best_ask(book, max_price):
    asks = sorted(((float(a["price"]), float(a["size"])) for a in book.get("asks", [])))
    if not asks:
        return None, 0.0, 0.0
    sh = sum(s for p, s in asks if p <= max_price + 1e-9)
    usd = sum(p * s for p, s in asks if p <= max_price + 1e-9)
    return asks[0][0], sh, usd


def fee(p):
    return FEE_RATE * p * (1 - p)


def ask_now(bf, tok):
    """For live.submit's pre-send re-check (live.RECHECK_DROP): a callable giving the side's current best ask from the
    websocket book — None when there is no ws book or it is stale — without any network call."""
    if bf is None:
        return None
    def _f():
        b, age = bf.book_now(tok)
        if b is None or age > WS_STALE:
            return None
        return best_ask(b, MAX_PRICE)[0]
    return _f


def evaluate(asks, min_price, max_price, shares):
    """asks: [(best_ask, shares_le_max, usd_le_max)] per side. First side whose best ask is in
    [min_price, max_price] with at least `shares` available -> (side_index, ask, shares, usd)."""
    for i, (ask, sh, usd) in enumerate(asks):
        if ask is not None and min_price <= ask <= max_price and sh >= shares:
            return i, ask, sh, usd
    return None


def fill(target, hit, outcomes, asks, secs_left, ms):
    i, ask, sh, usd = hit
    target.update(entered=1, secs_left=secs_left, side=outcomes[i], ask=ask,
                  ask_shares_le_max=round(sh, 2), ask_usd_le_max=round(usd, 2),
                  other_side_ask=asks[1 - i][0], fetch_ms=round(ms),
                  cost=round(ask * SHARES, 4), fee=round(fee(ask) * SHARES, 5), _t_entry=time.time())


def score(target, win):
    target["winner"] = win
    if target.get("entered"):
        target["won"] = int(target["side"] == win)
        target["pnl"] = round((SHARES if target["won"] else 0) - target["cost"] - target["fee"], 4)


# ---------------- BTC spot (for distance to priceToBeat) ----------------
def spot():
    """Current BTC/USD: the websocket mid when fresh, else the first REST source that answers. Returns price or None."""
    if _spot_feed is not None:
        px, age = _spot_feed.get()
        if px is not None and age <= WS_STALE:
            _ws_stats["spot_ws"] += 1; return px
    _ws_stats["spot_rest"] += 1
    for name, url in SPOT_URLS:
        try:
            d, _ = get(url, timeout=3)
            if name == "coinbase":
                return (float(d["bid"]) + float(d["ask"])) / 2
            return float(d["result"][KRAKEN_KEY]["c"][0])
        except Exception:
            continue
    return None


def sample_spot(end, samples):
    """Append (ts, price) once a second from end-SPOT_WINDOW until close."""
    t = end - SPOT_WINDOW
    while time.time() < t:
        time.sleep(min(1, max(0, t - time.time())))
    while time.time() < end:
        t0 = time.time()
        p = spot()
        if p:
            samples.append((t0, p))
        time.sleep(max(0, (0.5 if _spot_feed is not None else 1.0) - (time.time() - t0)))


def chainlink():
    """latestRoundData() of the on-chain feed -> (price, updated_at_unix, round_id) or (None, None, None)."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                       "params": [{"to": CHAIN_FEED, "data": "0xfeaf968c"}, "latest"]}).encode()
    for rpc in CHAIN_RPCS:
        try:
            req = urllib.request.Request(rpc, data=body, headers={**UA, "content-type": "application/json"})
            with urllib.request.urlopen(req, timeout=4) as r:
                d = json.load(r)["result"][2:]
            w = [int(d[i:i + 64], 16) for i in range(0, len(d), 64)]
            return w[1] / 1e8, w[3], w[0]
        except Exception:
            continue
    return None, None, None


def sample_chain(end, csamples):
    """Append (ts, price, updated_at, round_id) once a second from end-SPOT_WINDOW until close (only when the feed moved)."""
    t = end - SPOT_WINDOW
    while time.time() < t:
        time.sleep(min(1, max(0, t - time.time())))
    last = None
    while time.time() < end:
        t0 = time.time()
        p, up, rid = chainlink()
        if p is not None and up != last:
            csamples.append((t0, p, up, rid)); last = up
        time.sleep(max(0, 1.0 - (time.time() - t0)))


class LineRef:
    """Per-market line on Coinbase's scale (see LINE_TWAP). value() is cheap: the open is computed once, the gap every
    BASIS_EVERY seconds. src says which line is in use: 'chainlink_twap' (+ '+gap' when the gap is applied) or 'fallback'."""
    def __init__(self, start, fallback, cl=None, spot=None):
        self.start, self.fallback, self.cl, self.spot = start, fallback, cl, spot
        self.open = None; self.gap = None; self.gap_first = None; self._t = 0.0; self._gave_up = False

    def _open(self, now):
        if self.open is not None or self._gave_up or self.cl is None or now < self.start + 3:
            return self.open
        self.open = self.cl.window_mean(self.start - 60, self.start)
        if self.open is None and now > self.start + 10:     # the open window is not in the buffer and will not arrive
            self._gave_up = True
        return self.open

    def _gap(self, now):
        if self.spot is None or now - self._t < BASIS_EVERY:
            return self.gap
        self._t = now
        lo = int(now) - BASIS_WINDOW
        g = feeds.basis(self.cl.points(lo, int(now) + 1), self.spot.history(lo - 5))
        if g is not None and self.open and abs(g) / self.open * 1e4 <= BASIS_MAX_BPS:
            self.gap = g
            if self.gap_first is None: self.gap_first = g
        return self.gap

    def official(self, now=None):
        """The official-open line on Coinbase's scale, or None — computed whether or not LINE_TWAP uses it (sim_line.csv)."""
        now = now or time.time()
        if self._open(now) is None:
            return None
        return self.open - (self._gap(now) or 0.0)

    def value(self, now=None):
        try:
            o = self.official(now)
        except Exception:                   # never let the new line break a poll: the old line is always there
            o = None
        return o if LINE_TWAP and o is not None else self.fallback

    @property
    def src(self):
        if not LINE_TWAP or self.open is None: return "fallback"
        return "chainlink_twap+gap" if self.gap is not None else "chainlink_twap"


def cl_sigma_bps(points, ref):
    """Per-second volatility (bps) from {second: price}; 0.15 bps floor, 0.3 when there is too little data."""
    ks = sorted(points); d = [points[b] - points[a] for a, b in zip(ks, ks[1:]) if b - a == 1]
    if len(d) < 20 or not ref:
        return 0.3
    m = sum(d) / len(d)
    return max(0.15, (sum((x - m) ** 2 for x in d) / len(d)) ** 0.5 / ref * 1e4)


def win_prob(side, open_px, end, now, cl_points, est_now, sigma, basis_sd=None):
    """P(the side wins) under the settlement rule: final = mean of the 60 one-second Chainlink prices in [end-60, end).
    Seconds before `now` use the printed Chainlink price when there is one, else est_now (Coinbase now + gap, Chainlink scale);
    the rest is est_now plus noise. Returns (p, lean_bps, sd_bps)."""
    basis_sd = EXIT_BASIS_SD if basis_sd is None else basis_sd
    s = max(0.0, end - now)
    known = [cl_points.get(k, est_now) for k in range(end - 60, min(end, int(now)))]
    est = (sum(known) + est_now * (60 - len(known))) / 60
    lean = (1 if side == "Up" else -1) * (est - open_px) / open_px * 1e4
    sd = (((s / 60) * sigma * (s / 3) ** 0.5) ** 2 + basis_sd ** 2) ** 0.5
    return 0.5 * (1 + math.erf(lean / sd / 2 ** 0.5)), lean, sd


def latest(samples):
    return samples[-1][1] if samples else None


def dist(price, ptb):
    """Signed USD distance of spot from priceToBeat (>0 favours Up)."""
    return None if price is None or ptb is None else round(price - ptb, 2)


def twap_dist(samples, end, now, ptb):
    """Mean of the spot samples already inside the resolution TWAP window (end-60 .. now) minus ptb."""
    pts = [p for ts, p in samples if end - TWAP_WINDOW <= ts <= now]
    return None if not pts or ptb is None else round(sum(pts) / len(pts) - ptb, 2)


def min_abs_dist(samples, ptb):
    return None if not samples or ptb is None else round(min(abs(p - ptb) for _, p in samples), 2)


def finalize_chain(v, csamples, ptb, chain_at_start):
    v["chain_at_start"] = chain_at_start
    v["chain_ptb_err"] = dist(chain_at_start, ptb)
    v["chain_dist_at_entry"] = dist(v.get("chain_at_entry"), ptb)
    v["chain_samples"] = len(csamples)


def finalize_dists(v, samples, end, ptb, spot_at_start):
    """gamma only publishes priceToBeat after close, so every distance is back-filled once ptb is known.
    spot_at_start is our own spot sample at market open: a live proxy for ptb; ptb_err shows how good it is."""
    v["spot_at_start"] = spot_at_start
    v["ptb_err"] = dist(spot_at_start, ptb)
    v["dist_at_window"] = dist(v.get("spot_at_window"), ptb)
    v["dist_at_entry"] = dist(v.get("spot_at_entry"), ptb)
    v["twap_dist_at_entry"] = twap_dist(samples, end, v["_t_entry"], ptb) if v.get("entered") else None
    v["min_abs_dist"] = min_abs_dist(samples, ptb)
    v["spot_samples"] = len(samples)


# ---------------- csv ----------------
def versioned_path(path, fields):
    """If `path` exists with a different header, return the next free <name>_vN.csv so old files stay readable."""
    base, ext = os.path.splitext(path); n = 1
    while os.path.exists(path):
        with open(path, newline="") as f:
            if next(csv.reader(f), None) == fields:
                return path
        n += 1; path = f"{base}_v{n}{ext}"
    return path


def append_rows(path, fields, rows):
    path = versioned_path(path, fields)
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fields)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in fields})


def rule_state(v, asks, lead, secs_left, dl_fn, outcomes, amp_fn=lambda: None):
    """Plain-language state of one rule at this poll, for the live feed."""
    if v.get("entered"):
        return f"已買 {v['side']} @{v['ask']}（剩 {v['secs_left']} 秒）"
    if secs_left > v["window"]:
        return f"等窗口（最後 {v['window']} 秒才看）"
    hit = evaluate(asks, v["min_price"], v["_max"], SHARES)
    if hit is None:
        if lead is None: return "領先方沒有賣單"
        if lead > v["_max"]: return f"領先方賣價 {lead:.3f} 高於上限 {v['_max']:.2f}，太貴不買"
        if lead < v["min_price"]: return f"領先方賣價 {lead:.3f} 低於下限 {v['min_price']:.2f}，不夠確定不買"
        return f"賣價 {lead:.3f} 在區間，但不足 {SHARES:g} 股"
    if v["_filt"] is not None:
        side = outcomes[hit[0]]; dl = dl_fn()
        if dl is None: return f"賣價 {hit[1]:.3f} 可買，但還沒有現價，不買"
        side_ok = (dl > 0) if side == "Up" else (dl < 0)
        if not side_ok: return f"賣價 {hit[1]:.3f} 可買 {side}，但幣價在對面（{dl:+.4g}），不買"
        if abs(dl) < v["_filt"]: return f"賣價 {hit[1]:.3f} 可買 {side}，但離線只有 {dl:+.4g}（門檻 {v['_filt']:.4g}），不買"
    gb = gap_block(hit, asks, secs_left)
    if gb: return f"賣價 {hit[1]:.3f} 可買 {outcomes[hit[0]]}，但兩邊賣價和 {1 + gb['dl']:.2f} 超過 1（盤口不一致），不買"
    amp = amp_fn()
    if chop_block(hit, amp, secs_left): return f"賣價 {hit[1]:.3f} 可買 {outcomes[hit[0]]}，但這場幣價來回振幅 {amp:.1f} bps ≥ {CHOP_MAX_AMP_BPS:g}（震盪盤），不買"
    return f"條件成立，準備買 {outcomes[hit[0]]} @{hit[1]:.3f}"


def decision_row(v, row):
    """One line per rule per market: bought (with price/time) or skipped with the reason and the numbers behind it."""
    d = dict(slug=v["slug"], start_utc=v["start_utc"], variant=v["variant"], lead_min=v.get("_lead_min"), lead_max=v.get("_lead_max"),
             winner=row.get("winner"), won=v.get("won"), pnl=v.get("pnl"), live_status="", live_msg="", dl="", threshold=v.get("_filt") if v.get("_filt") is not None else "")
    if v.get("entered"):
        d.update(decision="buy", reason="entered", ask=v["ask"], secs_left=v["secs_left"])
    elif v.get("_block"):
        b = v["_block"]; d.update(decision="skip", reason=b["reason"], ask=b["ask"], secs_left=b["secs"], dl=b["dl"], threshold=b["thr"])
    elif v.get("_expensive"):
        e = v["_expensive"]; d.update(decision="skip", reason=f"too_expensive:{e['side']}", ask=e["ask"], secs_left=e["secs"])
    elif v.get("_depth_short"):
        d.update(decision="skip", reason="depth", ask=v.get("_lead_min"), secs_left="")
    elif v.get("_lead_min") is not None:
        d.update(decision="skip", reason="price_out_of_band", ask=v.get("_lead_min"), secs_left="")
    else:
        d.update(decision="skip", reason="no_ask", ask="", secs_left="")
    st = live.status_of(v["variant"], v["slug"])
    if st: d["live_status"], d["live_msg"] = st
    return d


# ---------------- one market ----------------
def watch(start, rows, vrows, lock):
    end = start + PERIOD
    base = dict(slug=f"{ASSET}-updown-{SLUG_LABEL}-{start}", start_utc=datetime.fromtimestamp(start, timezone.utc).isoformat())
    row = dict(base, entered=0, note="")
    vstate = {n: dict(base, variant=n, window=w, min_price=p, entered=0, note="", _filt=fl, _max=hi) for n, w, p, hi, fl in VARIANTS}
    samples = []
    threading.Thread(target=sample_spot, args=(end, samples), daemon=True).start()
    ptb = None; series = []; csamples = []
    threading.Thread(target=sample_chain, args=(end, csamples), daemon=True).start()
    bf = None
    spot_at_start = spot()
    chain_at_start, chain_up_at_start, _ = chainlink()   # the old line: on-chain aggregator at open (now the fallback, see LINE_TWAP)
    line = LineRef(start, chain_at_start if chain_at_start is not None else spot_at_start, _cl_feed, _spot_feed)
    try:
        m = None
        while m is None and time.time() < end - LAST_ENTRY:
            try:
                m = market(start)
            except Exception:
                m = None
            if m is None:
                time.sleep(2)
        if m is None:
            row["note"] = "market not found"
            for v in vstate.values():
                v["note"] = "market not found"
            return
        ptb = m["ptb"]
        bf = None
        if USE_WS:
            try: bf = feeds.BookFeed(m["tokens"]); bf.start()
            except Exception as e: log(f"book ws start failed {type(e).__name__}"); bf = None
        poll_from = max([ENTRY_WINDOW, BOOK_WINDOW] + [w for _, w, _, _, _ in VARIANTS])
        while time.time() < end - poll_from:
            time.sleep(min(1, max(0, end - poll_from - time.time())))
        if ptb is None:  # gamma sometimes fills priceToBeat late
            try:
                m = market(start) or m; ptb = m["ptb"]
            except Exception:
                pass
        row["price_to_beat"] = ptb
        feed.start_market(row["slug"], start)
        live.prefetch_tick(m)               # tick size looked up once per market, in the background (order speed-up 10-24)
        seen_asks = []; series = []; last_logged = 0.0; last_feed = 0.0
        spot_min = spot_max = None          # price range seen since the book window opened (for the chop filter)
        pending = dict(vstate)
        while time.time() < end - LAST_ENTRY:
            t_loop = time.time()
            if bf is not None and WAKE_ON_CHANGE: bf.changed.clear()   # any update from here on wakes the wait below early
            try:
                books, ms = [], 0
                for tok in m["tokens"]:
                    b, age = bf.book_now(tok) if bf is not None else (None, float("inf"))
                    if b is not None and age <= WS_STALE:
                        books.append(b); ms += age * 1000; _ws_stats["book_ws"] += 1
                    else:
                        b, dt = get(BOOK.format(tok), timeout=5); books.append(b); ms += dt; _ws_stats["book_rest"] += 1
                asks = [best_ask(b, MAX_PRICE) for b in books]
                now = time.time(); secs_left = round(end - now, 1); sp = latest(samples)
                if sp is not None:
                    spot_min = sp if spot_min is None else min(spot_min, sp); spot_max = sp if spot_max is None else max(spot_max, sp)
                ref = line.value(now)
                amp_now = amp_bps(spot_min, spot_max, ref)
                lead = max((a[0] for a in asks if a[0] is not None), default=None)
                if lead is not None and secs_left <= ENTRY_WINDOW:  # note range stays about the entry window
                    seen_asks.append(lead)
                if now - last_logged >= BOOK_LOG_EVERY - 0.02:   # the per-market curve stays at one row per 0.5 s (POLL is faster)
                    series.append(dict(slug=row["slug"], secs_left=secs_left, up_ask=asks[0][0], up_sh=round(asks[0][1], 2), up_usd=round(asks[0][2], 2),
                                       down_ask=asks[1][0], down_sh=round(asks[1][1], 2), down_usd=round(asks[1][2], 2), spot=sp, fetch_ms=round(ms)))
                    last_logged = now
                if not row["entered"] and secs_left <= ENTRY_WINDOW:
                    hit = evaluate(asks, MIN_PRICE, MAX_PRICE, SHARES)
                    if chop_block(hit, amp_now, secs_left):
                        row["_block"] = chop_block(hit, amp_now, secs_left); hit = None
                    if hit:
                        fill(row, hit, m["outcomes"], asks, secs_left, ms)
                        live.submit("main", m, hit[0], hit[1], secs_left, seen_at=now, book_ms=ms, book_fn=ask_now(bf, m["tokens"][hit[0]]))
                for name in list(pending):
                    v = pending[name]
                    if secs_left > v["window"]:
                        continue
                    if v.get("spot_at_window") is None:  # first poll inside this variant's window
                        v["spot_at_window"] = sp
                    if lead is not None:                 # favourite's best ask seen inside this rule's window (for the "why not" log)
                        v["_lead_min"] = min(v.get("_lead_min", lead), lead); v["_lead_max"] = max(v.get("_lead_max", lead), lead)
                    if lead is not None and lead > v["_max"] and "_expensive" not in v:
                        # Deliberately passed (fish 2026-10-26): the favourite was already above this rule's price cap while
                        # everything else (direction, distance, calm market) would have allowed a buy. Recorded once per rule
                        # with the side and price, so after settlement we know what letting it go cost or saved.
                        li = max(range(len(asks)), key=lambda i: asks[i][0] if asks[i][0] is not None else -1)
                        ref_e = line.value(now)
                        dl_e = None if sp is None or ref_e is None else sp - ref_e
                        ok_dist = v["_filt"] is None or dist_ok(m["outcomes"][li], dl_e, v["_filt"])
                        if ok_dist and not chop_block((li, lead), amp_bps(spot_min, spot_max, ref_e), secs_left):
                            v["_expensive"] = dict(side=m["outcomes"][li], ask=lead, secs=secs_left)
                    hit = evaluate(asks, v["min_price"], v["_max"], SHARES)
                    if hit is None and lead is not None and v["min_price"] <= lead <= v["_max"]:
                        v["_depth_short"] = True         # price was in band but fewer than SHARES on the book
                    if hit and v["_filt"] is not None:
                        ref = line.value(now)
                        dl = None if sp is None or ref is None else sp - ref
                        if not dist_ok(m["outcomes"][hit[0]], dl, v["_filt"]):
                            side_ok = dl is not None and ((dl > 0) if m["outcomes"][hit[0]] == "Up" else (dl < 0))
                            v["_block"] = dict(reason="distance" if side_ok else ("no_spot" if dl is None else "direction"),
                                               ask=hit[1], secs=secs_left, dl=None if dl is None else round(dl, 2), thr=v["_filt"])
                            hit = None
                    gb = gap_block(hit, asks, secs_left)
                    if gb:
                        v["_block"] = gb; hit = None
                    cb = chop_block(hit, amp_now, secs_left)
                    if cb:
                        v["_block"] = cb; hit = None
                    if hit:
                        fill(v, hit, m["outcomes"], asks, secs_left, ms)
                        live.submit(name, m, hit[0], hit[1], secs_left, seen_at=now, book_ms=ms, book_fn=ask_now(bf, m["tokens"][hit[0]]))
                        v["spot_at_entry"] = sp
                        if csamples:
                            v["chain_at_entry"] = csamples[-1][1]; v["chain_age_at_entry"] = round(now - csamples[-1][2])
                        del pending[name]
                if EXIT_SHADOW and secs_left <= 60 and line.open and sp is not None and _cl_feed is not None:
                    try:
                        held = [v for v in vstate.values() if v.get("entered")]
                        for v in held:                       # follow-up bids: how much speed is worth after a trigger
                            for key in ("_exit_lag", "_exit_p10"):
                                r = v.get(key)
                                if not r: continue
                                oth = asks[1 - m["outcomes"].index(v["side"])][0]
                                for lag, f in ((0.5, "bid_05s"), (1.0, "bid_1s"), (2.0, "bid_2s")):
                                    if f not in r and now - r["_t"] >= lag:
                                        r[f] = None if oth is None else round(1 - oth, 3)
                        held = [v for v in held if not (v.get("_exit_lag") and v.get("_exit_p10"))]
                        if held:
                            pts = _cl_feed.points(int(now) - 120, int(now) + 1)
                            est_now = sp + (line.gap or 0.0); sig = cl_sigma_bps(pts, line.open)
                            for v in held:
                                pw, lean, sd = win_prob(v["side"], line.open, end, now, pts, est_now, sig)
                                oth = asks[1 - m["outcomes"].index(v["side"])][0]
                                bid = None if oth is None else round(1 - oth, 3)
                                rec = dict(secs_left=secs_left, p_win=round(pw, 4), lean_bps=round(lean, 2), sd_bps=round(sd, 2), bid=bid, _t=now)
                                if not v.get("_exit_lag") and pw < EXIT_P and bid is not None and bid >= EXIT_MIN_BID:
                                    v["_exit_lag"] = rec
                                    log(f"{row['slug']} shadow exit (no order) {v['variant']} {v['side']} P(win) {pw:.2f} bid {bid} {secs_left}s left")
                                if not v.get("_exit_p10") and pw < 0.10:
                                    v["_exit_p10"] = rec
                    except Exception as e:
                        if not row.get("_exit_err"):
                            row["_exit_err"] = 1; log(f"{row['slug']} shadow exit check failed {type(e).__name__}")
                if now - last_feed >= BOOK_LOG_EVERY - 0.02:     # dashboard commentary (15 rule texts) only every 0.5 s: the pusher sends every 5 s anyway
                    ref = line.value(now)
                    dl_now = None if sp is None or ref is None else round(sp - ref, 2)
                    live_set = live.LIVE_VARIANTS or {n for n, *_ in VARIANTS}
                    feed.update(row["slug"], secs_left, asks, m["outcomes"], dl_now, {"fallback": "鏈上開盤價" if chain_at_start is not None else "Coinbase 開盤價"}.get(line.src, "官方開盤價（Chainlink 60 秒平均）"),
                                [(n, rule_state(v, asks, lead, secs_left, lambda: dl_now, m["outcomes"], lambda: amp_now), n in live_set) for n, v in vstate.items()])
                    last_feed = now
            except Exception as e:
                row["note"] = f"book error {type(e).__name__}"
            gap = time.time() - t_loop
            if bf is not None and WAKE_ON_CHANGE:
                if gap < WAKE_MIN_GAP:
                    time.sleep(WAKE_MIN_GAP - gap); gap = time.time() - t_loop
                bf.changed.wait(timeout=max(0, POLL - gap))      # returns at once on a book update, else after the poll interval
            else:
                time.sleep(max(0, POLL - gap))
        if seen_asks:
            rng = f" lead ask {min(seen_asks)}-{max(seen_asks)} last {seen_asks[-1]}"
            if not row["entered"]:
                row["note"] += rng
            for v in vstate.values():
                if not v["entered"]:
                    v["note"] += rng
        live_set = live.LIVE_VARIANTS or {n for n, *_ in VARIANTS}
        def summary_lines():
            out = []
            for n, v in vstate.items():
                if n not in live_set: continue
                d = decision_row(v, row); st = live.status_of(n, row["slug"])
                if d["decision"] == "buy":
                    out.append(f"{n}：買 {v['side']} @{v['ask']}（剩 {v['secs_left']} 秒）" + (f"，真單 {st[0]}" if st else "") + (f"，{'對' if v.get('won') == 1 else '錯'}" if v.get("won") in (0, 1) else ""))
                else:
                    why = {"no_ask": "整個窗口領先方沒有賣單", "price_out_of_band": f"賣價 {d['lead_min']}–{d['lead_max']} 不在區間", "depth": "深度不夠",
                           "direction": f"幣價在對面（{d['dl']}）", "distance": f"離線只有 {d['dl']}，門檻 {d['threshold']}", "no_spot": "沒有現價",
                           "chop": f"震盪盤，幣價來回振幅 {d['dl']} bps ≥ {d['threshold']:g}",
                           "gap": f"兩邊賣價和超過 1（差 {d['dl']}，門檻 {d['threshold']:g}），盤口不一致"}.get(d["reason"], d["reason"])
                    out.append(f"{n}：沒買，{why}")
            return out
        feed.finish(row["slug"], summary_lines())
        # provisional result from the closing book, so a loss starts the cool-down now and not when gamma gets round to it
        if bf is not None and PROVISIONAL_AFTER >= 0:
            time.sleep(max(0, end + PROVISIONAL_AFTER - time.time()))
            try:
                prov = provisional_winner(bf, m["tokens"], m["outcomes"])
                if prov and live.note_market(row["slug"], [(v.get("secs_left"), v.get("side"), int(v["side"] == prov))
                                                          for n, v in vstate.items() if v.get("entered") and n in live_set], provisional=True):
                    log(f"{row['slug']} closing book says {prov} won -> a live-listed entry lost, cool-down started before resolution")
            except Exception as e:
                log(f"{row['slug']} provisional result failed {type(e).__name__}")
        # wait for resolution
        time.sleep(max(0, end + 60 - time.time()))
        for _ in range(60):
            try:
                m2 = market(start)
                if m2 and m2["closed"]:
                    win = m2["outcomes"][0] if float(m2["prices"][0]) > 0.5 else m2["outcomes"][1]
                    ptb = m2["ptb"] or ptb
                    row["price_to_beat"] = ptb; row["resolved_after_s"] = round(time.time() - end)
                    score(row, win)
                    for v in vstate.values():
                        v["price_to_beat"] = ptb; score(v, win)
                    live.settle(row["slug"], win)
                    live_set_r = live.LIVE_VARIANTS or {n for n, *_ in VARIANTS}
                    live.note_market(row["slug"], [(v.get("secs_left"), v.get("side"), v.get("won")) for n, v in vstate.items()
                                                   if v.get("entered") and n in live_set_r])
                    feed.resolved(row["slug"], win, summary_lines())
                    break
            except Exception:
                pass
            time.sleep(30)
        else:
            row["note"] += " unresolved after 30m"
            for v in vstate.values():
                v["note"] += " unresolved after 30m"
    finally:
        row["_t"] = time.time()
        if bf is not None:
            try: bf.stop()
            except Exception: pass
        for v in vstate.values():
            v["_t"] = row["_t"]; finalize_dists(v, samples, end, ptb, spot_at_start); finalize_chain(v, csamples, ptb, chain_at_start)
        decisions = [decision_row(v, row) for v in vstate.values()]
        try:
            lrow = dict(base, price_to_beat=ptb, line_src=line.src, cl_open=line.open, line_official=line.official(row["_t"]),
                        chain_at_start=chain_at_start, spot_at_start=spot_at_start,
                        cl_open_err=dist(line.open, ptb), chain_open_err=dist(chain_at_start, ptb),
                        gap_first=None if line.gap_first is None else round(line.gap_first, 4),
                        gap_last=None if line.gap is None else round(line.gap, 4), line_last=line.value(row["_t"]))
        except Exception as e:              # the line log must never cost the trade/decision logs below
            log(f"{row['slug']} line log failed {type(e).__name__}"); lrow = None
        with lock:
            if lrow:
                append_rows(os.path.join(DATA, "sim_line.csv"), LFIELDS, [lrow])
            erows = exit_rows(vstate.values())
            if erows:
                append_rows(os.path.join(DATA, "sim_exit.csv"), EFIELDS, erows)
            rows.append(row); vrows.extend(vstate.values())
            append_rows(os.path.join(DATA, "sim_trades.csv"), FIELDS, [row])
            if decisions:
                append_rows(os.path.join(DATA, "sim_decisions.csv"), DFIELDS, decisions)
            if vstate:
                append_rows(os.path.join(DATA, "sim_variants.csv"), VFIELDS, vstate.values())
            if series:
                append_rows(os.path.join(DATA, "sim_book.csv"), BFIELDS, series)
            if csamples:
                append_rows(os.path.join(DATA, "sim_chain.csv"), CFIELDS,
                            [dict(slug=row["slug"], secs_left=round(end - ts, 1), price=p, updated_at=up, round_id=rid) for ts, p, up, rid in csamples])
        if row.get("entered"):
            log(f"{row['slug']} buy {row['side']} @{row['ask']} ({row['secs_left']}s left, {row['ask_usd_le_max']}$ on book) -> {row.get('winner')} pnl {row.get('pnl')}")
            if row.get("won") == 0:
                tg(f"❌ [{ASSET.upper()}] 模擬輸一筆 {row['slug']}\n買 {row['side']} @{row['ask']}，剩 {row['secs_left']}s，結果 {row.get('winner')}，損益 {row.get('pnl')}")
        else:
            log(f"{row['slug']} no entry {row.get('note','')} winner {row.get('winner')}")
        if vstate:
            log(f"{row['slug']} variants " + " ".join(
                f"{n}={'@'+str(v['ask'])+'/'+str(v['secs_left'])+'s' if v['entered'] else '-'}" for n, v in vstate.items())
                + f" | min|spot-ptb| {vstate[next(iter(vstate))]['min_abs_dist']} ({len(samples)} samples) | chain open-ptb {vstate[next(iter(vstate))]['chain_ptb_err']} ({len(csamples)} updates)")


def exit_rows(variants):
    """sim_exit.csv rows for the shadow exit signals of one market (after settlement, so the outcome is known)."""
    out = []
    for v in variants:
        for trig, key in (("lag", "_exit_lag"), ("p10", "_exit_p10")):
            r = v.get(key)
            if not r or not v.get("entered"):
                continue
            ask = v["ask"]; won = v.get("won")
            hold = None if won not in (0, 1) else round((1.0 if won else 0.0) - ask - fee(ask), 4)
            ex = None if r["bid"] is None else round(r["bid"] - ask - fee(ask) - fee(r["bid"]), 4)
            out.append(dict(slug=v["slug"], variant=v["variant"], trigger=trig, side=v["side"], entry_ask=ask, entry_secs=v["secs_left"],
                            winner=v.get("winner"), won=won, hold_pnl_sh=hold, exit_pnl_sh=ex,
                            **{k: x for k, x in r.items() if not k.startswith("_")}))
    return out


# ---------------- summaries ----------------
def passed_summary(day=None, shares=15.0, variants=None):
    """Markets the live rules let go because the favourite was already above the price cap, and what buying would have done.
    day: 'YYYY-MM-DD' (Taiwan) or None = all. Returns (markets, would_win, would_lose, pnl_if_bought) or None when no data."""
    variants = variants or live.LIVE_VARIANTS or {n for n, *_ in VARIANTS}
    best = {}
    for p in sorted(glob.glob(os.path.join(DATA, "sim_decisions*.csv"))):
        with open(p, newline="") as f:
            for r in csv.DictReader(f):
                if not r.get("reason", "").startswith("too_expensive:") or r.get("variant") not in variants or r.get("winner") not in ("Up", "Down"):
                    continue
                if day and datetime.fromisoformat(r["start_utc"]).astimezone(TZ).strftime("%Y-%m-%d") != day:
                    continue
                try: ask = float(r["ask"]); secs = float(r["secs_left"] or 0)
                except ValueError: continue
                k = r["slug"]
                if k not in best or secs > best[k][2]:             # earliest look per market
                    best[k] = (r["reason"].split(":", 1)[1], ask, secs, r["winner"])
    if not best:
        return None
    win = sum(1 for side, ask, _, w in best.values() if side == w)
    pnl = sum((shares * (1 - ask) if side == w else -shares * ask) for side, ask, _, w in best.values())
    return len(best), win, len(best) - win, round(pnl, 2)


def summary(rows, since):
    rs = [r for r in rows if r.get("_t", 0) >= since] if since else rows
    n = len(rs); ent = [r for r in rs if r.get("entered")]
    done = [r for r in ent if r.get("won") in (0, 1)]
    losses = sum(1 for r in done if r["won"] == 0)
    pnl = sum(r.get("pnl", 0) or 0 for r in done)
    usd = sorted(r["ask_usd_le_max"] for r in ent)
    med = usd[len(usd) // 2] if usd else 0
    return (f"場次 {n}｜可進場 {len(ent)}（{len(ent)/max(n,1):.0%}）｜已結算 {len(done)}，輸 {losses}"
            f"｜模擬損益 ${pnl:+.2f}（每筆 {SHARES:g} 股）｜98~99¢ 賣單中位數 ${med:,.0f}")


def vsummary(vrows, since):
    """One line per variant: entries / losses / pnl."""
    rs = [r for r in vrows if r.get("_t", 0) >= since] if since else vrows
    out = []
    for n, _, _, _, _ in VARIANTS:
        vs = [r for r in rs if r["variant"] == n]; ent = [r for r in vs if r.get("entered")]
        done = [r for r in ent if r.get("won") in (0, 1)]
        out.append(f"{n}: 進 {len(ent)}/{len(vs)} 輸 {sum(1 for r in done if r['won'] == 0)} 損益 {sum(r.get('pnl', 0) or 0 for r in done):+.2f}")
    return "\n".join(out)


def main():
    os.makedirs(DATA, exist_ok=True)
    rows, vrows, lock, seen = [], [], threading.Lock(), set()
    exec_mode = live.init(notify=tg, log=log)
    feed.init(log=log)
    global _spot_feed
    if USE_WS:
        try:
            _spot_feed = feeds.SpotFeed(A["cb"]); _spot_feed.start(); log(f"ws feeds on: spot {A['cb']} via Coinbase ws, order books via Polymarket ws, REST fallback after {WS_STALE:g}s")
        except Exception as e:
            log(f"ws spot feed unavailable ({type(e).__name__}); REST only"); _spot_feed = None
    global _cl_feed
    if USE_WS:   # always on: the official line is logged to sim_line.csv even when LINE_TWAP=0 keeps the old line for decisions
        try:
            _cl_feed = feeds.ChainlinkFeed(CL_SYMBOL); _cl_feed.start()
        except Exception as e:
            log(f"chainlink stream unavailable ({type(e).__name__})"); _cl_feed = None
    log(f"line for the distance filter: {'Chainlink 60s TWAP open of ' + CL_SYMBOL + ' (public stream) minus the Coinbase gap over ' + str(BASIS_WINDOW) + 's, fallback on-chain open' if LINE_TWAP else 'on-chain aggregator at open (LINE_TWAP=0)'}"
        f"; official line logged to sim_line.csv: {'yes' if _cl_feed is not None else 'no (no stream)'}")
    log(f"start [{ASSET.upper()}]: entry {MIN_PRICE}-{MAX_PRICE} in last {ENTRY_WINDOW}s, {SHARES:g} shares, poll {POLL}s"
        f"{f' (wake on book change, min gap {WAKE_MIN_GAP:g}s)' if WAKE_ON_CHANGE and USE_WS else ''}; dist scale {DIST_SCALE:.4g}; "
        f"variants {[n for n, _, _, _, _ in VARIANTS]}; book window {BOOK_WINDOW}s; spot window {SPOT_WINDOW}s")
    tg((f"🔴 {ASSET.upper()} {SLUG_LABEL} bot 啟動：真單模式（會真的下單）" if exec_mode == "live" else f"🟢 {ASSET.upper()} {SLUG_LABEL} 模擬 bot 啟動（{'dry：算好單但不送' if exec_mode == 'dry' else '不下單'}）")
       + f"\n規則：最後 {ENTRY_WINDOW} 秒，領先方賣價 {MIN_PRICE}~{MAX_PRICE} 就買 {SHARES:g} 股\n"
       f"影子變體：{', '.join(n for n, _, _, _, _ in VARIANTS)}\n下單模式：{exec_mode}" + ("（真單！）" if exec_mode == "live" else "（不送單）"))
    last_hour = int(time.time() // 3600); last_day = datetime.now(TZ).date(); day_start = time.time()
    while True:
        now = time.time()
        start = int(now // PERIOD) * PERIOD
        if start not in seen and now < start + PERIOD - ENTRY_WINDOW - 5:
            seen.add(start)
            threading.Thread(target=watch, args=(start, rows, vrows, lock), daemon=True).start()
        h = int(now // 3600)
        if h != last_hour:
            with lock:
                msg = summary(rows, now - 3600); vmsg = vsummary(vrows, now - 3600)
            ws_line = (f"\n即時資料：盤口 ws {_ws_stats['book_ws']}／REST {_ws_stats['book_rest']}，幣價 ws {_ws_stats['spot_ws']}／REST {_ws_stats['spot_rest']}" if USE_WS else "")
            for k in _ws_stats: _ws_stats[k] = 0
            tg(f"⏱ [{ASSET.upper()}] 過去一小時\n" + msg + ws_line + ("\n影子變體\n" + vmsg if VARIANTS else "") + ("\n" + live.summary() if exec_mode != "off" else "")); log("hourly:", msg); last_hour = h
        d = datetime.now(TZ).date()
        if d != last_day:
            with lock:
                ps = passed_summary(last_day)
                tg(f"📅 [{ASSET.upper()}] {last_day} 全日\n" + summary(rows, day_start) + "\n累計\n" + summary(rows, 0)
                   + (f"\n太貴放過 {ps[0]} 場：若照買 贏 {ps[1]} 輸 {ps[2]}，約 {ps[3]:+.2f}（15 股）" if ps else "")
                   + ("\n影子變體（全日）\n" + vsummary(vrows, day_start) if VARIANTS else ""))
            last_day = d; day_start = now
        time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
