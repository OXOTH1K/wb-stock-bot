import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.db import StateDB
from app.models import Product
from app.wb_order_lookup import WBOrderLookup, report_status


class FBWOrdersTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = StateDB(Path(self.tmp.name) / 'state.db')
        self.wb = Mock()
        self.lookup = WBOrderLookup(self.wb, self.db)
        self.products = {10: Product(10, 'SKU-A', 'Кейкап', (1,)),
                         20: Product(20, 'SKU-B', 'Брелок', (2,))}

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def order(self, srid, **kwargs):
        return dict(srid=srid, **({'warehouseType': 'Склад WB', 'supplierArticle': 'SKU-A',
            'nmId': 10, 'date': '2026-09-20T12:00:00', 'gNumber': '12345678901234567890',
            'warehouseName': 'Казань', 'regionName': 'Волгоградская область'} | kwargs))

    def get(self, first='2026-09-20', last='2026-09-20'):
        return self.lookup.fbw_orders(first, last, self.products)

    def test_only_confirmed_fbw_by_order_date_inclusive_moscow_days(self):
        self.db.archive_wb_orders('orders', [
            self.order('start', date='2026-09-19T21:00:00Z'),
            self.order('end', date='2026-09-20T23:59:59.999999'),
            self.order('before', date='2026-09-19T23:59:59'),
            self.order('after', date='2026-09-20T21:00:00Z'),
            self.order('fbs', warehouseType='Склад продавца'),
            self.order('unknown', warehouseType=None),
            self.order('invalid', date=''),
            self.order('changed', date='2026-09-10T00:00:00', lastChangeDate='2026-09-20T12:00:00'),
        ])
        self.db.archive_wb_orders('fbs', [{'id': 1, 'createdAt': '2026-09-20T12:00:00Z'}])
        result = self.get()
        orders = result['groups'][0]['orders']
        self.assertEqual([row['lookup_number'] for row in orders], ['end', 'start'])
        self.assertEqual(result['order_count'], 2)
        self.assertEqual(result['unclassified_count'], 1)
        self.assertEqual(orders[0]['warehouse'], 'Казань')
        self.assertIn('Волгоградская', orders[0]['region'])

    def test_groups_by_seller_article_and_numbers_each_group_without_basket_dedup(self):
        rows = [self.order('b'), self.order('a'), self.order('c', supplierArticle='SKU-B', nmId=20)]
        self.db.archive_wb_orders('orders', rows)
        self.db.archive_wb_orders('orders', rows)  # A new snapshot is not another order.
        result = self.get()
        self.assertEqual(result['order_count'], 3)
        self.assertEqual(result['product_count'], 2)
        groups = {group['article']: group for group in result['groups']}
        self.assertEqual(groups['SKU-A']['title'], 'Кейкап')
        self.assertEqual(groups['SKU-A']['count'], 2)
        self.assertEqual([row['index'] for row in groups['SKU-A']['orders']], [1, 2])
        self.assertEqual([row['lookup_number'] for row in groups['SKU-A']['orders']], ['a', 'b'])
        self.assertEqual(groups['SKU-B']['orders'][0]['index'], 1)
        self.assertEqual(groups['SKU-A']['orders'][0]['number'], '12345678901234567890')
        # Clicking the exact srid finds one item even when all share gNumber.
        item = self.lookup.search(groups['SKU-A']['orders'][0]['lookup_number'])['items']
        self.assertEqual(len(item), 1)
        self.assertEqual(item[0]['srid'], 'a')

    def test_status_uses_events_after_selected_period_but_only_same_srid(self):
        self.db.archive_wb_orders('orders', [self.order('sold'), self.order('returned'),
            self.order('ordered'), self.order('canceled', isCancel=True, cancelDate='2026-09-22T00:00:00')])
        self.db.archive_wb_orders('sales', [
            {'saleID': 'S1', 'srid': 'sold', 'date': '2026-09-22T10:00:00'},
            {'saleID': 'S2', 'srid': 'returned', 'date': '2026-09-21T10:00:00'},
            {'saleID': 'R2', 'srid': 'returned', 'date': '2026-09-25T10:00:00'},
            {'saleID': 'R3', 'srid': 'unrelated', 'gNumber': '12345678901234567890', 'date': '2026-09-27T10:00:00'},
        ])
        statuses = {row['lookup_number']: row['status']['code'] for row in self.get()['groups'][0]['orders']}
        self.assertEqual(statuses, {'sold': 'sold', 'returned': 'returned', 'ordered': 'ordered', 'canceled': 'canceled'})
        self.assertIn('Возврат', self.lookup.search('returned')['items'][0]['status'])
        self.assertIn('неизвестен', self.lookup.search('ordered')['items'][0]['status'])

    def test_unknown_and_ambiguous_statuses_do_not_invent_delivery(self):
        self.assertEqual(report_status({}, [{'saleID': 'X1'}])['code'], 'ordered')
        self.assertEqual(report_status({'isCancel': True}, [])['code'], 'canceled')
        self.assertEqual(report_status({'isCancel': True}, [{'saleID': 'S1', 'date': '2026-09-25T10:00:00'}])['code'], 'unknown')
        # A subsequent new sale takes precedence over an earlier return.
        status = report_status({}, [{'saleID': 'R1', 'date': '2026-09-24T10:00:00'}, {'saleID': 'S2', 'date': '2026-09-25T10:00:00'}])
        self.assertEqual(status['code'], 'sold')

    def test_missing_catalog_keeps_historical_orders_and_large_group(self):
        rows = [self.order(str(i), nmId=99, supplierArticle='REMOVED', gNumber='') for i in range(505)]
        self.db.archive_wb_orders('orders', rows)
        result = self.get()
        group = result['groups'][0]
        self.assertEqual(result['order_count'], 505)
        self.assertEqual(group['article'], 'REMOVED')
        self.assertIn('отсутствует', group['title'])
        self.assertEqual(group['orders'][-1]['index'], 505)
        self.assertTrue(all(row['number'] == row['lookup_number'] for row in group['orders']))

    def test_sources_exclude_fbs_and_read_does_not_mutate_stock_or_call_wb(self):
        self.lookup.sources['fbs']['error'] = 'FBS offline'
        for source in ('orders', 'sales'):
            self.lookup.sources[source].update(started=True, updated_at='2026-09-28T12:00:00Z')
        before = self.db.conn.total_changes
        result = self.get()
        self.assertFalse(result['partial'])
        self.assertFalse(result['syncing'])
        self.lookup.sources['sales']['error'] = 'offline'
        self.assertTrue(self.get()['partial'])
        self.assertEqual(self.db.conn.total_changes, before)
        self.wb.assert_not_called()
        self.wb._json.assert_not_called()

    def test_date_validation_and_empty_results(self):
        for first, last in [('', ''), ('2026-02-30', '2026-03-01'), ('20260920', '2026-09-20'),
                            ('2026-09-21', '2026-09-20'), ('2026-09-20', '9999-12-31'),
                            ("' OR 1=1 --", '2026-09-20')]:
            with self.assertRaises(ValueError):
                self.get(first, last)
        result = self.get()
        self.assertEqual(result['groups'], [])
        self.assertTrue(result['partial'])
        self.assertTrue(result['syncing'])
