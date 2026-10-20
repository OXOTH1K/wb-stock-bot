"""Top ordered products for WB and Ozon, based on order creation dates."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import date, datetime, time as datetime_time, timedelta, timezone

from .wb_order_lookup import event_time, MSK

CACHE_SECONDS = 300


def _safe_error(exc: Exception) -> str:
    status = getattr(exc, 'status', None)
    suffix = f' (HTTP {status})' if isinstance(status, int) else ''
    return f'Не удалось загрузить заказы Ozon{suffix}. Данные WB доступны отдельно.'


def _product_key(row: dict) -> tuple[str, str]:
    sku = str(row.get('supplierArticle') or row.get('vendorCode') or '').strip()
    title = str(row.get('title') or row.get('subject') or '').strip()
    if not sku:
        nm_id = str(row.get('nmId') or '').strip()
        sku = f'WB-{nm_id}' if nm_id else ''
    return sku, title


def _aggregate(items: dict, sku: str, title: str, order_id: str, quantity: int) -> None:
    if not sku or not order_id or quantity <= 0:
        return
    current = items.setdefault(sku, {'sku': sku, 'title': title or sku,
                                     'quantity': 0, 'order_ids': set()})
    if title and (not current['title'] or current['title'] == sku):
        current['title'] = title
    current['quantity'] += quantity
    current['order_ids'].add(order_id)


def _rank(items: dict) -> list[dict]:
    rows = [{'sku': row['sku'], 'title': row['title'], 'quantity': row['quantity'],
             'orders': len(row['order_ids'])} for row in items.values()]
    rows.sort(key=lambda row: (-row['quantity'], -row['orders'], row['sku'].casefold()))
    return rows[:10]


class TopSalesAnalytics:
    def __init__(self, db, products, ozon_client=None, wb_sources=None,
                 cache_seconds: int = CACHE_SECONDS):
        self.db = db
        self.products = products
        self.ozon_client = ozon_client
        self.wb_sources = wb_sources or {}
        self.cache_seconds = cache_seconds
        self._cache: dict | None = None
        self._cached_at = 0.0
        self._lock = asyncio.Lock()

    def _wb_orders(self, start: date, end: date) -> tuple[list[dict], str]:
        source = self.wb_sources.get('orders', {})
        error = str(source.get('error') or '')
        updated_at = str(source.get('updated_at') or '')
        if not updated_at:
            return [], error or 'Архив заказов WB ещё загружается.'
        catalog = {int(pid): product for pid, product in self.products().items()}
        selected: dict[str, dict] = {}
        first = datetime.combine(start, datetime_time.min, MSK).astimezone(timezone.utc)
        after_last = datetime.combine(end + timedelta(days=1), datetime_time.min, MSK).astimezone(timezone.utc)
        for record_id, link_id, payload in self.db.conn.execute(
                "SELECT record_id, link_id, payload FROM wb_order_archive WHERE source='orders'"):
            try:
                row = json.loads(payload)
            except (TypeError, ValueError):
                continue
            when = event_time(str(row.get('date') or ''), report=True)
            if not when:
                continue
            try:
                timestamp = datetime.fromisoformat(when)
            except ValueError:
                continue
            if not first <= timestamp < after_last:
                continue
            sku, title = _product_key(row)
            try:
                nm_id = int(row.get('nmId') or 0)
            except (TypeError, ValueError):
                nm_id = 0
            product = catalog.get(nm_id)
            title = title or (product.title if product else '')
            try:
                quantity = int(row.get('quantity') or 1)
            except (TypeError, ValueError):
                quantity = 1
            # srid identifies a product order; gNumber is a basket and can
            # contain multiple different products.
            _aggregate(selected, sku, title, str(link_id or record_id), quantity)
        return _rank(selected), error

    async def _ozon_orders(self, start: date, end: date) -> tuple[list[tuple[str, dict]], str]:
        if self.ozon_client is None:
            return [], 'Интеграция Ozon не настроена.'
        start_at = datetime.combine(start, datetime_time.min, MSK).astimezone(timezone.utc)
        end_at = datetime.combine(end + timedelta(days=1), datetime_time.min, MSK).astimezone(timezone.utc)
        try:
            fbs, fbo = await asyncio.gather(
                self.ozon_client.list_order_postings('fbs', start_at, end_at),
                self.ozon_client.list_order_postings('fbo', start_at, end_at),
                return_exceptions=True,
            )
        except Exception as exc:  # defensive around alternative client implementations
            return [], _safe_error(exc)
        if isinstance(fbs, BaseException) and not isinstance(fbs, Exception):
            raise fbs
        if isinstance(fbo, BaseException) and not isinstance(fbo, Exception):
            raise fbo
        failed = [value for value in (fbs, fbo) if isinstance(value, Exception)]
        postings: list[tuple[str, dict]] = []
        for model, value in (('fbs', fbs), ('fbo', fbo)):
            if not isinstance(value, Exception):
                postings.extend((model, row) for row in value)
        return postings, (_safe_error(failed[0]) if failed else '')

    @staticmethod
    def _ozon_rank(postings: list[tuple[str, dict]], start: date, end: date) -> list[dict]:
        selected: dict[str, dict] = {}
        first = datetime.combine(start, datetime_time.min, MSK).astimezone(timezone.utc)
        after_last = datetime.combine(end + timedelta(days=1), datetime_time.min, MSK).astimezone(timezone.utc)
        for model, posting in postings:
            if not isinstance(posting, dict):
                continue
            order_date = event_time(str(posting.get('created_at') or posting.get('in_process_at') or ''))
            if not order_date:
                continue
            try:
                timestamp = datetime.fromisoformat(order_date)
            except ValueError:
                continue
            if not first <= timestamp < after_last:
                continue
            posting_id = str(posting.get('posting_number') or posting.get('order_number') or '').strip()
            if not posting_id:
                continue
            for product in posting.get('products') or []:
                if not isinstance(product, dict):
                    continue
                sku = str(product.get('offer_id') or product.get('product_offer_id') or '').strip()
                try:
                    quantity = int(product.get('quantity') or 0)
                except (TypeError, ValueError):
                    quantity = 0
                _aggregate(selected, sku,
                           str(product.get('name') or product.get('product_name') or sku),
                           f'{model}:{posting_id}', quantity)
        return _rank(selected)

    async def view(self, force: bool = False) -> dict:
        if not force and self._cache is not None and time.monotonic() - self._cached_at < self.cache_seconds:
            return self._cache
        async with self._lock:
            if not force and self._cache is not None and time.monotonic() - self._cached_at < self.cache_seconds:
                return self._cache
            today = datetime.now(MSK).date()
            week_start = today - timedelta(days=6)
            month_start = today - timedelta(days=29)
            month_wb, wb_error = self._wb_orders(month_start, today)
            ozon_postings, ozon_error = await self._ozon_orders(month_start, today)
            month = {'date_from': month_start.isoformat(), 'date_to': today.isoformat(),
                     'wb': {'items': month_wb, 'error': wb_error, 'ready': bool(self.wb_sources.get('orders', {}).get('updated_at'))},
                     'ozon': {'items': self._ozon_rank(ozon_postings, month_start, today), 'error': ozon_error, 'ready': self.ozon_client is not None}}
            week_wb, week_error = self._wb_orders(week_start, today)
            self._cache = {
                'week': {'date_from': week_start.isoformat(), 'date_to': today.isoformat(),
                         'wb': {'items': week_wb, 'error': week_error, 'ready': month['wb']['ready']},
                         'ozon': {'items': self._ozon_rank(ozon_postings, week_start, today), 'error': ozon_error, 'ready': self.ozon_client is not None}},
                'month': month,
                'updated_at': datetime.now(timezone.utc).isoformat(),
            }
            self._cached_at = time.monotonic()
            return self._cache
