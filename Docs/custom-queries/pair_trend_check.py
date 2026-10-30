"""fish 2026-10-30：「好幾根一直往上（或往下）的行情，我會吃不到單、賠單腿。」
驗證：這一場開盤前的走勢（前幾根 5 分鐘 K 線）能不能事先告訴我們「這場容易配不成」。
  連續同向根數：前面連續幾場的結算方向都一樣（例如前 3 場都是 Up）。
  前 15／30 分鐘漲跌幅：開盤時 Chainlink 價格和 15／30 分鐘前比，取絕對值（bps＝萬分之一）。
進場（模仿 fish）：開盤 90 秒內，便宜那邊第一次 ≤ 0.40 就市價買；之後對面能不能讓每股淨賺 ≥ 7 分（第二張掛單）。
反向（fish 提議）：同一刻改買貴的那邊（正在漲的那邊），之後等便宜那邊跌到能配成。
唯讀 /opt/pm-pair/data。用法：cd /opt/pm-pair/data && python3 -I ../pair_trend_check.py
"""
import csv, glob, gzip
from collections import defaultdict
fee = lambda p: 0.07 * p * (1 - p)
TARGET, LEVEL, W = 0.07, 0.40, 90

games = defaultdict(dict)   # asset -> start -> dict
for asset in ("btc", "eth", "sol"):
    tmp = defaultdict(list); cl = {}
    for p in sorted(glob.glob(f"book_{asset}_*.csv") + glob.glob(f"book_{asset}_*.csv.gz")):   # 前幾天的檔案已壓縮
        for r in csv.DictReader(gzip.open(p, "rt") if p.endswith(".gz") else open(p)):
            g = lambda k: float(r[k]) if r[k] else None
            tmp[r["slug"]].append((float(r["s_from_open"]), g("up_ask"), g("dn_ask"), g("up_bid"), g("dn_bid")))
            if r["cl_price"] and r["cl_sec"]: cl[int(r["cl_sec"])] = float(r["cl_price"])
    for s, rows in tmp.items():
        rows.sort(); start = int(s.split("-")[-1])
        if rows[0][0] > -5 or rows[-1][0] < 280: continue
        win = "Up" if (rows[-1][3] or 0) >= 0.9 else "Down" if (rows[-1][4] or 0) >= 0.9 else None
        if win: games[asset][start] = {"post": [x for x in rows if x[0] >= 0], "win": win}
    for start, gm in games[asset].items():
        near = lambda t: next((cl[t + d] for d in (0, -1, 1, -2, 2) if t + d in cl), None)
        c0 = near(start)
        for m in (15, 30):
            cm = near(start - m * 60)
            gm[f"mv{m}"] = abs(c0 - cm) / cm * 1e4 if c0 and cm else None

def streak(asset, start):
    k, d = 0, None
    while True:
        g = games[asset].get(start - 300 * (k + 1))
        if not g: return k if d else None if k == 0 else k
        if d is None: d = g["win"]
        if g["win"] != d: return k
        k += 1

rows = []
for asset, G in games.items():
    for start, gm in G.items():
        post, win = gm["post"], gm["win"]; leg = None
        for i, (t, ua, da, ub, db) in enumerate(post):
            if t > W: break
            if ua is None or da is None: continue
            side, p1 = ("Up", ua) if ua <= da else ("Down", da)
            if p1 <= LEVEL: leg = (i, side, p1); break
        if not leg: continue
        i, side, p1 = leg
        def res(side, p1):
            o = [r[2] if side == "Up" else r[1] for r in post[i + 1:]]; o = [x for x in o if x is not None]
            if o and min(o) <= int((1 - p1 - fee(p1) - TARGET) * 100 + 1e-9) / 100 + 1e-9:
                p2 = int((1 - p1 - fee(p1) - TARGET) * 100 + 1e-9) / 100; return True, False, 1 - p1 - p2 - fee(p1), p1 + p2
            won = side == win; return False, not won, ((1 - p1) if won else -p1) - fee(p1), p1
        oth = "Down" if side == "Up" else "Up"; pr = post[i][2] if side == "Up" else post[i][1]
        ok, lost, pnl, cost = res(side, p1); rok, rlost, rpnl, rcost = res(oth, pr)
        rows.append(dict(asset=asset, st=streak(asset, start), mv15=gm["mv15"], mv30=gm["mv30"], ok=ok, lost=lost, pnl=pnl, cost=cost,
                         rok=rok, rlost=rlost, rpnl=rpnl, rcost=rcost))

print(f"{len(rows)} 場有進場（{', '.join(f'{a} {sum(r['asset']==a for r in rows)}' for a in games)}）；目標每對 {TARGET*100:.0f} 分、便宜那邊 ≤ {LEVEL} 就買、開盤 {W} 秒內\n")
def show(label, X):
    if not X: return
    n = len(X); ok = sum(r["ok"] for r in X); bad = sum(r["lost"] for r in X); rok = sum(r["rok"] for r in X); rbad = sum(r["rlost"] for r in X)
    pr = sum(r["pnl"] for r in X) / sum(r["cost"] for r in X); rr = sum(r["rpnl"] for r in X) / sum(r["rcost"] for r in X)
    print(f"  {label:<20} | {n:>4} 場 | 買便宜那邊：配成 {ok/n:>4.0%} 單腿輸 {bad:>3} 每$1 {pr:>+6.1%} | 反向買貴的：配成 {rok/n:>4.0%} 單腿輸 {rbad:>3} 每$1 {rr:>+6.1%}")
show("全部", rows); print()
print("== 前面連續同方向的根數（之前幾場結算方向都一樣）==")
for lo, hi, lab in ((0, 1, "0～1 根（剛換方向）"), (2, 2, "2 根"), (3, 3, "3 根"), (4, 99, "4 根以上")):
    show(lab, [r for r in rows if r["st"] is not None and lo <= r["st"] <= hi])
for m in (15, 30):
    X = [r for r in rows if r[f"mv{m}"] is not None]; v = sorted(r[f"mv{m}"] for r in X); n = len(v)
    cuts = [0, v[n // 3], v[2 * n // 3], v[int(n * 0.9)], 1e9]
    print(f"== 開盤前 {m} 分鐘的漲跌幅（絕對值，萬分之一）==")
    for lo, hi in zip(cuts, cuts[1:]):
        show(f"{lo:>5.1f}～{hi if hi < 1e8 else 999:>5.1f}", [r for r in X if lo <= r[f"mv{m}"] < hi])
