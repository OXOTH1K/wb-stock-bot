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
