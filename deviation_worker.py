"""Isolated AKShare inputs for post-close deviation calculations; JSON stdin/out.

Every stock source is explicitly qfq. Missing sessions are never forward-filled;
the caller owns settlement, process deadlines, request throttling and archives.
"""
from __future__ import annotations

import contextlib
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from review_worker import iso_day, main_board, number, text


BENCHMARKS = (
    ("sh", (("000002", "上证A指"), ("000001", "上证指数"))),
    ("sz", (("399107", "深证A指"), ("399001", "深证成指"))),
)


def requested_dates(value):
    if not isinstance(value, list) or not 31 <= len(value) <= 80:
        raise ValueError("31..80 exchange sessions required")
    dates = [date.fromisoformat(item).isoformat() for item in value]
    if dates != sorted(set(dates)):
        raise ValueError("sessions must be unique and ascending")
    return dates


def close_rows(records, dates):
    """Normalize Chinese/English provider fields and retain exact requested days."""
    expected, rows, seen = set(dates), {}, set()
    for record in records:
        day = iso_day(record.get("date", record.get("日期")))
        if day not in expected:
            continue
        if day in seen:
            raise ValueError("duplicate daily closing price")
        seen.add(day)
        close = number(record.get("close", record.get("收盘")))
        if close is not None and close > 0:
            rows[day] = {"date": day, "close": close}
    return [rows[day] for day in sorted(rows)]


def rate_limited(exc):
    return "RateLimit" in type(exc).__name__


def timeout(seconds):
    try:
        from akshare.utils.context import set_timeout
        set_timeout(seconds)
    except ImportError:
        pass  # Older releases are still bounded by the parent's kill deadline.


def benchmark(ak, market, alternatives, dates, *, skipped=None):
    limited = {"eastmoney": False, "sina": False}
    best = {"market": market, "code": alternatives[0][0], "name": alternatives[0][1],
            "fallback": False, "status": "unavailable", "source": None, "rows": [],
            "rate_limited": False, "limited_sources": limited}
    for index, (code, name) in enumerate(alternatives):
        symbol = market + code
        # Sources may differ; the benchmark identity must never silently differ.
        for source, fetch in (
            ("akshare.stock_zh_index_daily_em", lambda: ak.stock_zh_index_daily_em(
                symbol=symbol, start_date=dates[0].replace("-", ""), end_date=dates[-1].replace("-", ""))),
            ("akshare.stock_zh_index_daily", lambda: ak.stock_zh_index_daily(symbol=symbol)),
        ):
            provider = "eastmoney" if source.endswith("_em") else "sina"
            if limited[provider] or (skipped or {}).get(provider):
                continue
            try:
                rows = close_rows(fetch().to_dict("records"), dates)
                result = {"market": market, "code": code, "name": name, "fallback": bool(index),
                          "status": "ready" if len(rows) == len(dates) else "unavailable",
                          "source": source, "rows": rows, "rate_limited": any(limited.values()), "limited_sources": limited}
                if result["status"] == "ready":
                    return result
                if len(rows) > len(best["rows"]):
                    best = result
            except Exception as exc:
                print(f"deviation benchmark {symbol}: {type(exc).__name__}", file=sys.stderr)
                limited[provider] |= rate_limited(exc)
                best["rate_limited"] = any(limited.values())
    return best


def benchmarks(ak, dates, *, skipped=None):
    # Only two index downloads can run together. Each subprocess has a kill bound.
    timeout(5)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda row: benchmark(ak, row[0], row[1], dates, skipped=skipped), BENCHMARKS))
    return {"benchmarks": results, "rate_limited": any(row["rate_limited"] for row in results),
            "limited_sources": {provider: any(row["limited_sources"][provider] for row in results)
                                for provider in ("eastmoney", "sina")}}


def stock(ak, body):
    code, name = body.get("code"), text(body.get("name"))
    if name is None or "退" in name or not main_board(code, name):
        raise ValueError("named non-ST main-board stock required")
    dates = requested_dates(body["dates"])
    start = date.fromisoformat(body["start_date"]).isoformat()
    end = date.fromisoformat(body["end_date"]).isoformat()
    if not start <= dates[0] <= dates[-1] <= end:
        raise ValueError("stock range must cover the requested sessions")
    result = {"rows": [], "status": "unavailable", "adjust": "qfq", "source": None,
              "rate_limited": False, "limited_sources": {"eastmoney": False, "sina": False}}
    sources = (
        ("akshare.stock_zh_a_hist", lambda: ak.stock_zh_a_hist(
            symbol=code, period="daily", start_date=start.replace("-", ""),
            end_date=end.replace("-", ""), adjust="qfq")),
        ("akshare.stock_zh_a_daily", lambda: ak.stock_zh_a_daily(
            symbol=("sh" if code.startswith("60") else "sz") + code,
            start_date=start.replace("-", ""), end_date=end.replace("-", ""), adjust="qfq")),
    )
    for source, fetch in sources:
        provider = "eastmoney" if source == "akshare.stock_zh_a_hist" else "sina"
        if body.get("skip_" + provider) is True:
            continue
        # Sina's qfq calculation may request prices, float shares and factors;
        # smaller per-request bounds leave room inside the parent's 35 seconds.
        if provider == "sina":
            timeout(4)
        try:
            rows = close_rows(fetch().to_dict("records"), dates)
            if len(rows) > len(result["rows"]):
                result.update(rows=rows, status="ready" if len(rows) == len(dates) else "partial", source=source)
            if result["status"] == "ready":
                break
        except Exception as exc:
            print(f"deviation stock {code}: {type(exc).__name__}", file=sys.stderr)
            if rate_limited(exc):
                result["rate_limited"] = True
                result["limited_sources"][provider] = True
    return result


def candidates(ak, day):
    # Reuse the prior worker's raw-response distinction: AKShare otherwise maps
    # data:null and an authoritative empty pool to the same empty DataFrame.
    try:
        from akshare.utils import http
    except ImportError:
        http = None
    original = http.get if http else None
    observed = []

    def tracked_get(*args, **kwargs):
        kwargs.setdefault("timeout", 8)
        response = original(*args, **kwargs)
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        valid = isinstance(data, dict) and isinstance(data.get("pool"), list)
        if valid:
            echoed = str(data.get("date", ""))
            if re.fullmatch(r"[0-9]{8}", echoed) and echoed != day.replace("-", ""):
                valid = False
        observed.append(valid)
        return response

    if http:
        http.get = tracked_get
    try:
        frame = ak.stock_zt_pool_strong_em(date=day.replace("-", ""))
        records = frame.to_dict("records")
        if records and not {"代码", "名称"} <= set(frame.columns):
            raise ValueError("invalid strong-pool schema")
        items = []
        for row in records:
            code, name = row.get("代码"), text(row.get("名称"))
            if name is not None and "退" not in name and main_board(code, name):
                items.append({"code": code, "name": name})
        if len({row["code"] for row in items}) != len(items):
            raise ValueError("duplicate candidate code")
        valid = all(observed) if observed else bool(records)
        return {"items": items if valid else [], "status": "ready" if valid else "unavailable",
                "date": day, "source": "akshare.stock_zt_pool_strong_em", "rate_limited": False}
    except Exception as exc:
        print(f"deviation candidates: {type(exc).__name__}", file=sys.stderr)
        return {"items": [], "status": "unavailable", "date": day,
                "source": "akshare.stock_zt_pool_strong_em", "rate_limited": rate_limited(exc)}
    finally:
        if http:
            http.get = original


def run(body):
    if not isinstance(body, dict) or body.get("operation") not in {"benchmarks", "stock", "candidates"}:
        raise ValueError("valid deviation operation required")
    import akshare as ak
    timeout(8)
    operation = body["operation"]
    if operation == "benchmarks":
        return benchmarks(ak, requested_dates(body["dates"]),
                          skipped={provider: body.get("skip_" + provider) is True for provider in ("eastmoney", "sina")})
    if operation == "stock":
        return stock(ak, body)
    return candidates(ak, date.fromisoformat(body["date"]).isoformat())


if __name__ == "__main__":
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        body = json.load(sys.stdin)
        with contextlib.redirect_stdout(sys.stderr):
            result = run(body)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__}))
        sys.exit(1)
