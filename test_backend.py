"""Focused regression checks: python -m unittest -v test_backend."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import main
from config import SHANGHAI, Settings
from engine import Engine, self_check
from fetcher import demo_seed, parse_quote
from pre_market import MarketCalendar


class StateChecks(unittest.TestCase):
    def test_engine_boundaries(self):
        self_check()

    def test_auction_parser_boundaries_and_invalid_prices(self):
        row = {"f12": "600000", "f14": "竞价测试", "f2": 10.2, "f3": 2,
               "f18": 10, "f21": 1e9,
               **{key: "-" for key in ("f5", "f6", "f8", "f62", "f66", "f72")}}
        for minute in (15, 24, 25, 29):
            now = datetime(2026, 11, 4, 9, minute, tzinfo=SHANGHAI)
            with self.subTest(minute=minute):
                quote = parse_quote(row, now=now)
                self.assertIsNotNone(quote)
                self.assertEqual(quote.volume, 0)
                for field in ("f2", "f18", "f3"):
                    self.assertIsNone(parse_quote({**row, field: "-"}, now=now))
        for minute in (14, 30):
            self.assertIsNone(parse_quote(row, now=now.replace(minute=minute)))


class BidirectionalChecks(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 26, 10, tzinfo=SHANGHAI)
        self.quote = replace(demo_seed()[0], price=10, pre_close=10, high=10,
                             main_inflow=0, volume=1_000_000, amount=2e8)

    def tick(self, engine, quotes, second):
        return engine.update(quotes, {}, self.now + timedelta(seconds=second), clock=second)

    def test_one_minute_speed_volume_ratio_and_cold_start(self):
        engine = Engine(Settings())
        for second in range(0, 121, 2):
            # Startup cumulative volume must not leak into the baseline. The
            # prior minute is 10 lots/s; current minute is 30 lots/s.
            volume = self.quote.volume + min(second, 60) * 10 + max(0, second - 60) * 30
            price = 10 + min(second, 60) * .001 - max(0, second - 60) * .002
            result = self.tick(engine, [replace(self.quote, price=price, volume=volume)], second)
            row = result['leaders'][0]
            if second < 120:
                self.assertIsNone(row['vol_ratio'])
        self.assertAlmostEqual(row['vol_ratio'], 3)
        self.assertAlmostEqual(row['speed_1m'], -1.2)
        self.assertEqual(row['window_1m_seconds'], 60)
        self.assertLessEqual(row['window_1m_seconds'], 60)
        self.assertNotEqual(row['speed_1m'], row['speed_2m'])
        # Cutover must discard both the price and volume history.
        result = self.tick(engine, [replace(self.quote, price=10.4, volume=2_000_000,
                                           main_inflow=1e9, flow_epoch=1)], 122)
        row = result['leaders'][0]
        self.assertIsNone(row['speed_1m'])
        self.assertEqual(row['window_1m_seconds'], 0)
        self.assertIsNone(row['vol_ratio'])
        self.assertEqual(result['events'], [])

    def test_zero_baseline_degrades_without_inventing_volume(self):
        engine = Engine(Settings())
        for second in range(0, 121, 2):
            self.tick(engine, [self.quote], second)
        result = self.tick(engine, [replace(self.quote, price=10.3, main_inflow=5e6)], 122)
        event = next(e for e in result['events'] if e['event_type'] == 'anomaly')
        self.assertIsNone(event['vol_ratio'])
        self.assertEqual(event['signal_type'], 'rapid_rise')
        self.assertNotIn('None', event['reason'])
        self.assertNotIn('NaN', event['reason'])
        self.assertNotIn('量比0', event['reason'])

    def test_ready_volume_baseline_enforces_threshold(self):
        engine = Engine(Settings())
        for second in range(0, 121, 2):
            self.tick(engine, [replace(self.quote, volume=self.quote.volume + second * 10)], second)
        result = self.tick(engine, [replace(self.quote, price=10.3, main_inflow=5e6,
                                           volume=self.quote.volume + 122 * 10)], 122)
        self.assertAlmostEqual(result['leaders'][0]['vol_ratio'], 1)
        self.assertEqual(result['anomalies'], [])
        self.assertEqual(result['events'], [])

    def test_direction_limits_scores_and_protocol_are_independent(self):
        # 12 candidates per direction exercise both independent admission caps.
        seeds = [replace(self.quote, code=f'600{i:03d}') for i in range(24)]
        changed = [replace(q, price=10.3 if i < 12 else 9.7,
                           main_inflow=5e6 if i < 12 else -5e6)
                   for i, q in enumerate(seeds)]
        engine = Engine(Settings())
        self.tick(engine, seeds, 0)
        result = self.tick(engine, changed, 2)
        counts = {direction: sum(r['direction'] == direction for r in result['anomalies'])
                  for direction in ('bullish', 'bearish')}
        self.assertEqual(counts, {'bullish': 10, 'bearish': 10})
        self.assertEqual(result['pending_count'], 4)
        self.assertEqual(len(result['events']), 20)
        for event in result['events']:
            bullish = event['direction'] == 'bullish'
            self.assertEqual(event['signal_type'], 'rapid_rise' if bullish else 'heavy_plunge')
            self.assertEqual(event['signal_label'], '急速拉升' if bullish else '放量跳水')
            self.assertIn(event['signal_label'], event['signal_tags'])
            self.assertIn(event['signal_label'], event['reason'])
            self.assertEqual(event['alert_level'], 2)
        # Changing the other direction's population cannot change the score.
        bullish_only = Engine(Settings())
        self.tick(bullish_only, seeds[:12], 0)
        self.tick(bullish_only, changed[:12], 2)
        for q in seeds[:12]:
            self.assertEqual(engine.rows[q.code]['score'], bullish_only.rows[q.code]['score'])
        bearish_only = Engine(Settings())
        self.tick(bearish_only, seeds[12:], 0)
        self.tick(bearish_only, changed[12:], 2)
        for q in seeds[12:]:
            self.assertEqual(engine.rows[q.code]['score'], bearish_only.rows[q.code]['score'])

    def test_plunge_at_lower_limit_is_not_an_ordinary_anomaly(self):
        engine = Engine(Settings())
        self.tick(engine, [self.quote], 0)
        result = self.tick(engine, [replace(self.quote, price=9, main_inflow=-5e6)], 2)
        self.assertEqual(result['anomalies'], [])
        self.assertFalse(any(e['event_type'] == 'anomaly' for e in result['events']))

    def test_simultaneous_break_and_plunge_keep_distinct_signal_protocols(self):
        engine = Engine(Settings())
        sealed = replace(self.quote, price=11, high=11, main_inflow=1e7)
        self.tick(engine, [sealed], 0)
        result = self.tick(engine, [replace(sealed, price=10.7, main_inflow=5e6)], 2)
        self.assertEqual(len(result['events']), 2)
        broken = next(e for e in result['events'] if e['event_type'] == 'break_open')
        plunge = next(e for e in result['events'] if e['event_type'] == 'anomaly')
        self.assertEqual((broken['signal_type'], broken['alert_level']), ('break_open', 3))
        self.assertEqual((plunge['signal_type'], plunge['alert_level']), ('heavy_plunge', 2))
        self.assertIn('放量跳水', plunge['reason'])
        self.assertEqual(result['anomalies'][0]['signal_type'], 'heavy_plunge')
        self.assertEqual(result['anomalies'][0]['reason'], plunge['reason'])

    def test_counter_rollback_and_gap_clear_new_windows(self):
        for reset_kind in ('volume', 'amount', 'gap'):
            with self.subTest(reset_kind=reset_kind):
                engine = Engine(Settings())
                for second in range(0, 121, 2):
                    q = replace(self.quote, volume=self.quote.volume + second * 10,
                                amount=self.quote.amount + second * 1000)
                    self.tick(engine, [q], second)
                second = 140 if reset_kind == 'gap' else 122
                changes = {'price': 9.6, 'main_inflow': -1e7}
                if reset_kind == 'volume':
                    changes['volume'] = 10
                elif reset_kind == 'amount':
                    changes['amount'] = 10
                result = self.tick(engine, [replace(q, **changes)], second)
                row = result['leaders'][0]
                self.assertIsNone(row['vol_ratio'])
                self.assertIsNone(row['speed_1m'])
                self.assertEqual(result['events'], [])

    def test_bearish_urgency_and_missing_quote_do_not_distort_ranks(self):
        seeds = [replace(self.quote, code='600001'), replace(self.quote, code='600002')]
        engine = Engine(Settings())
        self.tick(engine, seeds, 0)
        changed = [replace(seeds[0], price=9.7, main_inflow=-3e6),
                   replace(seeds[1], price=9.5, main_inflow=-8e6)]
        result = self.tick(engine, changed, 2)
        self.assertGreater(engine.rows['600002']['score'], engine.rows['600001']['score'])
        self.assertEqual(result['anomalies'][0]['code'], '600002')
        result = self.tick(engine, changed[:1], 4)
        missing = next(r for r in result['anomalies'] if r['code'] == '600002')
        self.assertTrue(missing['quote_missing'])
        self.assertEqual(missing['age_seconds'], 2)
        # Only one fresh bearish candidate remains; a missing stale row must
        # not remain in the fresh cross-sectional population.
        alone = Engine(Settings())
        self.tick(alone, seeds[:1], 0)
        self.tick(alone, changed[:1], 2)
        self.tick(alone, changed[:1], 4)
        self.assertEqual(engine.rows['600001']['score'], alone.rows['600001']['score'])


class LiveLoopChecks(unittest.IsolatedAsyncioTestCase):
    async def test_ws_cancellation_during_accept(self):
        monitor = main.Monitor(None, Settings())
        socket = SimpleNamespace(headers={},
                                 app=SimpleNamespace(state=SimpleNamespace(monitor=monitor)),
                                 accept=AsyncMock(side_effect=asyncio.CancelledError))
        await main.websocket(socket)
        self.assertFalse(monitor.clients)

    async def test_auction_quiet_old_quote_is_not_filtered(self):
        cfg = replace(Settings(), mock_mode=False, wechat_webhook_url="", api_token="", poll_seconds=0.01)
        monitor = main.Monitor(None, cfg)
        now = datetime(2026, 11, 4, 9, 27, tzinfo=SHANGHAI)
        # 09:25 price with no new update at 09:27; volume is still zero.
        quiet = replace(demo_seed()[0], volume=0, source_ts=(now - timedelta(minutes=2)).timestamp())
        monitor.calendar = MarketCalendar(sessions=[quiet.listed + timedelta(days=i) for i in range(20)]
                                          + [now.date()], loaded_for=now.date(), error=None)
        monitor.initialize = AsyncMock()
        monitor.fetcher.fetch_market = AsyncMock(return_value=[quiet])
        monitor.fetcher.fetch_books = AsyncMock(return_value={})
        queue = asyncio.Queue()
        monitor.clients.add(queue)
        with patch("main.datetime") as clock:
            clock.now.return_value = now
            task = asyncio.create_task(monitor.poll())
            try:
                result = await asyncio.wait_for(queue.get(), timeout=1)
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["mode"], "live")
                self.assertEqual(result["stock_count"], 1)
                self.assertEqual(result["leaders"][0]["source_ts"], quiet.source_ts)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_ws_server_cancellation_joins_children(self):
        cfg = replace(Settings(), wechat_webhook_url="", api_token="")
        monitor = main.Monitor(None, cfg)
        sent = asyncio.Event()

        class Socket:
            headers = {}
            app = SimpleNamespace(state=SimpleNamespace(monitor=monitor))

            async def accept(self):
                pass

            async def send_json(self, data):
                sent.set()

            async def receive_text(self):
                await asyncio.Event().wait()

            async def close(self, **kwargs):
                pass

        before = set(asyncio.all_tasks())
        with patch.object(main, "settings", cfg):
            task = asyncio.create_task(main.websocket(Socket()))
            await asyncio.wait_for(sent.wait(), timeout=1)
            await asyncio.sleep(0)  # permit send/receive children to start
            task.cancel()
            await asyncio.wait_for(task, timeout=1)
        await asyncio.sleep(0)
        self.assertFalse(task.cancelled())
        self.assertFalse(monitor.clients)
        self.assertFalse(set(asyncio.all_tasks()) - before)


class WebSocketChecks(unittest.TestCase):
    def test_tencent_observation_date_is_serializable_over_websocket(self):
        cfg = replace(Settings(), wechat_webhook_url="", api_token="date-regression-token")
        now = datetime.now(SHANGHAI)
        quote = replace(demo_seed()[0], observed_since=now.date())
        with tempfile.TemporaryDirectory() as directory:
            monitor = main.Monitor(None, cfg, watchlist_path=Path(directory) / 'watchlist.json')
            monitor.broadcast(monitor.engines['live'].update([quote], {}, now))
            self.assertEqual(monitor.snapshot['leaders'][0]['observed_since'], now.date().isoformat())
            json.dumps(monitor.snapshot)

            @asynccontextmanager
            async def lifespan(app):
                app.state.monitor = monitor
                yield

            with patch.object(main, 'settings', cfg), patch.object(main.app.router, 'lifespan_context', lifespan):
                with TestClient(main.app) as client:
                    with client.websocket_connect('/ws', headers={'origin': 'http://localhost:5173'}) as ws:
                        ws.send_json({'token': cfg.api_token})
                        snapshot = ws.receive_json()
                        self.assertEqual(snapshot['leaders'][0]['observed_since'], now.date().isoformat())
                        self.assertEqual(snapshot['leaders'][0]['listed'], quote.listed.isoformat())

    def test_real_asgi_disconnect_and_authenticated_weights(self):
        cfg = replace(Settings(), wechat_webhook_url="", api_token="regression-token")
        monitor = main.Monitor(None, cfg)

        @asynccontextmanager
        async def lifespan(app):
            app.state.monitor = monitor
            yield

        with patch.object(main, "settings", cfg), patch.object(main.app.router, "lifespan_context", lifespan):
            with TestClient(main.app) as client:
                for _ in range(3):
                    with client.websocket_connect("/ws", headers={"origin": "http://localhost:5173"}) as ws:
                        ws.send_json({"token": cfg.api_token})
                        snapshot = ws.receive_json()
                        self.assertEqual(snapshot["events"], [])
                        ws.send_text("ping")
                    self.assertFalse(monitor.clients)
                weights = dict(monitor.engines["live"].weights)
                self.assertEqual(client.put("/api/weights", json=weights).status_code, 401)
                headers = {"authorization": "Bearer " + cfg.api_token, "origin": "http://localhost:5173"}
                response = client.put("/api/weights", headers=headers, json={**weights, "speed_2m": 70})
                self.assertEqual(response.status_code, 200)
                self.assertAlmostEqual(sum(response.json()["weights"].values()), 100)
                self.assertEqual(client.put("/api/weights", headers=headers,
                                           json={key: 0 for key in weights}).status_code, 422)
                self.assertEqual(client.put("/api/weights", headers={**headers, "origin": "https://evil.invalid"},
                                           json=weights).status_code, 403)


if __name__ == "__main__":
    unittest.main()
