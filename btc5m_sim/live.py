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
import base64, csv, hashlib, hmac, json, os, threading, time, urllib.error, urllib.request
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
SLUG_PREFIX = "btc-updown-5m-"
CLOB = os.getenv("CLOB_HOST", "https://clob.polymarket.com")
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
    _log(f"live: mode={MODE} shares={LIVE_SHARES:g} slip={SLIP} stops: rule/day -{MAX_DAILY_LOSS_VARIANT:g} total/day -{MAX_DAILY_LOSS_TOTAL:g} kill={KILL_FILE}"
         + (f" rules={sorted(LIVE_VARIANTS)}" if LIVE_VARIANTS else " rules=all"))
    if MODE == "live" and not (SIGNER_URL and SIGNER_TOKEN and SIGNER_ADDRESS and API_KEY and API_SECRET and API_PASSPHRASE):
        _log("live: MISSING credentials -> falling back to dry")
        return "dry"
    return MODE


def today():
    return datetime.now(TZ).strftime("%Y-%m-%d")


def realised(variant=None, day=None):
    d = _daily.get(day or today(), {})
    return sum(d.values()) if variant is None else d.get(variant, 0.0)


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
    req = urllib.request.Request(f"{CLOB}/auth/derive-api-key", headers={**UA, **h})
    with urllib.request.urlopen(req, timeout=10) as r:
        d = json.load(r)
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


def _get(url, timeout=6):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return json.load(r)


def _post(url, body_str, headers, timeout=10):
    req = urllib.request.Request(url, data=body_str.encode(), method="POST", headers={**UA, "content-type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
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
               ask=ask, price=price, size=size, tick=DEFAULT_TICK, neg_risk=market.get("negRisk", ""), order_type="FOK", status="", order_id="",
               filled_size=0, cost=0, secs_left=secs_left, msg="")
    with _lock:
        ok, why = guard(variant, slug, price, size, mode)
        if not ok:
            row["status"] = "skipped"; row["msg"] = why; _append("live_orders.csv", OFIELDS, row); _log(f"live skip {variant} {slug} {outcome}@{price}: {why}"); return row
        _orders[(variant, slug)] = row   # reserve the slot before any network call
    try:
        tick = DEFAULT_TICK
        if mode == "live":
            tick = str(_get(f"{CLOB}/tick-size?token_id={token}").get("minimum_tick_size", DEFAULT_TICK))
            row["tick"] = tick; row["price"] = price = tick_round(price, tick)
        maker, taker, shares, usdc = amounts(price, size, tick)
        row["size"] = shares; row["cost"] = usdc
        if mode != "live":
            row["status"] = "dry"; row["msg"] = f"not sent; would post FOK BUY {shares} sh for ${usdc:.2f}"
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
        body = json.dumps({"order": wire, "orderType": "FOK", "owner": API_KEY})
        st, resp = _post(f"{CLOB}/order", body, l2_headers("POST", "/order", body))
        row["order_id"] = str(resp.get("orderID") or ""); status = str(resp.get("status") or "").lower()
        try: taking = float(resp.get("takingAmount") or 0)
        except (TypeError, ValueError): taking = 0.0
        if resp.get("success") and status == "matched":
            row["status"] = "filled"; row["filled_size"] = taking or shares; row["cost"] = usdc
        elif resp.get("success"):
            row["status"] = "unfilled"; row["msg"] = status
        else:
            row["status"] = "rejected"; row["msg"] = str(resp.get("errorMsg") or resp)[:160]
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
    if not d: return "真單：今日無已結算"
    return "真單今日：" + "｜".join(f"{k} {v:+.2f}" for k, v in sorted(d.items())) + f"｜合計 {sum(d.values()):+.2f}"


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "whoami":
        print(json.dumps(identity()))
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
