import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from aiohttp.test_utils import TestClient, TestServer

from app.crm import CRMServer
from app.crm_ui import INDEX_HTML
from app.db import StateDB
from app.models import Product
from app.wb_order_lookup import WBOrderLookup


class FakeService:
    def __init__(self):
        self.products = {
            100: Product(100, "SKU-A", "Alpha", (1,)),
            200: Product(200, "SKU-B", "Beta", (2,)),
        }
        self.fbs_stock = {100: 5, 200: 0}
        self.wb_stock = {100: 2, 200: 7}
        self.warehouse = SimpleNamespace(name="Main FBS")


class FakeReleaseChecker:
    def __init__(self):
        self.forced = []

    async def check(self, force=False):
        self.forced.append(force)
        return {"available": False, "message": "Проверка завершена."}


class CRMTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = StateDB(Path(self.tmp.name) / "state.sqlite3")
        self.service = FakeService()
        self.settings = SimpleNamespace(
            crm_enabled=True,
            crm_host="127.0.0.1",
            crm_port=8080,
            crm_user="admin",
            crm_password="test-password",
            crm_allowed_networks=("127.0.0.1/32",),
        )
        self.crm = CRMServer(self.settings, self.service, self.db)
        self.client = TestClient(TestServer(self.crm.app), headers={
            'Authorization': 'Basic ' + base64.b64encode(b'admin:test-password').decode(),
            'X-CSRF-Token': self.crm.security.csrf_token,
        })
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.db.close()
        self.tmp.cleanup()

    async def test_inventory_bootstraps_local_stock_from_fbs_or_saved_snapshot(self):
        self.db.save_product_fbs("wb_auto", 200, {2: 4})

        response = await self.client.get("/api/inventory")
        self.assertEqual(response.status, 200)
        data = await response.json()
        by_sku = {row["sku"]: row for row in data["items"]}

        self.assertEqual(by_sku["SKU-A"]["local"], 5)
        self.assertEqual(by_sku["SKU-A"]["available"], 5)
        self.assertEqual(by_sku["SKU-A"]["wb_fbs"], 5)
        self.assertEqual(by_sku["SKU-A"]["wb_warehouses"], 2)
        self.assertEqual(by_sku["SKU-B"]["local"], 4)
        self.assertEqual(by_sku["SKU-B"]["available"], 0)
        self.assertTrue(by_sku["SKU-B"]["fbs_suppressed"])
        self.assertEqual(by_sku["SKU-B"]["drift_channels"], [])
        self.assertIsNone(by_sku["SKU-A"]["ozon_fbs"])
        self.assertIsNone(by_sku["SKU-A"]["ozon_fbo"])

    async def test_update_check_can_bypass_release_cache_on_user_request(self):
        checker = FakeReleaseChecker()
        self.crm.release_checker = checker

        response = await self.client.get("/api/update?force=1")

        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(checker.forced, [True])

    async def test_version_endpoint_reports_running_release_without_cache(self):
        response = await self.client.get("/api/version")

        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(await response.json(), {"version": "1.0.7"})

    async def test_unit_economics_endpoint_and_cost_save(self):
        class FakeAnalytics:
            def unit_economics_view(self, start, end, sku):
                return {"date_from": start, "date_to": end, "sku": sku,
                        "products": [], "ready": False, "unit_cost": None}
        self.crm.sales_analytics = FakeAnalytics()
        response = await self.client.get(
            "/api/wb/unit-economics?date_from=2026-09-20&date_to=2026-09-21&sku=SKU"
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual((await response.json())["sku"], "SKU")
        response = await self.client.post(
            "/api/wb/unit-cost", json={"sku": "SKU", "cost": "12.5"}
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(self.db.get_wb_unit_cost("SKU"), "12.50")
        for value in ("-1", "NaN", "1e4", "1.001", ""):
            response = await self.client.post(
                "/api/wb/unit-cost", json={"sku": "SKU", "cost": value}
            )
            self.assertEqual(response.status, 400)

    async def test_inventory_unions_wb_and_ozon_by_seller_sku(self):
        self.db.replace_channel_catalog(
            "ozon",
            [
                ("SKU-A", "Alpha Ozon", "1001"),
                ("OZON-ONLY", "Ozon Only", "1002"),
            ],
        )
        self.db.replace_channel_stock(
            "ozon_fbs",
            {"SKU-A": 5, "OZON-ONLY": 4},
        )
        self.db.replace_channel_stock(
            "ozon_fbo",
            {"SKU-A": 8, "OZON-ONLY": 2},
        )

        response = await self.client.get("/api/inventory")
        self.assertEqual(response.status, 200)
        data = await response.json()
        by_sku = {row["sku"]: row for row in data["items"]}

        shared = by_sku["SKU-A"]
        self.assertTrue(shared["wb_exists"])
        self.assertTrue(shared["ozon_exists"])
        self.assertEqual(shared["wb_fbs"], 5)
        self.assertEqual(shared["ozon_fbs"], 5)
        self.assertEqual(shared["ozon_fbo"], 8)
        self.assertEqual(shared["local"], 5)
        self.assertEqual(shared["available"], 5)
        self.assertEqual(shared["drift_channels"], [])

        wb_only = by_sku["SKU-B"]
        self.assertTrue(wb_only["wb_exists"])
        self.assertFalse(wb_only["ozon_exists"])
        self.assertIsNone(wb_only["ozon_fbs"])
        self.assertIsNone(wb_only["ozon_fbo"])

        ozon_only = by_sku["OZON-ONLY"]
        self.assertFalse(ozon_only["wb_exists"])
        self.assertTrue(ozon_only["ozon_exists"])
        self.assertIsNone(ozon_only["wb_fbs"])
        self.assertIsNone(ozon_only["wb_warehouses"])
        self.assertEqual(ozon_only["ozon_fbs"], 4)
        self.assertEqual(ozon_only["ozon_fbo"], 2)
        self.assertEqual(ozon_only["local"], 4)
        self.assertEqual(ozon_only["available"], 4)
        self.assertEqual(ozon_only["title"], "Ozon Only")

        self.assertEqual(data["totals"]["available"], 9)
        self.assertEqual(data["totals"]["ozon_fbs"], 9)
        self.assertEqual(data["totals"]["ozon_fbo"], 10)

    async def test_local_and_available_are_edited_independently(self):
        await self.client.get("/api/inventory")

        response = await self.client.post(
            "/api/inventory/set",
            json={"sku": "SKU-A", "quantity": 10},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["quantity"], 10)
        self.assertEqual(
            self.db.get_order_available(("SKU-A",))["SKU-A"],
            5,
        )

        response = await self.client.post(
            "/api/inventory/available/set",
            json={"sku": "SKU-A", "quantity": 8},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["quantity"], 8)
        self.assertEqual(
            self.db.get_local_stock(("SKU-A",))["SKU-A"],
            10,
        )

        response = await self.client.post(
            "/api/inventory/available/set",
            json={"sku": "SKU-A", "quantity": 2},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(
            self.db.get_local_stock(("SKU-A",))["SKU-A"],
            10,
        )
        self.assertEqual(
            self.db.get_order_available(("SKU-A",))["SKU-A"],
            2,
        )

        response = await self.client.post(
            "/api/inventory/available/set",
            json={"sku": "SKU-A", "quantity": 11},
        )
        self.assertEqual(response.status, 400)

    async def test_available_sync_failure_is_returned_to_crm(self):
        await self.client.get("/api/inventory")
        self.db.set_local_stock("SKU-A", 7, reason="restock")

        class FailingInventory:
            async def set_available_stock(
                inner_self, sku, quantity, reason="crm", **kwargs
            ):
                self.db.set_order_available(
                    sku, quantity, reason=reason
                )
                raise RuntimeError("OZON: write rejected")

        self.crm.shared_inventory = FailingInventory()

        response = await self.client.post(
            "/api/inventory/available/set",
            json={"sku": "SKU-A", "quantity": 6},
        )
        self.assertEqual(response.status, 502)
        payload = await response.json()
        self.assertIn("синхронизация", payload["error"])
        self.assertIn("OZON: write rejected", payload["error"])
        self.assertEqual(
            self.db.get_order_available(("SKU-A",))["SKU-A"],
            6,
        )
        self.assertEqual(
            self.db.get_local_stock(("SKU-A",))["SKU-A"],
            7,
        )

    async def test_order_routes_are_removed_from_crm(self):
        self.assertEqual((await self.client.get("/api/orders/wb")).status, 404)
        self.assertEqual((await self.client.get("/api/orders/ozon")).status, 404)

    async def test_lookup_validates_input_and_disables_http_cache(self):
        lookup = Mock()
        lookup.search.return_value = {"items": [], "partial": True}
        self.crm.order_lookup = lookup
        for number in ("", "x" * 201, "abc\n123"):
            response = await self.client.get("/api/wb/order-lookup", params={"number": number})
            self.assertEqual(response.status, 400)
        lookup.search.assert_not_called()
        response = await self.client.get("/api/wb/order-lookup", params={"number": " abc.123 "})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        lookup.search.assert_called_once_with("abc.123")

    async def test_fbw_orders_period_endpoint(self):
        self.crm.order_lookup = WBOrderLookup(Mock(), self.db)
        self.db.archive_wb_orders("orders", [{"srid": "fbw-123", "nmId": 100,
            "supplierArticle": "SKU-A", "date": "2026-09-20T12:00:00", "warehouseType": "Склад WB"}])
        response = await self.client.get("/api/wb/fbw-orders", params={"date_from": "2026-09-20", "date_to": "2026-09-20"})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        data = await response.json()
        self.assertEqual(data["order_count"], 1)
        self.assertEqual(data["groups"][0]["title"], "Alpha")
        for params in ({}, {"date_from": "2026-09-21", "date_to": "2026-09-20"}):
            response = await self.client.get("/api/wb/fbw-orders", params=params)
            self.assertEqual(response.status, 400)
            self.assertIn("период", (await response.json())["error"])
        self.crm.order_lookup = None
        self.assertEqual((await self.client.get("/api/wb/fbw-orders")).status, 503)

    def test_inventory_and_lookup_ui_layout(self):
        self.assertIn('class="app-footer"', INDEX_HTML)
        self.assertIn('id="appVersion"', INDEX_HTML)
        self.assertIn("loadAppVersion();", INDEX_HTML)
        self.assertIn('id="unitView"', INDEX_HTML)
        self.assertIn('id="unitSku"', INDEX_HTML)
        self.assertIn('id="unitCost"', INDEX_HTML)
        self.assertIn("/api/wb/unit-economics?", INDEX_HTML)
        self.assertNotIn("WB заказы", INDEX_HTML)
        self.assertNotIn("OZON заказы", INDEX_HTML)
        self.assertIn('class="tabs"', INDEX_HTML)
        self.assertIn('id="lookupView"', INDEX_HTML)
        self.assertIn('id="lookupForm"', INDEX_HTML)
        self.assertIn('Путь заказа', INDEX_HTML)
        self.assertNotIn("adjustStock(", INDEX_HTML)
        self.assertNotIn(">−1<", INDEX_HTML)
        self.assertNotIn(">+1<", INDEX_HTML)
        self.assertIn("position:sticky;top:0;z-index:5", INDEX_HTML)
        self.assertIn(".table-wrap{overflow:visible}", INDEX_HTML)
        self.assertIn(".panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow);overflow:visible}", INDEX_HTML)
        self.assertNotIn("max-height:calc(100vh", INDEX_HTML)
        inventory_renderer = INDEX_HTML.split("function renderInventory()", 1)[1].split("async function setStock", 1)[0]
        title_pos = inventory_renderer.index('<div class="title">')
        sku_pos = inventory_renderer.index('<div class="sku">')
        self.assertLess(title_pos, sku_pos)
        self.assertIn('title="Сохранить «Мой склад»"', INDEX_HTML)
        self.assertIn('title="Сохранить «Доступно для заказа»"', INDEX_HTML)
        self.assertIn("Доступно для заказа", INDEX_HTML)
        self.assertIn("FBO", INDEX_HTML)
        self.assertIn("WB FBS намеренно 0", INDEX_HTML)
        self.assertIn("Ozon FBS намеренно 0", INDEX_HTML)
        self.assertIn('id="fbwView"', INDEX_HTML)
        self.assertIn('id="fbwDateFrom"', INDEX_HTML)
        self.assertIn('id="fbwDateTo"', INDEX_HTML)

    async def test_network_allowlist_accepts_lan_and_rejects_other_networks(self):
        self.crm._allowed_networks = (
            __import__("ipaddress").ip_network("127.0.0.1/32"),
            __import__("ipaddress").ip_network("192.168.1.0/24"),
        )
        self.assertTrue(self.crm._client_ip_allowed("192.168.1.25"))
        self.assertTrue(self.crm._client_ip_allowed("127.0.0.1"))
        self.assertFalse(self.crm._client_ip_allowed("192.168.2.25"))
        self.assertFalse(self.crm._client_ip_allowed("10.0.0.5"))
        self.crm._allowed_networks = (__import__("ipaddress").ip_network("192.168.1.0/24"),)
        response = await self.client.get("/api/wb/order-lookup?number=123")
        self.assertEqual(response.status, 403)
        self.assertEqual((await self.client.get("/api/wb/fbw-orders")).status, 403)


class CRMAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = StateDB(Path(self.tmp.name) / "state.sqlite3")
        self.settings = SimpleNamespace(
            crm_enabled=True,
            crm_host="127.0.0.1",
            crm_port=8080,
            crm_user="admin",
            crm_password="secret",
            crm_allowed_networks=("127.0.0.1/32",),
        )
        self.crm = CRMServer(self.settings, FakeService(), self.db)
        self.client = TestClient(TestServer(self.crm.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.db.close()
        self.tmp.cleanup()

    async def test_basic_auth_protects_crm(self):
        self.assertEqual((await self.client.get("/api/wb/fbw-orders")).status, 401)
        response = await self.client.get("/api/wb/order-lookup?number=123")
        self.assertEqual(response.status, 401)
        response = await self.client.get("/healthz")
        self.assertEqual(response.status, 401)

        token = base64.b64encode(b"admin:secret").decode()
        response = await self.client.get(
            "/healthz",
            headers={"Authorization": f"Basic {token}"},
        )
        self.assertEqual(response.status, 200)


if __name__ == "__main__":
    unittest.main()
