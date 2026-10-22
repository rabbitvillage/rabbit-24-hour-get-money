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
import csv, json, os, sys, threading, time, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta

from . import live   # phase-1 executor; EXEC_MODE=off keeps it inert
from . import feed   # live commentary for the dashboard

UA = {"User-Agent": "Mozilla/5.0 btc5m-sim"}
GAMMA = "https://gamma-api.polymarket.com/events?slug=btc-updown-5m-{}"
BOOK = "https://clob.polymarket.com/book?token_id={}"
SPOT_URLS = [("coinbase", "https://api.exchange.coinbase.com/products/BTC-USD/ticker"),   # real-time best bid/ask
             ("kraken", "https://api.kraken.com/0/public/Ticker?pair=XBTUSD")]

ENTRY_WINDOW = int(os.getenv("ENTRY_WINDOW", "45"))   # seconds before close to start looking
LAST_ENTRY = int(os.getenv("LAST_ENTRY", "5"))        # stop looking this many seconds before close
MIN_PRICE = float(os.getenv("MIN_PRICE", "0.98"))
MAX_PRICE = float(os.getenv("MAX_PRICE", "0.99"))
SHARES = float(os.getenv("SHARES", "5"))
POLL = float(os.getenv("POLL", "1.0"))
DATA = os.getenv("DATA_DIR", "data")
TG_TOKEN = os.getenv("TG_TOKEN", "")
TG_CHAT = os.getenv("TG_CHAT_ID", "")
TZ = timezone(timedelta(hours=int(os.getenv("TZ_OFFSET", "8"))))
FEE_RATE = 0.07
# Chainlink BTC/USD aggregator on Polygon (public, no key). Polymarket resolves on the Chainlink BTC/USD 60s-TWAP *Data Stream*
# (credentialed, off-chain); the on-chain aggregator is the closest free reference, updated on deviation/heartbeat, not every second.
CHAIN_RPCS = [u for u in os.getenv("CHAIN_RPCS", "https://polygon-bor-rpc.publicnode.com,https://polygon.drpc.org").split(",") if u.strip()]
CHAIN_FEED = os.getenv("CHAIN_FEED", "0xc907E116054Ad103354f2D350FD2514433D57F6f")
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


VARIANTS = [parse_variant(v) for v in os.getenv("VARIANTS", "w45_p98,w30_p98,w20_p98,w45_p99,w30_p99,w45_p98_s,w45_p98_s10,w45_p98_s20,w45_p98_s30,w60_m95_s10,w60_m95_s20").split(",") if v.strip()]


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
        data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text}).encode()
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data, headers=UA), timeout=10)
    except Exception as e:  # never let TG kill the sim; never print the token
        log("TG send failed:", type(e).__name__)


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
    """Current BTC/USD from the first source that answers. Returns price or None."""
    for name, url in SPOT_URLS:
        try:
            d, _ = get(url, timeout=3)
            if name == "coinbase":
                return (float(d["bid"]) + float(d["ask"])) / 2
            return float(d["result"]["XXBTZUSD"]["c"][0])
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
        time.sleep(max(0, 1.0 - (time.time() - t0)))


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


def rule_state(v, asks, lead, secs_left, dl_fn, outcomes):
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
        if not side_ok: return f"賣價 {hit[1]:.3f} 可買 {side}，但幣價在對面（{dl:+.1f}），不買"
        if abs(dl) < v["_filt"]: return f"賣價 {hit[1]:.3f} 可買 {side}，但離線只有 {dl:+.1f}（門檻 {v['_filt']:g}），不買"
    return f"條件成立，準備買 {outcomes[hit[0]]} @{hit[1]:.3f}"


def decision_row(v, row):
    """One line per rule per market: bought (with price/time) or skipped with the reason and the numbers behind it."""
    d = dict(slug=v["slug"], start_utc=v["start_utc"], variant=v["variant"], lead_min=v.get("_lead_min"), lead_max=v.get("_lead_max"),
             winner=row.get("winner"), won=v.get("won"), pnl=v.get("pnl"), live_status="", live_msg="", dl="", threshold=v.get("_filt") if v.get("_filt") is not None else "")
    if v.get("entered"):
        d.update(decision="buy", reason="entered", ask=v["ask"], secs_left=v["secs_left"])
    elif v.get("_block"):
        b = v["_block"]; d.update(decision="skip", reason=b["reason"], ask=b["ask"], secs_left=b["secs"], dl=b["dl"], threshold=b["thr"])
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
    end = start + 300
    base = dict(slug=f"btc-updown-5m-{start}", start_utc=datetime.fromtimestamp(start, timezone.utc).isoformat())
    row = dict(base, entered=0, note="")
    vstate = {n: dict(base, variant=n, window=w, min_price=p, entered=0, note="", _filt=fl, _max=hi) for n, w, p, hi, fl in VARIANTS}
    samples = []
    threading.Thread(target=sample_spot, args=(end, samples), daemon=True).start()
    ptb = None; series = []; csamples = []
    threading.Thread(target=sample_chain, args=(end, csamples), daemon=True).start()
    spot_at_start = spot()
    chain_at_start, chain_up_at_start, _ = chainlink()   # the line, as the oracle saw it at open (closest free proxy for priceToBeat)  # proxy for priceToBeat (Chainlink at open), which gamma publishes only after close
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
        seen_asks = []; series = []
        pending = dict(vstate)
        while time.time() < end - LAST_ENTRY:
            t_loop = time.time()
            try:
                books, ms = [], 0
                for tok in m["tokens"]:
                    b, dt = get(BOOK.format(tok), timeout=5); books.append(b); ms += dt
                asks = [best_ask(b, MAX_PRICE) for b in books]
                now = time.time(); secs_left = round(end - now, 1); sp = latest(samples)
                lead = max((a[0] for a in asks if a[0] is not None), default=None)
                if lead is not None and secs_left <= ENTRY_WINDOW:  # note range stays about the entry window
                    seen_asks.append(lead)
                series.append(dict(slug=row["slug"], secs_left=secs_left, up_ask=asks[0][0], up_sh=round(asks[0][1], 2), up_usd=round(asks[0][2], 2),
                                   down_ask=asks[1][0], down_sh=round(asks[1][1], 2), down_usd=round(asks[1][2], 2), spot=sp, fetch_ms=round(ms)))
                if not row["entered"] and secs_left <= ENTRY_WINDOW:
                    hit = evaluate(asks, MIN_PRICE, MAX_PRICE, SHARES)
                    if hit:
                        fill(row, hit, m["outcomes"], asks, secs_left, ms)
                        live.submit("main", m, hit[0], hit[1], secs_left)
                for name in list(pending):
                    v = pending[name]
                    if secs_left > v["window"]:
                        continue
                    if v.get("spot_at_window") is None:  # first poll inside this variant's window
                        v["spot_at_window"] = sp
                    if lead is not None:                 # favourite's best ask seen inside this rule's window (for the "why not" log)
                        v["_lead_min"] = min(v.get("_lead_min", lead), lead); v["_lead_max"] = max(v.get("_lead_max", lead), lead)
                    hit = evaluate(asks, v["min_price"], v["_max"], SHARES)
                    if hit is None and lead is not None and v["min_price"] <= lead <= v["_max"]:
                        v["_depth_short"] = True         # price was in band but fewer than SHARES on the book
                    if hit and v["_filt"] is not None:
                        ref = chain_at_start if chain_at_start is not None else spot_at_start
                        dl = None if sp is None or ref is None else sp - ref
                        if not dist_ok(m["outcomes"][hit[0]], dl, v["_filt"]):
                            side_ok = dl is not None and ((dl > 0) if m["outcomes"][hit[0]] == "Up" else (dl < 0))
                            v["_block"] = dict(reason="distance" if side_ok else ("no_spot" if dl is None else "direction"),
                                               ask=hit[1], secs=secs_left, dl=None if dl is None else round(dl, 2), thr=v["_filt"])
                            hit = None
                    if hit:
                        fill(v, hit, m["outcomes"], asks, secs_left, ms)
                        live.submit(name, m, hit[0], hit[1], secs_left)
                        v["spot_at_entry"] = sp
                        if csamples:
                            v["chain_at_entry"] = csamples[-1][1]; v["chain_age_at_entry"] = round(now - csamples[-1][2])
                        del pending[name]
                ref = chain_at_start if chain_at_start is not None else spot_at_start
                dl_now = None if sp is None or ref is None else round(sp - ref, 2)
                live_set = live.LIVE_VARIANTS or {n for n, *_ in VARIANTS}
                feed.update(row["slug"], secs_left, asks, m["outcomes"], dl_now, "鏈上開盤價" if chain_at_start is not None else "Coinbase 開盤價",
                            [(n, rule_state(v, asks, lead, secs_left, lambda: dl_now, m["outcomes"]), n in live_set) for n, v in vstate.items()])
            except Exception as e:
                row["note"] = f"book error {type(e).__name__}"
            time.sleep(max(0, POLL - (time.time() - t_loop)))
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
                           "direction": f"幣價在對面（{d['dl']}）", "distance": f"離線只有 {d['dl']}，門檻 {d['threshold']}", "no_spot": "沒有現價"}.get(d["reason"], d["reason"])
                    out.append(f"{n}：沒買，{why}")
            return out
        feed.finish(row["slug"], summary_lines())
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
        for v in vstate.values():
            v["_t"] = row["_t"]; finalize_dists(v, samples, end, ptb, spot_at_start); finalize_chain(v, csamples, ptb, chain_at_start)
        decisions = [decision_row(v, row) for v in vstate.values()]
        with lock:
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
                tg(f"❌ 模擬輸一筆 {row['slug']}\n買 {row['side']} @{row['ask']}，剩 {row['secs_left']}s，結果 {row.get('winner')}，損益 {row.get('pnl')}")
        else:
            log(f"{row['slug']} no entry {row.get('note','')} winner {row.get('winner')}")
        if vstate:
            log(f"{row['slug']} variants " + " ".join(
                f"{n}={'@'+str(v['ask'])+'/'+str(v['secs_left'])+'s' if v['entered'] else '-'}" for n, v in vstate.items())
                + f" | min|spot-ptb| {vstate[next(iter(vstate))]['min_abs_dist']} ({len(samples)} samples) | chain open-ptb {vstate[next(iter(vstate))]['chain_ptb_err']} ({len(csamples)} updates)")


# ---------------- summaries ----------------
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
    log(f"start: entry {MIN_PRICE}-{MAX_PRICE} in last {ENTRY_WINDOW}s, {SHARES:g} shares, poll {POLL}s; "
        f"variants {[n for n, _, _, _, _ in VARIANTS]}; book window {BOOK_WINDOW}s; spot window {SPOT_WINDOW}s")
    tg((f"🔴 BTC 5m bot 啟動：真單模式（會真的下單）" if exec_mode == "live" else f"🟢 BTC 5m 模擬 bot 啟動（{'dry：算好單但不送' if exec_mode == 'dry' else '不下單'}）")
       + f"\n規則：最後 {ENTRY_WINDOW} 秒，領先方賣價 {MIN_PRICE}~{MAX_PRICE} 就買 {SHARES:g} 股\n"
       f"影子變體：{', '.join(n for n, _, _, _, _ in VARIANTS)}\n下單模式：{exec_mode}" + ("（真單！）" if exec_mode == "live" else "（不送單）"))
    last_hour = int(time.time() // 3600); last_day = datetime.now(TZ).date(); day_start = time.time()
    while True:
        now = time.time()
        start = int(now // 300) * 300
        if start not in seen and now < start + 300 - ENTRY_WINDOW - 5:
            seen.add(start)
            threading.Thread(target=watch, args=(start, rows, vrows, lock), daemon=True).start()
        h = int(now // 3600)
        if h != last_hour:
            with lock:
                msg = summary(rows, now - 3600); vmsg = vsummary(vrows, now - 3600)
            tg("⏱ 過去一小時\n" + msg + ("\n影子變體\n" + vmsg if VARIANTS else "") + ("\n" + live.summary() if exec_mode != "off" else "")); log("hourly:", msg); last_hour = h
        d = datetime.now(TZ).date()
        if d != last_day:
            with lock:
                tg(f"📅 {last_day} 全日\n" + summary(rows, day_start) + "\n累計\n" + summary(rows, 0)
                   + ("\n影子變體（全日）\n" + vsummary(vrows, day_start) if VARIANTS else ""))
            last_day = d; day_start = now
        time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
