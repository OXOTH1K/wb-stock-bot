"""Validate/render deployment files without sourcing .env as shell code."""
from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

from dotenv import dotenv_values, set_key

from .crm_security import validate_crm_settings, validate_domain


def crm_settings(path: Path):
    values = dotenv_values(path, interpolate=False)
    settings = SimpleNamespace(
        crm_enabled=str(values.get('CRM_ENABLED', '1')).lower() not in {'0', 'false', 'no', 'off'},
        crm_user=(values.get('CRM_USER') or '').strip(), crm_password=(values.get('CRM_PASSWORD') or '').strip(),
        crm_host=values.get('CRM_HOST') or '127.0.0.1', crm_port=int(values.get('CRM_PORT') or 8080),
        crm_public_url=values.get('CRM_PUBLIC_URL') or '',
    )
    validate_crm_settings(settings)
    return settings


def caddy_config(domain: str, port: int) -> str:
    domain = validate_domain(domain)
    if not 1024 <= port <= 65535:
        raise ValueError('Use an unprivileged CRM_PORT between 1024 and 65535')
    return f'''# Managed by wb-stock-bot deploy.sh
{domain} {{
    header {{
        Strict-Transport-Security "max-age=31536000"
        -Server
    }}
    request_body {{
        max_size 16KB
    }}
    reverse_proxy 127.0.0.1:{port} {{
        header_up Host {domain}
        header_up -Forwarded
        header_up X-Forwarded-Proto https
        header_up X-Forwarded-Host {domain}
        header_up X-CRM-Client-IP {{remote_host}}
        transport http {{
            dial_timeout 5s
            response_header_timeout 120s
        }}
    }}
}}
'''


def prepare(app_dir: Path, domain: str, stage: Path):
    domain = validate_domain(domain)
    env_path = app_dir / '.env'
    candidate = stage / '.env'
    candidate.write_text(env_path.read_text())
    candidate.chmod(0o600)
    for name, value in {'CRM_HOST': '127.0.0.1', 'CRM_PUBLIC_URL': f'https://{domain}',
                        'CRM_ENABLED': '1', 'CRM_ALLOWED_NETWORKS': '127.0.0.1/32,::1/128'}.items():
        set_key(candidate, name, value)
    settings = crm_settings(candidate)
    values = dotenv_values(candidate, interpolate=False)
    database = Path(values.get('DB_PATH') or './data/stocks.sqlite3')
    database = (app_dir / database).resolve()
    if not database.is_relative_to((app_dir / 'data').resolve()):
        raise ValueError('For public deployment DB_PATH must be inside APP_DIR/data')
    (stage / 'wb-stock-bot.caddy').write_text(caddy_config(domain, settings.crm_port))
    # Paths are inserted into a systemd directive, so reject syntax metacharacters.
    if any(c not in '/abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.' for c in str(app_dir)):
        raise ValueError('APP_DIR contains characters unsupported in the systemd drop-in')
    (stage / 'crm-security.conf').write_text(f'''[Service]
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths={app_dir}/data
ReadOnlyPaths={app_dir}
UMask=0077
RestrictSUIDSGID=true
LockPersonality=true
CapabilityBoundingSet=
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
''')


def healthcheck(app_dir: Path):
    settings = crm_settings(app_dir / '.env')
    if not settings.crm_enabled:
        return
    host = settings.crm_host
    if host not in {'127.0.0.1', '::1', 'localhost', '0.0.0.0'}:
        raise ValueError('Health check requires a localhost-accessible CRM listener')
    host = '[::1]' if host == '::1' else '127.0.0.1'
    url = f'http://{host}:{settings.crm_port}/healthz'
    headers = {}
    if settings.crm_public_url:
        headers = {'Host': settings.crm_public_url.removeprefix('https://'), 'X-Forwarded-Proto': 'https'}
    opener = build_opener(ProxyHandler({}))
    try:
        opener.open(Request(url, headers=headers), timeout=3).close()
    except HTTPError as exc:
        if exc.code != 401:
            raise RuntimeError(f'Unauthenticated health check returned {exc.code}, expected 401') from None
    else:
        raise RuntimeError('SECURITY: CRM health endpoint was reachable without authentication')
    credentials = f'{settings.crm_user}:{settings.crm_password}'.encode()
    headers['Authorization'] = 'Basic ' + base64.b64encode(credentials).decode()
    with opener.open(Request(url, headers=headers), timeout=5) as response:
        if response.status != 200:
            raise RuntimeError('Authenticated CRM health check failed')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare-caddy', 'validate', 'healthcheck'])
    parser.add_argument('--app-dir', type=Path, default=Path.cwd())
    parser.add_argument('--domain', default=os.getenv('CRM_DOMAIN', ''))
    parser.add_argument('--stage', type=Path)
    args = parser.parse_args()
    try:
        if args.action == 'prepare-caddy':
            if args.stage is None:
                raise ValueError('--stage is required')
            prepare(args.app_dir.resolve(), args.domain, args.stage)
        elif args.action == 'validate':
            crm_settings(args.app_dir / '.env')
        else:
            healthcheck(args.app_dir)
    except Exception as exc:
        # Do not print credentials or HTTP headers in deployment output.
        raise SystemExit(f'Deployment check failed: {exc}') from None


if __name__ == '__main__':
    main()
