import asyncio
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.db import StateDB
from app.models import Product
from app.wb_client import WBAPIError
from app.wb_sales_analytics import WBSalesAnalytics, aggregate, amounts, period


def row(rid=1, **overrides):
    return dict(rrdId=rid, rrDate='2026-09-20', nmId=1, vendorCode='SKU', title='Product',
                currency='RUB', docTypeName='Продажа', sellerOperName='Продажа',
                quantity=1, forPay='100.01') | overrides


class CalculationTests(unittest.TestCase):
    def test_returns_commission_not_deducted_twice_and_expenses_excluded(self):
        sold = row(forPay='80.10', acquiringFee='10', ppvzSalesCommission='20',
                   deliveryService='15', paidStorage='5', deduction='9')
        refund = row(2, docTypeName='Возврат', sellerOperName='Возврат', forPay='80.10')
        self.assertEqual(amounts(sold), (1, 0, Decimal('80.10')))
        self.assertEqual(amounts(refund), (0, 1, Decimal('-80.10')))
        result = aggregate([sold, refund], '2026-09-20', '2026-09-21', {})
        self.assertEqual(result['SKU']['days']['2026-09-20'], [1, 1, '0.00'])

    def test_explicit_return_amount_is_counted_even_when_operation_name_is_ambiguous(self):
        refund = row(docTypeName='Возврат', sellerOperName='Продажа', quantity=0,
                     returnAmount=1, forPay='80.10')
        self.assertEqual(amounts(refund), (0, 1, Decimal('-80.10')))

    def test_compensation_is_not_a_sale_and_negative_adjustments_remain_signed(self):
        self.assertEqual(amounts(row(sellerOperName='Компенсация ущерба')), (0, 0, Decimal('100.01')))
        self.assertEqual(amounts(row(forPay='-12.50', sellerOperName='Коррекция продаж')), (0, 0, Decimal('-12.50')))

    def test_duplicate_invalid_date_currency_and_money_are_not_silent_zero(self):
        for rows in ([row(), row()], [row(currency='USD')], [row(forPay='NaN')],
                     [row(rrDate='bad')], [row(forPay='invalid')], [row(docTypeName='Unknown')]):
            with self.assertRaises((ValueError, KeyError)):
                aggregate(rows, '2026-09-20', '2026-09-21', {})

    def test_catalog_fallback_archived_sku_and_all_warehouse_models(self):
        products = {1: Product(1, 'SKU', 'Name', ())}
        result = aggregate([row(vendorCode='', deliveryMethod='FBS'),
                            row(2, vendorCode='OLD', nmId=99, deliveryMethod='FBW'),
                            row(3, vendorCode='', nmId=0, docTypeName='', forPay='0', quantity=0)],
                           '2026-09-20', '2026-09-21', products)
        self.assertEqual(set(result), {'SKU', 'OLD'})

    def test_period_validation(self):
        for start, end in [('bad','2026-01-01'), ('2026-01-02','2026-01-01'),
                           ('2024-01-01','2026-01-01'), ('2026-01-01','9999-12-31')]:
            with self.assertRaises(ValueError):
                period(start, end)


class FinanceLoadingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = StateDB(Path(self.tmp.name) / 'test.db')
        self.wb = SimpleNamespace(_json=AsyncMock())
        self.products = {1: Product(1, 'SKU', 'Name', ())}
        self.service = WBSalesAnalytics(self.wb, self.db, lambda: self.products, 'finance-test-token')

    async def asyncTearDown(self):
        self.db.close()
        self.tmp.cleanup()

    async def test_paginated_report_commits_only_complete_snapshot_and_caches_all_skus(self):
        self.wb._json.side_effect = [[row(9007199254740993)],
                                    [row(9007199254740994, vendorCode='OLD', forPay='1.23')], None]
        with patch('app.wb_sales_analytics.asyncio.sleep', new_callable=AsyncMock) as sleep:
            await self.service.refresh('2026-09-20', '2026-09-22')
            self.assertEqual(sleep.await_count, 2)
        calls = self.wb._json.call_args_list
        self.assertEqual(calls[1].kwargs['json']['rrdId'], 9007199254740993)
        self.assertEqual(calls[0].kwargs['json']['period'], 'weekly')
        self.assertIn('returnAmount', calls[0].kwargs['json']['fields'])
        self.assertEqual(calls[0].kwargs['token'], 'finance-test-token')
        data = self.service.view('2026-09-20','2026-09-22','SKU')
        self.assertTrue(data['ready'])
        self.assertFalse(data['syncing'])
        self.assertEqual(data['totals']['net'], '100.01')
        self.assertEqual([p['sales'] for p in data['points']], [1,0,0])
        self.assertEqual({p['sku'] for p in data['products']}, {'SKU','OLD'})
        self.assertEqual(self.service.view('2026-09-20','2026-09-22','OLD')['totals']['net'], '1.23')
        restarted = WBSalesAnalytics(self.wb, self.db, lambda: self.products)
        self.assertTrue(restarted.view('2026-09-20','2026-09-22','SKU')['ready'])

    async def test_missing_snapshot_is_not_zero_and_requests_coalesce(self):
        for sku in ('SKU', 'OLD', 'SKU'):
            data = self.service.view('2026-09-20','2026-09-22',sku)
            self.assertFalse(data['ready'])
            self.assertEqual(data['points'], [])
        self.assertEqual(self.service.queue.qsize(), 1)

    async def test_failed_refresh_preserves_previous_snapshot(self):
        self.wb._json.side_effect = [[row()], None]
        with patch('app.wb_sales_analytics.asyncio.sleep', new_callable=AsyncMock):
            await self.service.refresh('2026-09-20','2026-09-22')
        self.wb._json.side_effect = [[row(forPay='999')], WBAPIError('secret upstream body', 429)]
        with patch('app.wb_sales_analytics.asyncio.sleep', new_callable=AsyncMock):
            with self.assertRaises(WBAPIError):
                await self.service.refresh('2026-09-20','2026-09-22')
        self.assertEqual(self.service.view('2026-09-20','2026-09-22','SKU')['totals']['net'], '100.01')

    async def test_permission_failure_is_visible_without_exposing_upstream_body(self):
        self.wb._json.side_effect = WBAPIError('secret upstream body', 403)
        self.service.view('2026-09-20','2026-09-22','SKU')
        task = asyncio.create_task(self.service.loop())
        await asyncio.wait_for(self.service.queue.join(), 2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        data = self.service.view('2026-09-20','2026-09-22','SKU')
        self.assertIn('Финансы', data['error'])
        self.assertNotIn('secret', json.dumps(data))
        self.assertFalse(data['syncing'])
        self.assertFalse(data['ready'])

    async def test_stuck_cursor_does_not_publish_partial_data(self):
        self.wb._json.side_effect = [[row()], [row()]]
        with patch('app.wb_sales_analytics.asyncio.sleep', new_callable=AsyncMock):
            with self.assertRaises(ValueError):
                await self.service.refresh('2026-09-20','2026-09-22')
        self.assertFalse(self.service.view('2026-09-20','2026-09-22','SKU')['ready'])
