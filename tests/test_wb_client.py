import unittest
from unittest.mock import AsyncMock, patch

import aiohttp

from app.wb_client import WildberriesClient


class FakeResponse:
    status = 200

    async def text(self):
        return '{"ok": true}'

    async def json(self, content_type=None):
        return {"ok": True}


class FakeRequestContext:
    def __init__(self, outcome):
        self.outcome = outcome

    async def __aenter__(self):
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeSession:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def request(self, method, url, headers=None, **kwargs):
        self.calls += 1
        if not self.outcomes:
            raise AssertionError("Unexpected extra request")
        return FakeRequestContext(self.outcomes.pop(0))


class WildberriesClientRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_disconnect_is_retried_for_safe_read(self):
        client = WildberriesClient("token")
        session = FakeSession(
            [
                aiohttp.ServerDisconnectedError(),
                FakeResponse(),
            ]
        )
        client._session = session

        with patch(
            "app.wb_client.asyncio.sleep",
            new=AsyncMock(),
        ) as sleep:
            result = await client._json(
                "POST",
                "https://example.test/read",
                retry_transient=True,
                json={"read": True},
            )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(session.calls, 2)
        sleep.assert_awaited_once_with(0.5)

    async def test_disconnect_is_not_retried_without_opt_in(self):
        client = WildberriesClient("token")
        session = FakeSession(
            [aiohttp.ServerDisconnectedError()]
        )
        client._session = session

        with self.assertRaises(aiohttp.ServerDisconnectedError):
            await client._json(
                "PUT",
                "https://example.test/write",
                json={"write": True},
            )

        self.assertEqual(session.calls, 1)


if __name__ == "__main__":
    unittest.main()
