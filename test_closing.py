import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx

import main
from closing import SnapshotStore, close_target
from config import SHANGHAI, Settings
from engine import Engine
from fetcher import demo_seed
from pre_market import MarketCalendar


def calendar(today):
    sessions = [date(2010, 1, 1) + timedelta(days=i) for i in range(20)]
    sessions += [date(2026, 11, i) for i in (16, 17, 18, 19, 20, 23)]
    return MarketCalendar(sessions=sessions, loaded_for=today, error=None)


class ClosingChecks(unittest.IsolatedAsyncioTestCase):
    def test_sector_memberships_reach_engine_without_changing_quote_values(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = main.Monitor(None, Settings(), watchlist_path=Path(directory) / 'watchlist.json')
            cache = monitor.fetcher.sectors
            cache.boards = {
                'hangye_test': {'kind': 'industry', 'name': '银行', 'codes': ['600000']},
                'gn_a': {'kind': 'concept', 'name': '概念甲', 'codes': ['600000']},
                'gn_b': {'kind': 'concept', 'name': '概念乙', 'codes': ['600000']}}
            cache._reindex()
            quote = demo_seed()[0]
            enriched = monitor.fetcher.with_metadata([quote])[0]
            self.assertEqual((enriched.price, enriched.main_inflow), (quote.price, quote.main_inflow))
            result = monitor.engines['live'].update([enriched], {}, datetime.now(SHANGHAI))
            self.assertEqual(result['leaders'][0]['industry'], '银行')
            self.assertEqual(set(result['leaders'][0]['concepts']), {'概念甲', '概念乙'})

    def test_targets_use_exchange_calendar_for_premarket_lunch_and_weekend(self):
        for now, expected in (
            ('2026-11-18T08:00:00+08:00', '2026-11-17T15:00:00+08:00'),
            ('2026-11-18T12:00:00+08:00', '2026-11-18T11:30:00+08:00'),
            ('2026-11-18T15:01:00+08:00', '2026-11-18T15:00:00+08:00'),
            ('2026-11-21T08:00:00+08:00', '2026-11-20T15:00:00+08:00')):
            stamp = datetime.fromisoformat(now)
            self.assertEqual(close_target(stamp, calendar(stamp.date())).isoformat(), expected)
        bad = calendar(date(2026, 11, 18))
        bad.error = 'unavailable'
        self.assertIsNone(close_target(datetime(2026, 11, 18, 16, tzinfo=SHANGHAI), bad))

    async def test_close_is_saved_and_reused_before_next_open_without_replaying_alerts(self):
        now = datetime(2026, 11, 18, 15, 1, tzinfo=SHANGHAI)
        quote = replace(demo_seed()[0], source_ts=now.replace(minute=0).timestamp())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'watchlist.json'
            monitor = main.Monitor(None, Settings(), watchlist_path=path)
            monitor.calendar = calendar(now.date())
            monitor.fetcher.fetch_market = AsyncMock(return_value=[quote])
            monitor.fetcher.fetch_books = AsyncMock(return_value={})
            monitor.notifier.publish = Mock(side_effect=AssertionError('closing must not alert'))
            await monitor.show_closed(now)
            saved = json.loads(json.dumps(monitor.snapshot))
            self.assertEqual(saved['status'], 'closed')
            self.assertFalse(saved['simulated'])
            self.assertEqual(saved['time'], '2026-11-18T15:00:00+08:00')
            self.assertEqual(saved['events'], [])
            self.assertTrue(saved['close_complete'])
            again = main.Monitor(None, Settings(), watchlist_path=path)
            next_day = now.replace(day=19, hour=8)
            again.calendar = calendar(next_day.date())
            again.fetcher.fetch_market = AsyncMock(side_effect=AssertionError('cache must suffice'))
            await again.show_closed(next_day)
            self.assertEqual(again.snapshot['time'], saved['time'])
            self.assertEqual(again.snapshot['leaders'], saved['leaders'])
            again.fetcher.fetch_market.assert_not_called()

    async def test_no_real_snapshot_never_falls_back_to_mock(self):
        now = datetime(2026, 11, 18, 16, tzinfo=SHANGHAI)
        with tempfile.TemporaryDirectory() as directory:
            monitor = main.Monitor(None, Settings(), watchlist_path=Path(directory) / 'watchlist.json')
            monitor.calendar = calendar(now.date())
            monitor.fetcher.fetch_market = AsyncMock(side_effect=httpx.ConnectError('offline'))
            await monitor.show_closed(now)
            self.assertEqual(monitor.snapshot['status'], 'waiting')
            self.assertEqual(monitor.snapshot['stock_count'], 0)
            self.assertFalse(monitor.snapshot['simulated'])
            self.assertFalse(hasattr(monitor, 'simulator'))

    def test_mock_and_corrupt_disk_snapshots_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SnapshotStore(Path(directory) / 'market_snapshot.json')
            for bad in ({'mode': 'mock', 'source': 'synthetic_demo_snapshot', 'simulated': True}, {}):
                with self.assertRaises(ValueError):
                    store.save(bad)
            store.path.write_text('{broken')
            self.assertIsNone(store.load())

    async def test_mock_setting_cannot_enable_simulated_production_polling(self):
        now = datetime(2026, 11, 18, 10, tzinfo=SHANGHAI)
        quote = replace(demo_seed()[0], source_ts=now.timestamp())
        with tempfile.TemporaryDirectory() as directory:
            cfg = replace(Settings(), mock_mode=True, poll_seconds=0.01, wechat_webhook_url='')
            monitor = main.Monitor(None, cfg, watchlist_path=Path(directory) / 'watchlist.json')
            monitor.initialize = AsyncMock()
            monitor.calendar = calendar(now.date())
            monitor.fetcher.fetch_market = AsyncMock(return_value=[quote])
            monitor.fetcher.fetch_books = AsyncMock(return_value={})
            queue = asyncio.Queue()
            monitor.clients.add(queue)
            with patch('main.datetime') as clock:
                clock.now.return_value = now
                task = asyncio.create_task(monitor.poll())
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=1)
                    self.assertEqual(payload['mode'], 'live')
                    self.assertFalse(payload['simulated'])
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
