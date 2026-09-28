import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dotenv import dotenv_values

from app.deployment import caddy_config, crm_settings, prepare
from app.config import Settings


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.stage = self.root / 'stage'
        self.stage.mkdir()
        self.env = self.root / '.env'
        self.env.write_text('WB_TOKEN=fake\nTELEGRAM_BOT_TOKEN=fake\nCRM_USER=admin\n'
                            'CRM_PASSWORD=long-unique-password-123456\nCRM_HOST=0.0.0.0\n'
                            'CRM_ALLOWED_NETWORKS=192.168.1.0/24\nDB_PATH=./data/test.sqlite3\n')

    def tearDown(self):
        self.tmp.cleanup()

    def test_prepare_preserves_secrets_and_original_config_and_forces_loopback(self):
        original = self.env.read_bytes()
        prepare(self.root, 'crm.example.ru', self.stage)
        self.assertEqual(self.env.read_bytes(), original)
        values = dotenv_values(self.stage / '.env', interpolate=False)
        self.assertEqual(values['WB_TOKEN'], 'fake')
        self.assertEqual(values['CRM_PASSWORD'], 'long-unique-password-123456')
        self.assertEqual(values['CRM_HOST'], '127.0.0.1')
        self.assertEqual(values['CRM_PUBLIC_URL'], 'https://crm.example.ru')
        self.assertEqual((self.stage / '.env').stat().st_mode & 0o777, 0o600)
        config = (self.stage / 'wb-stock-bot.caddy').read_text()
        self.assertIn('reverse_proxy 127.0.0.1:8080', config)
        self.assertIn('X-CRM-Client-IP {remote_host}', config)
        self.assertNotIn(values['CRM_PASSWORD'], config)
        self.assertNotIn('file_server', config)
        dropin = (self.stage / 'crm-security.conf').read_text()
        self.assertIn('ProtectSystem=strict', dropin)
        self.assertIn(f'ReadWritePaths={self.root}/data', dropin)

    def test_prepare_rejects_weak_password_external_db_and_invalid_domain(self):
        for suffix in ('CRM_PASSWORD=short\n', 'DB_PATH=/etc/something.db\n'):
            original = self.env.read_text()
            self.env.write_text(original + suffix)
            with self.assertRaises((ValueError, RuntimeError)):
                prepare(self.root, 'crm.example.ru', self.stage)
            self.env.write_text(original)
        for domain in ('https://crm.example.ru', 'a.ru\nfile_server', 'a.ru:443'):
            with self.assertRaises(ValueError):
                prepare(self.root, domain, self.stage)
        with self.assertRaises(ValueError):
            caddy_config('crm.example.ru', 80)

    def test_environment_is_parsed_as_data_not_shell_or_variable_expansion(self):
        self.env.write_text(self.env.read_text() + "CRM_PASSWORD='${WB_TOKEN}-this-is-a-literal-password'\n")
        self.assertEqual(crm_settings(self.env).crm_password, '${WB_TOKEN}-this-is-a-literal-password')
        values = dict(dotenv_values(self.env, interpolate=False))
        with patch.dict(os.environ, values, clear=True), patch('app.config.load_dotenv'):
            self.assertEqual(Settings.from_env().crm_password, '${WB_TOKEN}-this-is-a-literal-password')

    def test_settings_block_anonymous_crm_at_startup(self):
        with patch.dict(os.environ, {'WB_TOKEN': 'fake', 'TELEGRAM_BOT_TOKEN': 'fake'}, clear=True), patch('app.config.load_dotenv'):
            with self.assertRaisesRegex(RuntimeError, 'CRM_USER'):
                Settings.from_env()

    def test_custom_https_port_is_preserved_on_redeployment(self):
        prepare(self.root, 'crm.example.ru', self.stage, 4443)
        values = dotenv_values(self.stage / '.env', interpolate=False)
        self.assertEqual(values['CRM_PUBLIC_URL'], 'https://crm.example.ru:4443')
        self.assertEqual(values['CRM_HTTPS_PORT'], '4443')
        config = (self.stage / 'wb-stock-bot.caddy').read_text()
        self.assertIn('https://crm.example.ru:4443 {', config)
        self.assertIn('header_up Host crm.example.ru:4443', config)
        self.assertIn('header_up X-Forwarded-Host crm.example.ru:4443', config)
        self.env.write_text((self.stage / '.env').read_text())
        prepare(self.root, 'crm.example.ru', self.stage)
        self.assertEqual((self.stage / 'wb-stock-bot.caddy').read_text(), config)

    def test_invalid_https_ports_and_listener_collision(self):
        for port in (0, -1, 80, 65536, 8080):
            with self.assertRaises(ValueError):
                caddy_config('crm.example.ru', 8080, port)

    def test_selectel_dns_config_and_domain_change(self):
        prepare(self.root, 'crm.example.ru', self.stage, 4443, 'selectel')
        config = (self.stage / 'wb-stock-bot.caddy').read_text()
        self.assertIn('dns selectel {', config)
        self.assertIn('disable_http_challenge', config)
        self.assertIn('disable_tlsalpn_challenge', config)
        self.assertIn('https://acme-v02.api.letsencrypt.org/directory', config)
        self.env.write_text((self.stage / '.env').read_text())
        prepare(self.root, 'other.example.ru', self.stage)
        config = (self.stage / 'wb-stock-bot.caddy').read_text()
        self.assertIn('https://other.example.ru:4443 {', config)
        self.assertNotIn('crm.example.ru', config)
        self.assertIn('dns selectel {', config)
        with self.assertRaises(ValueError):
            caddy_config('crm.example.ru', 8080, 4443, 'unknown')

    def test_dns_root_preserves_sites_and_global_options(self):
        from app.deployment import caddy_root
        for original in ('', '# comment\n{\n email admin@example.ru\n}\nother.example.ru { respond OK }\n'):
            rendered = caddy_root(original, True)
            self.assertIn('auto_https disable_redirects', rendered)
            self.assertEqual(caddy_root(rendered, True), rendered)
            if original:
                self.assertIn('email admin@example.ru', rendered)
                self.assertIn('other.example.ru { respond OK }', rendered)
        with self.assertRaises(ValueError):
            caddy_root('{\n auto_https off\n}\n', True)

    def test_selectel_secrets_are_separate_and_not_executable(self):
        from app.deployment import prepare_selectel, selectel_credentials
        secret = self.root / 'selectel.env'
        secret.write_text("SELECTEL_USER=service\nSELECTEL_PASSWORD='literal-${VAR}-$(id)-password'\n"
                          'SELECTEL_ACCOUNT_ID=123456\nSELECTEL_PROJECT_NAME=test\n')
        secret.chmod(0o600)
        info = secret.stat()
        root_stat = type('Stat', (), {'st_uid': 0, 'st_mode': info.st_mode})()
        with patch.object(Path, 'stat', return_value=root_stat):
            prepare_selectel(secret, self.stage)
        content = (self.stage / 'selectel.env').read_text()
        self.assertIn('literal-${VAR}-$(id)-password', content)
        self.assertEqual((self.stage / 'selectel.env').stat().st_mode & 0o777, 0o600)
        dropin = (self.stage / 'caddy-selectel.conf').read_text()
        self.assertNotIn('--environ', dropin)
        self.assertNotIn('literal-', dropin)
        self.assertIn('EnvironmentFile=/etc/caddy/wb-crm-selectel.env', dropin)
        secret.chmod(0o644)
        with self.assertRaises(ValueError):
            selectel_credentials(secret)
