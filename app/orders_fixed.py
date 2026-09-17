from __future__ import annotations

from .orders import OrderMonitor as BaseOrderMonitor


class OrderMonitor(BaseOrderMonitor):
    """Runtime order monitor with direct one-box creation for new supplies.

    A newly created supply has no cargo places yet. The base implementation first
    queried GET /trbx before POSTing a box; in practice WB can return 404 for that
    immediate GET even though POST /trbx is available. For a supply created by this
    flow the pre-check is unnecessary because duplicate button clicks are already
    guarded by persisted order state.
    """

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
