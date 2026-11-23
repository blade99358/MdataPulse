#!/usr/bin/env bash
# WAHA Core (free WhatsApp HTTP API) in Docker, for Zabbix NOC alarms. Run as root on the Zabbix LXC.
# It listens on 127.0.0.1:3000 only: just the local relay can use it.
# Safe to run again: keeps the existing keys and the linked WhatsApp session.
set -eu
DIR=/opt/waha
mkdir -p "$DIR/sessions" "$DIR/media"
cd "$DIR"
if [ ! -f waha.env ]; then
  KEY=$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')
  PASS=$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')
  cat > waha.env <<EOF
WAHA_API_KEY=$KEY
WAHA_DASHBOARD_USERNAME=admin
WAHA_DASHBOARD_PASSWORD=$PASS
WHATSAPP_SWAGGER_USERNAME=admin
WHATSAPP_SWAGGER_PASSWORD=$PASS
WHATSAPP_DEFAULT_ENGINE=GOWS
WHATSAPP_START_SESSION=default
WHATSAPP_RESTART_ALL_SESSIONS=True
WAHA_PRINT_QR=True
TZ=UTC
EOF
  chmod 600 waha.env
fi
cat > docker-compose.yml <<'EOF'
services:
  waha:
    image: devlikeapro/waha:latest
    container_name: waha
    restart: unless-stopped
    ports:
      - "127.0.0.1:3000:3000"
    env_file: waha.env
    volumes:
      - ./sessions:/app/.sessions
      - ./media:/app/.media
    logging:
      options: {max-size: "10m", max-file: "3"}
EOF
if docker compose version >/dev/null 2>&1; then docker compose up -d; else docker-compose up -d; fi
echo
echo "WAHA is starting on http://127.0.0.1:3000 (first start takes about a minute)."
echo "Next: link the WhatsApp number by scanning the QR code shown by:   docker logs -f waha"
