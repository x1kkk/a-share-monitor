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
from config import SHANGHAI, FilterSettings, Settings
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
        self.assertEqual(result['events'][0]['direction'], 'bullish')
        self.assertEqual(result['events'][0]['signal_type'], 'limit_up')
        self.assertEqual(result['events'][0]['alert_level'], 3)
        result = engine.update([], {}, now, clock=4)
        self.assertEqual(len(result['watchlist_leaders']), 2)
        self.assertTrue(result['watchlist_anomalies'][0]['quote_missing'])
        result = engine.update(seed, {}, now, clock=6)
        self.assertEqual(result['watchlist_anomalies'][0]['event_type'], 'break_open')
        self.assertTrue(result['events'][0]['is_watchlist'])
        self.assertEqual(result['events'][0]['direction'], 'bearish')
        self.assertEqual(result['events'][0]['signal_type'], 'break_open')
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


class FilterUpgradeChecks(unittest.TestCase):
    def test_noise_settings_do_not_retrigger_or_refresh_existing_vip(self):
        now = datetime(2026, 11, 26, 10, tzinfo=SHANGHAI)
        q = replace(demo_seed()[0], pre_close=10, price=10, high=10)
        engine = Engine(Settings())
        engine.watchlist = {q.code}
        engine.update([q], {}, now, clock=0)
        q = replace(q, price=10.3, main_inflow=5e6)
        first = engine.update([q], {}, now + timedelta(seconds=2), clock=2)
        self.assertEqual(len(first['events']), 1)
        reason = first['watchlist_anomalies'][0]['reason']
        # Neither a purely ordinary-scan toggle nor an identical write is a
        # new false->true VIP market transition.
        for second in (4, 6):
            engine.set_filters({'exclude_st': True, 'exclude_low_amount': True})
            result = engine.update([q], {}, now + timedelta(seconds=second), clock=second)
            self.assertEqual(result['events'], [])
            self.assertEqual(result['watchlist_anomalies'][0]['age_seconds'], second - 2)
            self.assertEqual(result['watchlist_anomalies'][0]['reason'], reason)
        # A real opposite-direction transition still refreshes the VIP radar.
        result = engine.update([replace(q, price=9.7, main_inflow=-5e6)], {},
                               now + timedelta(seconds=8), clock=8)
        self.assertEqual(result['events'][0]['signal_type'], 'heavy_plunge')
        self.assertEqual(result['watchlist_anomalies'][0]['age_seconds'], 0)

    def test_opposite_direction_notices_have_independent_cooldowns_and_reasons(self):
        now = datetime(2026, 11, 26, 10, tzinfo=SHANGHAI)
        q = replace(demo_seed()[0], pre_close=10, price=10, high=10)
        engine = Engine(Settings())
        engine.update([q], {}, now, clock=0)
        rise = engine.update([replace(q, price=10.3, main_inflow=5e6)], {},
                             now + timedelta(seconds=2), clock=2)['events']
        plunge = engine.update([replace(q, price=9.7, main_inflow=-5e6)], {},
                               now + timedelta(seconds=4), clock=4)['events']
        notifier = Notifier(None, replace(Settings(), wechat_webhook_url=''))
        first = notifier.publish(rise, clock=2)
        second = notifier.publish(plunge, clock=4)
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]['direction'], 'bearish')
        self.assertIn(second[0]['reason'], notifier.markdown(second[0]))
        self.assertIn('放量跳水', notifier.markdown(second[0]))
        self.assertNotEqual(first[0]['id'], second[0]['id'])

    def test_filters_only_suppress_ordinary_scans_and_survive_day_rollover(self):
        now = datetime(2026, 11, 26, 10, tzinfo=SHANGHAI)
        base = replace(demo_seed()[0], pre_close=10, price=10, high=10)
        seeds = [replace(base, code='600001', name='*sT测试', amount=2e8),
                 replace(base, code='600002', name='小成交额', amount=1e7),
                 replace(base, code='600003', name='正常股票', amount=2e8)]
        filters = {'sensitivity': 'balanced', 'exclude_st': True,
                   'exclude_low_amount': True, 'min_daily_amount': 100_000_000}
        ordinary = Engine(Settings())
        ordinary.set_filters(filters)
        ordinary.update(seeds, {}, now, clock=0)
        raised = [replace(q, price=10.3, main_inflow=5e6) for q in seeds]
        result = ordinary.update(raised, {}, now + timedelta(seconds=2), clock=2)
        self.assertEqual([r['code'] for r in result['anomalies']], ['600003'])
        self.assertEqual(len(result['leaders']), 3)
        self.assertEqual(result['filters']['sensitivity'], 'balanced')
        # The exact same ST/low-amount quotes remain eligible for explicit VIPs.
        vip = Engine(Settings())
        vip.set_filters(filters)
        vip.watchlist = {q.code for q in seeds}
        vip.update(seeds, {}, now, clock=0)
        result = vip.update(raised, {}, now + timedelta(seconds=2), clock=2)
        self.assertEqual({r['code'] for r in result['watchlist_anomalies']}, vip.watchlist)
        self.assertTrue(all(e['alert_level'] == 3 for e in result['events']))
        self.assertEqual(result['minute_admitted_by_direction'], {'bullish': 0, 'bearish': 0})
        result = vip.update(seeds, {}, now + timedelta(days=1), clock=4)
        self.assertTrue(result['filters']['exclude_st'])
        self.assertTrue(result['filters']['exclude_low_amount'])
        self.assertEqual(result['watchlist_anomalies'], [])
        # Both switches default off, including ST at the Engine boundary.
        default = Engine(Settings())
        default.update(seeds, {}, now, clock=0)
        result = default.update(raised, {}, now + timedelta(seconds=2), clock=2)
        self.assertEqual(len(result['anomalies']), 3)

    def test_vip_plunge_uses_negative_cumulative_or_negative_window_flow(self):
        now = datetime(2026, 11, 26, 10, tzinfo=SHANGHAI)
        q = replace(demo_seed()[0], price=10, pre_close=10, high=10, main_inflow=-5e6)
        vip = Engine(Settings())
        vip.watchlist = {q.code}
        vip.update([q], {}, now, clock=0)
        # Positive delta but negative cumulative flow is a VIP risk signal.
        changed = replace(q, price=9.7, main_inflow=-4e6)
        result = vip.update([changed], {}, now + timedelta(seconds=2), clock=2)
        event = result['events'][0]
        self.assertEqual(event['signal_type'], 'heavy_plunge')
        self.assertEqual(event['alert_level'], 3)
        self.assertTrue(event['is_watchlist'])
        self.assertIn('当日累计主力净流出', event['reason'])
        self.assertEqual(result['minute_admitted_by_direction']['bearish'], 0)
        ordinary = Engine(Settings())
        ordinary.update([q], {}, now, clock=0)
        result = ordinary.update([changed], {}, now + timedelta(seconds=2), clock=2)
        self.assertEqual(result['events'], [])

    def test_filter_presets_change_speed_threshold(self):
        now = datetime(2026, 11, 26, 10, tzinfo=SHANGHAI)
        q = replace(demo_seed()[0], price=10, pre_close=10, high=10)
        for sensitivity, speed, volume, expected in (
                ('conservative', 3.0, 3.0, False),
                ('balanced', 2.0, 2.0, False),
                ('sensitive', 1.0, 1.5, True)):
            with self.subTest(sensitivity=sensitivity):
                engine = Engine(Settings())
                engine.set_filters({'sensitivity': sensitivity})
                engine.update([q], {}, now, clock=0)
                result = engine.update([replace(q, price=10.15, main_inflow=5e6)], {},
                                       now + timedelta(seconds=2), clock=2)
                self.assertEqual(bool(result['anomalies']), expected)
                self.assertEqual(result['filters']['rise_speed_threshold'], speed)
                self.assertEqual(result['filters']['plunge_speed_threshold'], speed)
                self.assertEqual(result['filters']['vol_ratio_threshold'], volume)

    def test_filter_api_auth_validation_persistence_broadcast_and_atomic_failure(self):
        cfg = replace(Settings(), api_token='filters-test', wechat_webhook_url='')
        body = {'sensitivity': 'sensitive', 'exclude_st': True, 'exclude_low_amount': True}
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            path = folder / 'monitor_filters.json'
            monitor = main.Monitor(None, cfg, watchlist_path=folder / 'watchlist.json', filters_path=path)

            @asynccontextmanager
            async def lifespan(app):
                app.state.monitor = monitor
                yield

            with patch.object(main, 'settings', cfg), patch.object(main.app.router, 'lifespan_context', lifespan):
                with TestClient(main.app) as client:
                    headers = {'authorization': 'Bearer ' + cfg.api_token, 'origin': 'http://localhost:5173'}
                    self.assertEqual(client.get('/api/filters').status_code, 401)
                    self.assertEqual(client.put('/api/filters', json=body).status_code, 401)
                    self.assertEqual(client.put('/api/filters', headers={**headers, 'origin': 'https://evil.invalid'},
                                                json=body).status_code, 403)
                    for invalid in ({**body, 'sensitivity': 'unknown'}, {**body, 'exclude_st': 'true'},
                                    {**body, 'min_daily_amount': -1}, {**body, 'min_daily_amount': True},
                                    {**body, 'unexpected': 1}):
                        self.assertEqual(client.put('/api/filters', headers=headers, json=invalid).status_code, 422)
                    with client.websocket_connect('/ws', headers={'origin': headers['origin']}) as ws:
                        ws.send_json({'token': cfg.api_token})
                        initial = ws.receive_json()
                        self.assertIn('ttl', initial)
                        self.assertEqual(initial['filters']['sensitivity'], 'balanced')
                        response = client.put('/api/filters', headers=headers, json=body)
                        self.assertEqual(response.status_code, 200)
                        snapshot = ws.receive_json()
                        self.assertEqual(snapshot['events'], [])
                        self.assertEqual(snapshot['filters'], response.json()['filters'])
                    saved = path.read_bytes()
                    persisted = json.loads(saved)
                    self.assertEqual(persisted['sensitivity'], 'sensitive')
                    self.assertEqual(persisted['min_daily_amount'], 100_000_000)
                    # Settings are applied to both engines and survive restart.
                    for engine in monitor.engines.values():
                        snap = engine.update([], {}, datetime.now(SHANGHAI), clock=0)
                        self.assertEqual(snap['filters']['sensitivity'], 'sensitive')
                    restored = main.Monitor(None, cfg, watchlist_path=folder / 'watchlist.json', filters_path=path)
                    self.assertEqual(restored.snapshot['filters']['sensitivity'], 'sensitive')
                    previous = client.get('/api/filters', headers=headers).json()
                    for operation in ('main.os.replace', 'main.os.fsync'):
                        with patch(operation, side_effect=OSError('disk unavailable')):
                            response = client.put('/api/filters', headers=headers,
                                                  json={**body, 'sensitivity': 'conservative'})
                        self.assertEqual(response.status_code, 503)
                        self.assertEqual(path.read_bytes(), saved)
                        self.assertEqual(client.get('/api/filters', headers=headers).json(), previous)
                        self.assertEqual(list(folder.glob('*.tmp')), [])

    def test_corrupt_filter_file_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            path = folder / 'monitor_filters.json'
            for invalid in ('{bad', '[]', '{"sensitivity":"unknown"}', '{"exclude_st":"false"}'):
                path.write_text(invalid)
                with self.assertRaises(ValueError):
                    main.Monitor(None, Settings(), watchlist_path=folder / 'watchlist.json', filters_path=path)
                self.assertEqual(path.read_text(), invalid)


class OrderbookFilterChecks(unittest.TestCase):
    def test_phase_one_file_migrates_defaults_without_overwriting_original(self):
        old={'sensitivity':'sensitive','exclude_st':True,'exclude_low_amount':False,
             'min_daily_amount':100_000_000}
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory)
            path=folder/'monitor_filters.json'
            content=json.dumps(old)
            path.write_text(content,encoding='utf-8')
            monitor=main.Monitor(None,replace(Settings(),wechat_webhook_url=''),
                                 watchlist_path=folder/'watchlist.json',filters_path=path)
            filters=monitor.snapshot['filters']
            self.assertEqual(filters['sensitivity'],'sensitive')
            self.assertTrue(filters['enable_orderbook_signals'])
            self.assertEqual(filters['support_min_amount_wan'],1000)
            self.assertEqual(filters['burst_min_amount_wan'],500)
            for engine in monitor.engines.values():
                self.assertEqual(engine.configuration()['filters'],filters)
            self.assertEqual(path.read_text(encoding='utf-8'),content)

    def test_new_filter_fields_validate_finite_positive_numbers_and_strict_booleans(self):
        for invalid in ({'enable_orderbook_signals':'true'},
                        {'support_min_amount_wan':True},
                        {'support_min_amount_wan':0},
                        {'support_min_amount_wan':-1},
                        {'support_min_amount_wan':float('inf')},
                        {'burst_min_amount_wan':float('nan')},
                        {'burst_min_amount_wan':1_000_001}):
            with self.subTest(invalid=invalid):
                with self.assertRaises((ValueError,TypeError)):
                    FilterSettings(**invalid)
        valid=FilterSettings(enable_orderbook_signals=False,support_min_amount_wan=1200.5,
                             burst_min_amount_wan=600)
        self.assertEqual(valid.snapshot()['support_min_amount_wan'],1200.5)
        self.assertFalse(valid.snapshot()['enable_orderbook_signals'])

    def test_phase_two_filter_api_persists_both_engines_and_legacy_put_preserves_new_values(self):
        cfg=replace(Settings(),api_token='phase-two-test',wechat_webhook_url='')
        old={'sensitivity':'balanced','exclude_st':False,'exclude_low_amount':False}
        body={**old,'enable_orderbook_signals':False,'support_min_amount_wan':1250.0,
              'burst_min_amount_wan':650.5}
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory)
            path=folder/'monitor_filters.json'
            monitor=main.Monitor(None,cfg,watchlist_path=folder/'watchlist.json',filters_path=path)
            @asynccontextmanager
            async def lifespan(app):
                app.state.monitor=monitor
                yield
            with patch.object(main,'settings',cfg),patch.object(main.app.router,'lifespan_context',lifespan):
                with TestClient(main.app) as client:
                    headers={'authorization':'Bearer '+cfg.api_token,'origin':'http://localhost:5173'}
                    self.assertEqual(client.put('/api/filters',json=body).status_code,401)
                    self.assertEqual(client.put('/api/filters',headers={**headers,'origin':'https://invalid.example'},json=body).status_code,403)
                    for invalid in ({**body,'enable_orderbook_signals':1},
                                    {**body,'support_min_amount_wan':0},
                                    {**body,'support_min_amount_wan':'1000'},
                                    {**body,'burst_min_amount_wan':False},
                                    {**body,'burst_min_amount_wan':1_000_001}):
                        self.assertEqual(client.put('/api/filters',headers=headers,json=invalid).status_code,422)
                    response=client.put('/api/filters',headers=headers,json=body)
                    self.assertEqual(response.status_code,200)
                    saved=response.json()['filters']
                    self.assertFalse(saved['enable_orderbook_signals'])
                    self.assertEqual(saved['support_min_amount_wan'],1250)
                    self.assertEqual(saved['burst_min_amount_wan'],650.5)
                    for engine in monitor.engines.values():
                        self.assertEqual(engine.configuration()['filters'],saved)
                    # An old phase-one client must not silently turn the new
                    # orderbook switch back on or reset customized thresholds.
                    response=client.put('/api/filters',headers=headers,json={**old,'sensitivity':'sensitive'})
                    self.assertEqual(response.status_code,200)
                    retained=response.json()['filters']
                    for key in ('enable_orderbook_signals','support_min_amount_wan','burst_min_amount_wan'):
                        self.assertEqual(retained[key],saved[key])
                    before=path.read_bytes()
                    for target in ('main.os.fsync','main.os.replace'):
                        with patch(target,side_effect=OSError('write failed')):
                            response=client.put('/api/filters',headers=headers,json={**body,'enable_orderbook_signals':True})
                        self.assertEqual(response.status_code,503)
                        self.assertEqual(path.read_bytes(),before)
                        for engine in monitor.engines.values():
                            self.assertEqual(engine.configuration()['filters'],retained)
                    restored=main.Monitor(None,cfg,watchlist_path=folder/'watchlist.json',filters_path=path)
                    self.assertEqual(restored.snapshot['filters'],retained)

    def test_phase_two_wechat_keeps_estimate_disclosure_and_signal_cooldown(self):
        notifier=Notifier(None,replace(Settings(),wechat_webhook_url=''))
        base={'code':'600000','name':'测试股票','event_type':'anomaly',
              'time':'2026-11-26T10:00:00+08:00','price':10.04,'pct':.4,
              'simulated':False,'direction':'bullish','alert_level':1}
        support={**base,'signal_type':'large_order_support','signal_label':'大单托底',
                 'signal_estimated':False,'reason':'10:00:00 触发【大单托底】，买二挂单1500万，已连续托底30秒'}
        sweep={**base,'signal_type':'suspected_active_buy','signal_label':'疑似盘口扫货',
               'signal_estimated':True,'reason':'10:00:02 触发【疑似盘口扫货】(快照估算)，2秒成交额突增600万，外盘占优'}
        accepted=notifier.publish([support,sweep],clock=0)
        self.assertEqual(len(accepted),2)
        text=notifier.markdown(accepted[1])
        self.assertIn('疑似盘口扫货',text)
        self.assertIn('快照估算',text)
        self.assertIn('2秒成交额突增600万',text)
        self.assertIn('快照估算',notifier.markdown({**accepted[1],'reason':'2秒成交额突增600万，外盘占优'}))
        self.assertEqual(notifier.publish([sweep],clock=2),[])


if __name__ == '__main__':
    unittest.main()
