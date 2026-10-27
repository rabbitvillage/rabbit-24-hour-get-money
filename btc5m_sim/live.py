"""Order executor for btc5m_sim (phase 1).  EXEC_MODE = off (default) | dry | live

  off : nothing happens (pure simulation, as before).
  dry : every guard runs and the full order intent is built and written to data/live_orders.csv with status "dry".
        Nothing leaves the host: no signer call, no exchange call.
  live: as dry, then the intent is sent to the signer Worker (which holds the key and re-checks a whitelist),
        the signed order is POSTed to the CLOB as FOK (fill-or-kill), and the exchange answer is recorded.

Per-rule accounting (data/live_results.csv) and stops:
  - a rule whose realised loss today <= -MAX_DAILY_LOSS_VARIANT stops trading until the next (Taiwan) day
  - if the realised loss of all rules together <= -MAX_DAILY_LOSS_TOTAL, every rule stops
  - if KILL_FILE exists, every rule stops (manual kill switch:  touch data/STOP)
Secrets (signer token, CLOB L2 credentials) live only in btc5m_sim/.live.env (0600). The private key is never on this host.
"""
import base64, csv, hashlib, hmac, http.client, json, os, queue, threading, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta

MODE = os.getenv("EXEC_MODE", "off").strip().lower()
DATA = os.getenv("DATA_DIR", "data")
TZ = timezone(timedelta(hours=int(os.getenv("TZ_OFFSET", "8"))))
LIVE_VARIANTS = {v.strip() for v in os.getenv("LIVE_VARIANTS", "").split(",") if v.strip()}   # empty = every rule incl. "main"
LIVE_SHARES = float(os.getenv("LIVE_SHARES", "5"))
MAX_SHARES = float(os.getenv("LIVE_MAX_SHARES", "10"))
# Size from the wallet (fish 2026-10-26: "我們沒有時間了"): each order ~ LIVE_WALLET_FRACTION of equity (free USDC + unsettled
# orders), so profits grow the size immediately and losses shrink it. Never below the exchange minimum, never above MAX_SHARES.
SIZE_MODE = os.getenv("LIVE_SIZE_MODE", "fixed").strip().lower()      # fixed | wallet
WALLET_FRACTION = float(os.getenv("LIVE_WALLET_FRACTION", "0.125"))
MIN_SHARES = float(os.getenv("LIVE_MIN_SHARES", "5"))
# fish 2026-10-22 16:00: no more "four or five $15 orders on one coin in one market". Per market, at most this many
# real orders (first rules to fire win the slots). 0 = unlimited.
MAX_ORDERS_PER_MARKET = int(os.getenv("LIVE_MAX_ORDERS_PER_MARKET", "2"))
# US stock market session pause (fish 2026-10-24: "美股開盤時段（只有週一到五，考慮夏令時間）BTC、SOL 自動不送真單，程式照跑收資料").
# Since 15-share sizing, BTC lost 12.7% of real orders (net -$32.84) in Taiwan 21:00-04:00 vs 2.3% (+$117.62) the rest of the day.
# Regular session = Mon-Fri 09:30-16:00 New York time; the tz database handles daylight saving (EDT: Taiwan 21:30-04:00,
# EST from 2026-11-02: 22:30-05:00). Listed NYSE holidays are closed all day, listed early closes end at their time.
# Holidays / early closes must be extended each year (US_MARKET_HOLIDAYS / US_MARKET_EARLY_CLOSE override the defaults).
PAUSE_US_SESSION = os.getenv("LIVE_PAUSE_US_SESSION", "0").strip().lower() in ("1", "true", "yes", "on")
US_TZ = os.getenv("US_SESSION_TZ", "America/New_York")
US_SESSION_START = os.getenv("US_SESSION_START", "09:30")
US_SESSION_END = os.getenv("US_SESSION_END", "16:00")
US_HOLIDAYS = {d.strip() for d in os.getenv("US_MARKET_HOLIDAYS", "2026-11-26,2026-12-25").split(",") if d.strip()}
US_EARLY_CLOSE = {k.strip(): v.strip() for k, v in (x.split("=", 1) for x in os.getenv("US_MARKET_EARLY_CLOSE", "2026-11-27=13:00,2026-12-24=13:00").split(",") if "=" in x)}


# Scheduled-event pause (fish 2026-10-26: "排程事件停單，行事曆用設定檔維護"). EVENTS_FILE lines:
#   <start ISO local time> <IANA tz> <minutes before> <minutes after> <label…>
#   2026-10-28T14:00 America/New_York 30 120 FOMC 利率決議＋記者會
# Real orders stop from start-before to start+after. Blank lines and # comments ignored. The file is re-read when it changes
# (no restart). Bad lines are skipped and logged once. 0 effect when the file is missing.
EVENTS_FILE = os.getenv("EVENTS_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "events.txt"))
_events = {"mtime": None, "items": [], "bad": set()}


def load_events(path=None, now=None):
    """Parsed calendar as a list of (start_utc, end_utc, label); cached on the file's mtime."""
    from zoneinfo import ZoneInfo
    path = path or EVENTS_FILE
    try: mtime = os.path.getmtime(path)
    except OSError:
        _events.update(mtime=None, items=[]); return []
    if _events["mtime"] == mtime:
        return _events["items"]
    items = []
    for ln, raw in enumerate(open(path, encoding="utf-8"), 1):
        s = raw.strip()
        if not s or s.startswith("#"): continue
        try:
            when, tz, before, after, *label = s.split()
            start = datetime.fromisoformat(when).replace(tzinfo=ZoneInfo(tz))
            items.append((start - timedelta(minutes=float(before)), start + timedelta(minutes=float(after)), " ".join(label) or when))
        except Exception as e:
            if (path, ln) not in _events["bad"]:
                _events["bad"].add((path, ln)); _log(f"live: events.txt line {ln} ignored ({type(e).__name__}): {s[:60]}")
    _events.update(mtime=mtime, items=items)
    return items


def event_pause(now=None):
    """Label of the scheduled event whose window covers now, else None."""
    now = now or datetime.now(timezone.utc)
    for start, end, label in load_events():
        if start <= now < end:
            return label
    return None


def _ny(now=None):
    from zoneinfo import ZoneInfo
    return (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(US_TZ))


def us_session_open(now=None):
    """True while the US stock market's regular session is open (see PAUSE_US_SESSION). now: an aware datetime (tests)."""
    t = _ny(now)
    if t.weekday() >= 5:
        return False
    d = t.strftime("%Y-%m-%d")
    if d in US_HOLIDAYS:
        return False
    hm = t.strftime("%H:%M")
    return US_SESSION_START <= hm < US_EARLY_CLOSE.get(d, US_SESSION_END)


# Loss cool-down = reversal-regime detector (fish 2026-10-26: "我們需要偵測這種行情…最大殺傷對手"). Losses come in clusters
# (10-24: 14 replayed losses in one day). Backtest on 8 days of BTC (1,960 markets, current rules, 15 sh): pausing real
# orders for 12 markets after any loss of the live rule set (real or shadow) -> 853 trades / 26 losses / +$292 vs no
# detection 1,050 / 39 / +$273; worst day -$89 -> -$15 (10-24 itself -$5). SOL rarely loses (4 in 1,083): 6 markets.
# Shadow losses count too, so the pause keeps extending while the bad regime lasts even though no real order goes out.
COOLDOWN_MARKETS = int(os.getenv("LIVE_LOSS_COOLDOWN_MARKETS", "0"))


def cooldown_until():
    try:
        return float(open(os.path.join(DATA, "COOLDOWN_UNTIL")).read().strip() or 0)
    except (OSError, ValueError):
        return 0.0


def note_market(slug, entries, now=None):
    """Called by the simulator once a market resolved. entries: (secs_left, side, won) of every live-listed rule that entered
    (shadow included). The live set is emulated (earliest first, one direction, at most MAX_ORDERS_PER_MARKET); any loss in it
    starts or extends the cool-down. Returns True when a loss was seen."""
    if not COOLDOWN_MARKETS:
        return False
    taken, held = [], None
    for secs, side, won in sorted(entries, key=lambda e: -float(e[0] or 0)):
        if held and side != held: continue
        if MAX_ORDERS_PER_MARKET and len(taken) >= MAX_ORDERS_PER_MARKET: break
        held = held or side; taken.append(won)
    if not any(w in (0, "0", False) for w in taken):
        return False
    now = now or time.time(); old = cooldown_until(); until = max(old, now + COOLDOWN_MARKETS * 300)
    try:
        open(os.path.join(DATA, "COOLDOWN_UNTIL"), "w").write(str(int(until)))
    except OSError:
        pass
    if old <= now:                                                   # a new cool-down, not an extension: tell once
        msg = f"⏸ [{ASSET.upper()}] 剛出現輸單（{slug[-10:]}），進入冷卻：{COOLDOWN_MARKETS * 5} 分鐘內不下真單，到 {datetime.fromtimestamp(until, TZ).strftime('%H:%M')}；期間影子再輸會自動延長"
        _log("live: " + msg); _notify(msg)
    return True


def review_block(variant, now=None):
    """Blocks set by review.py (2026-10-26 self-review), read on every order; missing files = no effect.
    DATA/PAUSE_UNTIL holds an epoch second: no real orders on this coin until then. DATA/DISABLED_RULES lists one rule per
    line: those rules send no real orders (shadow keeps recording). The review only adds blocks; a human removes them."""
    try:
        p = os.path.join(DATA, "PAUSE_UNTIL")
        if os.path.exists(p):
            until = float(open(p).read().strip() or 0)
            if (now or time.time()) < until:
                return f"paused by review until {datetime.fromtimestamp(until, TZ).strftime('%m-%d %H:%M')}"
        p = os.path.join(DATA, "DISABLED_RULES")
        if os.path.exists(p) and variant in {l.strip() for l in open(p) if l.strip()}:
            return "rule disabled by review"
    except (OSError, ValueError):
        pass
    return None


def us_session_close_local(now=None):
    """Today's session close as Taiwan clock time, for messages (e.g. '04:00')."""
    from zoneinfo import ZoneInfo
    t = _ny(now); d = t.strftime("%Y-%m-%d"); hh, mm = US_EARLY_CLOSE.get(d, US_SESSION_END).split(":")
    close = t.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    return close.astimezone(TZ).strftime("%H:%M")
SLIP = float(os.getenv("LIVE_SLIP", "0.01"))                 # pay up to ask + SLIP so a FOK still fills if the book moved
# 2026-10-27 18:34 (BTC: decided on Down at 0.84, filled at an average of 0.57, lost): a FAK spends the whole USDC amount at
# whatever asks sit at or below the limit, so when the side collapses during the ~1 s between the decision and the match we
# buy MORE shares of a side that is losing its lead. 661 real fills: the 54 filled >= 0.05 under the ask seen lost 14 (-$32.7),
# the 485 filled at the ask or above lost 10 (+$92.7). So right before signing and again right before the POST the submit
# thread re-reads the websocket book (no network): if the side's best ask has fallen by RECHECK_DROP or more under the ask
# the rule decided on, or has risen above the limit, the order is not sent (status "aborted"). 0 = off.
RECHECK_DROP = float(os.getenv("LIVE_RECHECK_DROP", "0.05"))
FEE_RATE = float(os.getenv("LIVE_FEE_RATE", "0.07"))         # Polymarket crypto taker fee rate: fee = rate x p x (1-p) x shares (USDC, on top)
PRICE_MIN, PRICE_MAX = 0.5, 0.99
MAX_DAILY_LOSS_VARIANT = float(os.getenv("MAX_DAILY_LOSS_VARIANT", "15"))
MAX_DAILY_LOSS_TOTAL = float(os.getenv("MAX_DAILY_LOSS_TOTAL", "50"))
KILL_FILE = os.getenv("KILL_FILE", os.path.join(DATA, "STOP"))
# Capital floor (fish 2026-10-22: no daily volume cap; only stop when the wallet's USDC drops below this).
MIN_BALANCE_USDC = float(os.getenv("MIN_BALANCE_USDC", "50"))
# Early warning (fish 2026-10-26, wallet down to ~$34): one Telegram a day when equity = free USDC + money sitting in
# not-yet-settled orders drops below this. Equity, not free USDC, so money locked in open markets does not raise false alarms.
MIN_BALANCE_WARN = float(os.getenv("MIN_BALANCE_WARN", "0"))
BALANCE_TTL = float(os.getenv("BALANCE_TTL", "60"))         # seconds between balance refreshes
_balance = {"usdc": None, "at": 0.0, "alerted": None}
# 2026-10-24 order speed-up (fish: "只做送單加速，策略不動"). live_timing showed ~1.85 s from book to exchange, of which
# ~0.7 s was guard() refreshing the wallet balance inline through the Sofia proxy (every market's first order: the cache
# had always expired) and ~0.2 s was GET /tick-size. Now a background thread refreshes the balance every BALANCE_REFRESH s;
# the order guard reads that cache and only fetches inline when it is older than BALANCE_STALE (thread dead). The $50 floor
# is unchanged. Tick sizes are looked up once per market when its book window opens (prefetch_tick), never in the order path.
BALANCE_REFRESH = float(os.getenv("BALANCE_REFRESH", "30"))
BALANCE_STALE = float(os.getenv("BALANCE_STALE", "300"))
_ticks = {}      # token_id -> tick size string, filled by prefetch_tick() / the first order that had to look it up
ASSET = os.getenv("ASSET", "btc").strip().lower()
PERIOD = int(os.getenv("PERIOD", "300"))
SLUG_PREFIX = f"{ASSET}-updown-{PERIOD // 60}m-"
CLOB = os.getenv("CLOB_HOST", "https://clob.polymarket.com")
# Polymarket refuses orders from US IPs (racknerd is in New York). Authenticated exchange calls (POST /order,
# /auth/derive-api-key, /data/*) go through this HTTP proxy when set — on racknerd it is the local sing-box
# inbound whose outbound exits in Sofia (BG, allowed). Market data (gamma, /book, /tick-size) stays direct for speed.
ORDER_PROXY = os.getenv("ORDER_PROXY", "")
ORDER_TYPE = os.getenv("LIVE_ORDER_TYPE", "FAK")          # FAK: take what is there at <= cap, cancel the rest (FOK lost half the chances)
WARM_EVERY = float(os.getenv("LIVE_WARM_EVERY", "25"))    # keep the exchange connections (through the proxy) open with a cheap GET
_pool = queue.Queue()                                     # warm HTTPS connections to the CLOB (tunnelled through the proxy)
SIGNER_URL = os.getenv("SIGNER_URL", "")                     # e.g. https://rcss-signer.../btc5m/sign
SIGNER_TOKEN = os.getenv("SIGNER_TOKEN", "")
SIGNER_ADDRESS = os.getenv("SIGNER_ADDRESS", "")             # EOA that signs (POLY_ADDRESS header)
API_KEY = os.getenv("CLOB_API_KEY", ""); API_SECRET = os.getenv("CLOB_API_SECRET", ""); API_PASSPHRASE = os.getenv("CLOB_API_PASSPHRASE", "")
DEFAULT_TICK = os.getenv("LIVE_TICK", "0.001")
GAMMA = "https://gamma-api.polymarket.com/events?slug=" + ASSET + "-updown-" + f"{PERIOD // 60}m" + "-{}"
ZERO32 = "0x" + "0" * 64
UA = {"User-Agent": "btc5m-live"}
LIVE_ENV = os.getenv("LIVE_ENV_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), ".live.env"))
_ident = {}      # {"address": EOA, "proxyWallet": maker} from btc5m_whoami, fetched once

OFIELDS = ["ts", "mode", "variant", "slug", "outcome", "token_id", "ask", "price", "size", "tick", "neg_risk",
           "order_type", "status", "order_id", "filled_size", "cost", "secs_left", "msg"]
RFIELDS = ["ts", "variant", "slug", "outcome", "size", "cost", "fee", "winner", "won", "pnl", "day"]
# live_timing.csv (2026-10-23, measure only — fish: "只量不改策略"): where the time goes between seeing the book and the
# exchange's answer. 10-23 21:39 SOL: decided on Down 0.88 at 56.6s, the market flipped to Up 0.64 by 54.5s, the order filled at
# an average 0.384 -> it landed after the flip. Columns are milliseconds; blank = step not reached (dry run, rejected earlier).
#   book_ms   fetching both order books (sequential)        book_age_ms  book fetched -> submit() called (evaluation of rules)
#   queue_ms  submit() -> worker thread running             guard_ms     guard() incl. the cached wallet balance (HTTP when stale)
#   tick_ms   GET /tick-size                                prep_ms      identity (cached) + building the order
#   sign_ms   signer Worker round trip                      post_ms      POST /order round trip (incl. one retry on 5xx)
#   total_ms  book fetched -> exchange answered             fill_avg     USDC spent / shares received (filled orders)
TFIELDS = ["ts", "variant", "slug", "outcome", "status", "secs_left", "ask", "fill_avg", "book_ms", "book_age_ms", "queue_ms",
           "guard_ms", "tick_ms", "prep_ms", "sign_ms", "post_ms", "retried", "total_ms"]

_lock = threading.Lock()
_notify = lambda text: None
_orders = {}        # (variant, slug) -> order row dict (filled or pending)
_last = {}          # (variant, slug) -> (status, short msg) of the last attempt, for the decision log
_daily = {}         # day -> {variant: realised pnl}
_stopped = set()    # variants stopped today (reset on day change)
_stop_all_day = None
_log = print


def init(notify=None, log=None):
    """Called once by sim.main(). Loads today's realised results so a restart keeps the stops."""
    global _notify, _log
    if notify: _notify = notify
    if log: _log = log
    os.makedirs(DATA, exist_ok=True)
    p = os.path.join(DATA, "live_results.csv")
    if os.path.exists(p):
        with open(p, newline="") as f:
            for r in csv.DictReader(f):
                _daily.setdefault(r["day"], {}); _daily[r["day"]][r["variant"]] = _daily[r["day"]].get(r["variant"], 0.0) + float(r["pnl"] or 0)
    _recover_pending()
    if MODE == "live" and WARM_EVERY > 0:
        threading.Thread(target=_keep_warm, daemon=True).start()
    if MODE == "live" and SIGNER_KEEPALIVE and SIGNER_URL.startswith("https://") and SIGNER_WARM_EVERY > 0:
        threading.Thread(target=_signer_keep_warm, daemon=True).start()
    if MODE == "live":
        threading.Thread(target=_balance_loop, daemon=True).start()
    if MODE == "live" and (PAUSE_US_SESSION or os.path.exists(EVENTS_FILE)):
        threading.Thread(target=_session_loop, daemon=True).start()
    if os.path.exists(EVENTS_FILE):
        _log(f"live: events calendar {EVENTS_FILE}: {len(load_events())} 個事件" + (f"，現在暫停中：{event_pause()}" if event_pause() else ""))
    _log(f"live: mode={MODE} shares={LIVE_SHARES:g}{f' (wallet x{WALLET_FRACTION:g}, {MIN_SHARES:g}-{MAX_SHARES:g})' if SIZE_MODE == 'wallet' else ''} max/market={MAX_ORDERS_PER_MARKET} slip={SLIP} proxy={ORDER_PROXY or 'direct'} type={ORDER_TYPE} stops: rule/day -{MAX_DAILY_LOSS_VARIANT:g} total/day -{MAX_DAILY_LOSS_TOTAL:g} floor ${MIN_BALANCE_USDC:g} warn ${MIN_BALANCE_WARN:g} cooldown {COOLDOWN_MARKETS} mkts kill={KILL_FILE}"
         + (f" rules={sorted(LIVE_VARIANTS)}" if LIVE_VARIANTS else " rules=all")
         + (f" us_pause=on ({US_SESSION_START}-{US_SESSION_END} {US_TZ} Mon-Fri, now {'OPEN -> paused' if us_session_open() else 'closed'})" if PAUSE_US_SESSION else " us_pause=off"))
    if MODE == "live" and not (SIGNER_URL and SIGNER_TOKEN and SIGNER_ADDRESS and API_KEY and API_SECRET and API_PASSPHRASE):
        _log("live: MISSING credentials -> falling back to dry")
        return "dry"
    return MODE


def _recover_pending():
    """After a restart, filled orders whose market has not been booked yet are re-registered and settled by a
    small poller, so restarts never lose real P&L (the sim only settles markets it watched itself)."""
    op = os.path.join(DATA, "live_orders.csv"); rp = os.path.join(DATA, "live_results.csv")
    if not os.path.exists(op): return
    booked = set()
    if os.path.exists(rp):
        with open(rp, newline="") as f:
            booked = {(r["variant"], r["slug"]) for r in csv.DictReader(f)}
    pending = {}
    with open(op, newline="") as f:
        for r in csv.DictReader(f):
            if r.get("mode") == "live" and r.get("status") == "filled" and (r["variant"], r["slug"]) not in booked:
                pending[(r["variant"], r["slug"])] = r
    if not pending: return
    with _lock:
        for k, r in pending.items():
            _orders[k] = dict(r, filled_size=float(r["filled_size"] or 0), cost=float(r["cost"] or 0), size=float(r["size"] or 0), price=float(r["price"] or 0))
    slugs = sorted({s for _, s in pending})
    _log(f"live: recovering {len(pending)} filled order(s) on {len(slugs)} market(s) not yet settled: {slugs}")
    for s in slugs:
        threading.Thread(target=_settle_when_resolved, args=(s,), daemon=True).start()


def _settle_when_resolved(slug):
    start = int(slug.rsplit("-", 1)[-1])
    for _ in range(80):                                   # up to ~40 min
        try:
            ev = _get(GAMMA.format(start), timeout=8)
            if ev:
                m = ev[0]["markets"][0]
                if m.get("closed"):
                    prices = json.loads(m["outcomePrices"]); outcomes = json.loads(m["outcomes"])
                    settle(slug, outcomes[0] if float(prices[0]) > 0.5 else outcomes[1]); return
        except Exception as e:
            _log(f"live: recover {slug} poll error {type(e).__name__}")
        time.sleep(30)
    _log(f"live: recover {slug} gave up (unresolved after 40 min)")


def status_of(variant, slug):
    """(status, msg) of this rule's live attempt on this market, or None when nothing was attempted."""
    return _last.get((variant, slug))


def today():
    return datetime.now(TZ).strftime("%Y-%m-%d")


def realised(variant=None, day=None):
    d = _daily.get(day or today(), {})
    return sum(d.values()) if variant is None else d.get(variant, 0.0)


def wallet_balance(force=False, max_age=None):
    """Available USDC in the proxy wallet via the CLOB (authenticated, proxied). Cached max_age (default BALANCE_TTL) seconds.
    Returns None when it cannot be read (then the guard lets orders through: a read failure must not freeze trading)."""
    now = time.time()
    ttl = BALANCE_TTL if max_age is None else max_age
    if not force and _balance["usdc"] is not None and now - _balance["at"] < ttl:
        return _balance["usdc"]
    if not (API_KEY and API_SECRET and API_PASSPHRASE and SIGNER_ADDRESS): return None
    try:
        h = l2_headers("GET", "/balance-allowance", "")
        d = _get(f"{CLOB}/balance-allowance?asset_type=COLLATERAL&signature_type=1", timeout=10, headers=h, proxied=True)
        usdc = int(d.get("balance", 0)) / 1e6
        _balance.update(usdc=usdc, at=now)
        return usdc
    except Exception as e:
        if now - _balance.get("fail_log", 0.0) > 600:            # the background refresh runs every 30 s: log a failure streak once
            _balance["fail_log"] = now; _log(f"live: balance read failed {type(e).__name__}")
        return _balance["usdc"]


def _balance_once():
    """One background balance refresh. Skipped while the kill file is present: no order can go out then."""
    if os.path.exists(KILL_FILE):
        return None
    bal = wallet_balance(force=True)
    _warn_low(bal)
    return bal


def order_shares(price):
    """Shares for the next order: LIVE_SHARES in fixed mode; in wallet mode equity x LIVE_WALLET_FRACTION / price."""
    if SIZE_MODE != "wallet":
        return LIVE_SHARES
    bal = _balance.get("usdc")
    if bal is None:
        return min(LIVE_SHARES, MAX_SHARES)
    sh = (bal + _pending_cost()) * WALLET_FRACTION / max(price, 0.01)
    return round(max(MIN_SHARES, min(MAX_SHARES, sh)), 2)


def _pending_cost():
    with _lock:
        return sum(float(r.get("cost") or 0) for r in _orders.values() if r.get("status") == "filled")


def _warn_low(bal):
    """Telegram once a day when equity falls under MIN_BALANCE_WARN (the floor itself still stops buying)."""
    if not MIN_BALANCE_WARN or bal is None:
        return
    pending = _pending_cost(); equity = bal + pending
    if equity < MIN_BALANCE_WARN and _balance.get("warned") != today():
        _balance["warned"] = today()
        _notify(f"⚠️ [{ASSET.upper()}] 錢包預警：可用 ${bal:.2f}＋未結算 ${pending:.2f}＝${equity:.2f}，低於預警線 ${MIN_BALANCE_WARN:g}；"
                f"可用低於 ${MIN_BALANCE_USDC:g} 會自動停買")


def _pause_state():
    """(kind, text) of the schedule pause in force: US session, a calendar event, or none."""
    if PAUSE_US_SESSION and us_session_open():
        return "us", f"美股開盤，真單暫停到收盤（台灣 {us_session_close_local()}）"
    ev = event_pause()
    if ev:
        end = next((e for s, e, l in load_events() if l == ev and s <= datetime.now(timezone.utc) < e), None)
        return "event", f"排程事件「{ev}」，真單暫停到 {end.astimezone(TZ).strftime('%H:%M') if end else '事件結束'}（台灣）"
    return None, ""


def _session_loop(every=30.0):
    """Tell Telegram when a schedule pause (US session or calendar event) starts and ends; quiet while the kill file is in place."""
    state = _pause_state()[0]
    while True:
        time.sleep(every)
        try:
            kind, text = _pause_state()
            if kind == state:
                continue
            state = kind
            msg = (f"⏸ [{ASSET.upper()}] {text}，程式照跑收資料" if kind else f"▶️ [{ASSET.upper()}] 暫停結束，恢復送真單")
            _log("live: " + msg)
            if not os.path.exists(KILL_FILE):
                _notify(msg)
        except Exception as e:
            _log(f"live: session loop error {type(e).__name__}")


def _balance_loop():
    while True:
        try: _balance_once()
        except Exception: pass
        time.sleep(BALANCE_REFRESH)


def _prefetch_tick_now(market):
    for tok in market.get("tokens", []):
        if str(tok) in _ticks: continue
        try:
            _ticks[str(tok)] = str(_get(f"{CLOB}/tick-size?token_id={tok}").get("minimum_tick_size", DEFAULT_TICK))
        except Exception as e:
            _log(f"live: tick prefetch failed {type(e).__name__}")
    if len(_ticks) > 400:
        for k in list(_ticks)[:200]: _ticks.pop(k, None)


def prefetch_tick(market):
    """Look the market's tick size up once, in the background, when its book window opens, so no order waits for it."""
    if MODE != "live":
        return
    threading.Thread(target=_prefetch_tick_now, args=(market,), daemon=True).start()


def guard(variant, slug, price, size, mode=None, outcome=None):
    """Return (ok, reason). Every reason is recorded in the ledger so skipped orders are visible.
    outcome: the side this order would buy. One market, one direction: once any rule holds Up here, no rule may buy
    Down (2026-10-22 15:48 BTC and ETH both bought Up at 120s then Down at 60s — paying $72 for a $60 max payout)."""
    mode = mode or MODE
    if mode not in ("dry", "live"): return False, "mode off"
    if os.path.exists(KILL_FILE): return False, "kill file"
    if PAUSE_US_SESSION and us_session_open(): return False, "US stock session pause"
    ev = event_pause()
    if ev: return False, f"event pause: {ev}"
    rb = review_block(variant)
    if rb: return False, rb
    cu = cooldown_until() if COOLDOWN_MARKETS else 0
    if cu and time.time() < cu: return False, f"cooldown after loss until {datetime.fromtimestamp(cu, TZ).strftime('%m-%d %H:%M')}"
    if LIVE_VARIANTS and variant not in LIVE_VARIANTS: return False, "rule not enabled"
    if not slug.startswith(SLUG_PREFIX): return False, "not a btc 5m market"
    if not (PRICE_MIN <= price <= PRICE_MAX): return False, f"price {price} out of range"
    if size <= 0 or size > MAX_SHARES: return False, f"size {size} out of range"
    if (variant, slug) in _orders: return False, "already ordered this market"
    same = [r for (v, s), r in _orders.items() if s == slug and r.get("status") in ("", "filled", "dry", "pending")]
    if outcome is not None:
        held = {r.get("outcome") for r in same}
        if held and outcome not in held: return False, f"opposite side already bought ({'/'.join(sorted(held))})"
    if MAX_ORDERS_PER_MARKET and len(same) >= MAX_ORDERS_PER_MARKET: return False, f"market already has {len(same)} order(s) (cap {MAX_ORDERS_PER_MARKET})"
    if realised() <= -MAX_DAILY_LOSS_TOTAL: return False, f"total daily loss {realised():+.2f} hit stop"
    if mode == "live":
        bal = wallet_balance(max_age=BALANCE_STALE)                # cache kept fresh by _balance_loop; inline only if it died
        if bal is not None and bal < MIN_BALANCE_USDC:
            if _balance["alerted"] != today():
                _balance["alerted"] = today(); _notify(f"⛔ 錢包 USDC ${bal:.2f} 低於下限 ${MIN_BALANCE_USDC:g}，停止下單（回到下限以上自動恢復）")
            return False, f"wallet ${bal:.2f} below floor ${MIN_BALANCE_USDC:g}"
    if realised(variant) <= -MAX_DAILY_LOSS_VARIANT: return False, f"rule daily loss {realised(variant):+.2f} hit stop"
    return True, ""


def tick_round(price, tick):
    t = float(tick); n = round(price / t)
    return round(n * t, 6)


def amounts(price, size, tick=DEFAULT_TICK):
    """V2 market BUY (FOK): makerAmount = USDC to the cent (the venue rejects finer), takerAmount = shares derived
    from it, truncated to tick_decimals + 2 places. Returns (maker_usdc_6dp, taker_shares_6dp, shares_float, usdc_float)."""
    usdc = int(size * price * 100 + 1e-9) / 100.0     # floor to cents
    dec = len(str(tick).split(".")[1]) if "." in str(tick) else 0
    scale = 10 ** (dec + 2)
    shares = int(usdc / price * scale + 1e-9) / scale  # truncate
    return int(round(usdc * 1e6)), int(round(shares * 1e6)), shares, usdc


def new_salt():
    return int.from_bytes(os.urandom(7), "big") & ((1 << 53) - 1)   # fits a JS safe integer, like the SDK


# 2026-10-27 signer keep-alive (fish: 降低延遲). live_timing: sign_ms median 550-740 ms, of which a new TCP + TLS handshake
# to the Worker edge is ~110 ms per order (curl: connect 30 ms, TLS 75 ms). The sign call now reuses one warm HTTPS
# connection (fresh one on any socket error, once), warmed every SIGNER_WARM_EVERY s with a GET the Worker answers 405
# to. identity() is also fetched at start-up so the first order after a restart does not pay the whoami round trip.
SIGNER_KEEPALIVE = os.getenv("SIGNER_KEEPALIVE", "1").strip().lower() in ("1", "true", "yes", "on")
SIGNER_WARM_EVERY = float(os.getenv("SIGNER_WARM_EVERY", "60"))
_signer_pool = queue.Queue()


def _signer_conn(timeout=8):
    u = urllib.parse.urlsplit(SIGNER_URL)
    c = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout)
    c.connect()
    return c


def worker(op, payload=None, timeout=8):
    """POST one op to the signer Worker, on a kept-alive connection when SIGNER_KEEPALIVE (default). Never logs the token."""
    body = json.dumps({"op": op, **(payload or {})})
    headers = {**UA, "content-type": "application/json", "authorization": f"Bearer {SIGNER_TOKEN}"}
    if SIGNER_KEEPALIVE and SIGNER_URL.startswith("https://"):
        u = urllib.parse.urlsplit(SIGNER_URL); path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        for attempt in (1, 2):
            c = None
            if attempt == 1:
                try: c = _signer_pool.get_nowait()
                except queue.Empty: c = None
            try:
                if c is None: c = _signer_conn(timeout)
                c.request("POST", path, body=body.encode(), headers=headers)
                r = c.getresponse(); data = r.read()
                _signer_pool.put(c)
                try: return r.status, json.loads(data)
                except Exception: return r.status, {"ok": False, "reason": f"http {r.status}"}
            except (http.client.HTTPException, OSError):
                try: c.close()
                except Exception: pass
                if attempt == 2: raise
    req = urllib.request.Request(SIGNER_URL, data=body.encode(), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try: return e.code, json.load(e)
        except Exception: return e.code, {"ok": False, "reason": f"http {e.code}"}


def _signer_keep_warm():
    """Keep one warm connection to the Worker: a GET on the sign path every SIGNER_WARM_EVERY s (405, ~0.1 s), so the
    next signature skips the handshake. Also fetches identity() once so the first order does not pay for whoami."""
    try: identity()
    except Exception as e: _log(f"live: identity prefetch failed {type(e).__name__}")
    u = urllib.parse.urlsplit(SIGNER_URL); path = u.path or "/"
    while True:
        time.sleep(SIGNER_WARM_EVERY)
        c = None
        try:
            try: c = _signer_pool.get_nowait()
            except queue.Empty: c = _signer_conn()
            c.request("GET", path, headers=UA); c.getresponse().read(); _signer_pool.put(c)
        except Exception:
            try: c.close()
            except Exception: pass


def identity():
    """EOA + proxy wallet from the Worker (cached). Public addresses, no secrets."""
    if not _ident:
        st, d = worker("btc5m_whoami")
        if st != 200 or not d.get("ok"): raise RuntimeError(f"whoami {st} {str(d)[:100]}")
        _ident.update(address=d["address"], proxyWallet=d["proxyWallet"])
    return _ident


def l1_headers(timestamp, nonce=0):
    """L1 headers for /auth/derive-api-key: the Worker signs the ClobAuth EIP-712 message; the key never leaves it."""
    st, d = worker("btc5m_auth", {"timestamp": str(timestamp), "nonce": nonce})
    if st != 200 or not d.get("ok"): raise RuntimeError(f"auth {st} {str(d)[:100]}")
    return {"POLY_ADDRESS": d["address"], "POLY_SIGNATURE": d["signature"], "POLY_TIMESTAMP": str(timestamp), "POLY_NONCE": str(nonce)}


def derive_l2():
    """GET /auth/derive-api-key with Worker-signed L1 headers -> (key, secret, passphrase, address). Values are never printed."""
    ts = int(time.time())
    h = l1_headers(ts)
    d = _get(f"{CLOB}/auth/derive-api-key", timeout=12, headers=h, proxied=True)
    key = d.get("apiKey") or d.get("api_key"); sec = d.get("secret"); pw = d.get("passphrase")
    if not (key and sec and pw): raise RuntimeError("derive-api-key: unexpected response shape")
    return key, sec, pw, h["POLY_ADDRESS"]


def write_live_env(key, sec, pw, address):
    """Write/refresh btc5m_sim/.live.env (0600) in place, keeping other lines. Never echoes values."""
    lines = []
    if os.path.exists(LIVE_ENV):
        with open(LIVE_ENV) as f: lines = [l.rstrip("\n") for l in f]
    vals = {"CLOB_API_KEY": key, "CLOB_API_SECRET": sec, "CLOB_API_PASSPHRASE": pw, "SIGNER_ADDRESS": address}
    out = [l for l in lines if not any(l.startswith(k + "=") for k in vals)] + [f"{k}={v}" for k, v in vals.items()]
    fd = os.open(LIVE_ENV, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f: f.write("\n".join(out) + "\n")
    os.chmod(LIVE_ENV, 0o600)


def l2_headers(method, path, body_str):
    ts = str(int(time.time()))
    msg = ts + method + path + (body_str or "")
    sig = base64.urlsafe_b64encode(hmac.new(base64.urlsafe_b64decode(API_SECRET), msg.encode(), hashlib.sha256).digest()).decode()
    return {"POLY_ADDRESS": SIGNER_ADDRESS, "POLY_SIGNATURE": sig, "POLY_TIMESTAMP": ts, "POLY_API_KEY": API_KEY, "POLY_PASSPHRASE": API_PASSPHRASE}


def _clob_host():
    return urllib.parse.urlsplit(CLOB).hostname


def _new_conn():
    """One HTTPS connection to the CLOB, tunnelled through ORDER_PROXY (CONNECT) when set."""
    if ORDER_PROXY:
        u = urllib.parse.urlsplit(ORDER_PROXY)
        c = http.client.HTTPSConnection(u.hostname, u.port or 80, timeout=12)
        c.set_tunnel(_clob_host(), 443)
    else:
        c = http.client.HTTPSConnection(_clob_host(), 443, timeout=12)
    c.connect()
    return c


def clob_request(method, path, body=None, headers=None, _retry=True):
    """Exchange call on a pooled, already-open connection (saves the TLS + proxy handshake per order).
    Returns (status, json). Reconnects once on a dead socket. _retry=False also forces a fresh connection."""
    c = None
    if _retry:
        try: c = _pool.get_nowait()
        except queue.Empty: c = None
    if c is None:
        c = _new_conn()
    try:
        c.request(method, path, body=body, headers={**UA, "content-type": "application/json", **(headers or {})})
        r = c.getresponse(); data = r.read()
        try: j = json.loads(data)
        except Exception: j = {"raw": data[:200].decode(errors="replace")}
        _pool.put(c)
        return r.status, j
    except (http.client.HTTPException, OSError) as e:
        try: c.close()
        except Exception: pass
        if _retry:
            return clob_request(method, path, body, headers, _retry=False)
        raise


def _keep_warm(n=3):
    """Hold n warm connections; touch each every WARM_EVERY seconds with GET /time so proxies and Cloudflare keep them open."""
    while True:
        try:
            conns = []
            for _ in range(n):
                try: conns.append(_pool.get_nowait())
                except queue.Empty: conns.append(_new_conn())
            for c in conns:
                try:
                    c.request("GET", "/time", headers=UA); c.getresponse().read(); _pool.put(c)
                except (http.client.HTTPException, OSError):
                    try: c.close()
                    except Exception: pass
        except Exception as e:
            _log(f"live: keep-warm {type(e).__name__}")
        time.sleep(WARM_EVERY)


def _opener(proxied):
    """urllib opener; when `proxied` and ORDER_PROXY is set, route through the proxy (never for market data)."""
    if proxied and ORDER_PROXY:
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": ORDER_PROXY, "https": ORDER_PROXY}))
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get(url, timeout=6, headers=None, proxied=False):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    try:
        with _opener(proxied).open(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try: return json.load(e)
        except Exception: raise


def _post(url, body_str, headers, timeout=10, proxied=False):
    req = urllib.request.Request(url, data=body_str.encode(), method="POST", headers={**UA, "content-type": "application/json", **headers})
    try:
        with _opener(proxied).open(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try: return e.code, json.load(e)
        except Exception: return e.code, {"success": False, "errorMsg": f"http {e.code}"}


def _append(name, fields, row):
    p = os.path.join(DATA, name); new = not os.path.exists(p)
    with open(p, "a", newline="") as f:
        w = csv.DictWriter(f, fields)
        if new: w.writeheader()
        w.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in fields})


def submit(variant, market, side_idx, ask, secs_left, mode=None, seen_at=None, book_ms=None, book_fn=None):
    """Non-blocking: builds + (live) sends one BUY for `variant` on `market` (dict from sim.market()).
    seen_at / book_ms: when the order books behind this decision were fetched and how long that took (timing only).
    book_fn: no-argument callable returning the side's current best ask from the websocket book (None = unknown); the
    pre-send re-check (RECHECK_DROP) calls it right before signing and right before the POST."""
    threading.Thread(target=_submit, args=(variant, market, side_idx, ask, secs_left, mode),
                     kwargs=dict(seen_at=seen_at, book_ms=book_ms, t_call=time.time(), book_fn=book_fn), daemon=True).start()


def _book_moved(book_fn, ask, limit, stage):
    """The pre-send re-check (see RECHECK_DROP): the side's best ask now against the ask the rule decided on. Returns the
    reason not to send, or None — also None when there is no websocket book to ask or the check is off."""
    if book_fn is None or RECHECK_DROP <= 0:
        return None
    try:
        cur = book_fn()
    except Exception:
        return None
    if cur is None:
        return None
    if cur <= ask - RECHECK_DROP + 1e-9:
        return f"ask fell {ask} -> {cur} before {stage}: side collapsing, not buying"
    if cur > limit + 1e-9:
        return f"ask rose {ask} -> {cur} above limit {limit} before {stage}: not sending"
    return None


def _ms(a, b):
    return "" if a is None or b is None else round((b - a) * 1000)


def _record_timing(row, tm, seen_at, book_ms, t_call):
    """One line per attempted order in live_timing.csv + a log line. Never raises."""
    try:
        end = tm.get("post") or tm.get("sign") or tm.get("tick") or tm.get("guard") or time.time()
        fill_avg = ""
        if row.get("status") == "filled":
            try: fill_avg = round(float(row["cost"]) / float(row["filled_size"]), 4)
            except (TypeError, ValueError, ZeroDivisionError): fill_avg = ""
        rec = dict(ts=row.get("ts"), variant=row.get("variant"), slug=row.get("slug"), outcome=row.get("outcome"), status=row.get("status"),
                   secs_left=row.get("secs_left"), ask=row.get("ask"), fill_avg=fill_avg,
                   book_ms="" if book_ms is None else round(book_ms), book_age_ms=_ms(seen_at, t_call), queue_ms=_ms(t_call, tm.get("start")),
                   guard_ms=_ms(tm.get("start"), tm.get("guard")), tick_ms=_ms(tm.get("guard"), tm.get("tick")),
                   prep_ms=_ms(tm.get("tick"), tm.get("presign")), sign_ms=_ms(tm.get("presign"), tm.get("sign")),
                   post_ms=_ms(tm.get("sign"), tm.get("post")), retried=tm.get("retried", 0),
                   total_ms=_ms(seen_at if seen_at is not None else (t_call if t_call is not None else tm.get("start")), end))
        with _lock:
            _append("live_timing.csv", TFIELDS, rec)
        _log(f"live timing {rec['variant']} {rec['slug']} {rec['status']}: total {rec['total_ms']}ms = book {rec['book_ms']} + age {rec['book_age_ms']}"
             f" + queue {rec['queue_ms']} + guard {rec['guard_ms']} + tick {rec['tick_ms']} + prep {rec['prep_ms']} + sign {rec['sign_ms']} + post {rec['post_ms']}"
             + (f" fill {fill_avg} vs ask {rec['ask']}" if fill_avg != "" else ""))
    except Exception as e:
        _log(f"live timing record failed {type(e).__name__}")


def _submit(variant, market, side_idx, ask, secs_left, mode=None, seen_at=None, book_ms=None, t_call=None, book_fn=None):
    tm = {"start": time.time(), "retried": 0}
    mode = mode or MODE
    slug = market["slug"]; outcome = market["outcomes"][side_idx]; token = market["tokens"][side_idx]
    price = min(PRICE_MAX, round(ask + SLIP, 3)); size = order_shares(price)
    row = dict(ts=datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"), mode=mode, variant=variant, slug=slug, outcome=outcome, token_id=token,
               ask=ask, price=price, size=size, tick=DEFAULT_TICK, neg_risk=market.get("negRisk", ""), order_type=ORDER_TYPE, status="", order_id="",
               filled_size=0, cost=0, secs_left=secs_left, msg="")
    with _lock:
        ok, why = guard(variant, slug, price, size, mode, outcome)
        tm["guard"] = time.time()
        if not ok:
            row["status"] = "skipped"; row["msg"] = why; _append("live_orders.csv", OFIELDS, row); _last[(variant, slug)] = ("skipped", why)
            _log(f"live skip {variant} {slug} {outcome}@{price}: {why}"); return row
        _orders[(variant, slug)] = row   # reserve the slot before any network call
    try:
        tick = DEFAULT_TICK
        if mode == "live":
            tick = _ticks.get(str(token))
            if tick is None:                                          # not prefetched (restart mid-market, prefetch failed)
                tick = str(_get(f"{CLOB}/tick-size?token_id={token}").get("minimum_tick_size", DEFAULT_TICK))
                _ticks[str(token)] = tick
            row["tick"] = tick; row["price"] = price = tick_round(price, tick)
        tm["tick"] = time.time()
        maker, taker, shares, usdc = amounts(price, size, tick)
        row["size"] = shares; row["cost"] = usdc
        moved = _book_moved(book_fn, ask, price, "sign")        # the side collapsed (or ran away) since the decision -> no order
        if moved:
            row["status"] = "aborted"; row["msg"] = moved; _log(f"live aborted {variant} {slug} {outcome}: {moved}")
            _notify(f"⚠️ [{ASSET.upper()}] 真單取消 {variant}\n{slug} 買 {outcome}：{moved}（剩 {secs_left}s）"); return row
        if mode != "live":
            row["status"] = "dry"; row["msg"] = f"not sent; would post {ORDER_TYPE} BUY {shares} sh for ${usdc:.2f}"
            _log(f"live DRY {variant} {slug} BUY {outcome} {shares}@{price} ${usdc:.2f} (ask {ask}, {secs_left}s)")
            return row
        ident = identity()
        neg_risk = bool(market.get("negRisk", False))
        order = dict(salt=new_salt(), maker=ident["proxyWallet"], signer=ident["address"], tokenId=str(token),
                     makerAmount=str(maker), takerAmount=str(taker), side=0, signatureType=1,
                     timestamp=str(int(time.time() * 1000)), metadata=ZERO32, builder=ZERO32, negRisk=neg_risk)
        # 1) the Worker re-validates (market, side, price, size, identity, daily cap) and signs; the key never leaves it
        tm["presign"] = time.time()
        st, signed = worker("btc5m_order", {"order": order, "idempotencyKey": f"{variant}:{slug}", "dry": False})
        tm["sign"] = time.time()
        if st != 200 or not signed.get("ok") or not signed.get("signature"):
            row["status"] = "rejected"; row["msg"] = f"signer {st} {str(signed.get('reason') or signed)[:140]}"; raise RuntimeError(row["msg"])
        moved = _book_moved(book_fn, ask, price, "post")        # signing took ~0.5 s: look once more before the order goes out
        if moved:
            row["status"] = "aborted"; row["msg"] = moved; _log(f"live aborted {variant} {slug} {outcome}: {moved}")
            _notify(f"⚠️ [{ASSET.upper()}] 真單取消 {variant}\n{slug} 買 {outcome}：{moved}（剩 {secs_left}s）"); return row
        wire = dict(salt=order["salt"], maker=order["maker"], signer=order["signer"], tokenId=order["tokenId"],
                    makerAmount=order["makerAmount"], takerAmount=order["takerAmount"], side="BUY", expiration="0",
                    signatureType=1, timestamp=order["timestamp"], metadata=ZERO32, builder=ZERO32, signature=signed["signature"])
        # 2) POST /order with L2 headers; the body string we sign is exactly the one we send
        body = json.dumps({"order": wire, "orderType": ORDER_TYPE, "owner": API_KEY})
        st, resp = clob_request("POST", "/order", body, l2_headers("POST", "/order", body))
        if st >= 500 or "internal server error" in str(resp.get("error", "")).lower():
            # exchange hiccup (13:04 10-22: two orders answered "internal server error" in the same second). The signed
            # order is still valid, so re-POST it once with fresh L2 headers on a fresh connection; no re-signing needed.
            time.sleep(0.3); row["msg"] = f"retry after {st} {str(resp)[:60]}; "; tm["retried"] = 1
            st, resp = clob_request("POST", "/order", body, l2_headers("POST", "/order", body), _retry=False)
        tm["post"] = time.time()
        row["order_id"] = str(resp.get("orderID") or ""); status = str(resp.get("status") or "").lower()
        def _num(k):
            try: return float(resp.get(k) or 0)
            except (TypeError, ValueError): return 0.0
        taking, making = _num("takingAmount"), _num("makingAmount")   # shares received / USDC actually spent (FAK may be partial)
        if resp.get("success") and taking > 0:
            row["status"] = "filled"; row["filled_size"] = taking; row["cost"] = making or usdc
            if taking + 1e-6 < shares: row["msg"] = f"partial {taking:g}/{shares:g}"
        elif resp.get("success"):
            row["status"] = "unfilled"; row["msg"] = status
        else:
            err = str(resp.get("errorMsg") or resp.get("error") or resp)
            row["status"] = "unfilled" if "fully filled" in err else "rejected"; row["msg"] = err[:160]
        row["msg"] = (row["msg"] + " " + str({k: resp.get(k) for k in ("status", "makingAmount", "takingAmount", "errorMsg")})).strip()
        _log(f"live {row['status']} {variant} {slug} BUY {outcome} {shares}@{price} id={row['order_id'][:12]} {row['msg'][:90]}")
        _notify(f"{'✅' if row['status']=='filled' else '⚠️'} [{ASSET.upper()}] 真單 {row['status']} {variant}\n{slug} 買 {outcome} {shares:g} 股 @{price}（${usdc:.2f}，剩 {secs_left}s）")
        if row["status"] in ("unfilled", "rejected"):      # what did the exchange's book look like right after the miss? (diagnostics only)
            threading.Thread(target=_snapshot_miss, args=(dict(row), token, price, ask, tm.get("post") or time.time()), daemon=True).start()
        return row
    except Exception as e:
        row["status"] = row["status"] or "error"; row["msg"] = (row["msg"] + " " + type(e).__name__ + " " + str(e)[:120]).strip()
        _log(f"live ERROR {variant} {slug}: {row['msg'][:160]}"); _notify(f"❌ 真單失敗 {variant} {slug}: {row['msg'][:120]}")
        return row
    finally:
        with _lock:   # the (variant, slug) slot stays reserved whatever happened: one attempt per rule per market
            _append("live_orders.csv", OFIELDS, row)
            _last[(variant, slug)] = (row["status"], str(row["msg"])[:80])
            if len(_last) > 2000:
                for k in list(_last)[:1000]: _last.pop(k, None)
        _record_timing(row, tm, seen_at, book_ms, t_call)


MFIELDS = ["ts", "variant", "slug", "outcome", "status", "secs_left", "ask_seen", "limit", "best_ask_after", "shares_le_limit",
           "asks_after", "snapshot_ms", "msg"]


def _snapshot_miss(row, token, limit, ask_seen, t_answer):
    """2026-10-27 (fish: 強化技術細節): after a FAK that did not fill, fetch the exchange's REST book for that token at once
    (direct, ~0.2 s) and record the best ask and the shares that were available at or below our limit. Tells, per miss,
    whether the price had simply run above ask + SLIP during the ~1 s in flight (best_ask_after > limit) or whether the
    book we decided on was wrong (shares at <= limit still there). live_miss.csv, never raises, never affects trading."""
    try:
        t0 = time.time(); b = _get(f"{CLOB}/book?token_id={token}", timeout=5)
        asks = sorted(((float(x["price"]), float(x["size"])) for x in b.get("asks", []) if float(x["size"]) > 0))
        best = asks[0][0] if asks else ""
        le = round(sum(s for p, s in asks if p <= float(limit) + 1e-9), 2)
        rec = dict(ts=row.get("ts"), variant=row.get("variant"), slug=row.get("slug"), outcome=row.get("outcome"), status=row.get("status"),
                   secs_left=row.get("secs_left"), ask_seen=ask_seen, limit=limit, best_ask_after=best, shares_le_limit=le,
                   asks_after=" ".join(f"{p:g}x{s:g}" for p, s in asks[:4]), snapshot_ms=round((t0 - t_answer) * 1000), msg=str(row.get("msg", ""))[:60])
        with _lock:
            _append("live_miss.csv", MFIELDS, rec)
        _log(f"live miss-check {row.get('variant')} {row.get('slug')} {row.get('outcome')}: saw {ask_seen}, limit {limit}, book now best {best} ({le} sh <= limit) {rec['snapshot_ms']}ms after answer")
    except Exception as e:
        _log(f"live miss-check failed {type(e).__name__}")


def settle(slug, winner):
    """Called by sim once a market resolved. Books P&L for every filled (or dry) order on it and applies the stops."""
    day = today(); trips = []
    with _lock:
        for (variant, s), row in list(_orders.items()):
            if s != slug or row.get("status") not in ("filled", "dry"): continue
            size = float(row["filled_size"] or row["size"]); cost = float(row["cost"] or 0)
            # Exchange taker fee, verified on chain 2026-10-26 (649 fills): 7% x p x (1-p) per share, charged in USDC on top of
            # `cost` (makingAmount). Recorded in the fee column for reporting; pnl stays gross so the daily stops behave exactly
            # as before (fish: risk parameters frozen until 11-02). Net = pnl - fee.
            p_fill = cost / size if size else 0.0; fee = round(FEE_RATE * p_fill * (1 - p_fill) * size, 4) if row["status"] == "filled" else 0.0
            won = int(row["outcome"] == winner); pnl = round((size if won else 0) - cost, 4)
            if row["status"] == "filled":                      # dry orders are not booked into the stops
                _daily.setdefault(day, {}); _daily[day][variant] = _daily[day].get(variant, 0.0) + pnl
            _append("live_results.csv", RFIELDS, dict(ts=datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"), variant=variant, slug=slug, outcome=row["outcome"],
                                                     size=size, cost=cost, fee=fee, winner=winner, won=won, pnl=pnl, day=day))
            _log(f"live settle {row['status']} {variant} {slug} {row['outcome']} -> {winner} pnl {pnl:+.4f}")
            if row["status"] == "filled":
                if not won: _notify(f"❌ [{ASSET.upper()}] 真單輸 {variant} {slug}\n買 {row['outcome']} @{row['price']}，結果 {winner}，{pnl:+.2f}")
                if realised(variant) <= -MAX_DAILY_LOSS_VARIANT and variant not in _stopped:
                    _stopped.add(variant); trips.append(f"⛔ {variant} 今日虧 {realised(variant):+.2f}，已停到明天")
                if realised() <= -MAX_DAILY_LOSS_TOTAL:
                    trips.append(f"⛔⛔ 全部規則今日合計虧 {realised():+.2f}，全停到明天")
            del _orders[(variant, s)]
    for t in trips: _log(t); _notify(t)


def summary(day=None):
    d = _daily.get(day or today(), {})
    bal = _balance["usdc"]; btxt = f"｜錢包 USDC ${bal:.2f}" if bal is not None else ""
    if not d: return "真單：今日無已結算" + btxt
    return "真單今日：" + "｜".join(f"{k} {v:+.2f}" for k, v in sorted(d.items())) + f"｜合計 {sum(d.values()):+.2f}" + btxt


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "whoami":
        print(json.dumps(identity()))
    elif cmd == "balance":
        print(json.dumps({"usdc": wallet_balance(force=True), "floor": MIN_BALANCE_USDC}))
    elif cmd == "region":           # is the exchange going to accept orders from where our authenticated calls exit?
        g = _get("https://polymarket.com/api/geoblock", timeout=12, proxied=True)
        print(json.dumps({"proxy": ORDER_PROXY or "(direct)", **g}))
    elif cmd == "setup":            # derive L2 credentials through the Worker and write .live.env (0600); prints nothing secret
        key, sec, pw, addr = derive_l2(); write_live_env(key, sec, pw, addr)
        print(f"wrote {LIVE_ENV} (0600): signer address {addr}, api key {key[:8]}…")
    elif cmd == "dry-sign":         # ask the Worker to validate (not sign) a sample order for the current market
        start = int(time.time() // 300) * 300
        evs = _get(GAMMA.format(start)) or _get(GAMMA.format(start - 300))   # gamma publishes a market a little after it opens
        if not evs: raise SystemExit("gamma has no current/previous market; try again in a minute")
        ev = evs[0]; m = ev["markets"][0]
        tokens = json.loads(m["clobTokenIds"]); ident = identity()
        maker, taker, shares, usdc = amounts(0.95, LIVE_SHARES)
        order = dict(salt=new_salt(), maker=ident["proxyWallet"], signer=ident["address"], tokenId=str(tokens[0]), makerAmount=str(maker),
                     takerAmount=str(taker), side=0, signatureType=1, timestamp=str(int(time.time() * 1000)), metadata=ZERO32, builder=ZERO32,
                     negRisk=bool(m.get("negRisk", False)))
        print(json.dumps(worker("btc5m_order", {"order": order, "idempotencyKey": f"dry:{start}:{int(time.time())}", "dry": True})[1]))
    else:
        print("usage: python3 -m btc5m_sim.live whoami|setup|dry-sign")
