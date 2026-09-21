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

    async def set_fbs_stocks(self, quantities):
        for sku, quantity in quantities.items():
            await self.set_fbs_stock(sku, quantity)


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

    async def test_legacy_wb_auto_zero_migrates_after_warehouse_stock_ended(self):
        self.wb.fbs_stock[100] = 0
        self.wb.wb_stock[100] = 0
        self.db.save_product_fbs("wb_auto", 100, {11: 3})

        await self.shared.initialize()

        self.assertEqual(
            self.shared.local_quantity("SKU-A"), 3
        )
        self.assertEqual(
            self.shared.available_quantity("SKU-A"), 0
        )
        self.assertTrue(
            self.shared.is_suppressed("wb", "SKU-A")
        )
        self.assertEqual(
            self.db.get_channel_suppression_reason(
                "wb", "SKU-A"
            ),
            "marketplace_stock",
        )

    async def test_wb_sale_decrements_available_and_syncs_both_fbs(self):
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
            self.shared.available_quantity("SKU-A"), 1
        )
        self.assertEqual(
            self.shared.local_quantity("SKU-A"), 0
        )
        self.assertEqual(self.ozon.writes, [("SKU-A", 1)])
        self.assertEqual(
            self.wb.wb.writes, [(7, {11: 1})]
        )

        await self.shared.consume_wb_orders(orders)
        self.assertEqual(
            self.shared.available_quantity("SKU-A"), 1
        )
        self.assertEqual(self.ozon.writes, [("SKU-A", 1)])
        self.assertEqual(
            self.wb.wb.writes, [(7, {11: 1})]
        )

    async def test_ozon_sale_decrements_available_and_syncs_both_fbs(self):
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
            self.shared.available_quantity("SKU-A"), 1
        )
        self.assertEqual(
            self.shared.local_quantity("SKU-A"), 0
        )
        self.assertEqual(
            self.wb.wb.writes, [(7, {11: 1})]
        )
        self.assertEqual(self.ozon.writes, [("SKU-A", 1)])

    async def test_manual_local_set_does_not_change_fbs(self):
        await self.shared.initialize()

        await self.shared.set_local_stock(
            "SKU-A", 4, reason="crm_set"
        )

        self.assertEqual(
            self.shared.local_quantity("SKU-A"), 4
        )
        self.assertEqual(
            self.shared.available_quantity("SKU-A"), 3
        )
        self.assertEqual(self.wb.wb.writes, [])
        self.assertEqual(self.ozon.writes, [])

    async def test_available_change_moves_stock_between_pools_and_syncs(self):
        await self.shared.initialize()
        await self.shared.set_local_stock(
            "SKU-A", 4, reason="restock"
        )

        available = await self.shared.set_available_stock(
            "SKU-A", 5, reason="crm_available_set"
        )

        self.assertEqual(available, 5)
        self.assertEqual(
            self.shared.local_quantity("SKU-A"), 2
        )
        self.assertEqual(
            self.wb.wb.writes[-1], (7, {11: 5})
        )
        self.assertEqual(self.ozon.writes[-1], ("SKU-A", 5))

        available = await self.shared.set_available_stock(
            "SKU-A", 1, reason="crm_available_set"
        )
        self.assertEqual(available, 1)
        self.assertEqual(
            self.shared.local_quantity("SKU-A"), 6
        )
        self.assertEqual(
            self.wb.wb.writes[-1], (7, {11: 1})
        )
        self.assertEqual(self.ozon.writes[-1], ("SKU-A", 1))

    async def test_available_increase_requires_local_stock(self):
        await self.shared.initialize()

        with self.assertRaisesRegex(
            ValueError,
            "Недостаточно товара",
        ):
            await self.shared.set_available_stock(
                "SKU-A", 4, reason="test"
            )

    async def test_available_sync_failure_is_reported_after_transfer(self):
        await self.shared.initialize()
        await self.shared.set_local_stock(
            "SKU-A", 2, reason="restock"
        )

        async def fail_ozon_write(sku, quantity):
            raise RuntimeError("write rejected")

        self.ozon.set_fbs_stock = fail_ozon_write

        with self.assertRaisesRegex(
            RuntimeError,
            "OZON: write rejected",
        ):
            await self.shared.set_available_stock(
                "SKU-A", 4, reason="crm_available_set"
            )

        self.assertEqual(
            self.shared.available_quantity("SKU-A"), 4
        )
        self.assertEqual(
            self.shared.local_quantity("SKU-A"), 1
        )
        self.assertEqual(
            self.wb.wb.writes[-1],
            (7, {11: 4}),
        )

    async def test_wb_suppression_zeros_shared_available_pool(self):

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
            0,
        )
        self.assertEqual(
            self.shared.available_quantity("SKU-A"), 0
        )
        self.assertEqual(
            self.shared.local_quantity("SKU-A"), 3
        )

        restored = await self.shared.restore_channel(
            "wb", "SKU-A"
        )
        self.assertEqual(restored, 3)
        self.assertEqual(
            self.wb.wb.writes[-1], (7, {11: 3})
        )
        self.assertEqual(self.ozon.writes[-1], ("SKU-A", 3))
        self.assertFalse(
            self.shared.is_suppressed("wb", "SKU-A")
        )

    async def test_mass_zero_returns_available_to_local_and_restore_moves_it_back(self):
        await self.shared.initialize()
        self.db.replace_channel_catalog(
            "ozon",
            [
                ("SKU-A", "Alpha OZON", "501"),
                ("OZON-ONLY", "Ozon Only", "502"),
            ],
        )
        self.db.replace_channel_stock(
            "ozon_fbs",
            {"SKU-A": 3, "OZON-ONLY": 4},
        )
        self.db.ensure_local_stock("OZON-ONLY", 4)
        self.db.ensure_order_available("OZON-ONLY", 0)
        self.db.transfer_order_available(
            "OZON-ONLY", 4, reason="test_bootstrap"
        )

        count, before = await self.shared.suppress_ozon_mass()

        self.assertEqual(count, 2)
        self.assertEqual(before, 7)
        self.assertEqual(
            self.shared.available_quantity("SKU-A"), 0
        )
        self.assertEqual(
            self.shared.available_quantity("OZON-ONLY"), 0
        )
        self.assertEqual(
            self.shared.local_quantity("SKU-A"), 3
        )
        self.assertEqual(
            self.shared.local_quantity("OZON-ONLY"), 4
        )

        restored, total = await self.shared.restore_ozon_mass()

        self.assertEqual(restored, 2)
        self.assertEqual(total, 7)
        self.assertEqual(
            self.shared.available_quantity("SKU-A"), 3
        )
        self.assertEqual(
            self.shared.available_quantity("OZON-ONLY"), 4
        )
        self.assertEqual(
            self.shared.local_quantity("SKU-A"), 0
        )
        self.assertEqual(
            self.shared.local_quantity("OZON-ONLY"), 0
        )

    async def test_mass_zero_does_not_override_individual_suppression(self):
        await self.shared.initialize()
        await self.shared.suppress_channel(
            "ozon", "SKU-A", "marketplace_stock"
        )

        count, total = await self.shared.suppress_wb_mass()

        self.assertEqual((count, total), (0, 0))
        self.assertEqual(
            self.db.get_channel_suppression_reason(
                "ozon", "SKU-A"
            ),
            "marketplace_stock",
        )
        self.assertIsNone(
            self.db.get_channel_suppression_reason(
                "wb", "SKU-A"
            )
        )

        restored, restored_total = (
            await self.shared.restore_wb_mass()
        )
        self.assertEqual((restored, restored_total), (0, 0))
        self.assertEqual(
            self.db.get_channel_suppression_reason(
                "ozon", "SKU-A"
            ),
            "marketplace_stock",
        )

    async def test_manual_available_edit_cancels_mass_restore_for_sku(self):
        await self.shared.initialize()
        await self.shared.suppress_wb_mass()

        self.assertEqual(
            self.db.get_available_snapshot("mass_shared"),
            {"SKU-A": 3},
        )

        await self.shared.set_available_stock(
            "SKU-A", 1, reason="manual_override"
        )

        self.assertEqual(
            self.db.get_available_snapshot("mass_shared"),
            {},
        )
        restored, total = await self.shared.restore_wb_mass()
        self.assertEqual((restored, total), (0, 0))
        self.assertEqual(
            self.shared.available_quantity("SKU-A"), 1
        )
        self.assertEqual(
            self.shared.local_quantity("SKU-A"), 2
        )

    async def test_ozon_suppression_zeros_both_fbs_and_restores_snapshot(self):
        await self.shared.initialize()

        await self.shared.suppress_channel(
            "ozon", "SKU-A", "marketplace_stock"
        )

        self.assertTrue(
            self.shared.is_suppressed("ozon", "SKU-A")
        )
        self.assertEqual(
            self.shared.available_quantity("SKU-A"), 0
        )
        self.assertEqual(
            self.shared.local_quantity("SKU-A"), 3
        )
        self.assertEqual(
            self.wb.wb.writes[-1], (7, {11: 0})
        )
        self.assertEqual(self.ozon.writes[-1], ("SKU-A", 0))

        restored = await self.shared.restore_channel(
            "ozon", "SKU-A"
        )
        self.assertEqual(restored, 3)
        self.assertEqual(
            self.shared.available_quantity("SKU-A"), 3
        )
        self.assertEqual(
            self.shared.local_quantity("SKU-A"), 0
        )
        self.assertEqual(
            self.wb.wb.writes[-1], (7, {11: 3})
        )
        self.assertEqual(self.ozon.writes[-1], ("SKU-A", 3))
        self.assertFalse(
            self.shared.is_suppressed("ozon", "SKU-A")
        )


if __name__ == "__main__":
    unittest.main()
