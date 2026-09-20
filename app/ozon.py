from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .config import Settings
from .db import StateDB
from .ozon_client import OzonClient, OzonPosting
from .telegram import TelegramBot

if TYPE_CHECKING:
    from .shared_inventory import SharedInventoryService

log = logging.getLogger(__name__)


class OzonIntegration:
    def __init__(
        self,
        settings: Settings,
        client: OzonClient,
        tg: TelegramBot,
        db: StateDB,
    ):
        self.settings = settings
        self.client = client
        self.tg = tg
        self.db = db
        self._lock = asyncio.Lock()
        self.current_pending: dict[str, OzonPosting] = {}
        self.inventory: SharedInventoryService | None = None
        self.warehouse_id: int | None = None
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

    def set_shared_inventory(
        self, inventory: "SharedInventoryService"
    ) -> None:
        self.inventory = inventory

    async def _resolve_warehouse(self) -> int | None:
        if self.warehouse_id is not None:
            return self.warehouse_id
        configured = getattr(
            self.settings, "ozon_warehouse_id", None
        )
        if configured is not None:
            self.warehouse_id = int(configured)
            return self.warehouse_id

        warehouses = await self.client.get_fbs_warehouses()
        if len(warehouses) == 1:
            self.warehouse_id = int(
                warehouses[0]["warehouse_id"]
            )
            log.info(
                "Ozon FBS warehouse auto-selected: %s (%s)",
                warehouses[0].get("name") or "—",
                self.warehouse_id,
            )
            return self.warehouse_id
        if len(warehouses) > 1:
            log.warning(
                "Multiple Ozon FBS/rFBS warehouses found; "
                "set OZON_WAREHOUSE_ID to enable stock writes"
            )
        else:
            log.warning("No Ozon FBS/rFBS warehouse found")
        return None

    async def initialize(self) -> None:
        await self.refresh_catalog_and_stocks()
        await self.refresh_orders()

    async def set_fbs_stock(self, sku: str, quantity: int) -> None:
        warehouse_id = await self._resolve_warehouse()
        if warehouse_id is None:
            raise RuntimeError(
                "OZON_WAREHOUSE_ID is required when more than one "
                "Ozon FBS/rFBS warehouse exists"
            )
        await self.client.set_fbs_stocks(
            warehouse_id, {str(sku): int(quantity)}
        )
        self.db.set_channel_stock(
            "ozon_fbs", str(sku), int(quantity)
        )

    async def refresh_catalog_and_stocks(self) -> None:
        previous_fbo = self.db.get_channel_stock(
            "ozon_fbo",
            tuple(self.db.get_channel_catalog("ozon")),
        )
        catalog, breakdown = await asyncio.gather(
            self.client.get_catalog(),
            self.client.get_stock_breakdown(),
        )
        fbs, fbo = breakdown
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
        self.db.replace_channel_stock("ozon_fbs", fbs)
        self.db.replace_channel_stock("ozon_fbo", fbo)
        try:
            await self._resolve_warehouse()
        except Exception:
            log.exception("Could not resolve Ozon FBS warehouse")

        if self.inventory is not None and previous_fbo:
            await self._notify_stock_transitions(
                previous_fbo, fbo
            )

    def _sku_by_product_id(self, product_id: str) -> str | None:
        for sku, row in self.db.get_channel_catalog("ozon").items():
            if str(row.get("external_id") or "") == str(product_id):
                return sku
        return None

    def _stock_keyboard(
        self, sku: str, action: str
    ) -> dict | None:
        row = self.db.get_channel_catalog("ozon").get(sku)
        if row is None or not row.get("external_id"):
            return None
        product_id = str(row["external_id"])
        if action == "zero":
            buttons = [
                {
                    "text": "Обнулить OZON FBS",
                    "callback_data": f"ozstock:{product_id}:zero",
                },
                {
                    "text": "Не обнулять",
                    "callback_data": f"ozstock:{product_id}:skipzero",
                },
            ]
        else:
            quantity = (
                self.inventory.local_quantity(sku)
                if self.inventory is not None
                else 0
            )
            buttons = [
                {
                    "text": f"Вернуть {quantity} шт. на OZON FBS",
                    "callback_data": f"ozstock:{product_id}:restore",
                },
                {
                    "text": "Не возвращать",
                    "callback_data": f"ozstock:{product_id}:skiprestore",
                },
            ]
        return {"inline_keyboard": [buttons]}

    async def _notify_stock_transitions(
        self,
        previous_fbo: dict[str, int],
        current_fbo: dict[str, int],
    ) -> None:
        if self.inventory is None:
            return
        fbs = self.db.get_channel_stock(
            "ozon_fbs", tuple(current_fbo)
        )
        for sku, new_qty in current_fbo.items():
            old_qty = previous_fbo.get(sku)
            if old_qty is None:
                continue
            local = self.inventory.local_quantity(sku)
            if old_qty == 0 and new_qty > 0:
                if (
                    local <= 0
                    or int(fbs.get(sku, 0)) <= 0
                    or self.inventory.is_suppressed("ozon", sku)
                ):
                    continue
                keyboard = self._stock_keyboard(sku, "zero")
                if keyboard is None:
                    continue
                await self.tg.broadcast(
                    self.settings.telegram_chat_ids,
                    (
                        "🟦 Товар появился на складе OZON\n"
                        f"Артикул продавца: {sku}\n"
                        f"Основной склад: {local} шт.\n"
                        f"Склад OZON: было 0 шт. → стало {new_qty} шт.\n\n"
                        "Обнулить только OZON FBS? "
                        "WB FBS останется равным основному складу."
                    ),
                    reply_markup=keyboard,
                )
                continue

            if (
                old_qty > 0
                and new_qty == 0
                and self.db.get_channel_suppression_reason(
                    "ozon", sku
                ) == "marketplace_stock"
            ):
                if local <= 0:
                    self.db.clear_channel_suppressed(
                        "ozon", sku
                    )
                    continue
                keyboard = self._stock_keyboard(
                    sku, "restore"
                )
                if keyboard is None:
                    continue
                await self.tg.broadcast(
                    self.settings.telegram_chat_ids,
                    (
                        "🔵 Товар закончился на складе OZON\n"
                        f"Артикул продавца: {sku}\n"
                        f"Актуальный остаток основного склада: {local} шт.\n\n"
                        "Вернуть этот актуальный остаток на OZON FBS?"
                    ),
                    reply_markup=keyboard,
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
            "🟦 Новый FBS-заказ OZON",
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
        unseen = [
            posting
            for posting in postings
            if self._state(posting.posting_number) is None
        ]
        if unseen and self.inventory is not None:
            await self.inventory.consume_ozon_postings(unseen)
        for posting in unseen:
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

        if data.startswith("ozstock:"):
            try:
                if self.inventory is None:
                    return True
                _, product_id, action = data.split(":", 2)
                sku = self._sku_by_product_id(product_id)
                if not sku:
                    await self._finish(
                        chat_id,
                        message_id,
                        original,
                        "⚠️ Товар OZON больше не найден в каталоге.",
                    )
                    return True
                if action in {"skipzero", "skiprestore"}:
                    await self._finish(
                        chat_id,
                        message_id,
                        original,
                        "⏭ Остаток OZON FBS оставлен без изменений.",
                    )
                    return True

                current_fbo = self.db.get_channel_stock(
                    "ozon_fbo", (sku,)
                ).get(sku, 0)
                if action == "zero":
                    if int(current_fbo) <= 0:
                        await self._finish(
                            chat_id,
                            message_id,
                            original,
                            "ℹ️ Обнуление отменено: на складе OZON уже 0.",
                        )
                        return True
                    await self.inventory.suppress_channel(
                        "ozon", sku, "marketplace_stock"
                    )
                    await self._finish(
                        chat_id,
                        message_id,
                        original,
                        (
                            "✅ OZON FBS обнулён. "
                            "WB FBS и основной склад не изменены."
                        ),
                    )
                    return True
                if action == "restore":
                    if int(current_fbo) > 0:
                        await self._finish(
                            chat_id,
                            message_id,
                            original,
                            "ℹ️ Восстановление отменено: товар снова есть на складе OZON.",
                        )
                        return True
                    quantity = await self.inventory.restore_channel(
                        "ozon", sku
                    )
                    await self._finish(
                        chat_id,
                        message_id,
                        original,
                        f"✅ На OZON FBS возвращён актуальный остаток: {quantity} шт.",
                    )
                    return True
                return True
            except Exception as exc:
                log.exception("Ozon stock callback failed: %s", data)
                await self.tg.send_message(
                    chat_id,
                    f"⚠️ Не удалось изменить OZON FBS: {exc}",
                )
                return True

        try:
            async with self._lock:
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
