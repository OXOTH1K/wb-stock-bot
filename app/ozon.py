from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from .config import Settings
from .db import StateDB
from .inventory_sync import SharedStockSync
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
        stock_sync: SharedStockSync | None = None,
    ):
        self.settings = settings
        self.client = client
        self.tg = tg
        self.db = db
        self.stock_sync = stock_sync
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
        catalog, stock_levels = await asyncio.gather(
            self.client.get_catalog(),
            self.client.get_stock_levels(),
        )
        fbs_stocks, fbo_stocks = stock_levels
        skus = tuple(
            sorted(
                {
                    product.offer_id
                    for product in catalog
                    if product.offer_id
                }
                | set(fbs_stocks)
                | set(fbo_stocks)
            )
        )
        previous_fbo = self.db.get_channel_stock(
            "ozon_fbo", skus
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

        if self.stock_sync is not None:
            await self._notify_fbo_transitions(
                previous_fbo,
                fbo_stocks,
                fbs_stocks,
            )
            await self.stock_sync.flush_pending(channel="ozon")

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
            "🔵 OZON · Новый FBS-заказ",
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

    def _sku_by_product_id(self, product_id: str) -> str:
        target = str(product_id)
        for sku, row in self.db.get_channel_catalog("ozon").items():
            if str(row.get("external_id") or "") == target:
                return sku
        return ""

    def _product_id_for_sku(self, sku: str) -> str:
        row = self.db.get_channel_catalog("ozon").get(sku) or {}
        return str(row.get("external_id") or "")

    def _ozon_zero_keyboard(self, sku: str) -> dict | None:
        product_id = self._product_id_for_sku(sku)
        if not product_id:
            return None
        return {
            "inline_keyboard": [[
                {
                    "text": "Обнулить OZON FBS",
                    "callback_data": f"ozonfbszero:{product_id}:yes",
                },
                {
                    "text": "Не обнулять",
                    "callback_data": f"ozonfbszero:{product_id}:skip",
                },
            ]]
        }

    def _ozon_restore_keyboard(
        self, sku: str, quantity: int
    ) -> dict | None:
        product_id = self._product_id_for_sku(sku)
        if not product_id:
            return None
        return {
            "inline_keyboard": [[
                {
                    "text": f"Вернуть {quantity} шт. на OZON FBS",
                    "callback_data": f"ozonfbsrestore:{product_id}:yes",
                },
                {
                    "text": "Не возвращать",
                    "callback_data": f"ozonfbsrestore:{product_id}:skip",
                },
            ]]
        }

    async def _notify_fbo_transitions(
        self,
        previous: dict[str, int],
        current: dict[str, int],
        fbs: dict[str, int],
    ) -> None:
        if self.stock_sync is None:
            return
        all_skus = set(previous) | set(current)
        for sku in sorted(all_skus):
            if sku not in previous:
                continue
            old_qty = int(previous.get(sku, 0))
            new_qty = int(current.get(sku, 0))
            suppressed = self.stock_sync.is_suppressed(
                "ozon", sku
            )

            if (
                old_qty == 0
                and new_qty > 0
                and int(fbs.get(sku, 0)) > 0
                and not suppressed
            ):
                local_qty = self.stock_sync.ensure_local(sku)
                keyboard = self._ozon_zero_keyboard(sku)
                if keyboard is None:
                    continue
                await self.tg.broadcast(
                    self.settings.telegram_chat_ids,
                    (
                        "🔵 Товар появился на складе OZON\n"
                        f"Артикул продавца: {sku}\n"
                        f"Локальный склад: {local_qty} шт.\n"
                        f"OZON FBS: {int(fbs.get(sku, 0))} шт.\n"
                        f"Склад OZON: было 0 шт. → стало {new_qty} шт.\n\n"
                        "Обнулить только OZON FBS?"
                    ),
                    reply_markup=keyboard,
                )
                continue

            if old_qty > 0 and new_qty == 0 and suppressed:
                local_qty = self.stock_sync.ensure_local(sku)
                if local_qty == 0:
                    self.db.set_channel_suppressed(
                        "ozon", sku, False, reason=""
                    )
                    continue
                keyboard = self._ozon_restore_keyboard(
                    sku, local_qty
                )
                if keyboard is None:
                    continue
                await self.tg.broadcast(
                    self.settings.telegram_chat_ids,
                    (
                        "🔵 Товар закончился на складе OZON\n"
                        f"Артикул продавца: {sku}\n"
                        "Склад OZON: 0 шт.\n"
                        f"Текущий локальный остаток: {local_qty} шт.\n\n"
                        "Вернуть актуальный локальный остаток "
                        "только на OZON FBS?"
                    ),
                    reply_markup=keyboard,
                )

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
            if self.stock_sync is not None:
                items: dict[str, int] = {}
                for product in posting.products:
                    sku = str(product.offer_id or "").strip()
                    if not sku or product.quantity <= 0:
                        continue
                    items[sku] = (
                        items.get(sku, 0)
                        + int(product.quantity)
                    )
                await self.stock_sync.apply_order(
                    "ozon",
                    posting.posting_number,
                    items,
                )
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
            or data.startswith("ozonfbszero:")
            or data.startswith("ozonfbsrestore:")
        ):
            return False
        if chat_id not in self.settings.telegram_chat_ids:
            return True

        try:
            async with self._lock:
                if data.startswith("ozonfbszero:"):
                    _, product_id, action = data.split(":", 2)
                    sku = self._sku_by_product_id(product_id)
                    if not sku or self.stock_sync is None:
                        await self._finish(
                            chat_id,
                            message_id,
                            original,
                            "⚠️ Не удалось определить товар OZON.",
                        )
                        return True
                    if action == "skip":
                        await self._finish(
                            chat_id,
                            message_id,
                            original,
                            "⏭ Решение: OZON FBS оставить без изменений.",
                        )
                        return True
                    fbo_qty = self.db.get_channel_stock(
                        "ozon_fbo", (sku,)
                    ).get(sku, 0)
                    if int(fbo_qty) <= 0:
                        await self._finish(
                            chat_id,
                            message_id,
                            original,
                            "ℹ️ Обнуление отменено: на складе OZON "
                            "этого товара уже нет.",
                        )
                        return True
                    await self.stock_sync.suppress_channel(
                        "ozon", sku
                    )
                    await self._finish(
                        chat_id,
                        message_id,
                        original,
                        "✅ Обнулён только OZON FBS. "
                        "WB FBS и локальный склад не изменены.",
                    )
                    return True

                if data.startswith("ozonfbsrestore:"):
                    _, product_id, action = data.split(":", 2)
                    sku = self._sku_by_product_id(product_id)
                    if not sku or self.stock_sync is None:
                        await self._finish(
                            chat_id,
                            message_id,
                            original,
                            "⚠️ Не удалось определить товар OZON.",
                        )
                        return True
                    if action == "skip":
                        await self._finish(
                            chat_id,
                            message_id,
                            original,
                            "⏭ Решение: OZON FBS пока не восстанавливать.",
                        )
                        return True
                    fbo_qty = self.db.get_channel_stock(
                        "ozon_fbo", (sku,)
                    ).get(sku, 0)
                    if int(fbo_qty) > 0:
                        await self._finish(
                            chat_id,
                            message_id,
                            original,
                            "ℹ️ Восстановление отменено: товар снова "
                            "есть на складе OZON.",
                        )
                        return True
                    quantity = await self.stock_sync.restore_channel(
                        "ozon", sku
                    )
                    await self._finish(
                        chat_id,
                        message_id,
                        original,
                        f"✅ На OZON FBS восстановлено {quantity} шт. "
                        "из актуального локального остатка.",
                    )
                    return True

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
