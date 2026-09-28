"""Validate/render deployment files without sourcing .env as shell code."""
from __future__ import annotations

import argparse
import base64
import os
import re
import subprocess
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


def https_authority(domain: str, https_port: int) -> str:
    domain = validate_domain(domain)
    if https_port != 443 and not 1024 <= https_port <= 65535:
        raise ValueError('CRM_HTTPS_PORT must be 443 or between 1024 and 65535')
    return domain if https_port == 443 else f'{domain}:{https_port}'


def caddy_config(domain: str, port: int, https_port: int = 443, dns_provider: str = 'http') -> str:
    authority = https_authority(domain, https_port)
    if port == https_port:
        raise ValueError("CRM_PORT and CRM_HTTPS_PORT must differ")
    if not 1024 <= port <= 65535:
        raise ValueError('Use an unprivileged CRM_PORT between 1024 and 65535')
    if dns_provider not in {'http', 'selectel'}:
        raise ValueError('CRM_DNS_PROVIDER must be http or selectel')
    challenge = 'disable_tlsalpn_challenge'
    if dns_provider == 'selectel':
        challenge = """disable_http_challenge
            disable_tlsalpn_challenge
            dns selectel {
                user {env.SELECTEL_USER}
                password {env.SELECTEL_PASSWORD}
                account_id {env.SELECTEL_ACCOUNT_ID}
                project_name {env.SELECTEL_PROJECT_NAME}
            }"""
    return f'''# Managed by wb-stock-bot deploy.sh
https://{authority} {{
    tls {{
        issuer acme {{
            dir https://acme-v02.api.letsencrypt.org/directory
            {challenge}
        }}
    }}
    header {{
        Strict-Transport-Security "max-age=31536000"
        -Server
    }}
    request_body {{
        max_size 16KB
    }}
    reverse_proxy 127.0.0.1:{port} {{
        header_up Host {authority}
        header_up -Forwarded
        header_up X-Forwarded-Proto https
        header_up X-Forwarded-Host {authority}
        header_up X-CRM-Client-IP {{remote_host}}
        transport http {{
            dial_timeout 5s
            response_header_timeout 120s
        }}
    }}
}}
'''


def prepare(app_dir: Path, domain: str, stage: Path, https_port: int | None = None, dns_provider: str | None = None):
    domain = validate_domain(domain)
    env_path = app_dir / '.env'
    local_values = dotenv_values(env_path, interpolate=False)
    if https_port is None:
        https_port = int(local_values.get('CRM_HTTPS_PORT') or 443)
    dns_provider = dns_provider or local_values.get('CRM_DNS_PROVIDER') or 'http'
    authority = https_authority(domain, https_port)
    candidate = stage / '.env'
    candidate.write_text(env_path.read_text())
    candidate.chmod(0o600)
    for name, value in {'CRM_HOST': '127.0.0.1', 'CRM_PUBLIC_URL': f'https://{authority}',
                        'CRM_HTTPS_PORT': str(https_port), 'CRM_DNS_PROVIDER': dns_provider,
                        'CRM_ENABLED': '1', 'CRM_ALLOWED_NETWORKS': '127.0.0.1/32,::1/128'}.items():
        set_key(candidate, name, value)
    settings = crm_settings(candidate)
    values = dotenv_values(candidate, interpolate=False)
    database = Path(values.get('DB_PATH') or './data/stocks.sqlite3')
    database = (app_dir / database).resolve()
    if not database.is_relative_to((app_dir / 'data').resolve()):
        raise ValueError('For public deployment DB_PATH must be inside APP_DIR/data')
    (stage / 'wb-stock-bot.caddy').write_text(caddy_config(domain, settings.crm_port, https_port, dns_provider))
    (stage / 'dns-provider').write_text(dns_provider)
    (stage / 'https-port').write_text(str(https_port))
    (stage / 'public-url').write_text(settings.crm_public_url)
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


SELECTEL_KEYS = ('SELECTEL_USER', 'SELECTEL_PASSWORD', 'SELECTEL_ACCOUNT_ID', 'SELECTEL_PROJECT_NAME')


def selectel_credentials(path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise ValueError('Create /etc/wb-stock-bot/selectel.env with Selectel service-user credentials')
    info = path.stat()
    if info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError('Selectel credentials must be owned by root with permissions 0600')
    values = dotenv_values(path, interpolate=False)
    result = {}
    for key in SELECTEL_KEYS:
        value = values.get(key)
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError(f'Selectel credential {key} is missing or contains control characters')
        result[key] = value
    return result


def prepare_selectel(path: Path, stage: Path):
    values = selectel_credentials(path)
    # Normalize dotenv data to systemd EnvironmentFile syntax; never source it as shell.
    lines = []
    for key, value in values.items():
        escaped = value.replace('\\', '\\\\').replace('"', '\\"')
        lines.append(f'{key}="{escaped}"')
    target = stage / 'selectel.env'
    target.write_text('\n'.join(lines) + '\n')
    target.chmod(0o600)
    (stage / 'caddy-selectel.conf').write_text("""[Service]
EnvironmentFile=/etc/caddy/wb-crm-selectel.env
ExecStart=
ExecStart=/usr/local/lib/wb-stock-bot/caddy run --config /etc/caddy/Caddyfile
ExecReload=
ExecReload=/usr/local/lib/wb-stock-bot/caddy reload --config /etc/caddy/Caddyfile --force
""")


def caddy_root(original: str, dns: bool) -> str:
    import_line = 'import /etc/caddy/wb-stock-bot.caddy'
    if dns:
        # DNS-only operation must not open an automatic HTTP redirect listener.
        if re.search(r'^\s*auto_https\s+', original, re.M):
            if not re.search(r'^\s*auto_https\s+disable_redirects\s*$', original, re.M):
                raise ValueError('Existing auto_https option conflicts with DNS-only deployment')
        else:
            start = re.match(r'(?:\s|#[^\n]*\n)*', original).end()
            if original[start:start + 1] == '{':
                original = original[:start + 1] + '\n    auto_https disable_redirects\n' + original[start + 1:]
            else:
                original = '{\n    auto_https disable_redirects\n}\n' + original
    if import_line not in original.splitlines():
        original += '\n' + import_line + '\n'
    return original


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
    parser.add_argument('action', choices=['prepare-caddy', 'validate', 'healthcheck', 'prepare-selectel', 'caddy-root', 'validate-caddy'])
    parser.add_argument('--app-dir', type=Path, default=Path.cwd())
    parser.add_argument('--domain', default=os.getenv('CRM_DOMAIN', ''))
    parser.add_argument('--stage', type=Path)
    parser.add_argument('--https-port', type=int, default=os.getenv('CRM_HTTPS_PORT') or None)
    parser.add_argument('--dns-provider', choices=['http', 'selectel'], default=os.getenv('CRM_DNS_PROVIDER') or None)
    parser.add_argument('--caddy-root', type=Path, default=Path('/etc/caddy/Caddyfile'))
    parser.add_argument('--caddy-bin', default='caddy')
    args = parser.parse_args()
    try:
        if args.action in {'prepare-caddy', 'prepare-selectel', 'caddy-root'} and args.stage is None:
            raise ValueError('--stage is required')
        if args.action == 'prepare-selectel':
            prepare_selectel(Path('/etc/wb-stock-bot/selectel.env'), args.stage)
        elif args.action == 'caddy-root':
            original = args.caddy_root.read_text() if args.caddy_root.exists() else ''
            (args.stage / 'Caddyfile').write_text(caddy_root(original, args.dns_provider == 'selectel'))
        elif args.action == 'validate-caddy':
            env = dict(os.environ)
            if args.dns_provider == 'selectel':
                env.update(selectel_credentials(Path('/etc/wb-stock-bot/selectel.env')))
            subprocess.run([args.caddy_bin, 'validate', '--config', str(args.caddy_root), '--adapter', 'caddyfile'], env=env, check=True)
        elif args.action == 'prepare-caddy':
            prepare(args.app_dir.resolve(), args.domain, args.stage, args.https_port, args.dns_provider)
        elif args.action == 'validate':
            crm_settings(args.app_dir / '.env')
        else:
            healthcheck(args.app_dir)
    except Exception as exc:
        # Do not print credentials or HTTP headers in deployment output.
        raise SystemExit(f'Deployment check failed: {exc}') from None


if __name__ == '__main__':
    main()
