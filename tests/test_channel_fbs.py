import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.db import StateDB
from app.models import Product
from app.ozon import OzonIntegration
from app.service import StockMonitorService
from app.shared_inventory import SharedInventoryService


class ChannelFBSTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = StateDB(Path(self.tmp.name) / "state.sqlite3")
        self.remote_wb = {11: 3}
        self.remote_ozon = {"SKU": 2}
        self.settings = SimpleNamespace(telegram_chat_ids={123})
        self.tg = SimpleNamespace(send_message=AsyncMock(), edit_message_text=AsyncMock())

        async def write_wb(warehouse, quantities):
            self.remote_wb.update(quantities)

        self.wb = StockMonitorService(self.settings, SimpleNamespace(
            get_fbs_stocks=AsyncMock(side_effect=lambda *args: dict(self.remote_wb)),
            set_fbs_stocks=AsyncMock(side_effect=write_wb),
        ), self.tg, self.db)
        self.wb.products = {100: Product(100, "SKU", "Product", (11,))}
        self.wb.warehouse = SimpleNamespace(id=7)
        self.wb.fbs_stock = {100: 3}
        self.wb.wb_stock = {100: 0}
        self.db.replace_channel_catalog("ozon", [("SKU", "Product", "501")])
        self.db.set_channel_stock("ozon_fbs", "SKU", 2)
        self.db.ensure_local_stock("SKU", 10)
        self.db.ensure_order_available("SKU", 3)

        async def refresh(**kwargs):
            self.db.replace_channel_stock("ozon_fbs", self.remote_ozon)

        async def write_ozon(sku, quantity):
            self.remote_ozon[sku] = quantity
            self.db.set_channel_stock("ozon_fbs", sku, quantity)

        self.ozon = SimpleNamespace(
            refresh_catalog_and_stocks=AsyncMock(side_effect=refresh),
            set_fbs_stock=AsyncMock(side_effect=write_ozon),
        )
        self.inventory = SharedInventoryService(self.db, self.wb, self.ozon)
        self.wb.set_shared_inventory(self.inventory)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    async def test_wb_zero_and_restore_do_not_write_ozon_or_change_shared_stock(self):
        await self.inventory.zero_fbs_channel("wb")
        self.assertEqual(self.remote_wb, {11: 0})
        self.ozon.set_fbs_stock.assert_not_awaited()
        self.assertEqual(self.inventory.available_quantity("SKU"), 3)
        self.assertEqual(self.inventory.local_quantity("SKU"), 10)
        await self.inventory.restore_fbs_channel("wb")
        self.assertEqual(self.remote_wb, {11: 3})
        self.ozon.set_fbs_stock.assert_not_awaited()

    async def test_ozon_restores_its_snapshot_not_shared_quantity(self):
        await self.inventory.zero_fbs_channel("ozon")
        self.wb.wb.set_fbs_stocks.assert_not_awaited()
        self.assertEqual(self.remote_ozon["SKU"], 0)
        await self.inventory.restore_fbs_channel("ozon")
        await self.inventory.reconcile_all()
        self.assertEqual(self.remote_ozon["SKU"], 2)
        self.wb.wb.set_fbs_stocks.assert_not_awaited()

    async def test_repeat_zero_preserves_snapshot_and_restart_preserves_pause(self):
        await self.inventory.zero_fbs_channel("wb")
        self.wb.wb.set_fbs_stocks.reset_mock()
        await self.inventory.zero_fbs_channel("wb")
        self.wb.wb.set_fbs_stocks.assert_not_awaited()
        restarted = SharedInventoryService(self.db, self.wb, self.ozon)
        await restarted.reconcile_all()
        self.assertEqual(self.remote_wb[11], 0)
        await restarted.restore_fbs_channel("wb")
        self.assertEqual(self.remote_wb[11], 3)

    async def test_sales_reduce_restore_quantity_once(self):
        await self.inventory.zero_fbs_channel("wb")
        await self.inventory.consume_sales("ozon", [("sale1", "SKU", 1)])
        await self.inventory.consume_sales("ozon", [("sale1", "SKU", 1)])
        await self.inventory.restore_fbs_channel("wb")
        self.assertEqual(self.remote_wb[11], 2)
        self.assertEqual(self.inventory.available_quantity("SKU"), 2)
        self.assertEqual(self.inventory.local_quantity("SKU"), 9)

    async def test_both_pauses_are_independent_and_set_does_not_reopen_them(self):
        await self.inventory.zero_fbs_channel("wb")
        await self.inventory.zero_fbs_channel("ozon")
        await self.inventory.set_available_stock("SKU", 5)
        self.assertEqual((self.remote_wb[11], self.remote_ozon["SKU"]), (0, 0))
        await self.inventory.restore_fbs_channel("wb")
        await self.inventory.reconcile_all()
        self.assertEqual((self.remote_wb[11], self.remote_ozon["SKU"]), (3, 0))
        await self.inventory.restore_fbs_channel("ozon")
        self.assertEqual(self.remote_ozon["SKU"], 2)

    async def test_wb_variant_snapshot_is_restored(self):
        self.remote_wb = {11: 1, 12: 2}
        self.wb.products[100] = Product(100, "SKU", "Product", (11, 12))
        await self.inventory.zero_fbs_channel("wb")
        self.assertEqual(self.remote_wb, {11: 0, 12: 0})
        await self.inventory.restore_fbs_channel("wb")
        self.assertEqual(self.remote_wb, {11: 1, 12: 2})

    async def test_partial_failure_preserves_intent_for_background_retry(self):
        write = self.ozon.set_fbs_stock
        self.ozon.set_fbs_stock = AsyncMock(side_effect=RuntimeError("temporary failure"))
        with self.assertRaisesRegex(RuntimeError, "Снимок сохранён"):
            await self.inventory.zero_fbs_channel("ozon")
        self.ozon.set_fbs_stock = write
        await self.inventory.reconcile_all()
        self.assertEqual(self.remote_ozon["SKU"], 0)
        await self.inventory.restore_fbs_channel("ozon")
        self.assertEqual(self.remote_ozon["SKU"], 2)

    async def test_old_commands_and_buttons_cannot_mutate_inventory(self):
        for command in ("/fbs_zero_all", "/fbs_restore", "/ozon_fbs_zero_all", "/ozon_fbs_restore", "/fbs_zero_all_ozon", "/fbs_restore_ozon"):
            await self.wb.handle_message(123, command)
            self.assertIn("Не удалось распознать", self.tg.send_message.call_args.args[1])
        await self.wb.handle_callback(123, 1, "fbsallzero:yes")
        self.wb.wb.set_fbs_stocks.assert_not_awaited()

    async def test_new_wb_command_requires_confirmation_and_then_zeros_only_wb(self):
        await self.wb.handle_message(123, "/set_fbs_wb_zero")
        self.wb.wb.set_fbs_stocks.assert_not_awaited()
        keyboard = self.tg.send_message.call_args.kwargs["reply_markup"]
        data = keyboard["inline_keyboard"][0][0]["callback_data"]
        await self.wb.handle_callback(123, 1, data)
        self.assertEqual(self.remote_wb[11], 0)
        self.ozon.set_fbs_stock.assert_not_awaited()

    async def test_old_shared_zero_migrates_to_two_independent_pauses(self):
        await self.inventory.suppress_wb_mass()
        self.assertEqual(self.inventory.available_quantity("SKU"), 0)
        self.inventory.channel_fbs.migrate_legacy()
        self.assertEqual(self.inventory.available_quantity("SKU"), 3)
        await self.inventory.reconcile_all()
        self.assertEqual((self.remote_wb[11], self.remote_ozon["SKU"]), (0, 0))
        await self.inventory.restore_fbs_channel("wb")
        self.assertEqual((self.remote_wb[11], self.remote_ozon["SKU"]), (3, 0))
        await self.inventory.restore_fbs_channel("ozon")
        self.assertEqual(self.remote_ozon["SKU"], 3)

    async def test_new_ozon_routes_and_removed_aliases(self):
        integration = OzonIntegration(self.settings, None, self.tg, self.db)
        integration.set_shared_inventory(self.inventory)
        for old in ("/ozon_fbs_zero_all", "/ozon_fbs_restore", "/fbs_zero_all_ozon", "/fbs_restore_ozon"):
            self.assertFalse(await integration.handle_message(123, old))
        await integration.handle_callback(123, 1, "ozfbsallzero:yes")
        self.ozon.set_fbs_stock.assert_not_awaited()
        await integration.handle_message(123, "/set_fbs_ozon_zero")
        self.ozon.set_fbs_stock.assert_not_awaited()
        data = self.tg.send_message.call_args.kwargs["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        await integration.handle_callback(123, 1, data)
        self.assertEqual(self.remote_ozon["SKU"], 0)
        await integration.handle_message(123, "/restore_fbs_ozon")
        data = self.tg.send_message.call_args.kwargs["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        await integration.handle_callback(123, 2, data)
        self.assertEqual(self.remote_ozon["SKU"], 2)
        self.wb.wb.set_fbs_stocks.assert_not_awaited()

    async def test_restore_failure_retries_saved_target_and_clamps_to_physical(self):
        await self.inventory.zero_fbs_channel("wb")
        self.db.set_local_stock("SKU", 1, reason="correction")
        write = self.wb.wb.set_fbs_stocks
        self.wb.wb.set_fbs_stocks = AsyncMock(side_effect=RuntimeError("offline"))
        with self.assertRaisesRegex(RuntimeError, "Снимок сохранён"):
            await self.inventory.restore_fbs_channel("wb")
        self.wb.wb.set_fbs_stocks = write
        restarted = SharedInventoryService(self.db, self.wb, self.ozon)
        await restarted.reconcile_all()
        self.assertEqual(self.remote_wb[11], 1)
        await restarted.restore_fbs_channel("wb")
        self.assertEqual(restarted.channel_fbs.pending_snapshot("wb"), {})

    async def test_new_commands_reject_untrusted_chat(self):
        await self.wb.handle_message(999, "/set_fbs_wb_zero")
        await self.wb.handle_callback(999, 1, "channelwb:zero:yes")
        self.wb.wb.get_fbs_stocks.assert_not_awaited()
        self.wb.wb.set_fbs_stocks.assert_not_awaited()

    async def test_help_lists_exactly_four_bulk_commands(self):
        text = self.wb._format_help()
        for new in ("/set_fbs_wb_zero", "/restore_fbs_wb", "/set_fbs_ozon_zero", "/restore_fbs_ozon"):
            self.assertEqual(text.count(new), 1)
        for old in ("/fbs_zero_all", "/fbs_restore", "/ozon_fbs_zero_all", "/ozon_fbs_restore"):
            self.assertNotIn(old, text)
