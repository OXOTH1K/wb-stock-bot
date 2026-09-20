from __future__ import annotations

import asyncio
import logging
import signal

from .config import Settings
from .crm import CRMServer
from .db import StateDB
from .orders import OrderMonitor
from .service import StockMonitorService
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
        async with WildberriesClient(
            settings.wb_token, settings.http_timeout
        ) as wb, TelegramBot(
            settings.telegram_bot_token
        ) as tg:
            service = StockMonitorService(settings, wb, tg, db)
            await service.initialize()
            if service.warehouse is None:
                raise RuntimeError("Seller warehouse is not initialized")

            orders = OrderMonitor(settings, wb, tg, db, service.warehouse.id)
            await orders.poll_once(service.reconcile_after_gap)

            crm = CRMServer(settings, service, db)
            await crm.start()

            async def message_handler(chat_id: int, text: str) -> None:
                command = (text.split(maxsplit=1)[0] if text.strip() else "").split("@", 1)[0].lower()
                if command == "/status" and chat_id in settings.telegram_chat_ids:
                    await tg.send_message(chat_id, "🔎 Проверяю заказы и остатки…")
                    try:
                        order_count = await orders.audit_pending(chat_id)
                        stock_count, wb_note = await service.audit_actionable_stocks(chat_id)
                        summary = (
                            service._format_status()
                            + "\n\n"
                            + f"Необработанных новых заказов: {order_count}\n"
                            + f"Ситуаций по остаткам, требующих решения: {stock_count}"
                        )
                        if wb_note:
                            summary += "\n" + wb_note
                        await tg.send_message(chat_id, summary)
                    except Exception as exc:
                        logging.getLogger(__name__).exception("Status audit failed")
                        await tg.send_message(chat_id, f"⚠️ Не удалось выполнить полную сверку: {exc}")
                    return
                await service.handle_message(chat_id, text)

            async def callback_handler(
                chat_id: int, message_id: int, data: str, message_text: str = ""
            ) -> None:
                if await orders.handle_callback(chat_id, message_id, data, message_text):
                    return
                await service.handle_callback(chat_id, message_id, data, message_text)

            tasks = [
                asyncio.create_task(
                    tg.polling_loop(message_handler, callback_handler),
                    name="telegram",
                ),
                asyncio.create_task(service.fbs_loop(), name="fbs-monitor"),
                asyncio.create_task(service.wb_loop(), name="wb-monitor"),
                asyncio.create_task(service.catalog_loop(), name="catalog-refresh"),
                asyncio.create_task(
                    orders.loop(service.reconcile_after_gap), name="fbs-orders"
                ),
            ]

            stop_event = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, stop_event.set)
                except NotImplementedError:
                    pass

            try:
                await stop_event.wait()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await crm.stop()
    finally:
        db.close()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
