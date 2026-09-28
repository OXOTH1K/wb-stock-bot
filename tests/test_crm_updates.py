import os
import unittest
from unittest.mock import AsyncMock, patch

from app.crm_updates import CRMReleaseChecker


CURRENT = '1' * 40
RELEASE = '2' * 40


class ReleaseCheckTests(unittest.IsolatedAsyncioTestCase):
    def checker(self, *, body='CRM-Update: self-service', release_sha=RELEASE,
                main_sha=RELEASE, comparison_status='ahead', changed_files=None):
        tag = 'v1.2.3'
        data = {
            'https://api.github.com/repos/OXOTH1K/wb-stock-bot/releases/latest': {
                'tag_name': tag, 'body': body,
                'html_url': 'https://github.com/OXOTH1K/wb-stock-bot/releases/tag/v1.2.3'},
            f'https://api.github.com/repos/OXOTH1K/wb-stock-bot/commits/{tag}': {'sha': release_sha},
            'https://api.github.com/repos/OXOTH1K/wb-stock-bot/commits/main': {'sha': main_sha},
            f'https://api.github.com/repos/OXOTH1K/wb-stock-bot/compare/{CURRENT}...{tag}': {
                'status': comparison_status,
                'files': changed_files if changed_files is not None else [{'filename': 'app/service.py'}]},
        }

        async def fetch(url):
            return data[url]

        return CRMReleaseChecker(current_sha=CURRENT, fetch_json=fetch, check_interval=0)

    async def test_only_published_self_service_release_at_main_head_is_offered(self):
        status = await self.checker().check(force=True)
        self.assertTrue(status['available'])
        self.assertEqual(status['version'], 'v1.2.3')
        self.assertEqual(status['url'], 'https://github.com/OXOTH1K/wb-stock-bot/releases/tag/v1.2.3')

    async def test_unmarked_release_is_not_offered(self):
        status = await self.checker(body='Requires a new environment variable').check(force=True)
        self.assertFalse(status['available'])
        self.assertIn('ручной проверки', status['message'])

    async def test_release_not_at_main_head_is_not_offered(self):
        status = await self.checker(main_sha='3' * 40).check(force=True)
        self.assertFalse(status['available'])

    async def test_non_fast_forward_release_is_not_offered(self):
        status = await self.checker(comparison_status='diverged').check(force=True)
        self.assertFalse(status['available'])

    async def test_release_changing_environment_or_server_setup_is_not_offered(self):
        status = await self.checker(changed_files=[{'filename': 'app/config.py'}]).check(force=True)
        self.assertFalse(status['available'])
        self.assertIn('настройки сервера', status['message'])

    async def test_current_release_is_not_offered(self):
        status = await self.checker(release_sha=CURRENT, main_sha=CURRENT,
                                    comparison_status='identical').check(force=True)
        self.assertFalse(status['available'])

    async def test_trigger_queues_fixed_request_file_after_validating_release(self):
        checker = self.checker()
        with patch('app.crm_updates.os.open', return_value=42) as open_file, \
             patch('app.crm_updates.os.close') as close_file:
            started, _ = await checker.trigger('v1.2.3')
        self.assertTrue(started)
        self.assertEqual(open_file.call_args.args[0], checker.update_request)
        self.assertEqual(open_file.call_args.args[2], 0o600)
        self.assertTrue(open_file.call_args.args[1] & os.O_EXCL)
        close_file.assert_called_once_with(42)

    async def test_trigger_reuses_a_recently_verified_release(self):
        checker = self.checker()
        await checker.check(force=True)
        with patch.object(checker, 'check', new=AsyncMock(side_effect=AssertionError('unexpected recheck'))), \
             patch('app.crm_updates.os.open', return_value=42), \
             patch('app.crm_updates.os.close'):
            started, _ = await checker.trigger('v1.2.3')
        self.assertTrue(started)

    async def test_trigger_rejects_unavailable_tag_without_running_commands(self):
        checker = self.checker(body='manual only')
        with patch('app.crm_updates.os.open') as open_file:
            started, _ = await checker.trigger('v9.9.9')
        self.assertFalse(started)
        open_file.assert_not_called()

    async def test_trigger_returns_fresh_release_check_reason(self):
        checker = self.checker(body='manual only')
        started, message = await checker.trigger('v1.2.3')
        self.assertFalse(started)
        self.assertIn('ручной проверки', message)

    async def test_trigger_reports_request_directory_error(self):
        checker = self.checker()
        with patch('app.crm_updates.os.open', side_effect=PermissionError):
            started, message = await checker.trigger('v1.2.3')
        self.assertFalse(started)
        self.assertIn('каталогу data', message)

    async def test_trigger_is_idempotent_when_request_is_already_queued(self):
        checker = self.checker()
        with patch('app.crm_updates.os.open', side_effect=FileExistsError):
            started, message = await checker.trigger('v1.2.3')
        self.assertTrue(started)
        self.assertIn('передан', message)
