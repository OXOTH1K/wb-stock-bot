from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable

import aiohttp

from .models import ProductSize, SellerWarehouse

log = logging.getLogger(__name__)


class WBAPIError(RuntimeError):
    pass


class WildberriesClient:
    CONTENT_URL = "https://content-api.wildberries.ru/content/v2/get/cards/list"
    MARKETPLACE_BASE = "https://marketplace-api.wildberries.ru"
    ANALYTICS_WB_STOCKS_URL = (
        "https://seller-analytics-api.wildberries.ru/"
        "api/analytics/v1/stocks-report/wb-warehouses"
    )

    def __init__(self, token: str, timeout: int = 30):
        self._token = token
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "WildberriesClient":
        self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session:
            await self._session.close()

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("WildberriesClient must be used as an async context manager")
        return self._session

    @property
    def headers(self) -> dict[str, str]:
        return self._headers(self._token)

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": token,
            "Content-Type": "application/json",
        }

    async def _json(self, method: str, url: str, *, token: str | None = None, **kwargs):
        headers = self._headers(token or self._token)
        async with self.session.request(method, url, headers=headers, **kwargs) as response:
            text = await response.text()
            if response.status == 204:
                return None
            if response.status >= 400:
                raise WBAPIError(f"WB API {response.status}: {text[:800]}")
            if not text:
                return None
            try:
                return await response.json(content_type=None)
            except Exception as exc:
                raise WBAPIError(f"WB API returned invalid JSON: {text[:800]}") from exc

    async def get_single_seller_warehouse(self) -> SellerWarehouse:
        data = await self._json(
            "GET", f"{self.MARKETPLACE_BASE}/api/v3/warehouses"
        )
        if not isinstance(data, list):
            raise WBAPIError(f"Unexpected warehouses response: {data!r}")
        active = [w for w in data if not w.get("isDeleting", False)]
        if len(active) != 1:
            raise WBAPIError(
                f"Expected exactly one active seller warehouse, got {len(active)}"
            )
        row = active[0]
        return SellerWarehouse(id=int(row["id"]), name=str(row.get("name") or row["id"]))

    async def get_all_product_sizes(self) -> list[ProductSize]:
        result: list[ProductSize] = []
        cursor: dict = {"limit": 100}

        while True:
            payload = {
                "settings": {
                    "sort": {"ascending": True},
                    "filter": {"withPhoto": -1},
                    "cursor": cursor,
                }
            }
            data = await self._json("POST", self.CONTENT_URL, json=payload)
            cards = (data or {}).get("cards", [])
            for card in cards:
                nm_id = int(card["nmID"])
                vendor_code = str(card.get("vendorCode") or "")
                title = str(card.get("title") or "")
                for size in card.get("sizes", []):
                    chrt_id = size.get("chrtID")
                    if chrt_id is None:
                        continue
                    size_name = str(size.get("techSize") or size.get("wbSize") or "")
                    result.append(
                        ProductSize(
                            nm_id=nm_id,
                            chrt_id=int(chrt_id),
                            vendor_code=vendor_code,
                            title=title,
                            size_name=size_name,
                        )
                    )

            page_total = int((data or {}).get("cursor", {}).get("total", len(cards)))
            if not cards or page_total < cursor["limit"]:
                break

            response_cursor = (data or {}).get("cursor", {})
            updated_at = response_cursor.get("updatedAt")
            nm_id = response_cursor.get("nmID")
            if not updated_at or not nm_id:
                raise WBAPIError("Content API pagination cursor is missing")

            cursor = {
                "limit": 100,
                "updatedAt": updated_at,
                "nmID": int(nm_id),
            }
            # Official limit: 10 requests/minute, 6-second interval.
            await asyncio.sleep(6.1)

        if not result:
            log.warning("Catalog is empty")
        return result

    async def get_fbs_stocks(
        self, warehouse_id: int, chrt_ids: Iterable[int]
    ) -> dict[int, int]:
        ids = list(dict.fromkeys(int(x) for x in chrt_ids))
        result: dict[int, int] = {chrt_id: 0 for chrt_id in ids}

        for start in range(0, len(ids), 1000):
            chunk = ids[start : start + 1000]
            data = await self._json(
                "POST",
                f"{self.MARKETPLACE_BASE}/api/v3/stocks/{warehouse_id}",
                json={"chrtIds": chunk},
            )
            for item in (data or {}).get("stocks", []):
                result[int(item["chrtId"])] = int(item.get("amount", 0))
            if start + 1000 < len(ids):
                await asyncio.sleep(0.21)

        return result


    async def set_fbs_stocks(
        self, warehouse_id: int, quantities: dict[int, int]
    ) -> None:
        """Set absolute FBS quantities for chrtIDs on the seller warehouse."""
        stocks = [
            {"chrtId": int(chrt_id), "amount": int(amount)}
            for chrt_id, amount in quantities.items()
        ]
        if not stocks:
            return
        if any(row["amount"] < 0 for row in stocks):
            raise ValueError("Stock amount cannot be negative")

        # Marketplace API accepts up to 1000 stock rows per request.
        for start in range(0, len(stocks), 1000):
            chunk = stocks[start : start + 1000]
            await self._json(
                "PUT",
                f"{self.MARKETPLACE_BASE}/api/v3/stocks/{warehouse_id}",
                json={"stocks": chunk},
            )
            if start + 1000 < len(stocks):
                await asyncio.sleep(0.21)

    async def get_wb_stocks_by_nm(self, catalog_nm_ids: set[int]) -> dict[int, int]:
        """Return total quantity across all WB warehouses, aggregated by nmID."""
        result = {nm_id: 0 for nm_id in catalog_nm_ids}
        limit = 250_000
        offset = 0

        while True:
            data = await self._json(
                "POST",
                self.ANALYTICS_WB_STOCKS_URL,
                json={"limit": limit, "offset": offset},
            )
            items = ((data or {}).get("data") or {}).get("items", [])
            for item in items:
                nm_id = int(item["nmId"])
                if nm_id in result:
                    result[nm_id] += int(item.get("quantity", 0))

            if len(items) < limit:
                break
            offset += limit
            # Analytics endpoint limit: one request per 20 seconds.
            await asyncio.sleep(20.1)

        return result
