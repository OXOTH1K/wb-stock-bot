import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.db import StateDB
from app.models import ProductSize, build_products
from app.service import StockMonitorService


class FakeTelegram:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))

    async def broadcast(self, chat_ids, text, **kwargs):
        self.sent.append((next(iter(chat_ids)), text, kwargs))

    async def edit_message_text(self, *args, **kwargs):
        pass


class FakeWB:
    def __init__(self, fbs_by_chrt, wb_by_nm):
        self.fbs_by_chrt = dict(fbs_by_chrt)
        self.wb_by_nm = dict(wb_by_nm)

    async def get_fbs_stocks(self, warehouse_id, chrt_ids):
        return {
            int(chrt_id): int(self.fbs_by_chrt.get(int(chrt_id), 0))
            for chrt_id in chrt_ids
        }

    async def get_wb_stocks_by_nm(self, nm_ids):
        return {int(nm_id): int(self.wb_by_nm.get(int(nm_id), 0)) for nm_id in nm_ids}


class StatusAuditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = StateDB(Path(self.tmp.name) / "state.sqlite3")
        self.settings = SimpleNamespace(
            telegram_chat_ids=frozenset({123}),
            stocks_page_size=25,
            fbs_check_interval=300,
            wb_check_interval=1800,
        )
        self.sizes = [
            ProductSize(100, 1, "BOTH", "Both", "one"),
            ProductSize(200, 2, "RESTORE", "Restore", "one"),
            ProductSize(300, 3, "EMPTY", "Empty", "one"),
        ]

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def service(self):
        wb = FakeWB(
            {1: 3, 2: 0, 3: 0},
            {100: 5, 200: 0, 300: 0},
        )
        tg = FakeTelegram()
        service = StockMonitorService(self.settings, wb, tg, self.db)
        service.warehouse = SimpleNamespace(id=77, name="FBS")
        service.sizes = list(self.sizes)
        service.products = build_products(self.sizes)
        service._fbs_loaded = True
        service._wb_loaded = True
        self.db.save_product_fbs("wb_auto", 200, {2: 4})
        return service, tg

    async def test_status_stock_audit_offers_three_actionable_flows(self):
        service, tg = self.service()

        count = await service.audit_actionable_stocks(123)

        self.assertEqual(count, 3)
        self.assertEqual(len(tg.sent), 3)

        messages = {text: kwargs for _, text, kwargs in tg.sent}
        all_text = "\n".join(messages)

        self.assertIn("товар есть одновременно на FBS и WB", all_text)
        self.assertIn("Сохранённый FBS-остаток: 4 шт.", all_text)
        self.assertIn("товар закончился везде", all_text)

        callbacks = [
            button["callback_data"]
            for _, _, kwargs in tg.sent
            for row in kwargs["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertIn("fbszero:100:yes", callbacks)
        self.assertIn("fbsrestore:200:yes", callbacks)
        self.assertIn("fbsadd:300:1", callbacks)
        self.assertIn("fbsadd:300:5", callbacks)


if __name__ == "__main__":
    unittest.main()
