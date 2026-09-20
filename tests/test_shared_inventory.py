import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.db import StateDB
from app.models import Product
from app.orders import FBSOrder
from app.ozon_client import OzonPosting, OzonPostingProduct
from app.shared_inventory import SharedInventoryService


class FakeWBClient:
    def __init__(self):
        self.writes = []

    async def set_fbs_stocks(self, warehouse_id, quantities):
        self.writes.append((warehouse_id, dict(quantities)))


class FakeWBService:
    def __init__(self):
        self.products = {
            100: Product(
                100, "SKU-A", "Alpha", (11,)
            )
        }
        self.fbs_stock = {100: 3}
        self.wb_stock = {100: 0}
        self.warehouse = SimpleNamespace(id=7, name="WB FBS")
        self.wb = FakeWBClient()


class FakeOzon:
    def __init__(self, db):
        self.db = db
        self.writes = []

    async def set_fbs_stock(self, sku, quantity):
        self.writes.append((sku, int(quantity)))
        self.db.set_channel_stock(
            "ozon_fbs", sku, int(quantity)
        )


class SharedInventoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = StateDB(Path(self.tmp.name) / "state.sqlite3")
        self.wb = FakeWBService()
        self.db.replace_channel_catalog(
            "ozon",
            [("SKU-A", "Alpha OZON", "501")],
        )
        self.db.replace_channel_stock(
            "ozon_fbs", {"SKU-A": 3}
        )
        self.ozon = FakeOzon(self.db)
        self.shared = SharedInventoryService(
            self.db, self.wb, self.ozon
        )

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    async def test_wb_sale_decrements_local_and_updates_ozon(self):
        await self.shared.initialize()
        orders = [
            FBSOrder(
                id=1,
                article="SKU-A",
                nm_id=100,
                warehouse_id=7,
                created_at="",
                cargo_type=1,
                cross_border_type=0,
            ),
            FBSOrder(
                id=2,
                article="SKU-A",
                nm_id=100,
                warehouse_id=7,
                created_at="",
                cargo_type=1,
                cross_border_type=0,
            ),
        ]

        changed = await self.shared.consume_wb_orders(orders)

        self.assertEqual(changed, {"SKU-A": 1})
        self.assertEqual(
            self.shared.local_quantity("SKU-A"), 1
        )
        self.assertEqual(self.ozon.writes, [("SKU-A", 1)])
        self.assertEqual(self.wb.wb.writes, [])

        await self.shared.consume_wb_orders(orders)
        self.assertEqual(
            self.shared.local_quantity("SKU-A"), 1
        )
        self.assertEqual(self.ozon.writes, [("SKU-A", 1)])

    async def test_ozon_sale_decrements_local_and_updates_wb(self):
        await self.shared.initialize()
        posting = OzonPosting(
            posting_number="P1",
            order_number="1",
            status="awaiting_packaging",
            cutoff="",
            warehouse_id=77,
            products=(
                OzonPostingProduct(
                    "SKU-A", "Alpha", 2, 9001
                ),
            ),
        )

        changed = await self.shared.consume_ozon_postings(
            [posting]
        )

        self.assertEqual(changed, {"SKU-A": 1})
        self.assertEqual(
            self.shared.local_quantity("SKU-A"), 1
        )
        self.assertEqual(
            self.wb.wb.writes, [(7, {11: 1})]
        )
        self.assertEqual(self.ozon.writes, [])

    async def test_wb_suppression_only_zeros_wb(self):
        await self.shared.initialize()

        await self.shared.suppress_channel(
            "wb", "SKU-A", "marketplace_stock"
        )

        self.assertTrue(
            self.shared.is_suppressed("wb", "SKU-A")
        )
        self.assertEqual(
            self.wb.wb.writes[-1], (7, {11: 0})
        )
        self.assertEqual(
            self.db.get_channel_stock(
                "ozon_fbs", ("SKU-A",)
            )["SKU-A"],
            3,
        )

        await self.shared.set_local_stock(
            "SKU-A", 2, reason="test"
        )
        self.assertEqual(
            self.wb.wb.writes[-1], (7, {11: 0})
        )
        self.assertEqual(self.ozon.writes[-1], ("SKU-A", 2))

        restored = await self.shared.restore_channel(
            "wb", "SKU-A"
        )
        self.assertEqual(restored, 2)
        self.assertEqual(
            self.wb.wb.writes[-1], (7, {11: 2})
        )
        self.assertFalse(
            self.shared.is_suppressed("wb", "SKU-A")
        )

    async def test_ozon_suppression_only_zeros_ozon(self):
        await self.shared.initialize()

        await self.shared.suppress_channel(
            "ozon", "SKU-A", "marketplace_stock"
        )
        self.assertTrue(
            self.shared.is_suppressed("ozon", "SKU-A")
        )
        self.assertEqual(self.ozon.writes[-1], ("SKU-A", 0))
        self.assertEqual(self.wb.fbs_stock[100], 3)

        await self.shared.set_local_stock(
            "SKU-A", 1, reason="test"
        )
        self.assertEqual(
            self.wb.wb.writes[-1], (7, {11: 1})
        )
        self.assertEqual(self.ozon.writes[-1], ("SKU-A", 0))

        restored = await self.shared.restore_channel(
            "ozon", "SKU-A"
        )
        self.assertEqual(restored, 1)
        self.assertEqual(self.ozon.writes[-1], ("SKU-A", 1))
        self.assertFalse(
            self.shared.is_suppressed("ozon", "SKU-A")
        )


if __name__ == "__main__":
    unittest.main()
