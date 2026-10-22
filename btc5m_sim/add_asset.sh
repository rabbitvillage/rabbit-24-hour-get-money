#!/usr/bin/env bash
# Add another 5-minute market (eth, sol, xrp) as its own systemd instance next to the BTC service.
# Usage (on the VPS, as root, inside the cloned repo):  bash btc5m_sim/add_asset.sh eth
# It reuses btc5m_sim/.env (exec mode, rules, Telegram), .live.env (wallet creds) and .dash.env (dashboard token);
# only ASSET and DATA_DIR differ, written to .asset-<asset>.env which the template units load last.
set -euo pipefail
A=${1:?asset (eth|sol|xrp)}
case "$A" in eth|sol|xrp) ;; *) echo "unknown asset $A"; exit 1;; esac
cd "$(dirname "$0")/.."
REPO=$(pwd)
mkdir -p "$REPO/btc5m_sim/data-$A"
printf 'ASSET=%s\nDATA_DIR=%s/btc5m_sim/data-%s\n' "$A" "$REPO" "$A" > "$REPO/btc5m_sim/.asset-$A.env"
chmod 600 "$REPO/btc5m_sim/.asset-$A.env"
for u in btc5m-sim@.service btc5m-push@.service btc5m-push@.timer; do
  sed "s|/opt/rabbit-24-hour-get-money|$REPO|g" "btc5m_sim/$u" > "/etc/systemd/system/$u"
done
systemctl daemon-reload
systemctl enable --now "btc5m-sim@$A" "btc5m-push@$A.timer"
echo "started btc5m-sim@$A. Logs: journalctl -u btc5m-sim@$A -f   Data: $REPO/btc5m_sim/data-$A/"
