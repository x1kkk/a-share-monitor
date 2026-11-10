"""Cached display must never refresh timestamps or advance the signal engine."""
import asyncio
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

import main
from config import Settings, SHANGHAI


class RecoveryLoopChecks(unittest.IsolatedAsyncioTestCase):
    async def check_recovery(self, age, expected):
        now = datetime(2026, 11, 10, 10, 30, tzinfo=SHANGHAI)
        cfg = replace(Settings(), mock_mode=False, http_proxy=None, http_proxy_backup=None,
                      wechat_webhook_url="", poll_seconds=0.01)
        with tempfile.TemporaryDirectory() as directory:
            monitor = main.Monitor(None, cfg, watchlist_path=Path(directory) / 'watchlist.json')
            monitor.mode = 'live'
            monitor.calendar = SimpleNamespace(session=lambda _: 'continuous', loaded_for=now.date(),
                                               error=None, streaks_ready=True)
            monitor.initialize = AsyncMock()
            monitor.fetcher.fetch_market = AsyncMock(side_effect=httpx.RemoteProtocolError('rotation'))
            monitor.engines['live'].update = Mock(side_effect=AssertionError('cached data entered engine'))
            old_time = (now - timedelta(seconds=age or 0)).isoformat()
            monitor.snapshot.update(mode='live', status='ok', time=old_time, stock_count=3050,
                                    leaders=[{'code': '600000', 'price': 10}])
            monitor.last_success_clock = time.monotonic() - age if age is not None else None
            queue = asyncio.Queue()
            monitor.clients.add(queue)
            with patch('main.datetime') as clock:
                clock.now.return_value = now
                task = asyncio.create_task(monitor.poll())
                try:
                    result = await asyncio.wait_for(queue.get(), timeout=1)
                    self.assertEqual(result['status'], expected)
                    self.assertTrue(result['using_cached_snapshot'])
                    self.assertEqual(result['time'], old_time)
                    self.assertEqual(result['leaders'], [{'code': '600000', 'price': 10}])
                    self.assertEqual(result['events'], [])
                    self.assertEqual(result['stock_count'], 3050)
                    monitor.engines['live'].update.assert_not_called()
                    if age is None:
                        self.assertIsNone(result['last_success_age_seconds'])
                    else:
                        self.assertGreaterEqual(result['last_success_age_seconds'], age)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_short_outage_keeps_cached_display_in_recovery(self):
        await self.check_recovery(1, 'recovering')

    async def test_long_outage_exposes_stale_data(self):
        await self.check_recovery(15, 'stale')

    async def test_without_success_there_is_no_recovery_grace(self):
        await self.check_recovery(None, 'stale')


if __name__ == '__main__':
    unittest.main()
