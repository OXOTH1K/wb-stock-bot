from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import aiohttp


class OzonAPIError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"Ozon API {status}: {message}")
        self.status = int(status)


@dataclass(frozen=True)
class OzonProduct:
    offer_id: str
    product_id: int
    name: str


@dataclass(frozen=True)
class OzonPostingProduct:
    offer_id: str
    name: str
    quantity: int
    sku: int = 0


@dataclass(frozen=True)
class OzonPosting:
    posting_number: str
    order_number: str
    status: str
    cutoff: str
    warehouse_id: int
    products: tuple[OzonPostingProduct, ...]
    substatus: str = ""


class OzonClient:
    BASE_URL = "https://api-seller.ozon.ru"

    def __init__(self, client_id: str, api_key: str, timeout: int = 30):
        self.client_id = str(client_id)
        self.api_key = str(api_key)
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "OzonClient":
        self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session is not None:
            await self._session.close()

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("OzonClient must be used as an async context manager")
        return self._session

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Client-Id": self.client_id,
            "Api-Key": self.api_key,
            "Content-Type": "application/json",
        }

    async def _json(self, path: str, payload: dict | None = None):
        url = f"{self.BASE_URL}{path}"
        body = {} if payload is None else payload
        for attempt in range(3):
            async with self.session.post(
                url,
                headers=self.headers,
                json=body,
            ) as response:
                text = await response.text()
                if response.status == 204:
                    return None
                if response.status == 429 and attempt < 2:
                    retry_after = response.headers.get("Retry-After", "").strip()
                    try:
                        delay = max(1.0, float(retry_after))
                    except ValueError:
                        delay = float(2 ** attempt)
                    await asyncio.sleep(min(delay, 30.0))
                    continue
                if response.status >= 400:
                    raise OzonAPIError(response.status, text[:1000])
                if not text:
                    return None
                try:
                    return await response.json(content_type=None)
                except Exception as exc:
                    raise OzonAPIError(
                        response.status,
                        f"invalid JSON: {text[:800]}",
                    ) from exc
        raise OzonAPIError(429, "rate limit retry exhausted")

    @staticmethod
    def _result_items(data) -> list[dict]:
        if not isinstance(data, dict):
            return []
        result = data.get("result")
        if isinstance(result, dict):
            items = result.get("items")
            return items if isinstance(items, list) else []
        if isinstance(result, list):
            return result
        items = data.get("items")
        return items if isinstance(items, list) else []

    async def get_catalog(self) -> list[OzonProduct]:
        base_rows: list[dict] = []
        last_id = ""
        seen: set[str] = set()

        while True:
            payload = {
                "filter": {"visibility": "ALL"},
                "limit": 1000,
                "last_id": last_id,
            }
            data = await self._json("/v3/product/list", payload)
            result = (data or {}).get("result") or {}
            rows = result.get("items", []) if isinstance(result, dict) else []
            if not isinstance(rows, list):
                rows = []
            base_rows.extend(row for row in rows if isinstance(row, dict))

            next_id = ""
            if isinstance(result, dict):
                next_id = str(result.get("last_id") or "")
            if not rows or not next_id or next_id == last_id or next_id in seen:
                break
            seen.add(next_id)
            last_id = next_id

        raw_products: dict[str, int] = {}
        for row in base_rows:
            offer_id = str(row.get("offer_id") or "").strip()
            product_id = row.get("product_id", row.get("id", 0))
            if not offer_id:
                continue
            try:
                pid = int(product_id or 0)
            except (TypeError, ValueError):
                pid = 0
            raw_products[offer_id] = pid

        if not raw_products:
            return []

        names: dict[str, str] = {}
        product_ids = [pid for pid in raw_products.values() if pid > 0]
        try:
            for start in range(0, len(product_ids), 100):
                chunk = product_ids[start : start + 100]
                data = await self._json(
                    "/v3/product/info/list",
                    {
                        "product_id": chunk,
                        "offer_id": [],
                        "sku": [],
                    },
                )
                for row in self._result_items(data):
                    offer_id = str(row.get("offer_id") or "").strip()
                    if offer_id:
                        names[offer_id] = str(row.get("name") or "").strip()
        except OzonAPIError:
            # Catalog identity (offer_id) is enough for stock/order mapping.
            # If the details endpoint changes or is temporarily unavailable,
            # keep the integration functional and use offer_id as display name.
            names = {}

        return [
            OzonProduct(
                offer_id=offer_id,
                product_id=product_id,
                name=names.get(offer_id) or offer_id,
            )
            for offer_id, product_id in sorted(raw_products.items())
        ]

    async def get_fbs_stocks(self) -> dict[str, int]:
        result: dict[str, int] = {}
        cursor = ""
        seen: set[str] = set()

        while True:
            payload = {
                "filter": {"visibility": "ALL"},
                "limit": 1000,
            }
            if cursor:
                payload["cursor"] = cursor

            data = await self._json("/v4/product/info/stocks", payload)
            rows = (data or {}).get("items", [])
            if not isinstance(rows, list):
                rows = []

            for row in rows:
                if not isinstance(row, dict):
                    continue
                offer_id = str(row.get("offer_id") or "").strip()
                if not offer_id:
                    continue
                total = 0
                stocks = row.get("stocks") or []
                if isinstance(stocks, dict):
                    stocks = list(stocks.values())
                for stock in stocks:
                    if not isinstance(stock, dict):
                        continue
                    if str(stock.get("type") or "").lower() != "fbs":
                        continue
                    total += max(0, int(stock.get("present") or 0))
                result[offer_id] = total

            next_cursor = str((data or {}).get("cursor") or "")
            has_more = bool((data or {}).get("has_next"))
            if not rows or not next_cursor or next_cursor == cursor or next_cursor in seen:
                break
            if not has_more and len(rows) < 1000:
                break
            seen.add(next_cursor)
            cursor = next_cursor

        return result

    @staticmethod
    def _parse_posting(row: dict) -> OzonPosting:
        products: list[OzonPostingProduct] = []
        for item in row.get("products") or []:
            if not isinstance(item, dict):
                continue
            offer_id = str(
                item.get("offer_id")
                or item.get("product_offer_id")
                or ""
            ).strip()
            name = str(
                item.get("name")
                or item.get("product_name")
                or offer_id
                or "—"
            )
            sku_raw = item.get("sku", item.get("product_id", 0))
            try:
                sku = int(sku_raw or 0)
            except (TypeError, ValueError):
                sku = 0
            products.append(
                OzonPostingProduct(
                    offer_id=offer_id,
                    name=name,
                    quantity=max(0, int(item.get("quantity") or 0)),
                    sku=sku,
                )
            )

        delivery_method = row.get("delivery_method") or {}
        try:
            warehouse_id = int(delivery_method.get("warehouse_id") or 0)
        except (TypeError, ValueError):
            warehouse_id = 0

        return OzonPosting(
            posting_number=str(row.get("posting_number") or ""),
            order_number=str(row.get("order_number") or ""),
            status=str(row.get("status") or ""),
            cutoff=str(
                row.get("cutoff")
                or row.get("shipment_date")
                or ""
            ),
            warehouse_id=warehouse_id,
            products=tuple(products),
            substatus=str(row.get("substatus") or ""),
        )

    async def get_awaiting_packaging(self) -> list[OzonPosting]:
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=30)
        to = now + timedelta(days=2)
        cursor = ""
        seen: set[str] = set()
        result: list[OzonPosting] = []

        while True:
            payload = {
                "sort_dir": "asc",
                "filter": {
                    "since": since.isoformat().replace("+00:00", "Z"),
                    "to": to.isoformat().replace("+00:00", "Z"),
                    "status": ["awaiting_packaging"],
                },
                "limit": 1000,
                "cursor": cursor,
                "with": {
                    "analytics_data": False,
                    "barcodes": False,
                    "financial_data": False,
                    "legal_info": False,
                },
            }
            data = await self._json("/v4/posting/fbs/list", payload)
            rows = (data or {}).get("postings", [])
            if not isinstance(rows, list):
                rows = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                posting = self._parse_posting(row)
                if (
                    posting.posting_number
                    and posting.status == "awaiting_packaging"
                ):
                    result.append(posting)

            next_cursor = str((data or {}).get("cursor") or "")
            if (
                not bool((data or {}).get("has_next"))
                or not next_cursor
                or next_cursor == cursor
                or next_cursor in seen
            ):
                break
            seen.add(next_cursor)
            cursor = next_cursor

        return result

    async def get_posting(self, posting_number: str) -> OzonPosting:
        data = await self._json(
            "/v3/posting/fbs/get",
            {
                "posting_number": str(posting_number),
                "with": {
                    "analytics_data": False,
                    "barcodes": False,
                    "financial_data": False,
                    "product_exemplars": False,
                    "related_postings": False,
                },
            },
        )
        row = (data or {}).get("result") or {}
        if not isinstance(row, dict) or not row.get("posting_number"):
            raise OzonAPIError(200, f"unexpected posting response: {data!r}")
        return self._parse_posting(row)

    async def ship_fbs(self, posting: OzonPosting) -> None:
        products = [
            {
                "product_id": int(product.sku),
                "quantity": int(product.quantity),
            }
            for product in posting.products
            if int(product.quantity) > 0
        ]
        if not products:
            raise OzonAPIError(
                400,
                "posting has no products that can be assembled",
            )
        if any(product["product_id"] <= 0 for product in products):
            raise OzonAPIError(
                400,
                "Ozon SKU is missing for one or more posting products",
            )

        await self._json(
            "/v4/posting/fbs/ship",
            {
                "packages": [{"products": products}],
                "posting_number": posting.posting_number,
                "with": {"additional_data": False},
            },
        )

        # Ozon explicitly notes that HTTP 200 does not guarantee successful
        # assembly. Verify the posting state without repeating the write call.
        for attempt in range(3):
            current = await self.get_posting(posting.posting_number)
            if current.status != "awaiting_packaging":
                if current.substatus == "ship_failed":
                    raise OzonAPIError(
                        409,
                        "Ozon returned ship_failed after assembly request",
                    )
                return
            if current.substatus == "ship_failed":
                raise OzonAPIError(
                    409,
                    "Ozon returned ship_failed after assembly request",
                )
            if attempt < 2:
                await asyncio.sleep(0.5)

        raise OzonAPIError(
            409,
            "posting is still awaiting_packaging after assembly request",
        )
