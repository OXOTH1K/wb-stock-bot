import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from app.db import StateDB
from app.models import Product, ProductSize
from app.orders import OrderMonitor
from app.service import StockMonitorService


class FlakyTelegram:
    def __init__(self, fail_broadcasts=0):
        self.fail_broadcasts = fail_broadcasts
        self.broadcasts = []
        self.edits = []
        self.sent = []

    async def broadcast(self, chat_ids, text, **kwargs):
        if self.fail_broadcasts > 0:
            self.fail_broadcasts -= 1
            raise RuntimeError("telegram unavailable")
        self.broadcasts.append((chat_ids, text, kwargs))

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))

    async def edit_message_text(self, chat_id, message_id, text, **kwargs):
        self.edits.append((chat_id, message_id, text, kwargs))


class StockWB:
    async def get_fbs_stocks(self, warehouse_id, chrt_ids):
        return {int(x): 3 for x in chrt_ids}

    async def get_wb_stocks_by_nm(self, nm_ids):
        return {int(x): 4 for x in nm_ids}


class RecoveryOrderWB:
    MARKETPLACE_BASE = "https://marketplace-api.wildberries.ru"

    def __init__(self, historical_status="new"):
        self.historical_status = historical_status

    def row(self):
        return {
            "id": 9001,
            "article": "MISSED-ORDER",
            "nmId": 100,
            "warehouseId": 77,
            "createdAt": "2026-09-19T06:00:00Z",
            "cargoType": 1,
            "crossBorderType": 0,
            "offices": ["СЦ Тест"],
            "supplyId": "WB-GI-77" if self.historical_status != "new" else "",
        }

    async def _json(self, method, url, **kwargs):
        if method == "GET" and url.endswith("/api/v3/orders/new"):
            return {"orders": []}
        if method == "GET" and url.endswith("/api/v3/orders"):
            self.last_history_params = kwargs["params"]
            return {"next": 0, "orders": [self.row()]}
        if method == "POST" and url.endswith("/api/v3/orders/status"):
            return {
                "orders": [
                    {
                        "id": 9001,
                        "supplierStatus": self.historical_status,
                        "wbStatus": "waiting",
                    }
                ]
            }
        if method == "GET" and url.endswith("/api/v3/supplies"):
            return {"next": 0, "supplies": []}
        raise AssertionError((method, url, kwargs))


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = StateDB(Path(self.tmp.name) / "state.sqlite3")
        self.settings = SimpleNamespace(
            telegram_chat_ids=frozenset({123}),
            order_check_interval=30,
            stocks_page_size=25,
            fbs_check_interval=300,
            wb_check_interval=1800,
        )

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    async def test_stock_alert_survives_telegram_failure(self):
        tg = FlakyTelegram(fail_broadcasts=1)
        service = object.__new__(StockMonitorService)
        service.products = {100: Product(100, "SKU-100", "Product", (1,))}
        service.fbs_stock = {100: 3}
        service.wb_stock = {100: 4}
        service.db = self.db
        service.tg = tg
        service.settings = self.settings

        with self.assertRaises(RuntimeError):
            await service._notify_wb_appearances([(100, 0, 4)])

        pending = self.db.list_pending_alerts()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0][1], "wb_appearance")

        await service._flush_pending_alerts()

        self.assertEqual(self.db.list_pending_alerts(), [])
        self.assertEqual(len(tg.broadcasts), 1)
        self.assertIn("Товар появился на FBW", tg.broadcasts[0][1])

    async def test_stale_pending_stock_alert_is_discarded(self):
        tg = FlakyTelegram()
        service = object.__new__(StockMonitorService)
        service.products = {100: Product(100, "SKU-100", "Product", (1,))}
        service.fbs_stock = {100: 0}
        service.wb_stock = {100: 0}
        service.db = self.db
        service.tg = tg
        service.settings = self.settings
        self.db.put_pending_alert("wb_appearance:100", "wb_appearance", 100, 0, 4)

        await service._flush_pending_alerts()

        self.assertEqual(self.db.list_pending_alerts(), [])
        self.assertEqual(tg.broadcasts, [])

    async def test_poll_once_recovers_missed_new_order_from_history(self):
        old = datetime.now(timezone.utc) - timedelta(minutes=10)
        self.db.set_meta("marketplace_last_success", old.isoformat())
        tg = FlakyTelegram()
        wb = RecoveryOrderWB("new")
        monitor = OrderMonitor(self.settings, wb, tg, self.db, 77)
        recovered = []

        async def on_recovered(start, end):
            recovered.append((start, end))

        await monitor.poll_once(on_recovered)

        self.assertEqual(len(recovered), 1)
        self.assertEqual(len(tg.broadcasts), 1)
        self.assertIn("WB-заказ найден при сверке", tg.broadcasts[0][1])
        self.assertIsNotNone(monitor._state(9001))
        self.assertGreaterEqual(wb.last_history_params["dateFrom"], int((old - timedelta(seconds=1)).timestamp()))

    async def test_poll_once_reports_missed_order_already_processed(self):
        old = datetime.now(timezone.utc) - timedelta(minutes=10)
        self.db.set_meta("marketplace_last_success", old.isoformat())
        tg = FlakyTelegram()
        wb = RecoveryOrderWB("confirm")
        monitor = OrderMonitor(self.settings, wb, tg, self.db, 77)

        await monitor.poll_once()

        self.assertEqual(len(tg.broadcasts), 1)
        self.assertIn("уже успели изменить статус", tg.broadcasts[0][1])
        state = monitor._state(9001)
        self.assertEqual(state[0], "recovered:confirm")
        self.assertEqual(state[1], "WB-GI-77")


if __name__ == "__main__":
    unittest.main()
