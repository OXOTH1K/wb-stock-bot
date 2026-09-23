import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.main import wait_for_shutdown
from app.service import StockMonitorService


class BackgroundResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_loops_survive_api_and_telegram_failure(self):
        for loop_name, refresh_name in (
            ("fbs_loop", "refresh_fbs"),
            ("wb_loop", "refresh_wb"),
            ("catalog_loop", "refresh_catalog"),
        ):
            with self.subTest(loop=loop_name):
                service = StockMonitorService(
                    SimpleNamespace(
                        telegram_chat_ids={123}, fbs_check_interval=1,
                        wb_check_interval=1, catalog_refresh_interval=1,
                    ),
                    None,
                    SimpleNamespace(broadcast=AsyncMock(
                        side_effect=RuntimeError("Telegram unavailable")
                    )),
                    None,
                )
                refresh = AsyncMock(side_effect=[RuntimeError("API unavailable"), None])
                setattr(service, refresh_name, refresh)
                with patch("app.service.asyncio.sleep", new=AsyncMock(
                    side_effect=[None, None, asyncio.CancelledError()]
                )), self.assertLogs("app.service", level="ERROR"):
                    with self.assertRaises(asyncio.CancelledError):
                        await getattr(service, loop_name)()
                self.assertEqual(refresh.await_count, 2)
                service.tg.broadcast.assert_awaited_once()

    async def test_worker_failure_propagates(self):
        async def fail():
            raise ValueError("worker failure")

        task = asyncio.create_task(fail(), name="test-worker")
        with self.assertRaisesRegex(ValueError, "worker failure"):
            await wait_for_shutdown(asyncio.Event(), [task])

    async def test_unexpected_normal_exit_is_failure(self):
        task = asyncio.create_task(AsyncMock()(), name="test-worker")
        with self.assertRaisesRegex(RuntimeError, "test-worker stopped"):
            await wait_for_shutdown(asyncio.Event(), [task])

    async def test_shutdown_leaves_workers_for_caller_cleanup(self):
        stop = asyncio.Event()
        stop.set()
        worker = asyncio.create_task(asyncio.Event().wait())
        try:
            await wait_for_shutdown(stop, [worker])
            self.assertFalse(worker.done())
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
