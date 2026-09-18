import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.db import StateDB
from app.models import ProductSize, aggregate_by_nm, build_products
from app.service import StockMonitorService


class FakeTelegram:
    def __init__(self):
        self.messages = []
        self.edits = []
        self.sent = []

    async def broadcast(self, chat_ids, text, **kwargs):
        self.messages.append((chat_ids, text, kwargs))

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))

    async def edit_message_text(self, chat_id, message_id, text, **kwargs):
        self.edits.append((chat_id, message_id, text, kwargs))


class FakeWB:
    def __init__(self, by_chrt):
        self.by_chrt = dict(by_chrt)
        self.updates = []

    async def get_fbs_stocks(self, warehouse_id, chrt_ids):
        return {int(chrt_id): int(self.by_chrt.get(int(chrt_id), 0)) for chrt_id in chrt_ids}

    async def set_fbs_stocks(self, warehouse_id, quantities):
        quantities = {int(k): int(v) for k, v in quantities.items()}
        self.updates.append((int(warehouse_id), quantities))
        self.by_chrt.update(quantities)


class SavedFBSStockTests(unittest.IsolatedAsyncioTestCase):
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
            ProductSize(100, 1, "SKU-100", "Product", "S"),
            ProductSize(100, 2, "SKU-100", "Product", "M"),
        ]

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def service(self, by_chrt, wb_qty=0):
        wb = FakeWB(by_chrt)
        tg = FakeTelegram()
        service = StockMonitorService(self.settings, wb, tg, self.db)
        service.warehouse = SimpleNamespace(id=77, name="FBS")
        service.sizes = list(self.sizes)
        service.products = build_products(self.sizes)
        service.fbs_stock = aggregate_by_nm(by_chrt, self.sizes)
        service.wb_stock = {100: wb_qty}
        service._fbs_loaded = True
        service._wb_loaded = True
        return service, wb, tg

    async def test_zero_from_wb_alert_saves_exact_variant_quantities(self):
        service, wb, _ = self.service({1: 1, 2: 2}, wb_qty=5)

        await service._zero_fbs_from_alert(123, 9, "alert", 100)

        self.assertEqual(self.db.get_saved_product_fbs("wb_auto", 100), {1: 1, 2: 2})
        self.assertEqual(wb.updates[-1], (77, {1: 0, 2: 0}))

    async def test_depletion_offers_saved_restore(self):
        service, _, tg = self.service({1: 0, 2: 0}, wb_qty=0)
        self.db.update_many("total", {100: 4})
        self.db.save_product_fbs("wb_auto", 100, {1: 1, 2: 2})

        await service._notify_total_depletions()

        self.assertEqual(len(tg.messages), 1)
        text = tg.messages[0][1]
        markup = tg.messages[0][2]["reply_markup"]
        self.assertIn("сохранено: 3 шт", text)
        callbacks = [b["callback_data"] for b in markup["inline_keyboard"][0]]
        self.assertIn("fbsrestore:100:yes", callbacks)

    async def test_restore_saved_product_restores_exact_variants_and_clears_snapshot(self):
        service, wb, tg = self.service({1: 0, 2: 0}, wb_qty=0)
        self.db.save_product_fbs("wb_auto", 100, {1: 1, 2: 2})

        await service.handle_callback(123, 9, "fbsrestore:100:yes", "alert")

        self.assertEqual(wb.updates[-1], (77, {1: 1, 2: 2}))
        self.assertEqual(self.db.get_saved_product_fbs("wb_auto", 100), {})
        self.assertIn("3 шт", tg.edits[-1][2])

    async def test_mass_zero_preserves_snapshot_on_repeated_zero(self):
        service, wb, _ = self.service({1: 2, 2: 3}, wb_qty=0)

        await service._zero_all_fbs(123, 9, "confirm")
        first = self.db.get_saved_fbs("mass")
        await service._zero_all_fbs(123, 10, "confirm2")
        second = self.db.get_saved_fbs("mass")

        self.assertEqual(first, {(100, 1): 2, (100, 2): 3})
        self.assertEqual(second, first)
        self.assertEqual(wb.by_chrt, {1: 0, 2: 0})

    async def test_mass_restore_refuses_to_overwrite_changed_fbs(self):
        service, wb, tg = self.service({1: 2, 2: 3}, wb_qty=0)
        await service._zero_all_fbs(123, 9, "confirm")
        wb.by_chrt[1] = 1
        updates_before = len(wb.updates)

        await service._restore_all_fbs(123, 10, "restore")

        self.assertEqual(len(wb.updates), updates_before)
        self.assertTrue(self.db.get_saved_fbs("mass"))
        self.assertIn("уже изменился", tg.edits[-1][2])


if __name__ == "__main__":
    unittest.main()
