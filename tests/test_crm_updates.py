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

    async def test_trigger_rechecks_and_uses_only_fixed_systemd_unit(self):
        checker = self.checker()
        process = AsyncMock()
        process.wait.return_value = 0
        with patch('app.crm_updates.asyncio.create_subprocess_exec', new=AsyncMock(return_value=process)) as run:
            started, _ = await checker.trigger('v1.2.3')
        self.assertTrue(started)
        self.assertEqual(run.await_args.args, ('/usr/bin/sudo', '-n', '/usr/bin/systemctl',
                                               'start', '--no-block', 'wb-stock-bot-crm-update.service'))

    async def test_trigger_rejects_unavailable_tag_without_running_commands(self):
        checker = self.checker(body='manual only')
        with patch('app.crm_updates.asyncio.create_subprocess_exec', new=AsyncMock()) as run:
            started, _ = await checker.trigger('v9.9.9')
        self.assertFalse(started)
        run.assert_not_awaited()
