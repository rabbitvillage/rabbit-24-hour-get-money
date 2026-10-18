"""Push a summary of sim_trades.csv / sim_variants.csv to the dashboard Worker.
Read-only on this host; one POST with a bearer token. Standard library only.

Env (btc5m_sim/.dash.env, 0600):  DASH_URL=https://btc5m.baixiong99.cc/api/push  DASH_TOKEN=...
Optional: DATA_DIR (default btc5m_sim/data), SERVICE (systemd unit to report, default btc5m-sim)
Run:  python3 -m btc5m_sim.push_dashboard
"""
import csv, json, os, socket, subprocess, sys, time, urllib.request
from datetime import datetime, timezone

DATA = os.getenv("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
SERVICE = os.getenv("SERVICE", "btc5m-sim")
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
    p = os.path.join(DATA, name)
    if not os.path.exists(p):
        return []
    with open(p, newline="") as fh:
        return list(csv.DictReader(fh))


def ts(r):
    try:
        return datetime.fromisoformat(r["start_utc"]).timestamp()
    except Exception:
        return 0


def service_state():
    try:
        return subprocess.run(["systemctl", "is-active", SERVICE], capture_output=True, text=True, timeout=5).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def build():
    rows = read("sim_trades.csv"); vrows = read("sim_variants.csv")
    now = time.time(); day = now - 86400
    ent = [r for r in rows if r.get("entered") == "1"]
    recent = [dict(slug=r["slug"].replace("btc-updown-5m-", ""), time=r["start_utc"][:16].replace("T", " "), side=r["side"], ask=f(r["ask"]),
                   secs_left=f(r["secs_left"]), depth_usd=f(r["ask_usd_le_max"]), winner=r["winner"], won=f(r["won"]), pnl=f(r["pnl"]))
              for r in ent[-10:]][::-1]
    vnames = sorted({r["variant"] for r in vrows}, key=lambda s: (s[1:3], s))
    variants = [dict(name=v, **stats([r for r in vrows if r["variant"] == v])) for v in vnames]
    losses = [dict(slug=r["slug"].replace("btc-updown-5m-", "") + " " + r["variant"], time=r["start_utc"][:16].replace("T", " "), side=r["side"], ask=f(r["ask"]),
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
                    entries_with_dist=sum(1 for r in vrows if r.get("entered") == "1" and f(r.get("dist_at_entry")) is not None))
    return dict(generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), host=socket.gethostname(), service=service_state(),
                rule="最後 45 秒，領先方賣價 0.98~0.99，5 股", first_row_utc=rows[0]["start_utc"][:16] if rows else "", last_row_utc=rows[-1]["start_utc"][:16] if rows else "",
                all=stats(rows), last24h=stats([r for r in rows if ts(r) >= day]), variants=variants, recent=recent, losses=losses, distance=distance)


def main():
    snap = build()
    if "--print" in sys.argv or not URL:
        print(json.dumps(snap, ensure_ascii=False, indent=1)); return 0
    body = json.dumps(snap, ensure_ascii=False).encode()
    req = urllib.request.Request(URL, data=body, method="POST", headers={"content-type": "application/json", "authorization": f"Bearer {TOKEN}", "user-agent": "btc5m-push"})
    with urllib.request.urlopen(req, timeout=15) as r:
        print(datetime.now().strftime("%m-%d %H:%M:%S"), "pushed", len(body), "bytes ->", r.status); return 0


if __name__ == "__main__":
    sys.exit(main())
