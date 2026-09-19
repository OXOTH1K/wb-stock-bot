import unittest
from types import SimpleNamespace

from app.models import Product, ProductSize, aggregate_by_nm, build_products
from app.service import StockMonitorService
from app.telegram import split_text


class LogicTests(unittest.TestCase):
    def setUp(self):
        self.sizes = [
            ProductSize(100, 1, "SKU-100", "Product 100", "S"),
            ProductSize(100, 2, "SKU-100", "Product 100", "M"),
            ProductSize(200, 3, "SKU-200", "Product 200", "0"),
        ]

    def test_aggregate_by_nm(self):
        result = aggregate_by_nm({1: 2, 2: 3, 3: 0}, self.sizes)
        self.assertEqual(result, {100: 5, 200: 0})

    def test_build_products(self):
        products = build_products(self.sizes)
        self.assertEqual(products[100].chrt_ids, (1, 2))
        self.assertEqual(products[200].vendor_code, "SKU-200")

    def test_split_text(self):
        chunks = split_text("a\nb\nc\n", 3)
        self.assertTrue(all(len(x) <= 3 for x in chunks))

    def _service_for_stock_format(self, fbs, wb):
        service = object.__new__(StockMonitorService)
        service.products = {100: Product(100, "SKU-100", "Product 100", (1,))}
        service.fbs_stock = {100: fbs}
        service.wb_stock = {100: wb}
        service.settings = SimpleNamespace(stocks_page_size=25)
        return service

    def test_stock_marker_is_green_when_fbs_has_stock(self):
        text = self._service_for_stock_format(3, 0)._format_stocks_page(1)
        self.assertIn("🟢 SKU-100", text)
        self.assertIn("   3    0", text)

    def test_stock_marker_is_green_when_wb_has_stock(self):
        text = self._service_for_stock_format(0, 2)._format_stocks_page(1)
        self.assertIn("🟢 SKU-100", text)
        self.assertIn("   0    2", text)

    def test_stock_marker_is_red_only_when_everywhere_zero(self):
        text = self._service_for_stock_format(0, 0)._format_stocks_page(1)
        self.assertIn("🔴 SKU-100", text)
        self.assertIn("   0    0", text)

    def test_stock_marker_is_purple_when_both_have_stock(self):
        text = self._service_for_stock_format(3, 2)._format_stocks_page(1)
        self.assertIn("🟣 SKU-100", text)
        self.assertIn("   3    2", text)

    def test_stock_page_hides_numeric_wb_article(self):
        text = self._service_for_stock_format(3, 0)._format_stocks_page(1)
        self.assertNotIn("WB 100", text)
        self.assertNotIn("Артикул WB", text)

    def test_stock_page_uses_preformatted_table(self):
        text = self._service_for_stock_format(3, 2)._format_stocks_page(1)
        self.assertIn("<pre>", text)
        self.assertIn("Артикул", text)
        self.assertIn("FBS", text)
        self.assertIn("WB", text)

    def test_stock_keyboard_has_next_button(self):
        service = object.__new__(StockMonitorService)
        service.products = {
            100: Product(100, "SKU-100", "Product 100", (1,)),
            200: Product(200, "SKU-200", "Product 200", (2,)),
        }
        service.fbs_stock = {100: 1, 200: 1}
        service.wb_stock = {100: 0, 200: 0}
        service.settings = SimpleNamespace(stocks_page_size=1)
        keyboard = service._stocks_keyboard(1)
        buttons = keyboard["inline_keyboard"][0]
        self.assertEqual(buttons[-1]["callback_data"], "stocks:2")
        self.assertEqual(buttons[1]["text"], "▶️")


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


class FakeDB:
    def __init__(self, previous=None):
        self.state = dict(previous or {})
        self.saved = {}
        self.pending = {}
        self.decisions = {}

    def update_many(self, source, quantities):
        transitions = []
        for nm_id, new_qty in quantities.items():
            key = (source, nm_id)
            old_qty = self.state.get(key)
            if old_qty is not None and old_qty != new_qty:
                transitions.append((nm_id, old_qty, new_qty))
            self.state[key] = new_qty
        return transitions

    def save_product_fbs(self, scope, nm_id, quantities):
        for key in [k for k in self.saved if k[0] == scope and k[1] == nm_id]:
            del self.saved[key]
        for chrt_id, quantity in quantities.items():
            self.saved[(scope, nm_id, chrt_id)] = int(quantity)

    def get_saved_product_fbs(self, scope, nm_id):
        return {
            chrt_id: qty
            for (saved_scope, saved_nm, chrt_id), qty in self.saved.items()
            if saved_scope == scope and saved_nm == nm_id
        }

    def clear_saved_product_fbs(self, scope, nm_id):
        for key in [k for k in self.saved if k[0] == scope and k[1] == nm_id]:
            del self.saved[key]

    def replace_saved_fbs(self, scope, rows):
        for key in [k for k in self.saved if k[0] == scope]:
            del self.saved[key]
        for (nm_id, chrt_id), quantity in rows.items():
            self.saved[(scope, nm_id, chrt_id)] = int(quantity)

    def get_saved_fbs(self, scope):
        return {
            (nm_id, chrt_id): qty
            for (saved_scope, nm_id, chrt_id), qty in self.saved.items()
            if saved_scope == scope
        }

    def clear_saved_fbs(self, scope):
        for key in [k for k in self.saved if k[0] == scope]:
            del self.saved[key]

    def put_pending_alert(self, alert_key, alert_type, nm_id, old_qty=0, new_qty=0):
        self.pending[alert_key] = (
            alert_key, alert_type, int(nm_id), int(old_qty), int(new_qty)
        )

    def list_pending_alerts(self):
        return list(self.pending.values())

    def delete_pending_alert(self, alert_key):
        self.pending.pop(alert_key, None)

    def save_stock_decision(self, nm_id, action, fbs_qty, wb_qty, decision):
        self.decisions[(int(nm_id), str(action))] = (
            int(fbs_qty), int(wb_qty), str(decision)
        )

    def stock_decision_matches(self, nm_id, action, fbs_qty, wb_qty, decision="skip"):
        return self.decisions.get((int(nm_id), str(action))) == (
            int(fbs_qty), int(wb_qty), str(decision)
        )

    def clear_stock_decision(self, nm_id, action):
        self.decisions.pop((int(nm_id), str(action)), None)

    def clear_stock_decisions(self, nm_id):
        for key in [k for k in self.decisions if k[0] == int(nm_id)]:
            del self.decisions[key]


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, fbs_qty):
        service = object.__new__(StockMonitorService)
        service.products = {100: Product(100, "SKU-100", "Product 100", (1,))}
        service.fbs_stock = {100: fbs_qty}
        service.wb_stock = {100: 0}
        service.tg = FakeTelegram()
        service.db = FakeDB()
        service.settings = SimpleNamespace(
            telegram_chat_ids=frozenset({123}), stocks_page_size=25
        )
        return service

    async def test_wb_appearance_notifies_when_fbs_has_stock(self):
        service = self._service(3)
        service.wb_stock[100] = 4
        await service._notify_wb_appearances([(100, 0, 4)])
        self.assertEqual(len(service.tg.messages), 1)
        text = service.tg.messages[0][1]
        self.assertIn("Товар появился на складе WB", text)
        self.assertIn("На вашем складе: 3 шт.", text)
        self.assertIn("было 0 шт. → стало 4 шт.", text)
        self.assertNotIn("Артикул WB", text)

    async def test_wb_appearance_does_not_notify_when_fbs_is_zero(self):
        service = self._service(0)
        await service._notify_wb_appearances([(100, 0, 4)])
        self.assertEqual(service.tg.messages, [])

    async def test_wb_positive_to_positive_is_not_appearance(self):
        service = self._service(3)
        await service._notify_wb_appearances([(100, 1, 4)])
        self.assertEqual(service.tg.messages, [])

    async def test_total_depletion_does_not_notify_when_fbs_zero_but_wb_has_stock(self):
        service = self._service(0)
        service.wb_stock = {100: 5}
        service.db = FakeDB({("total", 100): 8})
        await service._notify_total_depletions()
        self.assertEqual(service.tg.messages, [])

    async def test_total_depletion_does_not_notify_when_wb_zero_but_fbs_has_stock(self):
        service = self._service(3)
        service.wb_stock = {100: 0}
        service.db = FakeDB({("total", 100): 7})
        await service._notify_total_depletions()
        self.assertEqual(service.tg.messages, [])

    async def test_total_depletion_notifies_when_combined_stock_reaches_zero(self):
        service = self._service(0)
        service.wb_stock = {100: 0}
        service.db = FakeDB({("total", 100): 3})
        await service._notify_total_depletions()
        self.assertEqual(len(service.tg.messages), 1)
        text = service.tg.messages[0][1]
        self.assertIn("Товар закончился везде", text)
        self.assertIn("На вашем складе: 0 шт.", text)
        self.assertIn("На складах WB: 0 шт.", text)

    async def test_total_depletion_first_combined_snapshot_is_baseline(self):
        service = self._service(0)
        service.wb_stock = {100: 0}
        service.db = FakeDB()
        await service._notify_total_depletions()
        self.assertEqual(service.tg.messages, [])
        self.assertEqual(service.db.state[("total", 100)], 0)

    async def test_callback_edits_existing_stock_message(self):
        service = self._service(3)
        service.products[200] = Product(200, "SKU-200", "Product 200", (2,))
        service.fbs_stock[200] = 2
        service.wb_stock[200] = 1
        service.settings = SimpleNamespace(
            telegram_chat_ids=frozenset({123}), stocks_page_size=1
        )
        await service.handle_callback(123, 77, "stocks:2")
        self.assertEqual(len(service.tg.edits), 1)
        chat_id, message_id, text, kwargs = service.tg.edits[0]
        self.assertEqual((chat_id, message_id), (123, 77))
        self.assertIn("2/2", text)
        self.assertEqual(kwargs["parse_mode"], "HTML")


class FakeWB:
    def __init__(self, by_chrt=None):
        self.by_chrt = dict(by_chrt or {})
        self.updates = []

    async def get_fbs_stocks(self, warehouse_id, chrt_ids):
        return {chrt_id: self.by_chrt.get(chrt_id, 0) for chrt_id in chrt_ids}

    async def set_fbs_stocks(self, warehouse_id, quantities):
        self.updates.append((warehouse_id, dict(quantities)))
        self.by_chrt.update(quantities)


class StockActionTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, sizes, by_chrt, wb_qty):
        wb = FakeWB(by_chrt)
        tg = FakeTelegram()
        db = FakeDB()
        settings = SimpleNamespace(
            telegram_chat_ids=frozenset({123}), stocks_page_size=25
        )
        service = StockMonitorService(settings, wb, tg, db)
        service.warehouse = SimpleNamespace(id=77, name="FBS")
        service.sizes = sizes
        service.products = build_products(sizes)
        service.fbs_stock = aggregate_by_nm(by_chrt, sizes)
        service.wb_stock = {nm_id: wb_qty for nm_id in service.products}
        service._fbs_loaded = True
        service._wb_loaded = True
        return service, wb, tg

    def test_depletion_keyboard_has_requested_buttons(self):
        service, _, _ = self._service(
            [ProductSize(100, 1, "SKU-100", "Product", "0")], {1: 0}, 0
        )
        buttons = service._depletion_action_keyboard(100)["inline_keyboard"][0]
        self.assertEqual([b["text"] for b in buttons], ["1", "5", "Не добавлять"])

    def test_wb_appearance_keyboard_has_requested_buttons(self):
        service, _, _ = self._service(
            [ProductSize(100, 1, "SKU-100", "Product", "0")], {1: 3}, 2
        )
        buttons = service._wb_appearance_action_keyboard(100)["inline_keyboard"][0]
        self.assertEqual(
            [b["text"] for b in buttons], ["Обнулить FBS", "Не обнулять FBS"]
        )

    async def test_add_five_sets_single_variant_fbs_to_five(self):
        service, wb, tg = self._service(
            [ProductSize(100, 1, "SKU-100", "Product", "0")], {1: 0}, 0
        )
        await service.handle_callback(123, 9, "fbsadd:100:5", "alert")
        self.assertEqual(wb.updates, [(77, {1: 5})])
        self.assertEqual(service.fbs_stock[100], 5)
        self.assertIn("установлено 5 шт", tg.edits[-1][2])

    async def test_add_does_not_overwrite_newer_fbs_stock(self):
        service, wb, tg = self._service(
            [ProductSize(100, 1, "SKU-100", "Product", "0")], {1: 2}, 0
        )
        await service.handle_callback(123, 9, "fbsadd:100:5", "alert")
        self.assertEqual(wb.updates, [])
        self.assertIn("FBS уже изменился", tg.edits[-1][2])

    async def test_add_refuses_multivariant_product(self):
        service, wb, tg = self._service(
            [
                ProductSize(100, 1, "SKU-100", "Product", "S"),
                ProductSize(100, 2, "SKU-100", "Product", "M"),
            ],
            {1: 0, 2: 0},
            0,
        )
        await service.handle_callback(123, 9, "fbsadd:100:1", "alert")
        self.assertEqual(wb.updates, [])
        self.assertIn("несколько размеров/вариантов", tg.edits[-1][2])

    async def test_zero_fbs_zeros_all_variants(self):
        service, wb, tg = self._service(
            [
                ProductSize(100, 1, "SKU-100", "Product", "S"),
                ProductSize(100, 2, "SKU-100", "Product", "M"),
            ],
            {1: 1, 2: 2},
            4,
        )
        await service.handle_callback(123, 9, "fbszero:100:yes", "alert")
        self.assertEqual(wb.updates, [(77, {1: 0, 2: 0})])
        self.assertEqual(service.fbs_stock[100], 0)
        self.assertIn("FBS обнулён", tg.edits[-1][2])

    async def test_zero_fbs_is_blocked_if_wb_stock_is_gone(self):
        service, wb, tg = self._service(
            [ProductSize(100, 1, "SKU-100", "Product", "0")], {1: 3}, 0
        )
        await service.handle_callback(123, 9, "fbszero:100:yes", "alert")
        self.assertEqual(wb.updates, [])
        self.assertIn("больше не видит остаток", tg.edits[-1][2])

    async def test_skip_actions_do_not_write_stocks(self):
        service, wb, tg = self._service(
            [ProductSize(100, 1, "SKU-100", "Product", "0")], {1: 0}, 0
        )
        await service.handle_callback(123, 9, "fbsadd:100:skip", "alert")
        self.assertEqual(wb.updates, [])
        self.assertIn("не добавлять", tg.edits[-1][2])


if __name__ == "__main__":
    unittest.main()
