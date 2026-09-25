import unittest
from unittest.mock import AsyncMock, Mock

from app.ozon_client import OzonAPIError, OzonClient, OzonStockRateLimitError


class OzonClientResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_redirect_is_rejected_without_following_or_exposing_query(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                response = Mock(status=status, headers={
                    "Location": "https://user:secret@example.test/challenge?token=secret#secret"
                })
                context = AsyncMock()
                context.__aenter__.return_value = response
                client = OzonClient("client", "key")
                client._session = Mock()
                client._session.post.return_value = context
                with self.assertRaises(OzonAPIError) as caught:
                    await client._json("/v2/products/stocks", {"stocks": []})
                self.assertEqual(caught.exception.status, status)
                self.assertIn("https://example.test/challenge", str(caught.exception))
                self.assertNotIn("secret", str(caught.exception))
                self.assertEqual(client.session.post.call_count, 1)
                self.assertFalse(client.session.post.call_args.kwargs["allow_redirects"])

    async def test_stock_write_requires_confirmation_for_every_sku(self):
        for response in (None, {}, {"result": []}, {"result": [
            {"offer_id": "A", "updated": True}
        ]}, {"result": [
            {"offer_id": "A", "updated": True},
            {"offer_id": "B", "updated": False, "errors": [{"message": "rejected"}]},
        ]}):
            with self.subTest(response=response):
                client = OzonClient("client", "key")
                client._json = AsyncMock(return_value=response)
                with self.assertRaises(OzonAPIError):
                    await client.set_fbs_stocks(77, {"A": 2, "B": 3})

    async def test_confirmed_stock_write_succeeds(self):
        client = OzonClient("client", "key")
        client._json = AsyncMock(return_value={"result": [
            {"offer_id": "A", "updated": True, "errors": []},
            {"offer_id": "B", "updated": True, "errors": []},
        ]})
        await client.set_fbs_stocks(77, {"A": 2, "B": 3})

    async def test_item_frequency_error_identifies_affected_sku(self):
        client = OzonClient("client", "key")
        client._json = AsyncMock(return_value={"result": [
            {"offer_id": "A", "updated": True},
            {"offer_id": "B", "updated": False, "errors": [
                {"message": "Stock is updated too frequently"}
            ]},
        ]})
        with self.assertRaises(OzonStockRateLimitError) as caught:
            await client.set_fbs_stocks(77, {"A": 0, "B": 0})
        self.assertEqual(caught.exception.skus, {"B"})
