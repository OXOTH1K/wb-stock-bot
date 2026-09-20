from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable

from .db import StateDB
from .ozon_client import OzonClient
from .service import StockMonitorService

log = logging.getLogger(__name__)


class SharedInventory:
    """Local inventory is the source of truth for marketplace FBS stocks."""

    def __init__(
        self,
        db: StateDB,
        wb_service: StockMonitorService,
        ozon_client: OzonClient | None = None,
        ozon_warehouse_id: int = 0,
    ):
        self.db = db
        self.wb_service = wb_service
        self.ozon_client = ozon_client
        self.ozon_warehouse_id = int(ozon_warehouse_id or 0)
        self._lock = asyncio.Lock()

    def _wb_by_sku(self) -> dict[str, object]:
        return {
            str(product.vendor_code or f"WB-{product.nm_id}").strip(): product
            for product in self.wb_service.products.values()
        }

    def _bootstrap_wb_quantity(self, product) -> int:
        saved = self.db.get_saved_product_fbs("wb_auto", product.nm_id)
        if saved:
            return sum(int(qty) for qty in saved.values())
        return int(self.wb_service.fbs_stock.get(product.nm_id, 0))

    async def configure_ozon_warehouse(self) -> int:
        if self.ozon_client is None:
            return 0
        warehouses = await self.ozon_client.get_fbs_warehouses()
        ids = [int(row.get("warehouse_id") or 0) for row in warehouses]
        ids = [warehouse_id for warehouse_id in ids if warehouse_id > 0]

        if self.ozon_warehouse_id:
            if ids and self.ozon_warehouse_id not in ids:
                raise RuntimeError(
                    "OZON_WAREHOUSE_ID is not present in active FBS warehouses"
                )
            return self.ozon_warehouse_id

        if len(ids) == 1:
            self.ozon_warehouse_id = ids[0]
            return ids[0]
        if not ids:
            raise RuntimeError("Ozon active FBS warehouse was not found")
        raise RuntimeError(
            "Ozon has multiple active FBS warehouses; set OZON_WAREHOUSE_ID"
        )

    def bootstrap_local_inventory(self) -> None:
        wb_by_sku = self._wb_by_sku()
        ozon_catalog = self.db.get_channel_catalog("ozon")
        ozon_stock = self.db.get_channel_stock(
            "ozon_fbs", tuple(ozon_catalog)
        )
        all_skus = set(wb_by_sku) | set(ozon_catalog)

        for sku in all_skus:
            product = wb_by_sku.get(sku)
            if product is not None:
                saved = self.db.get_saved_product_fbs(
                    "wb_auto", product.nm_id
                )
                quantity = self._bootstrap_wb_quantity(product)
                if (
                    saved
                    and int(
                        self.wb_service.fbs_stock.get(
                            product.nm_id, 0
                        )
                    ) == 0
                ):
                    self.db.set_channel_suppressed(
                        "wb", sku, True
                    )
            else:
                quantity = int(ozon_stock.get(sku, 0))
            self.db.ensure_local_stock(sku, quantity)

    def local_quantity(self, sku: str) -> int:
        value = self.db.local_stock_quantity(str(sku).strip())
        return 0 if value is None else int(value)

    def is_suppressed(self, channel: str, sku: str) -> bool:
        return self.db.is_channel_suppressed(channel, sku)

    def set_suppressed(
        self, channel: str, sku: str, suppressed: bool
    ) -> None:
        self.db.set_channel_suppressed(channel, sku, suppressed)

    def _ensure_before_sale(
        self,
        channel: str,
        sku: str,
        quantity: int,
    ) -> None:
        if self.db.local_stock_quantity(sku) is not None:
            return

        current = 0
        if channel == "wb":
            product = self._wb_by_sku().get(sku)
            if product is not None:
                current = int(
                    self.wb_service.fbs_stock.get(product.nm_id, 0)
                )
        elif channel == "ozon":
            current = int(
                self.db.get_channel_stock("ozon_fbs", (sku,)).get(
                    sku, 0
                )
            )
        self.db.ensure_local_stock(sku, current + int(quantity))

    async def apply_sale(
        self,
        channel: str,
        event_id: str,
        items: Iterable[tuple[str, int]],
    ) -> dict[str, int]:
        """Apply one marketplace order once and sync the other channels."""
        changed: dict[str, int] = {}
        touched: set[str] = set()
        async with self._lock:
            for raw_sku, raw_quantity in items:
                sku = str(raw_sku).strip()
                quantity = max(0, int(raw_quantity))
                if not sku or quantity <= 0:
                    continue
                touched.add(sku)
                self._ensure_before_sale(
                    channel, sku, quantity
                )
                applied, before, after = self.db.record_inventory_sale(
                    channel,
                    str(event_id),
                    sku,
                    quantity,
                )
                if not applied:
                    continue
                changed[sku] = after
                if before < quantity:
                    log.warning(
                        "Sale %s/%s oversold local inventory for %s: "
                        "local=%d sale=%d",
                        channel,
                        event_id,
                        sku,
                        before,
                        quantity,
                    )

            # Sync all touched SKUs even when the sale event was already
            # recorded. This makes marketplace write failures retryable on the
            # next order poll without decrementing local inventory twice.
            for sku in touched:
                await self._sync_sku_locked(
                    sku, exclude_channel=channel
                )
        return changed

    async def set_local_quantity(
        self,
        sku: str,
        quantity: int,
        reason: str = "crm_set",
    ) -> int:
        async with self._lock:
            value = self.db.set_local_stock(
                sku, int(quantity), reason=reason
            )
            await self._sync_sku_locked(sku)
            return value

    async def sync_sku(
        self, sku: str, exclude_channel: str | None = None
    ) -> None:
        async with self._lock:
            await self._sync_sku_locked(
                str(sku).strip(),
                exclude_channel=exclude_channel,
            )

    async def sync_all(self) -> None:
        async with self._lock:
            wb_by_sku = self._wb_by_sku()
            ozon_catalog = self.db.get_channel_catalog("ozon")

            wb_quantities: dict[int, int] = {}
            wb_nm_quantities: dict[int, int] = {}
            if self.wb_service.warehouse is not None:
                for sku, product in wb_by_sku.items():
                    if self.db.is_channel_suppressed("wb", sku):
                        continue
                    local = self.db.local_stock_quantity(sku)
                    if local is None:
                        continue
                    if len(product.chrt_ids) != 1:
                        log.warning(
                            "Skipping automatic WB sync for %s: %d variants",
                            sku,
                            len(product.chrt_ids),
                        )
                        continue
                    wb_quantities[product.chrt_ids[0]] = int(local)
                    wb_nm_quantities[product.nm_id] = int(local)

            if wb_quantities and self.wb_service.warehouse is not None:
                actual_wb_quantities = {
                    chrt_id: quantity
                    for chrt_id, quantity in wb_quantities.items()
                    if self.wb_service.fbs_stock.get(
                        next(
                            product.nm_id
                            for product in wb_by_sku.values()
                            if chrt_id in product.chrt_ids
                        ),
                        0,
                    )
                    != quantity
                }
                if actual_wb_quantities:
                    await self.wb_service.wb.set_fbs_stocks(
                        self.wb_service.warehouse.id,
                        actual_wb_quantities,
                    )
                    changed_nm = {
                        product.nm_id: wb_nm_quantities[product.nm_id]
                        for product in wb_by_sku.values()
                        if any(
                            chrt_id in actual_wb_quantities
                            for chrt_id in product.chrt_ids
                        )
                    }
                    self.wb_service.fbs_stock.update(changed_nm)
                    self.db.update_many("fbs", changed_nm)

            if (
                self.ozon_client is not None
                and self.ozon_warehouse_id > 0
            ):
                ozon_quantities: dict[str, int] = {}
                for sku in ozon_catalog:
                    if self.db.is_channel_suppressed("ozon", sku):
                        continue
                    local = self.db.local_stock_quantity(sku)
                    if local is None:
                        continue
                    ozon_quantities[sku] = int(local)
                if ozon_quantities:
                    current_ozon = self.db.get_channel_stock(
                        "ozon_fbs", tuple(ozon_quantities)
                    )
                    changed_ozon = {
                        sku: quantity
                        for sku, quantity in ozon_quantities.items()
                        if current_ozon.get(sku) != quantity
                    }
                    if changed_ozon:
                        await self.ozon_client.set_fbs_stocks(
                            self.ozon_warehouse_id,
                            changed_ozon,
                        )
                        for sku, quantity in changed_ozon.items():
                            self.db.set_channel_stock(
                                "ozon_fbs", sku, quantity
                            )

    async def sync_loop(self, interval: int = 60) -> None:
        while True:
            await asyncio.sleep(max(30, int(interval)))
            try:
                await self.sync_all()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Shared inventory periodic sync failed")

    async def suppress_channel(
        self, channel: str, sku: str
    ) -> None:
        async with self._lock:
            await self._write_channel_locked(channel, sku, 0)
            self.db.set_channel_suppressed(channel, sku, True)

    async def restore_channel(
        self, channel: str, sku: str
    ) -> int:
        async with self._lock:
            quantity = self.local_quantity(sku)
            await self._write_channel_locked(
                channel, sku, quantity
            )
            self.db.set_channel_suppressed(channel, sku, False)
            return quantity

    async def _sync_sku_locked(
        self,
        sku: str,
        exclude_channel: str | None = None,
    ) -> None:
        quantity = self.local_quantity(sku)
        if (
            exclude_channel != "wb"
            and not self.db.is_channel_suppressed("wb", sku)
            and sku in self._wb_by_sku()
        ):
            await self._write_wb_locked(sku, quantity)

        if (
            exclude_channel != "ozon"
            and not self.db.is_channel_suppressed("ozon", sku)
            and sku in self.db.get_channel_catalog("ozon")
            and self.ozon_client is not None
            and self.ozon_warehouse_id > 0
        ):
            await self._write_ozon_locked(sku, quantity)

    async def _write_channel_locked(
        self, channel: str, sku: str, quantity: int
    ) -> None:
        if channel == "wb":
            await self._write_wb_locked(sku, quantity)
            return
        if channel == "ozon":
            await self._write_ozon_locked(sku, quantity)
            return
        raise ValueError(f"Unknown inventory channel: {channel}")

    async def _write_wb_locked(
        self, sku: str, quantity: int
    ) -> None:
        product = self._wb_by_sku().get(sku)
        if product is None:
            return
        if self.wb_service.warehouse is None:
            raise RuntimeError("WB seller warehouse is not initialized")
        if len(product.chrt_ids) != 1 and int(quantity) != 0:
            raise RuntimeError(
                f"WB stock sync requires one chrtId for positive stock on {sku}; "
                f"found {len(product.chrt_ids)}"
            )
        quantities = (
            {chrt_id: 0 for chrt_id in product.chrt_ids}
            if int(quantity) == 0
            else {product.chrt_ids[0]: int(quantity)}
        )
        current = int(
            self.wb_service.fbs_stock.get(product.nm_id, 0)
        )
        if current == int(quantity):
            return
        await self.wb_service.wb.set_fbs_stocks(
            self.wb_service.warehouse.id,
            quantities,
        )
        self.wb_service.fbs_stock[product.nm_id] = int(quantity)
        self.db.update_many(
            "fbs", {product.nm_id: int(quantity)}
        )

    async def _write_ozon_locked(
        self, sku: str, quantity: int
    ) -> None:
        if self.ozon_client is None:
            return
        if self.ozon_warehouse_id <= 0:
            raise RuntimeError(
                "Ozon FBS warehouse is not configured"
            )
        if sku not in self.db.get_channel_catalog("ozon"):
            return
        current = self.db.get_channel_stock(
            "ozon_fbs", (sku,)
        ).get(sku)
        if current == int(quantity):
            return
        await self.ozon_client.set_fbs_stocks(
            self.ozon_warehouse_id,
            {sku: int(quantity)},
        )
        self.db.set_channel_stock(
            "ozon_fbs", sku, int(quantity)
        )
