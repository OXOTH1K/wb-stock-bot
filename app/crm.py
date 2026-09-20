from __future__ import annotations

import base64
import ipaddress
import logging
import secrets
from typing import Any

from aiohttp import web

from .config import Settings
from .crm_ui import INDEX_HTML
from .db import StateDB
from .inventory_sync import SharedStockSync
from .service import StockMonitorService

log = logging.getLogger(__name__)


class CRMServer:
    def __init__(
        self,
        settings: Settings,
        service: StockMonitorService,
        db: StateDB,
        stock_sync: SharedStockSync | None = None,
    ):
        self.settings = settings
        self.service = service
        self.db = db
        self.stock_sync = stock_sync
        try:
            self._allowed_networks = tuple(
                ipaddress.ip_network(value, strict=False)
                for value in self.settings.crm_allowed_networks
            )
        except ValueError as exc:
            raise RuntimeError(f"Invalid CRM_ALLOWED_NETWORKS: {exc}") from exc
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None

        @web.middleware
        async def auth_middleware(
            request: web.Request, handler: web.RequestHandler
        ) -> web.StreamResponse:
            if self._allowed_networks and not self._client_ip_allowed(request.remote):
                raise web.HTTPForbidden(text="CRM access is not allowed from this network")
            if not (self.settings.crm_user and self.settings.crm_password):
                return await handler(request)
            auth = request.headers.get("Authorization", "")
            if auth.startswith("Basic "):
                try:
                    raw = base64.b64decode(auth[6:], validate=True).decode("utf-8")
                    username, password = raw.split(":", 1)
                except Exception:
                    username, password = "", ""
                if (
                    secrets.compare_digest(username, self.settings.crm_user)
                    and secrets.compare_digest(password, self.settings.crm_password)
                ):
                    return await handler(request)
            raise web.HTTPUnauthorized(
                headers={"WWW-Authenticate": 'Basic realm="WB CRM"'}
            )

        self.app = web.Application(middlewares=[auth_middleware])
        self.app.add_routes(
            [
                web.get("/", self.index),
                web.get("/healthz", self.health),
                web.get("/api/inventory", self.inventory),
                web.post("/api/inventory/adjust", self.adjust_inventory),
                web.post("/api/inventory/set", self.set_inventory),
                web.get("/api/inventory/movements", self.inventory_movements),
            ]
        )

    def _client_ip_allowed(self, value: str | None) -> bool:
        if not self._allowed_networks:
            return True
        if not value:
            return False
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            return False
        return any(address in network for network in self._allowed_networks)

    async def start(self) -> None:
        if not self.settings.crm_enabled:
            log.info("CRM web server disabled")
            return
        self.runner = web.AppRunner(self.app, access_log=log)
        await self.runner.setup()
        self.site = web.TCPSite(
            self.runner,
            self.settings.crm_host,
            self.settings.crm_port,
        )
        await self.site.start()
        log.info(
            "CRM web server listening on http://%s:%d",
            self.settings.crm_host,
            self.settings.crm_port,
        )

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None
            self.site = None

    async def index(self, request: web.Request) -> web.Response:
        return web.Response(
            text=INDEX_HTML,
            content_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "products": len(self.service.products),
                "warehouse": (
                    None
                    if self.service.warehouse is None
                    else self.service.warehouse.name
                ),
            }
        )

    @staticmethod
    def _sku(nm_id: int, vendor_code: str) -> str:
        value = str(vendor_code or "").strip()
        return value or f"WB-{int(nm_id)}"

    def _bootstrap_local_quantity(self, nm_id: int) -> int:
        saved = self.db.get_saved_product_fbs("wb_auto", nm_id)
        if saved:
            return sum(int(qty) for qty in saved.values())
        return int(self.service.fbs_stock.get(nm_id, 0))

    def _inventory_items(self) -> list[dict[str, Any]]:
        wb_by_sku = {
            self._sku(product.nm_id, product.vendor_code): product
            for product in self.service.products.values()
        }
        ozon_catalog = self.db.get_channel_catalog("ozon")
        all_skus = sorted(set(wb_by_sku) | set(ozon_catalog))
        ozon_stock = self.db.get_channel_stock(
            "ozon_fbs", tuple(all_skus)
        )

        for sku in all_skus:
            wb_product = wb_by_sku.get(sku)
            if wb_product is not None:
                initial = self._bootstrap_local_quantity(
                    wb_product.nm_id
                )
            else:
                initial = int(ozon_stock.get(sku, 0))
            self.db.ensure_local_stock(sku, initial)

        local = self.db.get_local_stock(tuple(all_skus))

        items: list[dict[str, Any]] = []
        for index, sku in enumerate(all_skus):
            wb_product = wb_by_sku.get(sku)
            ozon_product = ozon_catalog.get(sku)
            local_qty = int(local.get(sku, 0))

            wb_fbs: int | None = None
            wb_warehouses: int | None = None
            fbs_suppressed = self.db.is_channel_suppressed(
                "wb", sku
            )
            if wb_product is not None:
                wb_fbs = int(
                    self.service.fbs_stock.get(wb_product.nm_id, 0)
                )
                wb_warehouses = int(
                    self.service.wb_stock.get(wb_product.nm_id, 0)
                )

            ozon_suppressed = self.db.is_channel_suppressed(
                "ozon", sku
            )
            ozon_fbs: int | None = None
            if ozon_product is not None:
                ozon_fbs = int(ozon_stock.get(sku, 0))

            drift_channels: list[str] = []
            if (
                wb_fbs is not None
                and not fbs_suppressed
                and wb_fbs != local_qty
            ):
                drift_channels.append("WB")
            if (
                ozon_fbs is not None
                and not ozon_suppressed
                and ozon_fbs != local_qty
            ):
                drift_channels.append("OZON")

            title = ""
            if wb_product is not None:
                title = str(wb_product.title or "")
            if not title and ozon_product is not None:
                title = str(ozon_product.get("title") or "")

            items.append(
                {
                    "key": f"p{index}",
                    "nm_id": (
                        None
                        if wb_product is None
                        else int(wb_product.nm_id)
                    ),
                    "sku": sku,
                    "title": title or sku,
                    "local": local_qty,
                    "wb_fbs": wb_fbs,
                    "wb_warehouses": wb_warehouses,
                    "ozon_fbs": ozon_fbs,
                    "wb_exists": wb_product is not None,
                    "ozon_exists": ozon_product is not None,
                    "fbs_suppressed": fbs_suppressed,
                    "ozon_suppressed": ozon_suppressed,
                    "drift_channels": drift_channels,
                }
            )
        return items

    async def inventory(self, request: web.Request) -> web.Response:
        items = self._inventory_items()
        return web.json_response(
            {
                "items": items,
                "totals": {
                    "local": sum(item["local"] for item in items),
                    "wb_fbs": sum(
                        item["wb_fbs"] or 0 for item in items
                    ),
                    "wb_warehouses": sum(
                        item["wb_warehouses"] or 0 for item in items
                    ),
                    "ozon_fbs": sum(
                        item["ozon_fbs"] or 0 for item in items
                    ),
                    "drift": sum(
                        1
                        for item in items
                        if item["drift_channels"]
                    ),
                },
            }
        )

    async def _payload(self, request: web.Request) -> dict[str, Any]:
        try:
            data = await request.json()
        except Exception as exc:
            raise web.HTTPBadRequest(
                text='{"error":"invalid JSON"}',
                content_type="application/json",
            ) from exc
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(
                text='{"error":"JSON object required"}',
                content_type="application/json",
            )
        return data

    def _known_skus(self) -> set[str]:
        wb = {
            self._sku(product.nm_id, product.vendor_code)
            for product in self.service.products.values()
        }
        return wb | set(self.db.get_channel_catalog("ozon"))

    async def adjust_inventory(self, request: web.Request) -> web.Response:
        data = await self._payload(request)
        sku = str(data.get("sku") or "").strip()
        try:
            delta = int(data.get("delta"))
        except (TypeError, ValueError) as exc:
            raise web.HTTPBadRequest(
                text='{"error":"delta must be an integer"}',
                content_type="application/json",
            ) from exc
        if sku not in self._known_skus():
            raise web.HTTPNotFound(
                text='{"error":"unknown SKU"}',
                content_type="application/json",
            )
        try:
            if self.stock_sync is not None:
                current = self.stock_sync.ensure_local(sku)
                quantity = current + delta
                if quantity < 0:
                    raise ValueError(
                        "Local stock cannot be negative"
                    )
                quantity = await self.stock_sync.set_local_stock(
                    sku, quantity, reason="crm_adjust"
                )
            else:
                quantity = self.db.adjust_local_stock(
                    sku, delta, reason="crm_adjust"
                )
        except ValueError as exc:
            raise web.HTTPBadRequest(
                text=web.json_response({"error": str(exc)}).text,
                content_type="application/json",
            ) from exc
        return web.json_response({"sku": sku, "quantity": quantity})

    async def set_inventory(self, request: web.Request) -> web.Response:
        data = await self._payload(request)
        sku = str(data.get("sku") or "").strip()
        try:
            quantity = int(data.get("quantity"))
        except (TypeError, ValueError) as exc:
            raise web.HTTPBadRequest(
                text='{"error":"quantity must be an integer"}',
                content_type="application/json",
            ) from exc
        if sku not in self._known_skus():
            raise web.HTTPNotFound(
                text='{"error":"unknown SKU"}',
                content_type="application/json",
            )
        try:
            if self.stock_sync is not None:
                quantity = await self.stock_sync.set_local_stock(
                    sku, quantity, reason="crm_set"
                )
            else:
                quantity = self.db.set_local_stock(
                    sku, quantity, reason="crm_set"
                )
        except ValueError as exc:
            raise web.HTTPBadRequest(
                text=web.json_response({"error": str(exc)}).text,
                content_type="application/json",
            ) from exc
        return web.json_response({"sku": sku, "quantity": quantity})

    async def inventory_movements(self, request: web.Request) -> web.Response:
        try:
            limit = int(request.query.get("limit", "50"))
        except ValueError:
            limit = 50
        return web.json_response(
            {"items": self.db.list_inventory_movements(limit)}
        )

