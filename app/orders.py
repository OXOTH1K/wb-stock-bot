from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import Settings
from .db import StateDB
from .inventory_sync import SharedStockSync
from .telegram import TelegramBot
from .wb_client import WildberriesClient

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FBSOrder:
    id: int
    article: str
    nm_id: int
    warehouse_id: int
    created_at: str
    cargo_type: int
    cross_border_type: int
    offices: tuple[str, ...] = ()


@dataclass(frozen=True)
class FBSSupply:
    id: str
    name: str
    done: bool
    cargo_type: int
    cross_border_type: int
    created_at: str = ""


RecoveryHandler = Callable[[datetime, datetime], Awaitable[None]]


class OrderMonitor:
    def __init__(
        self,
        settings: Settings,
        wb: WildberriesClient,
        tg: TelegramBot,
        db: StateDB,
        warehouse_id: int,
        stock_sync: SharedStockSync | None = None,
    ):
        self.settings = settings
        self.wb = wb
        self.tg = tg
        self.db = db
        self.warehouse_id = int(warehouse_id)
        self.stock_sync = stock_sync
        self._lock = asyncio.Lock()
        self.current_new_orders: dict[int, FBSOrder] = {}
        self.current_supply_orders: dict[int, str] = {}
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self.db.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS order_state (
                order_id INTEGER PRIMARY KEY,
                article TEXT NOT NULL DEFAULT '',
                nm_id INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                supply_id TEXT,
                first_seen_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.db.conn.commit()

    def _state(self, order_id: int) -> tuple[str, str | None] | None:
        row = self.db.conn.execute(
            "SELECT status, supply_id FROM order_state WHERE order_id = ?", (int(order_id),)
        ).fetchone()
        return None if row is None else (str(row[0]), None if row[1] is None else str(row[1]))

    def _remember(self, order: FBSOrder) -> bool:
        now = datetime.now().astimezone().isoformat()
        with self.db.conn:
            cur = self.db.conn.execute(
                """
                INSERT OR IGNORE INTO order_state(order_id, article, nm_id, status, supply_id, first_seen_at, updated_at)
                VALUES (?, ?, ?, 'notified', NULL, ?, ?)
                """,
                (order.id, order.article, order.nm_id, now, now),
            )
        return cur.rowcount == 1

    def _set_state(self, order_id: int, status: str, supply_id: str | None = None) -> None:
        now = datetime.now().astimezone().isoformat()
        with self.db.conn:
            self.db.conn.execute(
                """
                INSERT INTO order_state(order_id, article, nm_id, status, supply_id, first_seen_at, updated_at)
                VALUES (?, '', 0, ?, ?, ?, ?)
                ON CONFLICT(order_id) DO UPDATE SET status=excluded.status, supply_id=excluded.supply_id, updated_at=excluded.updated_at
                """,
                (int(order_id), status, supply_id, now, now),
            )

    def _parse_order(self, row: dict) -> FBSOrder:
        return FBSOrder(
            id=int(row["id"]),
            article=str(row.get("article") or ""),
            nm_id=int(row.get("nmId") or 0),
            warehouse_id=int(row.get("warehouseId") or 0),
            created_at=str(row.get("createdAt") or ""),
            cargo_type=int(row.get("cargoType") or 0),
            cross_border_type=int(row.get("crossBorderType") or 0),
            offices=tuple(str(x) for x in (row.get("offices") or [])),
        )

    async def _new_orders(self) -> list[FBSOrder]:
        data = await self.wb._json(
            "GET", f"{self.wb.MARKETPLACE_BASE}/api/v3/orders/new"
        )
        return [
            self._parse_order(row)
            for row in (data or {}).get("orders", [])
            if int(row.get("warehouseId") or 0) == self.warehouse_id
        ]

    async def _orders_since(self, since: datetime) -> list[dict]:
        now = datetime.now(timezone.utc)
        since = since.astimezone(timezone.utc)
        earliest = now - timedelta(days=30)
        if since < earliest:
            since = earliest

        result: list[dict] = []
        cursor = 0
        seen: set[int] = set()
        while True:
            data = await self.wb._json(
                "GET",
                f"{self.wb.MARKETPLACE_BASE}/api/v3/orders",
                params={
                    "limit": 1000,
                    "next": cursor,
                    "dateFrom": int(since.timestamp()),
                    "dateTo": int(now.timestamp()),
                },
            )
            rows = (data or {}).get("orders", [])
            result.extend(
                row
                for row in rows
                if int(row.get("warehouseId") or 0) == self.warehouse_id
            )
            nxt = int((data or {}).get("next") or 0)
            if not rows or len(rows) < 1000 or nxt == 0 or nxt == cursor or nxt in seen:
                break
            seen.add(nxt)
            cursor = nxt
        return result

    async def _order_status_details(
        self, order_ids: list[int]
    ) -> dict[int, tuple[str, str]]:
        statuses: dict[int, tuple[str, str]] = {}
        ids = list(dict.fromkeys(int(x) for x in order_ids))
        for start in range(0, len(ids), 1000):
            chunk = ids[start : start + 1000]
            data = await self.wb._json(
                "POST",
                f"{self.wb.MARKETPLACE_BASE}/api/v3/orders/status",
                json={"orders": chunk},
            )
            for row in (data or {}).get("orders", []):
                statuses[int(row["id"])] = (
                    str(row.get("supplierStatus") or ""),
                    str(row.get("wbStatus") or ""),
                )
        return statuses

    async def _order_statuses(self, order_ids: list[int]) -> dict[int, str]:
        details = await self._order_status_details(order_ids)
        return {order_id: status[0] for order_id, status in details.items()}

    async def _sync_crm_order_statuses(
        self,
        orders: list[FBSOrder],
        extra_order_ids: set[int] | None = None,
    ) -> dict[int, tuple[str, str]]:
        current_ids = {order.id for order in orders}
        rows = self.db.list_order_state(limit=1000)

        candidates: set[int] = set(current_ids)
        if extra_order_ids:
            candidates.update(int(order_id) for order_id in extra_order_ids)
        for row in rows:
            order_id = int(row["order_id"])
            supplier_status = row.get("supplier_status")
            local_status = str(row.get("status") or "")
            if supplier_status in {"new", "confirm"}:
                candidates.add(order_id)
            elif supplier_status is None and local_status in {
                "notified",
                "assigned",
                "skipped",
            }:
                candidates.add(order_id)

        if not candidates:
            return {}

        details = await self._order_status_details(sorted(candidates))
        for order_id, (supplier_status, wb_status) in details.items():
            self.db.set_order_runtime_status(
                order_id, supplier_status, wb_status
            )
        return details

    async def _supplies(self) -> list[FBSSupply]:
        result: list[FBSSupply] = []
        cursor = 0
        seen: set[int] = set()
        while True:
            data = await self.wb._json("GET", f"{self.wb.MARKETPLACE_BASE}/api/v3/supplies", params={"limit": 1000, "next": cursor})
            rows = (data or {}).get("supplies", [])
            for row in rows:
                if row.get("done"):
                    continue
                result.append(FBSSupply(
                    id=str(row["id"]), name=str(row.get("name") or row["id"]), done=False,
                    cargo_type=int(row.get("cargoType") or 0), cross_border_type=int(row.get("crossBorderType") or 0),
                    created_at=str(row.get("createdAt") or ""),
                ))
            nxt = int((data or {}).get("next") or 0)
            if not rows or len(rows) < 1000 or nxt == 0:
                break
            if nxt == cursor or nxt in seen:
                break
            seen.add(nxt)
            cursor = nxt
        return result

    async def _supply_memberships(
        self, supplies: list[FBSSupply]
    ) -> dict[int, str]:
        memberships: dict[int, str] = {}
        for supply in supplies:
            data = await self.wb._json(
                "GET",
                (
                    f"{self.wb.MARKETPLACE_BASE}/api/marketplace/v3/"
                    f"supplies/{supply.id}/order-ids"
                ),
            )
            for order_id in (data or {}).get("orderIds", []):
                memberships[int(order_id)] = supply.id
        return memberships

    def _eligible(self, order: FBSOrder, supplies: list[FBSSupply]) -> list[FBSSupply]:
        out = []
        for supply in supplies:
            if supply.done:
                continue
            if supply.cargo_type == 0 or (
                supply.cargo_type == order.cargo_type and supply.cross_border_type == order.cross_border_type
            ):
                out.append(supply)
        return sorted(out, key=lambda x: (x.created_at, x.name.lower(), x.id), reverse=True)

    def _keyboard(self, order: FBSOrder, supplies: list[FBSSupply] | None) -> dict:
        rows = []
        if supplies is None:
            rows.append([{"text": "🔄 Обновить поставки", "callback_data": f"ordrefresh:{order.id}"}])
        else:
            eligible = self._eligible(order, supplies)
            if eligible:
                for supply in eligible[:20]:
                    name = supply.name if len(supply.name) <= 34 else supply.name[:33] + "…"
                    rows.append([{"text": f"📦 {name}", "callback_data": f"ordadd:{order.id}:{supply.id}"}])
            else:
                rows.append([{"text": "🆕 Создать поставку и добавить", "callback_data": f"ordnew:{order.id}"}])
        rows.append([{"text": "Не добавлять", "callback_data": f"ordskip:{order.id}"}])
        return {"inline_keyboard": rows}

    def _text(self, order: FBSOrder, supplies: list[FBSSupply] | None) -> str:
        lines = ["🟣 WB · Новый FBS-заказ", f"Артикул: {order.article or '—'}", f"Заказ: {order.id}"]
        if order.offices:
            lines.append(f"Направление WB: {', '.join(order.offices)}")
        if supplies is None:
            lines += ["", "⚠️ Не удалось получить список поставок."]
        else:
            n = len(self._eligible(order, supplies))
            lines += ["", (f"Подходящих поставок: {n}. Выберите нужную." if n > 1 else "Подходящая поставка найдена." if n == 1 else "Подходящей поставки нет — можно создать новую.")]
        return "\n".join(lines)

    async def refresh(self) -> None:
        orders = await self._new_orders()

        supplies: list[FBSSupply] = []
        memberships: dict[int, str] = {}
        membership_ok = False
        try:
            supplies = await self._supplies()
            memberships = await self._supply_memberships(supplies)
            membership_ok = True
        except Exception:
            log.exception("Could not refresh active supply membership")
            # Do not erase the last known active membership on a transient API error.
            memberships = dict(self.current_supply_orders)

        try:
            details = await self._sync_crm_order_statuses(
                orders, set(memberships)
            )
        except Exception:
            log.exception("Could not refresh CRM order statuses")
            details = {}

        # CRM shows only work that still belongs to our assembly stage.
        # wbStatus=waiting is the only WB-side state we keep. Refused, sold,
        # ready-for-pickup, sorted, defect and other downstream states are hidden
        # even if WB still returns the order inside a supply.
        current_by_id = {order.id: order for order in orders}
        active_memberships: dict[int, str] = {}
        for order_id, supply_id in memberships.items():
            status = details.get(order_id)
            if status is not None:
                supplier_status, wb_status = status
                if wb_status != "waiting":
                    continue
                if supplier_status not in {"new", "confirm"}:
                    continue

            state = self._state(order_id)
            order = current_by_id.get(order_id)
            if state is None and order is not None:
                if self.stock_sync is not None:
                    sku = self.stock_sync.resolve_wb_sku(
                        order.article, order.nm_id
                    )
                    if sku:
                        await self.stock_sync.apply_order(
                            "wb", str(order.id), {sku: 1}
                        )
                self._remember(order)
                state = self._state(order_id)
            if state is not None:
                self._set_state(order_id, "assigned", supply_id)
            active_memberships[order_id] = supply_id

        # Status=confirm is a fallback if WB temporarily omits an order from the
        # supply membership response. It is accepted only while wbStatus=waiting.
        for order_id, (supplier_status, wb_status) in details.items():
            if (
                supplier_status != "confirm"
                or wb_status != "waiting"
                or order_id in active_memberships
            ):
                continue
            state = self._state(order_id)
            if state is not None and state[1]:
                active_memberships[order_id] = state[1]

        self.current_supply_orders = active_memberships

        ready: list[FBSOrder] = []
        for order in orders:
            if order.id in active_memberships:
                continue
            status = details.get(order.id)
            if status is None:
                # On a temporary status API failure keep the /orders/new item
                # actionable rather than silently dropping it.
                ready.append(order)
                continue
            supplier_status, wb_status = status
            if supplier_status == "new" and wb_status == "waiting":
                ready.append(order)

        self.current_new_orders = {order.id: order for order in ready}
        for order in ready:
            self.db.set_order_runtime_status(order.id, "new", "waiting")

        unseen = [o for o in ready if self._state(o.id) is None]
        if unseen:
            if not supplies:
                try:
                    supplies = await self._supplies()
                except Exception:
                    log.exception("Could not load supplies")
            for order in sorted(unseen, key=lambda o: (o.created_at, o.id)):
                if self.stock_sync is not None:
                    sku = self.stock_sync.resolve_wb_sku(
                        order.article, order.nm_id
                    )
                    if sku:
                        await self.stock_sync.apply_order(
                            "wb", str(order.id), {sku: 1}
                        )
                await self.tg.broadcast(
                    self.settings.telegram_chat_ids,
                    self._text(order, supplies),
                    reply_markup=self._keyboard(order, supplies),
                )
                self._remember(order)

        if membership_ok:
            # Orders that were previously "assigned" but are no longer present in
            # any active supply are intentionally not kept in current_supply_orders.
            # CRM will therefore hide them unless they reappear as real new orders.
            pass

    async def audit_pending(self, chat_id: int) -> int:
        """Show currently ready-to-assemble orders that still need a decision."""
        await self.refresh()
        pending = []
        for order in self.current_new_orders.values():
            state = self._state(order.id)
            if state and state[0] in {"assigned", "skipped"}:
                continue
            pending.append(order)

        if not pending:
            return 0

        supplies = None
        try:
            supplies = await self._supplies()
        except Exception:
            log.exception("Could not load supplies for status audit")

        for order in sorted(pending, key=lambda o: (o.created_at, o.id)):
            await self.tg.send_message(
                chat_id,
                "🔎 /status: заказ требует решения\n\n" + self._text(order, supplies),
                reply_markup=self._keyboard(order, supplies),
            )
            if self._state(order.id) is None:
                self._remember(order)
        return len(pending)

    async def reconcile_since(self, since: datetime) -> tuple[int, int]:
        rows = await self._orders_since(since)
        unseen_rows = [row for row in rows if self._state(int(row["id"])) is None]
        if not unseen_rows:
            return 0, 0

        statuses = await self._order_statuses([int(row["id"]) for row in unseen_rows])
        new_orders = [
            self._parse_order(row)
            for row in unseen_rows
            if statuses.get(int(row["id"])) == "new"
        ]

        supplies = None
        if new_orders:
            try:
                supplies = await self._supplies()
            except Exception:
                log.exception("Could not load supplies during recovery")

        recovered_new = 0
        for order in sorted(new_orders, key=lambda o: (o.created_at, o.id)):
            if self.stock_sync is not None:
                sku = self.stock_sync.resolve_wb_sku(
                    order.article, order.nm_id
                )
                if sku:
                    await self.stock_sync.apply_order(
                        "wb", str(order.id), {sku: 1}
                    )
            await self.tg.broadcast(
                self.settings.telegram_chat_ids,
                "🧭 Заказ найден при сверке после восстановления связи\n\n"
                + self._text(order, supplies),
                reply_markup=self._keyboard(order, supplies),
            )
            self._remember(order)
            recovered_new += 1

        processed = []
        for row in unseen_rows:
            order_id = int(row["id"])
            status = statuses.get(order_id, "")
            if status == "new":
                continue
            self._set_state(order_id, f"recovered:{status or 'unknown'}", str(row.get("supplyId") or "") or None)
            processed.append((row, status or "unknown"))

        if processed:
            lines = [
                "🧭 Во время отсутствия связи были заказы, которые уже успели изменить статус:",
                "",
            ]
            for row, status in processed[:20]:
                lines.append(
                    f"• {row.get('article') or '—'} | заказ {row['id']} | статус: {status}"
                )
            if len(processed) > 20:
                lines.append(f"… ещё {len(processed) - 20}")
            lines.append("")
            lines.append("Они сохранены в истории бота и повторно как новые не появятся.")
            await self.tg.broadcast(
                self.settings.telegram_chat_ids,
                "\n".join(lines),
            )

        return recovered_new, len(processed)

    async def poll_once(self, on_recovered: RecoveryHandler | None = None) -> None:
        previous_raw = self.db.get_meta("marketplace_last_success")
        previous: datetime | None = None
        if previous_raw:
            try:
                previous = datetime.fromisoformat(previous_raw)
                if previous.tzinfo is None:
                    previous = previous.replace(tzinfo=timezone.utc)
            except ValueError:
                previous = None

        await self.refresh()
        recovered_at = datetime.now(timezone.utc)
        threshold = max(120, int(self.settings.order_check_interval) * 3)

        if (
            previous is not None
            and (recovered_at - previous).total_seconds() > threshold
        ):
            await self.reconcile_since(previous)
            if on_recovered is not None:
                await on_recovered(previous, recovered_at)

        self.db.set_meta("marketplace_last_success", recovered_at.isoformat())

    async def loop(self, on_recovered: RecoveryHandler | None = None) -> None:
        while True:
            await asyncio.sleep(self.settings.order_check_interval)
            try:
                await self.poll_once(on_recovered)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("FBS order refresh/recovery failed")

    async def _finish(self, chat_id: int, message_id: int, original: str, status: str) -> None:
        text = f"{original.rstrip()}\n\n{status}" if original.strip() else status
        try:
            await self.tg.edit_message_text(chat_id, message_id, text, reply_markup={"inline_keyboard": []})
        except Exception:
            await self.tg.send_message(chat_id, status)

    async def _current_order(self, order_id: int) -> FBSOrder | None:
        return next((o for o in await self._new_orders() if o.id == int(order_id)), None)

    async def _show_choices(self, chat_id: int, message_id: int, order: FBSOrder) -> None:
        supplies = await self._supplies()
        await self.tg.edit_message_text(chat_id, message_id, self._text(order, supplies), reply_markup=self._keyboard(order, supplies))

    async def _create_supply(self, order: FBSOrder) -> str:
        name = f"TG {datetime.now().astimezone():%Y-%m-%d %H:%M}"
        data = await self.wb._json("POST", f"{self.wb.MARKETPLACE_BASE}/api/v3/supplies", json={"name": name})
        supply_id = str((data or {}).get("id") or "")
        if not supply_id:
            raise RuntimeError(f"WB не вернул ID новой поставки: {data!r}")
        return supply_id

    async def _add_order(self, supply_id: str, order_id: int) -> None:
        await self.wb._json("PATCH", f"{self.wb.MARKETPLACE_BASE}/api/marketplace/v3/supplies/{supply_id}/orders", json={"orders": [int(order_id)]})

    async def _one_box(self, supply_id: str) -> str:
        data = await self.wb._json(
            "POST",
            f"{self.wb.MARKETPLACE_BASE}/api/v3/supplies/{supply_id}/trbx",
            json={"amount": 1},
        )
        ids = [str(x) for x in (data or {}).get("trbxIds", [])]
        if len(ids) != 1:
            raise RuntimeError("WB не вернул ID грузоместа")
        return ids[0]

    async def handle_callback(self, chat_id: int, message_id: int, data: str, original: str = "") -> bool:
        if not data.startswith("ord"):
            return False
        if chat_id not in self.settings.telegram_chat_ids:
            return True
        try:
            async with self._lock:
                parts = data.split(":")
                action, order_id = parts[0], int(parts[1])
                state = self._state(order_id)
                if state and state[0] in {"assigned", "skipped"}:
                    status = f"ℹ️ Заказ уже добавлен в поставку {state[1]}." if state[0] == "assigned" else "ℹ️ Уже выбрано «Не добавлять»."
                    await self._finish(chat_id, message_id, original, status)
                    return True
                if action == "ordskip":
                    self._set_state(order_id, "skipped")
                    await self._finish(chat_id, message_id, original, "⏭ Решение: заказ не добавлять в поставку.")
                    return True
                order = await self._current_order(order_id)
                if order is None:
                    await self._finish(chat_id, message_id, original, "ℹ️ Заказ больше не находится среди новых: возможно, уже обработан или отменён.")
                    return True
                if action == "ordrefresh":
                    await self._show_choices(chat_id, message_id, order)
                    return True
                supplies = await self._supplies()
                eligible = self._eligible(order, supplies)
                if action == "ordadd":
                    supply_id = parts[2]
                    supply = next((s for s in eligible if s.id == supply_id), None)
                    if supply is None:
                        await self._show_choices(chat_id, message_id, order)
                        return True
                    await self._add_order(supply.id, order.id)
                    self.current_new_orders.pop(order.id, None)
                    self.current_supply_orders[order.id] = supply.id
                    self.db.set_order_runtime_status(order.id, "confirm", "waiting")
                    self._set_state(order.id, "assigned", supply.id)
                    await self._finish(chat_id, message_id, original, f"✅ Заказ добавлен в поставку {supply.name} ({supply.id}).")
                    return True
                if action == "ordnew":
                    if eligible:
                        await self._show_choices(chat_id, message_id, order)
                        return True
                    supply_id = await self._create_supply(order)
                    try:
                        await self._add_order(supply_id, order.id)
                    except Exception:
                        try:
                            await self.wb._json("DELETE", f"{self.wb.MARKETPLACE_BASE}/api/v3/supplies/{supply_id}")
                        except Exception:
                            log.exception("Could not remove empty supply %s", supply_id)
                        raise
                    self.current_new_orders.pop(order.id, None)
                    self.current_supply_orders[order.id] = supply_id
                    self.db.set_order_runtime_status(order.id, "confirm", "waiting")
                    self._set_state(order.id, "assigned", supply_id)
                    try:
                        await self._one_box(supply_id)
                        status = f"✅ Заказ добавлен в новую поставку {supply_id}.\n📦 Создано одно грузоместо."
                    except Exception as exc:
                        log.exception("Order assigned but box creation failed")
                        status = f"✅ Заказ добавлен в новую поставку {supply_id}.\n⚠️ Грузоместо не создано: {exc}"
                    await self._finish(chat_id, message_id, original, status)
                    return True
        except Exception as exc:
            log.exception("Order callback failed: %s", data)
            await self.tg.send_message(chat_id, f"⚠️ Не удалось обработать заказ: {exc}")
        return True
