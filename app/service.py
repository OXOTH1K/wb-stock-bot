from __future__ import annotations

import asyncio
import logging
import math
from html import escape
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .config import Settings
from .db import StateDB
from .models import Product, ProductSize, SellerWarehouse, aggregate_by_nm, build_products
from .telegram import TelegramBot
from .wb_client import WildberriesClient

if TYPE_CHECKING:
    from .shared_inventory import SharedInventoryService

log = logging.getLogger(__name__)


class StockMonitorService:
    def __init__(
        self,
        settings: Settings,
        wb: WildberriesClient,
        tg: TelegramBot,
        db: StateDB,
    ):
        self.settings = settings
        self.wb = wb
        self.tg = tg
        self.db = db

        self.warehouse: SellerWarehouse | None = None
        self.sizes: list[ProductSize] = []
        self.products: dict[int, Product] = {}
        self.fbs_stock: dict[int, int] = {}
        self.wb_stock: dict[int, int] = {}

        self.catalog_updated_at: datetime | None = None
        self.fbs_updated_at: datetime | None = None
        self.wb_updated_at: datetime | None = None
        self._last_wb_request_mono = 0.0

        self._catalog_lock = asyncio.Lock()
        self._fbs_lock = asyncio.Lock()
        self._wb_lock = asyncio.Lock()
        self._error_notified_at: dict[str, float] = {}
        self._fbs_loaded = False
        self._wb_loaded = False
        self.shared_inventory: SharedInventoryService | None = None

    def set_shared_inventory(
        self, inventory: "SharedInventoryService"
    ) -> None:
        self.shared_inventory = inventory

    async def initialize(self) -> None:
        self.warehouse = await self.wb.get_single_seller_warehouse()
        await self.refresh_catalog()
        # On the very first run the DB has no previous values, so no transition
        # notifications are generated. On subsequent restarts, compare against
        # persisted state so changes that happened while the bot was down are seen.
        await self.refresh_fbs(notify=True)
        wb_startup_note = ""
        try:
            await self.refresh_wb(notify=True)
        except Exception as exc:
            if "WB API 429" not in str(exc):
                raise
            persisted = self.db.get_source("wb")
            if persisted:
                self.wb_stock = {
                    nm_id: int(persisted.get(nm_id, 0))
                    for nm_id in self.products
                }
                self._wb_loaded = True
                wb_startup_note = (
                    "\n⚠️ WB Analytics временно ограничил запросы (429); "
                    "использован последний сохранённый снимок."
                )
                log.warning(
                    "WB Analytics rate-limited at startup; using persisted snapshot"
                )
            else:
                wb_startup_note = (
                    "\n⚠️ WB Analytics временно ограничил запросы (429); "
                    "свежие WB-остатки будут загружены фоновым циклом."
                )
                log.warning(
                    "WB Analytics rate-limited at startup and no persisted snapshot exists"
                )

        zeros_fbs = sum(1 for q in self.fbs_stock.values() if q == 0)
        zeros_wb = (
            sum(1 for q in self.wb_stock.values() if q == 0)
            if self._wb_loaded
            else None
        )
        wb_zero_text = (
            str(zeros_wb) if zeros_wb is not None else "нет данных"
        )
        await self.tg.broadcast(
            self.settings.telegram_chat_ids,
            (
                "✅ Мониторинг остатков запущен\n"
                f"Склад продавца: {self.warehouse.name}\n"
                f"Товаров: {len(self.products)}\n"
                f"С нулём на FBS: {zeros_fbs}\n"
                f"С нулём на складах WB: {wb_zero_text}\n\n"
                "Команды: /stocks_wb, /stocks_ozon, /zero, /status, "
                "/fbs_zero_all, /fbs_restore, /ozon_fbs_zero_all, "
                "/ozon_fbs_restore"
                f"{wb_startup_note}"
            ),
        )

    async def refresh_catalog(self) -> None:
        async with self._catalog_lock:
            sizes = await self.wb.get_all_product_sizes()
            self.sizes = sizes
            self.products = build_products(sizes)
            self.catalog_updated_at = datetime.now(timezone.utc)
            log.info("Catalog refreshed: %d products / %d sizes", len(self.products), len(self.sizes))

    async def refresh_fbs(self, notify: bool = True) -> dict[int, int]:
        if self.warehouse is None:
            raise RuntimeError("Service is not initialized")
        async with self._fbs_lock:
            chrt_ids = [x.chrt_id for x in self.sizes]
            by_chrt = await self.wb.get_fbs_stocks(self.warehouse.id, chrt_ids)
            current = aggregate_by_nm(by_chrt, self.sizes)
            self.fbs_stock = current
            self.fbs_updated_at = datetime.now(timezone.utc)
            transitions = self.db.update_many("fbs", current)
            for nm_id, _, _ in transitions:
                self.db.clear_stock_decisions(nm_id)
            self._fbs_loaded = True
            if notify:
                await self._flush_pending_alerts()
                if self._wb_loaded:
                    await self._notify_total_depletions()
            return current

    async def refresh_wb(self, notify: bool = True, respect_min_interval: bool = True) -> dict[int, int]:
        async with self._wb_lock:
            # Endpoint allows one request every 20 seconds. A user command can arrive
            # right after a scheduled check, so reuse that fresh result instead of 429.
            since_last = time.monotonic() - self._last_wb_request_mono
            if respect_min_interval and self.wb_updated_at and since_last < 20.5:
                return self.wb_stock

            current = await self.wb.get_wb_stocks_by_nm(set(self.products))
            self._last_wb_request_mono = time.monotonic()
            self.wb_stock = current
            self.wb_updated_at = datetime.now(timezone.utc)
            transitions = self.db.update_many("wb", current)
            for nm_id, _, _ in transitions:
                self.db.clear_stock_decisions(nm_id)
            self._wb_loaded = True
            if notify:
                await self._flush_pending_alerts()
                if self._fbs_loaded:
                    await self._notify_total_depletions()
                await self._notify_wb_appearances(transitions)
            return current

    async def refresh_for_command(self) -> None:
        # FBS is near-real-time and cheap to refresh; WB source itself updates every ~30m.
        await self.refresh_fbs(notify=True)
        await self.refresh_wb(notify=True, respect_min_interval=True)

    async def _notify_total_depletions(self) -> None:
        """Notify only on a combined transition from available to zero everywhere.

        The combined state is persisted separately as ``total``. This avoids false
        alerts when only FBS or only WB reaches zero and also makes restarts safe:
        we do not treat a not-yet-loaded counterpart as a real zero.
        """
        current_total = {
            nm_id: self.fbs_stock.get(nm_id, 0) + self.wb_stock.get(nm_id, 0)
            for nm_id in self.products
        }
        transitions = self.db.update_many("total", current_total)
        for nm_id, old_qty, new_qty in transitions:
            if old_qty <= 0 or new_qty != 0:
                continue

            key = f"depletion:{nm_id}"
            self.db.put_pending_alert(key, "depletion", nm_id, old_qty, new_qty)
            await self._deliver_pending_alert(key, "depletion", nm_id, old_qty, new_qty)

    async def _notify_wb_appearances(
        self, transitions: list[tuple[int, int, int]]
    ) -> None:
        """Notify once when stock appears on WB while seller FBS stock is still positive."""
        for nm_id, old_qty, new_qty in transitions:
            if old_qty != 0 or new_qty <= 0:
                continue

            fbs_qty = self.fbs_stock.get(nm_id, 0)
            if fbs_qty <= 0:
                continue

            key = f"wb_appearance:{nm_id}"
            self.db.put_pending_alert(key, "wb_appearance", nm_id, old_qty, new_qty)
            await self._deliver_pending_alert(
                key, "wb_appearance", nm_id, old_qty, new_qty
            )

    async def _flush_pending_alerts(self) -> None:
        for alert_key, alert_type, nm_id, old_qty, new_qty in self.db.list_pending_alerts():
            await self._deliver_pending_alert(
                alert_key, alert_type, nm_id, old_qty, new_qty
            )

    async def _deliver_pending_alert(
        self,
        alert_key: str,
        alert_type: str,
        nm_id: int,
        old_qty: int,
        new_qty: int,
    ) -> None:
        product = self.products.get(nm_id)
        if product is None:
            self.db.delete_pending_alert(alert_key)
            return
        shared_inventory = getattr(
            self, "shared_inventory", None
        )

        if alert_type == "wb_appearance":
            fbs_qty = self.fbs_stock.get(nm_id, 0)
            wb_qty = self.wb_stock.get(nm_id, 0)
            sku = product.vendor_code or f"WB-{nm_id}"
            if (
                fbs_qty <= 0
                or wb_qty <= 0
                or (
                    shared_inventory is not None
                    and shared_inventory.is_suppressed("wb", sku)
                )
            ):
                self.db.delete_pending_alert(alert_key)
                return
            local_qty = (
                shared_inventory.local_quantity(sku)
                if shared_inventory is not None
                else 0
            )
            available_qty = (
                shared_inventory.available_quantity(sku)
                if shared_inventory is not None
                else fbs_qty
            )
            text = (
                "🟢 Товар появился на складе WB\n"
                f"Артикул продавца: {product.vendor_code or '—'}\n"
                f"Мой склад: {local_qty} шт.\n"
                f"Доступно для заказа: {available_qty} шт.\n"
                f"На складах WB: было {old_qty} шт. → стало {wb_qty} шт.\n\n"
                "Обнулить только WB FBS? "
                "«Доступно для заказа», OZON FBS и «Мой склад» не изменятся."
            )
            keyboard = self._wb_appearance_action_keyboard(nm_id)
        elif alert_type == "depletion":
            if self.fbs_stock.get(nm_id, 0) + self.wb_stock.get(nm_id, 0) != 0:
                self.db.delete_pending_alert(alert_key)
                return
            sku = product.vendor_code or f"WB-{nm_id}"
            wb_reason = (
                self.db.get_channel_suppression_reason("wb", sku)
                if shared_inventory is not None
                else None
            )
            if (
                shared_inventory is not None
                and wb_reason == "marketplace_stock"
            ):
                available_qty = shared_inventory.available_quantity(sku)
                if available_qty <= 0:
                    self.db.clear_channel_suppressed("wb", sku)
                    self.db.clear_saved_product_fbs("wb_auto", nm_id)
                    self.db.delete_pending_alert(alert_key)
                    return
                text = (
                    "🔴 Товар закончился на складе WB\n"
                    f"Артикул продавца: {product.vendor_code or '—'}\n"
                    "WB FBS сейчас намеренно равен 0.\n"
                    "На складах WB: 0 шт.\n\n"
                    f"Доступно для заказа: {available_qty} шт.\n"
                    "Вернуть текущее доступное количество только на WB FBS?"
                )
                keyboard = self._saved_restore_keyboard(
                    nm_id, available_qty
                )
            else:
                if (
                    shared_inventory is not None
                    and shared_inventory.available_quantity(sku) > 0
                ):
                    await shared_inventory.sync_sku(
                        sku,
                        raise_errors=False,
                        force=True,
                    )
                    self.db.delete_pending_alert(alert_key)
                    return
                saved = self.db.get_saved_product_fbs("wb_auto", nm_id)
                saved_total = sum(saved.values())
                if saved_total > 0:
                    text = (
                        "🔴 Товар закончился на складе WB\n"
                        f"Артикул продавца: {product.vendor_code or '—'}\n"
                        "На вашем складе FBS: 0 шт.\n"
                        "На складах WB: 0 шт.\n\n"
                        f"Перед обнулением FBS было сохранено: {saved_total} шт.\n"
                        "Вернуть сохранённый остаток на FBS?"
                    )
                    keyboard = self._saved_restore_keyboard(nm_id, saved_total)
                else:
                    local_qty = (
                        shared_inventory.local_quantity(sku)
                        if shared_inventory is not None
                        else 0
                    )
                    text = (
                        "🔴 Товар закончился в доступном пуле и на WB\n"
                        f"Артикул продавца: {product.vendor_code or '—'}\n"
                        "Доступно для заказа: 0 шт.\n"
                        f"Мой склад: {local_qty} шт.\n"
                        "На складах WB: 0 шт.\n\n"
                        "Перенести 1 или 5 шт. из «Моего склада» в «Доступно для заказа»?"
                    )
                    keyboard = self._depletion_action_keyboard(nm_id)
        else:
            self.db.delete_pending_alert(alert_key)
            return

        await self.tg.broadcast(
            self.settings.telegram_chat_ids,
            text,
            reply_markup=keyboard,
        )
        self.db.delete_pending_alert(alert_key)

    async def reconcile_after_gap(
        self, gap_started: datetime, recovered_at: datetime
    ) -> None:
        """Reconcile stock snapshots after a monitoring gap.

        Direct before/after changes are recoverable from persisted snapshots.
        Transient changes that start and finish entirely inside the gap cannot be
        reconstructed from the current-stock endpoints and are reported as such.
        """
        previous_fbs = self.db.get_source("fbs")
        previous_wb = self.db.get_source("wb")

        await self.refresh_fbs(notify=False)

        wb_note: str | None = None
        if self._wb_cache_is_fresh_for_status():
            wb_note = (
                "WB: использован последний успешный снимок "
                f"от {format_dt(self.wb_updated_at)}."
            )
        else:
            try:
                await self.refresh_wb(notify=False, respect_min_interval=True)
            except Exception as exc:
                if self.wb_updated_at is None:
                    raise
                if "WB API 429" not in str(exc):
                    raise
                wb_note = (
                    "⚠️ WB временно ограничил частоту запросов (429). "
                    "Для сверки использован последний успешный снимок "
                    f"от {format_dt(self.wb_updated_at)}."
                )

        actionable = 0
        current_total: dict[int, int] = {}
        for nm_id in self.products:
            fbs_qty = self.fbs_stock.get(nm_id, 0)
            wb_qty = self.wb_stock.get(nm_id, 0)
            current_total[nm_id] = fbs_qty + wb_qty

            old_fbs = previous_fbs.get(nm_id)
            old_wb = previous_wb.get(nm_id)
            if old_fbs is None or old_wb is None:
                continue

            if old_wb == 0 and wb_qty > 0 and fbs_qty > 0:
                key = f"wb_appearance:{nm_id}"
                self.db.put_pending_alert(
                    key, "wb_appearance", nm_id, old_wb, wb_qty
                )
                actionable += 1

            if old_fbs + old_wb > 0 and fbs_qty + wb_qty == 0:
                key = f"depletion:{nm_id}"
                self.db.put_pending_alert(
                    key, "depletion", nm_id, old_fbs + old_wb, 0
                )
                actionable += 1

        self.db.update_many("total", current_total)
        await self._flush_pending_alerts()

        minutes = max(1, int((recovered_at - gap_started).total_seconds() // 60))
        await self.tg.broadcast(
            self.settings.telegram_chat_ids,
            (
                "🌐 Связь восстановлена\n"
                f"Период без надёжного мониторинга: около {minutes} мин.\n"
                "Новые заказы, текущие остатки и недоставленные уведомления сверены.\n"
                f"Восстановлено актуальных ситуаций по остаткам: {actionable}.\n\n"
                "Важно: WB отдаёт текущий снимок остатков, поэтому короткий "
                "переход «появился и снова закончился» целиком внутри периода "
                "без связи восстановить точно нельзя."
            ),
        )

    async def handle_message(self, chat_id: int, text: str) -> None:
        command, *args = text.split()
        command = command.split("@", 1)[0].lower()

        if command in {"/start", "/id"}:
            await self.tg.send_message(
                chat_id,
                (
                    f"Ваш Telegram chat_id: {chat_id}\n\n"
                    "После добавления этого ID в TELEGRAM_CHAT_IDS доступны команды:\n"
                    "/stocks_wb — остатки WB FBS + склады WB\n"
                    "/stocks_wb 2 — открыть страницу WB\n"
                    "/stocks_ozon — остатки OZON FBS + FBO\n"
                    "/stocks_ozon 2 — открыть страницу OZON\n"
                    "/stock <артикул продавца> — найти товар\n"
                    "/set <артикул> <количество> — установить «Доступно для заказа»\n"
                    "/zero — товары с нулевым остатком\n"
                    "/fbs_zero_all — сохранить и обнулить весь FBS\n"
                    "/fbs_restore — восстановить WB FBS\n"
                    "/ozon_fbs_zero_all — обнулить весь OZON FBS\n"
                    "/ozon_fbs_restore — восстановить OZON FBS из локального склада\n"
                    "/status — состояние сервиса"
                ),
            )
            return

        if not self.settings.telegram_chat_ids:
            await self.tg.send_message(
                chat_id,
                "Доступ ещё не настроен. Выполните /id и добавьте chat_id в TELEGRAM_CHAT_IDS.",
            )
            return
        if chat_id not in self.settings.telegram_chat_ids:
            await self.tg.send_message(chat_id, "Доступ запрещён.")
            return

        try:
            if command == "/stocks_wb":
                page = 1
                if args:
                    try:
                        page = max(1, int(args[0]))
                    except ValueError:
                        await self.tg.send_message(
                            chat_id,
                            "Формат: /stocks_wb или /stocks_wb 2",
                        )
                        return
                await self.tg.send_message(
                    chat_id, "Обновляю остатки WB…"
                )
                await self.refresh_for_command()
                await self._send_stocks_page(chat_id, page)
            elif command == "/stocks":
                await self.tg.send_message(
                    chat_id,
                    (
                        "Остатки разделены по площадкам:\n"
                        "/stocks_wb — WB FBS + склады WB\n"
                        "/stocks_ozon — OZON FBS + FBO"
                    ),
                )
            elif command == "/zero":
                await self.tg.send_message(chat_id, "Обновляю остатки…")
                await self.refresh_for_command()
                await self.tg.send_message(chat_id, self._format_zero())
            elif command == "/stock":
                if not args:
                    await self.tg.send_message(chat_id, "Формат: /stock <артикул продавца>")
                    return
                await self.refresh_for_command()
                await self.tg.send_message(chat_id, self._format_search(" ".join(args)))
            elif command == "/set":
                if len(args) < 2:
                    await self.tg.send_message(
                        chat_id,
                        "Формат: /set <артикул продавца> <количество>",
                    )
                    return
                if self.shared_inventory is None:
                    raise RuntimeError("Общий склад не инициализирован")
                sku = " ".join(args[:-1]).strip()
                try:
                    quantity = int(args[-1])
                except ValueError:
                    await self.tg.send_message(
                        chat_id,
                        "Количество должно быть целым числом.",
                    )
                    return
                if quantity < 0:
                    await self.tg.send_message(
                        chat_id,
                        "Количество не может быть отрицательным.",
                    )
                    return
                if sku not in self.shared_inventory.all_skus():
                    await self.tg.send_message(
                        chat_id,
                        f"Товар с артикулом «{sku}» не найден.",
                    )
                    return
                before_available = (
                    self.shared_inventory.available_quantity(sku)
                )
                before_local = self.shared_inventory.local_quantity(sku)
                available = (
                    await self.shared_inventory.set_available_stock(
                        sku,
                        quantity,
                        reason="telegram_set",
                    )
                )
                local = self.shared_inventory.local_quantity(sku)
                delta = available - before_available
                direction = (
                    f"перенесено из «Моего склада»: {delta} шт."
                    if delta > 0
                    else (
                        f"возвращено на «Мой склад»: {-delta} шт."
                        if delta < 0
                        else "количество не изменилось."
                    )
                )
                await self.tg.send_message(
                    chat_id,
                    (
                        f"✅ {sku}\n"
                        f"Доступно для заказа: {available} шт.\n"
                        f"Мой склад: {before_local} → {local} шт.\n"
                        f"{direction}\n"
                        "WB FBS и OZON FBS синхронизированы."
                    ),
                )
            elif command == "/fbs_zero_all":
                await self.tg.send_message(
                    chat_id,
                    "⚠️ Обнулить «Доступно для заказа» у ВСЕХ товаров?\n"
                    "Количество вернётся на «Мой склад», а WB/OZON FBS станут 0.",
                    reply_markup={
                        "inline_keyboard": [[
                            {"text": "Обнулить весь FBS", "callback_data": "fbsallzero:yes"},
                            {"text": "Отмена", "callback_data": "fbsallzero:skip"},
                        ]]
                    },
                )
            elif command == "/fbs_restore":
                saved = self.db.get_available_snapshot(
                    "mass_shared"
                )
                saved_total = sum(saved.values())
                if not saved:
                    await self.tg.send_message(
                        chat_id,
                        "ℹ️ Сохранённого массового значения «Доступно для заказа» нет.",
                    )
                else:
                    await self.tg.send_message(
                        chat_id,
                        (
                            f"♻️ Восстановить «Доступно для заказа»: "
                            f"{len(saved)} товаров, суммарно {saved_total} шт.?\n"
                            "Количество будет перенесено с «Моего склада» и опубликовано в WB/OZON FBS."
                        ),
                        reply_markup={
                            "inline_keyboard": [[
                                {"text": "Восстановить", "callback_data": "fbsallrestore:yes"},
                                {"text": "Отмена", "callback_data": "fbsallrestore:skip"},
                            ]]
                        },
                    )
            elif command == "/status":
                await self.tg.send_message(chat_id, self._format_status())
            else:
                await self.tg.send_message(
                    chat_id,
                    (
                        "Команды: /stocks_wb, /stocks_ozon, "
                        "/stock <артикул продавца>, /set <артикул> <количество>, /zero, "
                        "/fbs_zero_all, /fbs_restore, "
                        "/ozon_fbs_zero_all, /ozon_fbs_restore, "
                        "/status, /id"
                    ),
                )
        except Exception as exc:
            log.exception("Command failed: %s", command)
            await self.tg.send_message(chat_id, f"⚠️ Не удалось выполнить команду: {exc}")

    def _stock_page_meta(self, page: int) -> tuple[list[Product], int, int, int]:
        rows = list(self.products.values())
        rows.sort(
            key=lambda p: (
                self.fbs_stock.get(p.nm_id, 0) + self.wb_stock.get(p.nm_id, 0) > 0,
                p.vendor_code.lower(),
                p.nm_id,
            )
        )
        page_size = max(1, self.settings.stocks_page_size)
        pages = max(1, math.ceil(len(rows) / page_size))
        page = min(max(1, page), pages)
        selected = rows[(page - 1) * page_size : page * page_size]
        return selected, page, pages, len(rows)

    def _format_stocks_page(self, page: int) -> str:
        selected, page, pages, total = self._stock_page_meta(page)

        # A <pre> block makes the numeric columns monospaced and aligned in Telegram.
        # Keep the article column compact enough to be readable on a phone.
        name_width = 32
        table = [f"   {'Артикул':<{name_width}} {'FBS':>4} {'WB':>4}"]
        for product in selected:
            fbs = self.fbs_stock.get(product.nm_id, 0)
            wb = self.wb_stock.get(product.nm_id, 0)
            if fbs > 0 and wb > 0:
                marker = "🟣"
            elif fbs > 0 or wb > 0:
                marker = "🟢"
            else:
                marker = "🔴"
            raw_name = product.vendor_code or product.title or "без артикула"
            if len(raw_name) > name_width:
                raw_name = raw_name[: name_width - 1] + "…"
            table.append(f"{marker} {raw_name:<{name_width}} {fbs:>4} {wb:>4}")

        escaped_table = escape("\n".join(table))
        return (
            f"🟣 <b>Остатки WB</b> — {page}/{pages} · товаров: {total}\n\n"
            f"<pre>{escaped_table}</pre>\n"
            "<i>WB — суммарный остаток на складах Wildberries.</i>"
        )

    def _stocks_keyboard(self, page: int) -> dict:
        _, page, pages, _ = self._stock_page_meta(page)
        buttons = []
        if page > 1:
            buttons.append({"text": "◀️", "callback_data": f"stocks:{page - 1}"})
        buttons.append({"text": f"{page}/{pages}", "callback_data": "stocks:noop"})
        if page < pages:
            buttons.append({"text": "▶️", "callback_data": f"stocks:{page + 1}"})
        return {"inline_keyboard": [buttons]}

    async def _send_stocks_page(self, chat_id: int, page: int) -> None:
        _, page, _, _ = self._stock_page_meta(page)
        await self.tg.send_message(
            chat_id,
            self._format_stocks_page(page),
            parse_mode="HTML",
            reply_markup=self._stocks_keyboard(page),
        )

    def _depletion_action_keyboard(self, nm_id: int) -> dict:
        return {
            "inline_keyboard": [[
                {"text": "1", "callback_data": f"fbsadd:{nm_id}:1"},
                {"text": "5", "callback_data": f"fbsadd:{nm_id}:5"},
                {"text": "Не добавлять", "callback_data": f"fbsadd:{nm_id}:skip"},
            ]]
        }

    def _wb_appearance_action_keyboard(self, nm_id: int) -> dict:
        return {
            "inline_keyboard": [[
                {"text": "Обнулить WB FBS", "callback_data": f"fbszero:{nm_id}:yes"},
                {"text": "Не обнулять WB FBS", "callback_data": f"fbszero:{nm_id}:skip"},
            ]]
        }

    def _saved_restore_keyboard(self, nm_id: int, quantity: int) -> dict:
        return {
            "inline_keyboard": [[
                {
                    "text": f"Вернуть {quantity} шт. на WB FBS",
                    "callback_data": f"fbsrestore:{nm_id}:yes",
                },
                {
                    "text": "Не возвращать",
                    "callback_data": f"fbsrestore:{nm_id}:skip",
                },
            ]]
        }

    async def _finish_action_message(
        self, chat_id: int, message_id: int, original_text: str, status: str
    ) -> None:
        text = f"{original_text.rstrip()}\n\n{status}" if original_text.strip() else status
        try:
            await self.tg.edit_message_text(
                chat_id,
                message_id,
                text,
                reply_markup={"inline_keyboard": []},
            )
        except Exception as exc:
            log.warning("Could not finalize action message: %s", exc)
            await self.tg.send_message(chat_id, status)

    async def _add_fbs_from_alert(
        self,
        chat_id: int,
        message_id: int,
        original_text: str,
        nm_id: int,
        quantity: int,
    ) -> None:
        product = self.products.get(nm_id)
        if product is None:
            await self._finish_action_message(
                chat_id,
                message_id,
                original_text,
                "⚠️ Товар больше не найден в каталоге.",
            )
            return
        if self.warehouse is None:
            raise RuntimeError("Склад продавца не определён")
        if len(product.chrt_ids) != 1:
            await self._finish_action_message(
                chat_id,
                message_id,
                original_text,
                (
                    "⚠️ Остаток не изменён: у товара несколько размеров/вариантов. "
                    "Автоматически выбирать chrtId небезопасно."
                ),
            )
            return

        await self.refresh_fbs(notify=False)
        current_fbs = self.fbs_stock.get(nm_id, 0)
        if current_fbs != 0:
            await self._finish_action_message(
                chat_id,
                message_id,
                original_text,
                (
                    "ℹ️ Действие не выполнено: WB FBS уже изменился "
                    f"и сейчас равен {current_fbs} шт."
                ),
            )
            return
        if self.wb_stock.get(nm_id, 0) > 0:
            await self._finish_action_message(
                chat_id,
                message_id,
                original_text,
                "ℹ️ Действие не выполнено: товар уже появился на складе WB.",
            )
            return

        sku = product.vendor_code or f"WB-{nm_id}"
        if self.shared_inventory is not None:
            await self.shared_inventory.set_available_stock(
                sku,
                quantity,
                reason="telegram_stock_add",
            )
            await self._finish_action_message(
                chat_id,
                message_id,
                original_text,
                (
                    f"✅ «Доступно для заказа» установлено в {quantity} шт. "
                    "Товар перенесён с «Моего склада», WB/OZON FBS синхронизированы."
                ),
            )
            return

        await self.wb.set_fbs_stocks(
            self.warehouse.id,
            {product.chrt_ids[0]: quantity},
        )
        await self.refresh_fbs(notify=True)
        actual = self.fbs_stock.get(nm_id, 0)
        status = (
            f"✅ На FBS установлено {quantity} шт."
            if actual == quantity
            else (
                f"✅ Команда на установку {quantity} шт. отправлена в WB. "
                f"Текущий ответ API: {actual} шт."
            )
        )
        await self._finish_action_message(
            chat_id, message_id, original_text, status
        )

    async def _zero_fbs_from_alert(
        self,
        chat_id: int,
        message_id: int,
        original_text: str,
        nm_id: int,
    ) -> None:
        product = self.products.get(nm_id)
        if product is None:
            await self._finish_action_message(
                chat_id,
                message_id,
                original_text,
                "⚠️ Товар больше не найден в каталоге.",
            )
            return
        if self.warehouse is None:
            raise RuntimeError("Склад продавца не определён")
        if self.wb_stock.get(nm_id, 0) <= 0:
            await self._finish_action_message(
                chat_id,
                message_id,
                original_text,
                (
                    "⚠️ WB FBS не обнулён: бот больше не видит остаток "
                    "этого товара на складе WB."
                ),
            )
            return

        await self.refresh_fbs(notify=False)
        current_fbs = self.fbs_stock.get(nm_id, 0)
        if current_fbs <= 0:
            await self._finish_action_message(
                chat_id,
                message_id,
                original_text,
                "ℹ️ WB FBS уже равен 0 шт.",
            )
            return

        sku = product.vendor_code or f"WB-{nm_id}"
        if self.shared_inventory is not None:
            await self.shared_inventory.suppress_channel(
                "wb", sku, "marketplace_stock"
            )
            self.db.clear_saved_product_fbs("wb_auto", nm_id)
            await self._finish_action_message(
                chat_id,
                message_id,
                original_text,
                (
                    "✅ WB FBS обнулён. "
                    "«Доступно для заказа», OZON FBS и «Мой склад» не изменены."
                ),
            )
            return

        by_chrt = await self.wb.get_fbs_stocks(
            self.warehouse.id, product.chrt_ids
        )
        self.db.save_product_fbs("wb_auto", nm_id, by_chrt)
        await self.wb.set_fbs_stocks(
            self.warehouse.id,
            {chrt_id: 0 for chrt_id in product.chrt_ids},
        )
        await self.refresh_fbs(notify=True)
        actual = self.fbs_stock.get(nm_id, 0)
        status = (
            "✅ FBS обнулён."
            if actual == 0
            else (
                "✅ Команда на обнуление отправлена в WB. "
                f"Текущий ответ API: {actual} шт."
            )
        )
        await self._finish_action_message(
            chat_id, message_id, original_text, status
        )

    async def _restore_saved_product(
        self,
        chat_id: int,
        message_id: int,
        original_text: str,
        nm_id: int,
    ) -> None:
        if self.warehouse is None:
            raise RuntimeError("Склад продавца не определён")
        product = self.products.get(nm_id)
        if product is None:
            await self._finish_action_message(
                chat_id,
                message_id,
                original_text,
                "⚠️ Товар больше не найден в каталоге.",
            )
            return
        if self.wb_stock.get(nm_id, 0) > 0:
            await self._finish_action_message(
                chat_id,
                message_id,
                original_text,
                (
                    "⚠️ Восстановление отменено: товар снова появился "
                    "на складе WB."
                ),
            )
            return

        sku = product.vendor_code or f"WB-{nm_id}"
        if (
            self.shared_inventory is not None
            and self.shared_inventory.is_suppressed("wb", sku)
        ):
            current = await self.wb.get_fbs_stocks(
                self.warehouse.id, product.chrt_ids
            )
            if any(int(qty) != 0 for qty in current.values()):
                await self._finish_action_message(
                    chat_id,
                    message_id,
                    original_text,
                    "⚠️ WB FBS уже изменился после обнуления.",
                )
                return
            quantity = await self.shared_inventory.restore_channel(
                "wb", sku
            )
            self.db.clear_saved_product_fbs("wb_auto", nm_id)
            self.db.clear_stock_decision(nm_id, "fbsrestore")
            await self._finish_action_message(
                chat_id,
                message_id,
                original_text,
                (
                    f"✅ WB FBS восстановлен до текущего «Доступно для заказа»: {quantity} шт. "
                    "OZON FBS и локальные остатки не изменены."
                ),
            )
            return

        saved = self.db.get_saved_product_fbs("wb_auto", nm_id)
        if not saved:
            await self._finish_action_message(
                chat_id,
                message_id,
                original_text,
                "ℹ️ Сохранённого остатка для товара уже нет.",
            )
            return
        current = await self.wb.get_fbs_stocks(
            self.warehouse.id, product.chrt_ids
        )
        if any(int(qty) != 0 for qty in current.values()):
            await self._finish_action_message(
                chat_id,
                message_id,
                original_text,
                (
                    "⚠️ FBS уже изменился после обнуления. "
                    "Сохранённый остаток не перезаписан."
                ),
            )
            return
        await self.wb.set_fbs_stocks(self.warehouse.id, saved)
        self.db.clear_saved_product_fbs("wb_auto", nm_id)
        self.db.clear_stock_decision(nm_id, "fbsrestore")
        await self.refresh_fbs(notify=False)
        total = sum(saved.values())
        await self._finish_action_message(
            chat_id,
            message_id,
            original_text,
            f"✅ На FBS возвращён сохранённый остаток: {total} шт.",
        )

    async def _zero_all_fbs(
        self,
        chat_id: int,
        message_id: int,
        original_text: str,
    ) -> None:
        if self.shared_inventory is not None:
            count, total = (
                await self.shared_inventory.suppress_wb_mass()
            )
            await self.refresh_fbs(notify=False)
            await self._finish_action_message(
                chat_id,
                message_id,
                original_text,
                (
                    "✅ «Доступно для заказа» обнулено для всех товаров.\n"
                    "Остаток возвращён на «Мой склад», WB/OZON FBS синхронизированы в 0.\n"
                    f"Товаров: {count}, возвращено: {total} шт."
                ),
            )
            return

        if self.warehouse is None:
            raise RuntimeError("Склад продавца не определён")
        chrt_to_nm = {size.chrt_id: size.nm_id for size in self.sizes}
        current = await self.wb.get_fbs_stocks(
            self.warehouse.id, chrt_to_nm
        )
        if not any(int(qty) > 0 for qty in current.values()):
            await self._finish_action_message(
                chat_id,
                message_id,
                original_text,
                (
                    "ℹ️ Все остатки WB FBS уже равны 0. "
                    "Предыдущий массовый режим не изменён."
                ),
            )
            return

        snapshot = {
            (chrt_to_nm[chrt_id], chrt_id): int(qty)
            for chrt_id, qty in current.items()
        }
        self.db.replace_saved_fbs("mass", snapshot)

        if self.shared_inventory is not None:
            await self.shared_inventory.suppress_wb_mass()

        try:
            await self.wb.set_fbs_stocks(
                self.warehouse.id,
                {chrt_id: 0 for chrt_id in current},
            )
        except Exception:
            if self.shared_inventory is not None:
                self.db.clear_channel_suppressions_by_reason(
                    "wb", "mass"
                )
            raise

        self.db.clear_saved_fbs("wb_auto")
        await self.refresh_fbs(notify=False)
        self.db.update_many(
            "total",
            {
                nm_id: self.fbs_stock.get(nm_id, 0)
                + self.wb_stock.get(nm_id, 0)
                for nm_id in self.products
            },
        )
        await self._finish_action_message(
            chat_id,
            message_id,
            original_text,
            (
                "✅ Все остатки WB FBS обнулены.\n"
                "Основной склад и OZON FBS не изменены.\n"
                f"Товарных вариантов в WB: {len(snapshot)}."
            ),
        )

    async def _restore_all_fbs(
        self,
        chat_id: int,
        message_id: int,
        original_text: str,
    ) -> None:
        if self.shared_inventory is not None:
            restored, total = (
                await self.shared_inventory.restore_wb_mass()
            )
            if restored == 0:
                await self._finish_action_message(
                    chat_id,
                    message_id,
                    original_text,
                    "ℹ️ Сохранённого массового значения «Доступно для заказа» нет.",
                )
                return
            await self.refresh_fbs(notify=False)
            await self._finish_action_message(
                chat_id,
                message_id,
                original_text,
                (
                    "✅ «Доступно для заказа» восстановлено из сохранённого значения.\n"
                    "Товар снова перенесён с «Моего склада», WB/OZON FBS синхронизированы.\n"
                    f"Товаров: {restored}, суммарно: {total} шт."
                ),
            )
            return

        if self.warehouse is None:
            raise RuntimeError("Склад продавца не определён")

        if self.shared_inventory is not None:
            mass_skus = [
                sku
                for sku in self.db.list_channel_suppressions("wb")
                if self.db.get_channel_suppression_reason(
                    "wb", sku
                ) == "mass"
            ]
            if not mass_skus:
                await self._finish_action_message(
                    chat_id,
                    message_id,
                    original_text,
                    "ℹ️ Массово подавленных WB FBS-остатков нет.",
                )
                return
            restored, total = (
                await self.shared_inventory.restore_wb_mass()
            )
            self.db.clear_saved_fbs("mass")
            await self._finish_action_message(
                chat_id,
                message_id,
                original_text,
                (
                    "✅ WB FBS восстановлен из актуального основного склада.\n"
                    f"Товаров: {restored}, суммарно: {total} шт."
                ),
            )
            return

        snapshot = self.db.get_saved_fbs("mass")
        if not snapshot:
            await self._finish_action_message(
                chat_id,
                message_id,
                original_text,
                "ℹ️ Сохранённого массового снимка FBS нет.",
            )
            return
        saved_by_chrt = {
            chrt_id: qty
            for (_nm_id, chrt_id), qty in snapshot.items()
        }
        current = await self.wb.get_fbs_stocks(
            self.warehouse.id, saved_by_chrt
        )
        if any(int(qty) != 0 for qty in current.values()):
            await self._finish_action_message(
                chat_id,
                message_id,
                original_text,
                (
                    "⚠️ Восстановление отменено: после обнуления FBS "
                    "уже изменился. Сохранённый снимок оставлен."
                ),
            )
            return
        await self.wb.set_fbs_stocks(
            self.warehouse.id, saved_by_chrt
        )
        self.db.clear_saved_fbs("mass")
        await self.refresh_fbs(notify=False)
        await self._finish_action_message(
            chat_id,
            message_id,
            original_text,
            (
                "✅ Сохранённые остатки FBS восстановлены.\n"
                f"Вариантов: {len(saved_by_chrt)}, "
                f"суммарно {sum(saved_by_chrt.values())} шт."
            ),
        )

    async def handle_callback(
        self, chat_id: int, message_id: int, data: str, message_text: str = ""
    ) -> None:
        if chat_id not in self.settings.telegram_chat_ids:
            return
        if data in {
            "stocks:noop",
            "stockswb:noop",
            "action:noop",
        }:
            return

        if data.startswith("fbsallzero:"):
            try:
                if data.endswith(":skip"):
                    await self._finish_action_message(
                        chat_id, message_id, message_text, "⏭ Массовое обнуление отменено."
                    )
                elif data.endswith(":yes"):
                    await self._zero_all_fbs(chat_id, message_id, message_text)
            except Exception as exc:
                log.exception("Mass FBS zero failed")
                await self.tg.send_message(chat_id, f"⚠️ Не удалось обнулить весь FBS: {exc}")
            return

        if data.startswith("fbsallrestore:"):
            try:
                if data.endswith(":skip"):
                    await self._finish_action_message(
                        chat_id, message_id, message_text, "⏭ Восстановление FBS отменено."
                    )
                elif data.endswith(":yes"):
                    await self._restore_all_fbs(chat_id, message_id, message_text)
            except Exception as exc:
                log.exception("Mass FBS restore failed")
                await self.tg.send_message(chat_id, f"⚠️ Не удалось восстановить FBS: {exc}")
            return

        if data.startswith("stockswb:") or data.startswith("stocks:"):
            try:
                page = int(data.split(":", 1)[1])
            except (TypeError, ValueError):
                return
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
                log.warning("Could not edit stocks page: %s", exc)
            return

        try:
            parts = data.split(":")
            if len(parts) != 3:
                return
            action, raw_nm_id, choice = parts
            nm_id = int(raw_nm_id)

            if action == "fbsadd":
                if choice == "skip":
                    self.db.save_stock_decision(
                        nm_id,
                        "fbsadd",
                        self.fbs_stock.get(nm_id, 0),
                        self.wb_stock.get(nm_id, 0),
                        "skip",
                    )
                    await self._finish_action_message(
                        chat_id, message_id, message_text, "⏭ Решение: не переносить товар в «Доступно для заказа»."
                    )
                    return
                if choice not in {"1", "5"}:
                    return
                await self._add_fbs_from_alert(
                    chat_id, message_id, message_text, nm_id, int(choice)
                )
                return

            if action == "fbszero":
                if choice == "skip":
                    self.db.save_stock_decision(
                        nm_id,
                        "fbszero",
                        self.fbs_stock.get(nm_id, 0),
                        self.wb_stock.get(nm_id, 0),
                        "skip",
                    )
                    await self._finish_action_message(
                        chat_id,
                        message_id,
                        message_text,
                        "⏭ Решение: WB FBS оставить без изменений.",
                    )
                    return
                if choice != "yes":
                    return
                await self._zero_fbs_from_alert(chat_id, message_id, message_text, nm_id)
                return

            if action == "fbsrestore":
                if choice == "skip":
                    self.db.save_stock_decision(
                        nm_id,
                        "fbsrestore",
                        self.fbs_stock.get(nm_id, 0),
                        self.wb_stock.get(nm_id, 0),
                        "skip",
                    )
                    await self._finish_action_message(
                        chat_id,
                        message_id,
                        message_text,
                        "⏭ Решение: WB FBS не восстанавливать.",
                    )
                    return
                if choice != "yes":
                    return
                await self._restore_saved_product(
                    chat_id, message_id, message_text, nm_id
                )
                return
        except Exception as exc:
            log.exception("Stock action callback failed: %s", data)
            await self.tg.send_message(
                chat_id,
                f"⚠️ Не удалось изменить остаток: {exc}",
            )

    def _format_zero(self) -> str:
        rows = []
        for product in self.products.values():
            fbs = self.fbs_stock.get(product.nm_id, 0)
            wb = self.wb_stock.get(product.nm_id, 0)
            if fbs == 0 or wb == 0:
                rows.append((product, fbs, wb))
        rows.sort(key=lambda x: (x[0].vendor_code.lower(), x[0].nm_id))

        if not rows:
            return "🟢 Нулевых остатков нет."
        lines = [f"🔴 Нулевые остатки: {len(rows)}", ""]
        for product, fbs, wb in rows:
            name = (product.vendor_code or product.title or "без артикула")[:45]
            zero_at = []
            if fbs == 0:
                zero_at.append("FBS")
            if wb == 0:
                zero_at.append("WB")
            lines.append(
                f"• {name} | ноль: {', '.join(zero_at)} | FBS {fbs} | WB {wb}"
            )
        return "\n".join(lines)

    def _format_search(self, query: str) -> str:
        q = query.strip().lower()
        matches: list[Product] = []
        for product in self.products.values():
            if q == str(product.nm_id) or q in product.vendor_code.lower():
                matches.append(product)
        if not matches:
            return f"Товар по запросу «{query}» не найден."
        lines = [f"🔎 Найдено: {len(matches)}", ""]
        for product in matches[:30]:
            fbs = self.fbs_stock.get(product.nm_id, 0)
            wb = self.wb_stock.get(product.nm_id, 0)
            lines.append(
                f"{product.vendor_code or '—'}\n"
                f"FBS: {fbs} шт. | Склады WB: {wb} шт."
            )
        if len(matches) > 30:
            lines.append(f"\nПоказаны первые 30 из {len(matches)}.")
        return "\n".join(lines)

    def _wb_cache_is_fresh_for_status(self) -> bool:
        if self.wb_updated_at is None:
            return False
        age = (datetime.now(timezone.utc) - self.wb_updated_at).total_seconds()
        return age < max(60, int(self.settings.wb_check_interval))

    async def audit_actionable_stocks(self, chat_id: int) -> tuple[int, str | None]:
        """Refresh FBS immediately and reuse a recent WB analytics snapshot."""
        await self.refresh_fbs(notify=False)

        wb_note: str | None = None
        if self._wb_cache_is_fresh_for_status():
            wb_note = (
                "WB: использован последний успешный снимок "
                f"от {format_dt(self.wb_updated_at)}."
            )
        else:
            try:
                await self.refresh_wb(notify=False, respect_min_interval=True)
            except Exception as exc:
                if self.wb_updated_at is None or "WB API 429" not in str(exc):
                    raise
                wb_note = (
                    "⚠️ WB временно ограничил частоту запросов (429). "
                    "Для сверки использован последний успешный снимок "
                    f"от {format_dt(self.wb_updated_at)}."
                )

        actionable = 0
        for product in sorted(
            self.products.values(),
            key=lambda p: (p.vendor_code.lower(), p.nm_id),
        ):
            nm_id = product.nm_id
            fbs_qty = self.fbs_stock.get(nm_id, 0)
            wb_qty = self.wb_stock.get(nm_id, 0)

            if fbs_qty > 0 and wb_qty > 0:
                if self.db.stock_decision_matches(
                    nm_id, "fbszero", fbs_qty, wb_qty, "skip"
                ):
                    continue
                actionable += 1
                await self.tg.send_message(
                    chat_id,
                    (
                        "🔎 /status: товар есть одновременно на FBS и WB\n"
                        f"Артикул продавца: {product.vendor_code or '—'}\n"
                        f"FBS: {fbs_qty} шт. | WB: {wb_qty} шт.\n\n"
                        "Обнулить остаток на FBS?"
                    ),
                    reply_markup=self._wb_appearance_action_keyboard(nm_id),
                )
                continue

            if fbs_qty == 0 and wb_qty == 0:
                sku = product.vendor_code or f"WB-{nm_id}"
                wb_reason = (
                    self.db.get_channel_suppression_reason(
                        "wb", sku
                    )
                )
                shared_suppressed = (
                    self.shared_inventory is not None
                    and wb_reason == "marketplace_stock"
                )
                local_qty = (
                    self.shared_inventory.local_quantity(sku)
                    if self.shared_inventory is not None
                    else 0
                )
                available_qty = (
                    self.shared_inventory.available_quantity(sku)
                    if self.shared_inventory is not None
                    else fbs_qty
                )

                if shared_suppressed:
                    if available_qty <= 0:
                        self.db.clear_channel_suppressed(
                            "wb", sku
                        )
                        self.db.clear_saved_product_fbs(
                            "wb_auto", nm_id
                        )
                        continue
                    restore_qty = available_qty
                else:
                    # In the shared model an unsuppressed WB FBS should
                    # mirror "Доступно для заказа". Repair drift silently
                    # instead of offering to add stock again.
                    if (
                        self.shared_inventory is not None
                        and available_qty > 0
                    ):
                        await self.shared_inventory.sync_sku(
                            sku,
                            raise_errors=False,
                            force=True,
                        )
                        continue
                    saved = self.db.get_saved_product_fbs(
                        "wb_auto", nm_id
                    )
                    restore_qty = sum(saved.values())

                action = (
                    "fbsrestore"
                    if restore_qty > 0
                    else "fbsadd"
                )
                if self.db.stock_decision_matches(
                    nm_id, action, fbs_qty, wb_qty, "skip"
                ):
                    continue
                actionable += 1
                if restore_qty > 0:
                    if shared_suppressed:
                        text = (
                            "🔎 /status: товар закончился на складе WB\n"
                            f"Артикул продавца: {product.vendor_code or '—'}\n"
                            "WB FBS: 0 шт. | склад WB: 0 шт.\n"
                            f"Доступно для заказа: {restore_qty} шт.\n\n"
                            "Вернуть текущее доступное количество только на WB FBS?"
                        )
                    else:
                        text = (
                            "🔎 /status: товар закончился на складе WB\n"
                            f"Артикул продавца: {product.vendor_code or '—'}\n"
                            "WB FBS: 0 шт. | склад WB: 0 шт.\n\n"
                            f"Сохранённый FBS-остаток: {restore_qty} шт.\n"
                            "Вернуть остаток на WB FBS?"
                        )
                    keyboard = self._saved_restore_keyboard(
                        nm_id, restore_qty
                    )
                else:
                    text = (
                        "🔎 /status: товар закончился везде\n"
                        f"Артикул продавца: {product.vendor_code or '—'}\n"
                        f"Мой склад: {local_qty} шт. | "
                        f"Доступно для заказа: {available_qty} шт.\n"
                        "WB: 0 шт.\n\n"
                        "Перенести 1 или 5 шт. в «Доступно для заказа»?"
                    )
                    keyboard = self._depletion_action_keyboard(
                        nm_id
                    )
                await self.tg.send_message(
                    chat_id,
                    text,
                    reply_markup=keyboard,
                )

        return actionable, wb_note

    def _format_status(self) -> str:
        warehouse = self.warehouse.name if self.warehouse else "—"
        return (
            "🤖 WB Stock Bot работает\n"
            f"Склад продавца: {warehouse}\n"
            f"Товаров: {len(self.products)}\n"
            f"Каталог: {format_dt(self.catalog_updated_at)}\n"
            f"FBS остатки: {format_dt(self.fbs_updated_at)}\n"
            f"WB остатки: {format_dt(self.wb_updated_at)}\n"
            f"Интервалы: FBS {self.settings.fbs_check_interval // 60} мин, "
            f"WB {self.settings.wb_check_interval // 60} мин"
        )

    async def fbs_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.fbs_check_interval)
            try:
                await self.refresh_fbs(notify=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("FBS refresh failed")
                await self._notify_error_once("fbs", exc)

    async def wb_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.wb_check_interval)
            try:
                await self.refresh_wb(notify=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("WB refresh failed")
                await self._notify_error_once("wb", exc)

    async def catalog_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.catalog_refresh_interval)
            try:
                await self.refresh_catalog()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("Catalog refresh failed")
                await self._notify_error_once("catalog", exc)

    async def _notify_error_once(self, key: str, exc: Exception) -> None:
        now = time.monotonic()
        last = self._error_notified_at.get(key, 0.0)
        if now - last < 3600:
            return
        self._error_notified_at[key] = now
        await self.tg.broadcast(
            self.settings.telegram_chat_ids,
            f"⚠️ Ошибка проверки {key}: {exc}\nПовторная попытка будет автоматически.",
        )


def format_dt(value: datetime | None) -> str:
    if value is None:
        return "нет данных"
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
