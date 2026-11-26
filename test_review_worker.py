"""Offline review-worker boundary and provider normalization checks."""
import sys
import types
import unittest
from unittest.mock import patch

import review_worker as worker


class Frame:
    def __init__(self, rows):
        self.rows = rows
        self.empty = not rows
        self.columns = set().union(*(r.keys() for r in rows)) if rows else set()

    def to_dict(self, orientation):
        assert orientation == "records"
        return self.rows


class ReviewWorkerChecks(unittest.TestCase):
    def test_finite_values_mainboard_and_pool_units(self):
        for value in (None, "-", "nan", float("inf"), True):
            self.assertIsNone(worker.number(value))
        self.assertEqual(worker.number("0"), 0)
        for code in ("000001", "001001", "002001", "003001", "600001", "601001", "603001", "605001"):
            self.assertTrue(worker.main_board(code, "样本"))
        for code, name in (("688001", "样本"), ("300001", "样本"), ("301001", "样本"),
                           ("830001", "样本"), ("430001", "样本"), ("920001", "样本"),
                           ("000001", "*sT样本"), ("600000", "St样本")):
            self.assertFalse(worker.main_board(code, name))
        raw = {"代码": "600000", "名称": "样本", "最新价": 10.1, "封板资金": 0,
               "首次封板时间": 93001, "连板数": 5, "炸板次数": 0}
        sealed = worker.pool_rows([raw], True)[0]
        self.assertEqual((sealed["price"], sealed["seal_amount"], sealed["streak_days"]), (10.1, 0, 5))
        self.assertEqual(sealed["first_seal_time"], "09:30:01")
        self.assertIsNone(sealed["amount"])
        self.assertIsNone(worker.pool_rows([raw], False)[0]["seal_amount"])
        self.assertEqual(worker.pool_rows([{**raw, "名称": "ST样本"}], True), [])
        self.assertIsNone(worker.seal_time(0))
        with self.assertRaises(ValueError):
            worker.pool_rows([raw, raw], True)

    def test_empty_pool_is_distinct_from_unavailable(self):
        http = types.SimpleNamespace()
        payloads = {"success": {"data": {"pool": []}}, "broken": {"data": None}}
        original = lambda key, **kwargs: types.SimpleNamespace(json=lambda: payloads[key])
        http.get = original
        utils = types.ModuleType("akshare.utils")
        utils.http = http
        package = types.ModuleType("akshare")
        package.__path__ = []

        def fetch(key):
            def call(**kwargs):
                http.get(key)
                return Frame([])
            return call

        ak = types.SimpleNamespace(stock_zt_pool_em=fetch("success"), stock_zt_pool_zbgc_em=fetch("broken"))
        with patch.dict(sys.modules, {"akshare": package, "akshare.utils": utils}):
            result = worker.pools(ak, "2026-11-26")
        self.assertEqual(result["status"], {"success": "ready", "broken": "unavailable"})
        self.assertIs(http.get, original)

    def test_pool_failure_is_local_and_restores_http(self):
        http = types.SimpleNamespace(get=lambda *args, **kwargs: None)
        original = http.get
        utils = types.ModuleType("akshare.utils")
        utils.http = http
        package = types.ModuleType("akshare")
        package.__path__ = []

        def fail(**kwargs):
            raise TimeoutError()

        ak = types.SimpleNamespace(stock_zt_pool_em=fail,
                                  stock_zt_pool_zbgc_em=lambda **kwargs: Frame([
                                      {"代码": "600000", "名称": "样本", "最新价": 10}]))
        with patch.dict(sys.modules, {"akshare": package, "akshare.utils": utils}):
            result = worker.pools(ak, "2026-11-26")
        self.assertEqual(result["status"], {"success": "unavailable", "broken": "ready"})
        self.assertIs(http.get, original)

    def test_stock_dates_units_and_partial_failure(self):
        prices = [{"日期": "2026-11-25", "收盘": 12, "成交量": 100},
                  {"日期": "2026-11-26", "收盘": 13, "成交额": 10000}]
        flows = [{"日期": "2026-11-25", "主力净流入-净额": 0},
                 {"日期": "2026-11-27", "主力净流入-净额": 900}]
        rows = worker.stock_rows(prices, flows, "2026-11-25", "2026-11-26")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["main_inflow"], 0)
        self.assertEqual(rows[0]["volume"], 100)
        self.assertIsNone(rows[0]["amount"])
        self.assertIsNone(rows[1]["main_inflow"])

        class Push2RateLimitError(Exception):
            pass

        def fail(**kwargs):
            raise Push2RateLimitError()

        ak = types.SimpleNamespace(stock_zh_a_hist=lambda **kwargs: Frame(prices), stock_individual_fund_flow=fail)
        result = worker.stock(ak, {"code": "600000", "start_date": "2026-11-25", "end_date": "2026-11-26"})
        self.assertEqual(result["status"], {"price": "ready", "flow": "unavailable"})
        self.assertEqual(len(result["rows"]), 2)
        self.assertTrue(result["rate_limited"])
        calls = []
        ak.stock_zh_a_hist = fail
        ak.stock_individual_fund_flow = lambda **kwargs: calls.append(kwargs)
        result = worker.stock(ak, {"code": "600000", "start_date": "2026-11-25", "end_date": "2026-11-26"})
        self.assertEqual(calls, [])
        self.assertEqual(result["status"], {"price": "unavailable", "flow": "unavailable"})
        with self.assertRaises(ValueError):
            worker.stock(ak, {"code": "300001", "start_date": "2026-11-25", "end_date": "2026-11-26"})

    def test_lhb_repeated_reasons_conflicts_and_multiday_scope(self):
        raw = {"代码": "600000", "名称": "样本", "上榜日": "2026-11-25", "龙虎榜净买额": 100}
        self.assertEqual(worker.lhb_rows([raw, raw], "2026-11-25"), {"600000": 100})
        self.assertEqual(worker.lhb_rows([raw, {**raw, "龙虎榜净买额": 200}], "2026-11-25"), {"600000": None})
        self.assertEqual(worker.lhb_rows([{**raw, "龙虎榜净买额": 0}], "2026-11-25"), {"600000": 0})
        self.assertEqual(worker.lhb_rows([raw], "2026-11-26"), {})
        self.assertEqual(worker.lhb_rows([{**raw, "上榜原因": "连续三个交易日累计涨幅"}], "2026-11-25"), {})
        self.assertEqual(worker.lhb_rows([{**raw, "代码": "688001"}], "2026-11-25"), {})


if __name__ == "__main__":
    unittest.main()
