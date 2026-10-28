"""Exit rules after entry — return AND risk, side by side. Read-only.

fish 2026-10-28: 「震盪到 0.70/0.65 果斷賣出，就算最後沒賠也保本回來不少」and「出手後離基準線還有安全距離就無所謂，
來來回回了就必須走」. Earlier studies only reported expected value; this one also reports what fish cares about: how much
of the stake comes back on the bad trades, the worst single trade, the sum of all losses, and the longest losing streak.

Entries: the live rules' shadow entries (sim_variants*.csv) and/or the REAL fills (live_orders.csv filled x live_results.csv).
Path after entry: sim_book.csv rows (0.5-1 s). Our bid = 1 - other side's best ask. Line = Coinbase TWAP of [open-60, open)
(the official open on Coinbase's scale, as the bot uses since FR-20261028-official-line). Fees 7% x p x (1-p) on buy and sell.
A sell is filled at the bid EXIT_LAG seconds after the trigger (1.0 s = today's sign+post; 0.5 s = with a pre-signed sell).

Rules tested
  hold                       never sell
  bid<=L                     price stop: our bid falls to L
  bid<=entry-D               price stop relative to the entry price
  cross>=k                   the coin price has crossed the line k times since entry (「來來回回」)
  dist<=X                    the coin price is within X USD of the line (no longer a safe distance), X=0 means crossed
  cross&bid>=B               crossed the line, and our bid is still >= B (sell while it is still worth something)

Usage (inside btc5m_sim/):  python3 -I stop_profile.py data [shadow|real|both]     SOL: DIST_SCALE=0.004 ... data-sol
Env: RULES (w60_m95_s20,w45_m95_s20), EXIT_LAG (1.0), DIST_SCALE (1), SINCE (slug start)
"""
import csv, os, sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from twap_vs_coinbase import load, start_of, twap_est
from replay_line_fix import book_rows

RULES = [r for r in os.getenv("RULES", "w60_m95_s20,w45_m95_s20").split(",") if r]
STRICT = os.getenv("STRICT", "1") == "1"   # 1: ignore mirror bids that disagree with our side's own ask (book holes, see 10-27 18:34)
LAG = float(os.getenv("EXIT_LAG", "1.0")); SCALE = float(os.getenv("DIST_SCALE", "1")); SINCE = int(os.getenv("SINCE", "0")); FEE = 0.07


def fee(p):
    return FEE * p * (1 - p)


def shadow_entries(d):
    vf = next((os.path.join(d, f) for f in ("sim_variants_v2.csv", "sim_variants.csv") if os.path.exists(os.path.join(d, f))), None)
    for r in (csv.DictReader(open(vf)) if vf else []):
        if r["variant"] in RULES and r["entered"] == "1" and r.get("won") in ("0", "1"):
            yield dict(rule=r["variant"], slug=r["slug"], side=r["side"], ask=float(r["ask"]), sl=float(r["secs_left"]), won=r["won"] == "1")


def real_entries(d):
    res = {}
    for r in csv.DictReader(open(os.path.join(d, "live_results.csv"))):
        if r.get("won") in ("0", "1"):
            res[(r["slug"], r["variant"])] = r["won"] == "1"
    for r in csv.DictReader(open(os.path.join(d, "live_orders.csv"))):
        k = (r["slug"], r["variant"])
        if r["status"] != "filled" or k not in res:
            continue
        try:
            px = float(r["cost"]) / float(r["filled_size"])
        except (ValueError, ZeroDivisionError):
            continue
        yield dict(rule=r["variant"], slug=r["slug"], side=r["outcome"], ask=round(px, 4), sl=float(r["secs_left"]), won=res[k])


def path_after(rows, e, line):
    """[(secs_left, bid, dist_usd)] from the entry on. bid = 1 - other side's ask; dist = spot - line signed toward our side."""
    out = []; sgn = 1 if e["side"] == "Up" else -1
    for sl, sp, ua, ush, da, dsh in rows:
        if sl > e["sl"] or sl < 0.5:
            continue
        other, ours = (da, ua) if e["side"] == "Up" else (ua, da)
        bid = None if other is None else round(1 - other, 3)
        if STRICT and (bid is None or ours is None or ours - bid > 0.10):
            bid = None               # the mirror bid is only trusted when our own ask is within 10c of it (no book hole)
        dist = None if sp is None or line is None else sgn * (sp - line)
        out.append((sl, bid, dist))
    return out


def fill_after(p, k):
    sl0 = p[k][0]
    for sl, bid, _ in p[k:]:
        if sl <= sl0 - LAG:
            return bid
    return None


def make_rules():
    R = {"hold": lambda p: None}
    for L in (0.75, 0.70, 0.65, 0.60, 0.50):
        R[f"bid<={L:.2f}"] = lambda p, L=L: next((i for i, (sl, b, _) in enumerate(p) if b is not None and b <= L), None)
    for D in (0.10, 0.20, 0.30):
        def rel(p, D=D):
            b0 = next((b for _, b, _ in p if b is not None), None)
            return None if b0 is None else next((i for i, (sl, b, _) in enumerate(p) if b is not None and b <= b0 - D), None)
        R[f"bid<=entry-{D:.2f}"] = rel
    def crossings(p):
        n = 0; last = None; idx = []
        for i, (sl, b, dist) in enumerate(p):
            if dist is None: continue
            s = dist > 0
            if last is not None and s != last:
                n += 1; idx.append(i)
            last = s
        return idx
    for k in (1, 2):
        R[f"cross>={k}"] = lambda p, k=k: (crossings(p)[k - 1] if len(crossings(p)) >= k else None)
    for X in (0, 5, 10):
        R[f"dist<={X * SCALE:g}"] = lambda p, X=X: next((i for i, (sl, b, dist) in enumerate(p) if dist is not None and dist <= X * SCALE), None)
    for B in (0.5, 0.7):
        R[f"cross&bid>={B}"] = lambda p, B=B: next((i for i in crossings(p) if p[i][1] is not None and p[i][1] >= B), None)
    for T in (30, 20, 15, 10):
        R[f"bid<=0.70&left<={T}"] = lambda p, T=T: next((i for i, (sl, b, _) in enumerate(p) if b is not None and b <= 0.70 and sl <= T), None)
        R[f"cross&left<={T}"] = lambda p, T=T: next((i for i in crossings(p) if p[i][0] <= T), None)
    return R


def stats(pnls):
    neg = [x for x in pnls if x < 0]
    streak = best = 0
    for x in pnls:
        streak = streak + 1 if x < 0 else 0; best = max(best, streak)
    return dict(n=len(pnls), net=sum(pnls), losses=len(neg), avg_loss=(sum(neg) / len(neg)) if neg else 0.0,
                worst=min(pnls) if pnls else 0.0, sum_loss=sum(neg), streak=best)


def run(d, entries, label):
    meta, _ = load(d); book = book_rows(d); by_start = {start_of(s): s for s in meta}
    ents = []
    for e in entries:
        if start_of(e["slug"]) < SINCE or e["slug"] not in book:
            continue
        pv = by_start.get(start_of(e["slug"]) - 300)
        te = twap_est(book.get(pv, []), 5) if pv else None
        p = path_after(book[e["slug"]], e, te[0] if te else None)
        if p:
            e["path"] = p; ents.append(e)
    ents.sort(key=lambda e: (start_of(e["slug"]), -e["sl"]))
    cost = sum(e["ask"] for e in ents)
    print(f"\n== {d} / {label}: {len(ents)} entries, avg entry {cost / max(len(ents), 1):.3f}, exit lag {LAG:g}s. "
          f"Per share after fees; 'recovered' = what the stake got back on the trades that would have lost")
    print(f"   {'rule':<18} {'exits':>5} {'net':>7} {'per$1':>7} | {'#loss':>5} {'avg':>6} {'worst':>6} {'sum':>7} {'streak':>6} | {'saved':>5} {'cut':>4} {'recovered':>9}")
    for name, trig in make_rules().items():
        pnls = []; exits = saved = cut = 0; rec = []
        for e in ents:
            hold = (1.0 if e["won"] else 0.0) - e["ask"] - fee(e["ask"])
            k = trig(e["path"])
            b = fill_after(e["path"], k) if k is not None else None
            if b is None or b <= 0:
                pnls.append(hold); continue
            exits += 1; pnls.append(b - e["ask"] - fee(e["ask"]) - fee(b))
            if e["won"]: cut += 1
            else: saved += 1; rec.append(b / e["ask"])
        s = stats(pnls)
        print(f"   {name:<18} {exits:>5} {s['net']:>+7.2f} {s['net'] / cost:>+7.1%} | {s['losses']:>5} {s['avg_loss']:>6.2f} {s['worst']:>6.2f} {s['sum_loss']:>7.2f} {s['streak']:>6} |"
              f" {saved:>5} {cut:>4} {(sum(rec) / len(rec)) if rec else 0:>9.0%}")
    # how often a dip comes back: the market's own view
    print("   bid touched <= L at some point after entry -> how many of those still won (if ~L, selling at L is a fair trade; higher = market overreacts)")
    for L in (0.8, 0.7, 0.6, 0.5, 0.4, 0.3):
        dip = [e for e in ents if any(b is not None and b <= L for _, b, _ in e["path"])]
        w = sum(e["won"] for e in dip)
        print(f"     <= {L:.1f}: {len(dip):>3} trades, won {w:>3} ({w / max(len(dip), 1):.0%})")
    print("   comeback rate by WHEN the bid first touched <= L (won% of those trades; the price at that moment is the market's own estimate)")
    print(f"     {'first touch at':<14}" + "".join(f"{'<=' + str(L):>12}" for L in (0.8, 0.7, 0.6, 0.5)))
    for lo, hi in ((45, 61), (30, 45), (20, 30), (10, 20), (0, 10)):
        cells = []
        for L in (0.8, 0.7, 0.6, 0.5):
            g = [e for e in ents if next(((sl, b) for sl, b, _ in e["path"] if b is not None and b <= L), (None, None))[0] is not None
                 and lo <= next(sl for sl, b, _ in e["path"] if b is not None and b <= L) < hi]
            cells.append(f"{sum(e['won'] for e in g):>3}/{len(g):<3}{(sum(e['won'] for e in g) / len(g)) if g else 0:>5.0%}")
        print(f"     {lo:>2}-{hi:<2}s left   " + "".join(f"{c:>12}" for c in cells))
    cr = [e for e in ents if any(dist is not None and dist <= 0 for _, _, dist in e["path"])]
    print(f"   price crossed the line after entry: {len(cr)} trades, won {sum(e['won'] for e in cr)} ({sum(e['won'] for e in cr) / max(len(cr), 1):.0%});"
          f" never crossed: {len(ents) - len(cr)} trades, lost {sum(not e['won'] for e in ents if e not in cr)}")


if __name__ == "__main__":
    d = sys.argv[1] if len(sys.argv) > 1 else "data"; which = sys.argv[2] if len(sys.argv) > 2 else "both"
    if which in ("shadow", "both"):
        run(d, list(shadow_entries(d)), f"shadow entries of {RULES}")
    if which in ("real", "both"):
        run(d, list(real_entries(d)), "REAL fills (all rules)")
