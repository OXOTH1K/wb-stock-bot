import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.db import StateDB
from app.orders import FBSOrder, FBSSupply, OrderMonitor


class FakeTelegram:
    def __init__(self):
        self.broadcasts = []
        self.edits = []
        self.sent = []

    async def broadcast(self, chat_ids, text, **kwargs):
        self.broadcasts.append((chat_ids, text, kwargs))

    async def edit_message_text(self, chat_id, message_id, text, **kwargs):
        self.edits.append((chat_id, message_id, text, kwargs))

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))


class FakeWB:
    MARKETPLACE_BASE = "https://marketplace-api.wildberries.ru"

    def __init__(self, orders=None, supplies=None, statuses=None):
        self.orders = list(orders or [])
        self.supplies = list(supplies or [])
        self.statuses = dict(statuses or {})
        self.boxes = {}
        self.created = []
        self.added = []
        self.box_adds = []

    async def _json(self, method, url, **kwargs):
        if method == "GET" and url.endswith("/api/v3/orders/new"):
            return {"orders": list(self.orders)}
        if method == "POST" and url.endswith("/api/v3/orders/status"):
            rows = []
            current_ids = {int(order["id"]) for order in self.orders}
            for order_id in kwargs["json"]["orders"]:
                default = (
                    ("new", "waiting")
                    if int(order_id) in current_ids
                    else ("complete", "waiting")
                )
                supplier_status, wb_status = self.statuses.get(
                    int(order_id), default
                )
                rows.append(
                    {
                        "id": order_id,
                        "supplierStatus": supplier_status,
                        "wbStatus": wb_status,
                    }
                )
            return {"orders": rows}
        if method == "GET" and "/api/marketplace/v3/supplies/" in url and url.endswith("/order-ids"):
            supply_id = url.split("/supplies/", 1)[1].split("/", 1)[0]
            supply = next((s for s in self.supplies if s["id"] == supply_id), None)
            return {"orderIds": list((supply or {}).get("orderIds", []))}
        if method == "GET" and url.endswith("/api/v3/supplies"):
            return {"next": 0, "supplies": list(self.supplies)}
        if method == "POST" and url.endswith("/api/v3/supplies"):
            supply_id = f"WB-GI-{len(self.created) + 1}"
            name = kwargs["json"]["name"]
            self.created.append((supply_id, name))
            self.supplies.append({"id": supply_id, "name": name, "done": False, "cargoType": 0, "crossBorderType": 0})
            self.boxes[supply_id] = []
            return {"id": supply_id}
        if method == "PATCH" and "/api/marketplace/v3/supplies/" in url:
            supply_id = url.split("/supplies/", 1)[1].split("/", 1)[0]
            ids = list(kwargs["json"]["orders"])
            self.added.append((supply_id, ids))
            self.orders = [o for o in self.orders if o["id"] not in ids]
            return None
        if method == "GET" and url.endswith("/trbx"):
            supply_id = url.split("/supplies/", 1)[1].split("/", 1)[0]
            return {"trbxes": [{"id": x} for x in self.boxes.get(supply_id, [])]}
        if method == "POST" and url.endswith("/trbx"):
            supply_id = url.split("/supplies/", 1)[1].split("/", 1)[0]
            amount = kwargs["json"]["amount"]
            ids = [f"TRBX-{len(self.boxes.get(supply_id, [])) + i + 1}" for i in range(amount)]
            self.boxes.setdefault(supply_id, []).extend(ids)
            self.box_adds.append((supply_id, amount))
            return {"trbxIds": ids}
        if method == "DELETE" and "/api/v3/supplies/" in url:
            return None
        raise AssertionError((method, url, kwargs))


class OrderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = StateDB(Path(self.tmp.name) / "state.sqlite3")
        self.settings = SimpleNamespace(telegram_chat_ids=frozenset({123}), order_check_interval=30)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def row(self, order_id=501, cargo=1, cross=0):
        return {
            "id": order_id,
            "article": f"SKU-{order_id}",
            "nmId": 100,
            "warehouseId": 77,
            "createdAt": "2026-09-16T12:00:00Z",
            "cargoType": cargo,
            "crossBorderType": cross,
            "offices": ["СЦ Тест"],
        }

    def monitor(self, orders=None, supplies=None, statuses=None):
        wb = FakeWB(orders, supplies, statuses)
        tg = FakeTelegram()
        return OrderMonitor(self.settings, wb, tg, self.db, 77), wb, tg

    def test_keyboard_offers_create_only_when_no_supply(self):
        monitor, _, _ = self.monitor()
        order = FBSOrder(501, "SKU", 100, 77, "", 1, 0)
        callbacks = [r[0]["callback_data"] for r in monitor._keyboard(order, [])["inline_keyboard"]]
        self.assertIn("ordnew:501", callbacks)
        self.assertIn("ordskip:501", callbacks)

    def test_multiple_supplies_are_filtered(self):
        monitor, _, _ = self.monitor()
        order = FBSOrder(501, "SKU", 100, 77, "", 1, 0)
        supplies = [
            FBSSupply("A", "A", False, 1, 0),
            FBSSupply("B", "B", False, 2, 0),
            FBSSupply("C", "C", False, 1, 1),
            FBSSupply("D", "D", False, 0, 0),
        ]
        self.assertEqual({x.id for x in monitor._eligible(order, supplies)}, {"A", "D"})

    async def test_new_order_notified_only_once(self):
        monitor, _, tg = self.monitor([self.row()], [])
        await monitor.refresh()
        await monitor.refresh()
        self.assertEqual(len(tg.broadcasts), 1)
        self.assertIn("Новый FBS-заказ", tg.broadcasts[0][1])

    async def test_status_audit_reoffers_notified_but_unassigned_order(self):
        monitor, _, tg = self.monitor([self.row()], [])
        await monitor.refresh()
        self.assertEqual(len(tg.broadcasts), 1)

        count = await monitor.audit_pending(123)

        self.assertEqual(count, 1)
        self.assertEqual(len(tg.sent), 1)
        self.assertIn("/status: заказ требует решения", tg.sent[0][1])
        buttons = tg.sent[0][2]["reply_markup"]["inline_keyboard"]
        self.assertTrue(any(row[0]["callback_data"] == "ordnew:501" for row in buttons))

    async def test_status_audit_skips_assigned_and_skipped_orders(self):
        monitor, _, tg = self.monitor([self.row(501), self.row(502)], [])
        monitor._set_state(501, "assigned", "WB-GI-1")
        monitor._set_state(502, "skipped")

        count = await monitor.audit_pending(123)

        self.assertEqual(count, 0)
        self.assertEqual(tg.sent, [])


    async def test_supply_membership_overrides_stale_new_and_notified_state(self):
        supply = {
            "id": "WB-GI-42",
            "name": "Сегодня",
            "done": False,
            "cargoType": 1,
            "crossBorderType": 0,
            "orderIds": [501],
        }
        monitor, _, tg = self.monitor(
            [self.row()],
            [supply],
            {501: ("new", "waiting")},
        )
        monitor._remember(monitor._parse_order(self.row()))

        await monitor.refresh()

        state = monitor._state(501)
        self.assertEqual(state, ("assigned", "WB-GI-42"))
        self.assertNotIn(501, monitor.current_new_orders)
        self.assertEqual(monitor.current_supply_orders[501], "WB-GI-42")
        row = next(
            x for x in self.db.list_order_state()
            if x["order_id"] == 501
        )
        self.assertEqual(row["supplier_status"], "confirm")
        self.assertEqual(row["status"], "assigned")
        self.assertEqual(row["supply_id"], "WB-GI-42")
        self.assertEqual(tg.broadcasts, [])

    async def test_orders_new_row_does_not_override_confirm_status(self):
        monitor, _, tg = self.monitor(
            [self.row()],
            [],
            {501: ("confirm", "waiting")},
        )
        monitor._set_state(501, "assigned", "WB-GI-1")

        await monitor.refresh()

        row = next(
            x for x in self.db.list_order_state()
            if x["order_id"] == 501
        )
        self.assertEqual(row["supplier_status"], "confirm")
        self.assertNotIn(501, monitor.current_new_orders)
        self.assertEqual(tg.broadcasts, [])

    async def test_refresh_tracks_confirm_then_complete_for_crm(self):
        monitor, wb, _ = self.monitor([], [], {501: ("confirm", "waiting")})
        monitor._set_state(501, "assigned", "WB-GI-1")

        await monitor.refresh()
        row = next(x for x in self.db.list_order_state() if x["order_id"] == 501)
        self.assertEqual(row["supplier_status"], "confirm")

        wb.statuses[501] = ("complete", "waiting")
        await monitor.refresh()
        row = next(x for x in self.db.list_order_state() if x["order_id"] == 501)
        self.assertEqual(row["supplier_status"], "complete")

    async def test_existing_supply_add_does_not_create_box(self):
        supply = {"id": "WB-GI-7", "name": "Сегодня", "done": False, "cargoType": 1, "crossBorderType": 0}
        monitor, wb, tg = self.monitor([self.row()], [supply])
        await monitor.refresh()
        await monitor.handle_callback(123, 9, "ordadd:501:WB-GI-7", "alert")
        self.assertEqual(wb.added, [("WB-GI-7", [501])])
        self.assertEqual(wb.box_adds, [])
        self.assertIn("Заказ добавлен", tg.edits[-1][2])

    async def test_new_supply_name_does_not_include_article(self):
        monitor, wb, _ = self.monitor([self.row()], [])
        await monitor.refresh()
        await monitor.handle_callback(123, 9, "ordnew:501", "alert")

        self.assertEqual(len(wb.created), 1)
        _, name = wb.created[0]
        self.assertRegex(name, r"^TG \d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
        self.assertNotIn("SKU-501", name)

    async def test_new_supply_creates_exactly_one_box_and_duplicate_click_is_safe(self):
        monitor, wb, tg = self.monitor([self.row()], [])
        await monitor.refresh()
        await monitor.handle_callback(123, 9, "ordnew:501", "alert")
        await monitor.handle_callback(123, 10, "ordnew:501", "alert2")
        self.assertEqual(len(wb.created), 1)
        self.assertEqual(wb.box_adds, [("WB-GI-1", 1)])
        self.assertEqual(len(wb.boxes["WB-GI-1"]), 1)
        self.assertIn("Создано одно грузоместо", tg.edits[0][2])

    async def test_create_refreshes_choices_if_supply_appeared(self):
        monitor, wb, tg = self.monitor([self.row()], [])
        await monitor.refresh()
        wb.supplies.append({"id": "WB-GI-9", "name": "Новая", "done": False, "cargoType": 1, "crossBorderType": 0})
        await monitor.handle_callback(123, 9, "ordnew:501", "alert")
        self.assertEqual(wb.created, [])
        buttons = tg.edits[-1][3]["reply_markup"]["inline_keyboard"]
        self.assertTrue(any(r[0]["callback_data"] == "ordadd:501:WB-GI-9" for r in buttons))


if __name__ == "__main__":
    unittest.main()
