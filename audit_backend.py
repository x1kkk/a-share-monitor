"""Read-only backend audit; external notifications are disabled for this run."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from config import HEADERS, SHANGHAI, Settings
from engine import Engine, self_check
from fetcher import Book, Fetcher, demo_seed, eligible, parse_books
from main import Monitor
from pre_market import MarketCalendar


async def audit() -> None:
    cfg = replace(Settings(), wechat_webhook_url="", mock_wechat=False, api_token="")
    result = {"audit_time": datetime.now(SHANGHAI).isoformat(), "sources": {}}
    for name in ("config.py", "fetcher.py", "engine.py", "main.py", "pre_market.py", "notifier.py"):
        source = Path(name).read_bytes()
        compile(source, name, "exec")
        result["sources"][name] = hashlib.sha256(source).hexdigest()
    self_check()
    result["builtin_self_check"] = "PASS"
    calendar = MarketCalendar()

    async with httpx.AsyncClient(headers=HEADERS, proxy=cfg.http_proxy, trust_env=False,
                                 timeout=cfg.http_timeout,
                                 limits=httpx.Limits(max_connections=cfg.max_concurrency)) as client:
        fetcher, engine = Fetcher(client, cfg), Engine(cfg)

        async def market_ticks():
            ticks = []
            for tick in range(1, 4):
                started = time.perf_counter()
                row = {"tick": tick}
                try:
                    raw = await fetcher.fetch_market(bootstrap=tick == 1)
                    row.update(provider_total=fetcher.last_total, parsed_count=len(raw),
                               fetch_ms=round((time.perf_counter() - started) * 1000, 2))
                    now = datetime.now(SHANGHAI)
                    quotes = eligible(raw, calendar, now.date(), cfg)
                    row["eligible_count"] = len(quotes)
                    if quotes:
                        books = await fetcher.fetch_books({q.code for q in quotes if q.price >= q.pre_close * 1.099}, now)
                        snapshot = engine.update(quotes, books, datetime.now(SHANGHAI))
                        row.update(anomalies=snapshot["anomalies"][:1],
                                   sealed=[q for q in snapshot["leaders"] if q["is_limit_up"]][:1],
                                   events=snapshot["events"])
                    else:
                        row["calculation"] = "skipped: calendar not ready/no eligible quotes"
                except (httpx.HTTPError, TimeoutError, ValueError) as exc:
                    row.update(status="failed", error=type(exc).__name__,
                               fetch_ms=round((time.perf_counter() - started) * 1000, 2),
                               provider_total=None, parsed_count=None)
                ticks.append(row)
                print(json.dumps({"live_tick": row}, ensure_ascii=False), flush=True)
                if tick < 3:
                    await asyncio.sleep(cfg.poll_seconds)
            return ticks

        async def tencent_probe():
            started = time.perf_counter()
            try:
                response = await client.get(cfg.tencent_url + "sh600000,sz000001", timeout=3)
                response.raise_for_status()
                books = parse_books(response.content.decode("gb18030", errors="replace"))
                return {"http_status": response.status_code, "parse_count": len(books),
                        "books": {code: asdict(book) for code, book in books.items()},
                        "ms": round((time.perf_counter() - started) * 1000, 2)}
            except (httpx.HTTPError, TimeoutError, ValueError) as exc:
                return {"error": type(exc).__name__, "ms": round((time.perf_counter() - started) * 1000, 2)}

        outputs = await asyncio.gather(market_ticks(), calendar.load(cfg, datetime.now(SHANGHAI).date()),
                                       tencent_probe())
        result["live_ticks"], result["tencent_probe"] = outputs[0], outputs[2]
    result["calendar"] = {"error": calendar.error, "sessions": len(calendar.sessions),
                          "streaks_ready": calendar.streaks_ready}

    calls, mode = [], "ok"

    async def handler(request):
        params = request.url.params
        assert params["fltt"] == "2" and params["invt"] == "2" and params["pz"] == "100"
        page = int(params["pn"])
        calls.append(page)
        start = (page - 1) * 100
        rows = [{"f12": f"{i:06d}", "f14": "分页测试", "f2": 10, "f3": 0,
                 "f5": 100, "f6": 10000, "f8": 1, "f18": 10, "f21": 1e9,
                 "f26": 20100101, "f62": 0, "f66": 0, "f72": 0}
                for i in range(start, start + 100)]
        if mode == "duplicate" and page == 2:
            rows[0]["f12"] = "000000"
        if mode == "partial" and page == 31:
            rows.pop()
        return httpx.Response(200, json={"data": {"total": 3100, "diff": rows}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = Fetcher(client, cfg)
        quotes = await fetcher.fetch_market(bootstrap=True)
        assert len(quotes) == 3100 and set(calls) == set(range(1, 32))
        result["pagination_fixture"] = {"count": len(quotes), "pages": len(calls), "status": "PASS"}
        for mode in ("duplicate", "partial"):
            try:
                await fetcher.fetch_market(bootstrap=True)
            except ValueError as exc:
                result["pagination_fixture"][mode] = str(exc)
            else:
                raise AssertionError(mode + " market accepted")

    now = datetime(2026, 11, 4, 9, 30, tzinfo=SHANGHAI)
    seeds = demo_seed()[:2]
    monitor = Monitor(None, cfg)
    engine = monitor.engines["mock"]
    result["fixture_ticks"] = []
    for tick in range(3):
        started = time.perf_counter()
        tick_time = now + timedelta(seconds=tick * 2)
        quotes = [replace(q, source_ts=tick_time.timestamp()) for q in seeds]
        if tick:
            quotes[0] = replace(quotes[0], price=10.3, pct=3.0, main_inflow=1e7, high=10.3)
            quotes[1] = replace(quotes[1], price=12.1, pct=10.0, main_inflow=2e7, high=12.1)
        books = {q.code: Book(q.price, 2000, 0 if tick and q.code == "600036" else 500,
                              tick_time.timestamp()) for q in quotes}
        snapshot = engine.update(quotes, books, tick_time, clock=tick * 2, simulated=True)
        snapshot["events"] = monitor.notifier.publish(snapshot["events"], clock=tick * 2)
        snapshot.update(status="ok", mode="mock", source="audit_synthetic_fixture", session="morning",
                        cycle_ms=round((time.perf_counter() - started) * 1000, 2),
                        calendar_error=None, poll_seconds=cfg.poll_seconds)
        monitor.broadcast(snapshot)
        result["fixture_ticks"].append({"tick": tick + 1, "stock_count": snapshot["stock_count"],
                                        "anomalies": len(snapshot["anomalies"]),
                                        "sealed": sum(q["is_limit_up"] for q in snapshot["leaders"]),
                                        "events": len(snapshot["events"]), "cycle_ms": snapshot["cycle_ms"]})
        if tick == 1:
            result["ws_example"] = monitor.snapshot
    assert result["fixture_ticks"][1]["anomalies"] == 1
    assert result["fixture_ticks"][1]["sealed"] == 1
    assert result["fixture_ticks"][2]["events"] == 0
    assert engine.anomaly_board(122)[0]["status"] == "expired"
    result["ttl_420_exact_count"] = len(engine.anomaly_board(422))
    result["ttl_420_plus_count"] = len(engine.anomaly_board(422.001))
    result["fixture_status"] = "PASS"
    Path("backend_audit_results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(audit())
