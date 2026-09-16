from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

from .config import Settings
from .db import StateDB
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


class OrderMonitor:
    def __init__(self, settings: Settings, wb: WildberriesClient, tg: TelegramBot, db: StateDB, warehouse_id: int):
        self.settings = settings
        self.wb = wb
        self.tg = tg
        self.db = db
        self.warehouse_id = int(warehouse_id)
        self._lock = asyncio.Lock()
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

    async def _new_orders(self) -> list[FBSOrder]:
        data = await self.wb._json("GET", f"{self.wb.MARKETPLACE_BASE}/api/v3/orders/new")
        out = []
        for row in (data or {}).get("orders", []):
            if int(row.get("warehouseId") or 0) != self.warehouse_id:
                continue
            out.append(FBSOrder(
                id=int(row["id"]), article=str(row.get("article") or ""), nm_id=int(row.get("nmId") or 0),
                warehouse_id=int(row.get("warehouseId") or 0), created_at=str(row.get("createdAt") or ""),
                cargo_type=int(row.get("cargoType") or 0), cross_border_type=int(row.get("crossBorderType") or 0),
                offices=tuple(str(x) for x in (row.get("offices") or [])),
            ))
        return out

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
        lines = ["🛒 Новый FBS-заказ", f"Артикул: {order.article or '—'}", f"Заказ: {order.id}"]
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
        unseen = [o for o in orders if self._state(o.id) is None]
        if not unseen:
            return
        supplies = None
        try:
            supplies = await self._supplies()
        except Exception:
            log.exception("Could not load supplies")
        for order in sorted(unseen, key=lambda o: (o.created_at, o.id)):
            await self.tg.broadcast(self.settings.telegram_chat_ids, self._text(order, supplies), reply_markup=self._keyboard(order, supplies))
            self._remember(order)

    async def loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.order_check_interval)
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("FBS order refresh failed")

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
        name = f"TG {datetime.now().astimezone():%Y-%m-%d %H:%M} {order.article or order.id}"[:128]
        data = await self.wb._json("POST", f"{self.wb.MARKETPLACE_BASE}/api/v3/supplies", json={"name": name})
        supply_id = str((data or {}).get("id") or "")
        if not supply_id:
            raise RuntimeError(f"WB не вернул ID новой поставки: {data!r}")
        return supply_id

    async def _add_order(self, supply_id: str, order_id: int) -> None:
        await self.wb._json("PATCH", f"{self.wb.MARKETPLACE_BASE}/api/marketplace/v3/supplies/{supply_id}/orders", json={"orders": [int(order_id)]})

    async def _one_box(self, supply_id: str) -> str:
        data = await self.wb._json("GET", f"{self.wb.MARKETPLACE_BASE}/api/v3/supplies/{supply_id}/trbx")
        boxes = [str(x["id"]) for x in (data or {}).get("trbxes", []) if x.get("id")]
        if len(boxes) == 1:
            return boxes[0]
        if len(boxes) > 1:
            raise RuntimeError(f"в новой поставке уже {len(boxes)} грузомест")
        data = await self.wb._json("POST", f"{self.wb.MARKETPLACE_BASE}/api/v3/supplies/{supply_id}/trbx", json={"amount": 1})
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
