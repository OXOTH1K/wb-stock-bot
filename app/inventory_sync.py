from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from .config import Settings
from .db import StateDB
from .ozon_client import OzonClient
from .telegram import TelegramBot
from .wb_client import WildberriesClient

if TYPE_CHECKING:
    from .service import StockMonitorService

log = logging.getLogger(__name__)


class SharedStockSync:
    """Keep local inventory as the source of truth for marketplace FBS stock."""

    def __init__(
        self,
        settings: Settings,
        db: StateDB,
        wb: WildberriesClient,
        service: "StockMonitorService",
        tg: TelegramBot,
        ozon: OzonClient | None = None,
    ):
        self.settings = settings
        self.db = db
        self.wb = wb
        self.service = service
        self.tg = tg
        self.ozon = ozon
        self.ozon_warehouse_id: int | None = settings.ozon_warehouse_id
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self.ozon is None:
            return
        if self.ozon_warehouse_id is not None:
            return
        warehouses = await self.ozon.get_fbs_warehouses()
        candidates = [
            row
            for row in warehouses
            if not bool(row.get("is_rfbs"))
            and str(row.get("status") or "").upper()
            not in {"DISABLED", "ARCHIVED", "INACTIVE"}
        ]
        if len(candidates) == 1:
            self.ozon_warehouse_id = int(candidates[0]["warehouse_id"])
            log.info(
                "Ozon FBS warehouse selected automatically: %s (%s)",
                candidates[0].get("name") or "—",
                self.ozon_warehouse_id,
            )
            return
        if not candidates:
            raise RuntimeError(
                "Не найден активный OZON FBS-склад. "
                "Укажите OZON_WAREHOUSE_ID явно, если склад существует."
            )
        ids = ", ".join(
            f"{row.get('name') or '—'}={row.get('warehouse_id')}"
            for row in candidates
        )
        raise RuntimeError(
            "Найдено несколько OZON FBS-складов. "
            f"Укажите OZON_WAREHOUSE_ID в .env. Доступны: {ids}"
        )

    def _wb_product(self, sku: str):
        clean = str(sku).strip()
        for product in self.service.products.values():
            if str(product.vendor_code or "").strip() == clean:
                return product
        return None

    def resolve_wb_sku(
        self, article: str, nm_id: int
    ) -> str:
        clean = str(article or "").strip()
        if clean:
            return clean
        product = self.service.products.get(int(nm_id))
        if product is None:
            return ""
        return str(product.vendor_code or "").strip()

    def channel_exists(self, channel: str, sku: str) -> bool:
        if channel == "wb":
            return self._wb_product(sku) is not None
        if channel == "ozon":
            return (
                self.ozon is not None
                and sku in self.db.get_channel_catalog("ozon")
            )
        return False

    def is_suppressed(self, channel: str, sku: str) -> bool:
        return self.db.is_channel_suppressed(channel, sku)

    def ensure_local(self, sku: str) -> int:
        sku = str(sku).strip()
        existing = self.db.get_local_stock((sku,))
        if sku in existing:
            return int(existing[sku])

        product = self._wb_product(sku)
        if product is not None:
            legacy_saved = self.db.get_saved_product_fbs(
                "wb_auto", product.nm_id
            )
            initial = (
                sum(int(qty) for qty in legacy_saved.values())
                if legacy_saved
                else int(self.service.fbs_stock.get(product.nm_id, 0))
            )
        else:
            initial = int(
                self.db.get_channel_stock("ozon_fbs", (sku,)).get(sku, 0)
            )
        return self.db.ensure_local_stock(sku, initial)

    async def apply_order(
        self,
        source_channel: str,
        order_key: str,
        items: dict[str, int],
    ) -> dict[str, dict[str, int]]:
        """Deduct a newly observed FBS order once and sync the other channel."""
        async with self._lock:
            normalized: dict[str, int] = {}
            for sku, quantity in items.items():
                clean = str(sku or "").strip()
                qty = max(0, int(quantity))
                if not clean or qty <= 0:
                    continue
                normalized[clean] = normalized.get(clean, 0) + qty
                self.ensure_local(clean)

            applied, changes = self.db.apply_order_sale(
                source_channel,
                str(order_key),
                normalized,
            )
            if not applied:
                return {}

            target = "ozon" if source_channel == "wb" else "wb"
            for sku, change in changes.items():
                if self.channel_exists(target, sku) and not self.is_suppressed(
                    target, sku
                ):
                    self.db.queue_stock_sync(
                        target,
                        sku,
                        change["after"],
                        f"order:{source_channel}:{order_key}",
                    )

            await self.flush_pending(channel=target)

            shortages = [
                (sku, change)
                for sku, change in changes.items()
                if change["shortage"] > 0
            ]
            if shortages:
                lines = [
                    "⚠️ Заказ превысил учтённый остаток локального склада:",
                ]
                for sku, change in shortages:
                    lines.append(
                        f"• {sku}: было {change['before']}, "
                        f"заказано {change['requested']}, локально стало 0"
                    )
                await self.tg.broadcast(
                    self.settings.telegram_chat_ids,
                    "\n".join(lines),
                )

            return changes

    async def set_local_stock(
        self,
        sku: str,
        quantity: int,
        reason: str = "manual",
    ) -> int:
        async with self._lock:
            sku = str(sku).strip()
            quantity = self.db.set_local_stock(
                sku, int(quantity), reason=reason
            )
            for channel in ("wb", "ozon"):
                if not self.channel_exists(channel, sku):
                    continue
                if self.is_suppressed(channel, sku):
                    continue
                self.db.queue_stock_sync(
                    channel,
                    sku,
                    quantity,
                    reason,
                )
            await self.flush_pending()
            return quantity

    async def suppress_channel(
        self,
        channel: str,
        sku: str,
        reason: str = "marketplace_stock",
    ) -> None:
        async with self._lock:
            sku = str(sku).strip()
            if not self.channel_exists(channel, sku):
                raise RuntimeError(
                    f"Товар {sku} отсутствует на канале {channel}"
                )
            await self._write_channel(channel, sku, 0, force=True)
            self.db.set_channel_suppressed(
                channel, sku, True, reason=reason
            )

    async def restore_channel(
        self,
        channel: str,
        sku: str,
    ) -> int:
        async with self._lock:
            sku = str(sku).strip()
            quantity = self.ensure_local(sku)
            await self._write_channel(
                channel, sku, quantity, force=True
            )
            self.db.set_channel_suppressed(
                channel, sku, False, reason=""
            )
            self.db.delete_stock_sync_pending(channel, sku)
            return quantity

    async def queue_current_local(
        self,
        channel: str,
        sku: str,
        reason: str,
    ) -> None:
        sku = str(sku).strip()
        if not self.channel_exists(channel, sku):
            return
        if self.is_suppressed(channel, sku):
            return
        quantity = self.ensure_local(sku)
        self.db.queue_stock_sync(channel, sku, quantity, reason)

    async def flush_pending(
        self,
        channel: str | None = None,
    ) -> None:
        for row in self.db.list_stock_sync_pending():
            target = row["channel"]
            sku = row["sku"]
            if channel is not None and target != channel:
                continue
            if self.is_suppressed(target, sku):
                self.db.delete_stock_sync_pending(target, sku)
                continue
            if not self.channel_exists(target, sku):
                self.db.delete_stock_sync_pending(target, sku)
                continue
            try:
                await self._write_channel(
                    target,
                    sku,
                    int(row["quantity"]),
                )
            except Exception:
                log.exception(
                    "Stock sync failed: channel=%s sku=%s qty=%s",
                    target,
                    sku,
                    row["quantity"],
                )
                continue
            self.db.delete_stock_sync_pending(target, sku)

    async def _write_channel(
        self,
        channel: str,
        sku: str,
        quantity: int,
        force: bool = False,
    ) -> None:
        quantity = max(0, int(quantity))
        if not force and self.is_suppressed(channel, sku):
            return

        if channel == "wb":
            product = self._wb_product(sku)
            if product is None:
                raise RuntimeError(f"WB не знает артикул {sku}")
            if self.service.warehouse is None:
                raise RuntimeError("WB FBS-склад не определён")
            if len(product.chrt_ids) != 1:
                raise RuntimeError(
                    f"Нельзя автоматически синхронизировать WB {sku}: "
                    "у товара несколько chrtId"
                )
            await self.wb.set_fbs_stocks(
                self.service.warehouse.id,
                {product.chrt_ids[0]: quantity},
            )
            self.service.fbs_stock[product.nm_id] = quantity
            self.db.update_many("fbs", {product.nm_id: quantity})
            return

        if channel == "ozon":
            if self.ozon is None:
                raise RuntimeError("OZON-интеграция отключена")
            if self.ozon_warehouse_id is None:
                raise RuntimeError(
                    "OZON_WAREHOUSE_ID не определён"
                )
            await self.ozon.set_fbs_stock(
                self.ozon_warehouse_id,
                sku,
                quantity,
            )
            self.db.set_channel_stock("ozon_fbs", sku, quantity)
            return

        raise RuntimeError(f"Неизвестный канал остатков: {channel}")

    async def loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                await self.flush_pending()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Shared stock sync loop failed")
