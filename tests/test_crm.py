import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

from app.crm import CRMServer
from app.crm_ui import INDEX_HTML
from app.db import StateDB
from app.models import Product


class FakeService:
    def __init__(self):
        self.products = {
            100: Product(100, "SKU-A", "Alpha", (1,)),
            200: Product(200, "SKU-B", "Beta", (2,)),
        }
        self.fbs_stock = {100: 5, 200: 0}
        self.wb_stock = {100: 2, 200: 7}
        self.warehouse = SimpleNamespace(name="Main FBS")


class CRMTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = StateDB(Path(self.tmp.name) / "state.sqlite3")
        self.service = FakeService()
        self.settings = SimpleNamespace(
            crm_enabled=True,
            crm_host="127.0.0.1",
            crm_port=8080,
            crm_user="",
            crm_password="",
            crm_allowed_networks=("127.0.0.1/32",),
        )
        self.crm = CRMServer(self.settings, self.service, self.db)
        self.client = TestClient(TestServer(self.crm.app))
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
        self.assertEqual(by_sku["SKU-A"]["wb_fbs"], 5)
        self.assertEqual(by_sku["SKU-A"]["wb_warehouses"], 2)
        self.assertEqual(by_sku["SKU-B"]["local"], 4)
        self.assertTrue(by_sku["SKU-B"]["fbs_suppressed"])
        self.assertIsNone(by_sku["SKU-A"]["ozon_fbs"])

    async def test_inventory_can_be_adjusted_and_is_audited(self):
        await self.client.get("/api/inventory")

        response = await self.client.post(
            "/api/inventory/adjust",
            json={"sku": "SKU-A", "delta": 2},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["quantity"], 7)

        response = await self.client.post(
            "/api/inventory/set",
            json={"sku": "SKU-A", "quantity": 3},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["quantity"], 3)

        movements = self.db.list_inventory_movements(10)
        self.assertEqual(movements[0]["sku"], "SKU-A")
        self.assertEqual(movements[0]["before"], 7)
        self.assertEqual(movements[0]["after"], 3)
        self.assertEqual(movements[0]["reason"], "crm_set")

        response = await self.client.post(
            "/api/inventory/adjust",
            json={"sku": "SKU-A", "delta": -99},
        )
        self.assertEqual(response.status, 400)

    async def test_order_routes_are_removed_from_crm(self):
        self.assertEqual((await self.client.get("/api/orders/wb")).status, 404)
        self.assertEqual((await self.client.get("/api/orders/ozon")).status, 404)

    def test_inventory_only_ui_layout(self):
        self.assertNotIn("WB заказы", INDEX_HTML)
        self.assertNotIn("OZON заказы", INDEX_HTML)
        self.assertNotIn('class="tabs"', INDEX_HTML)
        self.assertNotIn("adjustStock(", INDEX_HTML)
        self.assertNotIn(">−1<", INDEX_HTML)
        self.assertNotIn(">+1<", INDEX_HTML)
        self.assertIn("position:sticky;top:0;z-index:5", INDEX_HTML)
        self.assertIn(".table-wrap{overflow:visible}", INDEX_HTML)
        self.assertIn(".panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow);overflow:visible}", INDEX_HTML)
        self.assertNotIn("max-height:calc(100vh", INDEX_HTML)
        title_pos = INDEX_HTML.index('<div class="title">')
        sku_pos = INDEX_HTML.index('<div class="sku">')
        self.assertLess(title_pos, sku_pos)
        self.assertIn('title="Сохранить"', INDEX_HTML)

    async def test_network_allowlist_accepts_lan_and_rejects_other_networks(self):
        self.crm._allowed_networks = (
            __import__("ipaddress").ip_network("127.0.0.1/32"),
            __import__("ipaddress").ip_network("192.168.1.0/24"),
        )
        self.assertTrue(self.crm._client_ip_allowed("192.168.1.25"))
        self.assertTrue(self.crm._client_ip_allowed("127.0.0.1"))
        self.assertFalse(self.crm._client_ip_allowed("192.168.2.25"))
        self.assertFalse(self.crm._client_ip_allowed("10.0.0.5"))


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
