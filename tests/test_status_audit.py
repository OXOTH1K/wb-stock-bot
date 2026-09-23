import asyncio
import tempfile
import unittest
from unittest.mock import AsyncMock
from datetime import datetime, timezone
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
    def __init__(self, fbs_by_chrt, wb_by_nm, wb_error=None):
        self.fbs_by_chrt = dict(fbs_by_chrt)
        self.wb_by_nm = dict(wb_by_nm)
        self.wb_error = wb_error
        self.wb_calls = 0

    async def get_fbs_stocks(self, warehouse_id, chrt_ids):
        return {
            int(chrt_id): int(self.fbs_by_chrt.get(int(chrt_id), 0))
            for chrt_id in chrt_ids
        }

    async def get_wb_stocks_by_nm(self, nm_ids):
        self.wb_calls += 1
        if self.wb_error is not None:
            raise self.wb_error
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

        count, note = await service.audit_actionable_stocks(123)

        self.assertEqual(count, 3)
        self.assertIsNone(note)
        self.assertEqual(len(tg.sent), 3)

        messages = {text: kwargs for _, text, kwargs in tg.sent}
        all_text = "\n".join(messages)

        self.assertIn("товар есть одновременно на WB FBS и FBW", all_text)
        self.assertIn("Сохранённый WB FBS-остаток: 4 шт.", all_text)
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

    async def test_background_recovers_already_zero_stock_once_and_respects_skip(self):
        service, tg = self.service()
        service.products = {300: service.products[300]}
        service.shared_inventory = SimpleNamespace(
            available_quantity=lambda sku: 0,
            local_quantity=lambda sku: 5,
        )
        service.wb_stock = {300: 0}
        # Shared inventory writes total=0 before the periodic stock check.
        self.db.update_many("total", {300: 0})
        await service.refresh_fbs()
        self.assertEqual(len(tg.sent), 1)
        await service.refresh_fbs()
        self.assertEqual(len(tg.sent), 1)
        self.db.save_stock_decision(300, "fbsadd", 0, 0, "skip")
        await service.refresh_fbs()
        self.assertEqual(len(tg.sent), 1)
        # Replenishment followed by depletion starts a new notification cycle.
        service.wb.fbs_by_chrt[3] = 2
        await service.refresh_fbs()
        service.wb.fbs_by_chrt[3] = 0
        await service.refresh_fbs()
        self.assertEqual(len(tg.sent), 2)

    async def test_all_depletions_survive_failure_sending_first_alert(self):
        service, tg = self.service()
        service.fbs_stock = {100: 0, 200: 0, 300: 0}
        service.wb_stock = dict(service.fbs_stock)
        self.db.update_many("total", {100: 1, 200: 1, 300: 1})
        broadcast = tg.broadcast
        tg.broadcast = AsyncMock(side_effect=RuntimeError("offline"))
        with self.assertRaisesRegex(RuntimeError, "offline"):
            await service._notify_total_depletions()
        self.assertEqual(len(self.db.list_pending_alerts()), 3)
        tg.broadcast = broadcast
        await service._flush_pending_alerts()
        self.assertEqual(len(tg.sent), 3)
        self.assertEqual(self.db.list_pending_alerts(), [])

    async def test_concurrent_stock_checks_send_appearance_only_once(self):
        service, tg = self.service()
        service.fbs_stock = {100: 2}
        service.wb_stock = {100: 1}
        started = asyncio.Event()
        release = asyncio.Event()
        broadcast = tg.broadcast

        async def slow_broadcast(*args, **kwargs):
            started.set()
            await release.wait()
            await broadcast(*args, **kwargs)

        tg.broadcast = slow_broadcast
        first = asyncio.create_task(service._notify_wb_appearances([(100, 0, 1)]))
        try:
            await asyncio.wait_for(started.wait(), 1)
            second = asyncio.create_task(service._flush_pending_alerts())
            await asyncio.sleep(0)
        finally:
            release.set()
        await asyncio.gather(first, second)
        self.assertEqual(len(tg.sent), 1)
        self.assertEqual(self.db.list_pending_alerts(), [])

    async def test_appearance_remembers_delivery_and_respects_skip(self):
        service, tg = self.service()
        service.fbs_stock = {100: 2}
        service.wb_stock = {100: 1}
        await service._notify_wb_appearances([(100, 0, 1)])
        await service._notify_wb_appearances([(100, 0, 1)])
        self.assertEqual(len(tg.sent), 1)
        self.db.save_stock_decision(100, "fbszero", 2, 1, "skip")
        await service._notify_wb_appearances([(100, 0, 1)])
        self.assertEqual(len(tg.sent), 1)
        self.db.clear_stock_decisions(100)
        await service._notify_wb_appearances([(100, 0, 1)])
        self.assertEqual(len(tg.sent), 2)

    async def test_status_uses_fresh_wb_cache_without_extra_analytics_request(self):
        service, _ = self.service()
        service.wb_stock = {100: 5, 200: 0, 300: 0}
        service.wb_updated_at = datetime.now(timezone.utc)
        service.wb.wb_calls = 0

        _, note = await service.audit_actionable_stocks(123)

        self.assertEqual(service.wb.wb_calls, 0)
        self.assertIn("последний успешный снимок", note)

    async def test_status_falls_back_to_stale_cache_on_429(self):
        service, _ = self.service()
        service.wb_stock = {100: 5, 200: 0, 300: 0}
        service.wb_updated_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        service.wb.wb_error = RuntimeError("WB API 429: Too Many Requests")

        count, note = await service.audit_actionable_stocks(123)

        self.assertEqual(count, 3)
        self.assertEqual(service.wb.wb_calls, 1)
        self.assertIn("429", note)
        self.assertIn("последний успешный снимок", note)

    async def test_status_does_not_repeat_skipped_empty_product_until_stock_changes(self):
        service, tg = self.service()
        service.wb_stock = {100: 5, 200: 0, 300: 0}
        service.wb_updated_at = datetime.now(timezone.utc)

        # Simulate choosing "Не добавлять" for EMPTY while it is 0/0.
        self.db.save_stock_decision(300, "fbsadd", 0, 0, "skip")

        count, _ = await service.audit_actionable_stocks(123)

        self.assertEqual(count, 2)
        all_text = "\n".join(text for _, text, _ in tg.sent)
        self.assertNotIn("Артикул продавца: EMPTY", all_text)

        # A real stock transition starts a new decision cycle.
        self.db.update_many("fbs", {300: 2})
        self.db.clear_stock_decisions(300)
        service.wb.fbs_by_chrt[3] = 0
        service.wb.wb_by_nm[300] = 0
        service.wb_updated_at = datetime.now(timezone.utc)
        service.wb_stock[300] = 0

        # Current 0/0 state can be offered again after the intervening change.
        tg.sent.clear()
        count, _ = await service.audit_actionable_stocks(123)

        self.assertEqual(count, 3)
        all_text = "\n".join(text for _, text, _ in tg.sent)
        self.assertIn("Артикул продавца: EMPTY", all_text)

    async def test_skip_callback_is_persisted_for_status(self):
        service, _ = self.service()
        service.fbs_stock = {100: 3, 200: 0, 300: 0}
        service.wb_stock = {100: 5, 200: 0, 300: 0}

        await service.handle_callback(123, 7, "fbsadd:300:skip", "alert")

        self.assertTrue(
            self.db.stock_decision_matches(300, "fbsadd", 0, 0, "skip")
        )


if __name__ == "__main__":
    unittest.main()
