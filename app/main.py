from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import AsyncExitStack

from .config import Settings
from .crm import CRMServer
from .db import StateDB
from .orders import OrderMonitor
from .ozon import OzonIntegration
from .ozon_client import OzonClient
from .service import StockMonitorService
from .shared_inventory import SharedInventoryService
from .telegram import TelegramBot
from .wb_client import WildberriesClient


async def amain() -> None:
    settings = Settings.from_env()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    db = StateDB(settings.db_path)

    try:
        async with AsyncExitStack() as stack:
            wb = await stack.enter_async_context(
                WildberriesClient(
                    settings.wb_token,
                    settings.http_timeout,
                )
            )
            tg = await stack.enter_async_context(
                TelegramBot(settings.telegram_bot_token)
            )

            service = StockMonitorService(settings, wb, tg, db)
            await service.initialize()
            if service.warehouse is None:
                raise RuntimeError("Seller warehouse is not initialized")

            orders = OrderMonitor(
                settings, wb, tg, db, service.warehouse.id
            )

            ozon: OzonIntegration | None = None
            if settings.ozon_client_id and settings.ozon_api_key:
                ozon_client = await stack.enter_async_context(
                    OzonClient(
                        settings.ozon_client_id,
                        settings.ozon_api_key,
                        settings.http_timeout,
                    )
                )
                ozon = OzonIntegration(
                    settings, ozon_client, tg, db
                )
                try:
                    await ozon.refresh_catalog_and_stocks()
                except Exception:
                    logging.getLogger(__name__).exception(
                        "Ozon initial stock sync failed; background loop will retry"
                    )
                logging.getLogger(__name__).info(
                    "Ozon integration enabled"
                )
            else:
                logging.getLogger(__name__).info(
                    "Ozon integration disabled: credentials are not configured"
                )

            shared_inventory = SharedInventoryService(
                db, service, ozon
            )
            await shared_inventory.initialize()
            service.set_shared_inventory(shared_inventory)
            orders.set_shared_inventory(shared_inventory)
            if ozon is not None:
                ozon.set_shared_inventory(shared_inventory)

            await orders.poll_once(service.reconcile_after_gap)
            if ozon is not None:
                try:
                    await ozon.refresh_orders()
                except Exception:
                    logging.getLogger(__name__).exception(
                        "Ozon initial order sync failed; background loop will retry"
                    )

            crm = CRMServer(
                settings,
                service,
                db,
                shared_inventory=shared_inventory,
            )
            await crm.start()

            async def message_handler(
                chat_id: int, text: str
            ) -> None:
                command = (
                    text.split(maxsplit=1)[0] if text.strip() else ""
                ).split("@", 1)[0].lower()
                if (
                    command == "/status"
                    and chat_id in settings.telegram_chat_ids
                ):
                    await tg.send_message(
                        chat_id,
                        "🔎 Проверяю заказы и остатки…",
                    )
                    try:
                        wb_order_count = await orders.audit_pending(
                            chat_id
                        )
                        ozon_order_count = (
                            await ozon.audit_pending(chat_id)
                            if ozon is not None
                            else 0
                        )
                        stock_count, wb_note = (
                            await service.audit_actionable_stocks(
                                chat_id
                            )
                        )
                        ozon_stock_count = (
                            await ozon.audit_actionable_stocks(
                                chat_id
                            )
                            if ozon is not None
                            else 0
                        )
                        summary = (
                            service._format_status()
                            + "\n\n"
                            + (
                                "Необработанных новых WB-заказов: "
                                f"{wb_order_count}\n"
                            )
                        )
                        if ozon is not None:
                            summary += (
                                "Необработанных новых OZON-заказов: "
                                f"{ozon_order_count}\n"
                            )
                        summary += (
                            "Ситуаций WB по остаткам, требующих решения: "
                            f"{stock_count}\n"
                        )
                        if ozon is not None:
                            summary += (
                                "Ситуаций OZON по остаткам, требующих решения: "
                                f"{ozon_stock_count}"
                            )
                        else:
                            summary = summary.rstrip()
                        if wb_note:
                            summary += "\n" + wb_note
                        await tg.send_message(chat_id, summary)
                    except Exception as exc:
                        logging.getLogger(__name__).exception(
                            "Status audit failed"
                        )
                        await tg.send_message(
                            chat_id,
                            "⚠️ Не удалось выполнить полную сверку: "
                            f"{exc}",
                        )
                    return
                if ozon is not None:
                    try:
                        if await ozon.handle_message(
                            chat_id, text
                        ):
                            return
                    except Exception as exc:
                        logging.getLogger(__name__).exception(
                            "Ozon command failed: %s", command
                        )
                        await tg.send_message(
                            chat_id,
                            "⚠️ Не удалось выполнить OZON-команду: "
                            f"{exc}",
                        )
                        return
                await service.handle_message(chat_id, text)

            async def callback_handler(
                chat_id: int,
                message_id: int,
                data: str,
                message_text: str = "",
            ) -> None:
                if (
                    ozon is not None
                    and await ozon.handle_callback(
                        chat_id,
                        message_id,
                        data,
                        message_text,
                    )
                ):
                    return
                if await orders.handle_callback(
                    chat_id,
                    message_id,
                    data,
                    message_text,
                ):
                    return
                await service.handle_callback(
                    chat_id,
                    message_id,
                    data,
                    message_text,
                )

            tasks = [
                asyncio.create_task(
                    tg.polling_loop(
                        message_handler, callback_handler
                    ),
                    name="telegram",
                ),
                asyncio.create_task(
                    service.fbs_loop(), name="fbs-monitor"
                ),
                asyncio.create_task(
                    service.wb_loop(), name="wb-monitor"
                ),
                asyncio.create_task(
                    service.catalog_loop(),
                    name="catalog-refresh",
                ),
                asyncio.create_task(
                    orders.loop(service.reconcile_after_gap),
                    name="fbs-orders",
                ),
                asyncio.create_task(
                    shared_inventory.loop(),
                    name="shared-inventory",
                ),
            ]
            if ozon is not None:
                tasks.extend(
                    [
                        asyncio.create_task(
                            ozon.order_loop(),
                            name="ozon-orders",
                        ),
                        asyncio.create_task(
                            ozon.stock_loop(),
                            name="ozon-stocks",
                        ),
                    ]
                )

            stop_event = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(
                        sig, stop_event.set
                    )
                except NotImplementedError:
                    pass

            try:
                await stop_event.wait()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(
                    *tasks, return_exceptions=True
                )
                await crm.stop()
    finally:
        db.close()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
