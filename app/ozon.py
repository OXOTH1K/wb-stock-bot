from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from .config import Settings
from .db import StateDB
from .inventory_sync import SharedInventory
from .ozon_client import OzonClient, OzonPosting
from .telegram import TelegramBot

log = logging.getLogger(__name__)


class OzonIntegration:
    def __init__(
        self,
        settings: Settings,
        client: OzonClient,
        tg: TelegramBot,
        db: StateDB,
        inventory: SharedInventory | None = None,
    ):
        self.settings = settings
        self.client = client
        self.tg = tg
        self.db = db
        self.inventory = inventory
        self._lock = asyncio.Lock()
        self.current_pending: dict[str, OzonPosting] = {}
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self.db.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ozon_order_state (
                posting_number TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.db.conn.commit()

    def _state(self, posting_number: str) -> str | None:
        row = self.db.conn.execute(
            """
            SELECT status
            FROM ozon_order_state
            WHERE posting_number = ?
            """,
            (str(posting_number),),
        ).fetchone()
        return None if row is None else str(row[0])

    def _set_state(self, posting_number: str, status: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.db.conn:
            self.db.conn.execute(
                """
                INSERT INTO ozon_order_state(
                    posting_number, status, first_seen_at, updated_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(posting_number) DO UPDATE SET
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (str(posting_number), str(status), now, now),
            )

    def _remember(self, posting_number: str) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        with self.db.conn:
            cur = self.db.conn.execute(
                """
                INSERT OR IGNORE INTO ozon_order_state(
                    posting_number, status, first_seen_at, updated_at
                )
                VALUES (?, 'notified', ?, ?)
                """,
                (str(posting_number), now, now),
            )
        return cur.rowcount == 1

    async def initialize(self) -> None:
        await self.refresh_catalog_and_stocks()
        await self.refresh_orders()

    async def refresh_catalog_and_stocks(self) -> None:
        catalog, breakdown = await asyncio.gather(
            self.client.get_catalog(),
            self.client.get_stock_breakdown(),
        )
        fbs_stocks, fbo_stocks = breakdown

        previous_fbo = self.db.get_channel_stock(
            "ozon_fbo",
            tuple(
                product.offer_id
                for product in catalog
                if product.offer_id
            ),
        )

        self.db.replace_channel_catalog(
            "ozon",
            [
                (
                    product.offer_id,
                    product.name,
                    str(product.product_id),
                )
                for product in catalog
            ],
        )
        self.db.replace_channel_stock("ozon_fbs", fbs_stocks)
        self.db.replace_channel_stock("ozon_fbo", fbo_stocks)

        await self._handle_fbo_transitions(
            previous_fbo,
            fbo_stocks,
            fbs_stocks,
        )

    async def _apply_sale(self, posting: OzonPosting) -> None:
        if self.inventory is None:
            return
        try:
            await self.inventory.apply_sale(
                "ozon",
                posting.posting_number,
                [
                    (product.offer_id, product.quantity)
                    for product in posting.products
                    if product.offer_id and product.quantity > 0
                ],
            )
        except Exception:
            log.exception(
                "Could not apply shared inventory sale for Ozon posting %s",
                posting.posting_number,
            )

    def _catalog_external_id(self, sku: str) -> str | None:
        row = self.db.get_channel_catalog("ozon").get(sku)
        if not row:
            return None
        value = str(row.get("external_id") or "").strip()
        return value or None

    async def _handle_fbo_transitions(
        self,
        previous_fbo: dict[str, int],
        current_fbo: dict[str, int],
        current_fbs: dict[str, int],
    ) -> None:
        if self.inventory is None:
            return

        for sku, new_qty in current_fbo.items():
            if sku not in previous_fbo:
                continue
            old_qty = int(previous_fbo.get(sku, 0))
            new_qty = int(new_qty)

            if old_qty == 0 and new_qty > 0:
                if (
                    int(current_fbs.get(sku, 0)) <= 0
                    or self.inventory.is_suppressed("ozon", sku)
                ):
                    continue
                external_id = self._catalog_external_id(sku)
                if not external_id:
                    continue
                local = self.inventory.local_quantity(sku)
                await self.tg.broadcast(
                    self.settings.telegram_chat_ids,
                    (
                        "🔵 Товар появился на складе OZON\n"
                        f"Артикул продавца: {sku}\n"
                        f"Основной склад: {local} шт.\n"
                        f"OZON FBO: было {old_qty} шт. → стало {new_qty} шт.\n\n"
                        "Обнулить FBS только на OZON?"
                    ),
                    reply_markup={
                        "inline_keyboard": [[
                            {
                                "text": "Обнулить OZON FBS",
                                "callback_data": (
                                    f"ozstock:{external_id}:zero"
                                ),
                            },
                            {
                                "text": "Не обнулять",
                                "callback_data": (
                                    f"ozstock:{external_id}:skipzero"
                                ),
                            },
                        ]]
                    },
                )
                continue

            if (
                old_qty > 0
                and new_qty == 0
                and self.inventory.is_suppressed("ozon", sku)
            ):
                external_id = self._catalog_external_id(sku)
                if not external_id:
                    continue
                local = self.inventory.local_quantity(sku)
                await self.tg.broadcast(
                    self.settings.telegram_chat_ids,
                    (
                        "🔵 Товар закончился на складе OZON\n"
                        f"Артикул продавца: {sku}\n"
                        "OZON FBO: 0 шт.\n"
                        f"Актуальный основной склад: {local} шт.\n\n"
                        "Вернуть этот актуальный остаток на OZON FBS?"
                    ),
                    reply_markup={
                        "inline_keyboard": [[
                            {
                                "text": f"Вернуть {local} шт.",
                                "callback_data": (
                                    f"ozstock:{external_id}:restore"
                                ),
                            },
                            {
                                "text": "Не возвращать",
                                "callback_data": (
                                    f"ozstock:{external_id}:skiprestore"
                                ),
                            },
                        ]]
                    },
                )

    @staticmethod
    def _keyboard(posting: OzonPosting) -> dict:
        return {
            "inline_keyboard": [
                [
                    {
                        "text": "✅ Собрать",
                        "callback_data": (
                            f"ozonord:{posting.posting_number}:ship"
                        ),
                    }
                ],
                [
                    {
                        "text": "Не собирать",
                        "callback_data": (
                            f"ozonord:{posting.posting_number}:skip"
                        ),
                    }
                ],
            ]
        }

    @staticmethod
    def _text(posting: OzonPosting) -> str:
        lines = [
            "🔵 Новый FBS-заказ OZON",
            f"Отправление: {posting.posting_number}",
        ]
        if posting.order_number:
            lines.append(f"Заказ: {posting.order_number}")
        if posting.cutoff:
            lines.append(f"Собрать до: {posting.cutoff}")
        lines.extend(["", "Товары:"])
        for product in posting.products[:20]:
            article = product.offer_id or "—"
            lines.append(
                f"• {article} — {product.name} × {product.quantity}"
            )
        if len(posting.products) > 20:
            lines.append(f"… ещё позиций: {len(posting.products) - 20}")
        lines.extend(
            [
                "",
                "На OZON поставку создавать не нужно: "
                "кнопка ниже сразу переводит отправление в сборку.",
            ]
        )
        return "\n".join(lines)

    async def _notify(self, posting: OzonPosting) -> None:
        await self.tg.broadcast(
            self.settings.telegram_chat_ids,
            self._text(posting),
            reply_markup=self._keyboard(posting),
        )

    async def refresh_orders(self) -> None:
        postings = await self.client.get_awaiting_packaging()
        self.current_pending = {
            posting.posting_number: posting
            for posting in postings
        }
        for posting in postings:
            if self._state(posting.posting_number) is not None:
                continue
            await self._apply_sale(posting)
            await self._notify(posting)
            self._remember(posting.posting_number)

    async def audit_pending(self, chat_id: int) -> int:
        postings = await self.client.get_awaiting_packaging()
        self.current_pending = {
            posting.posting_number: posting
            for posting in postings
        }
        pending: list[OzonPosting] = []
        for posting in postings:
            state = self._state(posting.posting_number)
            if state in {"assembled", "skipped"}:
                continue
            pending.append(posting)

        for posting in pending:
            if self._state(posting.posting_number) is None:
                await self._apply_sale(posting)
            await self.tg.send_message(
                chat_id,
                "🔎 /status: OZON-заказ требует решения\n\n"
                + self._text(posting),
                reply_markup=self._keyboard(posting),
            )
            if self._state(posting.posting_number) is None:
                self._remember(posting.posting_number)
        return len(pending)

    async def _finish(
        self,
        chat_id: int,
        message_id: int,
        original: str,
        status: str,
    ) -> None:
        text = (
            f"{original.rstrip()}\n\n{status}"
            if original.strip()
            else status
        )
        try:
            await self.tg.edit_message_text(
                chat_id,
                message_id,
                text,
                reply_markup={"inline_keyboard": []},
            )
        except Exception:
            await self.tg.send_message(chat_id, status)

    async def _handle_stock_callback(
        self,
        chat_id: int,
        message_id: int,
        data: str,
        original: str,
    ) -> bool:
        if self.inventory is None:
            return True
        _, external_id, action = data.split(":", 2)
        sku = self.db.find_channel_sku_by_external_id(
            "ozon", external_id
        )
        if not sku:
            await self._finish(
                chat_id,
                message_id,
                original,
                "⚠️ Товар OZON больше не найден в каталоге.",
            )
            return True

        fbo = self.db.get_channel_stock(
            "ozon_fbo", (sku,)
        ).get(sku, 0)

        if action == "skipzero":
            await self._finish(
                chat_id,
                message_id,
                original,
                "⏭ OZON FBS оставлен без изменений.",
            )
            return True
        if action == "skiprestore":
            await self._finish(
                chat_id,
                message_id,
                original,
                "⏭ OZON FBS пока остаётся обнулённым.",
            )
            return True
        if action == "zero":
            if int(fbo) <= 0:
                await self._finish(
                    chat_id,
                    message_id,
                    original,
                    "⚠️ Обнуление отменено: на OZON FBO уже нет остатка.",
                )
                return True
            await self.inventory.suppress_channel("ozon", sku)
            await self._finish(
                chat_id,
                message_id,
                original,
                "✅ OZON FBS обнулён. WB FBS не изменялся.",
            )
            return True
        if action == "restore":
            if int(fbo) > 0:
                await self._finish(
                    chat_id,
                    message_id,
                    original,
                    "⚠️ Восстановление отменено: товар снова есть на OZON FBO.",
                )
                return True
            quantity = await self.inventory.restore_channel(
                "ozon", sku
            )
            await self._finish(
                chat_id,
                message_id,
                original,
                (
                    "✅ На OZON FBS восстановлен актуальный "
                    f"остаток основного склада: {quantity} шт."
                ),
            )
            return True
        return True

    async def handle_callback(
        self,
        chat_id: int,
        message_id: int,
        data: str,
        original: str = "",
    ) -> bool:
        if not (
            data.startswith("ozonord:")
            or data.startswith("ozstock:")
        ):
            return False
        if chat_id not in self.settings.telegram_chat_ids:
            return True

        try:
            async with self._lock:
                if data.startswith("ozstock:"):
                    return await self._handle_stock_callback(
                        chat_id,
                        message_id,
                        data,
                        original,
                    )

                _, posting_number, action = data.split(":", 2)
                state = self._state(posting_number)
                if state == "assembled":
                    await self._finish(
                        chat_id,
                        message_id,
                        original,
                        "ℹ️ OZON-заказ уже собран.",
                    )
                    return True
                if state == "skipped":
                    await self._finish(
                        chat_id,
                        message_id,
                        original,
                        "ℹ️ Для OZON-заказа уже выбрано «Не собирать».",
                    )
                    return True

                if action == "skip":
                    self._set_state(posting_number, "skipped")
                    await self._finish(
                        chat_id,
                        message_id,
                        original,
                        "⏭ Решение: OZON-заказ не собирать через бота.",
                    )
                    return True

                if action != "ship":
                    return True

                posting = await self.client.get_posting(posting_number)
                if posting.status != "awaiting_packaging":
                    self.current_pending.pop(posting_number, None)
                    self._set_state(
                        posting_number,
                        f"processed:{posting.status or 'unknown'}",
                    )
                    await self._finish(
                        chat_id,
                        message_id,
                        original,
                        (
                            "ℹ️ Отправление уже не ожидает сборки. "
                            f"Текущий статус OZON: {posting.status or 'unknown'}."
                        ),
                    )
                    return True

                await self.client.ship_fbs(posting)
                self.current_pending.pop(posting_number, None)
                self._set_state(posting_number, "assembled")
                await self._finish(
                    chat_id,
                    message_id,
                    original,
                    (
                        "✅ OZON-заказ собран. "
                        "Отправление переведено в ожидание отгрузки."
                    ),
                )
                return True
        except Exception as exc:
            log.exception("Ozon order callback failed: %s", data)
            await self.tg.send_message(
                chat_id,
                f"⚠️ Не удалось обработать OZON-заказ: {exc}",
            )
            return True

    async def order_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.ozon_order_check_interval)
            try:
                await self.refresh_orders()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Ozon FBS order refresh failed")

    async def stock_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.ozon_stock_check_interval)
            try:
                await self.refresh_catalog_and_stocks()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Ozon catalog/stock refresh failed")
