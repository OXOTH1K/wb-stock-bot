import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.db import StateDB
from app.wb_order_lookup import WBOrderLookup, event_time


class LookupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'state.db'
        self.db = StateDB(self.path)
        self.wb = AsyncMock()
        self.wb.MARKETPLACE_BASE = 'https://marketplace-api.wildberries.ru'
        self.lookup = WBOrderLookup(self.wb, self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def seed(self):
        self.db.archive_wb_orders('fbs', [{'id': 123, 'rid': 'a.b', 'article': 'SKU', 'warehouseId': 9,
                                         'createdAt': '2026-09-20T09:00:00Z', 'supplyId': 'WB-1'}])
        self.db.archive_wb_orders('orders', [
            {'srid': 'a.b', 'gNumber': 'basket', 'date': '2026-09-20T12:00:00', 'warehouseName': 'Казань'},
            {'srid': 'c.d', 'gNumber': 'basket', 'supplierArticle': 'OTHER'},
        ])
        self.db.archive_wb_orders('sales', [
            {'saleID': 'S1', 'srid': 'a.b', 'date': '2026-09-21T14:00:00'},
            {'saleID': 'R1', 'srid': 'a.b', 'date': '2026-09-22T14:00:00'},
        ])

    def test_lookup_by_all_identifiers_joins_sources_without_collapsing_basket(self):
        self.seed()
        for number in ('123', 'a.b', 'S1'):
            items = self.lookup.search(number)['items']
            self.assertEqual(len(items), 1)
            item = items[0]
            self.assertEqual(len(item['raw']), 4)
            self.assertEqual(item['article'], 'SKU')
            self.assertEqual(item['warehouse'], 'Казань')
            self.assertEqual(item['created_at'], '2026-09-20T09:00:00+00:00')
            self.assertEqual([e['title'] for e in item['events']], ['Заказ создан', 'Продажа в отчёте WB', 'Возврат в отчёте WB'])
            self.assertEqual(item['last_report_event']['title'], 'Возврат в отчёте WB')
        self.assertEqual(len(self.lookup.search('basket')['items']), 2)
        self.assertEqual(self.lookup.search('12')['items'], [])
        self.assertEqual(self.lookup.search("' OR 1=1 --")['items'], [])

    def test_status_changes_survive_restart_and_repeated_poll_does_not_duplicate(self):
        self.seed()
        for supplier, wb in [('new', 'waiting'), ('new', 'waiting'), ('confirm', 'waiting'), ('new', 'waiting')]:
            self.db.set_order_runtime_status(123, supplier, wb)
        self.db.close()
        self.db = StateDB(self.path)
        self.lookup = WBOrderLookup(self.wb, self.db)
        item = self.lookup.search('123')['items'][0]
        observed = [e for e in item['events'] if e['observed']]
        self.assertEqual(len(observed), 3)
        self.assertIn('Новое', item['status'])
        self.assertIsNotNone(item['status_at'])

    def test_fbs_partial_snapshot_preserves_identifiers_and_supply(self):
        self.seed()
        self.db.archive_wb_orders('fbs', [{'id': 123, 'article': 'UPDATED'}])
        item = self.lookup.search('a.b')['items'][0]
        self.assertEqual(item['article'], 'UPDATED')
        self.assertEqual(item['supply_id'], 'WB-1')

    def test_cancel_and_unknown_location_are_explicit(self):
        self.db.archive_wb_orders('orders', [{'srid': 'cancel', 'isCancel': True,
            'cancelDate': '2026-09-23T00:00:00', 'date': '2026-09-22T14:00:00', 'warehouseName': 'Казань'}])
        item = self.lookup.search('cancel')['items'][0]
        self.assertIn('недоступен', item['status'])
        self.assertEqual(item['events'][-1]['title'], 'Заказ отменён')
        self.assertFalse(any(e['observed'] for e in item['events']))
        self.assertEqual(event_time('0001-01-01T00:00:00', report=True), '')

    def test_old_local_order_is_searchable_without_inventing_creation_date(self):
        self.db.conn.execute('CREATE TABLE order_state (order_id INTEGER, article TEXT, nm_id INTEGER, supply_id TEXT, first_seen_at TEXT, status TEXT)')
        self.db.conn.execute("INSERT INTO order_state VALUES (777, 'OLD', 3, 'WB-old', '2025-01-01T10:00:00+03:00', 'assigned')")
        self.db.conn.commit()
        item = self.lookup.search('777')['items'][0]
        self.assertEqual(item['article'], 'OLD')
        self.assertEqual(item['created_at'], '')
        self.assertTrue(item['events'][0]['observed'])
        self.assertEqual(self.lookup.search('9' * 200)['items'], [])

    async def test_fbs_pagination_indexes_all_warehouses_and_statuses(self):
        self.wb._json.side_effect = [
            {'orders': [{'id': 1, 'warehouseId': 10}], 'next': 10},
            {'orders': [{'id': 2, 'warehouseId': 20}], 'next': 0},
            {'orders': [{'id': 1, 'supplierStatus': 'complete', 'wbStatus': 'sold'}]},
        ]
        with patch('app.wb_order_lookup.asyncio.sleep', new_callable=AsyncMock):
            await self.lookup.refresh_fbs()
        self.assertEqual(self.wb._json.call_args_list[1].kwargs['params']['next'], 10)
        self.assertEqual(self.wb._json.call_args_list[2].kwargs['json']['orders'], [1, 2])
        self.assertIn('Продан', self.lookup.search('1')['items'][0]['status'])
        self.assertEqual(self.lookup.search('2')['items'][0]['warehouse'], '20')

    async def test_fbs_bad_cursor_keeps_partial_rows_and_fails_visibly(self):
        self.wb._json.side_effect = [{'orders': [{'id': 1}], 'next': 10}, {'orders': [{'id': 2}], 'next': 10}]
        with patch('app.wb_order_lookup.asyncio.sleep', new_callable=AsyncMock):
            with self.assertRaisesRegex(ValueError, 'pagination'):
                await self.lookup.refresh_fbs()
        self.assertEqual(len(self.lookup.search('2')['items']), 1)

    async def test_statistics_pagination_waits_and_preserves_inclusive_boundary(self):
        changed = (datetime.now(timezone(timedelta(hours=3))) - timedelta(hours=1)).isoformat()
        page = [{'srid': 'a', 'lastChangeDate': changed}]
        self.wb._json.side_effect = [page, page]
        with patch('app.wb_order_lookup.asyncio.sleep', new_callable=AsyncMock) as sleep:
            await self.lookup.refresh_report('orders')
        sleep.assert_awaited_once_with(61)
        self.assertEqual(self.wb._json.call_args_list[1].kwargs['params']['dateFrom'], changed)
        self.assertEqual(len(self.lookup.search('a')['items']), 1)
        self.assertEqual(self.db.get_meta('wb_lookup_orders_cursor'), changed)

    async def test_report_incremental_refresh_overlaps_last_day(self):
        changed = datetime.now(timezone.utc) - timedelta(hours=2)
        self.db.set_meta('wb_lookup_orders_cursor', changed.isoformat())
        self.wb._json.return_value = []
        await self.lookup.refresh_report('orders')
        since = self.wb._json.call_args.kwargs['params']['dateFrom']
        self.assertEqual(datetime.fromisoformat(since), changed - timedelta(days=1))

    async def test_worker_outage_does_not_stop_other_sources_and_shutdown_cleans_up(self):
        fbs_ran, sales_ran = asyncio.Event(), asyncio.Event()

        async def fbs():
            fbs_ran.set()

        async def report(source):
            if source == 'orders':
                raise TimeoutError()
            sales_ran.set()

        self.lookup.refresh_fbs = fbs
        self.lookup.refresh_report = report
        with self.assertLogs('app.wb_order_lookup', level='ERROR'):
            task = asyncio.create_task(self.lookup.loop())
            try:
                await asyncio.wait_for(asyncio.gather(fbs_ran.wait(), sales_ran.wait()), 1)
                self.assertTrue(self.lookup.sources['orders']['error'])
                self.assertIsNotNone(self.lookup.sources['fbs']['updated_at'])
                self.assertIsNotNone(self.lookup.sources['sales']['updated_at'])
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

    async def test_report_failure_does_not_erase_cached_data_or_claim_complete(self):
        self.seed()
        self.wb._json.side_effect = TimeoutError()
        with patch('app.wb_order_lookup.asyncio.sleep', side_effect=asyncio.CancelledError):
            with self.assertLogs('app.wb_order_lookup', level='ERROR'):
                with self.assertRaises(asyncio.CancelledError):
                    await self.lookup._worker('orders')
        result = self.lookup.search('123')
        self.assertEqual(len(result['items']), 1)
        self.assertTrue(result['partial'])
        self.assertIn('TimeoutError', self.lookup.sources['orders']['error'])
        self.assertFalse(self.lookup.sources['orders']['syncing'])

    async def test_report_invalid_response_does_not_mark_success(self):
        self.wb._json.return_value = {'error': 'wrong shape'}
        with self.assertRaises(ValueError):
            await self.lookup.refresh_report('orders')
        self.assertIsNone(self.db.get_meta('wb_lookup_orders_cursor'))

    def test_lookup_is_read_only_for_stocks_and_does_not_call_api(self):
        self.seed()
        self.db.set_local_stock('SKU', 10, reason='test')
        self.db.set_order_available('SKU', 5, reason='test')
        before = self.db.conn.total_changes
        self.lookup.search('123')
        self.assertEqual(self.db.conn.total_changes, before)
        self.wb._json.assert_not_called()
        self.assertEqual(self.db.get_order_available(('SKU',))['SKU'], 5)


if __name__ == '__main__':
    unittest.main()
