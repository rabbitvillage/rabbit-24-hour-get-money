"""fish 2026-10-30 直覺：左邊（便宜的那邊）約 0.30～0.40 時，「右邊賣價 − 左邊賣價」小於某個值，這場就一定配得成（賺錢）。
做法：每一場、開盤前 W 秒內，取「左邊賣價第一次落在某個價位區間」那一刻當作買第一張（市價），
記下當下的差距 gap = 右邊賣價 − 左邊賣價、兩邊相加 sum；之後看右邊賣價有沒有跌到能配成：
  相加 < 1、掛單淨賺 ≥ 3 分、≥ 7 分（掛單價 = 1 − p1 − 第一張手續費 − 目標）。
第一部分：按左邊價位分組；第二部分：同一個左邊價位裡，差距小的和差距大的比較（看差距有沒有額外的資訊）；
第三部分：直接用差距門檻（gap ≤ D 且左邊 ≤ 0.45 的第一刻進場）。唯讀 /opt/pm-pair/data。
用法：cd /opt/pm-pair/data && python3 -I ../pair_gap_check.py
"""
import importlib.util, io, contextlib, os
here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("dt", os.path.join(here, "pair_dip_turn.py")); dt = importlib.util.module_from_spec(spec)
with contextlib.redirect_stdout(io.StringIO()): spec.loader.exec_module(dt)
fee = dt.fee; mk = dt.mk
W = 60

def outcome(post, i, side, p1, win):
    o = [r[3] if side == "Up" else r[1] for r in post[i + 1:]]; o = [x for x in o if x is not None]
    m = min(o) if o else 1.0
    return {"sum1": p1 + m < 1, "n3": 1 - p1 - m - fee(p1) >= 0.03, "n7": 1 - p1 - m - fee(p1) >= 0.07, "lost": side != win}

def entries(cond):
    """每場第一個符合 cond 的時刻（開盤前 W 秒內）"""
    out = []
    for s, (post, win) in mk.items():
        for i, (t, ua, us, da, ds, ub, db) in enumerate(post):
            if t > W: break
            if ua is None or da is None: continue
            side, p1, right = ("Up", ua, da) if ua <= da else ("Down", da, ua)
            if cond(p1, right - p1, t):
                out.append((p1, right - p1, p1 + right, t, outcome(post, i, side, p1, win))); break
    return out

def show(label, E):
    if not E: print(f"{label:<26} |   0"); return
    n = len(E); c = lambda k: sum(e[4][k] for e in E)
    gaps = sorted(e[1] for e in E); sums = sorted(e[2] for e in E)
    print(f"{label:<26} | {n:>3} | 差距 中位 {gaps[n//2]:.2f}（{gaps[0]:.2f}～{gaps[-1]:.2f}） 相加 中位 {sums[n//2]:.2f}（{sums[0]:.2f}～{sums[-1]:.2f}）"
          f" | 相加<1 {c('sum1'):>3}（{c('sum1')/n:>4.0%}） 淨≥3分 {c('n3'):>3}（{c('n3')/n:>4.0%}） 淨≥7分 {c('n7'):>3}（{c('n7')/n:>4.0%}）")

print(f"{len(mk)} 場完整資料，進場只看開盤前 {W} 秒\n")
print("== 第一部分：左邊第一次落到這個價位就買 ==")
for lo, hi in ((0.40, 0.45), (0.35, 0.40), (0.30, 0.35), (0.25, 0.30), (0.20, 0.25), (0.0, 0.20)):
    show(f"左邊 {lo:.2f}～{hi:.2f}", entries(lambda p, g, t, lo=lo, hi=hi: lo <= p < hi))

print("\n== 第二部分：同一個左邊價位，差距小 vs 差距大（差距 = 右 − 左；相加越大差距越大）==")
for lo, hi in ((0.35, 0.40), (0.30, 0.35), (0.25, 0.30)):
    E = entries(lambda p, g, t, lo=lo, hi=hi: lo <= p < hi)
    if len(E) < 6: continue
    E.sort(key=lambda e: e[1]); h = len(E) // 2
    show(f"左邊 {lo:.2f}～{hi:.2f} 差距小半", E[:h]); show(f"左邊 {lo:.2f}～{hi:.2f} 差距大半", E[h:])

print("\n== 第三部分：直接用差距門檻（左邊 ≤ 0.45 且 差距 ≥ D 的第一刻）==")
for D in (0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50):
    show(f"差距 ≥ {D:.2f}", entries(lambda p, g, t, D=D: p <= 0.45 and g >= D - 1e-9))
