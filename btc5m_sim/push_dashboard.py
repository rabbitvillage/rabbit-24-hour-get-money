"""Push a summary of sim_trades.csv / sim_variants.csv to the dashboard Worker.
Read-only on this host; one POST with a bearer token. Standard library only.

Env (btc5m_sim/.dash.env, 0600):  DASH_URL=https://btc5m.baixiong99.cc/api/push  DASH_TOKEN=...
Optional: DATA_DIR (default btc5m_sim/data), SERVICE (systemd unit to report, default btc5m-sim)
Run:  python3 -m btc5m_sim.push_dashboard
"""
import csv, glob, json, os, socket, subprocess, sys, time, urllib.request
from datetime import datetime, timezone

ASSET = os.getenv("ASSET", "btc").strip().lower()
DATA = os.getenv("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
SERVICE = os.getenv("SERVICE", "btc5m-sim" if ASSET == "btc" else f"btc5m-sim@{ASSET}")
URL = os.getenv("DASH_URL", "")
TOKEN = os.getenv("DASH_TOKEN", "")


def f(v):
    try:
        return float(v) if v not in ("", None) else None
    except ValueError:
        return None


def median(xs):
    xs = sorted(x for x in xs if x is not None)
    return None if not xs else xs[len(xs) // 2]


def pctl(xs, p):
    xs = sorted(x for x in xs if x is not None)
    return None if not xs else xs[min(len(xs) - 1, int(p * len(xs)))]


def stats(rows):
    ent = [r for r in rows if r.get("entered") == "1"]
    done = [r for r in ent if r.get("won") in ("0", "1")]
    losses = sum(1 for r in done if r["won"] == "0")
    pnl = sum(f(r.get("pnl")) or 0 for r in done)
    return dict(markets=len(rows), entries=len(ent), settled=len(done), losses=losses, pnl=round(pnl, 4),
                median_depth_usd=median([f(r.get("ask_usd_le_max")) for r in ent]),
                avg_pnl_per_trade=round(pnl / len(done), 4) if done else None,
                avg_fetch_ms=round(sum(f(r.get("fetch_ms")) or 0 for r in ent) / len(ent)) if ent else None)


def read(name):
    """Read <name>.csv plus any <name>_vN.csv (the simulator starts a new file when its columns change)."""
    base, ext = os.path.splitext(name); out = []
    for p in sorted(glob.glob(os.path.join(DATA, base + ext)) + glob.glob(os.path.join(DATA, base + "_v*" + ext))):
        with open(p, newline="") as fh:
            out.extend(csv.DictReader(fh))
    return out


def ts(r):
    try:
        return datetime.fromisoformat(r["start_utc"]).timestamp()
    except Exception:
        return 0


BUCKET = 5          # seconds per bucket on the curve
CURVE_FROM = 150    # first bucket (matches BOOK_WINDOW)
CURVE_DAYS = 2      # only the last N days of sim_book.csv feed the curve


def favorite(ua, uu, da, du):
    """(best ask, usd depth) of the favourite side, or (None, None) when the favourite has no ask.
    Favourite = the side with the higher ask; a side with no ask while the other is cheap (<=0.5) is the favourite with nothing to buy."""
    if ua is None and da is None:
        return None, None
    if ua is None:
        return (None, None) if da <= 0.5 else (da, du)
    if da is None:
        return (None, None) if ua <= 0.5 else (ua, uu)
    return (ua, uu) if ua >= da else (da, du)


def bucket(secs):
    """secs_left -> bucket upper edge (5..CURVE_FROM): 44.6 -> 45, 45.0 -> 45, 45.1 -> 50."""
    b = int(-(-secs // BUCKET)) * BUCKET
    return None if b < BUCKET or b > CURVE_FROM else b


def book_curve(brows):
    """Per-bucket share of markets whose favourite still has an ask, median favourite ask / depth; per-market vanish stats."""
    by = {}
    for r in brows:
        by.setdefault(r["slug"], []).append(r)
    agg = {b: [] for b in range(CURVE_FROM, 0, -BUCKET)}
    vanish, first98, ask_secs = [], [], []
    for slug, rs in by.items():
        seen = {}
        last_ask = None; f98 = None; n98 = 0
        for r in rs:
            s = f(r["secs_left"])
            if s is None:
                continue
            fa, fu = favorite(f(r["up_ask"]), f(r["up_usd"]), f(r["down_ask"]), f(r["down_usd"]))
            b = bucket(s)
            if b in agg and b not in seen:          # one sample per market per bucket
                seen[b] = True; agg[b].append((fa is not None, fa, fu))
            if fa is not None:
                last_ask = s if last_ask is None or s < last_ask else last_ask
                if 0.98 <= fa <= 0.99:
                    n98 += 1
                    if f98 is None or s > f98:
                        f98 = s
        vanish.append(last_ask); first98.append(f98); ask_secs.append(n98)
    curve = [dict(secs=b, markets=len(v), has_ask_pct=round(100 * sum(1 for h, _, _ in v if h) / len(v), 1) if v else None,
                  median_ask=median([a for _, a, _ in v if a is not None]), median_usd=median([u for _, _, u in v if u is not None]))
             for b, v in agg.items()]
    n = len(by)
    dist = dict(markets=n, never=sum(1 for x in vanish if x is None),
                before_102=sum(1 for x in vanish if x is not None and x > 102), s60_102=sum(1 for x in vanish if x is not None and 60 < x <= 102),
                s45_60=sum(1 for x in vanish if x is not None and 45 < x <= 60), s30_45=sum(1 for x in vanish if x is not None and 30 < x <= 45),
                under_30=sum(1 for x in vanish if x is not None and x <= 30),
                vanish_median=median([x for x in vanish if x is not None]),
                with_98=sum(1 for x in first98 if x is not None), first98_median=median([x for x in first98 if x is not None]),
                secs98_median=median([x for x, fx in zip(ask_secs, first98) if fx is not None]))
    return curve, dist


def decisions(n_markets=12):
    """Last n markets: per rule, bought or why not, plus live status. Compact for the dashboard."""
    rows = read("sim_decisions.csv")
    if not rows: return []
    slugs = []
    for r in rows:
        if r["slug"] not in slugs: slugs.append(r["slug"])
    out = []
    for s in slugs[-n_markets:]:
        rs = [r for r in rows if r["slug"] == s]
        out.append(dict(slug=s.replace(f"{ASSET}-updown-5m-", ""), time=rs[0]["start_utc"][11:16], winner=rs[0]["winner"],
                        rules=[dict(name=r["variant"], decision=r["decision"], reason=r["reason"], ask=f(r["ask"]), secs=f(r["secs_left"]),
                                    dl=f(r["dl"]), thr=f(r["threshold"]), lead_min=f(r["lead_min"]), lead_max=f(r["lead_max"]),
                                    live=r["live_status"], won=f(r["won"]), pnl=f(r["pnl"])) for r in rs]))
    return out[::-1]


def service_state():
    try:
        return subprocess.run(["systemctl", "is-active", SERVICE], capture_output=True, text=True, timeout=5).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def build():
    rows = read("sim_trades.csv"); vrows = read("sim_variants.csv")
    cutoff = time.time() - CURVE_DAYS * 86400
    brows = [r for r in read("sim_book.csv") if int(r["slug"].rsplit("-", 1)[-1]) >= cutoff]
    curve, vanish = book_curve(brows)
    now = time.time(); day = now - 86400
    ent = [r for r in rows if r.get("entered") == "1"]
    recent = [dict(slug=r["slug"].replace(f"{ASSET}-updown-5m-", ""), time=r["start_utc"][:16].replace("T", " "), side=r["side"], ask=f(r["ask"]),
                   secs_left=f(r["secs_left"]), depth_usd=f(r["ask_usd_le_max"]), winner=r["winner"], won=f(r["won"]), pnl=f(r["pnl"]))
              for r in ent[-10:]][::-1]
    vnames = sorted({r["variant"] for r in vrows}, key=lambda s: (s[1:3], s))
    variants = [dict(name=v, **stats([r for r in vrows if r["variant"] == v])) for v in vnames]
    losses = [dict(slug=r["slug"].replace(f"{ASSET}-updown-5m-", "") + " " + r["variant"], time=r["start_utc"][:16].replace("T", " "), side=r["side"], ask=f(r["ask"]),
                   secs_left=f(r["secs_left"]), dist_at_entry=f(r.get("dist_at_entry")), min_abs_dist=f(r.get("min_abs_dist")), pnl=f(r["pnl"]))
              for r in vrows if r.get("won") == "0"][-20:][::-1]
    per_market = {}
    for r in vrows:
        per_market.setdefault(r["slug"], r)
    pm = list(per_market.values())
    distance = dict(samples=sum(1 for r in pm if (f(r.get("spot_samples")) or 0) > 0),
                    ptb_err_median=median([abs(f(r.get("ptb_err"))) for r in pm if f(r.get("ptb_err")) is not None]),
                    ptb_err_p90=pctl([abs(f(r.get("ptb_err"))) for r in pm if f(r.get("ptb_err")) is not None], 0.9),
                    min_abs_dist_median=median([f(r.get("min_abs_dist")) for r in pm]),
                    entries_with_dist=sum(1 for r in vrows if r.get("entered") == "1" and f(r.get("dist_at_entry")) is not None),
                    chain_err_median=median([abs(f(r.get("chain_ptb_err"))) for r in pm if f(r.get("chain_ptb_err")) is not None]),
                    chain_err_p90=pctl([abs(f(r.get("chain_ptb_err"))) for r in pm if f(r.get("chain_ptb_err")) is not None], 0.9),
                    chain_markets=sum(1 for r in pm if f(r.get("chain_ptb_err")) is not None))
    return dict(asset=ASSET, generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), host=socket.gethostname(), service=service_state(),
                rule="最後 45 秒，領先方賣價 0.98~0.99，5 股", first_row_utc=rows[0]["start_utc"][:16] if rows else "", last_row_utc=rows[-1]["start_utc"][:16] if rows else "",
                all=stats(rows), last24h=stats([r for r in rows if ts(r) >= day]), variants=variants, recent=recent, losses=losses, distance=distance,
                curve=curve, vanish=vanish, decisions=decisions())


def main():
    snap = build()
    if "--print" in sys.argv or not URL:
        print(json.dumps(snap, ensure_ascii=False, indent=1)); return 0
    body = json.dumps(snap, ensure_ascii=False).encode()
    url = URL if ASSET == "btc" else URL + f"?asset={ASSET}"
    return post(url, body)


def post(url, body, attempts=2, pause=3.0):
    """POST once; on a transport error (connection reset, timeout) wait and try once more. A second failure still exits
    non-zero so systemd records it. 2026-10-22 21:54 one 'Connection reset by peer' from the edge tripped the host alert."""
    import time
    last = None
    for i in range(attempts):
        req = urllib.request.Request(url, data=body, method="POST", headers={"content-type": "application/json", "authorization": f"Bearer {TOKEN}", "user-agent": "btc5m-push"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                print(datetime.now().strftime("%m-%d %H:%M:%S"), "pushed", len(body), "bytes ->", r.status, "(retry)" if i else ""); return 0
        except urllib.error.HTTPError:
            raise                                   # 4xx/5xx from the Worker: not transient, do not repeat the body
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last = e; print(datetime.now().strftime("%m-%d %H:%M:%S"), f"push attempt {i + 1} failed: {type(e).__name__} {e}")
            if i + 1 < attempts: time.sleep(pause)
    raise last


if __name__ == "__main__":
    sys.exit(main())
