"""Entry window x price cap x distance threshold grid, replayed on sim_book.csv with the official line. Read-only.
fish 2026-10-29: 「出手點好像都消失了，倒數 2 分到 1 分半就決定了」-> how much frequency do earlier windows buy, at what cost?
Same filters as the live rules: floor FLOOR (0.85), SHARES on the book, direction + distance, chop 8 bps, book gap 0.05,
one order per market, non-US hours (09:00-17:00 New York Mon-Fri). Per share after fee; per$1 = net / cost.
Usage (inside btc5m_sim/): python3 -I window_grid.py [data]     needs twap_vs_coinbase.py + replay_line_fix.py beside it.
"""
import csv, os, sys
from datetime import datetime
from zoneinfo import ZoneInfo
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from twap_vs_coinbase import load, start_of, twap_est
from replay_line_fix import book_rows
NY = ZoneInfo("America/New_York"); FEE = 0.07; SH = float(os.getenv("SHARES", "5")); CHOP = float(os.getenv("CHOP_MAX_AMP_BPS", "8"))
GAP = float(os.getenv("BOOK_GAP_MAX", "0.05")); FLOOR = float(os.getenv("FLOOR", "0.85"))
WINDOWS = [int(x) for x in os.getenv("WINDOWS", "60,90,120").split(",")]; CAPS = [float(x) for x in os.getenv("CAPS", "0.95,0.99").split(",")]
THRS = [float(x) for x in os.getenv("THRS", "15,30").split(",")]

def us(st):
    t = datetime.fromtimestamp(st, NY); return t.weekday() < 5 and 9 <= t.hour < 17

def replay(rows, ref, window, cap, thr):
    lo = hi = None
    for sl, sp, ua, ush, da, dsh in rows:
        if sp is not None: lo = sp if lo is None else min(lo, sp); hi = sp if hi is None else max(hi, sp)
        if sl > window or sl <= 5: continue
        hit = None
        for side, ask, sh, oth in (("Up", ua, ush, da), ("Down", da, dsh, ua)):
            if ask is not None and FLOOR <= ask <= cap and (sh or 0) >= SH: hit = (side, ask, oth); break
        if not hit or sp is None or ref is None: continue
        dl = sp - ref
        if abs(dl) < thr or (dl > 0) != (hit[0] == "Up"): continue
        if hit[2] is not None and hit[1] + hit[2] - 1 > GAP + 1e-9: continue
        if lo is not None and (hi - lo) / ref * 1e4 >= CHOP: continue
        return hit[0], hit[1], sl
    return None

def main(d):
    meta, _ = load(d); book = book_rows(d); by = {start_of(s): s for s in meta}; mk = []
    for s, m in meta.items():
        st = start_of(s)
        if us(st) or s not in book or by.get(st - 300) not in book: continue
        te = twap_est(book[by[st - 300]], 5)
        if te: mk.append((s, m["winner"], te[0]))
    days = len({datetime.fromtimestamp(start_of(s)).date() for s, _, _ in mk})
    print(f"{len(mk)} non-US markets over {days} days; official line, floor {FLOOR}, chop {CHOP:g}, gap {GAP:g}; one order per market; per share after fee")
    print(f"{'window':>7}{'cap':>6}{'thr':>6} | {'per day':>8}{'lost':>6}{'loss%':>7}{'net':>8}{'per$1':>8}{'avg ask':>9}")
    for window in WINDOWS:
        for cap in CAPS:
            for thr in THRS:
                es = [(e[0] == w, e[1]) for s, w, ref in mk for e in [replay(book[s], ref, window, cap, thr)] if e]
                if not es: continue
                n = len(es); l = sum(not ok for ok, _ in es); cost = sum(a for _, a in es)
                net = sum(((1 - a) if ok else -a) - FEE * a * (1 - a) for ok, a in es)
                print(f"{window:>7}{cap:>6}{thr:>6g} | {n / days:>8.1f}{l:>6}{l / n:>7.1%}{net:>+8.2f}{net / cost:>+8.1%}{cost / n:>9.3f}")

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data")
