"""fish 2026-10-30：「只要整場沒有偏向某個確定結果、還在震盪，這個方法好像都可以。」
問題：買第一張的那一刻，能不能事先量出「還沒偏向結果」？試兩種量法：
  A. 幣價離開盤基準線多遠（基準線 = 開盤前 60 秒 Chainlink 平均，和官方結算一樣算法）：
     用「標準化距離」 = 距離（bps）/ √剩餘秒數；越小代表結果越沒定。
  B. 買之前 30 秒的晃動：Up 賣價穿過 0.50 幾次、Up 賣價的高低差。
進場：開盤前 W 秒內，左邊（便宜那邊）第一次 ≤ LEVEL 就市價買；之後右邊能不能配成（掛單淨賺 ≥ 7 分），配不成就是買到輸的那邊。
唯讀 /opt/pm-pair/data。用法：cd /opt/pm-pair/data && python3 -I ../pair_undecided_check.py
"""
import csv, glob, math
from collections import defaultdict
FEE = 0.07; fee = lambda p: FEE * p * (1 - p)
TARGET = 0.07

G = {}
for asset in ("btc", "eth", "sol"):
    tmp = defaultdict(list)
    for p in sorted(glob.glob(f"book_{asset}_*.csv")):
        for r in csv.DictReader(open(p)):
            g = lambda k: float(r[k]) if r[k] else None
            tmp[r["slug"]].append((float(r["s_from_open"]), g("up_ask"), g("dn_ask"), g("up_bid"), g("dn_bid"), g("cl_price"), r["cl_sec"]))
    for s, rows in tmp.items():
        rows.sort()
        if rows[0][0] > -60 or rows[-1][0] < 280: continue
        win = "Up" if (rows[-1][3] or 0) >= 0.9 else "Down" if (rows[-1][4] or 0) >= 0.9 else None
        start = int(s.split("-")[-1]); seen = {}
        for t, ua, da, ub, db, cl, cs in rows:
            if cl is not None and cs and start - 60 <= int(cs) < start: seen[int(cs)] = cl
        if not win or len(seen) < 45: continue
        G[s] = ([r for r in rows if r[0] >= 0], win, sum(seen.values()) / len(seen))
print(f"{len(G)} 場有完整盤口和開盤基準線；目標每對淨賺 {TARGET*100:.0f} 分（第二張掛單）\n")

def entries(level, w):
    out = []
    for s, (post, win, line) in G.items():
        for i, (t, ua, da, ub, db, cl, cs) in enumerate(post):
            if t > w: break
            if ua is None or da is None or cl is None: continue
            side, p1 = ("Up", ua) if ua <= da else ("Down", da)
            if p1 > level: continue
            d_bps = (cl - line) / line * 1e4 * (1 if side == "Up" else -1)   # 負 = 幣價往左邊不利的方向
            z = abs(d_bps) / math.sqrt(max(300 - t, 1))
            prev = [r[1] for r in post[:i + 1] if r[0] >= t - 30 and r[1] is not None]
            cross = sum(1 for a, b in zip(prev, prev[1:]) if (a > 0.5) != (b > 0.5))
            rng = max(prev) - min(prev) if prev else 0
            o = [r[2] if side == "Up" else r[1] for r in post[i + 1:]]; o = [x for x in o if x is not None]
            m = min(o) if o else 1.0
            ok = 1 - p1 - m - fee(p1) >= TARGET
            out.append(dict(s=s, p1=p1, t=t, d=d_bps, z=z, cross=cross, rng=rng, ok=ok, lost=(side != win)))
            break
    return out

def by(E, key, cuts, label):
    print(f"  按{label}：")
    for lo, hi in zip(cuts, cuts[1:]):
        X = [e for e in E if lo <= e[key] < hi]
        if not X: continue
        n = len(X); ok = sum(e["ok"] for e in X); bad = sum((not e["ok"]) and e["lost"] for e in X)
        print(f"    {lo:>6.2f}～{hi:<6.2f} | {n:>3} 場 | 配成 {ok:>3}（{ok/n:>4.0%}） 配不成又輸 {bad:>2}（{bad/n:>4.0%}）")

for level, w in ((0.40, 60), (0.35, 60), (0.30, 60), (0.40, 120)):
    E = entries(level, w)
    n = len(E); ok = sum(e["ok"] for e in E)
    print(f"== 左邊 ≤ {level:.2f}、開盤 {w} 秒內進場：{n} 場，配成 {ok}（{ok/n:.0%}）==")
    zs = sorted(e["z"] for e in E); q = [0, zs[n // 3], zs[2 * n // 3], 99]
    by(E, "z", q, "標準化距離（三等分，越小 = 結果越沒定）")
    by(E, "cross", [0, 1, 2, 4, 99], "前 30 秒 Up 穿過 0.50 的次數")
    rs = sorted(e["rng"] for e in E); by(E, "rng", [0, rs[n // 3], rs[2 * n // 3], 9], "前 30 秒 Up 賣價高低差（三等分）")
    print()
