#!/usr/bin/env bash
# TimeTrax on Ubuntu 24.04 (EC2) — Gunicorn + Caddy (auto-TLS) + systemd
# Run as: sudo DOMAIN=tt.example.com bash deploy_timetrax.sh
# For IP-only testing (no TLS): sudo DOMAIN=198.51.100.10 bash deploy_timetrax.sh
#   (198.51.100.0/24 is the RFC 5737 documentation range — substitute your own host/IP.)
set -euo pipefail

DOMAIN="${DOMAIN:?Set DOMAIN=your.hostname (or the public IP for http-only testing)}"
APP_DIR=/opt/timetrax
APP_USER=timetrax
REPO=https://github.com/veteranop/timetrax.git

echo "==> System packages"
apt-get update -qq
apt-get install -y -qq git python3-venv python3-pip debian-keyring debian-archive-keyring apt-transport-https curl

echo "==> Caddy (official repo)"
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq && apt-get install -y -qq caddy
fi

echo "==> App user + checkout"
id -u $APP_USER >/dev/null 2>&1 || useradd --system --home $APP_DIR --shell /usr/sbin/nologin $APP_USER
if [ ! -d $APP_DIR/.git ]; then
  git clone -q $REPO $APP_DIR
else
  git -C $APP_DIR pull -q
fi

echo "==> Python env"
python3 -m venv $APP_DIR/.venv
$APP_DIR/.venv/bin/pip install -q --upgrade pip
$APP_DIR/.venv/bin/pip install -q -r $APP_DIR/requirements.txt gunicorn

echo "==> .env (fill in the secrets after this script finishes)"
if [ ! -f $APP_DIR/.env ]; then
  SECRET=$($APP_DIR/.venv/bin/python -c "from django.core.management.utils import get_random_secret_key as k; print(k())")
  APIKEY=$(openssl rand -hex 32)
  cat > $APP_DIR/.env <<ENV
DJANGO_SECRET_KEY=$SECRET
DJANGO_DEBUG=0
ALLOWED_HOSTS=$DOMAIN
STRIPE_SECRET_KEY=sk_test_REPLACE_ME
INVOICE_DAYS_UNTIL_DUE=14
TIMETRAX_API_KEY=$APIKEY
N8N_TICKET_WEBHOOK=http://127.0.0.1:5678/webhook/timetrax-triage
ENV
fi
chmod 600 $APP_DIR/.env

echo "==> Migrate + static"
cd $APP_DIR
export DJANGO_SETTINGS_MODULE=config.settings_prod
.venv/bin/python manage.py migrate --noinput
.venv/bin/python manage.py collectstatic --noinput
chown -R $APP_USER:$APP_USER $APP_DIR

echo "==> systemd: gunicorn"
cat > /etc/systemd/system/timetrax.service <<UNIT
[Unit]
Description=TimeTrax (gunicorn)
After=network.target

[Service]
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
Environment=DJANGO_SETTINGS_MODULE=config.settings_prod
ExecStart=$APP_DIR/.venv/bin/gunicorn config.wsgi:application --bind 127.0.0.1:8000 --workers 2 --timeout 60
Restart=always
RestartSec=3
# hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$APP_DIR
ProtectHome=true

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now timetrax

echo "==> Caddy reverse proxy"
if [[ "$DOMAIN" =~ ^[0-9.]+$ ]]; then SITE="http://$DOMAIN"; else SITE="$DOMAIN"; fi
cat > /etc/caddy/Caddyfile <<CADDY
$SITE {
    encode gzip
    handle_path /static/* {
        root * $APP_DIR/staticfiles
        file_server
    }
    reverse_proxy 127.0.0.1:8000
    header {
        Strict-Transport-Security "max-age=31536000"
        X-Content-Type-Options nosniff
        X-Frame-Options DENY
        Referrer-Policy strict-origin-when-cross-origin
    }
}
CADDY
usermod -aG $APP_USER caddy || true
chmod 750 $APP_DIR; chmod -R g+rX $APP_DIR/staticfiles
systemctl reload caddy || systemctl restart caddy

echo
echo "Done. Next:"
echo "  1. sudo nano $APP_DIR/.env   -> set STRIPE_SECRET_KEY (test key first)"
echo "  2. sudo systemctl restart timetrax"
echo "  3. cd $APP_DIR && sudo -u $APP_USER DJANGO_SETTINGS_MODULE=config.settings_prod .venv/bin/python manage.py createsuperuser"
echo "  4. Open http(s)://$DOMAIN/"
echo "  Logs: journalctl -u timetrax -f   |   journalctl -u caddy -f"
