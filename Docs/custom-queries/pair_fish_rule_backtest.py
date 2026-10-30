"""fish 2026-10-30：用 fish 第一張的規律回測（從 11 場練習歸納，fish 說不出來，這是我們的猜測）。
第一張：買正在跌的那邊——它的賣價 ≤ LEVEL，而且近 20 秒從最高點跌了 ≥ DROP；只在開盤後 W 秒內；不等反彈，條件一成立就照賣價買（吃單）。
第二張（fish 的規律還看不出來，用幾種門檻試）：之後對面賣價讓「扣兩張吃單手續費後每股淨賺 ≥ TARGET」就立刻買同樣股數；
          都沒出現就抱到結算。另列「事後看最多能賺多少」當上限參考。
對照組：DROP = 0（不管跌多快，到價就買）。資料：/opt/pm-pair/data 全部場次（含已壓縮的前幾天）。
用法：cd /opt/pm-pair/data && python3 -I ../pair_fish_rule_backtest.py
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
            g = lambda k: float(r[k]) if r[k] else None          # 沒有賣單的時刻保留成 None，不能整筆丟（會只留下拖到最後還很接近的場次）
            tmp[r["slug"]].append((float(r["s_from_open"]), g("up_ask"), g("dn_ask"), g("up_bid") or 0, g("dn_bid") or 0))
    for s, rows in tmp.items():
        rows.sort()
        if rows[0][0] > -20 or rows[-1][0] < 280: continue
        win = "Up" if rows[-1][3] >= 0.9 else "Down" if rows[-1][4] >= 0.9 else None
        if win: G.append((asset, s, rows, win))
hours = len(G) / 3 * 5 / 60
print(f"{len(G)} 場（BTC／ETH／SOL 各約 {len(G)//3} 場，約 {hours:.0f} 小時）\n")

def entry(rows, level, drop, w):
    hist = []
    for i, (t, ua, da, ub, db) in enumerate(rows):
        hist.append((t, ua, da))
        while hist and hist[0][0] < t - 20: hist.pop(0)
        if t < 0: continue
        if t > w: return None
        for side, k, a in (("Up", 1, ua), ("Down", 2, da)):
            if a is None or a > level: continue
            hk = [h[k] for h in hist if h[k] is not None]
            if hist[-1][0] - hist[0][0] < 10 or not hk: continue   # 近 20 秒資料不足 10 秒不算
            if max(hk) - a >= drop - 1e-9: return i, side, a, t
    return None

def run(level, drop, w, target, assets=None):
    c = defaultdict(int); pnl = cost = 0.0; waits = []
    for asset, s, rows, win in G:
        if assets and asset not in assets: continue
        e = entry(rows, level, drop, w)
        if not e: continue
        i, side, p1, t1 = e; k2 = 2 if side == "Up" else 1; done = None; best = -9
        for r in rows[i + 1:]:
            if r[0] > 300: break
            if r[k2] is None: continue
            best = max(best, net(p1, r[k2]))
            if target is not None and done is None and net(p1, r[k2]) >= target - 1e-9: done = (r[k2], r[0] - t1)
        if target is None:                                    # 事後最多能賺多少（上限參考）
            if best > 0: c["pair"] += 1; pnl += best; cost += 1 - best; continue
        if done:
            c["pair"] += 1; pnl += net(p1, done[0]); cost += p1 + done[0] + fee(p1) + fee(done[0]); waits.append(done[1])
        else:
            won = side == win; c["won" if won else "lost"] += 1; pnl += (1 - p1 if won else -p1) - fee(p1); cost += p1 + fee(p1)
    n = sum(c.values()); waits.sort()
    return n, c, pnl, cost, (waits[len(waits) // 2] if waits else None)

print("== 你的規律：近 20 秒跌 ≥ 0.10、跌到 0.39 以下、開盤 120 秒內就買 ==")
print(f"{'第二張門檻':<14} | {'場數':>4} {'配成':>5} {'單邊輸':>5} {'單邊贏':>5} | {'每$1':>7} | {'每場平均':>8} | {'等多久(中位)':>10}")
for tg in (0.0, 0.02, 0.03, 0.05, 0.07, 0.10, 0.15, None):
    n, c, pnl, cost, wt = run(0.39, 0.10, 120, tg)
    lab = "事後最多（上限）" if tg is None else f"每股賺 ≥ {tg*100:.0f} 分就補"
    print(f"{lab:<14} | {n:>4} {c['pair']:>5} {c['lost']:>5} {c['won']:>5} | {pnl/cost:>+7.1%} | {pnl/n*100:>+6.1f} 分 | {('—' if wt is None else f'{wt:.0f} 秒'):>10}")

print("\n== 參數敏感度（第二張：每股賺 ≥ 5 分就補）==")
print(f"{'到價':>5}{'跌幅':>6}{'時窗':>6} | {'場數':>4} {'配成率':>6} {'單邊輸':>5} | {'每$1':>7}")
for level, drop, w in itertools.product((0.35, 0.39, 0.41), (0.0, 0.06, 0.10, 0.15), (60, 120)):
    n, c, pnl, cost, wt = run(level, drop, w, 0.05)
    if n: print(f"{level:>5.2f}{drop:>6.2f}{w:>5}s | {n:>4} {c['pair']/n:>6.0%} {c['lost']:>5} | {pnl/cost:>+7.1%}")

print("\n== 分幣種（你的規律，第二張每股 ≥ 5 分就補）==")
for a in ("btc", "eth", "sol"):
    n, c, pnl, cost, wt = run(0.39, 0.10, 120, 0.05, {a})
    if n: print(f"  {a.upper()}：{n} 場，配成 {c['pair']/n:.0%}，單邊輸 {c['lost']}，每$1 {pnl/cost:+.1%}")
