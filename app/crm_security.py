"""CRM access policy shared by startup, deployment validation and HTTP handlers."""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import re
import secrets
import time
from collections import OrderedDict
from urllib.parse import urlsplit

from aiohttp import web

from .crm_ui import INDEX_HTML


def validate_domain(domain: str) -> str:
    domain = domain.lower()
    if len(domain) > 253 or '.' not in domain or not all(
        re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part)
        for part in domain.split('.')
    ):
        raise ValueError('CRM_DOMAIN must be a DNS hostname, without scheme, port or path')
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        return domain
    raise ValueError('CRM_DOMAIN must be a DNS hostname, not an IP address')


def validate_crm_settings(settings) -> None:
    if not settings.crm_enabled:
        return
    if not settings.crm_user or not settings.crm_password:
        raise RuntimeError('CRM_USER and CRM_PASSWORD are required for every enabled CRM, including LAN/localhost')
    if ':' in settings.crm_user or any(ord(c) < 32 for c in settings.crm_user + settings.crm_password):
        raise RuntimeError('CRM credentials must not contain control characters or a colon in the username')
    if not 1 <= settings.crm_port <= 65535:
        raise RuntimeError('Invalid CRM_PORT')
    public_url = getattr(settings, 'crm_public_url', '')
    if public_url:
        parts = urlsplit(public_url)
        try:
            domain = validate_domain(parts.netloc)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        if public_url != f'https://{domain}':
            raise RuntimeError('CRM_PUBLIC_URL must be https://domain without a trailing slash')
        if settings.crm_host != '127.0.0.1':
            raise RuntimeError('Public CRM must listen only on 127.0.0.1 behind Caddy')
        if len(settings.crm_password) < 20:
            raise RuntimeError('Public CRM requires a unique CRM_PASSWORD of at least 20 characters')


def content_security_policy() -> str:
    def digest(tag):
        value = INDEX_HTML.split(f'<{tag}>', 1)[1].split(f'</{tag}>', 1)[0]
        return base64.b64encode(hashlib.sha256(value.encode()).digest()).decode()
    return (
        "default-src 'none'; script-src 'sha256-" + digest('script') + "'; "
        "style-src 'sha256-" + digest('style') + "'; connect-src 'self'; "
        "base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'self'"
    )


class CRMAccessPolicy:
    def __init__(self, settings):
        validate_crm_settings(settings)
        self.settings = settings
        self.csrf_token = secrets.token_urlsafe(32)
        self.public_url = getattr(settings, 'crm_public_url', '')
        self.failures: OrderedDict[str, tuple[int, float]] = OrderedDict()
        self.csp = content_security_policy()

    def client_key(self, request):
        # Only the explicitly configured local Caddy may supply this header.
        # Caddy overwrites it with the TCP peer, never the incoming X-Forwarded-For.
        if self.public_url and request.remote in {'127.0.0.1', '::1'}:
            value = request.headers.get('X-CRM-Client-IP', '')
            try:
                return str(ipaddress.ip_address(value))
            except ValueError:
                pass
        return request.remote or 'unknown'

    def authenticate(self, request):
        key, now = self.client_key(request), time.monotonic()
        count, until = self.failures.get(key, (0, 0))
        if until <= now:
            count = 0
            self.failures.pop(key, None)
        if count >= 10:
            raise web.HTTPTooManyRequests(headers={'Retry-After': str(max(1, int(until - now)))})
        auth = request.headers.get('Authorization', '')
        username = password = ''
        if auth.lower().startswith('basic '):
            try:
                raw = base64.b64decode(auth[6:], validate=True).decode('utf-8')
                username, password = raw.split(':', 1)
            except (ValueError, UnicodeError):
                pass
        user_ok = secrets.compare_digest(username.encode(), self.settings.crm_user.encode())
        password_ok = secrets.compare_digest(password.encode(), self.settings.crm_password.encode())
        if not (self.settings.crm_user and self.settings.crm_password and user_ok and password_ok):
            # Missing credentials trigger the browser challenge, without letting
            # unauthenticated navigations lock out a shared NAT address.
            if auth:
                self.failures[key] = (count + 1, until if count else now + 60)
                self.failures.move_to_end(key)
                while len(self.failures) > 4096:
                    self.failures.popitem(last=False)
            raise web.HTTPUnauthorized(headers={'WWW-Authenticate': 'Basic realm="WB CRM", charset="UTF-8"'})
        self.failures.pop(key, None)

    def check_request(self, request):
        if self.public_url:
            if request.host != urlsplit(self.public_url).netloc or request.headers.get('X-Forwarded-Proto') != 'https':
                raise web.HTTPForbidden(text='Use the configured HTTPS CRM address')
        if request.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise web.HTTPForbidden(text='Cross-site requests are not allowed')
        if request.headers.get('Content-Encoding', 'identity').lower() != 'identity':
            raise web.HTTPUnsupportedMediaType(text='Compressed request bodies are not supported')
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            expected_origin = self.public_url or f'{request.scheme}://{request.host}'
            origin = request.headers.get('Origin')
            if origin is not None and origin != expected_origin:
                raise web.HTTPForbidden(text='Invalid request origin')
            token = request.headers.get('X-CSRF-Token', '')
            if not secrets.compare_digest(token.encode(), self.csrf_token.encode()):
                raise web.HTTPForbidden(text='Refresh the CRM page and try again (CSRF token missing or expired)')
            if request.content_type != 'application/json':
                raise web.HTTPUnsupportedMediaType(text='application/json is required')

    @web.middleware
    async def headers(self, request, handler):
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            response = exc
        response.headers.update({
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'X-Frame-Options': 'DENY', 'Referrer-Policy': 'no-referrer',
            'Content-Security-Policy': self.csp,
            'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
        })
        return response
