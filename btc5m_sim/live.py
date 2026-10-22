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
SLIP = float(os.getenv("LIVE_SLIP", "0.01"))                 # pay up to ask + SLIP so a FOK still fills if the book moved
PRICE_MIN, PRICE_MAX = 0.5, 0.99
MAX_DAILY_LOSS_VARIANT = float(os.getenv("MAX_DAILY_LOSS_VARIANT", "15"))
MAX_DAILY_LOSS_TOTAL = float(os.getenv("MAX_DAILY_LOSS_TOTAL", "50"))
KILL_FILE = os.getenv("KILL_FILE", os.path.join(DATA, "STOP"))
# Capital floor (fish 2026-10-22: no daily volume cap; only stop when the wallet's USDC drops below this).
MIN_BALANCE_USDC = float(os.getenv("MIN_BALANCE_USDC", "50"))
BALANCE_TTL = float(os.getenv("BALANCE_TTL", "60"))         # seconds between balance refreshes
_balance = {"usdc": None, "at": 0.0, "alerted": None}
SLUG_PREFIX = "btc-updown-5m-"
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
GAMMA = "https://gamma-api.polymarket.com/events?slug=btc-updown-5m-{}"
ZERO32 = "0x" + "0" * 64
UA = {"User-Agent": "btc5m-live"}
LIVE_ENV = os.getenv("LIVE_ENV_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), ".live.env"))
_ident = {}      # {"address": EOA, "proxyWallet": maker} from btc5m_whoami, fetched once

OFIELDS = ["ts", "mode", "variant", "slug", "outcome", "token_id", "ask", "price", "size", "tick", "neg_risk",
           "order_type", "status", "order_id", "filled_size", "cost", "secs_left", "msg"]
RFIELDS = ["ts", "variant", "slug", "outcome", "size", "cost", "fee", "winner", "won", "pnl", "day"]

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
    _log(f"live: mode={MODE} shares={LIVE_SHARES:g} slip={SLIP} proxy={ORDER_PROXY or 'direct'} type={ORDER_TYPE} stops: rule/day -{MAX_DAILY_LOSS_VARIANT:g} total/day -{MAX_DAILY_LOSS_TOTAL:g} floor ${MIN_BALANCE_USDC:g} kill={KILL_FILE}"
         + (f" rules={sorted(LIVE_VARIANTS)}" if LIVE_VARIANTS else " rules=all"))
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


def wallet_balance(force=False):
    """Available USDC in the proxy wallet via the CLOB (authenticated, proxied). Cached BALANCE_TTL seconds.
    Returns None when it cannot be read (then the guard lets orders through: a read failure must not freeze trading)."""
    now = time.time()
    if not force and _balance["usdc"] is not None and now - _balance["at"] < BALANCE_TTL:
        return _balance["usdc"]
    if not (API_KEY and API_SECRET and API_PASSPHRASE and SIGNER_ADDRESS): return None
    try:
        h = l2_headers("GET", "/balance-allowance", "")
        d = _get(f"{CLOB}/balance-allowance?asset_type=COLLATERAL&signature_type=1", timeout=10, headers=h, proxied=True)
        usdc = int(d.get("balance", 0)) / 1e6
        _balance.update(usdc=usdc, at=now)
        return usdc
    except Exception as e:
        _log(f"live: balance read failed {type(e).__name__}"); return _balance["usdc"]


def guard(variant, slug, price, size, mode=None):
    """Return (ok, reason). Every reason is recorded in the ledger so skipped orders are visible."""
    mode = mode or MODE
    if mode not in ("dry", "live"): return False, "mode off"
    if os.path.exists(KILL_FILE): return False, "kill file"
    if LIVE_VARIANTS and variant not in LIVE_VARIANTS: return False, "rule not enabled"
    if not slug.startswith(SLUG_PREFIX): return False, "not a btc 5m market"
    if not (PRICE_MIN <= price <= PRICE_MAX): return False, f"price {price} out of range"
    if size <= 0 or size > MAX_SHARES: return False, f"size {size} out of range"
    if (variant, slug) in _orders: return False, "already ordered this market"
    if realised() <= -MAX_DAILY_LOSS_TOTAL: return False, f"total daily loss {realised():+.2f} hit stop"
    if mode == "live":
        bal = wallet_balance()
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


def worker(op, payload=None, timeout=8):
    """POST one op to the signer Worker. Never logs the token."""
    body = json.dumps({"op": op, **(payload or {})})
    req = urllib.request.Request(SIGNER_URL, data=body.encode(), method="POST",
                                 headers={**UA, "content-type": "application/json", "authorization": f"Bearer {SIGNER_TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try: return e.code, json.load(e)
        except Exception: return e.code, {"ok": False, "reason": f"http {e.code}"}


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


def submit(variant, market, side_idx, ask, secs_left, mode=None):
    """Non-blocking: builds + (live) sends one BUY for `variant` on `market` (dict from sim.market())."""
    threading.Thread(target=_submit, args=(variant, market, side_idx, ask, secs_left, mode), daemon=True).start()


def _submit(variant, market, side_idx, ask, secs_left, mode=None):
    mode = mode or MODE
    slug = market["slug"]; outcome = market["outcomes"][side_idx]; token = market["tokens"][side_idx]
    price = min(PRICE_MAX, round(ask + SLIP, 3)); size = LIVE_SHARES
    row = dict(ts=datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"), mode=mode, variant=variant, slug=slug, outcome=outcome, token_id=token,
               ask=ask, price=price, size=size, tick=DEFAULT_TICK, neg_risk=market.get("negRisk", ""), order_type=ORDER_TYPE, status="", order_id="",
               filled_size=0, cost=0, secs_left=secs_left, msg="")
    with _lock:
        ok, why = guard(variant, slug, price, size, mode)
        if not ok:
            row["status"] = "skipped"; row["msg"] = why; _append("live_orders.csv", OFIELDS, row); _last[(variant, slug)] = ("skipped", why)
            _log(f"live skip {variant} {slug} {outcome}@{price}: {why}"); return row
        _orders[(variant, slug)] = row   # reserve the slot before any network call
    try:
        tick = DEFAULT_TICK
        if mode == "live":
            tick = str(_get(f"{CLOB}/tick-size?token_id={token}").get("minimum_tick_size", DEFAULT_TICK))
            row["tick"] = tick; row["price"] = price = tick_round(price, tick)
        maker, taker, shares, usdc = amounts(price, size, tick)
        row["size"] = shares; row["cost"] = usdc
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
        st, signed = worker("btc5m_order", {"order": order, "idempotencyKey": f"{variant}:{slug}", "dry": False})
        if st != 200 or not signed.get("ok") or not signed.get("signature"):
            row["status"] = "rejected"; row["msg"] = f"signer {st} {str(signed.get('reason') or signed)[:140]}"; raise RuntimeError(row["msg"])
        wire = dict(salt=order["salt"], maker=order["maker"], signer=order["signer"], tokenId=order["tokenId"],
                    makerAmount=order["makerAmount"], takerAmount=order["takerAmount"], side="BUY", expiration="0",
                    signatureType=1, timestamp=order["timestamp"], metadata=ZERO32, builder=ZERO32, signature=signed["signature"])
        # 2) POST /order with L2 headers; the body string we sign is exactly the one we send
        body = json.dumps({"order": wire, "orderType": ORDER_TYPE, "owner": API_KEY})
        st, resp = clob_request("POST", "/order", body, l2_headers("POST", "/order", body))
        if st >= 500 or "internal server error" in str(resp.get("error", "")).lower():
            # exchange hiccup (13:04 10-22: two orders answered "internal server error" in the same second). The signed
            # order is still valid, so re-POST it once with fresh L2 headers on a fresh connection; no re-signing needed.
            time.sleep(0.3); row["msg"] = f"retry after {st} {str(resp)[:60]}; "
            st, resp = clob_request("POST", "/order", body, l2_headers("POST", "/order", body), _retry=False)
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
        _notify(f"{'✅' if row['status']=='filled' else '⚠️'} 真單 {row['status']} {variant}\n{slug} 買 {outcome} {shares:g} 股 @{price}（${usdc:.2f}，剩 {secs_left}s）")
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


def settle(slug, winner):
    """Called by sim once a market resolved. Books P&L for every filled (or dry) order on it and applies the stops."""
    day = today(); trips = []
    with _lock:
        for (variant, s), row in list(_orders.items()):
            if s != slug or row.get("status") not in ("filled", "dry"): continue
            size = float(row["filled_size"] or row["size"]); cost = float(row["cost"] or 0); fee = 0.0   # V2 taker fee is already inside the fill
            won = int(row["outcome"] == winner); pnl = round((size if won else 0) - cost, 4)
            if row["status"] == "filled":                      # dry orders are not booked into the stops
                _daily.setdefault(day, {}); _daily[day][variant] = _daily[day].get(variant, 0.0) + pnl
            _append("live_results.csv", RFIELDS, dict(ts=datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"), variant=variant, slug=slug, outcome=row["outcome"],
                                                     size=size, cost=cost, fee=fee, winner=winner, won=won, pnl=pnl, day=day))
            _log(f"live settle {row['status']} {variant} {slug} {row['outcome']} -> {winner} pnl {pnl:+.4f}")
            if row["status"] == "filled":
                if not won: _notify(f"❌ 真單輸 {variant} {slug}\n買 {row['outcome']} @{row['price']}，結果 {winner}，{pnl:+.2f}")
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
