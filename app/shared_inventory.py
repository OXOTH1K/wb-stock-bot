from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import TYPE_CHECKING

from .db import StateDB

if TYPE_CHECKING:
    from .orders import FBSOrder
    from .ozon import OzonIntegration
    from .ozon_client import OzonPosting
    from .service import StockMonitorService

log = logging.getLogger(__name__)


class SharedInventoryService:
    """Manage physical local stock and the shared order-available pool."""

    def __init__(
        self,
        db: StateDB,
        wb_service: "StockMonitorService",
        ozon: "OzonIntegration | None" = None,
    ):
        self.db = db
        self.wb_service = wb_service
        self.ozon = ozon
        self._lock = asyncio.Lock()

    @staticmethod
    def _wb_sku(product) -> str:
        value = str(product.vendor_code or "").strip()
        return value or f"WB-{int(product.nm_id)}"

    def _wb_by_sku(self) -> dict[str, object]:
        return {
            self._wb_sku(product): product
            for product in self.wb_service.products.values()
        }

    def all_skus(self) -> set[str]:
        return set(self._wb_by_sku()) | set(
            self.db.get_channel_catalog("ozon")
        )

    def local_quantity(self, sku: str) -> int:
        return int(
            self.db.get_local_stock((str(sku),)).get(str(sku), 0)
        )

    def available_quantity(self, sku: str) -> int:
        return int(
            self.db.get_order_available((str(sku),)).get(
                str(sku), 0
            )
        )

    def _clear_wb_decisions(self, sku: str) -> None:
        product = self._wb_by_sku().get(str(sku))
        if product is not None:
            self.db.clear_stock_decisions(product.nm_id)

    def is_suppressed(self, channel: str, sku: str) -> bool:
        return self.db.is_channel_suppressed(channel, sku)

    async def initialize(self) -> None:
        wb_by_sku = self._wb_by_sku()
        all_skus = sorted(self.all_skus())
        ozon_stock = self.db.get_channel_stock(
            "ozon_fbs", tuple(all_skus)
        )

        # First preserve the old single-pool quantity as physical total.
        for sku in all_skus:
            wb_product = wb_by_sku.get(sku)
            if wb_product is not None:
                saved = self.db.get_saved_product_fbs(
                    "wb_auto", wb_product.nm_id
                )
                legacy_total = (
                    sum(int(value) for value in saved.values())
                    if saved
                    else int(
                        self.wb_service.fbs_stock.get(
                            wb_product.nm_id, 0
                        )
                    )
                )
            else:
                legacy_total = int(ozon_stock.get(sku, 0))
            self.db.ensure_local_stock(sku, legacy_total)

        # Legacy per-channel suppression becomes a zero shared sellable pool.
        migrated: set[int] = set()
        for (nm_id, _chrt_id), _quantity in self.db.get_saved_fbs(
            "wb_auto"
        ).items():
            if nm_id in migrated:
                continue
            migrated.add(nm_id)
            product = self.wb_service.products.get(int(nm_id))
            if product is None:
                continue
            sku = self._wb_sku(product)
            if self.wb_service.fbs_stock.get(
                product.nm_id, 0
            ) == 0:
                self.db.set_channel_suppressed(
                    "wb", sku, "marketplace_stock"
                )

        # One-time per-SKU migration from the old single pool:
        # currently sellable FBS stock moves out of "Мой склад" into
        # "Доступно для заказа". This preserves local+available total.
        existing_available = self.db.get_order_available(
            tuple(all_skus)
        )
        for sku in all_skus:
            if sku in existing_available:
                continue
            suppressed = (
                self.db.is_channel_suppressed("wb", sku)
                or self.db.is_channel_suppressed("ozon", sku)
            )
            wb_qty = 0
            product = wb_by_sku.get(sku)
            if product is not None:
                wb_qty = int(
                    self.wb_service.fbs_stock.get(
                        product.nm_id, 0
                    )
                )
            ozon_qty = int(ozon_stock.get(sku, 0))
            current_marketplace = (
                0 if suppressed else max(wb_qty, ozon_qty)
            )
            local = self.local_quantity(sku)
            target = min(local, current_marketplace)
            self.db.ensure_order_available(sku, 0)
            if target:
                self.db.transfer_order_available(
                    sku,
                    target,
                    reason="migration_from_legacy_pool",
                )

    def _ensure_available_for_sale(
        self,
        source: str,
        sku: str,
        pending_quantity: int,
    ) -> None:
        existing = self.db.get_order_available((sku,))
        if sku in existing:
            return

        candidates: list[int] = []
        wb_product = self._wb_by_sku().get(sku)
        if wb_product is not None:
            current = int(
                self.wb_service.fbs_stock.get(
                    wb_product.nm_id, 0
                )
            )
            if source == "wb":
                current += int(pending_quantity)
            candidates.append(current)

        ozon_current = self.db.get_channel_stock(
            "ozon_fbs", (sku,)
        ).get(sku)
        if ozon_current is not None:
            current = int(ozon_current)
            if source == "ozon":
                current += int(pending_quantity)
            candidates.append(current)

        self.db.ensure_local_stock(sku, 0)
        self.db.ensure_order_available(
            sku,
            max(candidates)
            if candidates
            else int(pending_quantity),
        )

    async def consume_sales(
        self,
        source: str,
        events: list[tuple[str, str, int]],
    ) -> dict[str, int]:
        if not events:
            return {}

        async with self._lock:
            pending_by_sku: dict[str, int] = defaultdict(int)
            for _event_id, sku, quantity in events:
                clean_sku = str(sku).strip()
                if clean_sku and int(quantity) > 0:
                    pending_by_sku[clean_sku] += int(quantity)

            for sku, quantity in pending_by_sku.items():
                self._ensure_available_for_sale(
                    source, sku, quantity
                )

            changed: dict[str, int] = {}
            for event_id, sku, quantity in events:
                clean_sku = str(sku).strip()
                if not clean_sku or int(quantity) <= 0:
                    continue
                applied, before, after = self.db.apply_available_sale_once(
                    source,
                    str(event_id),
                    clean_sku,
                    int(quantity),
                )
                if not applied:
                    continue
                changed[clean_sku] = after
                self._clear_wb_decisions(clean_sku)
                if before < int(quantity):
                    log.warning(
                        "Order-available inventory underflow prevented for %s: "
                        "before=%d sale=%d source=%s",
                        clean_sku,
                        before,
                        int(quantity),
                        source,
                    )

            for sku in changed:
                await self.sync_sku(
                    sku,
                    raise_errors=False,
                )
            return changed

    async def consume_wb_orders(
        self, orders: list["FBSOrder"]
    ) -> dict[str, int]:
        return await self.consume_sales(
            "wb",
            [
                (str(order.id), order.article, 1)
                for order in orders
                if order.article
            ],
        )

    async def consume_ozon_postings(
        self, postings: list["OzonPosting"]
    ) -> dict[str, int]:
        events: list[tuple[str, str, int]] = []
        for posting in postings:
            totals: dict[str, int] = defaultdict(int)
            for product in posting.products:
                if product.offer_id and product.quantity > 0:
                    totals[product.offer_id] += int(product.quantity)
            for sku, quantity in totals.items():
                events.append(
                    (posting.posting_number, sku, quantity)
                )
        return await self.consume_sales("ozon", events)

    async def set_local_stock(
        self,
        sku: str,
        quantity: int,
        reason: str = "crm",
    ) -> int:
        """Edit physical local stock without changing marketplace FBS."""
        async with self._lock:
            result = self.db.set_local_stock(
                sku, quantity, reason=reason
            )
            self._clear_wb_decisions(sku)
            return result

    async def set_available_stock(
        self,
        sku: str,
        quantity: int,
        reason: str = "manual",
        *,
        force: bool = True,
    ) -> int:
        """Move stock between local and sellable pool, then sync both FBS."""
        async with self._lock:
            _local, available = self.db.transfer_order_available(
                sku, quantity, reason=reason
            )
            self._clear_wb_decisions(sku)
            if available > 0:
                self.db.clear_channel_suppressed("wb", sku)
                self.db.clear_channel_suppressed("ozon", sku)
            await self.sync_sku(
                sku,
                raise_errors=True,
                force=force,
            )
            return available

    async def _write_wb(self, sku: str, quantity: int) -> None:
        product = self._wb_by_sku().get(sku)
        if product is None:
            return
        if self.wb_service.warehouse is None:
            raise RuntimeError("WB seller warehouse is not initialized")
        if len(product.chrt_ids) != 1:
            raise RuntimeError(
                f"WB SKU {sku} has multiple variants; "
                "shared stock cannot choose a chrtId safely"
            )
        await self.wb_service.wb.set_fbs_stocks(
            self.wb_service.warehouse.id,
            {product.chrt_ids[0]: int(quantity)},
        )
        self.wb_service.fbs_stock[product.nm_id] = int(quantity)
        self.db.update_many(
            "fbs", {product.nm_id: int(quantity)}
        )
        self.db.update_many(
            "total",
            {
                product.nm_id: int(quantity)
                + int(
                    self.wb_service.wb_stock.get(
                        product.nm_id, 0
                    )
                )
            },
        )

    async def _write_ozon(self, sku: str, quantity: int) -> None:
        if self.ozon is None:
            return
        if sku not in self.db.get_channel_catalog("ozon"):
            return
        await self.ozon.set_fbs_stock(sku, int(quantity))

    async def sync_sku(
        self,
        sku: str,
        *,
        skip_channel: str | None = None,
        raise_errors: bool = True,
        force: bool = False,
    ) -> None:
        sku = str(sku).strip()
        if not sku:
            return
        quantity = self.available_quantity(sku)
        errors: list[str] = []

        if skip_channel != "wb":
            product = self._wb_by_sku().get(sku)
            if product is not None:
                current = int(
                    self.wb_service.fbs_stock.get(
                        product.nm_id, 0
                    )
                )
                if force or current != quantity:
                    try:
                        await self._write_wb(sku, quantity)
                    except Exception as exc:
                        errors.append(f"WB: {exc}")

        if (
            skip_channel != "ozon"
            and self.ozon is not None
            and sku in self.db.get_channel_catalog("ozon")
        ):
            current = self.db.get_channel_stock(
                "ozon_fbs", (sku,)
            ).get(sku)
            if force or current is None or int(current) != quantity:
                try:
                    await self._write_ozon(sku, quantity)
                except Exception as exc:
                    errors.append(f"OZON: {exc}")

        if errors:
            message = (
                f"Shared stock sync failed for {sku}: "
                + " | ".join(errors)
            )
            if raise_errors:
                raise RuntimeError(message)
            log.error(message)

    async def suppress_channel(
        self,
        channel: str,
        sku: str,
        reason: str = "marketplace_stock",
    ) -> None:
        """Compatibility action: zero the shared available pool.

        Since WB and OZON FBS now mirror one shared sellable quantity, a
        marketplace-stock suppression zeros both FBS channels and returns the
        sellable units to the physical local warehouse.
        """
        if channel not in {"wb", "ozon"}:
            raise ValueError(f"Unknown channel: {channel}")
        async with self._lock:
            before = self.available_quantity(sku)
            if before > 0:
                self.db.save_available_snapshot(
                    f"marketplace:{channel}",
                    sku,
                    before,
                )
                self.db.transfer_order_available(
                    sku,
                    0,
                    reason=f"{channel}_{reason}_zero",
                )
            self.db.set_channel_suppressed(
                channel, sku, reason
            )
            await self.sync_sku(
                sku, raise_errors=True, force=True
            )

    async def restore_channel(
        self,
        channel: str,
        sku: str,
    ) -> int:
        if channel not in {"wb", "ozon"}:
            raise ValueError(f"Unknown channel: {channel}")
        async with self._lock:
            snapshot = self.db.get_available_snapshot(
                f"marketplace:{channel}"
            )
            target = int(snapshot.get(sku, 0))
            if target <= 0:
                target = min(
                    self.local_quantity(sku),
                    self.available_quantity(sku),
                )
            if target > self.local_quantity(sku):
                target = self.local_quantity(sku)
            _local, available = self.db.transfer_order_available(
                sku,
                target,
                reason=f"{channel}_marketplace_restore",
            )
            self.db.clear_channel_suppressed(channel, sku)
            self.db.clear_available_snapshot(
                f"marketplace:{channel}", sku
            )
            await self.sync_sku(
                sku, raise_errors=True, force=True
            )
            return available

    async def _suppress_mass(
        self, scope: str, skus: list[str]
    ) -> tuple[int, int]:
        total = 0
        count = 0
        for sku in skus:
            current = self.available_quantity(sku)
            if current <= 0:
                continue
            self.db.save_available_snapshot(
                scope, sku, current
            )
            self.db.transfer_order_available(
                sku, 0, reason=f"{scope}_zero"
            )
            total += current
            count += 1
        for sku in skus:
            await self.sync_sku(
                sku, raise_errors=True, force=True
            )
        return count, total

    async def _restore_mass(
        self, scope: str
    ) -> tuple[int, int]:
        snapshot = self.db.get_available_snapshot(scope)
        restored = 0
        total = 0
        for sku, wanted in snapshot.items():
            target = min(
                int(wanted), self.local_quantity(sku)
            )
            self.db.transfer_order_available(
                sku, target, reason=f"{scope}_restore"
            )
            await self.sync_sku(
                sku, raise_errors=True, force=True
            )
            restored += 1
            total += target
        self.db.clear_available_snapshot(scope)
        return restored, total

    async def suppress_wb_mass(self) -> tuple[int, int]:
        async with self._lock:
            skus = sorted(self.all_skus())
            for sku in skus:
                self.db.set_channel_suppressed(
                    "wb", sku, "mass"
                )
            return await self._suppress_mass(
                "mass_shared", skus
            )

    async def restore_wb_mass(self) -> tuple[int, int]:
        async with self._lock:
            restored, total = await self._restore_mass(
                "mass_shared"
            )
            self.db.clear_channel_suppressions_by_reason(
                "wb", "mass"
            )
            self.db.clear_channel_suppressions_by_reason(
                "ozon", "mass"
            )
            return restored, total

    async def suppress_ozon_mass(self) -> tuple[int, int]:
        if self.ozon is None:
            raise RuntimeError(
                "OZON integration is not configured"
            )
        async with self._lock:
            skus = sorted(self.all_skus())
            for sku in skus:
                self.db.set_channel_suppressed(
                    "ozon", sku, "mass"
                )
            return await self._suppress_mass(
                "mass_shared", skus
            )

    async def restore_ozon_mass(self) -> tuple[int, int]:
        if self.ozon is None:
            raise RuntimeError(
                "OZON integration is not configured"
            )
        async with self._lock:
            restored, total = await self._restore_mass(
                "mass_shared"
            )
            self.db.clear_channel_suppressions_by_reason(
                "wb", "mass"
            )
            self.db.clear_channel_suppressions_by_reason(
                "ozon", "mass"
            )
            return restored, total

    async def reconcile_all(self) -> None:
        for sku in sorted(self.all_skus()):
            await self.sync_sku(
                sku, raise_errors=False
            )

    async def loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                async with self._lock:
                    await self.reconcile_all()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Shared inventory reconciliation failed")
