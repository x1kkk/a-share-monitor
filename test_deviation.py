"""Offline deviation checks: python -m unittest -v test_deviation."""
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

import deviation
import main
from config import SHANGHAI, Settings
from deviation import (BENCHMARKS, DeviationService, build_report, calculate_metrics,
                       classify_item, mock_report, risk_tier, select_candidates)
from pre_market import MarketCalendar
from review import main_board, mock_sessions

NOW = datetime(2026, 11, 26, 16, tzinfo=SHANGHAI)
SESSIONS = [d for d in mock_sessions(NOW.date()) if d <= NOW.date()][-31:]


def bars(values):
    return [{"date": day.isoformat(), "close": close} for day, close in zip(SESSIONS, values)]


def service(path=None, *, mock=False):
    calendar = MarketCalendar(sessions=mock_sessions(NOW.date()), loaded_for=NOW.date(), error=None)
    monitor = SimpleNamespace(cfg=replace(Settings(), mock_mode=mock, api_token="deviation-test"),
                              calendar=calendar,
                              fetcher=SimpleNamespace(universe=SimpleNamespace(stocks={})),
                              review=SimpleNamespace(archives={}, _read=lambda day: None))
    return DeviationService(monitor, path)


class DeviationMathChecks(unittest.TestCase):
    def test_compound_return_is_not_the_naive_remaining_percentage(self):
        metrics = calculate_metrics(bars([100] * 30 + [190]), bars([100] * 31), SESSIONS)
        self.assertEqual(metrics["dev_10"], 90)
        self.assertAlmostEqual(metrics["remain_10"], (2 / 1.9 - 1) * 100, places=8)
        self.assertNotAlmostEqual(metrics["remain_10"], 100 - metrics["dev_10"])
        self.assertAlmostEqual(metrics["projected_dev_10_if_limit_up"], 109)
        self.assertTrue(metrics["tomorrow_cross"])
        self.assertIsNone(metrics["tomorrow_gap"])

    def test_tomorrow_window_drops_t_minus_ten_and_scans_every_legal_base(self):
        prices = [100] * 31
        prices[20] = 50  # T-10 contributes to today's endpoint, but not tomorrow's 10-day window.
        metrics = calculate_metrics(bars(prices), bars([100] * 31), SESSIONS)
        self.assertEqual(metrics["dev_10"], 100)
        self.assertEqual(metrics["remain_10"], 100)
        self.assertTrue(metrics["already_triggered"])
        self.assertEqual(metrics["projected_dev_10_if_limit_up"], 10)
        prices[25] = 60  # The tightest legal base is inside the window, not its first day.
        prices[20] = 100
        metrics = calculate_metrics(bars(prices), bars([100] * 31), SESSIONS)
        self.assertEqual(metrics["remain_10"], 20)
        self.assertEqual(metrics["tightest_base_10"], SESSIONS[25].isoformat())

    def test_benchmark_compounds_and_changes_the_tightest_required_return(self):
        metrics = calculate_metrics(bars([100] * 30 + [190]), bars([100] * 30 + [110]), SESSIONS)
        self.assertEqual(metrics["dev_10"], 80)
        self.assertAlmostEqual(metrics["remain_10"], (2.1 / 1.9 - 1) * 100, places=8)
        self.assertEqual(metrics["projected_dev_10_if_limit_up"], 99)
        self.assertFalse(metrics["tomorrow_cross"])
        self.assertEqual(metrics["tomorrow_gap"], 0.53)

    def test_thirty_day_endpoint_needs_thirty_one_closes_and_missing_is_not_zero(self):
        prices, indexes = bars([100] * 30 + [190]), bars([100] * 31)
        metrics = calculate_metrics(prices[1:], indexes[1:], SESSIONS)
        self.assertIsNone(metrics["dev_30"])
        self.assertEqual(metrics["dev_10"], 90)
        for changed in (prices[:-2] + prices[-1:], [{**r, "close": 0} if i == 29 else r for i, r in enumerate(prices)]):
            with self.subTest(changed=changed[-2:]):
                result = calculate_metrics(changed, indexes, SESSIONS)
                self.assertIsNone(result["dev_10"])
                self.assertIsNone(result["remain_10"])
                self.assertIsNone(result["effective_remain"])
                self.assertIsNone(result["tomorrow_cross"])
                self.assertIsNone(result["tomorrow_gap"])
        future = {"date": (SESSIONS[-1] + timedelta(days=1)).isoformat(), "close": 999999}
        self.assertEqual(calculate_metrics(prices + [future], indexes + [future], SESSIONS),
                         calculate_metrics(prices, indexes, SESSIONS))
        with self.assertRaises(ValueError):
            calculate_metrics(prices + prices[-1:], indexes, SESSIONS)
        with self.assertRaises(ValueError):
            calculate_metrics(prices, indexes, list(reversed(SESSIONS)))

    def test_near_long_line_negative_remaining_and_exact_limit_up_boundary(self):
        near = classify_item({"remain_10": 30, "remain_30": 18, "dev_10": 61})
        self.assertTrue(near["near_line_30"])
        self.assertEqual(near["effective_remain"], 18)
        self.assertEqual(near["risk_tier"], "B")
        self.assertTrue(near["over_60"])
        for distance, crossed in ((-3, True), (0, True), (10, True), (10.01, False)):
            result = classify_item({"remain_10": distance, "remain_30": 100})
            self.assertEqual(result["tomorrow_cross"], crossed)
            self.assertEqual(result["already_triggered"], distance <= 0)
            self.assertEqual(result["tomorrow_gap"], None if crossed else 0.01)
        self.assertIsNone(classify_item({"remain_10": 5})["effective_remain"])

    def test_tier_boundaries_and_report_counts_keep_null_rows_last(self):
        values = [-1, 15.999, 16, 31.999, 32, 55, 55.001, None]
        self.assertEqual([risk_tier(x) for x in values], ["A", "A", "B", "B", "C", "C", "over_55", None])
        rows = [{"code": f"600{i:03d}", "name": "主板样本", "remain_10": value,
                 "remain_30": 100 if value is not None else None,
                 "dev_10": 60 if i % 2 else 59.99, "dev_30": 80,
                 "data_status": "ready" if value is not None else "unavailable"}
                for i, value in enumerate(values)]
        report = build_report(NOW.date().isoformat(), list(reversed(rows)),
                              [{**b, "status": "ready"} for b in BENCHMARKS], SESSIONS)
        self.assertEqual(report["summary"]["tier_counts"], {"A": 2, "B": 2, "C": 2, "over_55": 1, "unknown": 1})
        self.assertEqual(report["summary"]["over_60_count"], 4)
        self.assertEqual(report["summary"]["failed_count"], 1)
        self.assertEqual(report["items"][-1]["code"], "600007")
        self.assertEqual([r["effective_remain"] for r in report["near_trigger_top"]], [-1, 15.999, 16])
        self.assertTrue(report["summary"]["window_10_range"])
        self.assertTrue(report["summary"]["window_30_range"])
        ready_rows = [row for row in rows if row["data_status"] == "ready"]
        coverage_partial = build_report(NOW.date().isoformat(), ready_rows,
                                        [{**b, "status": "ready"} for b in BENCHMARKS], SESSIONS,
                                        coverage={"review_days_ready": 14, "review_days_requested": 15,
                                                  "strong_pool_status": "ready"})
        self.assertEqual(coverage_partial["status"], "partial")
        self.assertEqual(coverage_partial["summary"]["failed_count"], 0)

    def test_candidate_hard_filter_deduplication_and_bounded_download_set(self):
        invalid = [{"code": code, "name": "错误市场"} for code in ("300001", "301001", "688001", "830001", "920001")]
        invalid += [{"code": "600999", "name": label} for label in ("ST风险", "*st风险", "风险St")]
        review_rows = [{"code": f"600{i:03d}", "name": f"样本{i}"} for i in range(200)]
        strong = [{"code": f"002{i:03d}", "name": f"强势{i}"} for i in range(70)]
        archives = {"2026-11-25": {"success": review_rows + invalid, "broken": review_rows[:3]}}
        selected, coverage = select_candidates(archives, ["2026-11-25"], strong + invalid)
        self.assertEqual(len(selected), 150)
        self.assertEqual(len({r["code"] for r in selected}), 150)
        self.assertTrue(all(main_board(r) for r in selected))
        self.assertEqual(coverage["pool_union_count"], 270)
        self.assertEqual(coverage["truncated_count"], 120)
        self.assertEqual(sum(r["code"].startswith("002") for r in selected[:50]), 50)
        report = build_report("2026-11-26", invalid, BENCHMARKS, SESSIONS)
        self.assertEqual(report["items"], [])

    def test_latest_st_identity_cannot_return_under_an_older_normal_name(self):
        archives = {
            "2026-11-25": {"success": [{"code": "600001", "name": "旧正常名称"},
                                         {"code": "600003", "name": "另一个旧名称"}], "broken": []},
            "2026-11-26": {"success": [], "broken": [{"code": "600001", "name": "*sT风险"},
                                                          {"code": "600002", "name": "正常主板"}]} }
        selected, _ = select_candidates(archives, ["2026-11-25", "2026-11-26"],
                                        [{"code": "600003", "name": "ST新风险"}])
        self.assertEqual([row["code"] for row in selected], ["600002"])

    def test_lock_boundaries_use_exchange_sessions_and_1508_settlement_gate(self):
        module = service()
        for stamp, expected in (("2026-11-26T14:59:00+08:00", "2026-11-25"),
                                ("2026-11-26T15:01:00+08:00", "2026-11-26"),
                                ("2026-11-28T12:00:00+08:00", "2026-11-27")):
            now = datetime.fromisoformat(stamp)
            module.monitor.calendar.loaded_for = now.date()
            self.assertEqual(module.meta(now)["default_date"], expected)
        self.assertFalse(module.due("2026-11-26", NOW.replace(hour=15, minute=7)))
        self.assertTrue(module.due("2026-11-26", NOW.replace(hour=15, minute=8)))
        self.assertTrue(module.due("2026-11-25", NOW.replace(hour=10)))
        module.monitor.calendar.loaded_for = NOW.date()
        self.assertFalse(module.meta(NOW.replace(hour=10))["refresh_allowed"])
        self.assertFalse(module.meta(NOW.replace(hour=15, minute=1))["refresh_allowed"])
        self.assertTrue(module.meta(NOW)["refresh_allowed"])
        module.archives["2026-11-25"] = mock_report("2026-11-25", module.monitor.calendar.sessions)
        old = deepcopy(module.archives["2026-11-25"])
        with self.assertRaises(PermissionError):
            module.queue_refresh("2026-11-25", NOW.replace(hour=10))
        report = module.report(now=NOW.replace(hour=10))
        self.assertEqual(report["items"], old["items"])
        self.assertEqual(report["summary"]["updated_at"], old["summary"]["updated_at"])
        self.assertEqual(module.archives["2026-11-25"], old)
        self.assertEqual(module.report(now=NOW.replace(hour=15, minute=1))["status"], "scheduled")

    def test_mock_two_dates_cover_every_tier_and_multiple_long_window_near_cases(self):
        module = service(mock=True)
        days = module.dates(NOW)[-2:]
        for day in days:
            report = mock_report(day.isoformat(), module.sessions(NOW))
            self.assertEqual(report, mock_report(day.isoformat(), module.sessions(NOW)))
            self.assertGreaterEqual(len(report["items"]), 15)
            self.assertTrue(all(main_board(row) for row in report["items"]))
            for row in report["items"]:
                self.assertEqual(row["benchmark"]["code"], "000002" if row["code"].startswith("60") else "399107")
            self.assertTrue(all(report["risk_ladder"][tier] for tier in ("A", "B", "C")))
            self.assertTrue(report["risk_ladder"]["over_55_items"])
            self.assertGreaterEqual(sum(row["near_line_30"] for row in report["risk_ladder"]["B"]), 2)
            self.assertTrue(any(row["tomorrow_cross"] for row in report["risk_ladder"]["A"]))
            self.assertTrue(any(row["tomorrow_cross"] is False for row in report["risk_ladder"]["A"]))
            self.assertGreaterEqual(report["summary"]["over_60_count"], 2)


class DeviationPersistenceChecks(unittest.IsolatedAsyncioTestCase):
    async def test_atomic_persist_failure_and_cache_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            module = service(Path(directory))
            archive = mock_report("2026-11-26", module.monitor.calendar.sessions)
            archive["simulated"] = False
            await module.persist(archive)
            file = module.path / "2026-11-26.json"
            old = file.read_bytes()
            self.assertIsNotNone(module._read("2026-11-26"))
            with patch("pre_market.os.replace", side_effect=OSError("disk unavailable")):
                with self.assertRaises(OSError):
                    await module.persist({**archive, "status": "partial"})
            self.assertEqual(file.read_bytes(), old)
            self.assertEqual(module.archives["2026-11-26"], archive)
            self.assertEqual(list(module.path.glob("*.tmp")), [])
            for corrupted in ("{broken", json.dumps({**archive, "simulated": True}),
                              json.dumps({**archive, "date": "2026-11-25"})):
                file.write_text(corrupted, encoding="utf-8")
                self.assertIsNone(module._read("2026-11-26"))
            other = service(Path(directory), mock=True)
            self.assertEqual(other.path, Path(directory) / "mock")

    async def test_cache_rebuilds_derived_pools_from_validated_items(self):
        with tempfile.TemporaryDirectory() as directory:
            module = service(Path(directory))
            archive = mock_report("2026-11-26", module.monitor.calendar.sessions)
            archive["simulated"] = False
            injected = {**archive["items"][0], "code": "300001", "name": "错误市场"}
            archive["risk_ladder"]["A"].append(injected)
            archive["risk_ladder"]["over_55_count"] = 999
            archive["near_trigger_top"].append(injected)
            archive["summary"]["total_candidates"] = 999
            archive["summary"]["over_60_count"] = 999
            await module.persist(archive)
            loaded = module._read("2026-11-26")
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["summary"]["total_candidates"], len(loaded["items"]))
            self.assertEqual(loaded["summary"]["over_60_count"], sum(r["over_60"] for r in loaded["items"]))
            self.assertEqual(loaded["risk_ladder"]["over_55_count"], len(loaded["risk_ladder"]["over_55_items"]))
            self.assertTrue(all(main_board(row) for row in loaded["near_trigger_top"]))
            self.assertTrue(all(main_board(row) for tier in ("A", "B", "C") for row in loaded["risk_ladder"][tier]))

    async def test_partial_refresh_preserves_old_valid_stock_while_filling_another(self):
        with tempfile.TemporaryDirectory() as directory:
            module = service(Path(directory))
            prices, indexes = bars([100] * 30 + [190]), bars([100] * 31)
            benchmarks = [{**base, "status": "ready", "rows": indexes} for base in BENCHMARKS]
            good = {"code": "600001", "name": "原有效股", "data_status": "ready",
                    "benchmark": {k: v for k, v in benchmarks[0].items() if k != "rows"},
                    **calculate_metrics(prices, indexes, SESSIONS)}
            missing = {"code": "600002", "name": "原缺失股", "data_status": "unavailable", **classify_item({})}
            archive = build_report("2026-11-26", [good, missing], benchmarks, SESSIONS)
            archive.update(version=1, simulated=False)
            self.assertEqual(archive["status"], "partial")
            await module.persist(archive)
            async def worker(operation, **body):
                if operation == "benchmarks":
                    return {"benchmarks": benchmarks}
                if operation == "candidates":
                    return {"items": [{"code": row["code"], "name": row["name"]} for row in (good, missing)], "status": "ready"}
                if body["code"] == good["code"]:
                    return {"rows": [], "status": "unavailable", "adjust": "qfq"}
                return {"rows": prices, "status": "ready", "adjust": "qfq"}
            module.worker = AsyncMock(side_effect=worker)
            class FixedDatetime(datetime):
                @classmethod
                def now(cls, tz=None):
                    return NOW if tz else NOW.replace(tzinfo=None)
            with patch.object(deviation, "datetime", FixedDatetime):
                result = await module.settle("2026-11-26", NOW)
            found = {row["code"]: row for row in result["items"]}
            self.assertEqual(found[good["code"]]["dev_10"], good["dev_10"])
            self.assertEqual(found[good["code"]]["remain_10"], good["remain_10"])
            self.assertEqual(found[good["code"]]["data_status"], "ready")
            self.assertEqual(found[missing["code"]]["data_status"], "ready")

    async def test_fallback_benchmark_with_incomplete_new_stock_keeps_whole_old_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            module = service(Path(directory))
            prices, indexes = bars([100] * 30 + [190]), bars([100] * 31)
            benchmarks = [{**base, "status": "ready", "rows": indexes} for base in BENCHMARKS]
            good = {"code": "600001", "name": "原有效股", "data_status": "ready",
                    "benchmark": {k: v for k, v in benchmarks[0].items() if k != "rows"},
                    **calculate_metrics(prices, indexes, SESSIONS)}
            archive = build_report("2026-11-26", [good], benchmarks, SESSIONS)
            archive.update(version=1, simulated=False)
            await module.persist(archive)
            original_bytes = (module.path / "2026-11-26.json").read_bytes()
            fallback = [{**benchmarks[0], "code": "000001", "name": "上证指数", "fallback": True}, benchmarks[1]]
            async def worker(operation, **body):
                if operation == "benchmarks":
                    return {"benchmarks": fallback}
                if operation == "candidates":
                    return {"items": [{"code": good["code"], "name": good["name"]}], "status": "ready"}
                return {"rows": [], "status": "unavailable", "adjust": "qfq"}
            module.worker = AsyncMock(side_effect=worker)
            with self.assertRaisesRegex(ValueError, "基准"):
                await module.settle("2026-11-26", NOW)
            self.assertEqual(module.archives["2026-11-26"], archive)
            self.assertEqual((module.path / "2026-11-26.json").read_bytes(), original_bytes)
            self.assertFalse(module.lock.locked())
            self.assertIsNone(module.current_date)

    async def test_subprocess_cancellation_reaps_worker_and_rate_limit_blocks_new_calls(self):
        module = service()
        started = asyncio.Event()
        async def communicate(payload):
            started.set()
            await asyncio.Event().wait()
        process = SimpleNamespace(returncode=None, communicate=communicate, kill=Mock(), wait=AsyncMock())
        with patch("deviation.asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)):
            task = asyncio.create_task(module.worker("benchmarks", dates=[]))
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()
        module.stock_retry_at = deviation.time.monotonic() + 1200
        with patch("deviation.asyncio.create_subprocess_exec", new=AsyncMock()) as create:
            result = await module.worker("stock", code="600001")
        self.assertEqual(result["status"], "unavailable")
        create.assert_not_called()


class DeviationApiChecks(unittest.TestCase):
    def test_auth_read_only_queries_date_validation_and_refresh_lock(self):
        module = service()
        archive = mock_report("2026-11-26", module.monitor.calendar.sessions)
        archive["simulated"] = False
        module.archives["2026-11-26"] = archive
        module.worker = AsyncMock(side_effect=AssertionError("GET must not fetch"))
        class FixedDatetime(datetime):
            clock = NOW
            @classmethod
            def now(cls, tz=None):
                return cls.clock if tz else cls.clock.replace(tzinfo=None)
        @asynccontextmanager
        async def lifespan(app):
            app.state.monitor = SimpleNamespace(deviation=module)
            yield
        with patch.object(main, "settings", module.cfg), patch.object(main.app.router, "lifespan_context", lifespan), patch.object(deviation, "datetime", FixedDatetime):
            with TestClient(main.app) as client:
                headers = {"authorization": "Bearer deviation-test"}
                for route in ("/api/deviation/meta", "/api/deviation/report"):
                    self.assertEqual(client.get(route).status_code, 401)
                self.assertEqual(client.post("/api/deviation/refresh", json={}).status_code, 401)
                meta = client.get("/api/deviation/meta", headers=headers)
                self.assertEqual(meta.status_code, 200)
                self.assertEqual(meta.json()["default_date"], "2026-11-26")
                response = client.get("/api/deviation/report?date=2026-11-26", headers=headers)
                self.assertEqual(response.status_code, 200)
                self.assertGreaterEqual(response.json()["summary"]["total_candidates"], 15)
                for value in ("2026-11-99", "../../.env", "2020-01-01"):
                    self.assertEqual(client.get("/api/deviation/report?date=" + value, headers=headers).status_code, 422)
                self.assertEqual(client.post("/api/deviation/refresh", json={"extra": True}, headers=headers).status_code, 422)
                response = client.post("/api/deviation/refresh", json={"date": "2026-11-26"}, headers=headers)
                self.assertEqual(response.status_code, 202)
                self.assertEqual(module.queue.get_nowait(), "2026-11-26")
                self.assertEqual(client.post("/api/deviation/refresh", json={}, headers=headers).status_code, 429)
                module.pending = False
                module.last_manual -= 31
                FixedDatetime.clock = NOW.replace(hour=10)
                self.assertEqual(client.post("/api/deviation/refresh", json={"date": "2026-11-25"}, headers=headers).status_code, 409)
                FixedDatetime.clock = NOW.replace(hour=15, minute=1)
                self.assertEqual(client.post("/api/deviation/refresh", json={"date": "2026-11-26"}, headers=headers).status_code, 409)
        module.worker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
