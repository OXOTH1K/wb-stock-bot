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

    async def test_available_is_a_limit_not_a_separate_stock_pool(self):
        await self.shared.initialize()
        self.assertEqual(
            (
                self.shared.local_quantity("SKU-A"),
                self.shared.available_quantity("SKU-A"),
            ),
            (3, 3),
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
            (7, 6),
        )

        await self.shared.set_available_stock(
            "SKU-A", 2, reason="test"
        )
        self.assertEqual(
            (
                self.shared.local_quantity("SKU-A"),
                self.shared.available_quantity("SKU-A"),
            ),
            (7, 2),
        )

    async def test_set_command_changes_limit_without_changing_local(self):
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
            self.shared.local_quantity("SKU-A"), 7
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
            "Мой склад: 7 шт. (не изменён)",
            tg.sent[-1][1],
        )

    async def test_split_model_migrates_to_full_local_once(self):
        self.db.ensure_local_stock("SKU-A", 2)
        self.db.ensure_order_available("SKU-A", 3)

        await self.shared.initialize()

        self.assertEqual(
            self.shared.local_quantity("SKU-A"), 5
        )
        self.assertEqual(
            self.shared.available_quantity("SKU-A"), 3
        )

        await self.shared.initialize()
        self.assertEqual(
            self.shared.local_quantity("SKU-A"), 5
        )

    async def test_set_main_adds_to_full_physical_stock(self):
        await self.shared.initialize()

        tg = FakeTelegram()
        service = object.__new__(StockMonitorService)
        service.settings = SimpleNamespace(
            telegram_chat_ids=frozenset({123})
        )
        service.tg = tg
        service.shared_inventory = self.shared

        await service.handle_message(123, "/set_main SKU-A +5")

        self.assertEqual(
            self.shared.local_quantity("SKU-A"), 8
        )
        self.assertEqual(
            self.shared.available_quantity("SKU-A"), 3
        )
        self.assertIn(
            "Мой склад: 3 → 8 шт. (+5)",
            tg.sent[-1][1],
        )
        self.assertIn(
            "Доступно для заказа: 3 шт.",
            tg.sent[-1][1],
        )

    async def test_set_main_subtracts_and_clamps_available(self):
        await self.shared.initialize()
        await self.shared.set_local_stock(
            "SKU-A", 8, reason="restock"
        )
        await self.shared.set_available_stock(
            "SKU-A", 6, reason="test"
        )
        self.wb_service.wb.writes.clear()
        self.ozon.writes.clear()

        tg = FakeTelegram()
        service = object.__new__(StockMonitorService)
        service.settings = SimpleNamespace(
            telegram_chat_ids=frozenset({123})
        )
        service.tg = tg
        service.shared_inventory = self.shared

        await service.handle_message(123, "/set_main SKU-A -5")

        self.assertEqual(
            self.shared.local_quantity("SKU-A"), 3
        )
        self.assertEqual(
            self.shared.available_quantity("SKU-A"), 3
        )
        self.assertEqual(
            self.wb_service.wb.writes[-1],
            (7, {11: 3}),
        )
        self.assertEqual(
            self.ozon.writes[-1], ("SKU-A", 3)
        )
        self.assertIn(
            "Мой склад: 8 → 3 шт. (-5)",
            tg.sent[-1][1],
        )
        self.assertIn(
            "автоматически уменьшен: 6 → 3",
            tg.sent[-1][1],
        )

    async def test_set_main_requires_signed_delta_and_prevents_negative_stock(self):
        await self.shared.initialize()

        tg = FakeTelegram()
        service = object.__new__(StockMonitorService)
        service.settings = SimpleNamespace(
            telegram_chat_ids=frozenset({123})
        )
        service.tg = tg
        service.shared_inventory = self.shared

        await service.handle_message(123, "/set_main SKU-A 2")
        self.assertIn("со знаком", tg.sent[-1][1])
        self.assertEqual(
            self.shared.local_quantity("SKU-A"), 3
        )

        await service.handle_message(123, "/set_main SKU-A -4")
        self.assertIn("Недостаточно товара", tg.sent[-1][1])
        self.assertEqual(
            self.shared.local_quantity("SKU-A"), 3
        )

    async def test_stocks_main_shows_full_physical_stock(self):
        await self.shared.initialize()
        await self.shared.set_local_stock(
            "SKU-A", 9, reason="inventory_count"
        )

        tg = FakeTelegram()
        service = object.__new__(StockMonitorService)
        service.settings = SimpleNamespace(
            telegram_chat_ids=frozenset({123}),
            stocks_page_size=25,
        )
        service.tg = tg
        service.db = self.db
        service.shared_inventory = self.shared

        await service.handle_message(123, "/stocks_main")

        self.assertIn("Мой склад", tg.sent[-1][1])
        self.assertIn("SKU-A", tg.sent[-1][1])
        self.assertIn("9", tg.sent[-1][1])
        self.assertEqual(
            tg.sent[-1][2]["reply_markup"]["inline_keyboard"][0][0][
                "callback_data"
            ],
            "stocksmain:noop",
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
            self.shared.local_quantity("SKU-A"), 3
        )
        self.assertIn(
            "не может превышать",
            tg.sent[-1][1],
        )


if __name__ == "__main__":
    unittest.main()
