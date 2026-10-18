"""Phase 0: watch every BTC 5m market live, pretend to buy the favorite at
98-99c in the last 45s, record what was really on the book, and score it
when the market resolves. Read-only: no wallet, no orders.

Run:  python3 -m btc5m_sim.sim            (env: see .env.example)
"""
import csv, json, os, sys, threading, time, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta

UA = {"User-Agent": "Mozilla/5.0 btc5m-sim"}
GAMMA = "https://gamma-api.polymarket.com/events?slug=btc-updown-5m-{}"
BOOK = "https://clob.polymarket.com/book?token_id={}"

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

FIELDS = ["slug", "start_utc", "price_to_beat", "entered", "secs_left", "side", "ask",
          "ask_shares_le_max", "ask_usd_le_max", "other_side_ask", "fetch_ms",
          "cost", "fee", "winner", "won", "pnl", "resolved_after_s", "note"]


def get(url, timeout=10):
    t0 = time.time()
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return json.load(r), (time.time() - t0) * 1000


def log(*a):
    print(datetime.now(TZ).strftime("%m-%d %H:%M:%S"), *a, flush=True)


# ---------------- Telegram ----------------
def tg(text):
    global TG_CHAT
    if not TG_TOKEN:
        return
    try:
        if not TG_CHAT:
            ups, _ = get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates")
            chats = [u.get("message", u.get("channel_post", {})).get("chat", {}).get("id") for u in ups.get("result", [])]
            chats = [c for c in chats if c]
            if not chats:
                log("TG: no chat yet - send /start to the bot (or set TG_CHAT_ID)")
                return
            TG_CHAT = str(chats[-1])
            log("TG: using chat", TG_CHAT)
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


# ---------------- one market ----------------
def watch(start, rows, lock):
    end = start + 300
    row = dict(slug=f"btc-updown-5m-{start}", start_utc=datetime.fromtimestamp(start, timezone.utc).isoformat(),
               entered=0, note="")
    try:
        m = None
        while m is None and time.time() < end - LAST_ENTRY:
            try:
                m = market(start)
            except Exception:
                time.sleep(2)
        if m is None:
            row["note"] = "market not found"; return
        row["price_to_beat"] = m["ptb"]
        while time.time() < end - ENTRY_WINDOW:
            time.sleep(min(1, end - ENTRY_WINDOW - time.time()) if end - ENTRY_WINDOW - time.time() > 0 else 0)
        while not row["entered"] and time.time() < end - LAST_ENTRY:
            t_loop = time.time()
            try:
                books, ms = [], 0
                for tok in m["tokens"]:
                    b, dt = get(BOOK.format(tok), timeout=5); books.append(b); ms += dt
                asks = [best_ask(b, MAX_PRICE) for b in books]
                for i, (ask, sh, usd) in enumerate(asks):
                    if ask is not None and MIN_PRICE <= ask <= MAX_PRICE and sh >= SHARES:
                        row.update(entered=1, secs_left=round(end - time.time(), 1), side=m["outcomes"][i],
                                   ask=ask, ask_shares_le_max=round(sh, 2), ask_usd_le_max=round(usd, 2),
                                   other_side_ask=asks[1 - i][0], fetch_ms=round(ms),
                                   cost=round(ask * SHARES, 4), fee=round(fee(ask) * SHARES, 5))
                        break
            except Exception as e:
                row["note"] = f"book error {type(e).__name__}"
            time.sleep(max(0, POLL - (time.time() - t_loop)))
        # wait for resolution
        time.sleep(max(0, end + 60 - time.time()))
        for _ in range(60):
            try:
                m2 = market(start)
                if m2 and m2["closed"]:
                    win = m2["outcomes"][0] if float(m2["prices"][0]) > 0.5 else m2["outcomes"][1]
                    row["price_to_beat"] = m2["ptb"] or row.get("price_to_beat")
                    row["winner"] = win; row["resolved_after_s"] = round(time.time() - end)
                    if row["entered"]:
                        row["won"] = int(row["side"] == win)
                        row["pnl"] = round((SHARES if row["won"] else 0) - row["cost"] - row["fee"], 4)
                    break
            except Exception:
                pass
            time.sleep(30)
        else:
            row["note"] += " unresolved after 30m"
    finally:
        row["_t"] = time.time()
        with lock:
            rows.append(row)
            path = os.path.join(DATA, "sim_trades.csv")
            new = not os.path.exists(path)
            with open(path, "a", newline="") as f:
                w = csv.DictWriter(f, FIELDS); new and w.writeheader(); w.writerow({k: row.get(k, "") for k in FIELDS})
        if row.get("entered"):
            log(f"{row['slug']} buy {row['side']} @{row['ask']} ({row['secs_left']}s left, {row['ask_usd_le_max']}$ on book) -> {row.get('winner')} pnl {row.get('pnl')}")
            if row.get("won") == 0:
                tg(f"❌ 模擬輸一筆 {row['slug']}\n買 {row['side']} @{row['ask']}，剩 {row['secs_left']}s，結果 {row.get('winner')}，損益 {row.get('pnl')}")
        else:
            log(f"{row['slug']} no entry {row.get('note','')} winner {row.get('winner')}")


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


def main():
    os.makedirs(DATA, exist_ok=True)
    rows, lock, seen = [], threading.Lock(), set()
    log(f"start: entry {MIN_PRICE}-{MAX_PRICE} in last {ENTRY_WINDOW}s, {SHARES:g} shares, poll {POLL}s")
    tg(f"🟢 BTC 5m 模擬 bot 啟動（不下單）\n規則：最後 {ENTRY_WINDOW} 秒，領先方賣價 {MIN_PRICE}~{MAX_PRICE} 就假想買 {SHARES:g} 股")
    last_hour = int(time.time() // 3600); last_day = datetime.now(TZ).date(); day_start = time.time()
    while True:
        now = time.time()
        start = int(now // 300) * 300
        if start not in seen and now < start + 300 - ENTRY_WINDOW - 5:
            seen.add(start)
            threading.Thread(target=watch, args=(start, rows, lock), daemon=True).start()
        h = int(now // 3600)
        if h != last_hour:
            with lock:
                msg = summary(rows, now - 3600)
            tg("⏱ 過去一小時\n" + msg); log("hourly:", msg); last_hour = h
        d = datetime.now(TZ).date()
        if d != last_day:
            with lock:
                tg(f"📅 {last_day} 全日\n" + summary(rows, day_start) + "\n累計\n" + summary(rows, 0))
            last_day = d; day_start = now
        time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
