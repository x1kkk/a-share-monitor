"""Offline regressions for complete Tencent snapshots and pre-market caches."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from config import SHANGHAI, Settings
from engine import Engine
from fetcher import Fetcher, demo_seed, eligible, parse_flows, parse_tencent
from pre_market import MarketCalendar, Universe


def stock_pool(count=3050):
    codes = [f"{i:06d}" for i in range(1, count // 2 + 1)]
    codes += [str(600000 + i) for i in range(count - len(codes))]
    return {code: {"code": code, "name": "测试股份", "listed": "2010-01-04"} for code in codes}


def quote_line(code, now, *, amount=100, buy=80, sell=20, volume=1000, invalid=None):
    fields = ["0"] * 45
    values = {1: "测试股份", 2: code, 3: 10.5, 4: 10, 6: volume, 7: buy, 8: sell,
              10: 1234, 20: 567, 30: now.strftime("%Y%m%d%H%M%S"), 32: 5, 33: 10.8,
              37: amount, 38: 1.25, 44: 12.5}
    if invalid:
        values.update(invalid)
    for index, value in values.items():
        fields[index] = str(value)
    market = "sh" if code.startswith("60") else "sz"
    return f'v_{market}{code}="{"~".join(fields)}";'


def flow_line(code, today, value=3.5):
    market = "sh" if code.startswith("60") else "sz"
    fields = ["0"] * 14
    fields[0], fields[3], fields[13] = market + code, str(value), today.strftime("%Y%m%d")
    return f'v_ff_{market}{code}="{"~".join(fields)}";'


class TencentSnapshotChecks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = datetime.now(SHANGHAI).replace(hour=10, minute=0, second=0, microsecond=0)
        self.stocks = stock_pool(2)
        self.requests = []
        self.amount, self.buy, self.sell, self.volume = 100, 80, 20, 1000
        self.flow_values = {}
        self.mutation = None
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.response))
        self.addAsyncCleanup(self.client.aclose)
        self.fetcher = Fetcher(self.client, Settings())
        self.fetcher.universe.stocks = self.stocks
        self.fetcher.universe.loaded_for = self.now.date()

    def response(self, request):
        self.assertEqual(request.url.host, "qt.gtimg.cn")
        symbols = request.url.path.split("q=", 1)[1].split(",")
        is_flow = symbols[0].startswith("ff_")
        self.requests.append((is_flow, symbols))
        codes = [symbol[-6:] for symbol in symbols]
        if is_flow:
            lines = [flow_line(code, *self.flow_values[code]) for code in codes if code in self.flow_values]
        else:
            lines = [quote_line(code, self.now, amount=self.amount, buy=self.buy, sell=self.sell,
                                volume=self.volume) for code in codes]
            if self.mutation:
                lines = self.mutation(codes, lines, request)
        return httpx.Response(200, content="\n".join(lines).encode("gb18030"))

    async def test_full_market_uses_five_quote_and_flow_batches_and_reuses_books(self):
        self.stocks = stock_pool()
        self.fetcher.universe.stocks = self.stocks
        self.flow_values = {code: (self.now.date(), 3.5) for code in self.stocks}
        quotes = await self.fetcher.fetch_market()
        self.assertEqual(len(quotes), 3050)
        self.assertEqual([sum(flow == flag for flow, _ in self.requests) for flag in (False, True)], [5, 5])
        self.assertTrue(all(len(symbols) <= 650 for _, symbols in self.requests))
        self.assertEqual(len({symbol for flow, symbols in self.requests if not flow for symbol in symbols}), 3050)
        q = quotes[0]
        self.assertEqual((q.amount, q.float_cap, q.main_inflow), (1e6, 1.25e9, 35000))
        self.assertEqual((q.volume, q.pre_close, q.pct, q.high, q.turnover), (1000, 10, 5, 10.8, 1.25))
        self.assertEqual(q.listed, date(2010, 1, 4))
        self.assertEqual(q.source_ts, self.now.timestamp())
        books = await self.fetcher.fetch_books(set(self.stocks), self.now)
        self.assertEqual(len(books), 3050)
        self.assertEqual((books[q.code].buy_lots, books[q.code].sell_lots), (1234, 567))
        self.assertEqual(len(self.requests), 10)

    async def test_failed_missing_duplicate_and_invalid_batches_do_not_commit(self):
        self.stocks = stock_pool(652)
        self.fetcher.universe.stocks = self.stocks
        self.flow_values = {code: (self.now.date(), 3.5) for code in self.stocks}
        await self.fetcher.fetch_market()
        last_raw = self.fetcher.last_raw
        baseline = copy.deepcopy(self.fetcher._flow_state)
        books = dict(self.fetcher.last_books)

        def fail(codes, lines, request):
            if len(codes) < 650:
                raise httpx.RemoteProtocolError("empty response", request=request)
            return lines

        mutations = {
            "failed second batch": fail,
            "missing code": lambda codes, lines, request: lines[:-1],
            "duplicate code": lambda codes, lines, request: lines + lines[:1],
            "invalid amount": lambda codes, lines, request: [
                quote_line(codes[0], self.now, invalid={37: "NaN"}), *lines[1:]],
            "wrong embedded code": lambda codes, lines, request: [
                quote_line(codes[0], self.now, invalid={2: "999999"}), *lines[1:]],
        }
        self.now += timedelta(seconds=2)
        self.amount += 10
        for name, mutation in mutations.items():
            with self.subTest(case=name):
                self.mutation = mutation
                with self.assertRaises((httpx.HTTPError, ValueError)):
                    await self.fetcher.fetch_market()
                self.assertIs(self.fetcher.last_raw, last_raw)
                self.assertEqual(self.fetcher._flow_state, baseline)
                self.assertEqual(self.fetcher.last_books, books)

    async def test_missing_or_stale_ff_uses_incremental_active_volume_estimate(self):
        code, other = self.stocks
        # A valid peer keeps ff polling enabled; this stock's stale ff must not be used.
        self.flow_values = {code: (self.now.date() - timedelta(days=1), 9999),
                            other: (self.now.date(), 3.5)}
        first = {q.code: q for q in await self.fetcher.fetch_market()}[code]
        self.assertEqual(first.main_inflow, 600000)
        self.assertEqual(first.flow_source, "snapshot_estimate")
        self.assertTrue(first.split_estimated)
        self.now += timedelta(seconds=2)
        self.amount, self.buy, self.sell, self.volume = 110, 95, 25, 1100
        del self.flow_values[code]
        second = {q.code: q for q in await self.fetcher.fetch_market()}[code]
        self.assertEqual(second.main_inflow, 650000)
        self.assertEqual(second.super_inflow + second.large_inflow, second.main_inflow)
        self.assertEqual(second.flow_epoch, first.flow_epoch)
        self.flow_values[code] = (self.now.date(), 12.5)
        self.now += timedelta(seconds=2)
        third = {q.code: q for q in await self.fetcher.fetch_market()}[code]
        self.assertEqual(third.main_inflow, 125000)
        self.assertEqual(third.flow_source, "tencent_ff")
        self.assertEqual(third.flow_epoch, second.flow_epoch + 1)
        self.assertEqual(third.super_inflow + third.large_inflow, third.main_inflow)

    async def test_counter_rollback_and_new_day_reset_flow_baseline(self):
        code, other = self.stocks
        self.flow_values = {other: (self.now.date(), 3.5)}
        first = {q.code: q for q in await self.fetcher.fetch_market()}[code]
        self.now += timedelta(seconds=2)
        self.amount, self.buy, self.sell, self.volume = 120, 9, 1, 1200
        reset = {q.code: q for q in await self.fetcher.fetch_market()}[code]
        self.assertEqual(reset.main_inflow, 960000)
        self.assertEqual(reset.flow_epoch, first.flow_epoch + 1)
        self.now += timedelta(days=1)
        self.amount, self.buy, self.sell, self.volume = 10, 3, 1, 100
        next_day = {q.code: q for q in await self.fetcher.fetch_market()}[code]
        self.assertEqual(next_day.main_inflow, 50000)
        self.assertEqual(next_day.flow_epoch, reset.flow_epoch + 1)
        self.assertEqual(next_day.super_inflow + next_day.large_inflow, next_day.main_inflow)

    def test_ff_validates_identity_date_and_finite_amount(self):
        today = self.now.date()
        text = flow_line("600000", today, -2.5) + flow_line("000001", today - timedelta(days=1), 10)
        text += flow_line("600001", today, "NaN")
        self.assertEqual(parse_flows(text, today), {"600000": -25000})


class PreMarketCacheChecks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "universe_cache.json"
        self.today = datetime.now(SHANGHAI).date()
        self.stocks = stock_pool()

        def offline(request):
            raise httpx.RemoteProtocolError("IDC connection reset", request=request)

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(offline))
        self.addAsyncCleanup(self.client.aclose)
        self.universe = Universe(self.client, Settings(), path=self.path)

    def cache(self, updated_for, stocks=None):
        self.path.write_text(json.dumps({"updated_for": updated_for.isoformat(),
                                         "stocks": list((stocks or self.stocks).values())}), encoding="utf-8")

    async def test_same_day_complete_cache_loads_without_network(self):
        self.cache(self.today)
        with patch.object(self.universe, "_get", side_effect=AssertionError("network forbidden")):
            stocks = await self.universe.load(self.today)
        self.assertEqual(stocks, self.stocks)
        self.assertTrue(all(sum(code.startswith(p) for code in stocks) >= 1000 for p in ("00", "60")))

    async def test_network_failure_retains_last_complete_pool(self):
        self.cache(self.today - timedelta(days=1))
        stocks = await self.universe.load(self.today)
        self.assertEqual(stocks, self.stocks)
        reloaded = Universe(self.client, Settings(), path=self.path)
        self.assertEqual(await reloaded.load(self.today), self.stocks)

    async def test_partial_source_never_overwrites_complete_cache(self):
        self.cache(self.today - timedelta(days=1))

        async def partial(prefix):
            return {c: row for c, row in list(self.stocks.items())[:20] if c.startswith(prefix)}

        with patch.object(self.universe, "_exchange", partial), patch.object(self.universe, "_sina", partial):
            self.assertEqual(await self.universe.load(self.today), self.stocks)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(saved["stocks"]), 3050)

    async def test_incomplete_cache_and_no_sources_fail_without_demo(self):
        self.cache(self.today, stock_pool(24))
        with self.assertRaisesRegex(ValueError, "complete universe unavailable"):
            await self.universe.load(self.today)
        self.assertEqual(self.universe.stocks, {})
        self.assertIsNone(self.universe.loaded_for)
        self.assertEqual(len(json.loads(self.path.read_text(encoding="utf-8"))["stocks"]), 24)

    async def test_calendar_falls_back_to_cache_covering_today_offline(self):
        path = Path(self.tmp.name) / "calendar.json"
        sessions = [self.today - timedelta(days=20), self.today, self.today + timedelta(days=1)]
        path.write_text(json.dumps({"sessions": [x.isoformat() for x in sessions]}), encoding="utf-8")
        calendar = MarketCalendar(cache_path=path)
        with patch("pre_market.asyncio.create_subprocess_exec", new=AsyncMock(side_effect=OSError("offline"))):
            await calendar.load(Settings(), self.today)
        self.assertTrue(calendar.covered(self.today))
        self.assertEqual(calendar.sessions, sessions)
        self.assertFalse(calendar.streaks_ready)
        self.assertEqual(calendar.yesterday_streaks, {})

    async def test_cold_sina_pool_with_empty_szse_gets_and_caches_listing_profiles(self):
        stocks = stock_pool(2000)
        unavailable = next(iter(stocks))
        calls = []

        def response(request):
            calls.append(str(request.url))
            host, path = request.url.host, request.url.path
            self.assertNotIn("eastmoney", host)
            if host == "query.sse.com.cn":
                raise httpx.RemoteProtocolError("exchange unavailable", request=request)
            if host == "www.szse.cn":
                return httpx.Response(200, json=[])
            self.assertEqual(host, "vip.stock.finance.sina.com.cn")
            if "vCI_CorpInfo" in path:
                code = path.rsplit("/", 1)[1].removesuffix(".phtml")
                self.assertIn(code, stocks)
                if code == unavailable:
                    return httpx.Response(404)
                html = "<table><tr><td>上市日期：</td><td>2010-01-04</td></tr></table>"
                return httpx.Response(200, content=html.encode("gb18030"))
            prefix = "60" if request.url.params["node"] == "sh_a" else "00"
            rows = [{"code": code, "name": row["name"]} for code, row in stocks.items()
                    if code.startswith(prefix)]
            if path.endswith("getHQNodeStockCount"):
                return httpx.Response(200, json=str(len(rows)))
            self.assertTrue(path.endswith("getHQNodeData"))
            start = (int(request.url.params["page"]) - 1) * 100
            return httpx.Response(200, json=rows[start:start + 100])

        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            universe = Universe(client, Settings(), path=self.path)
            loaded = await universe.load(self.today)
            self.assertEqual(set(loaded), set(stocks))
            self.assertEqual(sum(bool(r["listed"]) for r in loaded.values()), 1999)
            self.assertIsNone(loaded[unavailable]["listed"])
            now = datetime.combine(self.today, datetime.min.time(), SHANGHAI).replace(hour=10)
            parsed, _, _ = parse_tencent("\n".join(quote_line(c, now) for c in loaded), loaded)
            sessions = [self.today - timedelta(days=i) for i in range(20, -1, -1)]
            calendar = MarketCalendar(sessions=sessions, loaded_for=self.today, error=None)
            qualified = eligible(list(parsed.values()), calendar, self.today, Settings())
            self.assertEqual(len(qualified), 1999)
            self.assertNotIn(unavailable, {q.code for q in qualified})
            profiles = [url.rsplit("/", 1)[1].removesuffix(".phtml")
                        for url in calls if "vCI_CorpInfo" in url]
            self.assertEqual(set(profiles), set(stocks))
            self.assertEqual(len([c for c in profiles if c != unavailable]), 1999)
            # Dates that have already been verified survive restart without more profiles.
            calls.clear()
            restarted = Universe(client, Settings(), path=self.path)
            self.assertEqual(await restarted.load(self.today), loaded)
            self.assertTrue(calls)
            self.assertTrue(all("vCI_CorpInfo" in url and unavailable in url for url in calls))
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(saved["stocks"]), 2000)
        self.assertEqual(saved["updated_for"], self.today.isoformat())

    async def test_successful_calendar_bootstrap_does_not_require_streaks(self):
        path = Path(self.tmp.name) / "calendar-success.json"
        sessions = [self.today - timedelta(days=1), self.today, self.today + timedelta(days=1)]
        stdout = json.dumps({"sessions": [x.isoformat() for x in sessions]}).encode()
        process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(stdout, b"")))
        calendar = MarketCalendar(cache_path=path, yesterday_streaks={"600000": 3}, streaks_ready=True)
        with patch("pre_market.asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)):
            await calendar.load(Settings(), self.today)
        self.assertTrue(calendar.covered(self.today))
        self.assertFalse(calendar.streaks_ready)
        self.assertEqual(calendar.yesterday_streaks, {})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["sessions"],
                         [x.isoformat() for x in sessions])


class SourceBoundaryChecks(unittest.TestCase):
    def test_eligible_rejects_previous_day_and_honors_fifteenth_session(self):
        today = date(2026, 11, 10)
        sessions = [today - timedelta(days=i) for i in range(14, -1, -1)]
        calendar = MarketCalendar(sessions=sessions, loaded_for=today, error=None)
        quiet_time = datetime(2026, 11, 10, 9, 25, tzinfo=SHANGHAI)
        mature = replace(demo_seed()[0], listed=sessions[0], source_ts=quiet_time.timestamp())
        yesterday = replace(mature, code="600001", source_ts=(quiet_time - timedelta(days=1)).timestamp())
        young = replace(mature, code="600002", listed=sessions[1])
        unknown = replace(mature, code="600003", listed=None, observed_since=today)
        result = eligible([mature, yesterday, young, unknown], calendar, today, Settings(), current_session=True)
        self.assertEqual(result, [mature])
        # Off-hours simulation/search can still use the last real static snapshot.
        self.assertEqual(eligible([mature, yesterday], calendar, today, Settings()), [mature, yesterday])
        self.assertTrue(calendar.listing_is_mature(sessions[0], today, 15))
        self.assertFalse(calendar.listing_is_mature(sessions[1], today, 15))

    def test_engine_restarts_inflow_window_when_source_epoch_changes(self):
        now = datetime(2026, 11, 10, 10, tzinfo=SHANGHAI)
        engine = Engine(Settings())
        q = replace(demo_seed()[0], main_inflow=1000, flow_epoch=0)
        engine.update([q], {}, now, clock=0)
        q = replace(q, main_inflow=2000)
        engine.update([q], {}, now + timedelta(seconds=2), clock=2)
        self.assertEqual(engine.rows[q.code]["inflow_2m"], 1000)
        q = replace(q, main_inflow=1e8, flow_epoch=1)
        engine.update([q], {}, now + timedelta(seconds=4), clock=4)
        self.assertEqual(engine.rows[q.code]["inflow_2m"], 0)
        q = replace(q, main_inflow=1e8 + 500)
        engine.update([q], {}, now + timedelta(seconds=6), clock=6)
        self.assertEqual(engine.rows[q.code]["inflow_2m"], 500)


if __name__ == "__main__":
    unittest.main()
