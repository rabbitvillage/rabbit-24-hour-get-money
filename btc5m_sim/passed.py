"""python3 -m btc5m_sim.passed [YYYY-MM-DD]  — markets the live rules let go because the favourite was above the price cap."""
import sys
from btc5m_sim import sim
day = sys.argv[1] if len(sys.argv) > 1 else None
ps = sim.passed_summary(day)
print(f"[{sim.ASSET.upper()}] {day or '全部'}：" + (f"太貴放過 {ps[0]} 場，若照買 贏 {ps[1]} 輸 {ps[2]}，約 {ps[3]:+.2f}（15 股）" if ps else "沒有放過的紀錄"))
