from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    wb_token: str
    telegram_bot_token: str
    telegram_chat_ids: frozenset[int]
    db_path: Path
    fbs_check_interval: int
    wb_check_interval: int
    catalog_refresh_interval: int
    stocks_page_size: int
    http_timeout: int

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()

        wb_token = os.getenv("WB_TOKEN", "").strip()
        telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not wb_token:
            raise RuntimeError("WB_TOKEN is required")
        if not telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required")

        raw_chat_ids = os.getenv("TELEGRAM_CHAT_IDS", "").strip()
        chat_ids: set[int] = set()
        if raw_chat_ids:
            for item in raw_chat_ids.split(","):
                item = item.strip()
                if item:
                    chat_ids.add(int(item))

        return cls(
            wb_token=wb_token,
            telegram_bot_token=telegram_bot_token,
            telegram_chat_ids=frozenset(chat_ids),
            db_path=Path(os.getenv("DB_PATH", "./data/stocks.sqlite3")),
            fbs_check_interval=int(os.getenv("FBS_CHECK_INTERVAL", "300")),
            wb_check_interval=int(os.getenv("WB_CHECK_INTERVAL", "1800")),
            catalog_refresh_interval=int(os.getenv("CATALOG_REFRESH_INTERVAL", "21600")),
            stocks_page_size=int(os.getenv("STOCKS_PAGE_SIZE", "30")),
            http_timeout=int(os.getenv("HTTP_TIMEOUT", "30")),
        )
