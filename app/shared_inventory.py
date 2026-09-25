from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import TYPE_CHECKING

from .db import StateDB
from .channel_fbs import ChannelFBSControl

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
        self.channel_fbs = ChannelFBSControl(self)

    async def zero_fbs_channel(self, channel):
        async with self._lock:
            return await self.channel_fbs.zero(channel)

    async def restore_fbs_channel(self, channel):
        async with self._lock:
            return await self.channel_fbs.restore(channel)

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
        return self.db.is_channel_suppressed(channel, sku) or self.channel_fbs.paused(channel, sku)

    async def initialize(self) -> None:
        wb_by_sku = self._wb_by_sku()
        all_skus = sorted(self.all_skus())
        ozon_stock = self.db.get_channel_stock(
            "ozon_fbs", tuple(all_skus)
        )
        existing_available = self.db.get_order_available(
            tuple(all_skus)
        )
        full_local_model = (
            self.db.get_meta("inventory_model_full_local_v1")
            == "1"
        )

        # Ensure a physical-stock row exists for every catalog SKU. On old
        # installations this value is the legacy single stock pool.
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

        # Preserve legacy WB per-platform suppression.
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

        changed_skus: set[str] = set()
        for sku in all_skus:
            wb_qty = 0
            product = wb_by_sku.get(sku)
            if product is not None:
                wb_qty = int(
                    self.wb_service.fbs_stock.get(
                        product.nm_id, 0
                    )
                )
            ozon_qty = int(ozon_stock.get(sku, 0))
            wb_reason = self.db.get_channel_suppression_reason(
                "wb", sku
            )
            ozon_reason = self.db.get_channel_suppression_reason(
                "ozon", sku
            )
            marketplace_suppressed = (
                wb_reason == "marketplace_stock"
                or ozon_reason == "marketplace_stock"
            )
            mass_suppressed = (
                wb_reason == "mass" or ozon_reason == "mass"
            )
            local = self.local_quantity(sku)

            if sku not in existing_available:
                # Legacy/fresh install: local_inventory already represents
                # the full physical quantity. Initialize only the sellable
                # limit; do not subtract anything from physical stock.
                current_marketplace = max(wb_qty, ozon_qty)
                if (
                    current_marketplace <= 0
                    and marketplace_suppressed
                    and not mass_suppressed
                ):
                    snapshots = [
                        int(
                            self.db.get_available_snapshot(
                                "marketplace:wb"
                            ).get(sku, 0)
                        ),
                        int(
                            self.db.get_available_snapshot(
                                "marketplace:ozon"
                            ).get(sku, 0)
                        ),
                    ]
                    current_marketplace = max(
                        [local, *snapshots]
                    )
                target = (
                    0
                    if mass_suppressed
                    else min(local, current_marketplace)
                )
                self.db.ensure_order_available(sku, target)
                continue

            available = int(existing_available.get(sku, 0))
            if not full_local_model:
                # PR #27-29 used split accounting: local_inventory contained
                # only the reserve outside "Доступно". Convert once so
                # local_inventory becomes the full physical quantity.
                #
                # Exception: an individual suppression in PR #27 may already
                # have returned available stock to local and left available=0.
                # In that case restore only the sellable limit, without adding
                # it to local again.
                recovered_from_shared_zero = False
                if (
                    available == 0
                    and marketplace_suppressed
                    and not mass_suppressed
                    and local > 0
                ):
                    recovery_candidates = [
                        int(
                            self.db.get_available_snapshot(
                                "marketplace:wb"
                            ).get(sku, 0)
                        ),
                        int(
                            self.db.get_available_snapshot(
                                "marketplace:ozon"
                            ).get(sku, 0)
                        ),
                    ]
                    if wb_reason != "marketplace_stock":
                        recovery_candidates.append(wb_qty)
                    if ozon_reason != "marketplace_stock":
                        recovery_candidates.append(ozon_qty)
                    recovered = max(recovery_candidates)
                    if recovered <= 0:
                        recovered = local
                    recovered = min(local, recovered)
                    if recovered > 0:
                        self.db.set_order_available(
                            sku,
                            recovered,
                            reason=(
                                "migration_restore_platform_"
                                "suppression_limit"
                            ),
                        )
                        available = recovered
                        recovered_from_shared_zero = True
                        changed_skus.add(sku)

                if available > 0 and not recovered_from_shared_zero:
                    self.db.set_local_stock(
                        sku,
                        local + available,
                        reason="migration_full_local_stock",
                    )
                    local += available

            # Enforce the new invariant even for manually edited/stale DBs.
            available = self.available_quantity(sku)
            local = self.local_quantity(sku)
            if available > local:
                self.db.set_order_available(
                    sku,
                    local,
                    reason="clamp_to_full_local_stock",
                )
                changed_skus.add(sku)

            self.db.clear_available_snapshot(
                "marketplace:wb", sku
            )
            self.db.clear_available_snapshot(
                "marketplace:ozon", sku
            )

        self.db.set_meta("inventory_model_full_local_v1", "1")
        self.channel_fbs.migrate_legacy()

        for sku in sorted(changed_skus):
            await self.sync_sku(
                sku,
                raise_errors=False,
                force=True,
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

        bootstrap = (
            max(candidates)
            if candidates
            else int(pending_quantity)
        )
        self.db.ensure_local_stock(sku, bootstrap)
        local = self.local_quantity(sku)
        self.db.ensure_order_available(
            sku,
            min(local, bootstrap),
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
                self.channel_fbs.consume(clean_sku, int(quantity))
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
        """Edit full physical stock; clamp sellable stock only if necessary."""
        async with self._lock:
            result = self.db.set_local_stock(
                sku, quantity, reason=reason
            )
            self._clear_wb_decisions(sku)
            if self.available_quantity(sku) > result:
                self.db.set_order_available(
                    sku,
                    result,
                    reason=f"{reason}:clamp_available",
                )
                await self.sync_sku(
                    sku,
                    raise_errors=True,
                    force=True,
                )
            return result

    async def set_available_stock(
        self,
        sku: str,
        quantity: int,
        reason: str = "manual",
        *,
        force: bool = False,
    ) -> int:
        """Set sellable limit without changing full physical stock."""
        async with self._lock:
            available = self.db.set_order_available(
                sku, quantity, reason=reason
            )
            self.channel_fbs.explicit_set(sku)
            self._clear_wb_decisions(sku)
            # An explicit available-stock edit supersedes only mass-zero
            # restoration. Platform-specific marketplace_stock suppression
            # must remain in place until that marketplace warehouse is empty.
            self.db.clear_available_snapshot("mass_shared", sku)
            for channel in ("wb", "ozon"):
                if (
                    self.db.get_channel_suppression_reason(
                        channel, sku
                    )
                    == "mass"
                ):
                    self.db.clear_channel_suppressed(channel, sku)
            # Old releases stored per-platform restore snapshots. They are no
            # longer needed because platform restoration uses the current
            # shared available quantity.
            self.db.clear_available_snapshot("marketplace:wb", sku)
            self.db.clear_available_snapshot("marketplace:ozon", sku)
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
        variants = self.channel_fbs.wb_variants(sku, quantity)
        if not product.chrt_ids or (quantity != 0 and len(product.chrt_ids) != 1 and variants is None):
            raise RuntimeError(
                f"WB SKU {sku} has multiple variants; "
                "shared stock cannot choose a chrtId safely"
            )
        await self.wb_service.wb.set_fbs_stocks(
            self.wb_service.warehouse.id,
            variants if variants is not None else {chrt_id: int(quantity) for chrt_id in product.chrt_ids},
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
        wb_target = (
            0
            if self.db.get_channel_suppression_reason(
                "wb", sku
            )
            == "marketplace_stock"
            else quantity
        )
        ozon_target = (
            0
            if self.db.get_channel_suppression_reason(
                "ozon", sku
            )
            == "marketplace_stock"
            else quantity
        )
        wb_target = self.channel_fbs.target("wb", sku, wb_target)
        ozon_target = self.channel_fbs.target("ozon", sku, ozon_target)
        if self.db.get_channel_suppression_reason("wb", sku) == "marketplace_stock":
            wb_target = 0
        if self.db.get_channel_suppression_reason("ozon", sku) == "marketplace_stock":
            ozon_target = 0

        if skip_channel != "wb":
            product = self._wb_by_sku().get(sku)
            if product is not None:
                current = int(
                    self.wb_service.fbs_stock.get(
                        product.nm_id, 0
                    )
                )
                if force or current != wb_target:
                    try:
                        await self._write_wb(sku, wb_target)
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
            if (
                force
                or current is None
                or int(current) != ozon_target
            ):
                try:
                    await self._write_ozon(sku, ozon_target)
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
        """Suppress one marketplace FBS without changing shared inventory."""
        if channel not in {"wb", "ozon"}:
            raise ValueError(f"Unknown channel: {channel}")
        async with self._lock:
            self.db.set_channel_suppressed(
                channel, sku, reason
            )
            # Remove stale snapshots left by the short-lived shared-zero
            # implementation. Platform restore now uses current available.
            self.db.clear_available_snapshot(
                f"marketplace:{channel}", sku
            )
            if channel == "wb":
                await self._write_wb(sku, 0)
            else:
                await self._write_ozon(sku, 0)

    async def restore_channel(
        self,
        channel: str,
        sku: str,
    ) -> int:
        """Restore one marketplace FBS to current shared available stock."""
        if channel not in {"wb", "ozon"}:
            raise ValueError(f"Unknown channel: {channel}")
        async with self._lock:
            quantity = self.available_quantity(sku)
            if channel == "wb":
                await self._write_wb(sku, quantity)
            else:
                await self._write_ozon(sku, quantity)
            self.db.clear_channel_suppressed(channel, sku)
            self.db.clear_available_snapshot(
                f"marketplace:{channel}", sku
            )
            return quantity

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
            self.db.set_order_available(
                sku, 0, reason=f"{scope}_zero"
            )
            total += current
            count += 1
        # Repeat commands must include earlier unfinished writes without
        # overwriting their original restoration snapshots.
        snapshot = self.db.get_available_snapshot(scope)
        for sku in snapshot:
            if self.available_quantity(sku) != 0:
                self.db.set_order_available(sku, 0, reason=f"{scope}_zero_retry")
        targets = set(skus) | set(snapshot) | {
            sku for sku in self.all_skus() if self.available_quantity(sku) == 0
        }
        errors = []
        for sku in sorted(targets):
            try:
                await self.sync_sku(sku, raise_errors=True)
            except Exception as exc:
                errors.append(f"{sku}: {exc}")
        if errors:
            raise RuntimeError(
                "Доступный остаток сохранён в 0. Не завершена синхронизация: "
                + " | ".join(errors)
                + ". Фоновая проверка повторит запись автоматически."
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
                int(wanted),
                self.local_quantity(sku),
            )
            self.db.set_order_available(
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
            skus = [
                sku
                for sku in sorted(self.all_skus())
                if self.available_quantity(sku) > 0
            ]
            for sku in skus:
                if not self.db.is_channel_suppressed("wb", sku):
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
            skus = [
                sku
                for sku in sorted(self.all_skus())
                if self.available_quantity(sku) > 0
            ]
            for sku in skus:
                if not self.db.is_channel_suppressed("ozon", sku):
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
