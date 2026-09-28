"""Checks published GitHub releases that are explicitly marked self-service."""
from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

import aiohttp

log = logging.getLogger(__name__)
REPOSITORY = 'OXOTH1K/wb-stock-bot'
API = f'https://api.github.com/repos/{REPOSITORY}'
UPDATE_SERVICE = 'wb-stock-bot-crm-update.service'
CHECK_INTERVAL = 6 * 60 * 60
TRIGGER_CHECK_FRESHNESS = 30
TAG_RE = re.compile(r'^v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$')
SERVER_CONFIG_PATHS = {
    '.env.example', 'app/config.py', 'app/deployment.py', 'deploy.sh',
    'requirements.txt', 'wb-stock-bot.service',
    'scripts/build-caddy-selectel.sh',
}


class CRMReleaseChecker:
    def __init__(self, repository: Path | None = None, current_sha: str | None = None,
                 fetch_json=None, check_interval: int = CHECK_INTERVAL):
        self.repository = repository or Path(__file__).resolve().parents[1]
        self.current_sha = current_sha or self._current_revision()
        self.fetch_json = fetch_json
        self.check_interval = check_interval
        self.checked_at = 0.0
        self.info = self._result(message='Проверка релизов ещё не выполнена.')
        self.lock = asyncio.Lock()

    def _current_revision(self) -> str:
        try:
            result = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=self.repository,
                                    capture_output=True, text=True, timeout=3, check=True)
            value = result.stdout.strip()
            return value if re.fullmatch(r'[0-9a-f]{40}', value) else ''
        except (OSError, subprocess.SubprocessError):
            log.warning('Cannot determine the deployed CRM revision')
            return ''

    def _result(self, *, available=False, version='', url='', message=''):
        return {'available': available, 'current': self.current_sha[:7], 'version': version,
                'url': url, 'message': message}

    async def _github_json(self, session, url):
        if self.fetch_json is not None:
            return await self.fetch_json(url)
        async with session.get(url, headers={
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
            'User-Agent': 'wb-stock-bot-crm-release-checker',
        }) as response:
            if response.status == 404:
                return None
            response.raise_for_status()
            return await response.json(content_type=None)

    async def check(self, force=False):
        if not force and time.monotonic() - self.checked_at < self.check_interval:
            return dict(self.info)
        async with self.lock:
            if not force and time.monotonic() - self.checked_at < self.check_interval:
                return dict(self.info)
            try:
                timeout = aiohttp.ClientTimeout(total=8)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    release = await self._github_json(session, API + '/releases/latest')
                    self.info = await self._evaluate(session, release)
                self.checked_at = time.monotonic()
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, TypeError) as exc:
                log.info('Could not check GitHub releases (%s)', type(exc).__name__)
                self.info = self._result(message='Не удалось проверить релизы GitHub. Попробуйте позже.')
                self.checked_at = time.monotonic()
            return dict(self.info)

    async def _evaluate(self, session, release):
        if not self.current_sha:
            return self._result(message='Не удалось определить установленную версию.')
        if not release:
            return self._result(message='В GitHub пока нет опубликованных релизов.')
        if not isinstance(release, dict):
            raise ValueError('Invalid GitHub release response')
        tag = release.get('tag_name', '')
        body = release.get('body') or ''
        if not isinstance(tag, str) or not TAG_RE.fullmatch(tag):
            return self._result(message='Последний релиз не имеет поддерживаемого номера версии.')
        if not isinstance(body, str):
            body = ''
        if not any(line.strip().casefold() == 'crm-update: self-service' for line in body.splitlines()):
            return self._result(message='Новая версия требует ручной проверки сервера.')

        tag_url = API + '/commits/' + quote(tag, safe='')
        tagged, main, comparison = await asyncio.gather(
            self._github_json(session, tag_url),
            self._github_json(session, API + '/commits/main'),
            self._github_json(session, API + '/compare/' + self.current_sha + '...' + quote(tag, safe='')),
        )
        release_sha = (tagged or {}).get('sha', '') if isinstance(tagged, dict) else ''
        main_sha = (main or {}).get('sha', '') if isinstance(main, dict) else ''
        if not release_sha or not main_sha or release_sha != main_sha:
            return self._result(message='Релиз не совпадает с проверенной версией main.')
        if release_sha == self.current_sha:
            return self._result(message='Установлена последняя версия.')
        if not isinstance(comparison, dict) or comparison.get('status') != 'ahead':
            return self._result(message='Автоматическое обновление этой версии небезопасно.')
        changed_files = comparison.get('files')
        if not isinstance(changed_files, list) or any(
                not isinstance(item, dict) or not isinstance(item.get('filename'), str)
                for item in changed_files):
            return self._result(message='Не удалось проверить требования релиза к серверу.')
        if any(item['filename'] in SERVER_CONFIG_PATHS for item in changed_files):
            return self._result(message='Релиз меняет настройки сервера и требует ручной установки.')
        return self._result(available=True, version=tag,
                            url=release.get('html_url', ''), message='')

    async def trigger(self, tag):
        log.info('CRM update requested for %s', tag)
        if self.checked_at and time.monotonic() - self.checked_at <= TRIGGER_CHECK_FRESHNESS:
            # The UI just checked the release against GitHub. Reuse that verified
            # result instead of making a second set of API calls on the click.
            status = dict(self.info)
            log.info('Using the recent GitHub release check for CRM update')
        else:
            try:
                status = await asyncio.wait_for(self.check(force=True), timeout=12)
            except asyncio.TimeoutError:
                log.warning('Timed out rechecking GitHub before CRM update')
                return False, 'Проверка GitHub не ответила за 12 секунд. Попробуйте ещё раз.'
        if not status['available']:
            reason = status.get('message') or 'Автоматическое обновление релиза недоступно.'
            log.info('CRM update %s rejected by fresh release check: %s', tag, reason)
            return False, reason
        if tag != status['version']:
            log.info('CRM update tag mismatch: requested %s, validated %s', tag, status['version'])
            return False, 'Для этого релиза автоматическое обновление недоступно.'
        try:
            process = await asyncio.wait_for(asyncio.create_subprocess_exec(
                '/usr/bin/sudo', '-n', '/usr/bin/systemctl', 'start', '--no-block',
                UPDATE_SERVICE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE), timeout=5)
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
        except (OSError, asyncio.TimeoutError) as exc:
            log.warning('Could not start CRM update systemd unit (%s)', type(exc).__name__)
            return False, 'Служба автоматического обновления не настроена.'
        if process.returncode:
            detail = (stderr or stdout).decode('utf-8', errors='replace').strip()
            log.error('systemctl rejected CRM update start (exit status %s): %s',
                      process.returncode, detail[:500] or 'no command output')
            return False, detail[:300] or 'Не удалось запустить обновление. Проверьте sudoers и systemd.'
        log.info('CRM update systemd unit accepted release %s', tag)
        return True, 'Обновление запущено. CRM перезапустится после установки.'
