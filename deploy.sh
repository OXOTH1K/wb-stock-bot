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
if [[ ! "$APP_USER" =~ ^[a-zA-Z_][a-zA-Z0-9_-]*$ || ! "$APP_DIR" =~ ^/[a-zA-Z0-9_./-]+$ || "$APP_DIR" == *..* ]]; then
  echo 'APP_USER or APP_DIR contains unsupported characters.' >&2
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
CADDY_CUSTOM=/usr/local/lib/wb-stock-bot/caddy
CADDY_DROPIN=/etc/systemd/system/caddy.service.d/wb-crm-selectel.conf
CADDY_ENV=/etc/caddy/wb-crm-selectel.env
CADDY_DNS_CHANGED=0
UPDATE_HELPER=/usr/local/lib/wb-stock-bot/crm-update-deploy.sh
UPDATE_UNIT=/etc/systemd/system/wb-stock-bot-crm-update.service
UPDATE_PATH_UNIT=/etc/systemd/system/wb-stock-bot-crm-update.path
UPDATE_SUDOERS=/etc/sudoers.d/wb-stock-bot-crm-update
DROPIN="/etc/systemd/system/$SERVICE.service.d/crm-security.conf"
APP_CHANGED=0
CONFIG_CHANGED=0
UPDATE_CONFIG_CHANGED=0
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
      if [[ $CADDY_DNS_CHANGED == 1 ]]; then
        if [[ -f "$STAGE/binary.old" ]]; then
          cp -p "$STAGE/binary.old" "$CADDY_CUSTOM.rollback"
          mv -f "$CADDY_CUSTOM.rollback" "$CADDY_CUSTOM"
        else
          rm -f "$CADDY_CUSTOM"
        fi
        restore_file "$CADDY_DROPIN" caddy-dropin.old
        restore_file "$CADDY_ENV" caddy-env.old
      fi
      systemctl daemon-reload || true
    fi
    if [[ $APP_CHANGED == 1 ]]; then
      sudo -u "$APP_USER" git reset --hard "$OLD_REV" >/dev/null || true
      systemctl restart "$SERVICE" || true
    fi
    if [[ $CADDY_NEW == 1 ]]; then
      systemctl disable --now caddy || true
    elif [[ $CONFIG_CHANGED == 1 && $CADDY_WAS_ACTIVE == 1 ]]; then
      if [[ $CADDY_DNS_CHANGED == 1 ]]; then systemctl restart caddy || true; else systemctl reload caddy || true; fi
    elif [[ $CONFIG_CHANGED == 1 ]]; then
      systemctl stop caddy || true
    fi
    if [[ $UPDATE_CONFIG_CHANGED == 1 ]]; then
      systemctl disable --now wb-stock-bot-crm-update.path >/dev/null 2>&1 || true
      restore_file "$UPDATE_HELPER" update-helper.old
      restore_file "$UPDATE_UNIT" update-unit.old
      restore_file "$UPDATE_PATH_UNIT" update-path.old
      restore_file "$UPDATE_SUDOERS" update-sudoers.old
      systemctl daemon-reload || true
      if [[ -f "$STAGE/update-path.old" ]]; then
        systemctl enable --now wb-stock-bot-crm-update.path || true
      fi
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
  CRM_HTTPS_PORT="$(cat "$STAGE/https-port")"
  CRM_PUBLIC_URL="$(cat "$STAGE/public-url")"
  CRM_DNS_PROVIDER="$(cat "$STAGE/dns-provider")"
  if [[ $CRM_DNS_PROVIDER == selectel ]]; then
    "$PYTHON" -m app.deployment prepare-selectel --stage "$STAGE"
  fi
  "$PYTHON" -m app.deployment caddy-root --dns-provider "$CRM_DNS_PROVIDER" --stage "$STAGE"
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
      echo "Install iproute2 (ss) to check whether ports 80/$CRM_HTTPS_PORT are free." >&2
      exit 1
    fi
    PORT_FILTER="( sport = :80 or sport = :$CRM_HTTPS_PORT )"
    if [[ $CRM_DNS_PROVIDER == selectel ]]; then PORT_FILTER="( sport = :$CRM_HTTPS_PORT )"; fi
    LISTENERS="$(ss -ltnH "$PORT_FILTER")"
    if [[ -n "$LISTENERS" ]]; then
      echo "Required listener is already in use: $PORT_FILTER. Configure the existing web server first." >&2
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
  CADDY_BIN="$(command -v caddy)"
  if [[ $CRM_DNS_PROVIDER == selectel ]]; then
    backup_file "$CADDY_CUSTOM" binary.old
    backup_file "$CADDY_DROPIN" caddy-dropin.old
    backup_file "$CADDY_ENV" caddy-env.old
    if [[ ${CRM_REBUILD_CADDY:-0} != 1 && -x "$CADDY_CUSTOM" ]] && "$CADDY_CUSTOM" list-modules | grep -Fx 'dns.providers.selectel' >/dev/null; then
      cp "$CADDY_CUSTOM" "$STAGE/caddy"
    else
      bash "$APP_DIR/scripts/build-caddy-selectel.sh" "$STAGE" "$PYTHON"
    fi
    CADDY_BIN="$STAGE/caddy"
  elif [[ -f "$CADDY_DROPIN" ]]; then
    CADDY_BIN="$CADDY_CUSTOM"
  fi
  install -d -m 0755 /etc/caddy "$(dirname "$DROPIN")"
  CONFIG_CHANGED=1
  install -m 0644 "$STAGE/wb-stock-bot.caddy" "$CADDY_SITE"
  install -m 0644 "$STAGE/Caddyfile" "$CADDY_ROOT"
  "$PYTHON" -m app.deployment validate-caddy --caddy-bin "$CADDY_BIN" --dns-provider "$CRM_DNS_PROVIDER"
  if [[ $CRM_DNS_PROVIDER == selectel ]]; then
    CADDY_DNS_CHANGED=1
    install -d -m 0755 "$(dirname "$CADDY_CUSTOM")" "$(dirname "$CADDY_DROPIN")"
    # Replace via rename: never truncate a currently running executable.
    install -o root -g root -m 0755 "$STAGE/caddy" "$CADDY_CUSTOM.new"
    mv -f "$CADDY_CUSTOM.new" "$CADDY_CUSTOM"
    install -o root -g root -m 0600 "$STAGE/selectel.env" "$CADDY_ENV"
    install -m 0644 "$STAGE/caddy-selectel.conf" "$CADDY_DROPIN"
  fi
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

# Keep the privileged runner outside the app tree. The app requests an update
# by creating a marker in data; a root-owned systemd path unit starts this fixed service.
if [[ ! -x /usr/bin/systemctl ]]; then
  echo '/usr/bin/systemctl is required to configure CRM updates.' >&2
  exit 1
fi
backup_file "$UPDATE_HELPER" update-helper.old
backup_file "$UPDATE_UNIT" update-unit.old
backup_file "$UPDATE_PATH_UNIT" update-path.old
backup_file "$UPDATE_SUDOERS" update-sudoers.old
UPDATE_CONFIG_CHANGED=1
install -d -o root -g root -m 0755 "$(dirname "$UPDATE_HELPER")"
if [[ -L "$APP_DIR/data" ]]; then
  echo 'Refusing symlinked data directory for CRM update requests.' >&2
  exit 1
fi
install -d -o "$APP_USER" -g "$APP_GROUP" -m 0700 "$APP_DIR/data"
install -o root -g root -m 0755 "$APP_DIR/deploy.sh" "$STAGE/crm-update-deploy.sh"
mv -f "$STAGE/crm-update-deploy.sh" "$UPDATE_HELPER"
cat > "$STAGE/crm-update.service" <<EOF
[Unit]
Description=Update wb-stock-bot from an approved GitHub release
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=root
Environment=APP_DIR=$APP_DIR
Environment=APP_USER=$APP_USER
Environment=SERVICE=$SERVICE
ExecStartPre=/usr/bin/rm -f -- $APP_DIR/data/crm-update.request
ExecStart=$UPDATE_HELPER
TimeoutStartSec=30min
PrivateTmp=true
EOF
install -o root -g root -m 0644 "$STAGE/crm-update.service" "$UPDATE_UNIT"
cat > "$STAGE/crm-update.path" <<EOF
[Unit]
Description=Watch for an authenticated CRM update request

[Path]
PathExists=$APP_DIR/data/crm-update.request
Unit=wb-stock-bot-crm-update.service

[Install]
WantedBy=multi-user.target
EOF
install -o root -g root -m 0644 "$STAGE/crm-update.path" "$UPDATE_PATH_UNIT"
rm -f "$UPDATE_SUDOERS"
systemctl daemon-reload
systemctl enable --now wb-stock-bot-crm-update.path
if [[ $SETUP_CADDY == 1 ]]; then
  systemctl enable caddy
  if [[ $CADDY_DNS_CHANGED == 1 ]]; then
    systemctl restart caddy
  elif systemctl is-active --quiet caddy; then systemctl reload caddy; else systemctl start caddy; fi
  systemctl is-active --quiet caddy
  if [[ $CRM_DNS_PROVIDER == selectel ]]; then
    echo "Caddy configured for $CRM_PUBLIC_URL. Forward TCP $CRM_HTTPS_PORT; certificates use Selectel DNS, not inbound 80/443."
  else
    echo "Caddy configured for $CRM_PUBLIC_URL. DNS must point here; TCP 80 is required for Let's Encrypt and TCP $CRM_HTTPS_PORT for CRM."
  fi
  echo 'Certificate issuance is asynchronous; check journalctl -u caddy and HTTPS from outside.'
fi
systemctl --no-pager --full status "$SERVICE"
SUCCESS=1
echo "Deploy complete: $OLD_REV -> $NEW_REV"
