"""Queue a fixed systemd restart request for the bot service."""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


class CRMServiceRestarter:
    def __init__(self, repository: Path | None = None,
                 path_unit: Path = Path('/etc/systemd/system/wb-stock-bot-crm-restart.path')):
        self.repository = repository or Path(__file__).resolve().parents[1]
        self.request_path = self.repository / 'data' / 'crm-restart.request'
        self.path_unit = path_unit

    def trigger(self) -> tuple[bool, str]:
        if not self.path_unit.is_file():
            return False, 'Перезапуск из CRM не настроен. Запустите deploy.sh от имени администратора сервера.'
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, 'O_NOFOLLOW'):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.request_path, flags, 0o600)
            os.close(descriptor)
        except FileExistsError:
            log.info('CRM bot restart request is already queued')
        except OSError as exc:
            log.error('Could not queue CRM bot restart (%s)', type(exc).__name__)
            return False, 'Не удалось передать запрос на перезапуск службе.'
        log.info('Queued CRM bot service restart')
        return True, 'Запрос передан. Сервис бота перезапускается.'
