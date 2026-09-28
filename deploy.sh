#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

APP_DIR="${APP_DIR:-/opt/wb-stock-bot}"
APP_USER="${APP_USER:-wb-bot}"
SERVICE="${SERVICE:-wb-stock-bot}"
PYTHON="$APP_DIR/.venv/bin/python"
SETUP_CADDY=0
CRM_DOMAIN="${CRM_DOMAIN:-}"
if [[ ${1:-} == --setup-caddy ]]; then
  SETUP_CADDY=1
  CRM_DOMAIN="${2:-$CRM_DOMAIN}"
elif [[ $# -gt 0 ]]; then
  echo 'Usage: sudo bash deploy.sh [--setup-caddy crm.example.ru]' >&2
  exit 1
fi
if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "Run as root: sudo bash $APP_DIR/deploy.sh" >&2
  exit 1
fi
if [[ ! "$SERVICE" =~ ^[a-zA-Z0-9_-]+$ ]]; then
  echo 'Invalid SERVICE name' >&2
  exit 1
fi
if [[ $SETUP_CADDY == 1 && -z "$CRM_DOMAIN" ]]; then
  echo 'Set CRM_DOMAIN or pass a DNS hostname after --setup-caddy. No changes made.' >&2
  exit 1
fi
cd "$APP_DIR"
if [[ ! -x "$PYTHON" || ! -f .env || -L .env ]]; then
  echo 'A Python venv and a regular .env file are required.' >&2
  exit 1
fi
if [[ "$(sudo -u "$APP_USER" git branch --show-current)" != main ]]; then
  echo 'Deploy only from the main branch.' >&2
  exit 1
fi
if [[ -n "$(sudo -u "$APP_USER" git status --porcelain --untracked-files=no)" ]]; then
  echo 'Tracked files have local changes. Resolve them before deploy.' >&2
  exit 1
fi
APP_GROUP="$(id -gn "$APP_USER")"
OLD_REV="$(sudo -u "$APP_USER" git rev-parse HEAD)"
STAGE="$(mktemp -d /tmp/wb-stock-deploy.XXXXXX)"
CADDY_ROOT=/etc/caddy/Caddyfile
CADDY_SITE=/etc/caddy/wb-stock-bot.caddy
DROPIN="/etc/systemd/system/$SERVICE.service.d/crm-security.conf"
APP_CHANGED=0
CONFIG_CHANGED=0
CADDY_NEW=0
CADDY_WAS_ACTIVE=0
SUCCESS=0

backup_file() {
  local path="$1" name="$2"
  if [[ -L "$path" ]]; then echo "Refusing symlink: $path" >&2; return 1; fi
  if [[ -f "$path" ]]; then cp -p "$path" "$STAGE/$name"; fi
}
restore_file() {
  local path="$1" name="$2"
  if [[ -f "$STAGE/$name" ]]; then cp -p "$STAGE/$name" "$path"; else rm -f "$path"; fi
}
cleanup() {
  local result=$?
  trap - EXIT
  set +e
  if [[ $SUCCESS != 1 ]]; then
    echo 'Deploy failed; restoring the previous code and configuration.' >&2
    if [[ $CONFIG_CHANGED == 1 ]]; then
      restore_file "$APP_DIR/.env" env.old
      restore_file "$CADDY_ROOT" caddy.old
      restore_file "$CADDY_SITE" site.old
      restore_file "$DROPIN" dropin.old
      systemctl daemon-reload || true
    fi
    if [[ $APP_CHANGED == 1 ]]; then
      sudo -u "$APP_USER" git reset --hard "$OLD_REV" >/dev/null || true
      systemctl restart "$SERVICE" || true
    fi
    if [[ $CADDY_NEW == 1 ]]; then
      systemctl disable --now caddy || true
    elif [[ $CONFIG_CHANGED == 1 && $CADDY_WAS_ACTIVE == 1 ]]; then
      systemctl reload caddy || true
    elif [[ $CONFIG_CHANGED == 1 ]]; then
      systemctl stop caddy || true
    fi
    echo 'Python package upgrades and an installed Caddy package are retained; application data is not rolled back.' >&2
  fi
  rm -rf "$STAGE"
  exit "$result"
}
trap cleanup EXIT

echo "Current revision: $OLD_REV"
sudo -u "$APP_USER" git pull --ff-only origin main
APP_CHANGED=1
NEW_REV="$(sudo -u "$APP_USER" git rev-parse HEAD)"
sudo -u "$APP_USER" "$PYTHON" -m pip install --upgrade --upgrade-strategy eager -r "$APP_DIR/requirements.txt"

if [[ $SETUP_CADDY == 1 ]]; then
  # Render first. Missing auth/domain or invalid paths must fail before installing or publishing anything.
  "$PYTHON" -m app.deployment prepare-caddy --domain "$CRM_DOMAIN" --app-dir "$APP_DIR" --stage "$STAGE"
else
  "$PYTHON" -m app.deployment validate --app-dir "$APP_DIR"
fi
sudo -u "$APP_USER" "$PYTHON" -m unittest discover -s tests -v
if command -v node >/dev/null; then sudo -u "$APP_USER" node tests/test_crm_ui.js; fi

if [[ $SETUP_CADDY == 1 ]]; then
  if ! command -v systemctl >/dev/null || ! command -v apt-get >/dev/null; then
    echo 'Automatic Caddy installation supports Debian/Ubuntu with systemd.' >&2
    exit 1
  fi
  systemctl is-active --quiet caddy && CADDY_WAS_ACTIVE=1
  backup_file "$APP_DIR/.env" env.old
  backup_file "$CADDY_ROOT" caddy.old
  backup_file "$CADDY_SITE" site.old
  backup_file "$DROPIN" dropin.old
  if ! command -v caddy >/dev/null; then
    # Do not replace another web server or change its firewall rules.
    if ! command -v ss >/dev/null; then
      echo 'Install iproute2 (ss) to check whether ports 80/443 are free.' >&2
      exit 1
    fi
    LISTENERS="$(ss -ltnH '( sport = :80 or sport = :443 )')"
    if [[ -n "$LISTENERS" ]]; then
      echo 'Ports 80/443 are already in use. Configure the existing web server first.' >&2
      exit 1
    fi
    CADDY_NEW=1
    apt-get update
    apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg
    curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
      https://dl.cloudsmith.io/public/caddy/stable/gpg.key -o "$STAGE/caddy.key"
    gpg --batch --yes --dearmor -o "$STAGE/caddy.gpg" "$STAGE/caddy.key"
    install -m 0644 "$STAGE/caddy.gpg" /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
      https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt -o "$STAGE/caddy.list"
    install -m 0644 "$STAGE/caddy.list" /etc/apt/sources.list.d/caddy-stable.list
    apt-get update
    apt-get install -y caddy
    systemctl stop caddy
  fi
  install -d -m 0755 /etc/caddy "$(dirname "$DROPIN")"
  CONFIG_CHANGED=1
  install -m 0644 "$STAGE/wb-stock-bot.caddy" "$CADDY_SITE"
  if [[ $CADDY_NEW == 1 || ! -f "$CADDY_ROOT" ]]; then
    printf 'import /etc/caddy/wb-stock-bot.caddy\n' > "$CADDY_ROOT"
  elif ! grep -Fxq 'import /etc/caddy/wb-stock-bot.caddy' "$CADDY_ROOT"; then
    printf '\nimport /etc/caddy/wb-stock-bot.caddy\n' >> "$CADDY_ROOT"
  fi
  chmod 0644 "$CADDY_ROOT"
  caddy validate --config "$CADDY_ROOT" --adapter caddyfile
  install -o root -g "$APP_GROUP" -m 0640 "$STAGE/.env" "$APP_DIR/.env"
  install -d -o "$APP_USER" -g "$APP_GROUP" -m 0700 "$APP_DIR/data"
  install -m 0644 "$STAGE/crm-security.conf" "$DROPIN"
  systemctl daemon-reload
fi

echo "Restarting $SERVICE..."
systemctl restart "$SERVICE"
HEALTH_OK=0
for attempt in {1..60}; do
  if "$PYTHON" -m app.deployment healthcheck --app-dir "$APP_DIR" >"$STAGE/health.log" 2>&1; then
    HEALTH_OK=1
    break
  fi
  sleep 2
done
if [[ $HEALTH_OK != 1 ]]; then
  cat "$STAGE/health.log" >&2
  exit 1
fi
systemctl is-active --quiet "$SERVICE"
if [[ $SETUP_CADDY == 1 ]]; then
  systemctl enable caddy
  if systemctl is-active --quiet caddy; then systemctl reload caddy; else systemctl start caddy; fi
  systemctl is-active --quiet caddy
  echo "Caddy configured for https://$CRM_DOMAIN. DNS must point here and TCP 80/443 must reach this server."
fi
systemctl --no-pager --full status "$SERVICE"
SUCCESS=1
echo "Deploy complete: $OLD_REV -> $NEW_REV"
