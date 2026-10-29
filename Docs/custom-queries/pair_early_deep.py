"""Deep, early first legs only (fish 2026-10-30 「波動從 50 出發」): rebound_vs_random.py showed that dips in the first ~20 s rebound
at or above the random benchmark while later dips trend. Replays leg 1 = first side whose ask <= L1 within FIRST s, leg 2 = the
other side once the pair nets > MARGIN after both taker fees, else held to settlement. Uses pair_dynamic.py (same folder).
Usage (in /opt/pm-pair/data): python3 -I ../pair_early_deep.py
"""
import importlib.util, os
spec = importlib.util.spec_from_file_location("pd", os.path.join(os.path.dirname(os.path.abspath(__file__)), "pair_dynamic.py"))
pd = importlib.util.module_from_spec(spec); spec.loader.exec_module(pd)
mks = pd.load(); print(f"{len(mks)} complete markets")
print(f"{'first leg':<14}{'window':>7}{'margin':>8} | {'n':>4} {'pair':>5} {'lost':>5} | {'net/sh':>7} {'per $1':>7}")
for l1 in (0.40, 0.35, 0.30, 0.25):
    for first in (10, 20, 60):
        for margin in (0.001, 0.02, 0.04):
            pd.MARGIN = margin
            n, c, pnl, r = pd.summarize(mks, mode="abs", l1=l1, drop=0, look=0, first=first, stop=None, cut_t=None)
            print(f"ask <= {l1:<7.2f}{first:>6}s{margin:>8.3f} | {n:>4} {c['pair']:>5} {c['held_lost']:>5} | {pnl:>+7.2f} {r:>+7.1%}")
