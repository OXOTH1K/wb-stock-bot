import asyncio
import unittest
from unittest.mock import AsyncMock

from app.telegram import TelegramBot


class TelegramPollingTests(unittest.IsolatedAsyncioTestCase):
    async def test_ack_timeout_does_not_drop_stock_callback(self):
        bot = TelegramBot("test")
        bot._call = AsyncMock(side_effect=[[
            {"update_id": 1, "callback_query": {
                "id": "callback", "data": "zero-all",
                "message": {"message_id": 2, "chat": {"id": 123}},
            }}
        ], asyncio.CancelledError()])
        bot.answer_callback_query = AsyncMock(side_effect=TimeoutError())
        handler = AsyncMock()
        with self.assertLogs("app.telegram", level="ERROR"):
            with self.assertRaises(asyncio.CancelledError):
                await bot.polling_loop(AsyncMock(), handler)
        handler.assert_awaited_once_with(123, 2, "zero-all", "")
