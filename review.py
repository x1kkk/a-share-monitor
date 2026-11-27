"""Main-board review: isolated downloads, atomic daily archives, memory-only reads."""
from __future__ import annotations

import asyncio
import bisect
import json
import logging
import math
import os
import re
import sys
import time
from collections import Counter
from copy import deepcopy
from datetime import date, datetime, time as daytime, timedelta
from pathlib import Path

from config import SHANGHAI
from engine import percentiles
from pre_market import save_json

log = logging.getLogger(__name__)
FACTORS = ("seal_amount", "main_inflow", "inflow_ratio", "turnover_rate")
NUMBERS = ("price", "change_pct", "seal_amount", "main_inflow", "turnover_rate", "amount",
           "volume_ratio_5d", "volume_vs_prev", "lhb_net_buy", "break_count", "volume")


def finite(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def main_board(row):
    return bool(isinstance(row, dict) and re.fullmatch(r"(?:00|60)[0-9]{4}", str(row.get("code", "")))
                and isinstance(row.get("name"), str) and row["name"].strip()
                and "ST" not in row["name"].upper() and "退" not in row["name"])


def normalize(row, sealed=False):
    result = {**row, **{key: finite(row.get(key)) for key in NUMBERS}}
    result["industry"] = row.get("industry") or None
    concepts = row.get("concepts") if isinstance(row.get("concepts"), list) else []
    result["concepts"] = list(dict.fromkeys(x for x in concepts
                                           if isinstance(x, str) and x.strip()))
    streak = finite(row.get("streak_days"))
    result["streak_days"] = int(streak) if streak is not None and streak >= 0 and streak.is_integer() else None
    result["first_seal_time"] = row.get("first_seal_time") or None
    result["is_limit_up"] = sealed
    if not sealed:
        result["seal_amount"] = None
    flow, amount = result["main_inflow"], result["amount"]
    result["inflow_ratio"] = round(flow / amount * 100, 2) if flow is not None and amount is not None and amount > 0 else None
    return result


def score_rows(rows, reference=None):
    """Equal 25% weights, average tie ranks; available weights renormalize.

    Failed stocks use the sealed-stock distribution as reference (empirical
    midpoint rank for out-of-sample values). With no sealed stocks use failures.
    """
    reference = rows if reference is None else reference
    ranks = {factor: percentiles({r["code"]: r[factor] for r in reference
                                  if finite(r.get(factor)) is not None}) for factor in FACTORS}
    distributions = {factor: sorted(r[factor] for r in reference if finite(r.get(factor)) is not None)
                     for factor in FACTORS}
    for row in rows:
        available = []
        for factor in FACTORS:
            value = finite(row.get(factor))
            values = distributions[factor]
            if value is None or not values:
                continue
            rank = ranks[factor].get(row["code"])
            if rank is None:
                if len(values) == 1:
                    rank = 50.0 if value == values[0] else (100.0 if value > values[0] else 0.0)
                else:
                    lo, hi = bisect.bisect_left(values, value), bisect.bisect_right(values, value)
                    rank = max(0.0, min(100.0, (lo + hi - 1) / 2 / (len(values) - 1) * 100))
            available.append(rank)
        row["ladder_score"] = round(sum(available) / len(available), 2) if available else None


def locked_date(now, sessions):
    now = now.astimezone(SHANGHAI)
    eligible = [d for d in sessions if d < now.date() or
                d == now.date() and now.time() >= daytime(15)]
    return eligible[-1] if eligible else None


def status_label(previous, sealed, touched, breaks):
    if sealed:
        if previous is None:
            return "连板底表待补齐"
        label = "首板" if previous == 0 else f"{previous}进{previous + 1} 成功"
        return label + (" · 回封" if breaks is not None and breaks > 0 else "")
    if previous:
        return f"{previous}进{previous + 1} 失败 · " + ("炸板" if touched else "断板")
    return "首板 · 炸板" if touched else "15日断板"


def mock_sessions(today):
    # Explicit simulation calendar only. Production never substitutes weekdays.
    return [today + timedelta(days=i) for i in range(-100, 15)
            if (today + timedelta(days=i)).weekday() < 5]


def mock_archives(sessions, end):
    """Deterministic 30-session fixture, with every state and factor gradients."""
    days = [d for d in sessions if d <= end][-30:]
    if not days:
        return {}
    # The newest session contains boards 1..5, both failure types for 1..3.
    patterns = [{29}, {29}, {28, 29}, {28, 29}, {27, 28, 29}, {26, 27, 28, 29},
                {25, 26, 27, 28, 29}, {28}, {28}, {27, 28}, {27, 28},
                {26, 27, 28}, {26, 27, 28}, set(), {16, 19, 22, 24, 26}, {17, 23}]
    result = {}
    for i, day in enumerate(days):
        success, broken, observations = [], [], {}
        for n, hits in enumerate(patterns):
            code = ("600" if n % 2 else "002") + f"{n + 1:03d}"
            streak, cursor = 0, i
            while cursor in hits:
                streak, cursor = streak + 1, cursor - 1
            sealed = i in hits
            touched = sealed or i == 29 and n in {7, 9, 11, 13}
            row = normalize({"code": code, "name": f"演练样本{n + 1:02d}", "price": 11 + n,
                             "change_pct": 10 if sealed else 6 - n / 2,
                             "industry": ["电子", "机械", "银行"][n % 3],
                             "concepts": [["人工智能", "机器人"], ["新能源"], ["国企改革"]][n % 3],
                             "seal_amount": (18 - n) * 2e6 if sealed else None,
                             "main_inflow": (n - 4) * 3e6 if n != 1 else None,
                             "amount": (n + 1) * 5e7, "turnover_rate": n + 0.5,
                             "volume": 1e6 + n * 1e5 + i * 1000,
                             "volume_ratio_5d": 1 + n / 10, "volume_vs_prev": 0.8 + n / 10,
                             "break_count": 1 if i == 29 and n in {1, 3, 7, 9, 11, 13} else 0,
                             "first_seal_time": "09:32:00" if touched else None,
                             "streak_days": streak, "lhb_net_buy": (n + 1) * 1e6 if n % 3 == 0 else None,
                             "flow_source": "mock"}, sealed)
            observations[code] = row
            if sealed:
                success.append(row)
            elif touched:
                broken.append(row)
        result[day.isoformat()] = {"version": 1, "date": day.isoformat(), "simulated": True,
                                  "complete": True, "success": success, "broken": broken,
                                  "observations": observations, "lhb_status": "ready",
                                  "aux_status": {"stock": "ready", "concepts": "ready"},
                                  "updated_at": datetime.combine(day, daytime(15, 5), SHANGHAI).isoformat()}
    return result


class ReviewService:
    def __init__(self, monitor, path=None):
        self.monitor, self.cfg = monitor, monitor.cfg
        self.path = Path(path) if path else Path(__file__).resolve().parent / "review_cache"
        self.archives = {}
        self.live = None
        self.lock = asyncio.Lock()
        self.requests = asyncio.Queue(maxsize=1)
        self.refresh_pending = False
        self.last_manual = None
        self.retry_at = {}
        self.series = {}
        self.enriched = set()
        self.mock_day = None
        self.loaded_days = set()
        self.progress = {}
        self.stock_retry_at = 0.0
        self.series_day = None
        self.errors = {}
        self.stock_failures = set()

    def sessions(self, now):
        if self.cfg.mock_mode:
            return mock_sessions(now.date())
        calendar = self.monitor.calendar
        return calendar.sessions if calendar.covered(now.date()) else []

    def dates(self, now):
        sessions = self.sessions(now)
        target = locked_date(now, sessions)
        return [d for d in sessions if target and d <= target][-15:]

    def meta(self, now=None):
        now = now or datetime.now(SHANGHAI)
        sessions, dates = self.sessions(now), self.dates(now)
        upcoming = [d for d in sessions if d > now.date() or
                    d == now.date() and now.time() < daytime(15)]
        next_switch = datetime.combine(upcoming[0], daytime(15), SHANGHAI).isoformat() if upcoming else None
        available = self.cfg.mock_mode or (now.date() in sessions and daytime(9, 15) <= now.time() < daytime(15))
        return {"dates": [d.isoformat() for d in reversed(dates)],
                "default_date": dates[-1].isoformat() if dates else None,
                "next_switch": next_switch, "live_available": available, "simulated": self.cfg.mock_mode,
                "status": "ready" if dates else "calendar_unavailable",
                "days": [{"date": d.isoformat(), "cached": d.isoformat() in self.archives,
                          "status": "ready" if self.archives.get(d.isoformat(), {}).get("complete") else "pending",
                          "error": self.errors.get(d.isoformat()),
                          "lhb_status": self.archives.get(d.isoformat(), {}).get("lhb_status", "pending")}
                         for d in reversed(dates)]}

    def base_streaks(self, now, for_day=None):
        previous = [d for d in self.sessions(now) if d < (for_day or now.date())]
        archive = self.archives.get(previous[-1].isoformat()) if previous else None
        if not archive or not archive.get("complete"):
            return None
        rows = archive["success"]
        if any(finite(r.get("streak_days")) is None or r["streak_days"] < 1 for r in rows if main_board(r)):
            return None
        return {r["code"]: int(r["streak_days"]) for r in rows if main_board(r)}

    def inject_streaks(self, now):
        if self.cfg.mock_mode:
            return self.base_streaks(now)
        streaks = self.base_streaks(now)
        calendar = self.monitor.calendar
        calendar.yesterday_streaks = streaks or {}
        calendar.streaks_ready = streaks is not None
        return streaks

    def capture_live(self, engine, now):
        """Called after Engine.update; copy its full market, never filtered boards."""
        success, broken, observations = [], [], {}
        for source in engine.rows.values():
            if not main_board(source) or source.get("quote_missing"):
                continue
            row = normalize({**source, "change_pct": source.get("pct"),
                             "turnover_rate": source.get("turnover"),
                             "streak_days": source.get("consecutive_days")}, bool(source.get("is_limit_up")))
            observations[row["code"]] = row
            if row["is_limit_up"]:
                success.append(row)
            elif source.get("ever_limit_up"):
                broken.append(row)
        self.live = {"date": now.date().isoformat(), "complete": True,
                     "excluded_codes": [r["code"] for r in engine.rows.values() if not main_board(r)],
                     "success": success, "broken": broken, "observations": observations,
                     "updated_at": now.isoformat(), "lhb_status": "pending",
                     "aux_status": {"stock": "live", "concepts": "cached"}}

    def ladder(self, mode="review", selected=None, now=None):
        now = now or datetime.now(SHANGHAI)
        meta = self.meta(now)
        target = selected or meta["default_date"]
        if mode == "live":
            target = now.date().isoformat()
            archive = self.live if meta["live_available"] and self.live and self.live["date"] == target else None
            # Deterministic mock live stays usable before the monitor's first tick.
            if self.cfg.mock_mode and archive is None:
                archive = self.archives.get(target) or self.archives.get(meta["default_date"])
        else:
            archive = self.archives.get(target)
        empty = {"date": target, "mode": mode, "status": "pending" if meta["status"] == "ready" else meta["status"],
                 "simulated": self.cfg.mock_mode, "summary": {}, "success_tiers": [], "failed_tiers": [],
                 "first_board_broken": [], "active_15d_broken": [],
                 "concepts_summary": {"industries": [], "concepts": []}}
        if not archive:
            empty["error"] = "休市，盘中梯队不可用" if mode == "live" and not meta["live_available"] else "后台正在补齐复盘缓存；缺失数据不会视作零"
            return empty
        day = date.fromisoformat(target)
        sessions = self.sessions(now)
        if mode == "live" and self.cfg.mock_mode and day not in sessions:
            sessions = sorted([*sessions, day])
        previous = [d for d in sessions if d < day]
        prior = self.archives.get(previous[-1].isoformat()) if previous else None
        prior_ready = bool(prior and prior.get("complete") and all(
            finite(r.get("streak_days")) is not None and r["streak_days"] >= 1
            for r in prior["success"] if main_board(r)))
        prev = {r["code"]: r for r in prior["success"] if main_board(r)} if prior_ready else {}
        window = [d for d in sessions if d <= day][-15:]
        history = {d.isoformat(): archive if d == day else self.archives.get(d.isoformat()) for d in window}
        def complete(n):
            return len(window) >= n and all(history[d.isoformat()] and history[d.isoformat()].get("complete") for d in window[-n:])
        complete15, complete7 = complete(15), complete(7)
        hits = {}
        for d in window:
            cached = history[d.isoformat()]
            if cached and cached.get("complete"):
                for row in cached["success"]:
                    if main_board(row):
                        hits.setdefault(row["code"], []).append(d)
        sealed = {r["code"]: r for r in archive["success"] if main_board(r)}
        touched = {r["code"]: r for r in archive["broken"] if main_board(r)}
        observations = archive.get("observations", {})
        active_codes = {code for code, days in hits.items() if complete15 and len(days) >= 2} - sealed.keys() - prev.keys()
        codes = sealed.keys() | touched.keys() | prev.keys() | active_codes
        codes -= set(archive.get("excluded_codes", []))
        successful, failures, first_broken, active = [], [], [], []
        for code in sorted(codes):
            source = sealed.get(code) or touched.get(code) or observations.get(code)
            if source is None:
                # Static identity may be carried forward; prices and cash flows may not.
                source = prev.get(code) or next((r for cached in reversed(list(history.values())) if cached
                                               for r in cached["success"] if r["code"] == code), {})
                source = {key: source.get(key) for key in ("code", "name", "industry", "concepts")}
            if not main_board(source):
                continue
            is_sealed = code in sealed
            row = normalize(source, is_sealed)
            previous_streak = finite(prev.get(code, {}).get("streak_days")) if code in prev else (0 if prior_ready else None)
            previous_streak = int(previous_streak) if previous_streak is not None else None
            streak = finite(sealed.get(code, {}).get("streak_days")) if is_sealed else 0
            if is_sealed and previous_streak is not None:
                streak = previous_streak + 1
            if is_sealed and streak is not None and previous_streak is None:
                previous_streak = int(streak) - 1
            days = hits.get(code, [])
            count15 = len(days) if complete15 else None
            count7 = sum(d in window[-7:] for d in days) if complete7 else None
            pattern = f"{(window.index(day) - window.index(days[0]) + 1)}天{count15}板" if count15 and days else None
            row.update(streak_days=int(streak) if streak is not None else None,
                       prev_streak_days=previous_streak, limit_up_count_7d=count7,
                       limit_up_count_15d=count15, pattern_label=pattern, history_complete=complete15)
            row["status_label"] = status_label(previous_streak, is_sealed, code in touched, row["break_count"])
            if is_sealed:
                if streak is None:
                    row["status_label"] = "连板底表待补齐"
                successful.append(row)
            elif code in prev:
                failures.append(row)
            elif code in touched and prior_ready:
                first_broken.append(row)
            elif code in active_codes:
                active.append(row)
        score_rows(successful)
        secondary = failures + first_broken + active
        score_rows(secondary, successful or secondary)
        max_board = max((r["streak_days"] or 0 for r in successful), default=0)
        max_from = max([int(finite(r.get("streak_days")) or 0) for r in prev.values()] + [max_board - 1, 0])
        promotions = []
        for n in range(1, max_from + 1):
            denominator = sum(finite(r.get("streak_days")) == n for r in prev.values())
            success_count = sum(r["prev_streak_days"] == n for r in successful)
            promotions.append({"from_board": n, "success_count": success_count,
                               "failed_count": denominator - success_count if prior_ready else None,
                               "promotion_rate": round(success_count / denominator * 100, 2) if denominator else None})
        tiers = []
        titles = {1: "首板", 2: "二板", 3: "三板", 4: "四板"}
        for n in range(1, max_board + 1):
            items = [r for r in successful if r["streak_days"] == n]
            promotion = next((p for p in promotions if p["from_board"] == n - 1), {})
            tiers.append({"board_count": n, "title": titles.get(n, f"{n}板"),
                          "promotion_label": f"{n - 1}进{n}" if n > 1 else "首板",
                          "success_count": len(items), "failed_count": promotion.get("failed_count"),
                          "promotion_rate": promotion.get("promotion_rate"), "items": items})
        max_failed = max((r["prev_streak_days"] or 0 for r in failures), default=0)
        failed_tiers = [{**next((p for p in promotions if p["from_board"] == n), {}),
                         "from_board": n, "title": f"{n}进{n + 1} 失败",
                         "items": [r for r in failures if r["prev_streak_days"] == n]}
                        for n in range(1, max_failed + 1)]
        stale = mode == "live" and self.live is archive and (now - datetime.fromisoformat(archive["updated_at"])).total_seconds() > 15
        distribution_rows = successful + secondary
        industries = Counter(r["industry"] for r in distribution_rows if r["industry"])
        concepts = Counter(tag for r in distribution_rows for tag in r["concepts"])
        return {**empty, "status": "stale" if stale else "ready" if archive.get("complete") else "partial",
                "error": "盘中行情已超过15秒未更新，保留最近内存快照" if stale else None,
                "success_tiers": tiers, "failed_tiers": failed_tiers,
                "first_board_broken": first_broken, "active_15d_broken": active,
                "concepts_summary": {"industries": [{"name": k, "count": v} for k, v in industries.most_common()],
                                     "concepts": [{"name": k, "count": v} for k, v in concepts.most_common()]},
                "summary": {"limit_up_count": len(successful), "broken_count": len(touched),
                            "reseal_count": sum(bool(r["break_count"] and r["break_count"] > 0) for r in successful),
                            "max_board": max_board, "promotion_rates": promotions,
                            "updated_at": archive.get("updated_at"), "lhb_status": archive.get("lhb_status", "pending"),
                            "history_complete": complete15, "baseline_ready": prior_ready,
                            "aux_status": archive.get("aux_status", {}),
                            "lock_hint": f"收盘复盘固定至下一交易日15:00；下次切换 {meta['next_switch'] or '--'}"}}

    async def worker(self, operation, **args):
        process = None
        env = {k: v for k, v in os.environ.items() if k.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}}
        if self.cfg.http_proxy:
            env.update(HTTP_PROXY=self.cfg.http_proxy, HTTPS_PROXY=self.cfg.http_proxy)
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable, str(Path(__file__).with_name("review_worker.py")),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env=env)
            stdout, _ = await asyncio.wait_for(process.communicate(json.dumps({"operation": operation, **args}).encode()), 35)
            if process.returncode:
                raise ValueError("review data worker failed")
            return json.loads(stdout)
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()

    def _read(self, day):
        try:
            value = json.loads((self.path / f"{day}.json").read_text(encoding="utf-8"))
            if (value.get("version") != 1 or value.get("date") != day or value.get("simulated") is not self.cfg.mock_mode
                    or not isinstance(value.get("complete"), bool)
                    or any(not isinstance(value.get(key), list) for key in ("success", "broken"))
                    or not isinstance(value.get("observations"), dict)):
                raise ValueError("invalid review archive")
            # Reapply hard filtering even to legacy/local data.
            for key in ("success", "broken"):
                value[key] = [normalize(r, key == "success") for r in value[key] if main_board(r)]
            value["observations"] = {c: normalize(r, bool(r.get("is_limit_up")))
                                      for c, r in value["observations"].items() if main_board(r) and c == r["code"]}
            return value
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return None

    async def persist(self, archive):
        def write():
            self.path.mkdir(parents=True, exist_ok=True)
            save_json(self.path / f"{archive['date']}.json", archive)
        await asyncio.to_thread(write)
        self.archives[archive["date"]] = archive

    def enrich_metadata(self, row):
        fields = self.monitor.fetcher.sectors.lookup(row["code"])
        tags = [t.strip() for t in fields.get("raw_tags", "").replace("，", ",").split(",") if t.strip()]
        return {**row, "industry": row.get("industry") or fields.get("industry") or None,
                "concepts": row.get("concepts") or tags}

    async def refresh_core(self, day):
        try:
            data = await self.worker("pools", date=day)
        except (ValueError, TimeoutError, OSError):
            self.errors[day] = "涨停池抓取暂不可用；保留有效归档"
            raise
        states = data.get("status", {})
        if states.get("success") != "ready" or states.get("broken") != "ready":
            # Retain last good data; unavailable historical pools aren't zero stocks.
            self.errors[day] = "涨停/炸板历史池不可用；缺失不视作零"
            raise ValueError("limit pools unavailable for requested session")
        old = self.archives.get(day, {})
        archive = {**old, "version": 1, "date": day, "simulated": False, "complete": True,
                   "success": [self.enrich_metadata(normalize(r, True)) for r in data["success"] if main_board(r)],
                   "broken": [self.enrich_metadata(normalize(r)) for r in data["broken"] if main_board(r)],
                   "observations": old.get("observations", {}), "lhb_status": old.get("lhb_status", "pending"),
                   "aux_status": {**old.get("aux_status", {}), "pools": "ready"},
                   "updated_at": datetime.now(SHANGHAI).isoformat()}
        archive["excluded_codes"] = [q.code for q in self.monitor.fetcher.last_raw
                                     if q.source_ts and datetime.fromtimestamp(q.source_ts, SHANGHAI).date().isoformat() == day
                                     and not main_board({"code": q.code, "name": q.name})]
        for key in ("success", "broken"):
            for row in archive[key]:
                cached = old.get("observations", {}).get(row["code"], {})
                for field in ("main_inflow", "lhb_net_buy", "volume_ratio_5d", "volume_vs_prev", "flow_source"):
                    if cached.get(field) is not None:
                        row[field] = cached[field]
                row.update(normalize(row, key == "success"))
        await self.persist(archive)
        self.errors.pop(day, None)

    def candidates(self, day, now):
        days = [d.isoformat() for d in self.sessions(now) if d.isoformat() <= day][-15:]
        return {r["code"]: r for d in days for r in self.archives.get(d, {}).get("success", []) if main_board(r)} | {
            r["code"]: r for r in self.archives.get(day, {}).get("broken", []) if main_board(r)}

    async def enrich(self, day, now, lhb=False, *, only_lhb=False):
        if day not in self.archives:
            return
        archive = deepcopy(self.archives[day])
        candidates = {} if only_lhb else self.candidates(day, now)
        gate = asyncio.Semaphore(1)
        dates = [d for d in self.sessions(now) if d.isoformat() <= day]
        start = dates[max(0, len(dates) - 35)].isoformat()
        end = self.dates(now)[-1].isoformat()
        done = self.progress.setdefault(day, set())
        batch = [c for c in sorted(candidates) if c not in done][:3]
        async def stock(code):
            async with gate:
                if code not in self.series:
                    if time.monotonic() < self.stock_retry_at:
                        return code, {"rows": [], "status": {"price": "unavailable", "flow": "unavailable"}}
                    try:
                        self.series[code] = await self.worker("stock", code=code, start_date=start, end_date=end)
                        if self.series[code].get("rate_limited"):
                            self.stock_retry_at = time.monotonic() + 1200
                    except (ValueError, TimeoutError, OSError):
                        self.series[code] = {"rows": [], "status": {"price": "unavailable", "flow": "unavailable"}}
                return code, self.series[code]
        results = await asyncio.gather(*(stock(c) for c in batch))
        stock_ready = day not in self.stock_failures
        pool_rows = {r["code"]: r for r in archive["success"] + archive["broken"]}
        for code, data in results:
            done.add(code)
            rows = {r["date"]: r for r in data.get("rows", [])}
            raw = rows.get(day, {})
            source = pool_rows.get(code) or {k: candidates[code].get(k) for k in ("code", "name", "industry", "concepts")}
            old_row = archive["observations"].get(code, {})
            row = {**old_row, **source}
            for field in ("main_inflow", "lhb_net_buy", "flow_source"):
                if row.get(field) is None and old_row.get(field) is not None:
                    row[field] = old_row[field]
            for key in ("price", "change_pct", "amount", "turnover_rate", "volume", "main_inflow"):
                if finite(raw.get(key)) is not None:
                    row[key] = raw[key]
            if finite(raw.get("main_inflow")) is not None:
                row["flow_source"] = "eastmoney_history"
            if archive.get("lhb_status") == "ready":
                row["lhb_net_buy"] = finite(archive.get("lhb_net_buy", {}).get(code))
            # Today's system snapshot wins only with an exact exchange-date stamp.
            for q in self.monitor.fetcher.last_raw if day == now.date().isoformat() else []:
                if q.code == code and q.source_ts and datetime.fromtimestamp(q.source_ts, SHANGHAI).date().isoformat() == day:
                    row["main_inflow"], row["flow_source"] = q.main_inflow, q.flow_source
            volumes = [finite(rows.get(d.isoformat(), {}).get("volume")) for d in dates[-6:-1]]
            volume = finite(row.get("volume"))
            row["volume_vs_prev"] = volume / volumes[-1] if volume is not None and volumes and volumes[-1] is not None and volumes[-1] > 0 else None
            row["volume_ratio_5d"] = volume / (sum(volumes) / 5) if volume is not None and len(volumes) == 5 and all(v is not None and v > 0 for v in volumes) else None
            archive["observations"][code] = normalize(self.enrich_metadata(row), code in {r["code"] for r in archive["success"]})
            if not raw or data.get("status", {}).get("flow") != "ready":
                self.stock_failures.add(day)
                stock_ready = False
        for key in ("success", "broken"):
            archive[key] = [archive["observations"].get(r["code"], r) for r in archive[key]]
        finished = len(done) >= len(candidates)
        if not only_lhb:
            archive["aux_status"] = {**archive.get("aux_status", {}),
                                     "stock": ("ready" if stock_ready else "partial") if finished else "pending",
                                     "concepts": "cached" if self.monitor.fetcher.sectors.index else "pending"}
        if lhb:
            try:
                data = await self.worker("lhb", date=day)
                archive["lhb_status"] = data.get("status", "unavailable")
                if archive["lhb_status"] == "ready":
                    archive["lhb_net_buy"] = data.get("net_buy", {})
                    for r in archive["observations"].values():
                        r["lhb_net_buy"] = finite(archive["lhb_net_buy"].get(r["code"]))
                    for key in ("success", "broken"):
                        archive[key] = [archive["observations"].get(r["code"], r) for r in archive[key]]
                        for row in archive[key]:
                            row["lhb_net_buy"] = finite(archive["lhb_net_buy"].get(row["code"]))
            except (ValueError, TimeoutError, OSError):
                archive["lhb_status"] = "unavailable"
        archive["updated_at"] = datetime.now(SHANGHAI).isoformat()
        await self.persist(archive)
        return finished

    def queue_refresh(self, day, lhb=False, now=None):
        now = now or datetime.now(SHANGHAI)
        if day not in {d.isoformat() for d in self.dates(now)}:
            raise ValueError("date must be one of the latest 15 completed sessions")
        clock = time.monotonic()
        if self.refresh_pending or self.last_manual is not None and clock - self.last_manual < 30:
            return False
        self.requests.put_nowait((day, lhb))
        self.refresh_pending, self.last_manual = True, clock
        return True

    async def run(self):
        while True:
            try:
                now = datetime.now(SHANGHAI)
                sessions = self.sessions(now)
                target = locked_date(now, sessions)
                if target is None:
                    await asyncio.sleep(5)
                    continue
                if self.cfg.mock_mode:
                    if self.mock_day != now.date():
                        # Separate directory prevents mock archives replacing real closes.
                        data = mock_archives(sessions, now.date() if now.date() in sessions else target)
                        for archive in data.values():
                            await self.persist(archive)
                        self.mock_day = now.date()
                    if not self.requests.empty():
                        self.requests.get_nowait()
                        self.refresh_pending = False
                    await asyncio.sleep(2)
                    continue
                window = [d.isoformat() for d in sessions if d <= target][-30:]
                for day in window:
                    if day not in self.loaded_days:
                        cached = await asyncio.to_thread(self._read, day)
                        if cached:
                            self.archives[day] = cached
                        self.loaded_days.add(day)
                self.inject_streaks(now)
                async with self.lock:
                    if not self.requests.empty():
                        day, lhb = self.requests.get_nowait()
                        try:
                            await self.refresh_core(day)
                            self.series.clear()
                            self.progress.pop(day, None)
                            self.stock_failures.discard(day)
                            self.enriched.discard((day, "stock"))
                            if lhb:
                                await self.enrich(day, now, True, only_lhb=True)
                        finally:
                            self.refresh_pending = False
                        continue
                    today = now.date().isoformat()
                    if self.series_day != target.isoformat():
                        self.series.clear()
                        self.progress.clear()
                        self.series_day = target.isoformat()
                    # The close and LHB take priority over historical supplementation.
                    if today in window and now.time() >= daytime(15, 5) and (today, "close") not in self.enriched:
                        if time.monotonic() >= self.retry_at.get((today, "close"), 0):
                            self.retry_at[(today, "close")] = time.monotonic() + 60
                            await self.refresh_core(today)
                            self.enriched.add((today, "close"))
                            continue
                    if today in window and daytime(16, 40) <= now.time() <= daytime(17, 40):
                        if self.archives.get(today, {}).get("lhb_status") != "ready" and time.monotonic() >= self.retry_at.get((today, "lhb"), 0):
                            self.retry_at[(today, "lhb")] = time.monotonic() + 300
                            await self.enrich(today, now, True, only_lhb=True)
                            continue
                    # Yesterday's baseline is fetched first, then today's close.
                    previous = [d for d in window if d < now.date().isoformat()]
                    order = list(dict.fromkeys(([previous[-1]] if previous else []) + list(reversed(window))))
                    missing = next((d for d in order if not self.archives.get(d, {}).get("complete")
                                    and time.monotonic() >= self.retry_at.get((d, "core"), 0)), None)
                    if missing:
                        self.retry_at[(missing, "core")] = time.monotonic() + 300
                        await self.refresh_core(missing)
                        continue
                    # Supplement recent dates after core history; failures stay local.
                    pending = next((d for d in reversed(window[-15:]) if d in self.archives
                                    and (d, "stock") not in self.enriched
                                    and self.archives[d].get("aux_status", {}).get("stock") != "ready"), None)
                    if pending:
                        finished = await self.enrich(pending, now, self.archives[pending].get("lhb_status") != "ready"
                                                     and (pending < today or now.time() >= daytime(16, 40)))
                        if finished:
                            self.enriched.add((pending, "stock"))
                        continue
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("复盘后台局部失败，保留最近有效缓存并稍后重试", exc_info=True)
            await asyncio.sleep(5)
