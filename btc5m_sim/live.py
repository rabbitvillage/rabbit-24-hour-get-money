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
import base64, csv, hashlib, hmac, json, os, threading, time, urllib.request
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
UA = {"User-Agent": "btc5m-live"}

OFIELDS = ["ts", "mode", "variant", "slug", "outcome", "token_id", "ask", "price", "size", "tick", "fee_bps",
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


def amounts(price, size):
    """USDC maker amount and token taker amount in 6-decimal integers (BUY)."""
    taker = int(round(size * 1e6))                     # tokens we receive
    maker = int(round(size * price * 1e6))             # USDC we pay
    return maker, taker


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
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.load(r)


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
               ask=ask, price=price, size=size, tick=DEFAULT_TICK, fee_bps="", order_type="FOK", status="", order_id="", filled_size=0, cost=0,
               secs_left=secs_left, msg="")
    with _lock:
        ok, why = guard(variant, slug, price, size, mode)
        if not ok:
            row["status"] = "skipped"; row["msg"] = why; _append("live_orders.csv", OFIELDS, row); _log(f"live skip {variant} {slug} {outcome}@{price}: {why}"); return row
        _orders[(variant, slug)] = row   # reserve the slot before any network call
    try:
        if mode == "live":
            tick = str(_get(f"{CLOB}/tick-size?token_id={token}").get("minimum_tick_size", DEFAULT_TICK))
            fee = int((_get(f"{CLOB}/fee-rate?token_id={token}") or {}).get("base_fee") or 0)
            row["tick"] = tick; row["fee_bps"] = fee; row["price"] = price = tick_round(price, tick)
        maker, taker = amounts(price, size)
        intent = dict(slug=slug, token_id=token, outcome=outcome, side="BUY", price=price, size=size, maker_amount=str(maker), taker_amount=str(taker),
                      fee_rate_bps=str(row["fee_bps"] or 0), tick=row["tick"], variant=variant, secs_left=secs_left, dry=(mode != "live"))
        if mode != "live":
            row["status"] = "dry"; row["cost"] = round(price * size, 4); row["msg"] = "not sent"
            _log(f"live DRY {variant} {slug} BUY {outcome} {size:g}@{price} (ask {ask}, {secs_left}s)")
            return row
        # 1) signer Worker builds + signs the order (it re-validates the whitelist and knows maker/signer)
        st, signed = _post(SIGNER_URL, json.dumps(intent), {"authorization": f"Bearer {SIGNER_TOKEN}"})
        if st != 200 or not signed.get("order"):
            row["status"] = "rejected"; row["msg"] = f"signer {st} {str(signed)[:120]}"; raise RuntimeError(row["msg"])
        # 2) POST /order with L2 headers; the body string is what we sign and exactly what we send
        body = json.dumps({"order": signed["order"], "owner": API_KEY, "orderType": "FOK", "postOnly": False})
        st, resp = _post(f"{CLOB}/order", body, l2_headers("POST", "/order", body))
        row["order_id"] = str(resp.get("orderID") or resp.get("orderId") or ""); status = str(resp.get("status") or "")
        if resp.get("success") and status.lower() in ("matched", "filled"):
            row["status"] = "filled"; row["filled_size"] = size; row["cost"] = round(price * size, 4)
        elif resp.get("success"):
            row["status"] = "unfilled"; row["msg"] = status
        else:
            row["status"] = "rejected"; row["msg"] = str(resp.get("errorMsg") or resp)[:160]
        row["msg"] = (row["msg"] + " " + str(resp)[:200]).strip()
        _log(f"live {row['status']} {variant} {slug} BUY {outcome} {size:g}@{price} id={row['order_id']} {row['msg'][:80]}")
        _notify(f"{'✅' if row['status']=='filled' else '⚠️'} 真單 {row['status']} {variant}\n{slug} 買 {outcome} {size:g} 股 @{price}（剩 {secs_left}s）")
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
            size = float(row["filled_size"] or row["size"]); price = float(row["price"]); cost = round(price * size, 4)
            fee = round((float(row["fee_bps"] or 0) / 10000) * price * (1 - price) * size, 5)
            won = int(row["outcome"] == winner); pnl = round((size if won else 0) - cost - fee, 4)
            if row["status"] == "filled":                      # dry orders are not booked into the stops
                _daily.setdefault(day, {}); _daily[day][variant] = _daily[day].get(variant, 0.0) + pnl
            _append("live_results.csv", RFIELDS, dict(ts=datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"), variant=variant, slug=slug, outcome=row["outcome"],
                                                     size=size, cost=cost, fee=fee, winner=winner, won=won, pnl=pnl, day=day))
            _log(f"live settle {row['status']} {variant} {slug} {row['outcome']} -> {winner} pnl {pnl:+.4f}")
            if row["status"] == "filled":
                if not won: _notify(f"❌ 真單輸 {variant} {slug}\n買 {row['outcome']} @{price}，結果 {winner}，{pnl:+.2f}")
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
