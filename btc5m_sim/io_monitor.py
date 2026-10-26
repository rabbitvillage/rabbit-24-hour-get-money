"""IO (Independent Operation, 獨立作業): a stand-alone shadow that "buys" the favourite at 0.97 whenever its best ask
is 0.97 inside the last IO_WINDOW seconds (default 60), once per market, and books the result after settlement.
fish 2026-10-26: 只看勝率，不管滑價 — so the fill is assumed at the ask it saw; the only cost booked is the exchange's
taker fee (7% x p x (1-p) per share, paid in USDC on top of the price).

It never places orders, never imports the bot and never writes into the sim's data folders. It reads
  <data>/sim_book.csv    the per-poll order-book curve of the last 150 s, which the sim appends after each settlement
  <data>/sim_trades.csv  the settled winner per market
and keeps its own files under btc5m_sim/io/<asset>/: io_trades.csv (one row per shadow buy), io_state.json
(read position in sim_book.csv + markets still waiting for a winner) and io_report.txt (rewritten every run).

Run once (the btc5m-io.timer does this every 5 min):   python3 -m btc5m_sim.io_monitor
Print the report only:                                   python3 -m btc5m_sim.io_monitor --report
Start from the beginning of the recorded history instead of from now (first run only):  --backfill
Env: IO_DATA_DIRS=data,data-eth,data-sol  IO_PRICE=0.97  IO_WINDOW=60  IO_SHARES=10  IO_OUT=<btc5m_sim>/io
"""
import csv, json, os, sys, time
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
TZ = timezone(timedelta(hours=8))
PRICE = float(os.getenv("IO_PRICE", "0.97"))
WINDOW = float(os.getenv("IO_WINDOW", "60"))
SHARES = float(os.getenv("IO_SHARES", "10"))
FEE_RATE = 0.07
DATA_DIRS = [d.strip() for d in os.getenv("IO_DATA_DIRS", "data,data-eth,data-sol").split(",") if d.strip()]
OUT = os.getenv("IO_OUT", os.path.join(HERE, "io"))
LFIELDS = ["booked_at", "slug", "start_utc", "asset", "side", "ask", "shares_at_ask", "secs_left", "shares", "cost", "fee", "winner", "won", "pnl", "session"]
BREAKEVEN = 1 - PRICE - FEE_RATE * PRICE * (1 - PRICE)      # flip rate at which a buy at PRICE nets zero after the fee

try:
    from zoneinfo import ZoneInfo
    NY = ZoneInfo("America/New_York")
except Exception:
    NY = None


def session(start_utc):
    """'US' during the New York cash session (Mon-Fri 09:30-16:00 NY), else 'nonUS'."""
    t = datetime.fromisoformat(start_utc)
    t = t.astimezone(NY) if NY else t.astimezone(timezone(timedelta(hours=-4 if t < datetime(2026, 11, 1, 6, tzinfo=timezone.utc) else -5)))
    return "US" if t.weekday() < 5 and 9 * 60 + 30 <= t.hour * 60 + t.minute < 16 * 60 else "nonUS"


def asset_of(d):
    base = os.path.basename(os.path.normpath(d))
    return "btc" if base == "data" else base.split("-", 1)[1]


def data_path(d):
    return d if os.path.isabs(d) else os.path.join(HERE, d)


def load_state(p):
    st = dict(since=None, offset=0, pending={}, markets=0, no_trigger=0, unresolved=0, recent=[])
    if os.path.exists(p):
        st.update(json.load(open(p)))
    return st


def save_state(p, st):
    tmp = p + ".tmp"
    with open(tmp, "w") as f: json.dump(st, f)
    os.replace(tmp, p)


def winners(d):
    out = {}
    p = os.path.join(d, "sim_trades.csv")
    if os.path.exists(p):
        for r in csv.DictReader(open(p)):
            if r.get("winner") in ("Up", "Down"): out[r["slug"]] = (r["winner"], r["start_utc"])
    return out


def scan(d, st, backfill):
    """Read the new rows of sim_book.csv; remember, per market, the earliest moment inside the window at which one
    side's best ask was PRICE (within half a cent, so 0.001-tick books count too) with shares on it."""
    p = os.path.join(d, "sim_book.csv")
    if not os.path.exists(p): return
    size = os.path.getsize(p)
    if st["since"] is None:
        st["since"] = datetime.now(TZ).isoformat(timespec="seconds")
        st["offset"] = 0 if backfill else size              # forward test: only markets settled from now on
    if st["offset"] > size:                                  # file truncated or rotated
        st["offset"] = 0; st["pending"] = {}
    recent = set(st["recent"])
    with open(p, newline="") as f:
        f.seek(st["offset"])
        if st["offset"] == 0: f.readline()                   # header
        while True:
            pos = f.tell(); line = f.readline()
            if not line or not line.endswith("\n"):           # partial last line: the sim is still writing it
                st["offset"] = pos; break
            try:
                slug, secs, up_ask, up_sh, _, down_ask, down_sh = next(csv.reader([line]))[:7]
                secs = float(secs)
            except (ValueError, StopIteration):
                continue
            if slug in recent: continue
            pend = st["pending"].setdefault(slug, {"trig": None})   # every market with book rows counts, triggered or not
            if secs > WINDOW: continue
            for side, a, sh in (("Up", up_ask, up_sh), ("Down", down_ask, down_sh)):
                try:
                    a = float(a); sh = float(sh or 0)
                except ValueError:
                    continue
                if abs(a - PRICE) < 0.005 and sh > 0 and (pend["trig"] is None or secs > pend["trig"][2]):
                    pend["trig"] = [side, a, secs, sh]


def finalize(d, st, ledger, asset):
    """Book every pending market that has a winner now; drop markets that never settle (sim gap) after 3 hours."""
    w = winners(d); rows = []
    for slug in list(st["pending"]):
        pend = st["pending"][slug]
        if slug in w:
            winner, start_utc = w[slug]; st["markets"] += 1
            if pend["trig"]:
                side, ask, secs, sh = pend["trig"]
                cost = round(SHARES * ask, 4); fee = round(FEE_RATE * ask * (1 - ask) * SHARES, 4); won = int(side == winner)
                rows.append(dict(booked_at=datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"), slug=slug, start_utc=start_utc, asset=asset, side=side, ask=ask,
                                 shares_at_ask=sh, secs_left=secs, shares=SHARES, cost=cost, fee=fee, winner=winner, won=won,
                                 pnl=round((SHARES - cost - fee) if won else -(cost + fee), 4), session=session(start_utc)))
            else:
                st["no_trigger"] += 1
            del st["pending"][slug]; st["recent"] = (st["recent"] + [slug])[-200:]
        else:
            try: start_ts = int(slug.rsplit("-", 1)[1])
            except ValueError: start_ts = 0
            if time.time() - start_ts > 3 * 3600:
                st["unresolved"] += 1; del st["pending"][slug]
    if rows:
        new = not os.path.exists(ledger)
        with open(ledger, "a", newline="") as f:
            wr = csv.DictWriter(f, fieldnames=LFIELDS)
            if new: wr.writeheader()
            wr.writerows(rows)
    return rows


def report(st, ledger, asset):
    rows = list(csv.DictReader(open(ledger))) if os.path.exists(ledger) else []
    n = len(rows); wins = sum(int(r["won"]) for r in rows); flips = n - wins; pnl = sum(float(r["pnl"]) for r in rows)
    L = [f"[{asset.upper()}] IO 獨立作業：最後 {WINDOW:g} 秒內領先方賣價 {PRICE:.2f} 就買（每筆 {SHARES:g} 股，只扣手續費，不算滑價）",
         f"  自 {st['since']} 起已結算 {st['markets']} 場，觸發 {n} 場" + (f"（{100*n/st['markets']:.0f}%）" if st["markets"] else "") +
         f"，贏 {wins}、翻船 {flips}" + (f"，勝率 {100*wins/n:.2f}%，翻船率 {100*flips/n:.2f}%（損益兩平 {100*BREAKEVEN:.2f}%）" if n else "") +
         f"，損益 {pnl:+.2f}" + (f"，未結算丟棄 {st['unresolved']}" if st["unresolved"] else "")]
    for name, key in (("非美股時段", "nonUS"), ("美股時段", "US")):
        g = [r for r in rows if r["session"] == key]
        if g:
            gw = sum(int(r["won"]) for r in g)
            L.append(f"  {name}：{len(g)} 場，贏 {gw}、翻船 {len(g)-gw}，翻船率 {100*(len(g)-gw)/len(g):.2f}%，損益 {sum(float(r['pnl']) for r in g):+.2f}")
    days = {}
    for r in rows:
        day = datetime.fromisoformat(r["start_utc"]).astimezone(TZ).strftime("%m-%d")
        x = days.setdefault(day, [0, 0, 0.0]); x[0] += 1; x[1] += int(r["won"]); x[2] += float(r["pnl"])
    for day in sorted(days):
        x = days[day]; L.append(f"  {day}：{x[0]} 場，贏 {x[1]}、翻船 {x[0]-x[1]}，損益 {x[2]:+.2f}")
    fl = [r for r in rows if r["won"] == "0"]
    if fl:
        L.append("  翻船明細（台灣時間 開盤時刻 市場 買的那邊 賣價 剩幾秒 時段）：")
        for r in fl[-30:]:
            L.append(f"    {datetime.fromisoformat(r['start_utc']).astimezone(TZ):%m-%d %H:%M} {r['slug']} {r['side']} {float(r['ask']):.3f} {float(r['secs_left']):.0f}s {r['session']}")
    return "\n".join(L)


def main(argv):
    only_report = "--report" in argv; backfill = "--backfill" in argv
    out_all = []
    for d in DATA_DIRS:
        d = data_path(d); asset = asset_of(d); odir = os.path.join(OUT, asset); os.makedirs(odir, exist_ok=True)
        state_p, ledger = os.path.join(odir, "io_state.json"), os.path.join(odir, "io_trades.csv")
        st = load_state(state_p)
        if not only_report:
            if backfill and st["since"] is not None: print(f"[{asset}] --backfill ignored: the ledger already started at {st['since']}")
            scan(d, st, backfill); finalize(d, st, ledger, asset); save_state(state_p, st)
        if st["since"] is None: print(f"[{asset}] not started yet"); continue
        text = report(st, ledger, asset)
        with open(os.path.join(odir, "io_report.txt"), "w") as f: f.write(text + "\n")
        out_all.append(text)
    print("\n".join(out_all))


if __name__ == "__main__":
    main(sys.argv[1:])
