"""After-close main-board deviation scenarios; no intraday calculations or alerts."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from copy import deepcopy
from datetime import date, datetime, time as daytime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from config import SHANGHAI
from pre_market import save_json
from review import finite, locked_date, main_board, mock_sessions

log = logging.getLogger(__name__)
MAX_CANDIDATES = 150
NUMERIC_FIELDS = ("dev_10", "dev_30", "remain_10", "remain_30", "effective_remain",
                  "projected_dev_10_if_limit_up", "tomorrow_gap")
BENCHMARKS = [{"market": "sh", "code": "000002", "name": "上证 A 指", "fallback": False, "status": "pending"},
              {"market": "sz", "code": "399107", "name": "深证 A 指", "fallback": False, "status": "pending"}]
LIMITATIONS = ["情景测算假设明日指数涨跌为0%、个股上涨10%，未作连续涨停板数承诺。",
               "采用前复权日K的未重置滑动窗口；未接入公告/复牌后指标重置、无涨跌幅限制日排除与其他严重异动条件，结果不等同交易所认定。",
               "候选初筛有数量上限，停牌/缺失日K不填充；指数降级会改变测算口径。"]


def decimal_price(value):
    if finite(value) is None:
        return None
    try:
        result = Decimal(str(value))
        return result if result > 0 else None
    except InvalidOperation:
        return None


def price_map(rows):
    result = {}
    for row in rows:
        day = date.fromisoformat(row["date"]).isoformat()
        if day in result:
            raise ValueError("duplicate daily close")
        result[day] = decimal_price(row.get("close"))
    return result


def scalar(value):
    # Remove arithmetic noise at exact tier boundaries, retain raw sorting precision.
    number = finite(value)
    return round(number, 10) if number is not None else None


def risk_tier(value):
    value = finite(value)
    if value is None:
        return None
    return "A" if value < 16 else "B" if value < 32 else "C" if value <= 55 else "over_55"


def classify_item(row):
    item = {**row, **{key: finite(row.get(key)) for key in NUMERIC_FIELDS}}
    short, long = item["remain_10"], item["remain_30"]
    # With an unknown window we cannot claim the known window is the tightest.
    effective = min(short, long) if short is not None and long is not None else None
    item.update(effective_remain=effective, risk_tier=risk_tier(effective),
                near_line_30=short is not None and long is not None and long < short,
                over_60=item["dev_10"] is not None and item["dev_10"] >= 60,
                already_triggered=any(value is not None and value >= threshold
                                      for value, threshold in ((item["dev_10"], 100), (item["dev_30"], 200)))
                                  or any(value is not None and value <= 0 for value in (short, long)),
                tomorrow_cross=effective <= 10 if effective is not None else None,
                tomorrow_gap=round(effective - 10, 2) if effective is not None and effective > 10 else None)
    return item


def calculate_metrics(stock_rows, index_rows, sessions):
    """Endpoint deviations and tightest T+1 bases; all returns in percentage points.

    remain_N = min_b [(1 + threshold + I_b) / (P_T/P_b) - 1] * 100,
    b=T-(N-1)..T. A 10-return endpoint needs 11 closes, not 10.
    """
    dates = [d.isoformat() if isinstance(d, date) else date.fromisoformat(d).isoformat() for d in sessions]
    if dates != sorted(set(dates)):
        raise ValueError("sessions must be unique and ascending")
    stock, index = price_map(stock_rows), price_map(index_rows)
    result = {key: None for key in NUMERIC_FIELDS}
    for n, threshold in ((10, Decimal(1)), (30, Decimal(2))):
        endpoint = dates[-n - 1:]
        if len(endpoint) == n + 1 and all(stock.get(d) and index.get(d) for d in endpoint):
            first, last = endpoint[0], endpoint[-1]
            result[f"dev_{n}"] = scalar((stock[last] / stock[first] - index[last] / index[first]) * 100)
        bases = dates[-n:]
        if len(bases) == n and all(stock.get(d) and index.get(d) for d in bases):
            last = bases[-1]
            required, forecasts = [], []
            for base in bases:
                stock_ratio = stock[last] / stock[base]
                index_return = index[last] / index[base] - 1
                required.append(((1 + threshold + index_return) / stock_ratio - 1, base))
                forecasts.append((stock_ratio * Decimal("1.10") - 1 - index_return) * 100)
            value, base = min(required)
            result[f"remain_{n}"] = scalar(value * 100)
            result[f"tightest_base_{n}"] = base
            if n == 10:
                result["projected_dev_10_if_limit_up"] = scalar(max(forecasts))
    return classify_item(result)


def window_range(sessions, n):
    if len(sessions) < n + 1:
        return None
    return f"{sessions[-n - 1][5:].replace('-', '.')}–{sessions[-1][5:].replace('-', '.')}"


def partitions(rows):
    tiers = {key: [r for r in rows if r["risk_tier"] == key] for key in ("A", "B", "C")}
    over = [r for r in rows if r["risk_tier"] == "over_55"]
    counts = {**{key: len(items) for key, items in tiers.items()}, "over_55": len(over),
              "unknown": sum(r["risk_tier"] is None for r in rows)}
    return {**tiers, "over_55_items": over, "over_55_count": len(over)}, counts


def build_report(day, rows, benchmarks, sessions, *, updated_at=None, locked_until_text="", coverage=None):
    dates = [d.isoformat() if isinstance(d, date) else d for d in sessions]
    rows = [classify_item(row) for row in rows if main_board(row)]
    monitored = [r for r in rows if r.get("data_status") != "ready"
                 or r["dev_10"] is not None and r["dev_10"] >= 30
                 or r["dev_30"] is not None and r["dev_30"] >= 70
                 or r["effective_remain"] is not None and r["effective_remain"] <= 55]
    monitored.sort(key=lambda r: (r["effective_remain"] is None,
                                  r["effective_remain"] if r["effective_remain"] is not None else 0, r["code"]))
    failed = sum(r.get("data_status") != "ready" for r in rows)
    ladder, counts = partitions(monitored)
    summary = {"date": day, "weekday": "星期" + "一二三四五六日"[date.fromisoformat(day).weekday()],
               "total_candidates": len(monitored), "candidate_count": len(rows), "failed_count": failed,
               "over_60_count": sum(r["over_60"] for r in monitored),
               "window_10_range": window_range(dates, 10), "window_30_range": window_range(dates, 30),
               "tier_counts": counts,
               "updated_at": updated_at, "locked_until_text": locked_until_text,
               "benchmarks": [{k: v for k, v in b.items() if k != "rows"} for b in benchmarks],
               "coverage": coverage or {}, "calculation_note": "前复权收盘价复利差值；明日遍历全部合法基准日，取最小剩余涨幅。",
               "limitations": LIMITATIONS}
    coverage = coverage or {}
    source_partial = (coverage.get("review_days_ready", 0) < coverage.get("review_days_requested", 0)
                      or coverage.get("strong_pool_status", "ready") != "ready")
    partial = failed > 0 or any(b.get("status") != "ready" for b in benchmarks) or not rows or source_partial
    return {"date": day, "status": "partial" if partial else "ready", "error": None, "summary": summary,
            "items": monitored, "risk_ladder": ladder,
            "near_trigger_top": [r for r in monitored if r["effective_remain"] is not None and r["effective_remain"] < 20]}


def select_candidates(archives, dates, strong, limit=MAX_CANDIDATES, *, identities=None):
    """Bound expensive qfq downloads; reserve room for the independent strong pool."""
    rows, counts, recency = {}, {}, {}
    for i, day in enumerate(dates):
        archive = archives.get(day, {})
        for row in archive.get("success", []) + archive.get("broken", []):
            if not isinstance(row, dict):
                continue
            if main_board(row):
                rows[row["code"]] = {"code": row["code"], "name": row["name"]}
                counts[row["code"]] = counts.get(row["code"], 0) + 1
                recency[row["code"]] = i
            elif row.get("code") in rows and isinstance(row.get("name"), str) and row["name"].strip():
                rows.pop(row["code"])
    strong_codes = []
    for row in strong:
        if main_board(row):
            rows[row["code"]] = {"code": row["code"], "name": row["name"]}
            strong_codes.append(row["code"])
        elif isinstance(row, dict) and row.get("code") in rows and isinstance(row.get("name"), str) and row["name"].strip():
            rows.pop(row["code"])
    for code, identity in (identities or {}).items():
        if code in rows and isinstance(identity, dict) and identity.get("name"):
            if main_board({"code": code, "name": identity["name"]}):
                rows[code]["name"] = identity["name"]
            else:
                rows.pop(code)
    review_codes = sorted((c for c in rows if c in counts), key=lambda c: (-counts[c], -recency[c], c))
    strong_codes = list(dict.fromkeys(c for c in strong_codes if c in rows))
    ordered = list(dict.fromkeys(strong_codes[:50] + review_codes + strong_codes[50:]))
    return [rows[c] for c in ordered[:limit]], {"pool_union_count": len(rows), "selected_count": min(len(ordered), limit),
                                               "truncated_count": max(0, len(ordered) - limit), "limit": limit}


def mock_report(day, sessions):
    dates = [d for d in sessions if d.isoformat() <= day][-31:]
    benchmark_rows = [{"date": d.isoformat(), "close": 100} for d in dates]
    benchmarks = [{**b, "status": "ready", "source": "mock", "rows": benchmark_rows} for b in BENCHMARKS]
    pairs = [(5, 80), (12, 90), (-3, 80), (15.99, 80), (30, 18), (40, 25), (20, 90), (31.99, 100),
             (32, 100), (40, 120), (55, 140), (45, 200), (70, 60), (90, 65), (120, 60), (100, 70)]
    items = []
    for i, (short, long) in enumerate(pairs):
        code = ("600" if i % 2 else "002") + f"{i + 1:03d}"
        prices = [{"date": d.isoformat(), "close": (100 * (1 + long / 100) / 3 if j < 20
                                                    else 100 * (1 + short / 100) / 2) if j < 30 else 100}
                  for j, d in enumerate(dates)]
        items.append({"code": code, "name": f"红线演练{i + 1:02d}",
                      **calculate_metrics(prices, benchmark_rows, dates), "data_status": "ready",
                      "benchmark": {k: v for k, v in benchmarks[0 if code.startswith("60") else 1].items() if k != "rows"}})
    result = build_report(day, items, benchmarks, dates, updated_at=day + "T15:08:00+08:00",
                          coverage={"pool_union_count": len(items), "selected_count": len(items), "truncated_count": 0})
    return {"version": 1, "simulated": True, **result}


class DeviationService:
    def __init__(self, monitor, path=None):
        self.monitor, self.cfg = monitor, monitor.cfg
        base = Path(path) if path is not None else Path(__file__).resolve().parent / "deviation_cache"
        self.path = base / "mock" if self.cfg.mock_mode else base
        self.archives, self.errors = {}, {}
        self.lock = asyncio.Lock()
        self.processes = asyncio.Semaphore(2)
        self.queue = asyncio.Queue(maxsize=1)
        self.pending = False
        self.current_date = None
        self.last_manual = None
        self.retry_at = {}
        self.stock_retry_at = 0.0
        self.loaded = set()

    def sessions(self, now):
        now = now.astimezone(SHANGHAI)
        if self.cfg.mock_mode:
            return mock_sessions(now.date())
        calendar = self.monitor.calendar
        return calendar.sessions if calendar.covered(now.date()) else []

    def dates(self, now):
        sessions = self.sessions(now)
        target = locked_date(now, sessions)
        return [d for d in sessions if target and d <= target][-7:]

    def intraday(self, now):
        now = now.astimezone(SHANGHAI)
        return now.date() in self.sessions(now) and daytime(9, 15) <= now.time() < daytime(15)

    def due(self, day, now):
        now = now.astimezone(SHANGHAI)
        return day < now.date().isoformat() or day == now.date().isoformat() and now.time() >= daytime(15, 8)

    def meta(self, now=None):
        now = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
        sessions, dates = self.sessions(now), self.dates(now)
        next_days = [d for d in sessions if d > now.date() or d == now.date() and now.time() < daytime(15)]
        settle_days = [d for d in sessions if d > now.date() or d == now.date() and now.time() < daytime(15, 8)]
        next_switch = datetime.combine(next_days[0], daytime(15), SHANGHAI).isoformat() if next_days else None
        next_refresh = datetime.combine(settle_days[0], daytime(15, 8), SHANGHAI).isoformat() if settle_days else None
        day = dates[-1].isoformat() if dates else None
        archive = self.archives.get(day, {})
        return {"dates": [d.isoformat() for d in reversed(dates)], "default_date": day,
                "next_refresh": next_refresh, "next_refresh_text": f"交易日15:08结算；下次计划 {next_refresh or '--'}",
                "locked_until_text": f"盘后结算 · 保持至下一交易日15:00（{next_switch or '--'}）",
                "benchmarks": archive.get("summary", {}).get("benchmarks", BENCHMARKS),
                "simulated": self.cfg.mock_mode, "status": "ready" if dates else "calendar_unavailable",
                "refresh_locked": self.intraday(now), "processing_date": self.current_date,
                "refresh_allowed": self.cfg.mock_mode or bool(day and not self.intraday(now) and self.due(day, now) and not self.lock.locked()),
                "days": [{"date": d.isoformat(), "cached": d.isoformat() in self.archives,
                          "status": self.archives.get(d.isoformat(), {}).get("status", "pending"),
                          "error": self.errors.get(d.isoformat())} for d in reversed(dates)]}

    def report(self, selected=None, now=None):
        now = now or datetime.now(SHANGHAI)
        meta = self.meta(now)
        day = selected or meta["default_date"]
        archive = self.archives.get(day)
        if archive:
            # Computed values remain frozen; only the calendar-derived lock hint changes.
            return {**archive, "summary": {**archive["summary"], "locked_until_text": meta["locked_until_text"]}}
        return {"date": day, "simulated": self.cfg.mock_mode,
                "status": "scheduled" if day and not self.due(day, now) else "pending",
                "error": self.errors.get(day) or "等待后台盘后结算，缺失数据不会视作零。",
                "summary": {"date": day, "total_candidates": None, "over_60_count": None,
                            "benchmarks": meta["benchmarks"], "locked_until_text": meta["locked_until_text"],
                            "limitations": LIMITATIONS}, "items": [],
                "risk_ladder": {"A": [], "B": [], "C": [], "over_55_items": [], "over_55_count": None}, "near_trigger_top": []}

    def _read(self, day):
        try:
            value = json.loads((self.path / f"{day}.json").read_text(encoding="utf-8"))
            if (value.get("version") != 1 or value.get("date") != day or value.get("simulated") is not self.cfg.mock_mode
                    or value.get("status") not in {"ready", "partial"} or value["summary"]["date"] != day
                    or not isinstance(value.get("items"), list)):
                raise ValueError("invalid deviation cache")
            if any(not main_board(row) for row in value["items"]):
                raise ValueError("invalid candidate in deviation cache")
            clean = [classify_item(r) for r in value["items"]]
            # Reject non-finite/missing data masquerading as a fully calculated row.
            if any(r.get("data_status") == "ready" and any(r[k] is None for k in ("dev_10", "dev_30", "remain_10", "remain_30")) for r in clean):
                raise ValueError("incomplete cached metrics")
            value["items"] = clean
            value["risk_ladder"], counts = partitions(clean)
            value["near_trigger_top"] = [r for r in clean if r["effective_remain"] is not None and r["effective_remain"] < 20]
            value["summary"].update(total_candidates=len(clean), over_60_count=sum(r["over_60"] for r in clean), tier_counts=counts)
            return value
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return None

    async def persist(self, archive):
        day = date.fromisoformat(archive["date"]).isoformat()
        def write():
            self.path.mkdir(parents=True, exist_ok=True)
            save_json(self.path / f"{day}.json", archive)
        await asyncio.to_thread(write)
        self.archives[day] = archive

    async def worker(self, operation, **args):
        async with self.processes:
            if operation == "stock" and time.monotonic() < self.stock_retry_at:
                return {"rows": [], "status": "unavailable", "error": "rate_limit_cooldown"}
            process = None
            env = {k: v for k, v in os.environ.items() if k.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}}
            if self.cfg.http_proxy:
                env.update(HTTP_PROXY=self.cfg.http_proxy, HTTPS_PROXY=self.cfg.http_proxy)
            try:
                process = await asyncio.create_subprocess_exec(
                    sys.executable, str(Path(__file__).with_name("deviation_worker.py")),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, env=env)
                stdout, _ = await asyncio.wait_for(process.communicate(json.dumps({"operation": operation, **args}).encode()), 35)
                if process.returncode:
                    raise ValueError("deviation data worker failed")
                data = json.loads(stdout)
                if data.get("rate_limited"):
                    self.stock_retry_at = time.monotonic() + 1200
                return data
            finally:
                if process is not None and process.returncode is None:
                    process.kill()
                    await process.wait()

    async def history(self, days):
        result = {}
        for day in days:
            archive = self.monitor.review.archives.get(day)
            if archive is None:
                archive = await asyncio.to_thread(self.monitor.review._read, day)
            if archive and archive.get("complete"):
                result[day] = archive
        return result

    async def settle(self, day, now=None):
        now = now or datetime.now(SHANGHAI)
        async with self.lock:
            if not self.due(day, now):
                raise ValueError("today settles at 15:08")
            self.current_date = day
            try:
                sessions = [d for d in self.sessions(now) if d.isoformat() <= day]
                window = sessions[-31:]
                if len(window) < 31:
                    raise ValueError("31 exchange sessions required")
                days = [d.isoformat() for d in sessions[-15:]]
                history = await self.history(days)
                downloads = await asyncio.gather(self.worker("benchmarks", dates=[d.isoformat() for d in window]),
                                                 self.worker("candidates", date=day), return_exceptions=True)
                benchmark_data, pool_data = downloads
                benchmarks = benchmark_data.get("benchmarks", []) if isinstance(benchmark_data, dict) else []
                by_market = {b["market"]: b for b in benchmarks}
                for base in BENCHMARKS:
                    if base["market"] not in by_market:
                        by_market[base["market"]] = {**base, "status": "unavailable", "rows": []}
                benchmarks = list(by_market.values())
                strong = pool_data.get("items", []) if isinstance(pool_data, dict) else []
                identities = {}
                if day == (self.meta(now)["default_date"]):
                    universe = getattr(self.monitor.fetcher, "universe", None)
                    identities = getattr(universe, "stocks", {})
                candidates, coverage = select_candidates(history, days, strong, identities=identities)
                coverage.update(review_days_ready=len(history), review_days_requested=len(days),
                                strong_pool_status=pool_data.get("status", "unavailable") if isinstance(pool_data, dict) else "unavailable")
                if not candidates:
                    raise ValueError("候选初筛暂无有效数据，保留最近归档")
                async def stock(candidate):
                    benchmark = by_market["sh" if candidate["code"].startswith("60") else "sz"]
                    try:
                        if benchmark.get("status") != "ready":
                            raise ValueError("benchmark unavailable")
                        data = await self.worker("stock", **candidate, start_date=window[0].isoformat(),
                                                 end_date=day, dates=[d.isoformat() for d in window])
                        if data.get("adjust") != "qfq":
                            raise ValueError("verified qfq daily bars required")
                        metrics = calculate_metrics(data.get("rows", []), benchmark.get("rows", []), window)
                        ready = all(metrics[k] is not None for k in ("dev_10", "dev_30", "remain_10", "remain_30"))
                        return {**candidate, **metrics, "data_status": "ready" if ready else "partial",
                                "benchmark": {k: v for k, v in benchmark.items() if k != "rows"}}
                    except (ValueError, TimeoutError, OSError, KeyError, TypeError):
                        return {**candidate, **classify_item({}), "data_status": "unavailable",
                                "benchmark": {k: v for k, v in benchmark.items() if k != "rows"}}
                rows = await asyncio.gather(*(stock(candidate) for candidate in candidates))
                old = self.archives.get(day)
                old_items = {r["code"]: r for r in old.get("items", [])} if old else {}
                preserved = 0
                for i, row in enumerate(rows):
                    previous = old_items.get(row["code"])
                    if row["data_status"] != "ready" and previous and previous.get("data_status") == "ready":
                        if previous.get("benchmark", {}).get("code") != row.get("benchmark", {}).get("code"):
                            raise ValueError("基准口径改变且补算不完整，保留原归档以避免混用指数")
                        rows[i] = {**previous, "name": row["name"], "retained_from_cache": True}
                        preserved += 1
                coverage["retained_from_cache"] = preserved
                report = build_report(day, rows, benchmarks, window, updated_at=datetime.now(SHANGHAI).isoformat(),
                                      locked_until_text=self.meta(now)["locked_until_text"], coverage=coverage)
                report.update(version=1, simulated=False)
                completed = datetime.now(SHANGHAI)
                if day in self.archives and self.intraday(completed):
                    raise ValueError("盘中锁定，不覆盖已结算归档")
                if old and old["status"] == "ready" and report["status"] == "partial":
                    raise ValueError("辅助数据失败，保留最近完整结算")
                await self.persist(report)
                self.errors.pop(day, None)
                return report
            finally:
                self.current_date = None

    def queue_refresh(self, day, now=None):
        now = now or datetime.now(SHANGHAI)
        if day not in {d.isoformat() for d in self.dates(now)}:
            raise ValueError("date must be one of the latest 7 completed sessions")
        if not self.cfg.mock_mode and (self.intraday(now) or not self.due(day, now)):
            raise PermissionError("盘中保持收盘结算；当日请于15:08后刷新")
        if self.pending or self.lock.locked() or self.last_manual is not None and time.monotonic() - self.last_manual < 30:
            return False
        self.queue.put_nowait(day)
        self.pending, self.last_manual = True, time.monotonic()
        return True

    async def prune(self, days):
        keep = {d.isoformat() for d in days}
        def clean():
            if not self.path.exists():
                return
            parent = self.path.resolve()
            for file in self.path.glob("????-??-??.json"):
                if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\.json", file.name) and file.stem not in keep and file.resolve().parent == parent:
                    file.unlink()
        await asyncio.to_thread(clean)
        self.archives = {day: value for day, value in self.archives.items() if day in keep}

    async def run(self):
        while True:
            current = None
            try:
                now = datetime.now(SHANGHAI)
                days = self.dates(now)
                for day in days:
                    key = day.isoformat()
                    if key not in self.loaded:
                        cached = await asyncio.to_thread(self._read, key)
                        if cached:
                            self.archives[key] = cached
                        self.loaded.add(key)
                if self.cfg.mock_mode:
                    for day in days[-2:]:
                        key = day.isoformat()
                        if key not in self.archives:
                            await self.persist(mock_report(key, self.sessions(now)))
                    if not self.queue.empty():
                        current = self.queue.get_nowait()
                        await self.persist(mock_report(current, self.sessions(now)))
                        self.pending = False
                elif not self.queue.empty():
                    current = self.queue.get_nowait()
                    try:
                        await self.settle(current, now)
                    finally:
                        self.pending = False
                else:
                    # Missing historical results can cold-fill; cached intraday results never recalculate.
                    for day in reversed(days[-2:]):
                        key = day.isoformat()
                        old = self.archives.get(key)
                        if (self.due(key, now) and time.monotonic() >= self.retry_at.get(key, 0)
                                and (old is None or old["status"] == "partial" and not self.intraday(now))):
                            current = key
                            self.retry_at[key] = time.monotonic() + 1200
                            await self.settle(key, now)
                            break
                if days:
                    await self.prune(days)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if current:
                    self.errors[current] = str(exc) if isinstance(exc, ValueError) else "后台数据暂不可用，保留已有结算"
                log.warning("红线情景结算局部失败，保留已有归档", exc_info=True)
            await asyncio.sleep(5)
