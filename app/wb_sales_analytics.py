"""Read-only, on-demand finance reports. Never changes stock or seller balances."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from .wb_order_lookup import MSK

log = logging.getLogger(__name__)
URL = 'https://finance-api.wildberries.ru/api/finance/v1/sales-reports/detailed'
FIELDS = ['rrdId', 'rrDate', 'nmId', 'vendorCode', 'title', 'currency', 'docTypeName',
          'sellerOperName', 'quantity', 'returnAmount', 'forPay']


def period(start: str, end: str) -> tuple[date, date]:
    try:
        a, b = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        raise ValueError('Укажите даты в формате ГГГГ-ММ-ДД.') from None
    if a.isoformat() != start or b.isoformat() != end:
        raise ValueError('Укажите даты в формате ГГГГ-ММ-ДД.')
    if a > b or (b - a).days >= 366:
        raise ValueError('Начало должно быть не позже окончания; максимум 366 дней.')
    if a < date(2024, 1, 29) or b > datetime.now(MSK).date():
        raise ValueError('Период доступен с 29.01.2024 до сегодняшнего дня (МСК).')
    return a, b


def money(row, field):
    try:
        value = Decimal(str(row.get(field) or '0'))
        if not value.is_finite() or abs(value) > Decimal('1e12'):
            raise ValueError()
        return value
    except (InvalidOperation, ValueError):
        raise ValueError(f'Некорректная сумма WB в поле {field}.') from None


def amounts(row):
    """forPay includes WB commission/payment services. Other expenses are excluded."""
    doc = row.get('docTypeName', '')
    sign = -1 if doc == 'Возврат' else 1
    if row.get('forPay') is None:
        raise ValueError('В финансовом отчёте WB отсутствует сумма к перечислению.')
    base = money(row, 'forPay')
    if base and doc not in {'Продажа', 'Возврат'}:
        raise ValueError('WB вернул начисление без известного типа документа.')
    credit = sign * base
    quantity = money(row, 'quantity')
    if quantity < 0 or quantity != quantity.to_integral_value():
        raise ValueError('Некорректное количество в финансовом отчёте WB.')
    operation = str(row.get('sellerOperName') or '').strip()
    return_amount = money(row, 'returnAmount') if row.get('returnAmount') is not None else Decimal(0)
    if return_amount < 0 or return_amount != return_amount.to_integral_value():
        raise ValueError('Некорректное количество возвратов в финансовом отчёте WB.')
    # WB may report a return in either the operation/quantity pair or the
    # explicit returnAmount field. Prefer the larger value so a zero/missing
    # legacy field cannot hide a return from the daily totals.
    returned = max(int(return_amount), int(quantity) if operation == 'Возврат' or doc == 'Возврат' else 0)
    sold = int(quantity) if operation == 'Продажа' and doc != 'Возврат' and not returned else 0
    return sold, returned, credit


def rub(value):
    return str(value.quantize(Decimal('.01'), rounding=ROUND_HALF_UP))


def aggregate(rows, start, end, products):
    result = {}
    seen = set()
    for row in rows:
        rid = int(row['rrdId'])
        if rid in seen:
            raise ValueError('Повтор строки в отчёте WB.')
        seen.add(rid)
        day = str(row.get('rrDate') or '')[:10]
        date.fromisoformat(day)  # Invalid dates must not silently turn into zero revenue.
        if not start <= day <= end:
            continue
        if str(row.get('currency') or '').upper() not in {'RUB', 'РУБ', 'РУБ.'}:
            raise ValueError('График поддерживает только отчёты в рублях; другая валюта не суммируется.')
        nm_id = int(row.get('nmId') or 0)
        product = products.get(nm_id)
        sku = str(row.get('vendorCode') or '').strip()
        if not sku or sku == '0':
            sku = product.vendor_code if product else (f'WB #{nm_id}' if nm_id else '')
        if not sku:
            continue  # Account-wide costs do not belong to a product chart.
        title = str(row.get('title') or (product.title if product else '') or sku)
        group = result.setdefault(sku, {'sku': sku, 'title': title, 'days': {}})
        values = group['days'].setdefault(day, [0, 0, Decimal(0)])
        for i, value in enumerate(amounts(row)):
            values[i] += value
    for group in result.values():
        group['days'] = {day: [v[0], v[1], str(v[2])] for day, v in group['days'].items()}
    return result


class WBSalesAnalytics:
    def __init__(self, wb, db, products, token=''):
        self.wb, self.db, self.products, self.token = wb, db, products, token
        self.queue = asyncio.Queue(maxsize=8)
        self.pending = set()
        self.errors = {}
        self.next_request = 0.0
        self.db.conn.execute('CREATE TABLE IF NOT EXISTS crm_finance_cache (period TEXT PRIMARY KEY, updated REAL NOT NULL, payload TEXT NOT NULL)')
        self.db.conn.commit()

    def view(self, start, end, sku):
        a, b = period(start, end)
        if len(sku) > 200 or any(ord(c) < 32 for c in sku):
            raise ValueError('Некорректный артикул.')
        key = start + ':' + end
        record = self.db.conn.execute('SELECT updated, payload FROM crm_finance_cache WHERE period=?', (key,)).fetchone()
        now = time.time()
        error, retry_at = self.errors.get(key, ('', 0))
        if (not record or now - record[0] > 3600) and key not in self.pending and now >= retry_at:
            if self.queue.full():
                error = 'Очередь загрузки занята. Повторите запрос позже.'
            else:
                self.queue.put_nowait((key, start, end))
                self.pending.add(key)
        groups = json.loads(record[1]) if record else {}
        catalog = {p.vendor_code: p.title for p in self.products().values()}
        catalog.update({k: v['title'] for k, v in groups.items() if k})
        selected = groups.get(sku, {}).get('days', {}) if sku else {}
        points = []
        totals = [0, 0, Decimal(0)]
        for offset in range((b - a).days + 1):
            day = (a + timedelta(days=offset)).isoformat()
            values = selected.get(day, [0, 0, '0'])
            sales, returns, credit = values[0], values[1], Decimal(values[2])
            points.append({'date': day, 'sales': sales, 'returns': returns, 'net': rub(credit)})
            for i, value in enumerate((sales, returns, credit)):
                totals[i] += value
        return {'sku': sku, 'products': [{'sku': k, 'title': v} for k, v in sorted(catalog.items())],
                'date_from': start, 'date_to': end, 'points': points if record else [],
                'ready': bool(record), 'syncing': key in self.pending, 'error': error,
                'stale': bool(record and now - record[0] > 3600),
                'updated_at': datetime.fromtimestamp(record[0], MSK).isoformat() if record else '',
                'has_rows': bool(selected),
                'totals': {'sales': totals[0], 'returns': totals[1], 'net': rub(totals[2])}}

    async def refresh(self, start, end):
        cursor, rows = 0, []
        while True:
            delay = self.next_request - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self.next_request = time.monotonic() + 61
            page = await self.wb._json('POST', URL, token=self.token or None,
                json={'dateFrom': start, 'dateTo': end + 'T23:59:59.999', 'period': 'weekly',
                      'limit': 10000, 'rrdId': cursor, 'fields': FIELDS})
            if page is None or page == []:
                break
            if not isinstance(page, list) or any(not isinstance(r, dict) for r in page):
                raise ValueError('Некорректный ответ финансового API WB.')
            ids = [int(r['rrdId']) for r in page]
            if min(ids) <= cursor or ids != sorted(set(ids)):
                raise ValueError('Пагинация финансового отчёта WB не продвигается.')
            cursor = ids[-1]
            rows.extend(page)
            if len(rows) > 100000:
                raise ValueError('Слишком большой отчёт. Выберите меньший период.')
        payload = await asyncio.to_thread(aggregate, rows, start, end, dict(self.products()))
        with self.db.conn:
            self.db.conn.execute('INSERT OR REPLACE INTO crm_finance_cache VALUES (?, ?, ?)',
                                 (start + ':' + end, time.time(), json.dumps(payload, ensure_ascii=False)))
            self.db.conn.execute('DELETE FROM crm_finance_cache WHERE period NOT IN (SELECT period FROM crm_finance_cache ORDER BY updated DESC LIMIT 12)')

    async def loop(self):
        while True:
            key, start, end = await self.queue.get()
            try:
                await self.refresh(start, end)
                self.errors.pop(key, None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                status = getattr(exc, 'status', None)
                if status in {401, 403}:
                    message = 'Нет доступа к финансовым отчётам WB. Нужен токен категории «Финансы» (WB_FINANCE_TOKEN или WB_TOKEN).'
                elif isinstance(exc, ValueError):
                    message = 'Не удалось проверить данные финансового отчёта WB; расчёт не обновлён.'
                else:
                    message = 'Не удалось загрузить финансовый отчёт WB. Будет повторная попытка.'
                # Never log HTTP bodies/credentials from finance responses.
                log.warning('WB finance refresh failed (%s, HTTP %s)', type(exc).__name__, status)
                self.errors[key] = (message, time.time() + 300)
                if len(self.errors) > 32:
                    self.errors.pop(next(iter(self.errors)))
            finally:
                self.pending.discard(key)
                self.queue.task_done()
