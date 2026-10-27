#!/usr/bin/env bash
# Pull the latest commit on the host and restart the sim services at a safe moment (no order can be in flight).
# Usage (on the VPS, as root):  bash /opt/rabbit-24-hour-get-money/btc5m_sim/update_and_restart.sh [service ...]
# Default services: btc5m-sim btc5m-sim@sol btc5m-sim@eth btc5m-sim@btc15 (only the ones that exist are restarted).
#
# Safe moment: markets run on 5-minute boundaries; orders go out only in the last 60 s before a close and the sim must be
# back before the next market's book window opens (150 s before its close). So restart between close+5 s and close+140 s.
# The restart loses nothing that matters: today's realised P&L (the stops) is reloaded from live_results.csv.
set -euo pipefail
cd "$(dirname "$0")/.."
SERVICES=("$@"); [ ${#SERVICES[@]} -eq 0 ] && SERVICES=(btc5m-sim btc5m-sim@sol btc5m-sim@eth btc5m-sim@btc15)

if [ "${NO_PULL:-0}" != "1" ]; then
  # Host copies of files that were once edited in place (same content as the commits) would block the pull: restore them.
  for f in btc5m_sim/review.py; do
    if ! git diff --quiet -- "$f"; then echo "restoring host-edited $f (committed version is identical)"; git checkout -- "$f"; fi
  done
  for f in btc5m_sim/io_monitor.py btc5m_sim/btc5m-io.service btc5m_sim/btc5m-io.timer; do
    if [ -f "$f" ] && ! git ls-files --error-unmatch "$f" >/dev/null 2>&1; then
      if git cat-file -e "origin/$(git rev-parse --abbrev-ref HEAD):$f" 2>/dev/null; then echo "removing untracked $f (tracked upstream)"; rm -f "$f"; fi
    fi
  done
  git fetch --quiet origin
  git pull --ff-only
else
  echo "NO_PULL=1: restarting with the files already on disk"
fi
python3 -m py_compile btc5m_sim/live.py btc5m_sim/sim.py btc5m_sim/feeds.py

s=$(( $(date +%s) % 300 ))
if [ "$s" -gt 140 ] || [ "$s" -lt 5 ]; then
  wait=$(( (300 - s + 5) % 300 )); [ "$wait" -eq 0 ] && wait=5
  echo "waiting ${wait}s for the next 5-minute close (now +${s}s)"; sleep "$wait"
fi
for u in "${SERVICES[@]}"; do
  if systemctl cat "$u" >/dev/null 2>&1; then systemctl restart "$u"; echo "restarted $u"; fi
done
sleep 3
for u in "${SERVICES[@]}"; do
  systemctl is-active "$u" >/dev/null 2>&1 && echo "$u: active" || echo "$u: NOT ACTIVE"
done
journalctl -u btc5m-sim --since "-30s" --no-pager | grep -E "live: mode|ws feeds on" | tail -2
