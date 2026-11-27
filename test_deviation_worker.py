"""Offline checks for deviation worker data boundaries and labelled fallbacks."""
import sys
import types
import unittest
from datetime import date, timedelta
from unittest.mock import Mock, patch

import deviation_worker as worker


DATES = [(date(2026, 9, 1) + timedelta(days=i)).isoformat() for i in range(60)
         if (date(2026, 9, 1) + timedelta(days=i)).weekday() < 5][:31]


class Frame:
    def __init__(self, rows):
        self.rows = rows
        self.columns = set().union(*(row.keys() for row in rows)) if rows else set()

    def to_dict(self, orientation):
        assert orientation == "records"
        return self.rows


def prices(dates=DATES, close=10):
    return [{"date": day, "close": close} for day in dates]


def body(**extra):
    return {"code": "600000", "name": "测试主板", "dates": DATES,
            "start_date": DATES[0], "end_date": DATES[-1], **extra}


class DeviationWorkerChecks(unittest.TestCase):
    def test_exact_dates_positive_closes_duplicates_and_session_validation(self):
        rows = [{"日期": DATES[0], "收盘": "10.02"}, {"date": DATES[1], "close": 0},
                {"date": DATES[2], "close": None}, {"date": DATES[3], "close": float("nan")},
                {"date": DATES[4], "close": True}, {"date": "2099-01-01", "close": 99}]
        self.assertEqual(worker.close_rows(rows, DATES), [{"date": DATES[0], "close": 10.02}])
        with self.assertRaises(ValueError):
            worker.close_rows(rows + [rows[0]], DATES)
        self.assertEqual(worker.requested_dates(DATES), DATES)
        for value in (None, DATES[:30], list(reversed(DATES)), DATES + [DATES[-1]]):
            with self.assertRaises(ValueError):
                worker.requested_dates(value)

    def test_stock_is_explicit_qfq_and_rejects_nonmainboard_or_unknown_names(self):
        ak = types.SimpleNamespace(stock_zh_a_hist=Mock(return_value=Frame(prices())),
                                   stock_zh_a_daily=Mock(side_effect=AssertionError("unneeded fallback")))
        result = worker.stock(ak, body())
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["adjust"], "qfq")
        self.assertEqual(result["source"], "akshare.stock_zh_a_hist")
        ak.stock_zh_a_hist.assert_called_once_with(symbol="600000", period="daily", adjust="qfq",
                                                  start_date=DATES[0].replace("-", ""), end_date=DATES[-1].replace("-", ""))
        ak.stock_zh_a_daily.assert_not_called()
        for code, name in (("300001", "主板"), ("688001", "主板"), ("920001", "主板"),
                           ("830001", "主板"), ("430001", "主板"), ("600001", "*sT风险"),
                           ("600001", None), ("600001", "退市样本")):
            with self.assertRaises(ValueError):
                worker.stock(ak, body(code=code, name=name))
        for code in ("000001", "001001", "002001", "003001", "600001", "601001", "603001", "605001"):
            self.assertEqual(worker.stock(ak, body(code=code))["status"], "ready")

    def test_qfq_source_fallback_never_mix_prices_or_fill_missing_days(self):
        ak = types.SimpleNamespace(stock_zh_a_hist=Mock(return_value=Frame(prices(DATES[:-1], 9))),
                                   stock_zh_a_daily=Mock(return_value=Frame(prices(close=12))))
        with patch.object(worker, "timeout"):
            result = worker.stock(ak, body())
        self.assertEqual(result["status"], "ready")
        self.assertEqual({row["close"] for row in result["rows"]}, {12})
        self.assertEqual(result["source"], "akshare.stock_zh_a_daily")
        self.assertEqual(ak.stock_zh_a_daily.call_args.kwargs["adjust"], "qfq")
        self.assertEqual(ak.stock_zh_a_daily.call_args.kwargs["symbol"], "sh600000")
        ak.stock_zh_a_daily = Mock(return_value=Frame(prices(DATES[1:], 12)))
        with patch.object(worker, "timeout"):
            result = worker.stock(ak, body())
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["rows"]), 30)
        self.assertEqual({row["close"] for row in result["rows"]}, {9})
        self.assertNotIn(DATES[-1], [row["date"] for row in result["rows"]])

    def test_stock_rate_limit_only_skips_the_limited_provider(self):
        class Push2RateLimitError(Exception):
            pass
        ak = types.SimpleNamespace(stock_zh_a_hist=Mock(side_effect=Push2RateLimitError()),
                                   stock_zh_a_daily=Mock(return_value=Frame(prices(close=12))))
        with patch.object(worker, "timeout"):
            result = worker.stock(ak, body())
        self.assertTrue(result["rate_limited"])
        self.assertEqual(result["limited_sources"], {"eastmoney": True, "sina": False})
        self.assertEqual(result["status"], "ready")
        self.assertEqual({row["close"] for row in result["rows"]}, {12})
        ak.stock_zh_a_hist.reset_mock()
        with patch.object(worker, "timeout"):
            self.assertEqual(worker.stock(ak, body(skip_eastmoney=True))["status"], "ready")
        ak.stock_zh_a_hist.assert_not_called()

    def test_benchmark_limit_preserves_a_working_same_index_source(self):
        class Push2RateLimitError(Exception):
            pass
        ak = types.SimpleNamespace(stock_zh_index_daily_em=Mock(side_effect=Push2RateLimitError()),
                                   stock_zh_index_daily=Mock(return_value=Frame(prices())))
        with patch.object(worker, "timeout"):
            result = worker.benchmarks(ak, DATES)
        self.assertEqual(result["limited_sources"], {"eastmoney": True, "sina": False})
        self.assertTrue(all(row["status"] == "ready" and not row["fallback"] for row in result["benchmarks"]))
        ak.stock_zh_index_daily_em.reset_mock()
        with patch.object(worker, "timeout"):
            cooled = worker.benchmarks(ak, DATES, skipped={"eastmoney": True})
        ak.stock_zh_index_daily_em.assert_not_called()
        self.assertTrue(all(row["status"] == "ready" for row in cooled["benchmarks"]))

    def test_benchmarks_try_same_identity_then_label_actual_fallback(self):
        calls = []
        def em(**kwargs):
            symbol = kwargs["symbol"]
            calls.append(("em", symbol))
            if symbol == "sh000002":
                raise TimeoutError()
            return Frame(prices(DATES[:-1] if symbol == "sz399107" else DATES, 3000))
        def sina(**kwargs):
            symbol = kwargs["symbol"]
            calls.append(("sina", symbol))
            if symbol == "sz399107":
                raise TimeoutError()
            return Frame(prices(close=3200))
        ak = types.SimpleNamespace(stock_zh_index_daily_em=em, stock_zh_index_daily=sina)
        with patch.object(worker, "timeout"):
            result = worker.benchmarks(ak, DATES)
        sh, sz = result["benchmarks"]
        self.assertEqual((sh["code"], sh["name"], sh["fallback"], sh["status"]), ("000002", "上证A指", False, "ready"))
        self.assertEqual(sh["source"], "akshare.stock_zh_index_daily")
        self.assertEqual((sz["code"], sz["name"], sz["fallback"], sz["status"]), ("399001", "深证成指", True, "ready"))
        self.assertNotIn(("em", "sh000001"), calls)
        self.assertNotIn(("sina", "sz399001"), calls)

    def test_incomplete_benchmark_is_not_reported_ready(self):
        ak = types.SimpleNamespace(stock_zh_index_daily_em=lambda **kwargs: Frame(prices(DATES[:-1])),
                                   stock_zh_index_daily=lambda **kwargs: Frame([]))
        result = worker.benchmark(ak, "sh", worker.BENCHMARKS[0][1], DATES)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["code"], "000002")
        self.assertEqual(len(result["rows"]), 30)

    def test_candidates_hard_filter_empty_vs_unavailable_and_wrong_date(self):
        http = types.SimpleNamespace()
        payload = {"data": {"pool": [], "date": DATES[-1].replace("-", "")}}
        original = lambda *args, **kwargs: types.SimpleNamespace(json=lambda: payload)
        http.get = original
        utils = types.ModuleType("akshare.utils"); utils.http = http
        package = types.ModuleType("akshare"); package.__path__ = []
        records = []
        def fetch(**kwargs):
            self.assertEqual(kwargs["date"], DATES[-1].replace("-", ""))
            http.get("pool")
            return Frame(records)
        ak = types.SimpleNamespace(stock_zt_pool_strong_em=fetch)
        with patch.dict(sys.modules, {"akshare": package, "akshare.utils": utils}):
            self.assertEqual(worker.candidates(ak, DATES[-1])["status"], "ready")
            payload["data"] = None
            self.assertEqual(worker.candidates(ak, DATES[-1])["status"], "unavailable")
            payload["data"] = {"pool": [], "date": "20990101"}
            self.assertEqual(worker.candidates(ak, DATES[-1])["status"], "unavailable")
            payload["data"] = {"pool": []}
            records.extend([{"代码": "600000", "名称": "浦发银行"}, {"代码": "002001", "名称": "主板样本"},
                            {"代码": "300001", "名称": "创业样本"}, {"代码": "688001", "名称": "科创样本"},
                            {"代码": "600001", "名称": "ST样本"}, {"代码": "600002", "名称": None}])
            result = worker.candidates(ak, DATES[-1])
        self.assertEqual(result["items"], [{"code": "600000", "name": "浦发银行"}, {"code": "002001", "name": "主板样本"}])
        self.assertIs(http.get, original)


if __name__ == "__main__":
    unittest.main()
