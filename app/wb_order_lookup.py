"""Read-only WB order archive and CRM projection; never changes stock or orders."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, time, timedelta, timezone

from .db import StateDB
from .models import Product
from .wb_client import WildberriesClient

log = logging.getLogger(__name__)
MSK = timezone(timedelta(hours=3))
SUPPLIER = {
    "new": "Новое сборочное задание", "confirm": "На сборке",
    "complete": "Передано в доставку", "cancel": "Отменено продавцом",
    "cancel_carrier": "Отменено перевозчиком",
}
WB_STATUS = {
    "waiting": "Ожидает обработки WB", "sorted": "Отсортирован WB",
    "sold": "Продан", "canceled": "Отменён", "canceled_by_client": "Отменён покупателем",
    "declined_by_client": "Отказ покупателя", "defect": "Брак",
    "ready_for_pickup": "Готов к выдаче", "delivered": "Получен покупателем",
}
SOURCES = {"fbs": "Сборочные задания FBS", "orders": "Отчёт о заказах WB", "sales": "Продажи и возвраты WB"}
COVERAGE = (
    "Поиск по ID сборочного задания FBS, rid/srid или gNumber из отчёта WB. "
    "При первом запуске загружаются FBS за 30 дней и отчёты WB за 90 дней; "
    "накопленный архив хранится дальше. Отчёты WB могут запаздывать на 30 минут "
    "и не включать заказы без подтверждённой оплаты. Номер из приложения покупателя "
    "может отличаться. Полный маршрут и точное местоположение WB не предоставляет."
)


def event_time(value: str, *, report: bool = False) -> str:
    """Reports without an offset are Moscow time, not the server/browser timezone."""
    if not value or value.startswith("0001-"):
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=MSK if report else timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except ValueError:
        return ""


def report_status(report: dict, sales: list[dict]) -> dict:
    """The latest confirmed report event, never an inferred delivery stage."""
    events = []
    if report.get("isCancel") is True:
        events.append({"code": "canceled", "label": "Отменён",
                       "at": event_time(report.get("cancelDate", ""), report=True)})
    for sale in sales:
        prefix = str(sale.get("saleID") or "")[:1]
        if prefix not in {"S", "R"}:
            continue
        events.append({"code": "returned" if prefix == "R" else "sold",
                       "label": "Возврат" if prefix == "R" else "Продан",
                       "at": event_time(sale.get("date", ""), report=True)})
    if events:
        # A confirmed event without a date cannot be ordered reliably.
        if len(events) > 1 and any(not event["at"] for event in events):
            return {"code": "unknown", "label": "Есть события без даты — откройте подробности", "at": ""}
        priority = {"sold": 0, "canceled": 1, "returned": 2}
        return max(events, key=lambda event: (event["at"], priority[event["code"]]))
    return {"code": "ordered", "label": "Заказан · статус доставки неизвестен", "at": ""}


class WBOrderLookup:
    def __init__(self, wb: WildberriesClient, db: StateDB):
        self.wb, self.db = wb, db
        self.sources = {
            source: {"name": name, "syncing": False, "error": "",
                     "updated_at": db.get_meta(f"wb_lookup_{source}_at"), "started": False}
            for source, name in SOURCES.items()
        }

    async def loop(self) -> None:
        # Independent workers: a Statistics outage cannot stop FBS indexing.
        tasks = [asyncio.create_task(self._worker(source)) for source in SOURCES]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _worker(self, source: str) -> None:
        while True:
            state = self.sources[source]
            state.update(syncing=True, started=True, error="")
            try:
                if source == "fbs":
                    await self.refresh_fbs()
                else:
                    await self.refresh_report(source)
                state["updated_at"] = datetime.now(timezone.utc).isoformat()
                self.db.set_meta(f"wb_lookup_{source}_at", state["updated_at"])
            except Exception as exc:
                log.exception("WB order archive refresh failed: %s", source)
                code = getattr(exc, "status", None)
                state["error"] = (
                    f"Источник недоступен (HTTP {code}). Проверьте доступ токена WB к категории «Статистика»."
                    if code in {401, 402, 403} and source != "fbs"
                    else f"Не удалось обновить источник ({type(exc).__name__}" + (f", HTTP {code}" if code else "") + "). Будет повторная попытка."
                )
            finally:
                state["syncing"] = False
            await asyncio.sleep(300 if source == "fbs" or state["error"] else 1800)

    async def refresh_fbs(self) -> None:
        now = datetime.now(timezone.utc)
        cursor, seen, ids = 0, set(), set()
        while True:
            data = await self.wb._json(
                "GET", f"{self.wb.MARKETPLACE_BASE}/api/v3/orders",
                params={"limit": 1000, "next": cursor,
                        "dateFrom": int((now - timedelta(days=30)).timestamp()),
                        "dateTo": int(now.timestamp())},
            )
            if not isinstance(data, dict) or not isinstance(data.get("orders"), list):
                raise ValueError("Invalid FBS orders response")
            rows = data["orders"]
            self.db.archive_wb_orders("fbs", rows)
            ids.update(int(row["id"]) for row in rows)
            nxt = int(data.get("next") or 0)
            if not rows or not nxt:
                break
            if nxt == cursor or nxt in seen:
                raise ValueError("FBS pagination did not advance")
            seen.add(nxt)
            cursor = nxt
            await asyncio.sleep(0.25)
        # Also follow older unfinished orders already stored in the archive.
        for (order_id,) in self.db.conn.execute(
            "SELECT order_id FROM crm_order_status WHERE wb_status NOT IN "
            "('sold','canceled','canceled_by_client','declined_by_client','defect','delivered')"
        ):
            ids.add(int(order_id))
        ids = sorted(ids)
        for start in range(0, len(ids), 1000):
            await asyncio.sleep(0.25)
            await self.refresh_statuses(ids[start:start + 1000])

    async def refresh_statuses(self, ids: list[int]) -> None:
        data = await self.wb._json(
            "POST", f"{self.wb.MARKETPLACE_BASE}/api/v3/orders/status", json={"orders": ids}
        )
        if not isinstance(data, dict) or not isinstance(data.get("orders"), list):
            raise ValueError("Invalid FBS status response")
        for row in data["orders"]:
            if int(row["id"]) in ids:
                self.db.set_order_runtime_status(row["id"], row.get("supplierStatus", ""), row.get("wbStatus", ""))

    async def refresh_report(self, source: str) -> None:
        now = datetime.now(MSK)
        earliest = now - timedelta(days=90)
        previous = self.db.get_meta(f"wb_lookup_{source}_cursor")
        since = earliest
        if previous:
            parsed = event_time(previous, report=True)
            if parsed:
                since = max(earliest, datetime.fromisoformat(parsed) - timedelta(days=1))
        cursor = since.astimezone(MSK).isoformat()
        while True:
            rows = await self.wb._json(
                "GET", f"https://statistics-api.wildberries.ru/api/v1/supplier/{source}",
                params={"dateFrom": cursor, "flag": 0},
            )
            if not isinstance(rows, list):
                raise ValueError("Invalid Statistics response")
            self.db.archive_wb_orders(source, rows)
            if not rows:
                break
            nxt = str(rows[-1].get("lastChangeDate") or "")
            if not event_time(nxt, report=True):
                raise ValueError("Missing Statistics pagination cursor")
            if event_time(nxt, report=True) < event_time(cursor, report=True):
                raise ValueError("Statistics pagination moved backwards")
            if event_time(nxt, report=True) == event_time(cursor, report=True):
                # dateFrom is inclusive: the final timestamp can repeat.
                if len(rows) >= 80000:
                    raise ValueError("Statistics pagination did not advance")
                break
            self.db.set_meta(f"wb_lookup_{source}_cursor", nxt)
            cursor = nxt
            await asyncio.sleep(61)  # WB Statistics allows one request/minute/method.

    def search(self, number: str) -> dict:
        rows = self.db.conn.execute(
            "SELECT source, record_id, link_id, payload, observed_at FROM wb_order_archive "
            "WHERE record_id = ? OR link_id = ? OR group_number = ?",
            (number, number, number),
        ).fetchall()
        records = {(r[0], r[1]): r for r in rows}
        # Join FBS rid to Statistics srid. gNumber may contain several items;
        # keep their separate srids, never collapse the entire basket into one item.
        links = {r[2] for r in rows if r[2]}
        for link in links:
            for row in self.db.conn.execute(
                "SELECT source, record_id, link_id, payload, observed_at FROM wb_order_archive WHERE link_id = ?", (link,)
            ):
                records[(row[0], row[1])] = row
        groups: dict[str, list] = {}
        for row in records.values():
            groups.setdefault(row[2] or f"{row[0]}:{row[1]}", []).append(row)
        items = [self._card(rows) for rows in groups.values()]
        # Older bot versions only saved the numeric FBS ID and local workflow state.
        if not items and number.isascii() and number.isdecimal() and len(number) <= 18:
            exists = self.db.conn.execute("SELECT 1 FROM sqlite_master WHERE name='order_state'").fetchone()
            if exists:
                row = self.db.conn.execute(
                    "SELECT article, nm_id, supply_id, first_seen_at, status FROM order_state WHERE order_id=?", (int(number),)
                ).fetchone()
                if row:
                    raw = {"id": int(number), "article": row[0], "nmId": row[1], "supplyId": row[2],
                           "localFirstSeenAt": row[3], "localWorkflowStatus": row[4]}
                    items = [self._card([("fbs", number, "", json.dumps(raw), row[3])])]
        sources = [dict(value, key=key) for key, value in self.sources.items()]
        return {"number": number, "items": items, "sources": sources, "coverage": COVERAGE,
                "syncing": any(s["syncing"] or not s["started"] for s in sources),
                "partial": any(s["error"] or not s["updated_at"] for s in sources)}

    def fbw_orders(self, date_from: str, date_to: str, products: dict[int, Product]) -> dict:
        try:
            first, last = date.fromisoformat(date_from), date.fromisoformat(date_to)
            if first.isoformat() != date_from or last.isoformat() != date_to:
                raise ValueError
            if first > last:
                raise ValueError
            start = datetime.combine(first, time.min, MSK).astimezone(timezone.utc).isoformat()
            end = datetime.combine(last + timedelta(days=1), time.min, MSK).astimezone(timezone.utc).isoformat()
        except (ValueError, OverflowError):
            raise ValueError("Укажите период в формате ГГГГ-ММ-ДД: дата начала не позже даты окончания.") from None

        selected = {}
        unclassified = 0
        for link, payload in self.db.conn.execute(
            "SELECT link_id, payload FROM wb_order_archive WHERE source='orders'"
        ):
            row = json.loads(payload)
            created = event_time(row.get("date", ""), report=True)
            if not link or not created or not start <= created < end:
                continue
            model = row.get("warehouseType")
            if model != "Склад WB":
                if model != "Склад продавца":
                    unclassified += 1
                continue
            selected[link] = (row, created)

        # Join by srid, not basket number; sales after the chosen date range
        # still describe the current known state of the selected orders.
        sales_by_order: dict[str, list[dict]] = {}
        links = list(selected)
        for offset in range(0, len(links), 500):
            batch = links[offset:offset + 500]
            placeholders = ",".join("?" for _ in batch)
            for link, payload in self.db.conn.execute(
                f"SELECT link_id, payload FROM wb_order_archive WHERE source='sales' AND link_id IN ({placeholders})", batch
            ):
                sales_by_order.setdefault(link, []).append(json.loads(payload))

        groups = {}
        for link, (row, created) in selected.items():
            nm_id = int(row.get("nmId") or 0)
            product = products.get(nm_id)
            article = str(row.get("supplierArticle") or (product.vendor_code if product else "") or "")
            key = article or (f"WB-{nm_id}" if nm_id else f"unknown:{link}")
            group = groups.setdefault(key, {"key": key, "article": article or key,
                                           "titles": set(), "orders": []})
            if product and product.title:
                group["titles"].add(product.title)
            group["orders"].append({
                "number": str(row.get("gNumber") or link), "lookup_number": link,
                "created_at": created, "warehouse": str(row.get("warehouseName") or ""),
                "region": ", ".join(str(row[k]) for k in ("countryName", "oblastOkrugName", "regionName") if row.get(k)),
                "status": report_status(row, sales_by_order.get(link, [])),
            })
        result = []
        for group in groups.values():
            group["title"] = " / ".join(sorted(group.pop("titles"))) or "Название отсутствует в каталоге"
            group["orders"].sort(key=lambda order: order["lookup_number"])
            group["orders"].sort(key=lambda order: order["created_at"], reverse=True)
            for index, order in enumerate(group["orders"], 1):
                order["index"] = index
            group["count"] = len(group["orders"])
            result.append(group)
        result.sort(key=lambda group: (group["title"].casefold(), group["article"].casefold()))
        sources = [dict(self.sources[key], key=key) for key in ("orders", "sales")]
        return {
            "date_from": date_from, "date_to": date_to, "groups": result,
            "order_count": len(selected), "product_count": len(result),
            "unclassified_count": unclassified, "sources": sources,
            "syncing": any(s["syncing"] or not s["started"] for s in sources),
            "partial": any(s["error"] or not s["updated_at"] for s in sources),
        }

    def _card(self, rows: list) -> dict:
        raw = [{"source": r[0], "data": json.loads(r[3]), "observed_at": r[4]} for r in rows]
        fbs = next((r["data"] for r in raw if r["source"] == "fbs"), {})
        report = next((r["data"] for r in raw if r["source"] == "orders"), {})
        sales = sorted((r["data"] for r in raw if r["source"] == "sales"), key=lambda r: event_time(r.get("date", ""), report=True))
        info = report or (sales[-1] if sales else {})
        events = []

        def add(at, title, source, observed=False, report_time=False):
            at = event_time(str(at or ""), report=report_time)
            if at:
                events.append({"at": at, "title": title, "source": source, "observed": observed})

        created = event_time(fbs.get("createdAt", "")) or event_time(report.get("date", ""), report=True)
        add(created, "Заказ создан", "WB")
        add(fbs.get("localFirstSeenAt"), "Бот впервые увидел заказ", "Локальный архив", True)
        if report.get("isCancel"):
            add(report.get("cancelDate"), "Заказ отменён", "Отчёт WB", report_time=True)
        for sale in sales:
            kind = "Возврат" if str(sale.get("saleID", "")).startswith("R") else "Продажа"
            add(sale.get("date"), kind + " в отчёте WB", "Отчёт WB", report_time=True)
        status, status_at = "Текущий статус доставки недоступен", None
        if not fbs and report.get("warehouseType") == "Склад WB":
            status = report_status(report, sales)["label"] + " (по отчётам WB)"
        report_events = [e for e in events if e["source"] == "Отчёт WB"]
        last_report_event = max(report_events, key=lambda e: e["at"]) if report_events else None
        if fbs.get("id"):
            order_id = int(fbs["id"])
            for supplier, wb, observed in self.db.conn.execute(
                "SELECT supplier_status, wb_status, observed_at FROM wb_order_status_history WHERE order_id=? ORDER BY id", (order_id,)
            ):
                label = self._status(supplier, wb)
                add(observed, label, "Статус WB, зафиксирован ботом", True)
            current = self.db.conn.execute(
                "SELECT supplier_status, wb_status, updated_at FROM crm_order_status WHERE order_id=?", (order_id,)
            ).fetchone()
            if current:
                status, status_at = self._status(current[0], current[1]), current[2]
        # Reports describe confirmed events, not a live location; display separately.
        return {
            "order_id": str(fbs.get("id") or ""), "srid": fbs.get("rid") or info.get("srid") or "",
            "group_number": info.get("gNumber") or "", "article": fbs.get("article") or info.get("supplierArticle") or "",
            "nm_id": fbs.get("nmId") or info.get("nmId"), "created_at": created,
            "model": "FBS" if fbs else (info.get("warehouseType") or "Отчёт WB"),
            "status": status, "status_at": status_at,
            "last_report_event": last_report_event,
            "warehouse": info.get("warehouseName") or str(fbs.get("warehouseId") or ""),
            "destination": ", ".join(str(info[k]) for k in ("countryName", "oblastOkrugName", "regionName") if info.get(k)),
            "supply_id": fbs.get("supplyId") or "", "events": sorted(events, key=lambda e: e["at"]),
            "raw": raw,
        }

    @staticmethod
    def _status(supplier: str, wb: str) -> str:
        return " · ".join(x for x in (SUPPLIER.get(supplier, supplier), WB_STATUS.get(wb, wb)) if x) or "Статус не указан WB"
