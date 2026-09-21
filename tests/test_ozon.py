import tempfile
import unittest
from unittest.mock import AsyncMock, patch
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
        self.fbo_stocks = {"SKU-A": 0, "OZON-ONLY": 0}
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
        return dict(self.stocks), dict(self.fbo_stocks)

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


class FakeInventory:
    def __init__(self, db, local=None, available=None):
        self.db = db
        self.local = dict(local or {})
        self.available = dict(available or {})
        self.set_calls = []
        self.suppress_calls = []
        self.restore_calls = []
        self.snapshots = {}

    def local_quantity(self, sku):
        return int(self.local.get(sku, 0))

    def available_quantity(self, sku):
        return int(self.available.get(sku, 0))

    def is_suppressed(self, channel, sku):
        return self.db.is_channel_suppressed(channel, sku)

    async def set_local_stock(self, sku, quantity, reason="test"):
        self.local[str(sku)] = int(quantity)
        return int(quantity)

    async def set_available_stock(
        self, sku, quantity, reason="test", **kwargs
    ):
        sku = str(sku)
        quantity = int(quantity)
        local = self.local_quantity(sku)
        if quantity > local:
            raise ValueError("Доступно больше физического остатка")
        self.available[sku] = quantity
        self.set_calls.append((sku, quantity, reason))
        self.db.set_channel_stock("ozon_fbs", sku, quantity)
        return quantity

    async def suppress_channel(self, channel, sku, reason):
        sku = str(sku)
        self.suppress_calls.append((channel, sku, reason))
        self.db.set_channel_suppressed(channel, sku, reason)
        if channel == "ozon":
            self.db.set_channel_stock("ozon_fbs", sku, 0)

    async def restore_channel(self, channel, sku):
        sku = str(sku)
        quantity = self.available_quantity(sku)
        self.restore_calls.append((channel, sku, quantity))
        self.db.clear_channel_suppressed(channel, sku)
        if channel == "ozon":
            self.db.set_channel_stock(
                "ozon_fbs", sku, quantity
            )
        return quantity

    async def sync_sku(
        self, sku, raise_errors=True, force=False, **kwargs
    ):
        sku = str(sku)
        target = (
            0
            if self.db.get_channel_suppression_reason(
                "ozon", sku
            )
            == "marketplace_stock"
            else self.available_quantity(sku)
        )
        self.db.set_channel_stock("ozon_fbs", sku, target)


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

    async def test_auto_selects_only_active_fbs_warehouse(self):
        async def warehouses():
            return [
                {
                    "warehouse_id": 10,
                    "name": "Old archived",
                    "status": "disabled",
                },
                {
                    "warehouse_id": 77,
                    "name": "Current FBS",
                    "status": "created",
                },
                {
                    "warehouse_id": 99,
                    "name": "Blocked",
                    "status": "blocked",
                },
            ]

        self.client.get_fbs_warehouses = warehouses

        warehouse_id = await self.ozon._resolve_warehouse()

        self.assertEqual(warehouse_id, 77)
        self.assertEqual(self.ozon.warehouse_id, 77)

    async def test_multiple_active_fbs_warehouses_still_require_configuration(self):
        async def warehouses():
            return [
                {
                    "warehouse_id": 10,
                    "name": "FBS A",
                    "status": "created",
                },
                {
                    "warehouse_id": 20,
                    "name": "FBS B",
                    "status": "created",
                },
                {
                    "warehouse_id": 30,
                    "name": "Archived",
                    "status": "disabled",
                },
            ]

        self.client.get_fbs_warehouses = warehouses

        warehouse_id = await self.ozon._resolve_warehouse()

        self.assertIsNone(warehouse_id)
        self.assertIsNone(self.ozon.warehouse_id)

    async def test_stocks_ozon_command_shows_fbs_and_fbo(self):
        self.client.fbo_stocks["SKU-A"] = 8

        handled = await self.ozon.handle_message(
            123, "/stocks_ozon"
        )

        self.assertTrue(handled)
        self.assertGreaterEqual(len(self.tg.sent), 2)
        text = self.tg.sent[-1][1]
        self.assertIn("Остатки OZON", text)
        self.assertIn("FBS", text)
        self.assertIn("FBO", text)
        self.assertIn("SKU-A", text)

    async def test_fbo_appearance_offers_zeroing_ozon_fbs(self):
        inventory = FakeInventory(
            self.db,
            local={"SKU-A": 0, "OZON-ONLY": 0},
            available={"SKU-A": 5, "OZON-ONLY": 4},
        )
        self.ozon.set_shared_inventory(inventory)
        await self.ozon.refresh_catalog_and_stocks(
            notify=False
        )
        self.client.fbo_stocks["SKU-A"] = 2

        await self.ozon.refresh_catalog_and_stocks()

        alerts = [
            item
            for item in self.tg.broadcasts
            if "Товар появился на складе OZON" in item[1]
        ]
        self.assertEqual(len(alerts), 1)
        self.assertIn("SKU-A", alerts[0][1])
        keyboard = alerts[0][2]["reply_markup"]
        self.assertIn(
            "ozstock:101:zero",
            str(keyboard),
        )

    async def test_fbo_zero_action_suppresses_only_ozon_fbs(self):
        inventory = FakeInventory(
            self.db,
            local={"SKU-A": 2, "OZON-ONLY": 0},
            available={"SKU-A": 5, "OZON-ONLY": 4},
        )
        self.ozon.set_shared_inventory(inventory)
        await self.ozon.refresh_catalog_and_stocks(
            notify=False
        )
        self.client.fbo_stocks["SKU-A"] = 2
        await self.ozon.refresh_catalog_and_stocks()

        handled = await self.ozon.handle_callback(
            123,
            9,
            "ozstock:101:zero",
            "alert",
        )

        self.assertTrue(handled)
        self.assertEqual(
            inventory.available_quantity("SKU-A"), 5
        )
        self.assertEqual(
            inventory.local_quantity("SKU-A"), 2
        )
        self.assertEqual(
            self.db.get_channel_stock(
                "ozon_fbs", ("SKU-A",)
            )["SKU-A"],
            0,
        )
        self.assertEqual(
            self.db.get_channel_suppression_reason(
                "ozon", "SKU-A"
            ),
            "marketplace_stock",
        )
        self.assertIn(
            "OZON FBS обнулён",
            self.tg.edits[-1][2],
        )

    async def test_total_ozon_depletion_offers_transfer_to_available(self):
        inventory = FakeInventory(
            self.db,
            local={"SKU-A": 5, "OZON-ONLY": 0},
            available={"SKU-A": 0, "OZON-ONLY": 4},
        )
        self.ozon.set_shared_inventory(inventory)
        await self.ozon.refresh_catalog_and_stocks(
            notify=False
        )
        self.client.stocks["SKU-A"] = 0
        self.client.fbo_stocks["SKU-A"] = 0

        await self.ozon.refresh_catalog_and_stocks()

        alerts = [
            item
            for item in self.tg.broadcasts
            if "закончился в OZON FBS и FBO" in item[1]
        ]
        self.assertEqual(len(alerts), 1)
        self.assertIn("ozstock:101:add1", str(alerts[0][2]))
        self.assertIn("ozstock:101:add5", str(alerts[0][2]))

        await self.ozon.refresh_catalog_and_stocks()
        alerts_again = [
            item
            for item in self.tg.broadcasts
            if "закончился в OZON FBS и FBO" in item[1]
        ]
        self.assertEqual(len(alerts_again), 1)

    async def test_depletion_add_button_sets_shared_available_stock(self):
        inventory = FakeInventory(
            self.db,
            local={"SKU-A": 5, "OZON-ONLY": 0},
            available={"SKU-A": 0, "OZON-ONLY": 4},
        )
        self.ozon.set_shared_inventory(inventory)
        await self.ozon.refresh_catalog_and_stocks(
            notify=False
        )
        self.client.stocks["SKU-A"] = 0
        self.client.fbo_stocks["SKU-A"] = 0
        await self.ozon.refresh_catalog_and_stocks(
            notify=False
        )

        handled = await self.ozon.handle_callback(
            123,
            9,
            "ozstock:101:add5",
            "alert",
        )

        self.assertTrue(handled)
        self.assertEqual(
            inventory.set_calls[-1],
            ("SKU-A", 5, "telegram_ozon_stock_add"),
        )

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

    async def test_ship_waits_for_async_status_propagation_without_resending(self):
        awaiting = {
            "result": {
                "posting_number": "A",
                "status": "awaiting_packaging",
                "products": [
                    {
                        "offer_id": "SKU-A",
                        "name": "Alpha",
                        "quantity": 2,
                        "sku": 101,
                    }
                ],
            }
        }
        delivered = {
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
        }
        client = ParsingOzonClient(
            [
                ("/v4/posting/fbs/ship", {"result": ["A"]}),
                ("/v3/posting/fbs/get", awaiting),
                ("/v3/posting/fbs/get", awaiting),
                ("/v3/posting/fbs/get", awaiting),
                ("/v3/posting/fbs/get", delivered),
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

        with patch(
            "app.ozon_client.asyncio.sleep",
            new=AsyncMock(),
        ) as sleep:
            await client.ship_fbs(posting)

        self.assertEqual(
            [path for path, _ in client.requests].count(
                "/v4/posting/fbs/ship"
            ),
            1,
        )
        self.assertEqual(
            [path for path, _ in client.requests].count(
                "/v3/posting/fbs/get"
            ),
            4,
        )
        self.assertEqual(
            [call.args[0] for call in sleep.await_args_list],
            [0.5, 1.0, 2.0],
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
