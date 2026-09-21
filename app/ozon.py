from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timezone
from html import escape
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
        self.db.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ozon_stock_decision (
                sku TEXT NOT NULL,
                action TEXT NOT NULL,
                fbs_qty INTEGER NOT NULL,
                fbo_qty INTEGER NOT NULL,
                decision TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (sku, action)
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

    def _save_stock_decision(
        self,
        sku: str,
        action: str,
        fbs_qty: int,
        fbo_qty: int,
        decision: str,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.db.conn:
            self.db.conn.execute(
                """
                INSERT INTO ozon_stock_decision(
                    sku, action, fbs_qty, fbo_qty, decision, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(sku, action) DO UPDATE SET
                    fbs_qty = excluded.fbs_qty,
                    fbo_qty = excluded.fbo_qty,
                    decision = excluded.decision,
                    updated_at = excluded.updated_at
                """,
                (
                    str(sku),
                    str(action),
                    int(fbs_qty),
                    int(fbo_qty),
                    str(decision),
                    now,
                ),
            )

    def _stock_decision_matches(
        self,
        sku: str,
        action: str,
        fbs_qty: int,
        fbo_qty: int,
        decision: str = "skip",
    ) -> bool:
        row = self.db.conn.execute(
            """
            SELECT fbs_qty, fbo_qty, decision
            FROM ozon_stock_decision
            WHERE sku = ? AND action = ?
            """,
            (str(sku), str(action)),
        ).fetchone()
        return bool(
            row is not None
            and int(row[0]) == int(fbs_qty)
            and int(row[1]) == int(fbo_qty)
            and str(row[2]) == str(decision)
        )

    def _clear_stock_decisions(self, sku: str) -> None:
        with self.db.conn:
            self.db.conn.execute(
                "DELETE FROM ozon_stock_decision WHERE sku = ?",
                (str(sku),),
            )

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

        # Ozon returns archived/blocked/etc. warehouses together with
        # active ones. In Seller API status="created" means the warehouse
        # is active; archived warehouses use status="disabled".
        status_known = any(
            str(row.get("status") or "").strip()
            for row in warehouses
        )
        candidates = (
            [
                row
                for row in warehouses
                if str(row.get("status") or "").strip().lower()
                == "created"
            ]
            if status_known
            else warehouses
        )

        if len(candidates) == 1:
            self.warehouse_id = int(
                candidates[0]["warehouse_id"]
            )
            log.info(
                "Ozon active FBS warehouse auto-selected: %s (%s)",
                candidates[0].get("name") or "—",
                self.warehouse_id,
            )
            return self.warehouse_id
        if len(candidates) > 1:
            log.warning(
                "Multiple active Ozon FBS/rFBS warehouses found; "
                "set OZON_WAREHOUSE_ID to enable stock writes"
            )
        elif status_known and warehouses:
            log.warning(
                "No active Ozon FBS/rFBS warehouse found "
                "(status=created)"
            )
        else:
            log.warning("No Ozon FBS/rFBS warehouse found")
        return None

    async def initialize(self) -> None:
        await self.refresh_catalog_and_stocks()
        await self.refresh_orders()

    async def set_fbs_stocks(
        self, quantities: dict[str, int]
    ) -> None:
        clean = {
            str(sku): int(quantity)
            for sku, quantity in quantities.items()
        }
        if not clean:
            return
        warehouse_id = await self._resolve_warehouse()
        if warehouse_id is None:
            raise RuntimeError(
                "Could not auto-select an active Ozon FBS/rFBS "
                "warehouse; set OZON_WAREHOUSE_ID if more than one "
                "active warehouse exists"
            )
        await self.client.set_fbs_stocks(
            warehouse_id, clean
        )
        for sku, quantity in clean.items():
            self.db.set_channel_stock(
                "ozon_fbs", sku, quantity
            )

    async def set_fbs_stock(self, sku: str, quantity: int) -> None:
        await self.set_fbs_stocks(
            {str(sku): int(quantity)}
        )

    async def refresh_catalog_and_stocks(
        self, notify: bool = True
    ) -> None:
        previous_catalog = self.db.get_channel_catalog("ozon")
        previous_skus = tuple(previous_catalog)
        previous_fbs = self.db.get_channel_stock(
            "ozon_fbs", previous_skus
        )
        previous_fbo = self.db.get_channel_stock(
            "ozon_fbo", previous_skus
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

        if (
            notify
            and self.inventory is not None
            and (previous_fbs or previous_fbo)
        ):
            await self._notify_stock_transitions(
                previous_fbs,
                previous_fbo,
                fbs,
                fbo,
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

    def _stock_snapshot(
        self,
    ) -> tuple[dict[str, dict], dict[str, int], dict[str, int]]:
        catalog = self.db.get_channel_catalog("ozon")
        skus = tuple(catalog)
        return (
            catalog,
            self.db.get_channel_stock("ozon_fbs", skus),
            self.db.get_channel_stock("ozon_fbo", skus),
        )

    def _stock_page_meta(
        self, page: int
    ) -> tuple[list[str], int, int, int]:
        catalog, fbs, fbo = self._stock_snapshot()
        rows = sorted(
            catalog,
            key=lambda sku: (
                int(fbs.get(sku, 0)) + int(fbo.get(sku, 0)) > 0,
                sku.lower(),
            ),
        )
        page_size = max(
            1, int(getattr(self.settings, "stocks_page_size", 20))
        )
        pages = max(1, math.ceil(len(rows) / page_size))
        page = min(max(1, int(page)), pages)
        selected = rows[
            (page - 1) * page_size : page * page_size
        ]
        return selected, page, pages, len(rows)

    def _format_stocks_page(self, page: int) -> str:
        catalog, fbs, fbo = self._stock_snapshot()
        selected, page, pages, total = self._stock_page_meta(page)
        name_width = 32
        table = [
            f"   {'Артикул':<{name_width}} {'FBS':>4} {'FBO':>4}"
        ]
        for sku in selected:
            fbs_qty = int(fbs.get(sku, 0))
            fbo_qty = int(fbo.get(sku, 0))
            if fbs_qty > 0 and fbo_qty > 0:
                marker = "🟣"
            elif fbs_qty > 0 or fbo_qty > 0:
                marker = "🟢"
            else:
                marker = "🔴"
            raw_name = sku or str(
                catalog.get(sku, {}).get("title") or "без артикула"
            )
            if len(raw_name) > name_width:
                raw_name = raw_name[: name_width - 1] + "…"
            table.append(
                f"{marker} {raw_name:<{name_width}} "
                f"{fbs_qty:>4} {fbo_qty:>4}"
            )
        escaped_table = escape("\n".join(table))
        return (
            f"🟦 <b>Остатки OZON</b> — {page}/{pages} · "
            f"товаров: {total}\n\n"
            f"<pre>{escaped_table}</pre>\n"
            "<i>FBO — остаток на складе OZON.</i>"
        )

    def _stocks_keyboard(self, page: int) -> dict:
        _, page, pages, _ = self._stock_page_meta(page)
        buttons = []
        if page > 1:
            buttons.append(
                {
                    "text": "◀️",
                    "callback_data": f"ozstocks:{page - 1}",
                }
            )
        buttons.append(
            {
                "text": f"{page}/{pages}",
                "callback_data": "ozstocks:noop",
            }
        )
        if page < pages:
            buttons.append(
                {
                    "text": "▶️",
                    "callback_data": f"ozstocks:{page + 1}",
                }
            )
        return {"inline_keyboard": [buttons]}

    async def _send_stocks_page(
        self, chat_id: int, page: int
    ) -> None:
        _, page, _, _ = self._stock_page_meta(page)
        await self.tg.send_message(
            chat_id,
            self._format_stocks_page(page),
            parse_mode="HTML",
            reply_markup=self._stocks_keyboard(page),
        )

    def _depletion_keyboard(
        self, sku: str
    ) -> dict | None:
        row = self.db.get_channel_catalog("ozon").get(sku)
        if row is None or not row.get("external_id"):
            return None
        product_id = str(row["external_id"])
        return {
            "inline_keyboard": [
                [
                    {
                        "text": "1",
                        "callback_data": f"ozstock:{product_id}:add1",
                    },
                    {
                        "text": "5",
                        "callback_data": f"ozstock:{product_id}:add5",
                    },
                    {
                        "text": "Не добавлять",
                        "callback_data": f"ozstock:{product_id}:skipadd",
                    },
                ]
            ]
        }

    async def _notify_current_depletion(
        self, sku: str
    ) -> None:
        if self.inventory is None:
            return
        fbs_qty = int(
            self.db.get_channel_stock(
                "ozon_fbs", (sku,)
            ).get(sku, 0)
        )
        fbo_qty = int(
            self.db.get_channel_stock(
                "ozon_fbo", (sku,)
            ).get(sku, 0)
        )
        local = self.inventory.local_quantity(sku)
        available = self.inventory.available_quantity(sku)
        reason = self.db.get_channel_suppression_reason(
            "ozon", sku
        )
        any_shared_suppression = (
            self.db.is_channel_suppressed("ozon", sku)
            or self.db.is_channel_suppressed("wb", sku)
        )
        if (
            fbs_qty != 0
            or fbo_qty != 0
            or available > 0
            or any_shared_suppression
        ):
            return
        if self._stock_decision_matches(
            sku,
            "add",
            fbs_qty,
            fbo_qty,
            "notified",
        ):
            return
        keyboard = self._depletion_keyboard(sku)
        if keyboard is None:
            return
        await self.tg.broadcast(
            self.settings.telegram_chat_ids,
            (
                "🔴 Товар закончился в OZON FBS и FBO\n"
                f"Артикул продавца: {sku}\n"
                "OZON FBS: 0 шт. | OZON FBO: 0 шт.\n"
                f"Мой склад: {local} шт.\n"
                "Доступно для заказа: 0 шт.\n\n"
                "Перенести 1 или 5 шт. из «Моего склада» в «Доступно для заказа»?"
            ),
            reply_markup=keyboard,
        )
        self._save_stock_decision(
            sku,
            "add",
            fbs_qty,
            fbo_qty,
            "notified",
        )

    async def _notify_stock_transitions(
        self,
        previous_fbs: dict[str, int],
        previous_fbo: dict[str, int],
        current_fbs: dict[str, int],
        current_fbo: dict[str, int],
    ) -> None:
        if self.inventory is None:
            return
        for sku in sorted(set(current_fbs) | set(current_fbo)):
            old_fbs = previous_fbs.get(sku)
            old_fbo = previous_fbo.get(sku)
            if old_fbs is None or old_fbo is None:
                continue
            new_fbs = int(current_fbs.get(sku, 0))
            new_fbo = int(current_fbo.get(sku, 0))
            if int(old_fbs) != new_fbs or int(old_fbo) != new_fbo:
                self._clear_stock_decisions(sku)

            local = self.inventory.local_quantity(sku)
            available = self.inventory.available_quantity(sku)
            reason = self.db.get_channel_suppression_reason(
                "ozon", sku
            )
            any_shared_suppression = (
                self.db.is_channel_suppressed("ozon", sku)
                or self.db.is_channel_suppressed("wb", sku)
            )

            if (
                int(old_fbo) == 0
                and new_fbo > 0
                and new_fbs > 0
                and available > 0
                and reason is None
            ):
                keyboard = self._stock_keyboard(sku, "zero")
                if (
                    keyboard is not None
                    and not self._stock_decision_matches(
                        sku,
                        "zero",
                        new_fbs,
                        new_fbo,
                        "notified",
                    )
                ):
                    await self.tg.broadcast(
                        self.settings.telegram_chat_ids,
                        (
                            "🟦 Товар появился на складе OZON\n"
                            f"Артикул продавца: {sku}\n"
                            f"Мой склад: {local} шт.\n"
                            f"Доступно для заказа: {available} шт.\n"
                            f"OZON FBS: {new_fbs} шт.\n"
                            f"OZON FBO: было 0 шт. → стало {new_fbo} шт.\n\n"
                            "Обнулить «Доступно для заказа»? "
                            "Тогда WB FBS и OZON FBS станут 0, а товар вернётся на «Мой склад»."
                        ),
                        reply_markup=keyboard,
                    )
                    self._save_stock_decision(
                        sku,
                        "zero",
                        new_fbs,
                        new_fbo,
                        "notified",
                    )

            if (
                int(old_fbs) + int(old_fbo) > 0
                and new_fbs + new_fbo == 0
            ):
                await self._notify_current_depletion(sku)

            if (
                int(old_fbo) > 0
                and new_fbo == 0
                and reason == "marketplace_stock"
                and local > 0
            ):
                restore_qty = int(
                    self.db.get_available_snapshot(
                        "marketplace:ozon"
                    ).get(sku, 0)
                )
                keyboard = (
                    self._stock_keyboard(sku, "restore")
                    if restore_qty > 0
                    else None
                )
                if (
                    keyboard is not None
                    and not self._stock_decision_matches(
                        sku,
                        "restore",
                        new_fbs,
                        new_fbo,
                        "notified",
                    )
                ):
                    await self.tg.broadcast(
                        self.settings.telegram_chat_ids,
                        (
                            "🔵 Товар закончился на складе OZON\n"
                            f"Артикул продавца: {sku}\n"
                            f"Мой склад: {local} шт.\n"
                            f"До обнуления было доступно: {restore_qty} шт.\n\n"
                            "Вернуть сохранённое количество в «Доступно для заказа» и оба FBS?"
                        ),
                        reply_markup=keyboard,
                    )
                    self._save_stock_decision(
                        sku,
                        "restore",
                        new_fbs,
                        new_fbo,
                        "notified",
                    )

    async def audit_actionable_stocks(
        self, chat_id: int
    ) -> int:
        if self.inventory is None:
            return 0
        await self.refresh_catalog_and_stocks(notify=False)
        catalog, fbs, fbo = self._stock_snapshot()
        actionable = 0
        for sku in sorted(catalog):
            fbs_qty = int(fbs.get(sku, 0))
            fbo_qty = int(fbo.get(sku, 0))
            local = self.inventory.local_quantity(sku)
            available = self.inventory.available_quantity(sku)
            reason = self.db.get_channel_suppression_reason(
                "ozon", sku
            )

            if (
                fbs_qty > 0
                and fbo_qty > 0
                and available > 0
                and reason is None
            ):
                if self._stock_decision_matches(
                    sku, "zero", fbs_qty, fbo_qty
                ):
                    continue
                keyboard = self._stock_keyboard(sku, "zero")
                if keyboard is None:
                    continue
                actionable += 1
                await self.tg.send_message(
                    chat_id,
                    (
                        "🔎 /status: товар есть одновременно "
                        "в OZON FBS и FBO\n"
                        f"Артикул продавца: {sku}\n"
                        f"OZON FBS: {fbs_qty} шт. | "
                        f"OZON FBO: {fbo_qty} шт.\n\n"
                        "Обнулить «Доступно для заказа» и оба FBS?"
                    ),
                    reply_markup=keyboard,
                )
                continue

            if fbs_qty == 0 and fbo_qty == 0:
                if reason == "marketplace_stock" and local > 0:
                    restore_qty = int(
                        self.db.get_available_snapshot(
                            "marketplace:ozon"
                        ).get(sku, 0)
                    )
                    if restore_qty <= 0:
                        continue
                    if self._stock_decision_matches(
                        sku, "restore", fbs_qty, fbo_qty
                    ):
                        continue
                    keyboard = self._stock_keyboard(
                        sku, "restore"
                    )
                    if keyboard is None:
                        continue
                    actionable += 1
                    await self.tg.send_message(
                        chat_id,
                        (
                            "🔎 /status: товар закончился на складе OZON\n"
                            f"Артикул продавца: {sku}\n"
                            f"Мой склад: {local} шт.\n"
                            f"Сохранено до обнуления: "
                            f"{restore_qty} шт.\n\n"
                            "Вернуть в «Доступно для заказа» и оба FBS?"
                        ),
                        reply_markup=keyboard,
                    )
                    continue
                if any_shared_suppression:
                    continue
                if available <= 0:
                    if self._stock_decision_matches(
                        sku, "add", fbs_qty, fbo_qty
                    ):
                        continue
                    keyboard = self._depletion_keyboard(sku)
                    if keyboard is None:
                        continue
                    actionable += 1
                    await self.tg.send_message(
                        chat_id,
                        (
                            "🔎 /status: товар закончился "
                            "в OZON FBS и FBO\n"
                            f"Артикул продавца: {sku}\n"
                            f"Мой склад: {local} шт.\n"
                            "Доступно для заказа: 0 шт.\n\n"
                            "Перенести 1 или 5 шт. в «Доступно для заказа»?"
                        ),
                        reply_markup=keyboard,
                    )
        return actionable

    async def handle_message(
        self, chat_id: int, text: str
    ) -> bool:
        parts = text.split()
        if not parts:
            return False
        command = parts[0].split("@", 1)[0].lower()
        args = parts[1:]
        relevant = {
            "/stocks_ozon",
            "/ozon_fbs_zero_all",
            "/ozon_fbs_restore",
            "/fbs_zero_all_ozon",
            "/fbs_restore_ozon",
        }
        if command not in relevant:
            return False
        if chat_id not in self.settings.telegram_chat_ids:
            await self.tg.send_message(chat_id, "Доступ запрещён.")
            return True

        if command == "/stocks_ozon":
            page = 1
            if args:
                try:
                    page = max(1, int(args[0]))
                except ValueError:
                    await self.tg.send_message(
                        chat_id,
                        "Формат: /stocks_ozon или /stocks_ozon 2",
                    )
                    return True
            await self.tg.send_message(
                chat_id, "Обновляю остатки OZON…"
            )
            await self.refresh_catalog_and_stocks()
            await self._send_stocks_page(chat_id, page)
            return True

        if command in {
            "/ozon_fbs_zero_all",
            "/fbs_zero_all_ozon",
        }:
            await self.tg.send_message(
                chat_id,
                (
                    "⚠️ Обнулить «Доступно для заказа» для всех товаров?\n"
                    "Количество вернётся на «Мой склад», а WB/OZON FBS станут 0."
                ),
                reply_markup={
                    "inline_keyboard": [
                        [
                            {
                                "text": "Обнулить OZON FBS",
                                "callback_data": "ozfbsallzero:yes",
                            },
                            {
                                "text": "Отмена",
                                "callback_data": "ozfbsallzero:skip",
                            },
                        ]
                    ]
                },
            )
            return True

        mass_skus = [
            sku
            for sku in self.db.list_channel_suppressions("ozon")
            if self.db.get_channel_suppression_reason(
                "ozon", sku
            )
            == "mass"
        ]
        if not mass_skus:
            await self.tg.send_message(
                chat_id,
                "ℹ️ Массово обнулённых OZON FBS-остатков нет.",
            )
            return True
        saved = self.db.get_available_snapshot(
            "mass_shared"
        )
        total = sum(int(value) for value in saved.values())
        if not saved:
            await self.tg.send_message(
                chat_id,
                "ℹ️ Сохранённого массового значения «Доступно для заказа» нет.",
            )
            return True
        await self.tg.send_message(
            chat_id,
            (
                f"♻️ Восстановить сохранённое «Доступно для заказа»?\n"
                f"Товаров: {len(saved)}, суммарно: {total} шт."
            ),
            reply_markup={
                "inline_keyboard": [
                    [
                        {
                            "text": "Восстановить OZON FBS",
                            "callback_data": "ozfbsallrestore:yes",
                        },
                        {
                            "text": "Отмена",
                            "callback_data": "ozfbsallrestore:skip",
                        },
                    ]
                ]
            },
        )
        return True

    async def _zero_all_fbs(
        self,
        chat_id: int,
        message_id: int,
        original: str,
    ) -> None:
        if self.inventory is None:
            raise RuntimeError(
                "Shared inventory is not initialized"
            )
        await self.refresh_catalog_and_stocks(notify=False)
        _catalog, fbs, _fbo = self._stock_snapshot()
        before = sum(int(value) for value in fbs.values())
        count, _ = await self.inventory.suppress_ozon_mass()
        await self._finish(
            chat_id,
            message_id,
            original,
            (
                "✅ «Доступно для заказа» обнулено.\n"
                "Количество возвращено на «Мой склад», WB/OZON FBS установлены в 0.\n"
                f"Товаров: {count}, до обнуления: {before} шт."
            ),
        )

    async def _restore_all_fbs(
        self,
        chat_id: int,
        message_id: int,
        original: str,
    ) -> None:
        if self.inventory is None:
            raise RuntimeError(
                "Shared inventory is not initialized"
            )
        restored, total = (
            await self.inventory.restore_ozon_mass()
        )
        if restored == 0:
            await self._finish(
                chat_id,
                message_id,
                original,
                "ℹ️ Массово обнулённых OZON FBS-остатков нет.",
            )
            return
        await self._finish(
            chat_id,
            message_id,
            original,
            (
                "✅ «Доступно для заказа» восстановлено; WB/OZON FBS синхронизированы.\n"
                f"Товаров: {restored}, суммарно: {total} шт."
            ),
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
            changed = await self.inventory.consume_ozon_postings(
                unseen
            )
            for sku in changed:
                await self._notify_current_depletion(sku)
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
            or data.startswith("ozstocks:")
            or data.startswith("ozfbsallzero:")
            or data.startswith("ozfbsallrestore:")
        ):
            return False
        if chat_id not in self.settings.telegram_chat_ids:
            return True

        if data.startswith("ozstocks:"):
            if data == "ozstocks:noop":
                return True
            try:
                page = int(data.split(":", 1)[1])
            except (TypeError, ValueError):
                return True
            _, page, _, _ = self._stock_page_meta(page)
            try:
                await self.tg.edit_message_text(
                    chat_id,
                    message_id,
                    self._format_stocks_page(page),
                    parse_mode="HTML",
                    reply_markup=self._stocks_keyboard(page),
                )
            except Exception as exc:
                log.warning(
                    "Could not edit OZON stocks page: %s", exc
                )
            return True

        if data.startswith("ozfbsallzero:"):
            try:
                if data.endswith(":skip"):
                    await self._finish(
                        chat_id,
                        message_id,
                        original,
                        "⏭ Массовое обнуление OZON FBS отменено.",
                    )
                elif data.endswith(":yes"):
                    await self._zero_all_fbs(
                        chat_id, message_id, original
                    )
            except Exception as exc:
                log.exception("OZON mass FBS zero failed")
                await self.tg.send_message(
                    chat_id,
                    f"⚠️ Не удалось обнулить OZON FBS: {exc}",
                )
            return True

        if data.startswith("ozfbsallrestore:"):
            try:
                if data.endswith(":skip"):
                    await self._finish(
                        chat_id,
                        message_id,
                        original,
                        "⏭ Восстановление OZON FBS отменено.",
                    )
                elif data.endswith(":yes"):
                    await self._restore_all_fbs(
                        chat_id, message_id, original
                    )
            except Exception as exc:
                log.exception("OZON mass FBS restore failed")
                await self.tg.send_message(
                    chat_id,
                    f"⚠️ Не удалось восстановить OZON FBS: {exc}",
                )
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

                await self.refresh_catalog_and_stocks(
                    notify=False
                )
                fbs_qty = int(
                    self.db.get_channel_stock(
                        "ozon_fbs", (sku,)
                    ).get(sku, 0)
                )
                fbo_qty = int(
                    self.db.get_channel_stock(
                        "ozon_fbo", (sku,)
                    ).get(sku, 0)
                )

                if action == "skipzero":
                    self._save_stock_decision(
                        sku, "zero", fbs_qty, fbo_qty, "skip"
                    )
                    await self._finish(
                        chat_id,
                        message_id,
                        original,
                        "⏭ «Доступно для заказа» оставлено без изменений.",
                    )
                    return True
                if action == "skiprestore":
                    self._save_stock_decision(
                        sku,
                        "restore",
                        fbs_qty,
                        fbo_qty,
                        "skip",
                    )
                    await self._finish(
                        chat_id,
                        message_id,
                        original,
                        "⏭ Сохранённое «Доступно для заказа» не восстанавливать.",
                    )
                    return True
                if action == "skipadd":
                    self._save_stock_decision(
                        sku, "add", fbs_qty, fbo_qty, "skip"
                    )
                    await self._finish(
                        chat_id,
                        message_id,
                        original,
                        "⏭ Не переносить товар в «Доступно для заказа».",
                    )
                    return True

                if action == "zero":
                    if fbo_qty <= 0:
                        await self._finish(
                            chat_id,
                            message_id,
                            original,
                            "ℹ️ Обнуление отменено: на складе OZON уже 0.",
                        )
                        return True
                    if fbs_qty <= 0:
                        await self._finish(
                            chat_id,
                            message_id,
                            original,
                            "ℹ️ OZON FBS уже равен 0.",
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
                            "✅ «Доступно для заказа» обнулено. "
                            "Количество возвращено на «Мой склад», WB/OZON FBS установлены в 0."
                        ),
                    )
                    return True

                if action == "restore":
                    if fbo_qty > 0:
                        await self._finish(
                            chat_id,
                            message_id,
                            original,
                            "ℹ️ Восстановление отменено: товар снова есть на складе OZON.",
                        )
                        return True
                    reason = (
                        self.db.get_channel_suppression_reason(
                            "ozon", sku
                        )
                    )
                    if reason != "marketplace_stock":
                        await self._finish(
                            chat_id,
                            message_id,
                            original,
                            "ℹ️ OZON FBS не находится в режиме автоматического обнуления из-за FBO.",
                        )
                        return True
                    quantity = await self.inventory.restore_channel(
                        "ozon", sku
                    )
                    await self._finish(
                        chat_id,
                        message_id,
                        original,
                        f"✅ В «Доступно для заказа» восстановлено {quantity} шт.; WB/OZON FBS синхронизированы.",
                    )
                    return True

                if action in {"add1", "add5"}:
                    if fbs_qty > 0 or fbo_qty > 0:
                        await self._finish(
                            chat_id,
                            message_id,
                            original,
                            (
                                "ℹ️ Действие отменено: остаток OZON "
                                "уже изменился."
                            ),
                        )
                        return True
                    available = self.inventory.available_quantity(
                        sku
                    )
                    if available > 0:
                        await self._finish(
                            chat_id,
                            message_id,
                            original,
                            (
                                "ℹ️ Действие отменено: «Доступно для заказа» "
                                f"уже равно {available} шт."
                            ),
                        )
                        return True
                    quantity = 1 if action == "add1" else 5
                    await self.inventory.set_available_stock(
                        sku,
                        quantity,
                        reason="telegram_ozon_stock_add",
                    )
                    await self._finish(
                        chat_id,
                        message_id,
                        original,
                        (
                            f"✅ В «Доступно для заказа» перенесено {quantity} шт. "
                            "WB/OZON FBS синхронизированы."
                        ),
                    )
                    return True
                return True
            except Exception as exc:
                log.exception("Ozon stock callback failed: %s", data)
                await self.tg.send_message(
                    chat_id,
                    f"⚠️ Не удалось изменить «Доступно для заказа»: {exc}",
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
