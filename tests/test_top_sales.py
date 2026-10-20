import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock

from app.db import StateDB
from app.models import Product
from app.top_sales import TopSalesAnalytics


class TopSalesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = StateDB(Path(self.tmp.name) / 'state.db')
        self.products = {10: Product(10, 'WB-A', 'Альфа', (1,)),
                         20: Product(20, 'WB-B', 'Бета', (2,))}
        self.sources = {'orders': {'updated_at': '2026-10-20T10:00:00Z', 'error': ''},
                        'sales': {'updated_at': '2026-10-20T10:00:00Z', 'error': ''}}

    async def asyncTearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def archive(self, *rows):
        self.db.archive_wb_orders('orders', list(rows))

    async def test_wb_counts_ordered_units_and_distinct_orders_from_both_warehouse_types(self):
        self.archive(
            {'srid': 'fbw-1', 'supplierArticle': 'WB-A', 'nmId': 10, 'date': '2026-10-20T10:00:00',
             'warehouseType': 'Склад WB', 'quantity': 2},
            {'srid': 'fbs-1', 'supplierArticle': 'WB-A', 'nmId': 10, 'date': '2026-10-19T10:00:00',
             'warehouseType': 'Склад продавца', 'quantity': 1},
            {'srid': 'fbs-1', 'supplierArticle': 'WB-A', 'nmId': 10, 'date': '2026-10-19T10:00:00',
             'warehouseType': 'Склад продавца', 'quantity': 1},
            {'srid': 'old', 'supplierArticle': 'WB-B', 'nmId': 20, 'date': '2026-09-01T10:00:00',
             'warehouseType': 'Склад WB', 'quantity': 5},
        )
        self.db.archive_wb_orders('sales', [
            {'saleID': 'S-sale-1', 'supplierArticle': 'WB-A', 'date': '2026-10-20T10:00:00', 'quantity': 1},
            {'saleID': 'R-return-1', 'supplierArticle': 'WB-A', 'date': '2026-10-20T10:00:00', 'quantity': 4},
        ])
        analytics = TopSalesAnalytics(self.db, lambda: self.products,
                                      wb_sources=self.sources)
        rows, error = analytics._wb_orders(date(2026, 10, 14), date(2026, 10, 20))
        self.assertEqual(error, '')
        self.assertEqual(rows, [{'sku': 'WB-A', 'title': 'Альфа', 'quantity': 3, 'orders': 2, 'buyouts': 1}])

    async def test_ozon_counts_fbs_and_fbo_postings_by_order_created_at_and_units(self):
        client = type('Ozon', (), {})()
        client.list_order_postings = AsyncMock(side_effect=[
            [{'posting_number': 'FBS-1', 'status': 'delivered', 'created_at': '2026-10-20T09:00:00Z',
              'products': [{'offer_id': 'OZ-A', 'name': 'Ozon Alpha', 'quantity': 2}]}],
            [{'posting_number': 'FBO-1', 'status': 'awaiting_deliver', 'created_at': '2026-10-18T09:00:00Z',
              'products': [{'offer_id': 'OZ-A', 'name': 'Ozon Alpha', 'quantity': 1},
                           {'offer_id': 'OZ-B', 'name': 'Ozon Beta', 'quantity': 4}]},
             {'posting_number': 'OLD', 'created_at': '2026-09-01T09:00:00Z',
              'products': [{'offer_id': 'OZ-B', 'quantity': 10}]}],
        ])
        analytics = TopSalesAnalytics(self.db, lambda: {}, client, wb_sources=self.sources)
        postings, error = await analytics._ozon_orders(date(2026, 10, 1), date(2026, 10, 20))
        self.assertEqual(error, '')
        self.assertEqual(analytics._ozon_rank(postings, date(2026, 10, 14), date(2026, 10, 20)), [
            {'sku': 'OZ-B', 'title': 'Ozon Beta', 'quantity': 4, 'orders': 1, 'buyouts': 0},
            {'sku': 'OZ-A', 'title': 'Ozon Alpha', 'quantity': 3, 'orders': 2, 'buyouts': 2},
        ])
        self.assertEqual(client.list_order_postings.await_count, 2)

    async def test_view_returns_week_and_30_day_rankings_and_keeps_partial_source_errors_visible(self):
        self.archive({'srid': 'one', 'supplierArticle': 'WB-A', 'nmId': 10,
                      'date': '2026-10-20T10:00:00', 'quantity': 1})
        client = type('Ozon', (), {})()
        client.list_order_postings = AsyncMock(side_effect=[RuntimeError('private detail'), []])
        analytics = TopSalesAnalytics(self.db, lambda: self.products, client,
                                      wb_sources=self.sources)
        result = await analytics.view(force=True)
        self.assertEqual(result['week']['wb']['items'][0]['sku'], 'WB-A')
        self.assertTrue(result['week']['wb']['ready'])
        self.assertIn('Не удалось загрузить заказы Ozon', result['week']['ozon']['error'])
        self.assertNotIn('private detail', result['week']['ozon']['error'])
        self.assertEqual(result['week']['ozon']['items'], [])


if __name__ == '__main__':
    unittest.main()
