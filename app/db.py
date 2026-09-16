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
        self.conn.commit()

    def get(self, source: str, nm_id: int) -> int | None:
        row = self.conn.execute(
            "SELECT quantity FROM stock_state WHERE source = ? AND nm_id = ?",
            (source, nm_id),
        ).fetchone()
        return None if row is None else int(row[0])

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

    def get_order_state(self, order_id: int) -> tuple[str, str | None] | None:
        row = self.conn.execute(
            "SELECT status, supply_id FROM order_state WHERE order_id = ?",
            (int(order_id),),
        ).fetchone()
        if row is None:
            return None
        return str(row[0]), (None if row[1] is None else str(row[1]))

    def remember_order(self, order_id: int, article: str, nm_id: int) -> bool:
        """Remember a new order. Return True only when it was not seen before."""
        now = datetime.now(timezone.utc).isoformat()
        with self.conn:
            cur = self.conn.execute(
                """
                INSERT OR IGNORE INTO order_state(
                    order_id, article, nm_id, status, supply_id, first_seen_at, updated_at
                ) VALUES (?, ?, ?, 'notified', NULL, ?, ?)
                """,
                (int(order_id), str(article), int(nm_id), now, now),
            )
        return cur.rowcount == 1

    def set_order_action(
        self, order_id: int, status: str, supply_id: str | None = None
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO order_state(
                    order_id, article, nm_id, status, supply_id, first_seen_at, updated_at
                ) VALUES (?, '', 0, ?, ?, ?, ?)
                ON CONFLICT(order_id) DO UPDATE SET
                    status = excluded.status,
                    supply_id = excluded.supply_id,
                    updated_at = excluded.updated_at
                """,
                (int(order_id), str(status), supply_id, now, now),
            )

    def close(self) -> None:
        self.conn.close()
