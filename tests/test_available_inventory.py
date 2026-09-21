import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.db import StateDB
from app.models import Product
from app.service import StockMonitorService
from app.shared_inventory import SharedInventoryService


class FakeTelegram:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))

    async def broadcast(self, *args, **kwargs):
        return None


class FakeWBClient:
    def __init__(self):
        self.writes = []

    async def set_fbs_stocks(self, warehouse_id, quantities):
        self.writes.append((warehouse_id, dict(quantities)))


class FakeWBService:
    def __init__(self):
        self.products = {
            100: Product(100, "SKU-A", "Alpha", (11,))
        }
        self.fbs_stock = {100: 3}
        self.wb_stock = {100: 0}
        self.warehouse = SimpleNamespace(id=7, name="FBS")
        self.wb = FakeWBClient()


class FakeOzon:
    def __init__(self, db):
        self.db = db
        self.writes = []

    async def set_fbs_stock(self, sku, quantity):
        self.writes.append((str(sku), int(quantity)))
        self.db.set_channel_stock(
            "ozon_fbs", str(sku), int(quantity)
        )


class AvailableInventoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = StateDB(Path(self.tmp.name) / "state.sqlite3")
        self.wb_service = FakeWBService()
        self.db.replace_channel_catalog(
            "ozon", [("SKU-A", "Alpha OZON", "501")]
        )
        self.db.replace_channel_stock(
            "ozon_fbs", {"SKU-A": 3}
        )
        self.ozon = FakeOzon(self.db)
        self.shared = SharedInventoryService(
            self.db, self.wb_service, self.ozon
        )

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    async def test_transfer_available_conserves_owned_stock(self):
        await self.shared.initialize()
        self.assertEqual(
            (
                self.shared.local_quantity("SKU-A"),
                self.shared.available_quantity("SKU-A"),
            ),
            (0, 3),
        )

        await self.shared.set_local_stock(
            "SKU-A", 7, reason="restock"
        )
        await self.shared.set_available_stock(
            "SKU-A", 6, reason="test"
        )
        self.assertEqual(
            (
                self.shared.local_quantity("SKU-A"),
                self.shared.available_quantity("SKU-A"),
            ),
            (4, 6),
        )

        await self.shared.set_available_stock(
            "SKU-A", 2, reason="test"
        )
        self.assertEqual(
            (
                self.shared.local_quantity("SKU-A"),
                self.shared.available_quantity("SKU-A"),
            ),
            (8, 2),
        )

    async def test_set_command_moves_delta_and_syncs_both_fbs(self):
        await self.shared.initialize()
        await self.shared.set_local_stock(
            "SKU-A", 7, reason="restock"
        )

        tg = FakeTelegram()
        service = object.__new__(StockMonitorService)
        service.settings = SimpleNamespace(
            telegram_chat_ids=frozenset({123})
        )
        service.tg = tg
        service.shared_inventory = self.shared

        await service.handle_message(123, "/set SKU-A 6")

        self.assertEqual(
            self.shared.available_quantity("SKU-A"), 6
        )
        self.assertEqual(
            self.shared.local_quantity("SKU-A"), 4
        )
        self.assertEqual(
            self.wb_service.wb.writes[-1],
            (7, {11: 6}),
        )
        self.assertEqual(
            self.ozon.writes[-1], ("SKU-A", 6)
        )
        self.assertIn(
            "Доступно для заказа: 6 шт.",
            tg.sent[-1][1],
        )
        self.assertIn(
            "Мой склад: 7 → 4 шт.",
            tg.sent[-1][1],
        )

    async def test_set_command_rejects_more_than_local_reserve(self):
        await self.shared.initialize()
        tg = FakeTelegram()
        service = object.__new__(StockMonitorService)
        service.settings = SimpleNamespace(
            telegram_chat_ids=frozenset({123})
        )
        service.tg = tg
        service.shared_inventory = self.shared

        await service.handle_message(123, "/set SKU-A 4")

        self.assertEqual(
            self.shared.available_quantity("SKU-A"), 3
        )
        self.assertEqual(
            self.shared.local_quantity("SKU-A"), 0
        )
        self.assertIn(
            "Недостаточно товара",
            tg.sent[-1][1],
        )


if __name__ == "__main__":
    unittest.main()
