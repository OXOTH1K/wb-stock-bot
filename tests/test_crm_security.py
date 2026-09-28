import base64
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from app.crm import CRMServer
from app.crm_ui import INDEX_HTML
from app.crm_security import CRMAccessPolicy, validate_crm_settings, validate_domain
from app.db import StateDB
from app.models import Product


def settings(**kwargs):
    return SimpleNamespace(**(dict(crm_enabled=True, crm_host='127.0.0.1', crm_port=8080,
        crm_user='admin', crm_password='a-unique-password-at-least-20',
        crm_allowed_networks=('127.0.0.1/32',), crm_public_url='') | kwargs))


def auth(password='a-unique-password-at-least-20', user='admin'):
    return {'Authorization': 'Basic ' + base64.b64encode(f'{user}:{password}'.encode()).decode()}


class CRMPolicyTests(unittest.TestCase):
    def test_requires_password_even_with_loopback_or_lan_allowlist(self):
        for host in ('127.0.0.1', '0.0.0.0'):
            with self.assertRaises(RuntimeError):
                validate_crm_settings(settings(crm_host=host, crm_password=''))
        validate_crm_settings(settings(crm_enabled=False, crm_user='', crm_password=''))

    def test_public_url_and_domain_reject_injection_and_unsafe_bind(self):
        for value in ('https://crm.example.ru/', 'http://crm.example.ru', 'https://crm.example.ru:65536', 'https://crm.example.ru:0',
                      'https://crm.example.ru:foo', 'https://crm.example.ru:04443',
                      'https://user@crm.example.ru', 'https://127.0.0.1', 'https://crm.example.ru/\nfoo'):
            with self.assertRaises(RuntimeError):
                validate_crm_settings(settings(crm_public_url=value))
        for value in ('example.ru {\nrespond hacked\n}', '-x.example.ru', '*.example.ru', 'example.ru/path', 'localhost'):
            with self.assertRaises(ValueError):
                validate_domain(value)
        with self.assertRaises(RuntimeError):
            validate_crm_settings(settings(crm_public_url='https://crm.example.ru', crm_host='0.0.0.0'))
        with self.assertRaises(RuntimeError):
            validate_crm_settings(settings(crm_public_url='https://crm.example.ru', crm_password='short'))
        validate_crm_settings(settings(crm_public_url='https://crm.example.ru'))
        validate_crm_settings(settings(crm_public_url='https://crm.example.ru:4443'))

    def test_csp_hashes_match_ui_and_inline_handlers_are_removed(self):
        policy = CRMAccessPolicy(settings()).csp
        self.assertNotIn('unsafe-inline', policy)
        self.assertNotIn('unsafe-eval', policy)
        self.assertNotRegex(INDEX_HTML, r'\bon(?:click|load|error)=')
        for tag in ('script', 'style'):
            content = INDEX_HTML.split(f'<{tag}>')[1].split(f'</{tag}>')[0]
            digest = base64.b64encode(hashlib.sha256(content.encode()).digest()).decode()
            self.assertIn(digest, policy)


class CRMSecurityHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = StateDB(Path(self.tmp.name) / 'state.db')
        service = SimpleNamespace(products={1: Product(1, 'SKU', 'Name', (1,))},
                                  fbs_stock={1: 5}, wb_stock={1: 0}, warehouse=SimpleNamespace(name='Main'))
        self.crm = CRMServer(settings(), service, self.db)
        self.client = TestClient(TestServer(self.crm.app))
        await self.client.start_server()
        self.headers = auth() | {'X-CSRF-Token': self.crm.security.csrf_token}

    async def asyncTearDown(self):
        await self.client.close()
        self.db.close()
        self.tmp.cleanup()

    async def test_every_route_and_files_require_auth_and_no_forwarded_bypass(self):
        before = self.db.conn.total_changes
        paths = ['/', '/healthz', '/api/session', '/api/inventory', '/api/inventory/movements',
                 '/api/wb/order-lookup?number=1', '/api/wb/fbw-orders', '/api/wb/sales-analytics', '/.env', '/.git/config',
                 '/data/stocks.sqlite3', '/api/inventory/set', '/api/inventory/adjust', '/api/inventory/available/set']
        for path in paths:
            for method in ('GET', 'POST', 'OPTIONS'):
                response = await self.client.request(method, path, headers={'X-Forwarded-For': '127.0.0.1',
                    'Forwarded': 'for=127.0.0.1', 'X-CRM-Client-IP': '127.0.0.1'}, json={'sku': 'SKU', 'quantity': 0})
                self.assertEqual(response.status, 401, (method, path))
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(self.db.conn.total_changes, before)
        for path in ('/.env', '/.git/config', '/data/stocks.sqlite3', '/app/config.py'):
            self.assertEqual((await self.client.get(path, headers=auth())).status, 404)

    async def test_csrf_protects_all_mutations_and_legitimate_edits_still_work(self):
        await self.client.get('/api/inventory', headers=auth())
        before = self.db.conn.total_changes
        for route in ('set', 'adjust', 'available/set'):
            for headers in (auth(), auth() | {'X-CSRF-Token': 'wrong'},
                            self.headers | {'Origin': 'https://evil.example'},
                            self.headers | {'Sec-Fetch-Site': 'cross-site'}):
                response = await self.client.post('/api/inventory/' + route, json={'sku': 'SKU', 'quantity': 0, 'delta': -1}, headers=headers)
                self.assertEqual(response.status, 403)
        self.assertEqual(self.db.conn.total_changes, before)
        session = await self.client.get('/api/session', headers=auth())
        self.assertEqual((await session.json())['csrf_token'], self.crm.security.csrf_token)
        response = await self.client.post('/api/inventory/set', json={'sku': 'SKU', 'quantity': 7}, headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.db.get_local_stock(('SKU',))['SKU'], 7)

    async def test_content_type_size_compression_and_integer_validation(self):
        for quantity in (True, 1.5, '7', -1, 10**30):
            response = await self.client.post('/api/inventory/set', json={'sku': 'SKU', 'quantity': quantity}, headers=self.headers)
            self.assertEqual(response.status, 400)
        response = await self.client.post('/api/inventory/set', data='{"sku":"SKU","quantity":0}', headers=self.headers | {'Content-Type': 'text/plain'})
        self.assertEqual(response.status, 415)
        response = await self.client.post('/api/inventory/set', json={'sku': 'SKU', 'quantity': 0, 'pad': 'x'*20000}, headers=self.headers)
        self.assertEqual(response.status, 413)
        response = await self.client.post('/api/inventory/set', data=b'compressed', headers=self.headers | {'Content-Encoding': 'gzip'})
        self.assertEqual(response.status, 415)

    async def test_invalid_credentials_rate_limit_and_expiration(self):
        for _ in range(10):
            self.assertEqual((await self.client.get('/', headers=auth('bad'))).status, 401)
        response = await self.client.get('/', headers=auth('bad'))
        self.assertEqual(response.status, 429)
        self.assertIn('Retry-After', response.headers)
        with patch('app.crm_security.time.monotonic', return_value=10**12):
            self.assertEqual((await self.client.get('/', headers=auth())).status, 200)

    async def test_utf8_credentials_and_public_host_check(self):
        self.crm.settings.crm_user = 'пользователь'
        self.crm.settings.crm_password = 'пароль-длинный-уникальный'
        response = await self.client.get('/', headers=auth('пароль-длинный-уникальный', 'пользователь'))
        self.assertEqual(response.status, 200)
        self.crm.security.public_url = 'https://crm.example.ru'
        headers = auth('пароль-длинный-уникальный', 'пользователь')
        self.assertEqual((await self.client.get('/', headers=headers)).status, 403)
        response = await self.client.get('/', headers=headers | {'Host': 'crm.example.ru', 'X-Forwarded-Proto': 'https'})
        self.assertEqual(response.status, 200)
        self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
        self.assertEqual(response.headers['X-Frame-Options'], 'DENY')
        self.assertNotIn('Access-Control-Allow-Origin', response.headers)

    async def test_custom_https_port_requires_exact_host_and_origin(self):
        self.crm.security.public_url = 'https://crm.example.ru:4443'
        headers = self.headers | {'Host': 'crm.example.ru:4443', 'X-Forwarded-Proto': 'https'}
        self.assertEqual((await self.client.get('/', headers=headers)).status, 200)
        for host in ('crm.example.ru', 'crm.example.ru:443', 'crm.example.ru:4444'):
            self.assertEqual((await self.client.get('/', headers=headers | {'Host': host})).status, 403)
        for origin in ('https://crm.example.ru', 'https://crm.example.ru:4444'):
            response = await self.client.post('/api/inventory/set', json={'sku': 'SKU', 'quantity': 7},
                headers=headers | {'Origin': origin})
            self.assertEqual(response.status, 403)
        await self.client.get('/api/inventory', headers=headers)
        response = await self.client.post('/api/inventory/set', json={'sku': 'SKU', 'quantity': 7},
            headers=headers | {'Origin': 'https://crm.example.ru:4443'})
        self.assertEqual(response.status, 200)

    async def test_sales_analytics_validates_period_and_reports_loading(self):
        from app.wb_sales_analytics import WBSalesAnalytics
        self.crm.sales_analytics = WBSalesAnalytics(None, self.db, lambda: self.crm.service.products)
        response = await self.client.get('/api/wb/sales-analytics?date_from=bad&date_to=2026-09-20', headers=auth())
        self.assertEqual(response.status, 400)
        response = await self.client.get('/api/wb/sales-analytics?date_from=2026-09-20&date_to=2026-09-21&sku=SKU', headers=auth())
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertFalse(data['ready'])
        self.assertTrue(data['syncing'])
        self.assertEqual(data['points'], [])
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
