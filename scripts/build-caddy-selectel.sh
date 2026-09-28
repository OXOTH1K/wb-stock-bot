#!/usr/bin/env bash
# Build in a private staging directory; do not modify the installed Caddy.
set -Eeuo pipefail
STAGE="$1"
PYTHON="$2"
CADDY_VERSION=v2.11.4
SELECTEL_MODULE=github.com/caddy-dns/selectel@bbe6cd0ca1af6a654bae9b958d832b40cd627a13
case "$(uname -m)" in
  x86_64) GO_ARCH=amd64 ;;
  aarch64|arm64) GO_ARCH=arm64 ;;
  *) echo 'Automatic Selectel build supports Linux amd64/arm64.' >&2; exit 1 ;;
esac
curl --fail --silent --show-error --location --retry 3 --max-time 120 --proto '=https' \
  'https://go.dev/dl/?mode=json' -o "$STAGE/go-releases.json"
"$PYTHON" - "$STAGE/go-releases.json" "$GO_ARCH" > "$STAGE/go-download" <<'PY'
import json, re, sys
releases = json.load(open(sys.argv[1]))
release = next(r for r in releases if r['stable'])
item = next(f for f in release['files'] if f['os'] == 'linux' and f['arch'] == sys.argv[2] and f['kind'] == 'archive')
if not re.fullmatch(r'go[0-9.]+\.linux-(amd64|arm64)\.tar\.gz', item['filename']):
    raise SystemExit('Unexpected Go archive name')
if not re.fullmatch(r'[0-9a-f]{64}', item['sha256']):
    raise SystemExit('Unexpected Go checksum')
print(item['filename'])
print(item['sha256'])
PY
GO_ARCHIVE="$(sed -n '1p' "$STAGE/go-download")"
GO_SHA="$(sed -n '2p' "$STAGE/go-download")"
curl --fail --silent --show-error --location --retry 3 --max-time 600 --proto '=https' \
  "https://go.dev/dl/$GO_ARCHIVE" -o "$STAGE/go.tar.gz"
printf '%s  %s\n' "$GO_SHA" "$STAGE/go.tar.gz" | sha256sum --check --status
tar -xzf "$STAGE/go.tar.gz" -C "$STAGE"
export PATH="$STAGE/go/bin:$PATH"
export GOPATH="$STAGE/gopath" GOCACHE="$STAGE/gocache"
export GOPROXY=https://proxy.golang.org GOSUMDB=sum.golang.org CGO_ENABLED=0
cd "$STAGE"
go run github.com/caddyserver/xcaddy/cmd/xcaddy@v0.4.5 build "$CADDY_VERSION" \
  --with "$SELECTEL_MODULE" --output "$STAGE/caddy"
"$STAGE/caddy" list-modules | grep -Fx 'dns.providers.selectel'
