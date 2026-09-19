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
