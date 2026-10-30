"""fish 2026-10-30：第一張照 fish 規律（賣價 ≤ 0.39、近 20 秒跌 ≥ 0.10、開盤 120 秒內），比較第二張的各種補法：
  A 固定目標：對面讓每股淨賺 ≥ T 就補（兩張都吃單、都扣手續費）。
  B 目標＋停損：同 A，但第一張那邊再跌 D（賣價 ≤ p1 − D）時，立刻用對面現價補（鎖小虧，不抱單邊到 0）。
  C 目標隨時間降低：買後 0 秒要 T，之後每過 60 秒少 STEP，最低降到 FLOOR（可以是負的＝願意小虧補）。
  D 收盤前保底：同 A，但到開盤後 X 秒還沒補，就用對面現價補（不管賺賠）。
  E 不算手續費的帳面門檻（fish 的看法）：兩邊相加 ≤ 1 − G（帳面每股賺 ≥ G）就補；同時列出扣手續費後每對平均剩多少。
沒補到就抱到結算。資料：/opt/pm-pair/data 全部場次。用法：cd /opt/pm-pair/data && python3 -I ../pair_leg2_rules.py
"""
import csv, glob, gzip, itertools
from collections import defaultdict
fee = lambda p: 0.07 * p * (1 - p)
net = lambda p1, p2: 1 - p1 - p2 - fee(p1) - fee(p2)
G = []
for asset in ("btc", "eth", "sol"):
    tmp = defaultdict(list)
    for p in sorted(glob.glob(f"book_{asset}_*.csv") + glob.glob(f"book_{asset}_*.csv.gz")):
        for r in csv.DictReader(gzip.open(p, "rt") if p.endswith(".gz") else open(p)):
            g = lambda k: float(r[k]) if r[k] else None
            tmp[r["slug"]].append((float(r["s_from_open"]), g("up_ask"), g("dn_ask"), g("up_bid") or 0, g("dn_bid") or 0))
    for s, rows in tmp.items():
        rows.sort()
        if rows[0][0] > -20 or rows[-1][0] < 280: continue
        win = "Up" if rows[-1][3] >= 0.9 else "Down" if rows[-1][4] >= 0.9 else None
        if win: G.append((rows, win))

def entry(rows, level=0.39, drop=0.10, w=120):
    hist = []
    for i, (t, ua, da, ub, db) in enumerate(rows):
        hist.append((t, ua, da))
        while hist and hist[0][0] < t - 20: hist.pop(0)
        if t < 0: continue
        if t > w: return None
        for side, k, a in (("Up", 1, ua), ("Down", 2, da)):
            if a is None or a > level: continue
            hk = [h[k] for h in hist if h[k] is not None]
            if hist[-1][0] - hist[0][0] < 10 or not hk: continue
            if max(hk) - a >= drop - 1e-9: return i, side, a, t
E = [(rows, win, entry(rows)) for rows, win in G]; E = [x for x in E if x[2]]
print(f"{len(G)} 場，第一張觸發 {len(E)} 場\n")

def sim(rule):
    c = defaultdict(int); pnl = cost = 0.0; worst = 0
    for rows, win, (i, side, p1, t1) in E:
        k1, k2 = (1, 2) if side == "Up" else (2, 1); res = None
        for r in rows[i + 1:]:
            if r[0] > 299: break
            o, me = r[k2], r[k1]
            if o is None: continue
            if rule(p1, t1, r[0], o, me): res = net(p1, o); cost_ = p1 + o + fee(p1) + fee(o); break
        if res is not None:
            c["pair+" if res > 0 else "pair-"] += 1; pnl += res; cost += cost_; worst = min(worst, res)
        else:
            won = side == win; c["held_won" if won else "held_lost"] += 1; v = (1 - p1 if won else -p1) - fee(p1); pnl += v; cost += p1 + fee(p1); worst = min(worst, v)
    n = len(E)
    return f"配成賺 {c['pair+']:>3}  配成虧 {c['pair-']:>3}  單邊輸 {c['held_lost']:>3} | 每$1 {pnl/cost:>+6.1%} | 每場 {pnl/n*100:>+5.1f} 分"

print("== A 固定目標 ==")
for T in (0.03, 0.05, 0.10, 0.15, 0.20):
    print(f"  目標 {T*100:>3.0f} 分                     | " + sim(lambda p1, t1, t, o, me, T=T: net(p1, o) >= T))
print("== B 目標＋停損（第一張那邊再跌 D 就用對面現價補）==")
for T, D in itertools.product((0.05, 0.10, 0.15), (0.05, 0.10, 0.15, 0.20)):
    print(f"  目標 {T*100:>3.0f} 分、再跌 {D:.2f} 停損     | " + sim(lambda p1, t1, t, o, me, T=T, D=D: net(p1, o) >= T or (me is not None and me <= p1 - D)))
print("== C 目標隨時間降低（每 60 秒少 STEP，最低 FLOOR）==")
for T, STEP, FLOOR in itertools.product((0.10, 0.15), (0.05, 0.10), (0.0, -0.05, -0.10)):
    print(f"  {T*100:>3.0f} 分起、每分鐘降 {STEP*100:.0f}、最低 {FLOOR*100:>+3.0f} | " + sim(lambda p1, t1, t, o, me, T=T, S=STEP, F=FLOOR: net(p1, o) >= max(F, T - S * (t - t1) / 60)))
print("== D 目標＋時間到就補（開盤後 X 秒還沒補，用對面現價補）==")
for T, X in itertools.product((0.05, 0.10, 0.15), (150, 200, 250)):
    print(f"  目標 {T*100:>3.0f} 分、第 {X} 秒保底        | " + sim(lambda p1, t1, t, o, me, T=T, X=X: net(p1, o) >= T or t >= X))

print("== E 不算手續費的帳面門檻（相加 ≤ 1 − G 就補）==")
for G in (0.03, 0.05, 0.07, 0.09, 0.12, 0.15, 0.20):
    c = defaultdict(int); pnl = cost = gross = netp = 0.0
    for rows, win, (i, side, p1, t1) in E:
        k1, k2 = (1, 2) if side == "Up" else (2, 1); res = None
        for r in rows[i + 1:]:
            if r[0] > 299: break
            o = r[k2]
            if o is not None and 1 - p1 - o >= G - 1e-9: res = o; break
        if res is not None:
            c["pair"] += 1; v = net(p1, res); pnl += v; cost += p1 + res + fee(p1) + fee(res); gross += 1 - p1 - res; netp += v
        else:
            won = side == win; c["won" if won else "lost"] += 1; v = (1 - p1 if won else -p1) - fee(p1); pnl += v; cost += p1 + fee(p1)
    n = len(E); pr = c["pair"]
    print(f"  帳面 ≥ {G*100:>3.0f} 分就補 | 配成 {pr/n:>4.0%}  單邊輸 {c['lost']:>3} | 每對平均：帳面 {gross/pr*100:>4.1f} 分、扣手續費後 {netp/pr*100:>+5.1f} 分 | 每$1 {pnl/cost:>+6.1%}")
