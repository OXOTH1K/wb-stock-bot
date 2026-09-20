import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.db import StateDB
from app.ozon import OzonIntegration
from app.ozon_client import (
    OzonClient,
    OzonPosting,
    OzonPostingProduct,
    OzonProduct,
)


class FakeTelegram:
    def __init__(self):
        self.broadcasts = []
        self.sent = []
        self.edits = []

    async def broadcast(self, chat_ids, text, **kwargs):
        self.broadcasts.append((chat_ids, text, kwargs))

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))

    async def edit_message_text(
        self, chat_id, message_id, text, **kwargs
    ):
        self.edits.append((chat_id, message_id, text, kwargs))


class FakeOzon:
    def __init__(self):
        self.catalog = [
            OzonProduct("SKU-A", 101, "Alpha Ozon"),
            OzonProduct("OZON-ONLY", 202, "Ozon Only"),
        ]
        self.stocks = {"SKU-A": 5, "OZON-ONLY": 4}
        self.postings = [
            OzonPosting(
                posting_number="12345678-0001-1",
                order_number="12345678",
                status="awaiting_packaging",
                cutoff="2026-09-21T10:00:00Z",
                warehouse_id=10,
                products=(
                    OzonPostingProduct(
                        "SKU-A", "Alpha Ozon", 1, 1001
                    ),
                ),
            )
        ]
        self.details = {
            posting.posting_number: posting
            for posting in self.postings
        }
        self.shipped = []

    async def get_catalog(self):
        return list(self.catalog)

    async def get_fbs_stocks(self):
        return dict(self.stocks)

    async def get_stock_breakdown(self):
        return dict(self.stocks), {
            "SKU-A": 0,
            "OZON-ONLY": 0,
        }

    async def get_fbs_warehouses(self):
        return [
            {
                "warehouse_id": 77,
                "name": "Ozon FBS",
            }
        ]

    async def set_fbs_stocks(self, warehouse_id, quantities):
        self.stocks.update(
            {str(k): int(v) for k, v in quantities.items()}
        )

    async def get_awaiting_packaging(self):
        return list(self.postings)

    async def get_posting(self, posting_number):
        return self.details[posting_number]

    async def ship_fbs(self, posting):
        self.shipped.append(posting.posting_number)


class OzonIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = StateDB(Path(self.tmp.name) / "state.sqlite3")
        self.settings = SimpleNamespace(
            telegram_chat_ids=frozenset({123}),
            ozon_order_check_interval=30,
            ozon_stock_check_interval=300,
        )
        self.client = FakeOzon()
        self.tg = FakeTelegram()
        self.ozon = OzonIntegration(
            self.settings, self.client, self.tg, self.db
        )

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    async def test_initialize_saves_catalog_stocks_and_notifies_once(self):
        await self.ozon.initialize()

        catalog = self.db.get_channel_catalog("ozon")
        stocks = self.db.get_channel_stock(
            "ozon_fbs", ("SKU-A", "OZON-ONLY")
        )
        self.assertEqual(catalog["SKU-A"]["title"], "Alpha Ozon")
        self.assertEqual(catalog["OZON-ONLY"]["external_id"], "202")
        self.assertEqual(stocks, {"SKU-A": 5, "OZON-ONLY": 4})
        self.assertEqual(len(self.tg.broadcasts), 1)
        self.assertIn("Новый FBS-заказ OZON", self.tg.broadcasts[0][1])
        self.assertIn("SKU-A", self.tg.broadcasts[0][1])

        await self.ozon.refresh_orders()
        self.assertEqual(len(self.tg.broadcasts), 1)

    async def test_ship_callback_assembles_without_supply_and_is_idempotent(self):
        await self.ozon.initialize()
        posting_number = "12345678-0001-1"

        handled = await self.ozon.handle_callback(
            123,
            9,
            f"ozonord:{posting_number}:ship",
            "alert",
        )
        self.assertTrue(handled)
        self.assertEqual(self.client.shipped, [posting_number])
        self.assertEqual(self.ozon._state(posting_number), "assembled")
        self.assertNotIn(posting_number, self.ozon.current_pending)
        self.assertIn("OZON-заказ собран", self.tg.edits[-1][2])

        await self.ozon.handle_callback(
            123,
            10,
            f"ozonord:{posting_number}:ship",
            "alert2",
        )
        self.assertEqual(self.client.shipped, [posting_number])
        self.assertIn("уже собран", self.tg.edits[-1][2])

    async def test_ship_rechecks_remote_status_before_action(self):
        await self.ozon.initialize()
        posting_number = "12345678-0001-1"
        current = self.client.details[posting_number]
        self.client.details[posting_number] = OzonPosting(
            posting_number=current.posting_number,
            order_number=current.order_number,
            status="awaiting_deliver",
            cutoff=current.cutoff,
            warehouse_id=current.warehouse_id,
            products=current.products,
        )

        await self.ozon.handle_callback(
            123,
            9,
            f"ozonord:{posting_number}:ship",
            "alert",
        )

        self.assertEqual(self.client.shipped, [])
        self.assertEqual(
            self.ozon._state(posting_number),
            "processed:awaiting_deliver",
        )
        self.assertIn("уже не ожидает сборки", self.tg.edits[-1][2])

    async def test_skip_is_persistent_for_status_audit(self):
        await self.ozon.initialize()
        posting_number = "12345678-0001-1"
        await self.ozon.handle_callback(
            123,
            9,
            f"ozonord:{posting_number}:skip",
            "alert",
        )

        count = await self.ozon.audit_pending(123)

        self.assertEqual(count, 0)
        self.assertEqual(self.tg.sent, [])


class ParsingOzonClient(OzonClient):
    def __init__(self, responses):
        super().__init__("client", "key")
        self.responses = list(responses)
        self.requests = []

    async def _json(self, path, payload=None):
        self.requests.append((path, payload))
        if not self.responses:
            raise AssertionError((path, payload))
        expected_path, response = self.responses.pop(0)
        self.assert_path(path, expected_path)
        return response

    @staticmethod
    def assert_path(actual, expected):
        if actual != expected:
            raise AssertionError((actual, expected))


class OzonClientParsingTests(unittest.IsolatedAsyncioTestCase):
    async def test_stock_parser_uses_offer_id_and_fbs_present(self):
        client = ParsingOzonClient(
            [
                (
                    "/v4/product/info/stocks",
                    {
                        "items": [
                            {
                                "offer_id": "SKU-A",
                                "stocks": [
                                    {
                                        "type": "fbs",
                                        "present": 7,
                                        "reserved": 2,
                                    },
                                    {
                                        "type": "fbo",
                                        "present": 99,
                                        "reserved": 0,
                                    },
                                ],
                            }
                        ],
                        "cursor": "",
                        "has_next": False,
                    },
                )
            ]
        )

        stocks = await client.get_fbs_stocks()

        self.assertEqual(stocks, {"SKU-A": 7})

    async def test_posting_list_uses_ozon_max_limit_100(self):
        client = ParsingOzonClient(
            [
                (
                    "/v4/posting/fbs/list",
                    {
                        "postings": [],
                        "cursor": "",
                        "has_next": False,
                    },
                )
            ]
        )

        await client.get_awaiting_packaging()

        self.assertEqual(
            client.requests[0][1]["limit"],
            100,
        )

    async def test_warehouse_list_uses_v2_cursor_pagination(self):
        client = ParsingOzonClient(
            [
                (
                    "/v2/warehouse/list",
                    {
                        "result": {
                            "warehouses": [
                                {
                                    "warehouse_id": 10,
                                    "name": "First",
                                }
                            ],
                            "cursor": "next-page",
                            "has_next": True,
                        }
                    },
                ),
                (
                    "/v2/warehouse/list",
                    {
                        "result": {
                            "warehouses": [
                                {
                                    "warehouse_id": 20,
                                    "name": "Second",
                                }
                            ],
                            "cursor": "",
                            "has_next": False,
                        }
                    },
                ),
            ]
        )

        warehouses = await client.get_fbs_warehouses()

        self.assertEqual(
            [row["warehouse_id"] for row in warehouses],
            [10, 20],
        )
        self.assertEqual(
            client.requests[0],
            ("/v2/warehouse/list", {"limit": 100}),
        )
        self.assertEqual(
            client.requests[1],
            (
                "/v2/warehouse/list",
                {"limit": 100, "cursor": "next-page"},
            ),
        )

    async def test_ship_sends_single_package_and_verifies_status(self):
        client = ParsingOzonClient(
            [
                ("/v4/posting/fbs/ship", {"result": ["A"]}),
                (
                    "/v3/posting/fbs/get",
                    {
                        "result": {
                            "posting_number": "A",
                            "status": "awaiting_deliver",
                            "products": [
                                {
                                    "offer_id": "SKU-A",
                                    "name": "Alpha",
                                    "quantity": 2,
                                    "sku": 101,
                                }
                            ],
                        }
                    },
                ),
            ]
        )
        posting = OzonPosting(
            posting_number="A",
            order_number="1",
            status="awaiting_packaging",
            cutoff="",
            warehouse_id=1,
            products=(
                OzonPostingProduct("SKU-A", "Alpha", 2, 101),
            ),
        )

        await client.ship_fbs(posting)

        path, payload = client.requests[0]
        self.assertEqual(path, "/v4/posting/fbs/ship")
        self.assertEqual(payload["posting_number"], "A")
        self.assertEqual(
            payload["packages"],
            [
                {
                    "products": [
                        {"product_id": 101, "quantity": 2}
                    ]
                }
            ],
        )
        self.assertEqual(
            client.requests[1][0], "/v3/posting/fbs/get"
        )

    def test_posting_parser_accepts_offer_id_and_product_offer_id(self):
        first = OzonClient._parse_posting(
            {
                "posting_number": "A",
                "status": "awaiting_packaging",
                "products": [
                    {
                        "offer_id": "SKU-A",
                        "name": "Alpha",
                        "quantity": 2,
                        "sku": 10,
                    }
                ],
            }
        )
        second = OzonClient._parse_posting(
            {
                "posting_number": "B",
                "status": "awaiting_packaging",
                "products": [
                    {
                        "product_offer_id": "SKU-B",
                        "product_name": "Beta",
                        "quantity": 1,
                        "product_id": 20,
                    }
                ],
            }
        )

        self.assertEqual(first.products[0].offer_id, "SKU-A")
        self.assertEqual(first.products[0].quantity, 2)
        self.assertEqual(second.products[0].offer_id, "SKU-B")
        self.assertEqual(second.products[0].name, "Beta")


if __name__ == "__main__":
    unittest.main()
