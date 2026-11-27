"""Offline review regressions: python -m unittest -v test_review."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import main
import review
from config import SHANGHAI, Settings
from engine import Engine
from fetcher import Book, demo_seed
from pre_market import MarketCalendar
from review import (FACTORS, ReviewService, finite, locked_date, main_board, mock_archives,
                    mock_sessions, normalize, score_rows, status_label)


NOW = datetime(2026, 11, 26, 16, tzinfo=SHANGHAI)


def service(path=None, *, mock=False):
    sessions = mock_sessions(NOW.date())
    monitor = SimpleNamespace(
        cfg=replace(Settings(), mock_mode=mock, api_token="review-test"),
        calendar=MarketCalendar(sessions=sessions, loaded_for=NOW.date(), error=None),
        fetcher=SimpleNamespace(last_raw=[], sectors=SimpleNamespace(index={}, lookup=lambda code: {})))
    result = ReviewService(monitor, path)
    result.archives = mock_archives(sessions, NOW.date())
    for archive in result.archives.values():
        archive["simulated"] = mock
    return result


def every_row(payload):
    return ([r for tier in payload["success_tiers"] + payload["failed_tiers"] for r in tier["items"]]
            + payload["first_board_broken"] + payload["active_15d_broken"])


class PureReviewChecks(unittest.TestCase):
    def test_main_board_filter_applies_to_every_output_pool(self):
        for code in ("000001", "001001", "002001", "003001", "600001", "601001", "603001", "605001"):
            self.assertTrue(main_board({"code": code, "name": "主板样本"}))
        invalid = [
            {"code": code, "name": "错误板块"}
            for code in ("688001", "300001", "301001", "830001", "430001", "920001", "60001", "6000000")]
        invalid += [{"code": "600999", "name": name} for name in ("ST测试", "*st测试", "测试St", "退市样本", "")]
        for row in invalid:
            self.assertFalse(main_board(row), row)
        module = service()
        for archive in module.archives.values():
            archive["success"] += [{**r, "streak_days": 2} for r in invalid]
            archive["broken"] += deepcopy(invalid)
        payload = module.ladder(now=NOW)
        self.assertTrue(every_row(payload))
        self.assertTrue(all(main_board(r) for r in every_row(payload)))

    def test_all_state_labels_and_dynamic_ascending_tiers(self):
        self.assertEqual(status_label(0, True, True, 0), "首板")
        self.assertEqual(status_label(0, True, True, 1), "首板 · 回封")
        self.assertEqual(status_label(0, False, True, 1), "首板 · 炸板")
        for previous in (1, 2, 3, 4, 7):
            with self.subTest(previous=previous):
                prefix = f"{previous}进{previous + 1}"
                self.assertEqual(status_label(previous, True, True, 0), prefix + " 成功")
                self.assertEqual(status_label(previous, True, True, 2), prefix + " 成功 · 回封")
                self.assertEqual(status_label(previous, False, True, 1), prefix + " 失败 · 炸板")
                self.assertEqual(status_label(previous, False, False, 0), prefix + " 失败 · 断板")
        payload = service().ladder(now=NOW)
        self.assertEqual([t["board_count"] for t in payload["success_tiers"]], [1, 2, 3, 4, 5])
        self.assertEqual([t["from_board"] for t in payload["failed_tiers"]], [1, 2, 3])
        self.assertEqual([len(t["items"]) for t in payload["failed_tiers"]], [2, 2, 2])
        labels = {r["status_label"] for r in every_row(payload)}
        for label in ("首板", "首板 · 回封", "首板 · 炸板", "1进2 成功", "1进2 成功 · 回封",
                      "1进2 失败 · 炸板", "1进2 失败 · 断板", "2进3 失败 · 炸板", "2进3 失败 · 断板"):
            self.assertIn(label, labels)
        self.assertEqual(payload["summary"]["limit_up_count"], 7)
        self.assertEqual(payload["summary"]["reseal_count"], 2)
        self.assertEqual(payload["summary"]["broken_count"], 4)
        rates = payload["summary"]["promotion_rates"]
        self.assertEqual([(p["success_count"], p["failed_count"]) for p in rates], [(2, 2), (1, 2), (1, 2), (1, 0)])
        self.assertEqual([p["promotion_rate"] for p in rates], [50.0, 33.33, 33.33, 100.0])
        # No hard cap at five, and empty lower columns retain their positions.
        module = service()
        latest = module.archives[NOW.date().isoformat()]
        highest = latest["success"][-1]["code"]
        previous = module.archives["2026-11-25"]
        next(r for r in previous["success"] if r["code"] == highest)["streak_days"] = 7
        payload = module.ladder(now=NOW)
        self.assertEqual([t["board_count"] for t in payload["success_tiers"]], list(range(1, 9)))
        self.assertEqual(payload["success_tiers"][-1]["items"][0]["status_label"], "7进8 成功")

    def test_scores_ties_partial_factors_and_missing_values(self):
        def row(code, values):
            return {"code": code, **dict(zip(FACTORS, values))}
        rows = [row("600001", [10, 30, None, None]), row("600002", [20, 20, None, None]),
                row("600003", [None, 10, None, None]), row("600004", [None] * 4)]
        score_rows(rows)
        self.assertEqual([r["ladder_score"] for r in rows], [50, 75, 0, None])
        tied = [row("600001", [5, 10, 2, 3]), row("600002", [5, 10, 2, 3])]
        score_rows(tied)
        self.assertEqual([r["ladder_score"] for r in tied], [50, 50])
        single = [row("600001", [None, 7, None, None])]
        score_rows(single)
        self.assertEqual(single[0]["ladder_score"], 50)
        failures = [row("600005", [None, -20, None, None])]
        score_rows(failures, tied)
        self.assertEqual(failures[0]["ladder_score"], 0)
        self.assertTrue(all(r["seal_amount"] is None for r in every_row(service().ladder(now=NOW)) if not r["is_limit_up"]))

    def test_inflow_ratio_and_unknown_values_are_not_zero(self):
        self.assertEqual(normalize({"main_inflow": 2, "amount": 3})["inflow_ratio"], 66.67)
        self.assertEqual(normalize({"main_inflow": 0, "amount": 100})["inflow_ratio"], 0)
        for flow, amount in ((None, 10), (1, None), (1, 0), (1, -1), (float("nan"), 1)):
            self.assertIsNone(normalize({"main_inflow": flow, "amount": amount})["inflow_ratio"])
        for value in (None, "nan", float("inf"), "--", True):
            self.assertIsNone(finite(value))
        self.assertEqual(finite("0"), 0)
        normalized = normalize({"seal_amount": 300, "price": None})
        self.assertIsNone(normalized["seal_amount"])
        self.assertIsNone(normalized["price"])

    def test_unknown_concepts_and_unavailable_streak_degrade_without_page_failure(self):
        self.assertEqual(normalize({"concepts": None})["concepts"], [])
        module = service()
        del module.archives["2026-11-25"]
        for row in module.archives["2026-11-26"]["success"]:
            row["streak_days"] = None
        payload = module.ladder(now=NOW)
        self.assertNotEqual(payload["status"], "failed")
        self.assertFalse(payload["summary"]["baseline_ready"])

    def test_lock_changes_at_1500_and_respects_exchange_holidays(self):
        sessions = [date(2026, 11, d) for d in (20, 23, 25, 26, 27)]
        for stamp, expected in (
            ("2026-11-26T14:59:59+08:00", "2026-11-25"),
            ("2026-11-26T15:00:00+08:00", "2026-11-26"),
            ("2026-11-26T15:01:00+08:00", "2026-11-26"),
            ("2026-11-24T18:00:00+08:00", "2026-11-23"),
            ("2026-11-28T18:00:00+08:00", "2026-11-27"),
            ("2026-11-26T06:59:59+00:00", "2026-11-25")):
            self.assertEqual(locked_date(datetime.fromisoformat(stamp), sessions).isoformat(), expected)
        self.assertIsNone(locked_date(NOW, []))
        module = service()
        self.assertEqual(module.meta(NOW.replace(hour=14, minute=59))["default_date"], "2026-11-25")
        self.assertEqual(module.meta(NOW)["next_switch"], "2026-11-27T15:00:00+08:00")
        module.monitor.calendar.error = "unavailable"
        self.assertIsNone(module.meta(NOW)["default_date"])
        self.assertEqual(module.meta(NOW)["status"], "calendar_unavailable")

    def test_fifteen_session_counts_include_broken_and_do_not_invent_missing_history(self):
        module = service()
        payload = module.ladder(now=NOW)
        self.assertTrue(payload["summary"]["history_complete"])
        self.assertEqual(len(module.meta(NOW)["dates"]), 15)
        active = payload["active_15d_broken"]
        self.assertEqual(sorted(r["limit_up_count_15d"] for r in active), [2, 5])
        self.assertTrue(all(r["streak_days"] == 0 and r["status_label"] == "15日断板" for r in active))
        five = next(r for r in active if r["limit_up_count_15d"] == 5)
        self.assertEqual(five["limit_up_count_7d"], 2)
        self.assertEqual(five["pattern_label"], "14天5板")
        # Losing an old date invalidates 15-session counts, while 7-session facts remain valid.
        selected = module.meta(NOW)["dates"]
        del module.archives[selected[-1]]
        payload = module.ladder(now=NOW)
        self.assertFalse(payload["summary"]["history_complete"])
        self.assertTrue(all(r["limit_up_count_15d"] is None for r in every_row(payload)))
        self.assertTrue(all(r["limit_up_count_7d"] is not None for r in every_row(payload)))
        self.assertEqual(payload["active_15d_broken"], [])
        # Missing yesterday's base means no denominator or fabricated failed pool.
        del module.archives["2026-11-25"]
        payload = module.ladder(now=NOW)
        self.assertFalse(payload["summary"]["baseline_ready"])
        self.assertEqual(payload["failed_tiers"], [])
        self.assertTrue(all(t["promotion_rate"] is None for t in payload["success_tiers"]))

    def test_live_engine_uses_verified_previous_streaks_and_fresh_books(self):
        module = service()
        now = NOW.replace(hour=10)
        quote = replace(demo_seed()[0], price=11, pre_close=10, high=11, source_ts=now.timestamp())
        previous = module.archives["2026-11-25"]
        previous["success"] = [{"code": quote.code, "name": quote.name, "streak_days": 4}]
        streaks = module.inject_streaks(now)
        self.assertEqual(streaks, {quote.code: 4})
        self.assertTrue(module.monitor.calendar.streaks_ready)
        engine = Engine(module.cfg)
        book = Book(11, 3000, 0, now.timestamp())
        engine.update([quote], {quote.code: book}, now, clock=0, yesterday_streaks=streaks)
        module.capture_live(engine, now)
        live = module.ladder("live", now=now)
        self.assertEqual(live["summary"]["max_board"], 5)
        item = live["success_tiers"][-1]["items"][0]
        self.assertEqual(item["status_label"], "4进5 成功")
        self.assertEqual(item["seal_amount"], 11 * 3000 * 100)
        self.assertEqual(engine.rows[quote.code]["consecutive_days"], 5)
        broken = replace(quote, price=10.5)
        engine.update([broken], {}, now + timedelta(seconds=2), clock=2, yesterday_streaks=streaks)
        module.capture_live(engine, now + timedelta(seconds=2))
        live = module.ladder("live", now=now + timedelta(seconds=2))
        item = live["failed_tiers"][-1]["items"][0]
        self.assertEqual(item["status_label"], "4进5 失败 · 炸板")
        self.assertIsNone(item["seal_amount"])
        previous["complete"] = False
        self.assertIsNone(module.inject_streaks(now))
        self.assertFalse(module.monitor.calendar.streaks_ready)
        self.assertEqual(module.monitor.calendar.yesterday_streaks, {})
        self.assertEqual(module.ladder("live", now=NOW)["success_tiers"], [])

    def test_weekend_mock_live_uses_its_own_simulated_session(self):
        module = service(mock=True)
        for day in (date(2026, 11, 28), date(2026, 11, 29)):
            now = datetime.combine(day, NOW.timetz())
            module.archives = mock_archives(mock_sessions(day), date(2026, 11, 27))
            payload = module.ladder("live", now=now)
            self.assertEqual(payload["date"], day.isoformat())
            self.assertTrue(payload["simulated"])
            self.assertEqual(payload["status"], "ready")
            self.assertTrue(payload["summary"]["history_complete"])
            self.assertTrue(every_row(payload))

    def test_today_st_rename_cannot_return_from_previous_or_active_history(self):
        module = service()
        now = NOW.replace(hour=10)
        payload = module.ladder(now=NOW)
        previous_code = module.archives["2026-11-25"]["success"][0]["code"]
        active_code = payload["active_15d_broken"][0]["code"]
        excluded = {previous_code, active_code}
        base = demo_seed()[0]
        quotes = [replace(base, code=code, name="*sT风险样本", price=10, pre_close=10, high=10,
                          source_ts=now.timestamp()) for code in excluded]
        engine = Engine(module.cfg)
        engine.update(quotes, {}, now, clock=0)
        module.capture_live(engine, now)
        self.assertEqual(set(module.live["excluded_codes"]), excluded)
        live = module.ladder("live", now=now)
        self.assertFalse(excluded & {row["code"] for row in every_row(live)})


class ReviewPersistenceChecks(unittest.IsolatedAsyncioTestCase):
    async def test_lhb_before_stock_enrichment_updates_success_and_survives_later_fill(self):
        with tempfile.TemporaryDirectory() as directory:
            module = service(Path(directory))
            day = NOW.date().isoformat()
            archive = module.archives[day]
            target = archive["success"][0]
            code = target["code"]
            archive["observations"] = {}
            archive["lhb_status"] = "pending"
            module.candidates = lambda selected, now: {code: target}
            module.worker = AsyncMock(return_value={"status": "ready", "net_buy": {code: 2_000_000}})
            await module.enrich(day, NOW, True, only_lhb=True)
            module.worker.assert_awaited_once_with("lhb", date=day)
            filled = next(r for r in module.archives[day]["success"] if r["code"] == code)
            self.assertEqual(filled["lhb_net_buy"], 2_000_000)
            module.worker = AsyncMock(return_value={"rows": [{"date": day, "main_inflow": 3_000_000}],
                                                    "status": {"price": "ready", "flow": "ready"}})
            await module.enrich(day, NOW)
            filled = next(r for r in module.archives[day]["success"] if r["code"] == code)
            self.assertEqual(filled["lhb_net_buy"], 2_000_000)
            self.assertEqual(filled["main_inflow"], 3_000_000)
            self.assertEqual(module.archives[day]["observations"][code]["lhb_net_buy"], 2_000_000)

    async def test_failed_stock_fill_preserves_same_day_cached_main_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            module = service(Path(directory))
            day = NOW.date().isoformat()
            archive = module.archives[day]
            target = archive["success"][0]
            code = target["code"]
            archive["observations"] = {code: {**target, "main_inflow": 7_500_000, "flow_source": "eastmoney_history"}}
            target["main_inflow"] = None
            target["flow_source"] = None
            module.candidates = lambda selected, now: {code: target}
            module.worker = AsyncMock(return_value={"rows": [], "status": {"price": "unavailable", "flow": "unavailable"}})
            await module.enrich(day, NOW)
            filled = next(r for r in module.archives[day]["success"] if r["code"] == code)
            self.assertEqual(filled["main_inflow"], 7_500_000)
            self.assertEqual(filled["flow_source"], "eastmoney_history")
            self.assertEqual(module.archives[day]["aux_status"]["stock"], "partial")

    async def test_stock_enrichment_has_small_batches_and_stops_new_requests_after_rate_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            module = service(Path(directory))
            day = NOW.date().isoformat()
            self.assertGreater(len(module.candidates(day, NOW)), 3)
            module.worker = AsyncMock(return_value={"rows": [{"date": day, "main_inflow": 5_000_000}],
                                                    "status": {"price": "ready", "flow": "ready"}})
            finished = await module.enrich(day, NOW)
            self.assertFalse(finished)
            self.assertEqual(module.worker.await_count, 3)
            self.assertEqual(len(module.progress[day]), 3)
            self.assertEqual(module.archives[day]["aux_status"]["stock"], "pending")
        with tempfile.TemporaryDirectory() as directory:
            module = service(Path(directory))
            module.worker = AsyncMock(return_value={"rate_limited": True, "rows": [],
                                                    "status": {"price": "unavailable", "flow": "unavailable"}})
            await module.enrich(day, NOW)
            self.assertEqual(module.worker.await_count, 1)
            self.assertGreater(module.stock_retry_at, review.time.monotonic())
            # A second batch during cooldown also cannot launch a stock request.
            await module.enrich(day, NOW)
            self.assertEqual(module.worker.await_count, 1)

    async def test_atomic_archive_preserves_last_good_file_and_memory_on_failed_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            module = service(Path(directory))
            day = NOW.date().isoformat()
            archive = deepcopy(module.archives[day])
            await module.persist(archive)
            path = Path(directory) / f"{day}.json"
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["date"], day)
            self.assertEqual(module._read(day)["date"], day)
            old_bytes = path.read_bytes()
            replacement = {**archive, "updated_at": "changed"}
            with patch("pre_market.os.replace", side_effect=OSError("disk unavailable")):
                with self.assertRaises(OSError):
                    await module.persist(replacement)
            self.assertEqual(path.read_bytes(), old_bytes)
            self.assertEqual(module.archives[day], archive)
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])
            simulated = {**archive, "simulated": True}
            path.write_text(json.dumps(simulated), encoding="utf-8")
            self.assertIsNone(module._read(day))
            path.write_text("{broken", encoding="utf-8")
            self.assertIsNone(module._read(day))

    async def test_failed_pool_refresh_retains_archive(self):
        module = service()
        day = NOW.date().isoformat()
        old = deepcopy(module.archives[day])
        module.worker = AsyncMock(return_value={"success": [], "broken": [],
                                                "status": {"success": "unavailable", "broken": "ready"}})
        with self.assertRaises(ValueError):
            await module.refresh_core(day)
        self.assertEqual(module.archives[day], old)

    async def test_subprocess_is_killed_and_reaped_when_parent_cancels(self):
        module = service()
        entered = asyncio.Event()
        async def communicate(data):
            entered.set()
            await asyncio.Event().wait()
        process = SimpleNamespace(returncode=None, communicate=communicate, kill=Mock(), wait=AsyncMock())
        with patch("review.asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)):
            task = asyncio.create_task(module.worker("pools", date="2026-11-26"))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()


class ReviewApiChecks(unittest.TestCase):
    def test_auth_read_only_queries_parameter_validation_and_refresh_throttle(self):
        module = service()
        module.worker = AsyncMock(side_effect=AssertionError("GET must not access network"))
        monitor = SimpleNamespace(review=module)
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW if tz else NOW.replace(tzinfo=None)
        @asynccontextmanager
        async def lifespan(app):
            app.state.monitor = monitor
            yield
        with patch.object(main, "settings", module.cfg), patch.object(main.app.router, "lifespan_context", lifespan), patch.object(review, "datetime", FixedDateTime):
            with TestClient(main.app) as client:
                headers = {"authorization": "Bearer review-test"}
                for path in ("/api/review/meta", "/api/review/ladder"):
                    self.assertEqual(client.get(path).status_code, 401)
                self.assertEqual(client.post("/api/review/refresh", json={}).status_code, 401)
                meta = client.get("/api/review/meta", headers=headers).json()
                self.assertEqual(meta["default_date"], "2026-11-26")
                self.assertEqual(len(meta["dates"]), 15)
                response = client.get("/api/review/ladder?mode=review&date=2026-11-26", headers=headers)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["summary"]["max_board"], 5)
                for query in ("mode=unknown", "date=2026-11-99", "date=../../.env", "date=2020-01-01"):
                    self.assertEqual(client.get("/api/review/ladder?" + query, headers=headers).status_code, 422)
                for body in ({"date": "../../.env"}, {"date": "2020-01-01"}, {"unexpected": 1}):
                    self.assertEqual(client.post("/api/review/refresh", headers=headers, json=body).status_code, 422)
                response = client.post("/api/review/refresh", headers=headers, json={"date": "2026-11-26", "lhb": True})
                self.assertEqual(response.status_code, 202)
                self.assertEqual(response.json(), {"status": "queued"})
                self.assertEqual(module.requests.get_nowait(), ("2026-11-26", True))
                self.assertEqual(client.post("/api/review/refresh", headers=headers, json={}).status_code, 429)
                module.refresh_pending = False
                self.assertEqual(client.post("/api/review/refresh", headers=headers, json={}).status_code, 429)
                module.last_manual -= 31
                self.assertEqual(client.post("/api/review/refresh", headers=headers, json={}).status_code, 202)
        module.worker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
