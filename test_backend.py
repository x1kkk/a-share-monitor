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
from fetcher import Book, demo_seed, parse_quote
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


class OrderbookSignalChecks(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 26, 10, tzinfo=SHANGHAI)
        self.quote = replace(demo_seed()[0], price=10, pre_close=10, high=10, pct=0,
                             volume=1_000_000, amount=2e8, main_inflow=0,
                             outer_vol=500_000, inner_vol=300_000)

    def quote_at(self, second, **values):
        current = self.now + timedelta(seconds=second)
        return replace(self.quote, volume=self.quote.volume + int(second * 10),
                       amount=self.quote.amount + second * 1000,
                       source_ts=current.timestamp(), **values)

    def book(self, quote, second, support=True):
        bids = ((9.99, 100), (9.98, 15000 if support else 100),
                (9.97, 100), (9.96, 100), (9.95, 100))
        return Book(quote.price, 100, 100, (self.now + timedelta(seconds=second)).timestamp(),
                    bids=bids, asks=((10.01, 100), (10.02, 100), (10.03, 100),
                                     (10.04, 100), (10.05, 100)))

    def tick(self, engine, second, *, quote=None, support=True, missing_book=False):
        quote = quote or self.quote_at(second)
        books = {} if missing_book else {quote.code: self.book(quote, second, support)}
        return engine.update([quote], books, self.now + timedelta(seconds=second), clock=second)

    @staticmethod
    def signals(result, kind=None):
        events = result['events']
        return [e for e in events if e['signal_type'] == kind] if kind else events

    def test_support_waits_full_thirty_seconds_and_emits_once_per_cycle(self):
        engine = Engine(Settings())
        for second in range(0, 30, 2):
            self.assertEqual(self.signals(self.tick(engine, second), 'large_order_support'), [])
        self.assertEqual(self.signals(self.tick(engine, 29.999), 'large_order_support'), [])
        result = self.tick(engine, 30)
        event = self.signals(result, 'large_order_support')[0]
        self.assertEqual(event['direction'], 'bullish')
        self.assertFalse(event['signal_estimated'])
        self.assertEqual(event['alert_level'], 1)
        self.assertEqual(event['support_info']['level'], 2)
        self.assertAlmostEqual(event['support_info']['price'], 9.98)
        self.assertAlmostEqual(event['support_info']['amount'], 14_970_000)
        self.assertGreaterEqual(event['support_info']['duration_seconds'], 30)
        self.assertIn('挂单', event['reason'])
        self.assertNotIn('None', event['reason'])
        for second in (32, 34, 36):
            self.assertEqual(self.signals(self.tick(engine, second), 'large_order_support'), [])
        self.assertEqual(len(result['anomalies']), 1)
        self.tick(engine, 38, support=False)
        for second in range(40, 70, 2):
            self.assertEqual(self.signals(self.tick(engine, second), 'large_order_support'), [])
        self.assertEqual(len(self.signals(self.tick(engine, 70), 'large_order_support')), 1)

    def test_support_cancellation_missing_quotes_books_and_epoch_restart_duration(self):
        for interruption in ('cancel', 'missing_quote', 'missing_book', 'epoch'):
            with self.subTest(interruption=interruption):
                engine = Engine(Settings())
                for second in range(0, 18, 2):
                    self.tick(engine, second)
                if interruption == 'missing_quote':
                    engine.update([], {}, self.now + timedelta(seconds=18), clock=18)
                else:
                    self.tick(engine, 18, support=interruption != 'cancel',
                              missing_book=interruption == 'missing_book',
                              quote=self.quote_at(18, flow_epoch=1) if interruption == 'epoch' else None)
                for second in range(20, 48, 2):
                    quote = self.quote_at(second, flow_epoch=1 if interruption == 'epoch' else 0)
                    result = self.tick(engine, second, quote=quote)
                    self.assertEqual(self.signals(result, 'large_order_support'), [])
                final_second = 48 if interruption == 'epoch' else 50
                for second in range(48, final_second + 1, 2):
                    result = self.tick(engine, second,
                                       quote=self.quote_at(second, flow_epoch=1 if interruption == 'epoch' else 0))
                self.assertEqual(len(self.signals(result, 'large_order_support')), 1)

    def test_long_gap_restarts_support_duration(self):
        engine = Engine(Settings())
        for second in range(0, 22, 2): self.tick(engine, second)
        self.assertEqual(self.signals(self.tick(engine, 32), 'large_order_support'), [])
        for second in range(34, 62, 2):
            self.assertEqual(self.signals(self.tick(engine, second), 'large_order_support'), [])
        self.assertEqual(len(self.signals(self.tick(engine, 62), 'large_order_support')), 1)

    def test_support_prominence_needs_comparison_and_includes_exact_fivefold_boundary(self):
        for bids, threshold, expected in (
                (((9.98,15000),),1000,False),
                (((9.99,15000),(9.98,15000),(9.97,15000)),1000,False),
                (((9.99,2000),(9.98,10000),(9.97,2000)),900,True)):
            with self.subTest(bids=bids):
                engine=Engine(Settings())
                engine.set_filters({'support_min_amount_wan':threshold})
                for second in range(0,32,2):
                    quote=self.quote_at(second)
                    book=Book(quote.price,bids[0][1],100,(self.now+timedelta(seconds=second)).timestamp(),
                              bids=bids,asks=((10.01,100),))
                    result=engine.update([quote],{quote.code:book},self.now+timedelta(seconds=second),clock=second)
                self.assertEqual(bool(self.signals(result,'large_order_support')),expected)

    def test_st_limits_use_historical_five_and_current_ten_percent(self):
        from config import stock_limit_prices
        for day, name, upper, lower in (
                ('2026-07-03', '*sT测试', 10.5, 9.5),
                ('2026-07-06', '*ST测试', 11.0, 9.0),
                ('2026-11-26', 'ST测试', 11.0, 9.0),
                ('2026-07-03', '普通股票', 11.0, 9.0)):
            with self.subTest(day=day, name=name):
                now = datetime.fromisoformat(day + 'T10:00:00+08:00')
                self.assertEqual(stock_limit_prices(10, name, now.date()), (upper, lower))
                engine = Engine(Settings())
                baseline = replace(self.quote, name=name, source_ts=now.timestamp())
                engine.update([baseline], {}, now, clock=0)
                sealed = replace(baseline, price=upper, high=upper,
                                 source_ts=(now + timedelta(seconds=2)).timestamp())
                for second in range(2, 36, 2):
                    book = Book(upper, 30000, 0, (now + timedelta(seconds=second)).timestamp(),
                                bids=((upper, 30000), (upper-.01, 100)), asks=())
                    result = engine.update([sealed], {sealed.code: book}, now + timedelta(seconds=second), clock=second)
                    self.assertEqual(self.signals(result, 'large_order_support'), [])
                    self.assertTrue(result['leaders'][0]['is_limit_up'])
                self.assertEqual(result['leaders'][0]['limit_up_price'], upper)
                self.assertEqual(result['leaders'][0]['limit_down_price'], lower)
                # A price above the historical ST floor is not enough: the
                # exact date-aware floor excludes a false smash signal.
                engine = Engine(Settings())
                engine.update([baseline], {}, now, clock=0)
                down = replace(baseline, price=lower, pct=(lower/10-1)*100,
                               amount=baseline.amount+6e6, volume=baseline.volume+8000,
                               main_inflow=-6e6, inner_vol=baseline.inner_vol+7000,
                               outer_vol=baseline.outer_vol+1000,
                               source_ts=(now + timedelta(seconds=2)).timestamp())
                result = engine.update([down], {}, now + timedelta(seconds=2), clock=2)
                self.assertEqual(self.signals(result, 'suspected_large_smash'), [])

    def test_bursts_require_price_cash_and_active_side_with_honest_labels(self):
        for bearish in (False, True):
            with self.subTest(bearish=bearish):
                engine = Engine(Settings())
                self.tick(engine, 0, support=False)
                q = replace(self.quote, price=9.96 if bearish else 10.04,
                            pct=-.4 if bearish else .4, amount=self.quote.amount+6e6,
                            volume=self.quote.volume+8000,
                            inner_vol=self.quote.inner_vol+(7000 if bearish else 1000),
                            outer_vol=self.quote.outer_vol+(1000 if bearish else 7000),
                            main_inflow=5e6 if bearish else -5e6,
                            source_ts=(self.now+timedelta(seconds=2)).timestamp())
                result = self.tick(engine, 2, quote=q, support=False)
                kind = 'suspected_large_smash' if bearish else 'suspected_active_buy'
                event = self.signals(result, kind)[0]
                self.assertTrue(event['signal_estimated'])
                self.assertEqual(event['alert_level'], 1)
                self.assertIn('疑似', event['signal_label'])
                self.assertIn('快照估算', event['reason'])
                self.assertEqual(event['direction'], 'bearish' if bearish else 'bullish')
                self.assertIn('内盘' if bearish else '外盘', event['reason'])
                self.assertAlmostEqual(result['leaders'][0]['outer_inner_ratio'],
                                       round(q.outer_vol/q.inner_vol, 2))

    def test_snapshot_burst_thresholds_use_inclusive_cash_price_time_and_strict_dominance(self):
        for bearish in (False, True):
            for seconds, cash, move, dominant, expected in (
                    (6, 5e6, .3, 1501, True),
                    (6.001, 5e6, .3, 1501, False),
                    (2, 5e6-1, .3, 1501, False),
                    (2, 5e6, .299, 1501, False),
                    (2, 5e6, .3, 1500, False)):
                with self.subTest(bearish=bearish, seconds=seconds, cash=cash,
                                  move=move, dominant=dominant):
                    engine = Engine(Settings())
                    self.tick(engine, 0, support=False)
                    sign = -1 if bearish else 1
                    quote = replace(self.quote, price=10*(1+sign*move/100),
                                    pct=sign*move, amount=self.quote.amount+cash,
                                    volume=self.quote.volume+3000,
                                    outer_vol=self.quote.outer_vol+(1000 if bearish else dominant),
                                    inner_vol=self.quote.inner_vol+(dominant if bearish else 1000),
                                    main_inflow=sign*1e6,
                                    source_ts=(self.now+timedelta(seconds=seconds)).timestamp())
                    result = self.tick(engine, seconds, quote=quote, support=False)
                    kind = 'suspected_large_smash' if bearish else 'suspected_active_buy'
                    self.assertEqual(bool(self.signals(result, kind)), expected)

    def test_same_round_rapid_and_snapshot_buy_keep_both_timeline_records(self):
        engine=Engine(Settings())
        self.tick(engine,0,support=False)
        q=replace(self.quote,price=10.3,pct=3,amount=self.quote.amount+6e6,
                  volume=self.quote.volume+8000,outer_vol=self.quote.outer_vol+7000,
                  inner_vol=self.quote.inner_vol+1000,main_inflow=5e6,
                  source_ts=(self.now+timedelta(seconds=2)).timestamp())
        result=self.tick(engine,2,quote=q,support=False)
        self.assertEqual({e['signal_type'] for e in result['events']},
                         {'rapid_rise','suspected_active_buy'})
        self.assertEqual(result['minute_admitted_by_direction']['bullish'],1)
        self.assertEqual(len(result['anomalies']),1)
        self.assertEqual({item['signal_type'] for item in result['anomalies'][0]['timeline']},
                         {'rapid_rise','suspected_active_buy'})

    def test_missing_active_side_uses_main_flow_and_first_frame_never_bursts(self):
        engine = Engine(Settings())
        q = replace(self.quote, amount=5e9, outer_vol=None, inner_vol=None)
        result = self.tick(engine, 0, quote=q, support=False)
        self.assertEqual(self.signals(result), [])
        q = replace(q, price=10.04, pct=.4, amount=q.amount+6e6,
                    volume=q.volume+8000, main_inflow=4e6,
                    source_ts=(self.now+timedelta(seconds=2)).timestamp())
        result = self.tick(engine, 2, quote=q, support=False)
        event = self.signals(result, 'suspected_active_buy')[0]
        self.assertIn('主力', event['reason'])
        self.assertIn('降级', event['reason'])
        self.assertIsNone(result['leaders'][0]['outer_inner_ratio'])

    def test_reset_or_overlong_pair_never_creates_snapshot_burst(self):
        for cause in ('epoch', 'amount', 'volume', 'active_counter', 'gap', 'over_six_seconds', 'missing_quote'):
            with self.subTest(cause=cause):
                engine = Engine(Settings())
                self.tick(engine, 0, support=False)
                if cause == 'missing_quote':
                    engine.update([], {}, self.now+timedelta(seconds=1), clock=1)
                second = 12 if cause == 'gap' else 8 if cause == 'over_six_seconds' else 2
                values = {'price': 10.04, 'pct': .4, 'amount': self.quote.amount+6e6,
                          'volume': self.quote.volume+8000, 'outer_vol': self.quote.outer_vol+7000,
                          'inner_vol': self.quote.inner_vol+1000,
                          'main_inflow': 5e6, 'source_ts': (self.now+timedelta(seconds=second)).timestamp()}
                values.update({'flow_epoch': 1} if cause == 'epoch' else
                              {'amount': self.quote.amount-1} if cause == 'amount' else
                              {'volume': self.quote.volume-1} if cause == 'volume' else
                              {'outer_vol': self.quote.outer_vol-1} if cause == 'active_counter' else {})
                result = self.tick(engine, second, quote=replace(self.quote, **values), support=False)
                self.assertEqual(self.signals(result, 'suspected_active_buy'), [])

    def test_timeline_appends_existing_card_bounds_and_clears_next_day(self):
        engine = Engine(Settings())
        engine.watchlist = {self.quote.code}
        self.tick(engine, 0, support=False)
        outer, inner = self.quote.outer_vol, self.quote.inner_vol
        for index in range(1, 19):
            bullish = index % 2 == 1
            outer += 7000 if bullish else 1000
            inner += 1000 if bullish else 7000
            second = index*2
            q = replace(self.quote, price=10.04 if bullish else 10,
                        pct=.4 if bullish else 0, amount=self.quote.amount+index*6e6,
                        volume=self.quote.volume+index*8000,
                        outer_vol=outer, inner_vol=inner, main_inflow=5e6 if bullish else 0,
                        source_ts=(self.now+timedelta(seconds=second)).timestamp())
            result = self.tick(engine, second, quote=q, support=False)
            events = self.signals(result)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]['alert_level'], 2)
        row = result['watchlist_leaders'][0]
        timeline = row['timeline']
        self.assertEqual(len(timeline), 8)
        self.assertEqual([item['time'] for item in timeline], sorted((item['time'] for item in timeline), reverse=True))
        self.assertTrue(all(item['is_estimated'] for item in timeline))
        self.assertTrue(all(set(('time','direction','signal_type','signal_label','price','change_pct','summary','is_estimated')) <= item.keys() for item in timeline))
        self.assertEqual(len(engine.timelines[self.quote.code]), 12)
        self.assertEqual(engine.timelines[self.quote.code].maxlen, 12)
        self.assertEqual(result['minute_admitted_by_direction'], {'bullish':0,'bearish':0})
        result = engine.update([self.quote], {}, self.now+timedelta(days=1), clock=40)
        self.assertEqual(result['watchlist_leaders'][0]['timeline'], [])
        self.assertEqual(engine.timelines, {})

    def test_window_reset_clears_timeline_and_support_continuity(self):
        engine=Engine(Settings())
        for second in range(0,32,2): result=self.tick(engine,second)
        self.assertTrue(result['anomalies'][0]['timeline'])
        engine.clear_windows()
        self.assertEqual(engine.timelines,{})
        for second in range(32,62,2):
            self.assertEqual(self.signals(self.tick(engine,second),'large_order_support'),[])
        self.assertEqual(len(self.signals(self.tick(engine,62),'large_order_support')),1)

    def test_phase_two_admissions_keep_independent_direction_caps(self):
        engine = Engine(Settings())
        seeds = [replace(self.quote, code=f'600{i:03d}') for i in range(24)]
        engine.update(seeds, {}, self.now, clock=0)
        changed = [replace(q, price=10.04 if i<12 else 9.96,
                           amount=q.amount+6e6, volume=q.volume+8000,
                           outer_vol=q.outer_vol+(7000 if i<12 else 1000),
                           inner_vol=q.inner_vol+(1000 if i<12 else 7000),
                           source_ts=(self.now+timedelta(seconds=2)).timestamp())
                   for i,q in enumerate(seeds)]
        result = engine.update(changed, {}, self.now+timedelta(seconds=2), clock=2)
        self.assertEqual(result['minute_admitted_by_direction'], {'bullish':10,'bearish':10})
        self.assertEqual(len(result['anomalies']),20)
        self.assertEqual(len(result['events']),20)
        self.assertEqual({e['signal_type'] for e in result['events']},
                         {'suspected_active_buy','suspected_large_smash'})

    def test_same_direction_new_signal_updates_track_without_new_quota_or_ttl(self):
        engine = Engine(Settings())
        self.tick(engine,0,support=False)
        q = replace(self.quote, price=10.3, pct=3, main_inflow=5e6,
                    source_ts=(self.now+timedelta(seconds=2)).timestamp())
        first=self.tick(engine,2,quote=q,support=False)
        self.assertEqual(first['events'][0]['signal_type'],'rapid_rise')
        q = replace(q,price=10.34,pct=3.4,amount=q.amount+6e6,
                    volume=q.volume+8000,outer_vol=q.outer_vol+7000,inner_vol=q.inner_vol+1000,
                    source_ts=(self.now+timedelta(seconds=4)).timestamp())
        second=self.tick(engine,4,quote=q,support=False)
        self.assertEqual([e['signal_type'] for e in second['events']],['suspected_active_buy'])
        self.assertEqual(second['minute_admitted_by_direction']['bullish'],1)
        row=second['anomalies'][0]
        self.assertEqual(row['age_seconds'],2)
        self.assertEqual(row['signal_type'],'suspected_active_buy')
        self.assertEqual([item['signal_type'] for item in row['timeline']],
                         ['suspected_active_buy','rapid_rise'])

    def test_disabled_orderbook_signals_keep_ratio_but_do_not_emit(self):
        engine = Engine(Settings())
        engine.set_filters({'enable_orderbook_signals':False})
        for second in range(0, 34, 2): result = self.tick(engine, second)
        self.assertEqual(self.signals(result), [])
        self.assertAlmostEqual(result['leaders'][0]['outer_inner_ratio'],
                               round(self.quote_at(32).outer_vol/self.quote_at(32).inner_vol,2))


if __name__ == "__main__":
    unittest.main()
