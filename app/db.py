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

    def close(self) -> None:
        self.conn.close()
