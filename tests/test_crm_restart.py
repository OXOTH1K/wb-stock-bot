import tempfile
import unittest
from pathlib import Path

from app.crm_restart import CRMServiceRestarter


class CRMServiceRestarterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'data').mkdir()
        self.path_unit = self.root / 'wb-stock-bot-crm-restart.path'
        self.path_unit.touch()
        self.restarter = CRMServiceRestarter(self.root, self.path_unit)

    def tearDown(self):
        self.tmp.cleanup()

    def test_restart_creates_private_fixed_request_and_is_idempotent(self):
        started, message = self.restarter.trigger()
        self.assertTrue(started)
        self.assertIn('перезапускается', message)
        self.assertEqual(self.restarter.request_path.stat().st_mode & 0o777, 0o600)
        started, _ = self.restarter.trigger()
        self.assertTrue(started)

    def test_restart_fails_closed_when_fixed_systemd_path_unit_is_missing(self):
        self.path_unit.unlink()
        started, message = self.restarter.trigger()
        self.assertFalse(started)
        self.assertIn('deploy.sh', message)
        self.assertFalse(self.restarter.request_path.exists())


if __name__ == '__main__':
    unittest.main()
