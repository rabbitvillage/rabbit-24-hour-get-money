"""Coinbase vs the official settlement price (Chainlink 60-second TWAP), market by market. Read-only.

Polymarket's 5-minute up/down markets resolve on the Chainlink 60s-TWAP data stream:
  priceToBeat(k) = TWAP over the 60 s before market k opens  (== finalPrice(k-1))
  finalPrice(k)  = TWAP over the last 60 s of market k
So finalPrice(k) = priceToBeat(k+1). The bot records Coinbase mid every 0.5 s for the last 150 s of each market (sim_book.csv),
which lets us rebuild a "Coinbase TWAP" for the same windows and ask:
  1. how far Coinbase sits from the official number (basis) and how stable that gap is from one market to the next;
  2. how often Coinbase alone gets the official winner right, versus the bot's current distance (Coinbase now - on-chain open);
  3. with s seconds left, how well "Coinbase TWAP so far + current price for the rest" predicts the result, and how that
     compares with the market's ask at the same moment.

Usage:  python3 -I twap_vs_coinbase.py [data_dir ...]      (run inside btc5m_sim/, default: data)
"""
import csv, math, os, statistics as st, sys
from collections import defaultdict

LEFTS = [60, 45, 30, 20, 10, 5]
EBINS = [("<0", 0), ("0-1", 1), ("1-2", 2), ("2-4", 4), ("4-8", 8), (">=8", float("inf"))]


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))] if xs else float("nan")


def ncdf(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def load(d):
    meta = {}
    for fn in ("sim_variants.csv", "sim_variants_v2.csv"):
        p = os.path.join(d, fn)
        if not os.path.exists(p):
            continue
        for r in csv.DictReader(open(p)):
            s = r["slug"]
            if s in meta or not r.get("price_to_beat") or r.get("winner") not in ("Up", "Down"):
                continue
            f = lambda k: float(r[k]) if r.get(k) not in (None, "") else None
            meta[s] = dict(ptb=float(r["price_to_beat"]), winner=r["winner"], spot0=f("spot_at_start"), chain0=f("chain_at_start"))
    book = defaultdict(list)
    for r in csv.DictReader(open(os.path.join(d, "sim_book.csv"))):
        try:
            sl = float(r["secs_left"])
        except ValueError:
            continue
        if sl > 61 or not r["spot"]:
            continue
        ua = float(r["up_ask"]) if r["up_ask"] else None
        da = float(r["down_ask"]) if r["down_ask"] else None
        book[r["slug"]].append((sl, float(r["spot"]), ua, da))
    return meta, book


def start_of(slug):
    return int(slug.rsplit("-", 1)[1])


def twap_est(rows, s):
    """Time-weighted Coinbase TWAP for the last 60 s using only samples taken with secs_left >= s (rows sorted by secs_left,
    descending; each price holds until the next sample). The unseen rest (s..0) is filled with the latest price.
    Needs a sample within 3 s of the window start and no gap > 3 s. Returns (estimate, latest_price) or None."""
    pts = [(sl, p) for sl, p, *_ in rows if s <= sl <= 62]
    if not pts or pts[0][0] < 57:
        return None
    area = 0.0; prev_sl, prev_p = min(pts[0][0], 60.0), pts[0][1]
    for sl, p in pts[1:]:
        if sl > 60:
            prev_p = p; continue
        if prev_sl - sl > 3:
            return None
        area += prev_p * (prev_sl - sl); prev_sl, prev_p = sl, p
    if prev_sl - s > 3:
        return None
    area += prev_p * prev_sl                     # latest price held from its sample time to the close
    return area / 60.0, prev_p


def bps(x, ref):
    return x / ref * 1e4


def main(d):
    meta, book = load(d)
    by_start = {start_of(s): s for s in meta}
    for s, m in meta.items():
        nxt = by_start.get(start_of(s) + 300)
        m["final"] = meta[nxt]["ptb"] if nxt else None
        book[s].sort(key=lambda r: -r[0])
    prev_of = lambda s: by_start.get(start_of(s) - 300)
    thr_usd = 20 * float(os.getenv("DIST_SCALE", "1"))      # bot's s20 rule: $20 for BTC; SOL runs with DIST_SCALE=0.004
    cbt = {}
    for s in meta:                                          # Coinbase TWAP of the closing window (last 5 s filled)
        e = twap_est(book.get(s, []), 5)
        if e:
            cbt[s] = e[0]
    print(f"== {d}: markets with result {len(meta)}, with official final {sum(1 for m in meta.values() if m['final'])}, "
          f"with Coinbase closing TWAP {len(cbt)}  (bps = 0.01% of price; $1 BTC ~ 0.12 bps; bot's distance rule ${thr_usd:g})")

    # 1. basis
    basis = {s: bps(cbt[s] - meta[s]["final"], meta[s]["ptb"]) for s in cbt if meta[s]["final"] is not None}
    bl = list(basis.values()); ab = lambda xs, q: pct([abs(x) for x in xs], q)
    dbs = [b - basis[prev_of(s)] for s, b in basis.items() if prev_of(s) in basis]
    pe = [bps(m["spot0"] - m["ptb"], m["ptb"]) for m in meta.values() if m["spot0"] is not None]
    ce = [bps(m["chain0"] - m["ptb"], m["ptb"]) for m in meta.values() if m["chain0"] is not None]
    print("\n1) Coinbase TWAP - official TWAP over the same 60 s, in bps")
    print(f"   gap (basis)            n={len(bl):>5}  p10 {pct(bl, .1):+.2f}  p50 {pct(bl, .5):+.2f}  p90 {pct(bl, .9):+.2f}")
    print(f"   gap change, next mkt   n={len(dbs):>5}  |p50| {ab(dbs, .5):.2f}  |p90| {ab(dbs, .9):.2f}  |p99| {ab(dbs, .99):.2f}   <- error left when Coinbase is used on both ends")
    print(f"   bot's open line today: Coinbase spot at open vs official open |p50| {ab(pe, .5):.2f} |p90| {ab(pe, .9):.2f};"
          f"  Polygon on-chain feed at open |p50| {ab(ce, .5):.2f} |p90| {ab(ce, .9):.2f}")

    # 2. who gets the official winner right, at the close
    close = lambda m: m["final"] is not None and abs(m["final"] - m["ptb"]) < thr_usd
    print(f"\n2) Who calls the official winner right at the close ('close' = official move < ${thr_usd:g})")
    tests = {
        "Coinbase TWAP close vs Coinbase TWAP open": lambda s, m: (cbt[s] - cbt[prev_of(s)]) if s in cbt and prev_of(s) in cbt else None,
        "Coinbase TWAP close vs official open": lambda s, m: (cbt[s] - m["ptb"]) if s in cbt else None,
        "bot's line: Coinbase spot @5s vs on-chain open": lambda s, m: (book[s][-1][1] - m["chain0"])
            if book.get(s) and m["chain0"] is not None and book[s][-1][0] <= 7 else None,
        "Coinbase spot @5s vs Coinbase spot at open": lambda s, m: (book[s][-1][1] - m["spot0"])
            if book.get(s) and m["spot0"] is not None and book[s][-1][0] <= 7 else None,
    }
    for name, fn in tests.items():
        n = ok = cn = cok = 0
        for s, m in meta.items():
            v = fn(s, m)
            if v is None:
                continue
            r = ("Up" if v >= 0 else "Down") == m["winner"]; n += 1; ok += r
            if close(m):
                cn += 1; cok += r
        if n:
            print(f"   {name:<48} {ok}/{n} = {ok / n:6.1%}   close markets {cok}/{cn} = {cok / max(cn, 1):6.1%}")

    # 3. with s seconds left
    print("\n3) With s seconds left.  TWAP call: E = (Coinbase TWAP so far + current price for the rest) - Coinbase TWAP of the open window")
    print("   bot call: sign(Coinbase now - on-chain open), the line the distance filter uses today")
    for L in LEFTS:
        obs = []
        for s, m in meta.items():
            pv = prev_of(s)
            if pv not in cbt or m["final"] is None:
                continue
            e = twap_est(book.get(s, []), L)
            if not e:
                continue
            row = min((r for r in book[s] if r[0] >= L), key=lambda r: r[0])
            E = bps(e[0] - cbt[pv], m["ptb"]); real = bps(m["final"] - m["ptb"], m["ptb"])
            dl = None if m["chain0"] is None else row[1] - m["chain0"]
            obs.append(dict(E=E, real=real, win=m["winner"], ua=row[2], da=row[3], dl=dl, close=close(m)))
        if not obs:
            continue
        errs = [o["real"] - o["E"] for o in obs]
        tw = sum(("Up" if o["E"] >= 0 else "Down") == o["win"] for o in obs)
        bo = [o for o in obs if o["dl"] is not None]
        bw = sum(("Up" if o["dl"] >= 0 else "Down") == o["win"] for o in bo)
        co = [o for o in obs if o["close"]]
        ctw = sum(("Up" if o["E"] >= 0 else "Down") == o["win"] for o in co)
        cbo = [o for o in co if o["dl"] is not None]
        cbw = sum(("Up" if o["dl"] >= 0 else "Down") == o["win"] for o in cbo)
        print(f"\n   s={L:>2}s  n={len(obs)}  TWAP-call |error| p50 {pct([abs(x) for x in errs], .5):.2f} p90 {pct([abs(x) for x in errs], .9):.2f}"
              f" p99 {pct([abs(x) for x in errs], .99):.2f} bps   right: TWAP call {tw / len(obs):.1%}, bot call {bw / max(len(bo), 1):.1%}"
              f"   | close markets: TWAP {ctw / max(len(co), 1):.1%}, bot {cbw / max(len(cbo), 1):.1%} (n={len(co)})")
        # bot's band: a side's ask is 0.80-0.95 at this moment. Group by how far the TWAP call leans to that side.
        cells = defaultdict(lambda: [0, 0, 0.0])
        for o in obs:
            for side, ask in (("Up", o["ua"]), ("Down", o["da"])):
                if ask is None or not (0.80 <= ask <= 0.95):
                    continue
                sgn = 1 if side == "Up" else -1
                es = sgn * o["E"]
                bot_ok = o["dl"] is not None and sgn * o["dl"] >= thr_usd
                k = next(lbl for lbl, hi in EBINS if es < hi)
                for key in ((("bot passes" if bot_ok else "bot blocks"), k), ("all", k)):
                    c = cells[key]; c[0] += 1; c[1] += side == o["win"]; c[2] += ask
        if cells:
            print("      a side's ask is 0.80-0.95 now (bot's band), grouped by TWAP lean toward that side (bps); edge = won - ask per share, before fees")
            for g in ("all", "bot passes", "bot blocks"):
                for lbl, _ in EBINS:
                    n, w, sa = cells[(g, lbl)]
                    if n:
                        print(f"         {g:<10} lean {lbl:<9} n={n:>4}  won {w / n:6.1%}  avg ask {sa / n:.3f}  edge {w / n - sa / n:+.3f}")


    # 4. the bot's own rule entries (shadow ledger of the live rules): would the TWAP call have flagged the losers?
    rules = [r.strip() for r in os.getenv("RULES", "w60_m95_s20,w45_m95_s20").split(",") if r.strip()]
    ents = []
    p2 = os.path.join(d, "sim_variants_v2.csv")
    for r in csv.DictReader(open(p2)) if os.path.exists(p2) else []:
        if r["variant"] not in rules or r["entered"] != "1" or r.get("won") not in ("0", "1", "True", "False"):
            continue
        sl = float(r["secs_left"]); slug = r["slug"]; pv = prev_of(slug)
        if slug not in meta or pv not in cbt:
            continue
        e = twap_est(book.get(slug, []), sl)
        if not e:
            continue
        sgn = 1 if r["side"] == "Up" else -1
        lean = sgn * bps(e[0] - cbt[pv], meta[slug]["ptb"])
        ents.append(dict(rule=r["variant"], sl=sl, ask=float(r["ask"]), won=r["won"] in ("1", "True"), lean=lean))
    if ents:
        print(f"\n4) Shadow entries of {rules}: TWAP lean toward the side bought, at the moment of entry (bps)")
        bins = [("<0 (TWAP says other side)", 0), ("0-0.5", .5), ("0.5-1", 1), ("1-2", 2), ("2-4", 4), (">=4", float("inf"))]
        for rule in rules + ["both"]:
            es = [x for x in ents if rule == "both" or x["rule"] == rule]
            if not es:
                continue
            lost = sum(not x["won"] for x in es)
            print(f"   {rule}: entries {len(es)}, lost {lost} ({lost / len(es):.1%}), avg ask {sum(x['ask'] for x in es) / len(es):.3f}")
            lo = -float("inf")
            for lbl, hi in bins:
                g = [x for x in es if lo <= x["lean"] < hi]; lo = hi
                if g:
                    w = sum(x["won"] for x in g); a = sum(x["ask"] for x in g) / len(g)
                    print(f"      lean {lbl:<26} n={len(g):>4}  lost {len(g) - w:>3}  won {w / len(g):6.1%}  avg ask {a:.3f}  edge/share {w / len(g) - a:+.3f}")
            ls = sorted(x["lean"] for x in es if not x["won"])
            print(f"      losers' lean: {', '.join(f'{v:+.2f}' for v in ls)}")


if __name__ == "__main__":
    for d in sys.argv[1:] or ["data"]:
        main(d)
