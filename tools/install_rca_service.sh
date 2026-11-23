#!/usr/bin/env bash
# Install and start the RCA report service (systemd) from wherever this package was unpacked.
#   bash tools/install_rca_service.sh [env-file]      (default env file: ~/.zabbix_env)
set -eu
DIR=$(cd "$(dirname "$0")/.." && pwd)
ENV_FILE=$(readlink -f "${1:-$HOME/.zabbix_env}")
[ -f "$ENV_FILE" ] || { echo "No settings file $ENV_FILE (see INSTALL step 3)"; exit 1; }
grep -q ZABBIX_TOKEN "$ENV_FILE" || { echo "$ENV_FILE has no ZABBIX_TOKEN"; exit 1; }
cat > /etc/systemd/system/rca-server.service <<UNIT
[Unit]
Description=MDP RCA report server for Zabbix
After=network-online.target docker.service

[Service]
WorkingDirectory=$DIR
ExecStart=/bin/bash -c 'source "$ENV_FILE" && exec python3 tools/rca_server.py'
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable rca-server >/dev/null 2>&1
systemctl restart rca-server
sleep 5
if python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:${RCA_PORT:-8081}/health', timeout=5).read() == b'ok' else 1)" 2>/dev/null; then
    echo "RCA service running from $DIR on port ${RCA_PORT:-8081}"
else
    echo "RCA service did not answer. Last log lines:"; journalctl -u rca-server -n 20 --no-pager
    exit 1
fi
