#!/usr/bin/env bash
# One-command install of the phase-0 simulator as a systemd service.
# Usage (on the VPS, as root, inside the cloned repo):  bash btc5m_sim/deploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."
REPO=$(pwd)
command -v python3 >/dev/null || { apt-get update && apt-get install -y python3; }
python3 -c 'import sys; assert sys.version_info >= (3, 9)' || { echo "need python >= 3.9"; exit 1; }
ENV=btc5m_sim/.env
if [ ! -f "$ENV" ]; then
  cp btc5m_sim/.env.example "$ENV"
  read -rsp "Telegram bot token (input hidden, Enter to skip): " TOK; echo
  [ -n "$TOK" ] && sed -i "s|^TG_TOKEN=.*|TG_TOKEN=$TOK|" "$ENV"
fi
chmod 600 "$ENV"
mkdir -p "$REPO/btc5m_sim/data"
cat > /etc/systemd/system/btc5m-sim.service <<UNIT
[Unit]
Description=BTC 5m phase-0 simulator (no orders)
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$REPO
EnvironmentFile=$REPO/$ENV
EnvironmentFile=-$REPO/btc5m_sim/.live.env
EnvironmentFile=-$REPO/btc5m_sim/.dash.env
Environment=DATA_DIR=$REPO/btc5m_sim/data
ExecStart=/usr/bin/env python3 -m btc5m_sim.sim
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now btc5m-sim
echo "Running. Logs: journalctl -u btc5m-sim -f   Data: $REPO/btc5m_sim/data/sim_trades.csv"
