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
    order_check_interval: int
    stocks_page_size: int
    http_timeout: int
    crm_enabled: bool
    crm_host: str
    crm_port: int
    crm_user: str
    crm_password: str
    crm_allowed_networks: tuple[str, ...]
    ozon_client_id: str
    ozon_api_key: str
    ozon_order_check_interval: int
    ozon_stock_check_interval: int
    ozon_warehouse_id: int | None

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

        ozon_client_id = os.getenv("OZON_CLIENT_ID", "").strip()
        ozon_api_key = os.getenv("OZON_API_KEY", "").strip()
        raw_ozon_warehouse_id = os.getenv("OZON_WAREHOUSE_ID", "").strip()
        ozon_warehouse_id = (
            int(raw_ozon_warehouse_id)
            if raw_ozon_warehouse_id
            else None
        )
        if bool(ozon_client_id) != bool(ozon_api_key):
            raise RuntimeError(
                "OZON_CLIENT_ID and OZON_API_KEY must be set together"
            )

        crm_enabled = os.getenv("CRM_ENABLED", "1").strip().lower() not in {
            "0", "false", "no", "off"
        }
        crm_host = os.getenv("CRM_HOST", "127.0.0.1").strip() or "127.0.0.1"
        crm_port = int(os.getenv("CRM_PORT", "8080"))
        crm_user = os.getenv("CRM_USER", "").strip()
        crm_password = os.getenv("CRM_PASSWORD", "").strip()
        raw_allowed_networks = os.getenv("CRM_ALLOWED_NETWORKS", "").strip()
        crm_allowed_networks = tuple(
            item.strip()
            for item in raw_allowed_networks.split(",")
            if item.strip()
        )
        if bool(crm_user) != bool(crm_password):
            raise RuntimeError("CRM_USER and CRM_PASSWORD must be set together")
        if (
            crm_enabled
            and crm_host not in {"127.0.0.1", "localhost", "::1"}
            and not crm_allowed_networks
            and not (crm_user and crm_password)
        ):
            raise RuntimeError(
                "Non-local CRM_HOST requires CRM_ALLOWED_NETWORKS or CRM_USER/CRM_PASSWORD"
            )

        return cls(
            wb_token=wb_token,
            telegram_bot_token=telegram_bot_token,
            telegram_chat_ids=frozenset(chat_ids),
            db_path=Path(os.getenv("DB_PATH", "./data/stocks.sqlite3")),
            fbs_check_interval=int(os.getenv("FBS_CHECK_INTERVAL", "300")),
            wb_check_interval=int(os.getenv("WB_CHECK_INTERVAL", "1800")),
            catalog_refresh_interval=int(os.getenv("CATALOG_REFRESH_INTERVAL", "21600")),
            order_check_interval=int(os.getenv("ORDER_CHECK_INTERVAL", "30")),
            stocks_page_size=int(os.getenv("STOCKS_PAGE_SIZE", "30")),
            http_timeout=int(os.getenv("HTTP_TIMEOUT", "30")),
            crm_enabled=crm_enabled,
            crm_host=crm_host,
            crm_port=crm_port,
            crm_user=crm_user,
            crm_password=crm_password,
            crm_allowed_networks=crm_allowed_networks,
            ozon_client_id=ozon_client_id,
            ozon_api_key=ozon_api_key,
            ozon_order_check_interval=int(
                os.getenv(
                    "OZON_ORDER_CHECK_INTERVAL",
                    os.getenv("ORDER_CHECK_INTERVAL", "30"),
                )
            ),
            ozon_stock_check_interval=int(
                os.getenv(
                    "OZON_STOCK_CHECK_INTERVAL",
                    os.getenv("FBS_CHECK_INTERVAL", "300"),
                )
            ),
            ozon_warehouse_id=ozon_warehouse_id,
        )
