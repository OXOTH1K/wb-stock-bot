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


class FakeOrders:
    def __init__(self):
        self.current_new_orders = {}
        self.current_supply_orders = {}


class CRMTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = StateDB(Path(self.tmp.name) / "state.sqlite3")
        self.db.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS order_state (
                order_id INTEGER PRIMARY KEY,
                article TEXT NOT NULL DEFAULT '',
                nm_id INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                supply_id TEXT,
                first_seen_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.db.conn.commit()
        self.service = FakeService()
        self.orders = FakeOrders()
        self.settings = SimpleNamespace(
            crm_enabled=True,
            crm_host="127.0.0.1",
            crm_port=8080,
            crm_user="",
            crm_password="",
            crm_allowed_networks=("127.0.0.1/32",),
        )
        self.crm = CRMServer(
            self.settings, self.service, self.orders, self.db
        )
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

    async def test_wb_orders_show_supply_and_local_assembled_flag(self):
        self.db.conn.execute(
            """
            INSERT INTO order_state(
                order_id, article, nm_id, status, supply_id,
                first_seen_at, updated_at
            )
            VALUES (501, 'SKU-A', 100, 'assigned', 'WB-GI-1',
                    '2026-09-20T10:00:00+00:00',
                    '2026-09-20T10:01:00+00:00')
            """
        )
        self.db.conn.commit()
        self.db.set_order_runtime_status(501, "confirm", "waiting")
        self.orders.current_supply_orders = {501: "WB-GI-1"}

        response = await self.client.get("/api/orders/wb")
        data = await response.json()
        self.assertEqual(data["items"][0]["supply_id"], "WB-GI-1")
        self.assertFalse(data["items"][0]["is_new"])
        self.assertEqual(data["items"][0]["supplier_status"], "confirm")
        self.assertFalse(data["items"][0]["assembled"])

        response = await self.client.post(
            "/api/orders/wb/assembled",
            json={"order_id": 501, "assembled": True},
        )
        self.assertEqual(response.status, 200)

        response = await self.client.get("/api/orders/wb")
        data = await response.json()
        row = next(x for x in data["items"] if x["order_id"] == 501)
        self.assertTrue(row["assembled"])


    async def test_wb_orders_hide_complete_and_canceled_orders(self):
        rows = [
            (601, "SKU-A", 100, "assigned", "WB-GI-1"),
            (602, "SKU-B", 200, "assigned", "WB-GI-2"),
            (603, "SKU-A", 100, "notified", None),
        ]
        for order_id, article, nm_id, status, supply_id in rows:
            self.db.conn.execute(
                """
                INSERT INTO order_state(
                    order_id, article, nm_id, status, supply_id,
                    first_seen_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id, article, nm_id, status, supply_id,
                    "2026-09-20T10:00:00+00:00",
                    "2026-09-20T10:01:00+00:00",
                ),
            )
        self.db.conn.commit()
        self.db.set_order_runtime_status(601, "confirm", "waiting")
        self.db.set_order_runtime_status(602, "complete", "waiting")
        self.db.set_order_runtime_status(603, "cancel", "canceled")
        self.orders.current_supply_orders = {601: "WB-GI-1"}

        response = await self.client.get("/api/orders/wb")
        data = await response.json()
        ids = {row["order_id"] for row in data["items"]}

        self.assertEqual(ids, {601})
        self.assertEqual(data["items"][0]["supplier_status"], "confirm")

    async def test_wb_membership_overrides_notified_and_stale_new_status(self):
        self.db.conn.execute(
            """
            INSERT INTO order_state(
                order_id, article, nm_id, status, supply_id,
                first_seen_at, updated_at
            )
            VALUES (5805907258, 'keycap-double-yellow-heart', 100,
                    'notified', NULL,
                    '2026-09-18T14:38:04+00:00',
                    '2026-09-18T14:38:04+00:00')
            """
        )
        self.db.conn.commit()
        self.db.set_order_runtime_status(5805907258, "new", "waiting")
        self.orders.current_supply_orders = {
            5805907258: "WB-GI-REAL"
        }

        response = await self.client.get("/api/orders/wb")
        data = await response.json()

        self.assertEqual(len(data["items"]), 1)
        row = data["items"][0]
        self.assertEqual(row["order_id"], 5805907258)
        self.assertFalse(row["is_new"])
        self.assertEqual(row["supplier_status"], "confirm")
        self.assertEqual(row["status"], "assigned")
        self.assertEqual(row["supply_id"], "WB-GI-REAL")

    def test_wb_orders_table_has_sequential_number_column(self):
        self.assertIn("<th>№</th>", INDEX_HTML)
        self.assertIn("(index+1)", INDEX_HTML)

    async def test_ozon_orders_are_placeholder(self):
        response = await self.client.get("/api/orders/ozon")
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertFalse(data["connected"])
        self.assertEqual(data["items"], [])

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
        self.crm = CRMServer(
            self.settings, FakeService(), FakeOrders(), self.db
        )
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
