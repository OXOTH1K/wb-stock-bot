from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class StateDB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stock_state (
                source TEXT NOT NULL,
                nm_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (source, nm_id)
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fbs_saved_stock (
                scope TEXT NOT NULL,
                nm_id INTEGER NOT NULL,
                chrt_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (scope, nm_id, chrt_id)
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_alert (
                alert_key TEXT PRIMARY KEY,
                alert_type TEXT NOT NULL,
                nm_id INTEGER NOT NULL,
                old_qty INTEGER NOT NULL DEFAULT 0,
                new_qty INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stock_decision (
                nm_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                fbs_qty INTEGER NOT NULL,
                wb_qty INTEGER NOT NULL,
                decision TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (nm_id, action)
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS local_inventory (
                sku TEXT PRIMARY KEY,
                quantity INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS inventory_movement (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sku TEXT NOT NULL,
                delta INTEGER NOT NULL,
                before_qty INTEGER NOT NULL,
                after_qty INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS channel_stock (
                source TEXT NOT NULL,
                sku TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (source, sku)
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS channel_catalog (
                source TEXT NOT NULL,
                sku TEXT NOT NULL,
                title TEXT NOT NULL,
                external_id TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (source, sku)
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS crm_order_meta (
                order_id INTEGER PRIMARY KEY,
                assembled INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS crm_order_status (
                order_id INTEGER PRIMARY KEY,
                supplier_status TEXT NOT NULL,
                wb_status TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS marketplace_stock_policy (
                channel TEXT NOT NULL,
                sku TEXT NOT NULL,
                suppressed INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (channel, sku)
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS inventory_sale_event (
                channel TEXT NOT NULL,
                event_id TEXT NOT NULL,
                sku TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                local_before INTEGER NOT NULL,
                local_after INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (channel, event_id, sku)
            )
            """
        )
        self.conn.commit()

    def get(self, source: str, nm_id: int) -> int | None:
        row = self.conn.execute(
            "SELECT quantity FROM stock_state WHERE source = ? AND nm_id = ?",
            (source, nm_id),
        ).fetchone()
        return None if row is None else int(row[0])

    def get_source(self, source: str) -> dict[int, int]:
        rows = self.conn.execute(
            "SELECT nm_id, quantity FROM stock_state WHERE source = ?",
            (source,),
        ).fetchall()
        return {int(nm_id): int(quantity) for nm_id, quantity in rows}

    def update_many(self, source: str, quantities: dict[int, int]) -> list[tuple[int, int, int]]:
        """Persist quantities and return transitions (nm_id, old_qty, new_qty)."""
        now = datetime.now(timezone.utc).isoformat()
        transitions: list[tuple[int, int, int]] = []
        with self.conn:
            for nm_id, new_qty in quantities.items():
                old_qty = self.get(source, nm_id)
                if old_qty is not None and old_qty != new_qty:
                    transitions.append((nm_id, old_qty, new_qty))
                self.conn.execute(
                    """
                    INSERT INTO stock_state(source, nm_id, quantity, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(source, nm_id) DO UPDATE SET
                        quantity = excluded.quantity,
                        updated_at = excluded.updated_at
                    """,
                    (source, nm_id, int(new_qty), now),
                )
        return transitions

    def replace_saved_fbs(
        self, scope: str, rows: dict[tuple[int, int], int]
    ) -> None:
        """Replace one saved FBS snapshot.

        rows maps (nm_id, chrt_id) to absolute quantity.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self.conn:
            self.conn.execute("DELETE FROM fbs_saved_stock WHERE scope = ?", (scope,))
            self.conn.executemany(
                """
                INSERT INTO fbs_saved_stock(scope, nm_id, chrt_id, quantity, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (scope, int(nm_id), int(chrt_id), int(quantity), now)
                    for (nm_id, chrt_id), quantity in rows.items()
                ],
            )

    def save_product_fbs(
        self, scope: str, nm_id: int, quantities: dict[int, int]
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.conn:
            self.conn.execute(
                "DELETE FROM fbs_saved_stock WHERE scope = ? AND nm_id = ?",
                (scope, int(nm_id)),
            )
            self.conn.executemany(
                """
                INSERT INTO fbs_saved_stock(scope, nm_id, chrt_id, quantity, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (scope, int(nm_id), int(chrt_id), int(quantity), now)
                    for chrt_id, quantity in quantities.items()
                ],
            )

    def get_saved_product_fbs(self, scope: str, nm_id: int) -> dict[int, int]:
        rows = self.conn.execute(
            """
            SELECT chrt_id, quantity
            FROM fbs_saved_stock
            WHERE scope = ? AND nm_id = ?
            ORDER BY chrt_id
            """,
            (scope, int(nm_id)),
        ).fetchall()
        return {int(chrt_id): int(quantity) for chrt_id, quantity in rows}

    def get_saved_fbs(self, scope: str) -> dict[tuple[int, int], int]:
        rows = self.conn.execute(
            """
            SELECT nm_id, chrt_id, quantity
            FROM fbs_saved_stock
            WHERE scope = ?
            ORDER BY nm_id, chrt_id
            """,
            (scope,),
        ).fetchall()
        return {
            (int(nm_id), int(chrt_id)): int(quantity)
            for nm_id, chrt_id, quantity in rows
        }

    def clear_saved_product_fbs(self, scope: str, nm_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "DELETE FROM fbs_saved_stock WHERE scope = ? AND nm_id = ?",
                (scope, int(nm_id)),
            )

    def clear_saved_fbs(self, scope: str) -> None:
        with self.conn:
            self.conn.execute(
                "DELETE FROM fbs_saved_stock WHERE scope = ?",
                (scope,),
            )

    def put_pending_alert(
        self,
        alert_key: str,
        alert_type: str,
        nm_id: int,
        old_qty: int = 0,
        new_qty: int = 0,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO pending_alert(
                    alert_key, alert_type, nm_id, old_qty, new_qty, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(alert_key) DO UPDATE SET
                    alert_type = excluded.alert_type,
                    nm_id = excluded.nm_id,
                    old_qty = excluded.old_qty,
                    new_qty = excluded.new_qty
                """,
                (
                    str(alert_key),
                    str(alert_type),
                    int(nm_id),
                    int(old_qty),
                    int(new_qty),
                    now,
                ),
            )

    def list_pending_alerts(self) -> list[tuple[str, str, int, int, int]]:
        rows = self.conn.execute(
            """
            SELECT alert_key, alert_type, nm_id, old_qty, new_qty
            FROM pending_alert
            ORDER BY created_at, alert_key
            """
        ).fetchall()
        return [
            (str(key), str(kind), int(nm_id), int(old_qty), int(new_qty))
            for key, kind, nm_id, old_qty, new_qty in rows
        ]

    def delete_pending_alert(self, alert_key: str) -> None:
        with self.conn:
            self.conn.execute(
                "DELETE FROM pending_alert WHERE alert_key = ?",
                (str(alert_key),),
            )

    def save_stock_decision(
        self,
        nm_id: int,
        action: str,
        fbs_qty: int,
        wb_qty: int,
        decision: str,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO stock_decision(
                    nm_id, action, fbs_qty, wb_qty, decision, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(nm_id, action) DO UPDATE SET
                    fbs_qty = excluded.fbs_qty,
                    wb_qty = excluded.wb_qty,
                    decision = excluded.decision,
                    updated_at = excluded.updated_at
                """,
                (
                    int(nm_id),
                    str(action),
                    int(fbs_qty),
                    int(wb_qty),
                    str(decision),
                    now,
                ),
            )

    def stock_decision_matches(
        self,
        nm_id: int,
        action: str,
        fbs_qty: int,
        wb_qty: int,
        decision: str = "skip",
    ) -> bool:
        row = self.conn.execute(
            """
            SELECT fbs_qty, wb_qty, decision
            FROM stock_decision
            WHERE nm_id = ? AND action = ?
            """,
            (int(nm_id), str(action)),
        ).fetchone()
        if row is None:
            return False
        return (
            int(row[0]) == int(fbs_qty)
            and int(row[1]) == int(wb_qty)
            and str(row[2]) == str(decision)
        )

    def clear_stock_decision(self, nm_id: int, action: str) -> None:
        with self.conn:
            self.conn.execute(
                "DELETE FROM stock_decision WHERE nm_id = ? AND action = ?",
                (int(nm_id), str(action)),
            )

    def clear_stock_decisions(self, nm_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "DELETE FROM stock_decision WHERE nm_id = ?",
                (int(nm_id),),
            )

    def ensure_local_stock(self, sku: str, quantity: int) -> int:
        sku = str(sku).strip()
        quantity = max(0, int(quantity))
        row = self.conn.execute(
            "SELECT quantity FROM local_inventory WHERE sku = ?",
            (sku,),
        ).fetchone()
        if row is not None:
            return int(row[0])
        now = datetime.now(timezone.utc).isoformat()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO local_inventory(sku, quantity, updated_at)
                VALUES (?, ?, ?)
                """,
                (sku, quantity, now),
            )
            if quantity:
                self.conn.execute(
                    """
                    INSERT INTO inventory_movement(
                        sku, delta, before_qty, after_qty, reason, created_at
                    )
                    VALUES (?, ?, 0, ?, 'bootstrap', ?)
                    """,
                    (sku, quantity, quantity, now),
                )
        return quantity

    def get_local_stock(self, skus: list[str] | tuple[str, ...]) -> dict[str, int]:
        cleaned = [str(sku).strip() for sku in skus if str(sku).strip()]
        if not cleaned:
            return {}
        placeholders = ",".join("?" for _ in cleaned)
        rows = self.conn.execute(
            f"SELECT sku, quantity FROM local_inventory WHERE sku IN ({placeholders})",
            cleaned,
        ).fetchall()
        return {str(sku): int(quantity) for sku, quantity in rows}

    def set_local_stock(self, sku: str, quantity: int, reason: str = "crm") -> int:
        sku = str(sku).strip()
        if not sku:
            raise ValueError("SKU is required")
        quantity = int(quantity)
        if quantity < 0:
            raise ValueError("Local stock cannot be negative")
        row = self.conn.execute(
            "SELECT quantity FROM local_inventory WHERE sku = ?",
            (sku,),
        ).fetchone()
        before = int(row[0]) if row is not None else 0
        now = datetime.now(timezone.utc).isoformat()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO local_inventory(sku, quantity, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(sku) DO UPDATE SET
                    quantity = excluded.quantity,
                    updated_at = excluded.updated_at
                """,
                (sku, quantity, now),
            )
            if quantity != before:
                self.conn.execute(
                    """
                    INSERT INTO inventory_movement(
                        sku, delta, before_qty, after_qty, reason, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (sku, quantity - before, before, quantity, str(reason), now),
                )
        return quantity

    def adjust_local_stock(self, sku: str, delta: int, reason: str = "crm") -> int:
        sku = str(sku).strip()
        if not sku:
            raise ValueError("SKU is required")
        row = self.conn.execute(
            "SELECT quantity FROM local_inventory WHERE sku = ?",
            (sku,),
        ).fetchone()
        before = int(row[0]) if row is not None else 0
        after = before + int(delta)
        if after < 0:
            raise ValueError("Local stock cannot be negative")
        return self.set_local_stock(sku, after, reason=reason)

    def list_inventory_movements(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT sku, delta, before_qty, after_qty, reason, created_at
            FROM inventory_movement
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, min(int(limit), 500)),),
        ).fetchall()
        return [
            {
                "sku": str(sku),
                "delta": int(delta),
                "before": int(before),
                "after": int(after),
                "reason": str(reason),
                "created_at": str(created_at),
            }
            for sku, delta, before, after, reason, created_at in rows
        ]

    def get_channel_stock(
        self, source: str, skus: list[str] | tuple[str, ...]
    ) -> dict[str, int]:
        cleaned = [str(sku).strip() for sku in skus if str(sku).strip()]
        if not cleaned:
            return {}
        placeholders = ",".join("?" for _ in cleaned)
        rows = self.conn.execute(
            f"""
            SELECT sku, quantity
            FROM channel_stock
            WHERE source = ? AND sku IN ({placeholders})
            """,
            [str(source), *cleaned],
        ).fetchall()
        return {str(sku): int(quantity) for sku, quantity in rows}

    def set_channel_stock(self, source: str, sku: str, quantity: int) -> None:
        quantity = int(quantity)
        if quantity < 0:
            raise ValueError("Channel stock cannot be negative")
        now = datetime.now(timezone.utc).isoformat()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO channel_stock(source, sku, quantity, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(source, sku) DO UPDATE SET
                    quantity = excluded.quantity,
                    updated_at = excluded.updated_at
                """,
                (str(source), str(sku).strip(), quantity, now),
            )

    def replace_channel_stock(
        self, source: str, quantities: dict[str, int]
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        cleaned = {
            str(sku).strip(): int(quantity)
            for sku, quantity in quantities.items()
            if str(sku).strip()
        }
        if any(quantity < 0 for quantity in cleaned.values()):
            raise ValueError("Channel stock cannot be negative")
        with self.conn:
            self.conn.execute(
                "DELETE FROM channel_stock WHERE source = ?",
                (str(source),),
            )
            self.conn.executemany(
                """
                INSERT INTO channel_stock(source, sku, quantity, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (str(source), sku, quantity, now)
                    for sku, quantity in cleaned.items()
                ],
            )

    def replace_channel_catalog(
        self,
        source: str,
        products: list[tuple[str, str, str]],
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        rows = []
        seen: set[str] = set()
        for sku, title, external_id in products:
            clean_sku = str(sku).strip()
            if not clean_sku or clean_sku in seen:
                continue
            seen.add(clean_sku)
            rows.append(
                (
                    str(source),
                    clean_sku,
                    str(title or clean_sku),
                    str(external_id or ""),
                    now,
                )
            )
        with self.conn:
            self.conn.execute(
                "DELETE FROM channel_catalog WHERE source = ?",
                (str(source),),
            )
            self.conn.executemany(
                """
                INSERT INTO channel_catalog(
                    source, sku, title, external_id, updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                rows,
            )

    def get_channel_catalog(self, source: str) -> dict[str, dict]:
        rows = self.conn.execute(
            """
            SELECT sku, title, external_id
            FROM channel_catalog
            WHERE source = ?
            ORDER BY sku
            """,
            (str(source),),
        ).fetchall()
        return {
            str(sku): {
                "sku": str(sku),
                "title": str(title),
                "external_id": str(external_id),
            }
            for sku, title, external_id in rows
        }

    def local_stock_quantity(self, sku: str) -> int | None:
        row = self.conn.execute(
            "SELECT quantity FROM local_inventory WHERE sku = ?",
            (str(sku).strip(),),
        ).fetchone()
        return None if row is None else int(row[0])

    def is_channel_suppressed(self, channel: str, sku: str) -> bool:
        row = self.conn.execute(
            """
            SELECT suppressed
            FROM marketplace_stock_policy
            WHERE channel = ? AND sku = ?
            """,
            (str(channel), str(sku).strip()),
        ).fetchone()
        return False if row is None else bool(row[0])

    def set_channel_suppressed(
        self, channel: str, sku: str, suppressed: bool
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO marketplace_stock_policy(
                    channel, sku, suppressed, updated_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(channel, sku) DO UPDATE SET
                    suppressed = excluded.suppressed,
                    updated_at = excluded.updated_at
                """,
                (
                    str(channel),
                    str(sku).strip(),
                    1 if suppressed else 0,
                    now,
                ),
            )

    def record_inventory_sale(
        self,
        channel: str,
        event_id: str,
        sku: str,
        quantity: int,
    ) -> tuple[bool, int, int]:
        sku = str(sku).strip()
        quantity = max(0, int(quantity))
        if not sku or quantity <= 0:
            current = self.local_stock_quantity(sku) or 0
            return False, current, current

        existing = self.conn.execute(
            """
            SELECT local_before, local_after
            FROM inventory_sale_event
            WHERE channel = ? AND event_id = ? AND sku = ?
            """,
            (str(channel), str(event_id), sku),
        ).fetchone()
        if existing is not None:
            return False, int(existing[0]), int(existing[1])

        before_row = self.conn.execute(
            "SELECT quantity FROM local_inventory WHERE sku = ?",
            (sku,),
        ).fetchone()
        before = int(before_row[0]) if before_row is not None else 0
        after = max(0, before - quantity)
        now = datetime.now(timezone.utc).isoformat()

        with self.conn:
            self.conn.execute(
                """
                INSERT INTO local_inventory(sku, quantity, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(sku) DO UPDATE SET
                    quantity = excluded.quantity,
                    updated_at = excluded.updated_at
                """,
                (sku, after, now),
            )
            self.conn.execute(
                """
                INSERT INTO inventory_sale_event(
                    channel, event_id, sku, quantity,
                    local_before, local_after, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(channel),
                    str(event_id),
                    sku,
                    quantity,
                    before,
                    after,
                    now,
                ),
            )
            if after != before:
                self.conn.execute(
                    """
                    INSERT INTO inventory_movement(
                        sku, delta, before_qty, after_qty,
                        reason, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sku,
                        after - before,
                        before,
                        after,
                        f"sale:{channel}:{event_id}",
                        now,
                    ),
                )
        return True, before, after

    def find_channel_sku_by_external_id(
        self, source: str, external_id: str
    ) -> str | None:
        row = self.conn.execute(
            """
            SELECT sku
            FROM channel_catalog
            WHERE source = ? AND external_id = ?
            LIMIT 1
            """,
            (str(source), str(external_id)),
        ).fetchone()
        return None if row is None else str(row[0])

    def set_order_runtime_status(
        self, order_id: int, supplier_status: str, wb_status: str = ""
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO crm_order_status(
                    order_id, supplier_status, wb_status, updated_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(order_id) DO UPDATE SET
                    supplier_status = excluded.supplier_status,
                    wb_status = excluded.wb_status,
                    updated_at = excluded.updated_at
                """,
                (
                    int(order_id),
                    str(supplier_status),
                    str(wb_status or ""),
                    now,
                ),
            )

    def set_order_assembled(self, order_id: int, assembled: bool) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO crm_order_meta(order_id, assembled, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(order_id) DO UPDATE SET
                    assembled = excluded.assembled,
                    updated_at = excluded.updated_at
                """,
                (int(order_id), 1 if assembled else 0, now),
            )

    def get_order_assembled(self, order_ids: list[int] | tuple[int, ...]) -> dict[int, bool]:
        ids = [int(order_id) for order_id in order_ids]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"""
            SELECT order_id, assembled
            FROM crm_order_meta
            WHERE order_id IN ({placeholders})
            """,
            ids,
        ).fetchall()
        return {int(order_id): bool(assembled) for order_id, assembled in rows}

    def list_order_state(self, limit: int = 200) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT
                s.order_id,
                s.article,
                s.nm_id,
                s.status,
                s.supply_id,
                s.first_seen_at,
                s.updated_at,
                COALESCE(m.assembled, 0),
                r.supplier_status,
                r.wb_status
            FROM order_state AS s
            LEFT JOIN crm_order_meta AS m ON m.order_id = s.order_id
            LEFT JOIN crm_order_status AS r ON r.order_id = s.order_id
            ORDER BY s.first_seen_at DESC, s.order_id DESC
            LIMIT ?
            """,
            (max(1, min(int(limit), 1000)),),
        ).fetchall()
        return [
            {
                "order_id": int(order_id),
                "article": str(article),
                "nm_id": int(nm_id),
                "status": str(status),
                "supply_id": None if supply_id is None else str(supply_id),
                "first_seen_at": str(first_seen_at),
                "updated_at": str(updated_at),
                "assembled": bool(assembled),
                "supplier_status": (
                    None if supplier_status is None else str(supplier_status)
                ),
                "wb_status": None if wb_status is None else str(wb_status),
            }
            for (
                order_id,
                article,
                nm_id,
                status,
                supply_id,
                first_seen_at,
                updated_at,
                assembled,
                supplier_status,
                wb_status,
            ) in rows
        ]

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM bot_meta WHERE key = ?",
            (str(key),),
        ).fetchone()
        return None if row is None else str(row[0])

    def set_meta(self, key: str, value: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO bot_meta(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (str(key), str(value), now),
            )

    def close(self) -> None:
        self.conn.close()
