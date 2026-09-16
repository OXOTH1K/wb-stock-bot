#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/wb-stock-bot}"
APP_USER="${APP_USER:-wb-bot}"
SERVICE="${SERVICE:-wb-stock-bot}"
PYTHON="$APP_DIR/.venv/bin/python"

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "Run as root: sudo bash $APP_DIR/deploy.sh" >&2
  exit 1
fi

cd "$APP_DIR"

if [[ ! -x "$PYTHON" ]]; then
  echo "Python venv not found: $PYTHON" >&2
  exit 1
fi

if [[ -n "$(sudo -u "$APP_USER" git status --porcelain --untracked-files=no)" ]]; then
  echo "Tracked files have local changes. Commit/revert them before deploy." >&2
  sudo -u "$APP_USER" git status --short
  exit 1
fi

OLD_REV="$(sudo -u "$APP_USER" git rev-parse HEAD)"
echo "Current revision: $OLD_REV"

echo "Pulling origin/main..."
sudo -u "$APP_USER" git pull --ff-only origin main
NEW_REV="$(sudo -u "$APP_USER" git rev-parse HEAD)"
echo "Target revision:  $NEW_REV"

rollback() {
  echo "Deploy validation failed; rolling tracked files back to $OLD_REV" >&2
  sudo -u "$APP_USER" git reset --hard "$OLD_REV" >/dev/null
}

echo "Installing/updating Python dependencies..."
if ! sudo -u "$APP_USER" "$PYTHON" -m pip install -r "$APP_DIR/requirements.txt"; then
  rollback
  exit 1
fi

echo "Running tests..."
if ! sudo -u "$APP_USER" "$PYTHON" -m unittest discover -s tests -v; then
  rollback
  exit 1
fi

echo "Restarting $SERVICE..."
systemctl restart "$SERVICE"
systemctl --no-pager --full status "$SERVICE"

echo "Deploy complete: $OLD_REV -> $NEW_REV"
