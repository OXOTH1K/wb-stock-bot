import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.db import StateDB
from app.inventory_sync import SharedInventory
from app.models import Product


class FakeWBClient:
    def __init__(self):
        self.writes = []

    async def set_fbs_stocks(self, warehouse_id, quantities):
        self.writes.append((int(warehouse_id), dict(quantities)))


class FakeWBService:
    def __init__(self):
        self.products = {
            100: Product(100, "SKU-A", "Alpha", (11,)),
            200: Product(200, "WB-ONLY", "WB only", (22,)),
        }
        self.fbs_stock = {100: 3, 200: 2}
        self.wb_stock = {100: 0, 200: 0}
        self.warehouse = SimpleNamespace(id=77, name="WB FBS")
        self.wb = FakeWBClient()


class FakeOzonClient:
    def __init__(self):
        self.writes = []
        self.warehouses = [
            {
                "warehouse_id": 501,
                "name": "Ozon FBS",
                "is_rfbs": False,
                "status": "active",
            }
        ]

    async def get_fbs_warehouses(self):
        return list(self.warehouses)

    async def set_fbs_stocks(self, warehouse_id, quantities):
        self.writes.append((int(warehouse_id), dict(quantities)))


class SharedInventoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = StateDB(Path(self.tmp.name) / "state.sqlite3")
        self.wb = FakeWBService()
        self.ozon = FakeOzonClient()
        self.db.replace_channel_catalog(
            "ozon",
            [
                ("SKU-A", "Alpha Ozon", "1001"),
                ("OZON-ONLY", "Ozon Only", "1002"),
            ],
        )
        self.db.replace_channel_stock(
            "ozon_fbs",
            {"SKU-A": 3, "OZON-ONLY": 4},
        )
        self.inventory = SharedInventory(
            self.db,
            self.wb,
            ozon_client=self.ozon,
            ozon_warehouse_id=501,
        )
        self.inventory.bootstrap_local_inventory()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    async def test_wb_sale_decrements_local_and_updates_only_ozon(self):
        changed = await self.inventory.apply_sale(
            "wb",
            "5800000001",
            [("SKU-A", 2)],
        )

        self.assertEqual(changed, {"SKU-A": 1})
        self.assertEqual(self.inventory.local_quantity("SKU-A"), 1)
        self.assertEqual(self.wb.wb.writes, [])
        self.assertEqual(self.ozon.writes, [(501, {"SKU-A": 1})])
        self.assertEqual(
            self.db.get_channel_stock("ozon_fbs", ("SKU-A",)),
            {"SKU-A": 1},
        )

    async def test_ozon_sale_decrements_local_and_updates_only_wb(self):
        await self.inventory.apply_sale(
            "ozon",
            "12345678-0001-1",
            [("SKU-A", 2)],
        )

        self.assertEqual(self.inventory.local_quantity("SKU-A"), 1)
        self.assertEqual(self.ozon.writes, [])
        self.assertEqual(self.wb.wb.writes, [(77, {11: 1})])
        self.assertEqual(self.wb.fbs_stock[100], 1)

    async def test_duplicate_sale_is_idempotent_but_can_retry_sync(self):
        await self.inventory.apply_sale(
            "wb",
            "5800000001",
            [("SKU-A", 2)],
        )
        self.ozon.writes.clear()

        # Simulate the remote write not being reflected in the local channel
        # cache so the next poll retries synchronization without decrementing
        # the local inventory a second time.
        self.db.set_channel_stock("ozon_fbs", "SKU-A", 3)
        await self.inventory.apply_sale(
            "wb",
            "5800000001",
            [("SKU-A", 2)],
        )

        self.assertEqual(self.inventory.local_quantity("SKU-A"), 1)
        self.assertEqual(self.ozon.writes, [(501, {"SKU-A": 1})])

    async def test_suppressed_wb_does_not_follow_local_but_ozon_does(self):
        await self.inventory.suppress_channel("wb", "SKU-A")
        self.wb.wb.writes.clear()
        self.ozon.writes.clear()

        await self.inventory.set_local_quantity(
            "SKU-A", 2, reason="test"
        )

        self.assertTrue(self.inventory.is_suppressed("wb", "SKU-A"))
        self.assertEqual(self.wb.wb.writes, [])
        self.assertEqual(self.ozon.writes, [(501, {"SKU-A": 2})])

    async def test_restore_wb_uses_current_local_quantity(self):
        await self.inventory.suppress_channel("wb", "SKU-A")
        self.db.set_local_stock("SKU-A", 1, reason="sale:test")
        self.wb.wb.writes.clear()

        restored = await self.inventory.restore_channel("wb", "SKU-A")

        self.assertEqual(restored, 1)
        self.assertFalse(self.inventory.is_suppressed("wb", "SKU-A"))
        self.assertEqual(self.wb.wb.writes, [(77, {11: 1})])

    async def test_suppressed_ozon_does_not_follow_local_but_wb_does(self):
        await self.inventory.suppress_channel("ozon", "SKU-A")
        self.wb.wb.writes.clear()
        self.ozon.writes.clear()

        await self.inventory.set_local_quantity(
            "SKU-A", 2, reason="test"
        )

        self.assertTrue(self.inventory.is_suppressed("ozon", "SKU-A"))
        self.assertEqual(self.ozon.writes, [])
        self.assertEqual(self.wb.wb.writes, [(77, {11: 2})])

    async def test_restore_ozon_uses_current_local_quantity(self):
        await self.inventory.suppress_channel("ozon", "SKU-A")
        self.db.set_local_stock("SKU-A", 1, reason="sale:test")
        self.ozon.writes.clear()

        restored = await self.inventory.restore_channel(
            "ozon", "SKU-A"
        )

        self.assertEqual(restored, 1)
        self.assertFalse(self.inventory.is_suppressed("ozon", "SKU-A"))
        self.assertEqual(self.ozon.writes, [(501, {"SKU-A": 1})])

    async def test_marketplace_specific_products_are_not_written_elsewhere(self):
        self.wb.wb.writes.clear()
        self.ozon.writes.clear()

        await self.inventory.set_local_quantity(
            "WB-ONLY", 1, reason="test"
        )
        await self.inventory.set_local_quantity(
            "OZON-ONLY", 2, reason="test"
        )

        self.assertEqual(self.wb.wb.writes, [(77, {22: 1})])
        self.assertEqual(
            self.ozon.writes,
            [(501, {"OZON-ONLY": 2})],
        )

    async def test_configure_single_ozon_fbs_warehouse(self):
        inventory = SharedInventory(
            self.db,
            self.wb,
            ozon_client=self.ozon,
            ozon_warehouse_id=0,
        )

        warehouse_id = await inventory.configure_ozon_warehouse()

        self.assertEqual(warehouse_id, 501)
        self.assertEqual(inventory.ozon_warehouse_id, 501)

    async def test_multiple_ozon_warehouses_require_explicit_id(self):
        self.ozon.warehouses.append(
            {
                "warehouse_id": 502,
                "name": "Ozon FBS 2",
                "is_rfbs": False,
                "status": "active",
            }
        )
        inventory = SharedInventory(
            self.db,
            self.wb,
            ozon_client=self.ozon,
            ozon_warehouse_id=0,
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "Ozon has multiple active FBS warehouses",
        ):
            await inventory.configure_ozon_warehouse()


if __name__ == "__main__":
    unittest.main()
