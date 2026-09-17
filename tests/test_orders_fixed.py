import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.db import StateDB
from app.orders_fixed import OrderMonitor


class FakeTelegram:
    async def broadcast(self, *args, **kwargs):
        return None

    async def edit_message_text(self, *args, **kwargs):
        return None

    async def send_message(self, *args, **kwargs):
        return None


class FakeWB:
    MARKETPLACE_BASE = "https://marketplace-api.wildberries.ru"

    def __init__(self):
        self.calls = []

    async def _json(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if method == "GET" and url.endswith("/trbx"):
            raise AssertionError("GET /trbx must not run before creating the box")
        if method == "POST" and url.endswith("/api/v3/supplies/WB-GI-1/trbx"):
            self.assert_payload(kwargs)
            return {"trbxIds": ["WB-TRBX-1"]}
        raise AssertionError((method, url, kwargs))

    @staticmethod
    def assert_payload(kwargs):
        if kwargs.get("json") != {"amount": 1}:
            raise AssertionError(kwargs)


class FixedOrderMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_supply_creates_box_with_direct_post(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            db = StateDB(Path(tmp.name) / "state.sqlite3")
            settings = SimpleNamespace(
                telegram_chat_ids=frozenset({123}), order_check_interval=30
            )
            wb = FakeWB()
            monitor = OrderMonitor(settings, wb, FakeTelegram(), db, 77)

            box_id = await monitor._one_box("WB-GI-1")

            self.assertEqual(box_id, "WB-TRBX-1")
            self.assertEqual(len(wb.calls), 1)
            method, url, kwargs = wb.calls[0]
            self.assertEqual(method, "POST")
            self.assertEqual(
                url,
                "https://marketplace-api.wildberries.ru/api/v3/supplies/WB-GI-1/trbx",
            )
            self.assertEqual(kwargs["json"], {"amount": 1})
            db.close()
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
