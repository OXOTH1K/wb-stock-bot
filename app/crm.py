from __future__ import annotations

import base64
import ipaddress
import logging
import secrets
from dataclasses import asdict
from typing import Any

from aiohttp import web

from .config import Settings
from .crm_ui import INDEX_HTML
from .db import StateDB
from .orders import OrderMonitor
from .service import StockMonitorService

log = logging.getLogger(__name__)


class CRMServer:
    def __init__(
        self,
        settings: Settings,
        service: StockMonitorService,
        orders: OrderMonitor,
        db: StateDB,
    ):
        self.settings = settings
        self.service = service
        self.orders = orders
        self.db = db
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
                web.get("/api/orders/wb", self.wb_orders),
                web.post("/api/orders/wb/assembled", self.set_wb_order_assembled),
                web.get("/api/orders/ozon", self.ozon_orders),
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
        products = sorted(
            self.service.products.values(),
            key=lambda p: (
                (p.vendor_code or "").lower(),
                (p.title or "").lower(),
                p.nm_id,
            ),
        )
        skus = [self._sku(p.nm_id, p.vendor_code) for p in products]

        for product, sku in zip(products, skus):
            self.db.ensure_local_stock(
                sku,
                self._bootstrap_local_quantity(product.nm_id),
            )

        local = self.db.get_local_stock(tuple(skus))
        ozon = self.db.get_channel_stock("ozon_fbs", tuple(skus))

        items: list[dict[str, Any]] = []
        for product, sku in zip(products, skus):
            local_qty = int(local.get(sku, 0))
            wb_fbs = int(self.service.fbs_stock.get(product.nm_id, 0))
            wb_warehouses = int(self.service.wb_stock.get(product.nm_id, 0))
            saved = self.db.get_saved_product_fbs("wb_auto", product.nm_id)
            items.append(
                {
                    "key": str(product.nm_id),
                    "nm_id": int(product.nm_id),
                    "sku": sku,
                    "title": product.title or "",
                    "local": local_qty,
                    "wb_fbs": wb_fbs,
                    "wb_warehouses": wb_warehouses,
                    "ozon_fbs": ozon.get(sku),
                    "fbs_suppressed": bool(saved) and wb_fbs == 0,
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
                    "wb_fbs": sum(item["wb_fbs"] for item in items),
                    "wb_warehouses": sum(
                        item["wb_warehouses"] for item in items
                    ),
                    "drift": sum(
                        1
                        for item in items
                        if item["local"] != item["wb_fbs"]
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
        return {
            self._sku(product.nm_id, product.vendor_code)
            for product in self.service.products.values()
        }

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

    async def wb_orders(self, request: web.Request) -> web.Response:
        rows = self.db.list_order_state(limit=300)
        by_id = {int(row["order_id"]): row for row in rows}

        active_new = set(self.orders.current_new_orders)
        active_supply = dict(self.orders.current_supply_orders)
        active_ids = active_new | set(active_supply)

        items = []
        for order_id in active_ids:
            row = by_id.get(order_id)
            live = self.orders.current_new_orders.get(order_id)
            if row is None:
                row = {
                    "order_id": order_id,
                    "article": live.article if live is not None else "",
                    "nm_id": live.nm_id if live is not None else 0,
                    "status": "notified" if live is not None else "assigned",
                    "supply_id": active_supply.get(order_id),
                    "first_seen_at": (
                        live.created_at if live is not None else ""
                    ),
                    "updated_at": (
                        live.created_at if live is not None else ""
                    ),
                    "assembled": False,
                    "supplier_status": (
                        "new" if order_id in active_new else "confirm"
                    ),
                    "wb_status": "waiting",
                }
            else:
                row = dict(row)
                if live is not None and not row.get("article"):
                    row["article"] = live.article
                row["supplier_status"] = (
                    "new" if order_id in active_new else "confirm"
                )
                row["is_new"] = order_id in active_new
                if order_id in active_supply:
                    row["supply_id"] = active_supply[order_id]
                    row["status"] = "assigned"
            row["is_new"] = order_id in active_new
            items.append(row)

        items.sort(
            key=lambda row: (
                0 if row["is_new"] else 1,
                str(row.get("first_seen_at") or ""),
                int(row["order_id"]),
            )
        )
        return web.json_response({"items": items})

    async def set_wb_order_assembled(
        self, request: web.Request
    ) -> web.Response:
        data = await self._payload(request)
        try:
            order_id = int(data.get("order_id"))
        except (TypeError, ValueError) as exc:
            raise web.HTTPBadRequest(
                text='{"error":"order_id must be an integer"}',
                content_type="application/json",
            ) from exc
        assembled = bool(data.get("assembled"))
        known_ids = {
            int(row["order_id"])
            for row in self.db.list_order_state(limit=1000)
        } | set(self.orders.current_new_orders)
        if order_id not in known_ids:
            raise web.HTTPNotFound(
                text='{"error":"unknown order"}',
                content_type="application/json",
            )
        self.db.set_order_assembled(order_id, assembled)
        return web.json_response(
            {"order_id": order_id, "assembled": assembled}
        )

    async def ozon_orders(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "connected": False,
                "items": [],
                "message": "OZON API integration is not configured yet",
            }
        )
