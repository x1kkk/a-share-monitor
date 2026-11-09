"""Watchlist regression: python -m unittest -v test_watchlist."""
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
from config import SHANGHAI, Settings
from engine import Engine
from fetcher import demo_seed, limit_price
from notifier import Notifier
from pre_market import MarketCalendar


class WatchlistEngineChecks(unittest.TestCase):
    def test_vip_bypasses_full_minute_and_keeps_independent_radar(self):
        cfg = Settings()
        now = datetime(2026, 11, 9, 10, tzinfo=SHANGHAI)
        seed = demo_seed()
        engine = Engine(cfg)
        engine.update(seed, {}, now, clock=0)
        raised = [replace(q, price=q.pre_close * 1.03, main_inflow=1e7) for q in seed]
        first = engine.update(raised, {}, now + timedelta(seconds=2), clock=2)
        self.assertEqual(first['minute_admitted'], 10)
        queued_code = next(q.code for q in seed if q.code not in engine.anomalies)
        engine.watchlist = {queued_code}
        result = engine.update(raised, {}, now + timedelta(seconds=4), clock=4)
        self.assertEqual(result['minute_admitted'], 10)
        self.assertEqual(len(result['anomalies']), 11)
        self.assertEqual(result['anomalies'][0]['code'], queued_code)
        self.assertEqual(result['watchlist_anomalies'][0]['code'], queued_code)
        self.assertEqual(result['events'][0]['priority'], 'vip')
        # It is already in the ordinary board; a separate false -> true still alerts.
        engine.update(seed, {}, now + timedelta(seconds=6), clock=6)
        result = engine.update(raised, {}, now + timedelta(seconds=8), clock=8)
        self.assertEqual([e['code'] for e in result['events']], [queued_code])
        self.assertEqual(engine.watchlist_board(127.999)[0]['status'], 'active')
        self.assertEqual(engine.watchlist_board(128)[0]['status'], 'expired')
        self.assertEqual(len(engine.watchlist_board(307.999)), 1)
        self.assertEqual(engine.watchlist_board(308), [])

    def test_seal_break_missing_quote_and_day_rollover(self):
        now = datetime(2026, 11, 9, 10, tzinfo=SHANGHAI)
        engine = Engine(Settings())
        seed = demo_seed()[:2]
        engine.watchlist = {q.code for q in seed}
        engine.update(seed, {}, now, clock=0)
        q = seed[1]
        sealed = replace(q, price=limit_price(q.pre_close))
        result = engine.update([seed[0], sealed], {}, now, clock=2)
        self.assertEqual(result['watchlist_leaders'][0]['code'], q.code)
        self.assertEqual(result['watchlist_anomalies'][0]['event_type'], 'limit_up')
        result = engine.update([], {}, now, clock=4)
        self.assertEqual(len(result['watchlist_leaders']), 2)
        self.assertTrue(result['watchlist_anomalies'][0]['quote_missing'])
        result = engine.update(seed, {}, now, clock=6)
        self.assertEqual(result['watchlist_anomalies'][0]['event_type'], 'break_open')
        self.assertTrue(result['events'][0]['is_watchlist'])
        result = engine.update(seed, {}, now + timedelta(days=1), clock=8)
        self.assertEqual(len(result['watchlist_leaders']), 2)
        self.assertEqual(result['watchlist_anomalies'], [])

    def test_vip_notifier_priority_and_repeat_transitions(self):
        notifier = Notifier(None, replace(Settings(), wechat_webhook_url=''))
        raw = {'code': '600036', 'name': '招商银行', 'event_type': 'break_open',
               'time': '2026-11-09T10:00:00+08:00', 'price': 10, 'pct': 3,
               'simulated': True}
        vip = {**raw, 'code': '000001', 'is_watchlist': True, 'priority': 'vip'}
        first = notifier.publish([raw, vip], clock=0)
        self.assertTrue(first[0]['is_watchlist'])
        second = notifier.publish([raw, vip], clock=2)
        self.assertEqual(len(second), 1)
        self.assertNotEqual(first[0]['id'], second[0]['id'])
        self.assertIn('★自选VIP', notifier.markdown(second[0]))


class WatchlistApiChecks(unittest.TestCase):
    def test_persistence_search_auth_and_failed_write(self):
        cfg = replace(Settings(), api_token='watchlist-test', wechat_webhook_url='')
        now = datetime.now(SHANGHAI)
        seed = demo_seed()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'watchlist.json'
            monitor = main.Monitor(None, cfg, watchlist_path=path)
            monitor.calendar = MarketCalendar(
                sessions=[now.date() - timedelta(days=i) for i in range(40, -1, -1)],
                loaded_for=now.date(), error=None)
            monitor.fetcher.last_raw = seed
            monitor.mode = 'mock'
            monitor.engines['mock'].update(seed, {}, now, clock=0, simulated=True)

            @asynccontextmanager
            async def lifespan(app):
                app.state.monitor = monitor
                yield

            with patch.object(main, 'settings', cfg), patch.object(main.app.router, 'lifespan_context', lifespan):
                with TestClient(main.app) as client:
                    headers = {'authorization': 'Bearer ' + cfg.api_token, 'origin': 'http://localhost:5173'}
                    self.assertEqual(client.get('/api/watchlist').status_code, 401)
                    self.assertEqual(client.post('/api/watchlist', headers={**headers, 'origin': 'https://evil.invalid'},
                                                 json={'code': '600036'}).status_code, 403)
                    self.assertEqual(client.get('/api/stocks/search?q=招商', headers=headers).json()[0]['code'], '600036')
                    self.assertEqual(client.post('/api/watchlist', headers=headers, json={'code': '300001'}).status_code, 422)
                    self.assertEqual(client.post('/api/watchlist', headers=headers, json={'code': '600999'}).status_code, 422)
                    young = replace(seed[0], listed=now.date())
                    monitor.fetcher.last_raw = [young, *seed[1:]]
                    self.assertEqual(client.post('/api/watchlist', headers=headers, json={'code': young.code}).status_code, 422)
                    with client.websocket_connect('/ws', headers={'origin': headers['origin']}) as ws:
                        ws.send_json({'token': cfg.api_token})
                        ws.receive_json()
                        response = client.post('/api/watchlist', headers=headers, json={'code': '600036'})
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.json()['name'], '招商银行')
                        self.assertEqual(response.json()['price'], seed[1].price)
                        snapshot = ws.receive_json()
                        self.assertEqual(snapshot['watchlist_leaders'][0]['code'], '600036')
                        self.assertIn('watchlist_anomalies', snapshot)
                        self.assertEqual(snapshot['events'], [])
                    client.post('/api/watchlist', headers=headers, json={'code': '600036'})
                    self.assertEqual(json.loads(path.read_text()), ['600036'])
                    restored = main.Monitor(None, cfg, watchlist_path=path)
                    self.assertEqual(restored.engines['live'].watchlist, {'600036'})
                    with patch('main.os.replace', side_effect=OSError('disk unavailable')):
                        response = client.post('/api/watchlist', headers=headers, json={'code': '000001'})
                        self.assertEqual(response.status_code, 503)
                    self.assertEqual(json.loads(path.read_text()), ['600036'])
                    self.assertEqual(monitor.engines['mock'].watchlist, {'600036'})
                    self.assertEqual(list(Path(directory).glob('.watchlist-*')), [])
                    self.assertEqual(len(client.get('/api/watchlist', headers=headers).json()), 1)
                    client.delete('/api/watchlist/600036', headers=headers)
                    client.delete('/api/watchlist/600036', headers=headers)
                    self.assertEqual(client.get('/api/watchlist', headers=headers).json(), [])
                    self.assertEqual(json.loads(path.read_text()), [])
                    monitor.calendar.error = 'offline'
                    self.assertEqual(client.post('/api/watchlist', headers=headers, json={'code': '600036'}).status_code, 422)

    def test_corrupt_persistence_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'watchlist.json'
            for invalid in ('{bad', '["300001"]', '{}'):
                path.write_text(invalid)
                with self.assertRaises(ValueError):
                    main.Monitor(None, Settings(), watchlist_path=path)
                self.assertEqual(path.read_text(), invalid)


if __name__ == '__main__':
    unittest.main()
