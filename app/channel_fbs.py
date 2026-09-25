from __future__ import annotations

import json


class ChannelFBSControl:
    """Persist independent FBS pause snapshots and restoration targets."""

    def __init__(self, inventory):
        self.inventory = inventory
        self.db = inventory.db

    def state(self, channel):
        return json.loads(self.db.get_meta(f"channel_fbs:{channel}") or "{}")

    def save(self, channel, state):
        self.db.set_meta(f"channel_fbs:{channel}", json.dumps(state))

    def migrate_legacy(self):
        """Keep both channels paused when upgrading an old shared mass-zero."""
        legacy = self.db.get_available_snapshot("mass_shared")
        if not legacy:
            return
        products = self.inventory._wb_by_sku()
        catalog = self.db.get_channel_catalog("ozon")
        for channel in ("wb", "ozon"):
            state = self.state(channel)
            if state:
                continue
            items = {}
            for sku, quantity in legacy.items():
                if (channel == "wb" and sku not in products) or (channel == "ozon" and sku not in catalog):
                    continue
                saved = 0 if self.db.get_channel_suppression_reason(channel, sku) == "marketplace_stock" else quantity
                row = {"saved": saved, "remaining": saved}
                if channel == "wb":
                    product = products[sku]
                    if len(product.chrt_ids) == 1:
                        row["variants"] = {str(product.chrt_ids[0]): saved}
                    else:
                        variants = self.db.get_saved_product_fbs("mass", product.nm_id)
                        if variants:
                            row["variants"] = {str(k): v for k, v in variants.items()}
                items[sku] = row
            self.save(channel, {"phase": "zero", "items": items})
        for sku, quantity in legacy.items():
            self.db.set_order_available(
                sku, min(quantity, self.inventory.local_quantity(sku)),
                reason="migrate_independent_fbs_pauses",
            )
        for channel in ("wb", "ozon"):
            self.db.clear_channel_suppressions_by_reason(channel, "mass")
        self.db.clear_available_snapshot("mass_shared")

    def pending_snapshot(self, channel):
        state = self.state(channel)
        if state.get("phase") not in {"zero", "restoring"}:
            return {}
        return {sku: row["remaining"] for sku, row in state["items"].items()}

    def target(self, channel, sku, default):
        state = self.state(channel)
        row = state.get("items", {}).get(sku)
        if row is None:
            return default
        if state["phase"] == "zero":
            return 0
        return min(row["remaining"], self.inventory.local_quantity(sku))

    def paused(self, channel, sku):
        state = self.state(channel)
        return state.get("phase") == "zero" and sku in state.get("items", {})

    def consume(self, sku, quantity):
        for channel in ("wb", "ozon"):
            state = self.state(channel)
            row = state.get("items", {}).get(sku)
            if row is not None:
                row["remaining"] = max(0, row["remaining"] - quantity)
                self.save(channel, state)

    def explicit_set(self, sku):
        # An explicit shared /set supersedes restored limits, but not a pause.
        for channel in ("wb", "ozon"):
            state = self.state(channel)
            if state.get("phase") in {"restored", "restoring"}:
                state["items"].pop(sku, None)
                self.save(channel, state)

    def wb_variants(self, sku, quantity):
        row = self.state("wb").get("items", {}).get(sku, {})
        variants = row.get("variants")
        if variants is None:
            return None
        result = {}
        remaining = quantity
        for key, saved in variants.items():
            value = min(int(saved), remaining)
            result[int(key)] = value
            remaining -= value
        if remaining:
            return None
        return result

    async def zero(self, channel):
        if channel not in {"wb", "ozon"}:
            raise ValueError("Unknown marketplace")
        state = self.state(channel)
        if state.get("phase") != "zero":
            items = {}
            if channel == "wb":
                service = self.inventory.wb_service
                products = self.inventory._wb_by_sku()
                quantities = await service.wb.get_fbs_stocks(
                    service.warehouse.id,
                    [chrt for p in products.values() for chrt in p.chrt_ids],
                )
                for sku, product in products.items():
                    variants = {str(chrt): int(quantities.get(chrt, 0)) for chrt in product.chrt_ids}
                    total = sum(variants.values())
                    items[sku] = {"saved": total, "remaining": total, "variants": variants}
                    service.fbs_stock[product.nm_id] = total
                    self.db.update_many("fbs", {product.nm_id: total})
            else:
                if self.inventory.ozon is None:
                    raise RuntimeError("Ozon не подключён")
                await self.inventory.ozon.refresh_catalog_and_stocks(notify=False)
                catalog = self.db.get_channel_catalog("ozon")
                quantities = self.db.get_channel_stock("ozon_fbs", tuple(catalog))
                for sku in catalog:
                    quantity = int(quantities.get(sku, 0))
                    items[sku] = {"saved": quantity, "remaining": quantity}
            state = {"phase": "zero", "items": items}
            # Persist the entire snapshot before the first marketplace write.
            self.save(channel, state)
        await self._sync(channel, state)
        return len(state["items"]), sum(row["saved"] for row in state["items"].values())

    async def restore(self, channel):
        state = self.state(channel)
        if state.get("phase") not in {"zero", "restoring"}:
            return 0, 0
        state["phase"] = "restoring"
        self.save(channel, state)
        await self._sync(channel, state)
        state["phase"] = "restored"
        self.save(channel, state)
        return len(state["items"]), sum(
            self.target(channel, sku, 0) for sku in state["items"]
        )

    async def _sync(self, channel, state):
        errors = []
        for sku in state["items"]:
            try:
                await self.inventory.sync_sku(
                    sku, skip_channel="ozon" if channel == "wb" else "wb",
                    force=False,
                )
            except Exception as exc:
                errors.append(f"{sku}: {exc}")
        if errors:
            raise RuntimeError(
                "Снимок сохранён. Не завершена запись: " + " | ".join(errors)
                + ". Фоновая синхронизация повторит попытку."
            )
