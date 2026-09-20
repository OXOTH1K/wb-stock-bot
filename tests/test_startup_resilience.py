import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.db import StateDB
from app.models import ProductSize
from app.service import StockMonitorService


class FakeTelegram:
    def __init__(self):
        self.messages = []

    async def broadcast(self, chat_ids, text, **kwargs):
        self.messages.append(text)


class FakeWB:
    async def get_single_seller_warehouse(self):
        return SimpleNamespace(id=77, name="FBS")

    async def get_all_product_sizes(self):
        return [
            ProductSize(100, 1, "SKU-100", "Product", "0")
        ]

    async def get_fbs_stocks(self, warehouse_id, chrt_ids):
        return {1: 3}

    async def get_wb_stocks_by_nm(self, nm_ids):
        raise RuntimeError("WB API 429: Too Many Requests")


class StartupResilienceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = StateDB(Path(self.tmp.name) / "state.sqlite3")
        self.settings = SimpleNamespace(
            telegram_chat_ids=frozenset({123}),
            fbs_check_interval=300,
            wb_check_interval=1800,
            catalog_refresh_interval=21600,
            stocks_page_size=25,
        )

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    async def test_startup_uses_persisted_wb_snapshot_on_429(self):
        self.db.update_many("wb", {100: 9})
        tg = FakeTelegram()
        service = StockMonitorService(
            self.settings, FakeWB(), tg, self.db
        )

        await service.initialize()

        self.assertEqual(service.wb_stock, {100: 9})
        self.assertTrue(service._wb_loaded)
        self.assertIn(
            "последний сохранённый снимок",
            tg.messages[-1],
        )

    async def test_startup_survives_429_without_persisted_snapshot(self):
        tg = FakeTelegram()
        service = StockMonitorService(
            self.settings, FakeWB(), tg, self.db
        )

        await service.initialize()

        self.assertFalse(service._wb_loaded)
        self.assertIn("нет данных", tg.messages[-1])
        self.assertIn(
            "будут загружены фоновым циклом",
            tg.messages[-1],
        )


if __name__ == "__main__":
    unittest.main()
