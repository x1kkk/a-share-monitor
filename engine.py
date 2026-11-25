"""Pure in-memory state machine. Run `python engine.py` for boundary checks."""
from __future__ import annotations

import math
import time
from collections import OrderedDict, deque
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal

from config import DEFAULT_WEIGHTS, SHANGHAI, FilterSettings, Settings
from fetcher import Book, Quote, demo_seed, limit_price


def percentiles(values: dict[str, float]) -> dict[str, float]:
    """Average ranks for ties. A flat market gets 50, not a spurious 100."""
    ordered = sorted(values, key=values.get)
    result = {}
    start, count = 0, len(ordered)
    while start < count:
        end = start + 1
        while end < count and values[ordered[end]] == values[ordered[start]]:
            end += 1
        score = 50.0 if count == 1 else ((start + end - 1) / 2) / (count - 1) * 100
        for code in ordered[start:end]:
            result[code] = score
        start = end
    return result


@dataclass
class LimitState:
    sealed: bool = False
    first_seal_ts: float | None = None
    break_count: int = 0
    touched: bool = False
    book_broken: bool = False


@dataclass
class Entry:
    entered_clock: float
    entered_at: str


class Engine:
    def __init__(self, cfg: Settings):
        self.cfg = cfg
        self.weights = dict(DEFAULT_WEIGHTS)
        self.filters = FilterSettings()
        self.histories: dict[str, deque] = {}
        self.short_histories: dict[str, deque] = {}
        self.volume_histories: dict[str, deque] = {}
        self.limits: dict[str, LimitState] = {}
        self.anomalies: OrderedDict[str, Entry] = OrderedDict()
        self.previous_condition: dict[str, str | None] = {}
        self.pending: set[str] = set()
        self.minute: str | None = None
        self.minute_count = 0
        self.minute_counts = {"bullish": 0, "bearish": 0}
        self.day = None
        self.rows: dict[str, dict] = {}
        self.watchlist: set[str] = set()
        self.watchlist_events: OrderedDict[str, Entry] = OrderedDict()
        self.watchlist_kinds: dict[str, str] = {}
        self.watchlist_condition: dict[str, str | None] = {}
        self.anomaly_signals: dict[str, dict] = {}
        self.watchlist_signals: dict[str, dict] = {}

    def clear_windows(self) -> None:
        self.histories.clear()
        self.short_histories.clear()
        self.volume_histories.clear()
        self.previous_condition.clear()
        self.pending.clear()
        self.watchlist_condition.clear()

    def set_filters(self, filters: dict) -> None:
        self.filters = FilterSettings(**filters)
        # The next quote evaluates the new thresholds against the old condition.
        # Resetting edge detectors here would duplicate VIP alerts on every PUT.

    def configuration(self) -> dict:
        return {"filters": self.filters.snapshot(), "ttl": {
            "anomalies": {"active_seconds": self.cfg.active_seconds, "remove_seconds": self.cfg.remove_seconds},
            "watchlist": {"active_seconds": self.cfg.watchlist_active_seconds,
                          "remove_seconds": self.cfg.watchlist_remove_seconds}}}

    def scan_allowed(self, row: dict) -> bool:
        return (row["code"] in self.watchlist or
                ((not self.filters.exclude_st or "ST" not in row["name"].upper()) and
                 (not self.filters.exclude_low_amount or row["amount"] >= self.filters.min_daily_amount)))

    def short_metrics(self, q: Quote, clock: float, reset: bool) -> dict:
        short = self.short_histories.setdefault(q.code, deque())
        volumes = self.volume_histories.setdefault(q.code, deque())
        if reset or (short and (q.volume < short[-1][3] or q.amount < short[-1][4])):
            short.clear()
            volumes.clear()
        while short and clock - short[0][0] > 60:
            short.popleft()
        short.append((clock, q.price, q.main_inflow, q.volume, q.amount))
        # ponytail: ten-second cumulative samples bound baseline storage to ~128
        # points per stock; raw tick storage is unnecessary for minute-rate ratios.
        if not volumes or clock - volumes[-1][0] >= 10:
            volumes.append((clock, q.volume))
        while len(volumes) > 1 and volumes[1][0] < clock - 1260:
            volumes.popleft()
        old = short[0]
        span = clock - old[0]
        base_span = old[0] - volumes[0][0]
        ratio = None
        if clock - volumes[0][0] >= 120 and span > 0 and base_span >= 60:
            baseline = (old[3] - volumes[0][1]) / base_span
            if baseline > 0:
                ratio = max(0, (q.volume - old[3]) / span) / baseline
        return {"speed_1m": round((q.price - old[1]) / q.pre_close * 100, 6) if span > 0 else None,
                "window_1m_seconds": round(span, 2), "inflow_1m": q.main_inflow - old[2],
                "vol_ratio": round(ratio, 6) if ratio is not None else None}

    @staticmethod
    def signal(row: dict, kind: str, now: datetime) -> dict:
        labels = {"rapid_rise": "急速拉升", "heavy_plunge": "放量跳水",
                  "limit_up": "首次封板", "break_open": "炸板告警"}
        direction = "bearish" if kind in {"heavy_plunge", "break_open"} else "bullish"
        tags = [labels[kind]]
        details = []
        speed, ratio = row.get("speed_1m"), row.get("vol_ratio")
        if speed is not None:
            window = "1分钟" if row["window_1m_seconds"] >= 60 else f"{row['window_1m_seconds']:g}秒窗口"
            details.append(f"{window}{'涨' if speed >= 0 else '跌'}{abs(speed):.2f}%")
        if ratio is not None:
            details.append(f"量比{ratio:.1f}倍")
            if ratio >= row["volume_threshold"]:
                tags.append("放量")
        elif kind in {"rapid_rise", "heavy_plunge"}:
            details.append("量比基线未就绪，按涨跌速与资金判断")
        cumulative = row.get("is_watchlist") and (kind == "rapid_rise" or (
            kind == "heavy_plunge" and row.get("inflow_1m") is not None and row["inflow_1m"] >= 0))
        flow = row.get("main_inflow") if cumulative else row.get("inflow_1m")
        if flow is not None and flow != 0:
            absolute = abs(flow)
            cash = f"{absolute / 1e8:.2f}亿" if absolute >= 1e8 else (
                f"{absolute / 1e4:.0f}万" if absolute >= 1e4 else f"{absolute:.0f}元")
            scope = "当日累计" if cumulative else "窗口"
            details.append(f"{scope}主力净{'流入' if flow > 0 else '流出'}{cash}")
            tags.append("资金流入" if flow > 0 else "资金流出")
        if row.get("flow_source") == "snapshot_estimate":
            details.append("资金为快照估算")
        reason = f"{now:%H:%M:%S} 触发【{labels[kind]}】"
        if details:
            reason += "，" + "，".join(details)
        return {"direction": direction, "signal_type": kind, "signal_label": labels[kind],
                "signal_tags": tags, "reason": reason,
                "speed_1m": speed, "window_1m_seconds": row.get("window_1m_seconds"),
                "inflow_1m": row.get("inflow_1m"), "vol_ratio": ratio,
                "alert_level": 3 if row.get("is_watchlist") or kind in {"limit_up", "break_open"} else 2}

    def set_weights(self, weights: dict[str, float]) -> None:
        if set(weights) != set(DEFAULT_WEIGHTS):
            raise ValueError("weights must contain exactly: " + ", ".join(DEFAULT_WEIGHTS))
        if any(not math.isfinite(v) or v < 0 or v > 100 for v in weights.values()) or sum(weights.values()) <= 0:
            raise ValueError("each weight must be finite, 0..100; total must be positive")
        total = sum(weights.values())
        self.weights = {key: value * 100 / total for key, value in weights.items()}

    def anomaly_board(self, clock: float | None = None) -> list[dict]:
        """TTL advances even when providers are down; prices and seal state do not."""
        clock = time.monotonic() if clock is None else clock
        board = []
        for code, entry in list(self.anomalies.items()):
            age = max(0, clock - entry.entered_clock)
            if age >= self.cfg.remove_seconds:
                del self.anomalies[code]
                self.anomaly_signals.pop(code, None)
            elif code in self.rows and self.scan_allowed(self.rows[code]):
                board.append({**self.rows[code], **self.anomaly_signals.get(code, {}), "entered_at": entry.entered_at,
                              "age_seconds": round(age, 2),
                              "status": "active" if age < self.cfg.active_seconds else "expired"})
        return board

    def watchlist_board(self, clock: float | None = None) -> list[dict]:
        clock = time.monotonic() if clock is None else clock
        board = []
        for code, entry in list(self.watchlist_events.items()):
            age = max(0, clock - entry.entered_clock)
            if code not in self.watchlist or age >= self.cfg.watchlist_remove_seconds:
                del self.watchlist_events[code]
                self.watchlist_kinds.pop(code, None)
                self.watchlist_signals.pop(code, None)
            elif code in self.rows:
                board.append({**self.rows[code], **self.watchlist_signals.get(code, {}), "is_watchlist": True,
                              "event_type": self.watchlist_kinds[code], "entered_at": entry.entered_at,
                              "age_seconds": round(age, 2),
                              "status": "active" if age < self.cfg.watchlist_active_seconds else "expired"})
        return board

    def watchlist_rows(self) -> list[dict]:
        rows = [{**self.rows[code], "is_watchlist": True} if code in self.rows else
                {"code": code, "name": code, "quote_missing": True, "is_watchlist": True}
                for code in sorted(self.watchlist)]
        return sorted(rows, key=lambda row: (not row.get("is_limit_up", False), row["code"]))

    @property
    def touched_codes(self) -> set[str]:
        return {code for code, state in self.limits.items() if state.touched}

    def update(self, quotes: list[Quote], books: dict[str, Book], now: datetime,
               *, clock: float | None = None, simulated: bool = False,
               yesterday_streaks: dict[str, int] | None = None) -> dict:
        now = now.astimezone(SHANGHAI)
        clock = time.monotonic() if clock is None else clock
        if self.day != now.date():
            weights = self.weights.copy()
            watchlist = self.watchlist.copy()
            filters = self.filters
            self.__init__(self.cfg)
            self.weights, self.day = weights, now.date()
            self.watchlist = watchlist
            self.filters = filters
        minute = now.strftime("%Y%m%d%H%M")
        if minute != self.minute:
            self.minute, self.minute_count = minute, 0
            self.minute_counts = {"bullish": 0, "bearish": 0}
        events, rows, vip_signals, conditions = [], {}, set(), {}
        thresholds = self.filters.snapshot()
        codes = {q.code for q in quotes}
        for code in list(self.anomalies):
            if clock - self.anomalies[code].entered_clock >= self.cfg.remove_seconds:
                del self.anomalies[code]
                self.anomaly_signals.pop(code, None)
        for code in set(self.histories) - codes:
            self.histories.pop(code, None)
            self.short_histories.pop(code, None)
            self.volume_histories.pop(code, None)
            self.previous_condition.pop(code, None)
            self.watchlist_condition.pop(code, None)
            self.pending.discard(code)

        def event(kind: str, q: Quote, **extra: object) -> None:
            events.append({"code": q.code, "name": q.name, "event_type": kind,
                           "time": now.isoformat(), "price": q.price, "pct": q.pct,
                           "simulated": simulated, "is_watchlist": q.code in self.watchlist,
                           "priority": "vip" if q.code in self.watchlist else "normal", **extra})

        for q in quotes:
            history = self.histories.setdefault(q.code, deque(maxlen=60))
            # No cross-session, long-gap, ex-rights or cash-counter-reset deltas.
            short = self.short_histories.get(q.code)
            reset = bool(history and (clock - history[-1][0] > 10 or q.pre_close != history[-1][3]
                            or q.main_inflow < history[-1][2] - 1e10
                            or q.flow_epoch != history[-1][4])) or bool(
                                short and (q.volume < short[-1][3] or q.amount < short[-1][4]))
            if reset:
                history.clear()
                self.previous_condition.pop(q.code, None)
                self.watchlist_condition.pop(q.code, None)
                self.pending.discard(q.code)
            while history and clock - history[0][0] > 120:
                history.popleft()
            history.append((clock, q.price, q.main_inflow, q.pre_close, q.flow_epoch))
            old = history[0]
            speed = (q.price - old[1]) / q.pre_close * 100
            inflow = q.main_inflow - old[2]
            metrics = self.short_metrics(q, clock, reset)
            ceiling = limit_price(q.pre_close)
            floor = limit_price(q.pre_close, rate=-0.10)
            at_limit = Decimal(str(q.price)) >= Decimal(str(ceiling))
            at_floor = Decimal(str(q.price)) <= Decimal(str(floor))
            state = self.limits.setdefault(q.code, LimitState())
            state.touched = state.touched or q.high >= ceiling or at_limit
            book = books.get(q.code)
            # Two sources may be milliseconds apart. Only price-matched fresh books are usable.
            if book and (abs(now.timestamp() - book.ts) > 15 or abs(book.price - q.price) > 0.005):
                book = None
            pressure = bool(book and at_limit and book.sell_lots >= self.cfg.break_sell_lots)
            if not at_limit:
                state.book_broken = False
            elif pressure:
                state.book_broken = True
            elif book and book.sell_lots == 0:
                state.book_broken = False
            # Missing books cannot undo a known sell-pressure break. A zero-ask book
            # confirms resealing; price-only equality is enough before any such break.
            sealed = at_limit and not state.book_broken
            if sealed and not state.sealed:
                if state.first_seal_ts is None:
                    state.first_seal_ts = now.timestamp()
                    event("limit_up", q)
            elif state.sealed and not sealed:
                state.break_count += 1
                event("break_open", q, break_count=state.break_count)
            state.sealed = sealed
            if at_limit:
                self.anomalies.pop(q.code, None)
                self.anomaly_signals.pop(q.code, None)
            seal_amount = q.price * book.buy_lots * 100 if sealed and book else (0.0 if not sealed else None)
            row = asdict(q)
            row["listed"] = q.listed.isoformat() if q.listed else None
            row["observed_since"] = q.observed_since.isoformat() if q.observed_since else None
            row.update({"speed_2m": round(speed, 6), "inflow_2m": inflow, **metrics,
                        "is_watchlist": q.code in self.watchlist,
                        "window_seconds": round(clock - old[0], 2), "limit_up_price": ceiling,
                        "limit_down_price": floor, "at_limit_down_price": at_floor,
                        "volume_threshold": thresholds["vol_ratio_threshold"],
                        "direction": None, "signal_type": None, "signal_label": None,
                        "signal_tags": [], "reason": None, "alert_level": 1, "score": None,
                        "is_limit_up": sealed, "at_limit_price": at_limit, "ever_limit_up": state.touched,
                        "first_seal_time": datetime.fromtimestamp(state.first_seal_ts, SHANGHAI).isoformat()
                        if state.first_seal_ts is not None else None,
                        "first_seal_ts": state.first_seal_ts, "break_count": state.break_count,
                        "consecutive_days": (yesterday_streaks.get(q.code, 0) + 1)
                        if sealed and yesterday_streaks is not None else None,
                        "seal_amount": seal_amount,
                        "seal_float_pct": seal_amount / q.float_cap * 100
                        if seal_amount is not None and q.float_cap > 0 else None,
                        "buy_one_lots": book.buy_lots if book else None,
                        "sell_one_lots": book.sell_lots if book else None,
                        "sell_pressure": pressure,
                        "concepts": [tag.strip() for tag in q.raw_tags.replace("，", ",").split(",")
                                     if tag.strip() and tag.strip() not in {"-", "None", "nan"}],
                        # Main flow already includes super + large; never count super twice.
                        "total_net_inflow": q.super_inflow + q.large_inflow,
                        "total_net_inflow_scope": "super+large", "quote_missing": False,
                        "simulated": simulated})
            rows[q.code] = row
            current_speed, ratio = metrics["speed_1m"], metrics["vol_ratio"]
            volume_ok = ratio is None or ratio >= thresholds["vol_ratio_threshold"]
            vip = q.code in self.watchlist
            rise_cash = q.main_inflow > 0 if vip else metrics["inflow_1m"] > 0
            plunge_cash = (metrics["inflow_1m"] < 0 or q.main_inflow < 0) if vip else metrics["inflow_1m"] < 0
            direction = None
            if current_speed is not None and volume_ok and self.scan_allowed(row):
                if current_speed >= thresholds["rise_speed_threshold"] - 1e-9 and rise_cash and not at_limit:
                    direction = "bullish"
                elif current_speed <= -thresholds["plunge_speed_threshold"] + 1e-9 and plunge_cash and not at_floor:
                    direction = "bearish"
            conditions[q.code] = direction
            if vip:
                if direction and direction != self.watchlist_condition.get(q.code):
                    vip_signals.add(q.code)
                self.watchlist_condition[q.code] = direction
            previous_signal = self.anomaly_signals.get(q.code, {}).get("direction")
            if direction and direction != self.previous_condition.get(q.code) and (
                    q.code not in self.anomalies or previous_signal != direction):
                self.pending.add(q.code)
            if not direction:
                self.pending.discard(q.code)
            self.previous_condition[q.code] = direction

        # Separate candidate populations: bearish urgency cannot dilute bullish ranks.
        for direction in ("bullish", "bearish"):
            cohort = {c: row for c, row in rows.items() if conditions[c] == direction}
            if direction == "bullish":
                metrics_by_weight = {"speed_2m": "speed_1m", "main_inflow": "main_inflow",
                                     "turnover": "turnover", "large_inflow": "large_inflow", "amount": "amount"}
                values = {c: {key: row[metric] for key, metric in metrics_by_weight.items()}
                          for c, row in cohort.items()}
            else:
                # Keep the five-key weight API; bearish slots mean speed, net
                # outflow, turnover, volume ratio and amount respectively.
                values = {c: {"speed_2m": abs(row["speed_1m"]), "main_inflow": max(0, -row["inflow_1m"]),
                              "turnover": row["turnover"], "large_inflow": row["vol_ratio"],
                              "amount": row["amount"]} for c, row in cohort.items()}
                for code, row in cohort.items():
                    if row["is_watchlist"] and row["inflow_1m"] >= 0:
                        values[code]["main_inflow"] = max(0, -row["main_inflow"])
            ranks = {metric: percentiles({c: v[metric] for c, v in values.items() if v[metric] is not None})
                     for metric in self.weights}
            for code, row in cohort.items():
                available = sum(weight for metric, weight in self.weights.items() if code in ranks[metric])
                row["score"] = round(sum(ranks[m][code] * w for m, w in self.weights.items()
                                         if code in ranks[m]) / available, 2) if available else 50.0
                kind = "rapid_rise" if direction == "bullish" else "heavy_plunge"
                row.update(self.signal(row, kind, now))
        candidates = sorted((c for c in self.pending if c in rows and conditions[c] and
                             (c not in self.anomalies or self.anomaly_signals.get(c, {}).get("direction") != conditions[c])),
                            key=lambda c: (-rows[c]["score"], c))
        vip = [c for c in candidates if c in self.watchlist]
        ordinary = [c for c in candidates if c not in self.watchlist]
        normal = []
        for direction in ("bullish", "bearish"):
            admitted = [c for c in ordinary if conditions[c] == direction][
                :max(0, self.cfg.max_new_per_minute - self.minute_counts[direction])]
            normal.extend(admitted)
            self.minute_counts[direction] += len(admitted)
        normal.sort(key=lambda c: (-rows[c]["score"], c))
        accepted = vip + normal
        # Reverse insertion keeps the strongest of a simultaneous batch at the top.
        for code in reversed(accepted):
            self.anomalies[code] = Entry(clock, now.isoformat())
            self.anomalies.move_to_end(code, last=False)
            self.pending.discard(code)
            self.anomaly_signals[code] = {**self.signal(rows[code], rows[code]["signal_type"], now),
                                          "score": rows[code]["score"]}
        self.minute_count = self.minute_counts["bullish"]
        by_code = {q.code: q for q in quotes}
        for code in accepted:
            if code not in self.watchlist:
                event("anomaly", by_code[code], score=rows[code]["score"],
                      speed_2m=rows[code]["speed_2m"], inflow_2m=rows[code]["inflow_2m"])
        for code in sorted(vip_signals, key=lambda code: (-rows[code]["score"], code)):
            event("anomaly", by_code[code], score=rows[code]["score"],
                  speed_2m=rows[code]["speed_2m"], inflow_2m=rows[code]["inflow_2m"])
        for item in events:
            row = rows[item["code"]]
            kind = ("rapid_rise" if conditions[item["code"]] == "bullish" else "heavy_plunge") if (
                item["event_type"] == "anomaly") else item["event_type"]
            item.update(self.signal(row, kind, now))
            if item["event_type"] != "anomaly":
                row.update(self.signal(row, kind, now))
        # Missing a code is not evidence of expiry or a limit-up. Keep its last
        # displayed values without feeding stale rows into fresh-market ranks.
        retained = {code: {**self.rows[code], "quote_missing": True}
                    for code in set(self.anomalies) | self.watchlist if code not in rows and code in self.rows}
        self.rows = {**retained, **rows}
        for item in reversed(events):
            code = item["code"]
            if item["is_watchlist"]:
                self.watchlist_events[code] = Entry(clock, now.isoformat())
                self.watchlist_events.move_to_end(code, last=False)
                self.watchlist_kinds[code] = item["event_type"]
                self.watchlist_signals[code] = {key: item[key] for key in (
                    "direction", "signal_type", "signal_label", "signal_tags", "reason", "speed_1m",
                    "window_1m_seconds", "inflow_1m", "vol_ratio", "alert_level")}
        board = self.anomaly_board(clock)
        sealed = sorted((row for row in rows.values() if row["is_limit_up"]),
                        key=lambda row: (row["first_seal_ts"], row["code"]))
        others = sorted((row for row in rows.values() if not row["is_limit_up"]),
                        key=lambda row: (-row["pct"], row["code"]))
        # If >150 stocks seal, retaining every sealed stock takes precedence over the cap.
        leaders = sealed + others[:max(0, 150 - len(sealed))]
        return {"type": "snapshot", "time": now.isoformat(), "simulated": simulated,
                "stock_count": len(rows), "anomalies": board, "leaders": leaders,
                "watchlist_anomalies": self.watchlist_board(clock),
                "watchlist_leaders": self.watchlist_rows(),
                "weights": self.weights.copy(), "events": events, **self.configuration(),
                "minute_admitted": self.minute_count, "minute_admitted_by_direction": self.minute_counts.copy(),
                "pending_count": len(self.pending)}


def self_check() -> None:
    from fetcher import parse_quote, parse_books
    cfg, now = Settings(), datetime(2026, 11, 3, 9, 30, tzinfo=SHANGHAI)
    seed = demo_seed()
    e = Engine(cfg)
    e.update(seed, {}, now, clock=0)
    raised = [replace(q, price=round(q.pre_close * 1.03, 2), main_inflow=1e7) for q in seed]
    result = e.update(raised, {}, now + timedelta(seconds=2), clock=2)
    assert len(result["anomalies"]) == 10 and result["pending_count"] == 14
    order = [r["code"] for r in result["anomalies"]]
    for second in range(4, 60, 2):
        result = e.update(list(reversed(raised)), {}, now + timedelta(seconds=second), clock=second)
    assert [r["code"] for r in result["anomalies"]] == order
    result = e.update(raised, {}, now + timedelta(seconds=60), clock=60)
    assert result["minute_admitted"] <= 10 and len(result["anomalies"]) == 20
    # Isolate one record to verify exact TTL boundaries and no persistent-condition re-entry.
    e = Engine(cfg)
    e.update(seed[:1], {}, now, clock=0)
    e.update(raised[:1], {}, now + timedelta(seconds=2), clock=2)
    for second in range(4, 424, 2):
        result = e.update(raised[:1], {}, now + timedelta(seconds=second), clock=second)
        if second == 120:
            assert result["anomalies"][0]["status"] == "active"
        if second == 122:
            assert result["anomalies"][0]["status"] == "expired"
        if second == 422:
            assert not result["anomalies"]
    result = e.update(raised[:1], {}, now + timedelta(seconds=424), clock=424)
    assert not result["anomalies"]
    outage = Engine(cfg)
    outage.update(seed[:1], {}, now, clock=0)
    outage.update(raised[:1], {}, now + timedelta(seconds=2), clock=2)
    assert outage.anomaly_board(122)[0]["status"] == "expired"
    assert len(outage.anomaly_board(421.999)) == 1
    assert not outage.anomaly_board(422)
    # Snapshot omissions retain order, Entry and last values until the exact TTL.
    missing = Engine(cfg)
    missing.update(seed[:2], {}, now, clock=0)
    result = missing.update(raised[:2], {}, now + timedelta(seconds=2), clock=2)
    original = [row["code"] for row in result["anomalies"]]
    result = missing.update([], {}, now + timedelta(seconds=4), clock=4)
    assert [row["code"] for row in result["anomalies"]] == original
    assert all(row["quote_missing"] and row["age_seconds"] == 2 for row in result["anomalies"])
    result = missing.update(raised[:1], {}, now + timedelta(seconds=122), clock=122)
    assert [row["code"] for row in result["anomalies"]] == original
    assert all(row["status"] == "expired" for row in result["anomalies"])
    assert not missing.update([], {}, now + timedelta(seconds=422), clock=422)["anomalies"]
    transferred = Engine(cfg)
    transferred.update(seed[:1], {}, now, clock=0)
    transferred.update(raised[:1], {}, now + timedelta(seconds=2), clock=2)
    limit_quote = replace(seed[0], price=limit_price(seed[0].pre_close),
                          super_inflow=6e6, large_inflow=4e6)
    result = transferred.update([limit_quote], {}, now + timedelta(seconds=4), clock=4)
    assert not result["anomalies"] and result["leaders"][0]["is_limit_up"]
    assert result["leaders"][0]["total_net_inflow"] == 1e7
    q = seed[0]
    sealed = replace(q, price=limit_price(q.pre_close), high=limit_price(q.pre_close))
    result = e.update([sealed], {}, now + timedelta(seconds=426), clock=426)
    assert [event["event_type"] for event in result["events"]] == ["limit_up"]
    result = e.update([q], {}, now + timedelta(seconds=428), clock=428)
    assert result["leaders"][0]["break_count"] == 1
    e.update([q], {}, now + timedelta(seconds=430), clock=430)
    result = e.update([sealed], {}, now + timedelta(seconds=432), clock=432)
    assert not any(event["event_type"] == "limit_up" for event in result["events"])
    pressure_time = now + timedelta(seconds=434)
    result = e.update([sealed], {q.code: Book(sealed.price, 100, 20000, pressure_time.timestamp())},
                      pressure_time, clock=434)
    assert result["leaders"][0]["break_count"] == 2 and not result["leaders"][0]["is_limit_up"]
    result = e.update([sealed], {}, now + timedelta(seconds=436), clock=436)
    assert not result["leaders"][0]["is_limit_up"]  # missing book cannot confirm resealing
    clean_time = now + timedelta(seconds=438)
    result = e.update([sealed], {q.code: Book(sealed.price, 100, 0, clean_time.timestamp())},
                      clean_time, clock=438)
    assert result["leaders"][0]["is_limit_up"] and result["leaders"][0]["seal_amount"] == sealed.price * 10000
    assert limit_price(10.05) == 11.06
    assert set(percentiles({"a": 0, "b": 0}).values()) == {50}
    assert parse_quote({"f12": "300001", "f14": "普通股票"}) is None
    assert parse_quote({"f12": "600001", "f14": "*ST测试", "f2": 10, "f5": 10, "f18": 10}) is None
    assert parse_books('v_sh600000="bad";') == {}
    auction_row = {"f12": "600000", "f14": "竞价测试", "f2": 10.2, "f3": 2,
                   "f5": 0, "f6": 0, "f8": 0, "f18": 10, "f21": 1e9,
                   "f62": 0, "f66": 0, "f72": 0}
    assert parse_quote(auction_row).volume == 0
    assert parse_quote({**auction_row, "f5": -1}) is None
    assert parse_quote({**auction_row, "f3": "-"}) is None
    placeholders = {**auction_row, **{key: "-" for key in ("f5", "f6", "f62", "f66", "f72")}}
    auction = now.replace(hour=9, minute=20)
    parsed = parse_quote(placeholders, now=auction)
    assert parsed is not None and parsed.volume == 0 and parsed.main_inflow == 0
    assert parse_quote(placeholders, now=auction.replace(minute=30)) is None
    assert parse_quote({**placeholders, "f5": 100}, now=auction) is None
    from fetcher import Simulator
    from notifier import Notifier
    from pre_market import MarketCalendar
    simulator, simulated_engine = Simulator(seed, cfg, "self_check"), Engine(cfg)
    seen, saw_expired, saw_removed = set(), False, False
    entered = set()
    for tick in range(225):
        tick_time = now + timedelta(seconds=tick * 2)
        quotes, books = simulator.step(tick_time)
        result = simulated_engine.update(quotes, books, tick_time, clock=tick * 2, simulated=True)
        seen.update(event["event_type"] for event in result["events"])
        present = {row["code"] for row in result["anomalies"]}
        entered.update(present)
        saw_expired |= any(row["status"] == "expired" for row in result["anomalies"])
        if tick > 220 and entered - simulator.sealed - present:
            saw_removed = True
    assert seen == {"anomaly", "limit_up", "break_open"} and saw_expired and saw_removed
    notifier = Notifier(None, replace(cfg, wechat_webhook_url=""))
    raw_event = {"code": q.code, "name": q.name, "event_type": "anomaly", "simulated": True,
                 "price": q.price, "pct": q.pct, "time": now.isoformat()}
    assert len(notifier.publish([raw_event], clock=0)) == 1
    assert not notifier.publish([raw_event], clock=299)
    assert len(notifier.publish([raw_event], clock=300)) == 1
    calendar = MarketCalendar(sessions=[now.date() + timedelta(days=i) for i in range(20)],
                              loaded_for=now.date() + timedelta(days=19), error=None)
    assert not calendar.listing_is_mature(now.date() + timedelta(days=6), calendar.loaded_for)
    assert calendar.listing_is_mature(now.date() + timedelta(days=5), calendar.loaded_for)
    session_calendar = MarketCalendar(sessions=[now.date()], loaded_for=now.date(), error=None)
    for hour, minute, expected in ((9, 14, "closed"), (9, 15, "morning"),
                                    (9, 25, "morning"), (9, 29, "morning"),
                                    (11, 29, "morning"), (11, 30, "closed"),
                                    (13, 0, "afternoon"), (15, 0, "closed")):
        assert session_calendar.session(now.replace(hour=hour, minute=minute)) == expected
    calendar.error = "offline"
    assert not calendar.covered(calendar.loaded_for)
    print("PASS: minute cap, stable rows, TTL/outage, seal/break/reseal, sell pressure, rounding, "
          "ties, auction/zero-volume, missing quotes, fund scope, full mock lifecycle, "
          "300s cooldown, 15-session listing boundary")


if __name__ == "__main__":
    self_check()
