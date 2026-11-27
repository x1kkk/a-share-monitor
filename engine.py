"""Pure in-memory state machine. Run `python engine.py` for boundary checks."""
from __future__ import annotations

import math
import time
from collections import OrderedDict, deque
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal

from config import DEFAULT_WEIGHTS, SHANGHAI, FilterSettings, Settings, stock_limit_prices
from fetcher import Book, Quote, demo_seed, limit_price

ORDERBOOK_SIGNALS = {"large_order_support", "suspected_active_buy", "suspected_large_smash"}
SIGNAL_PRIORITY = {"large_order_support": 0, "suspected_active_buy": 1, "suspected_large_smash": 1,
                   "rapid_rise": 2, "heavy_plunge": 2, "limit_up": 3, "break_open": 3}


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
        self.orderbook_baselines: dict[str, tuple[float, Quote]] = {}
        self.support_timers: dict[str, dict[float, float]] = {}
        self.orderbook_conditions: dict[str, set[str]] = {}
        self.signal_pending: dict[tuple[str, str], dict] = {}
        self.timelines: dict[str, deque] = {}

    def clear_windows(self) -> None:
        self.histories.clear()
        self.short_histories.clear()
        self.volume_histories.clear()
        self.previous_condition.clear()
        self.pending.clear()
        self.watchlist_condition.clear()
        self.orderbook_baselines.clear()
        self.support_timers.clear()
        self.orderbook_conditions.clear()
        self.signal_pending.clear()
        self.timelines.clear()

    def set_filters(self, filters: dict) -> None:
        was_enabled = self.filters.enable_orderbook_signals
        self.filters = FilterSettings(**filters)
        # The next quote evaluates the new thresholds against the old condition.
        # Resetting edge detectors here would duplicate VIP alerts on every PUT.
        if was_enabled and not self.filters.enable_orderbook_signals:
            self.orderbook_baselines.clear()
            self.support_timers.clear()
            self.orderbook_conditions.clear()
            self.signal_pending = {key: value for key, value in self.signal_pending.items()
                                   if key[1] not in ORDERBOOK_SIGNALS}

    def orderbook_signals(self, q: Quote, book: Book | None, now: datetime, clock: float,
                          reset: bool, ceiling: float, floor: float, allowed: bool) -> dict[str, dict]:
        old = self.orderbook_baselines.get(q.code)
        if old:
            previous = old[1]
            rollback = any(current is not None and before is not None and current < before
                           for current, before in ((q.outer_vol, previous.outer_vol), (q.inner_vol, previous.inner_vol)))
            reset = reset or rollback or clock - old[0] > 10 or clock <= old[0]
        if reset:
            old = None
            self.support_timers.pop(q.code, None)
            self.orderbook_conditions.pop(q.code, None)
            for key in list(self.signal_pending):
                if key[0] == q.code and key[1] in ORDERBOOK_SIGNALS:
                    del self.signal_pending[key]
        if not self.filters.enable_orderbook_signals or not allowed:
            self.orderbook_baselines.pop(q.code, None)
            self.support_timers.pop(q.code, None)
            self.orderbook_conditions.pop(q.code, None)
            return {}
        self.orderbook_baselines[q.code] = (clock, q)
        found = {}
        timers = self.support_timers.get(q.code, {})
        current_timers = {}
        strongest = None
        if book and q.price < ceiling:
            for index, (price, lots) in enumerate(book.bids):
                if price <= 0 or lots <= 0 or price >= ceiling:
                    continue
                others = [quantity for j, (p, quantity) in enumerate(book.bids)
                          if j != index and p > 0 and quantity > 0]
                # No invented historical baseline: at least one other valid bid
                # is required, and the candidate itself is excluded from its mean.
                amount = price * lots * 100
                if (not others or amount < self.filters.support_min_amount_wan * 10000
                        or lots < sum(others) / len(others) * 5):
                    continue
                key = round(price, 2)
                since = timers.get(key, clock)
                current_timers[key] = since
                duration = clock - since
                if duration >= 30 and (strongest is None or amount > strongest["amount"]):
                    level = book.bid_levels[index] if len(book.bid_levels) == len(book.bids) else index + 1
                    strongest = {"level": level, "price": price, "amount": amount,
                                 "duration_seconds": round(duration, 2)}
        self.support_timers[q.code] = current_timers
        if strongest:
            summary = (f"买{strongest['level']}({strongest['price']:.2f}元)挂单"
                       f"{strongest['amount'] / 10000:,.0f}万，已连续托底{strongest['duration_seconds']:g}秒")
            found["large_order_support"] = {"support_info": strongest, "summary": summary}
        # An adjacent interval longer than six seconds is a new baseline, never
        # a two-second burst. Startup cumulative counters are never a delta.
        if old and 0 < clock - old[0] <= 6:
            previous = old[1]
            delta_amount = q.amount - previous.amount
            change = (q.price - previous.price) / previous.price * 100
            flow = q.main_inflow - previous.main_inflow
            outer = q.outer_vol - previous.outer_vol if q.outer_vol is not None and previous.outer_vol is not None else None
            inner = q.inner_vol - previous.inner_vol if q.inner_vol is not None and previous.inner_vol is not None else None
            if delta_amount >= self.filters.burst_min_amount_wan * 10000:
                buy = outer > inner * 1.5 if outer is not None and inner is not None else flow > 0
                sell = inner > outer * 1.5 if outer is not None and inner is not None else flow < 0
                kind = "suspected_active_buy" if change >= 0.3 - 1e-9 and buy and q.price < ceiling else (
                    "suspected_large_smash" if change <= -0.3 + 1e-9 and sell and q.price > floor else None)
                if kind:
                    dominance = ("外盘占优" if kind == "suspected_active_buy" else "内盘占优") if (
                        outer is not None and inner is not None) else "内外盘缺失，按主力资金净额增量降级"
                    summary = (f"{clock - old[0]:g}秒成交额突增{delta_amount / 10000:,.0f}万，{dominance}，"
                               f"股价{'上冲' if change >= 0 else '回落'}{abs(change):.2f}%")
                    found[kind] = {"summary": summary, "delta_amount": delta_amount,
                                   "delta_outer_vol": outer, "delta_inner_vol": inner,
                                   "delta_main_inflow": flow, "burst_change_pct": change,
                                   "burst_window_seconds": round(clock - old[0], 2)}
        return found

    def timeline(self, code: str) -> list[dict]:
        return list(reversed(self.timelines.get(code, ())))[:8]

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
    def signal(row: dict, kind: str, now: datetime, extra: dict | None = None) -> dict:
        labels = {"rapid_rise": "急速拉升", "heavy_plunge": "放量跳水",
                  "limit_up": "首次封板", "break_open": "炸板告警",
                  "large_order_support": "大单托底", "suspected_active_buy": "疑似盘口扫货",
                  "suspected_large_smash": "疑似大单砸盘"}
        direction = "bearish" if kind in {"heavy_plunge", "break_open", "suspected_large_smash"} else "bullish"
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
        estimated = kind in {"suspected_active_buy", "suspected_large_smash"}
        if kind in ORDERBOOK_SIGNALS:
            details = [extra["summary"]] if extra else []
            tags = [labels[kind]] + (["快照估算"] if estimated else ["五档买盘"])
            reason = f"{now:%H:%M:%S} 触发【{labels[kind]}】" + ("(快照估算)" if estimated else "")
            if details:
                reason += "，" + "，".join(details)
        return {"direction": direction, "signal_type": kind, "signal_label": labels[kind],
                "signal_tags": tags, "reason": reason,
                "signal_estimated": estimated, "support_info": (extra or {}).get("support_info"),
                "summary": (extra or {}).get("summary") or "，".join(details),
                "speed_1m": speed, "window_1m_seconds": row.get("window_1m_seconds"),
                "inflow_1m": row.get("inflow_1m"), "vol_ratio": ratio,
                "alert_level": (2 if row.get("is_watchlist") else 1) if kind in ORDERBOOK_SIGNALS else (
                    3 if row.get("is_watchlist") or kind in {"limit_up", "break_open"} else 2)}

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
        events, rows, active_signals = [], {}, {}
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
            self.orderbook_baselines.pop(code, None)
            self.support_timers.pop(code, None)
            self.orderbook_conditions.pop(code, None)
            for key in list(self.signal_pending):
                if key[0] == code:
                    del self.signal_pending[key]

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
            ceiling, floor = stock_limit_prices(q.pre_close, q.name, now.date())
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
                        "signal_estimated": False, "support_info": None,
                        "outer_inner_ratio": round(q.outer_vol / q.inner_vol, 2)
                        if q.outer_vol is not None and q.inner_vol is not None and q.inner_vol > 0 else None,
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
            if direction:
                kind = "rapid_rise" if direction == "bullish" else "heavy_plunge"
                active_signals[q.code, kind] = self.signal(row, kind, now)
                previous = (self.watchlist_condition if vip else self.previous_condition).get(q.code)
                if direction != previous and (vip or q.code not in self.anomalies or
                        self.anomaly_signals.get(q.code, {}).get("signal_type") != kind):
                    self.signal_pending[q.code, kind] = active_signals[q.code, kind]
            if vip:
                self.watchlist_condition[q.code] = direction
            self.previous_condition[q.code] = direction
            book_signals = self.orderbook_signals(q, book, now, clock, reset, ceiling, floor, self.scan_allowed(row))
            previous_book_signals = self.orderbook_conditions.get(q.code, set())
            for kind, extra in book_signals.items():
                info = self.signal(row, kind, now, extra)
                active_signals[q.code, kind] = info
                if kind not in previous_book_signals:
                    self.signal_pending[q.code, kind] = info
            self.orderbook_conditions[q.code] = set(book_signals)

        # Re-evaluate pending conditions, never queue a burst after it has ended.
        self.signal_pending = {key: active_signals[key] for key in self.signal_pending if key in active_signals}
        scores = {}
        for direction in ("bullish", "bearish"):
            members = {code for (code, _), signal in active_signals.items() if signal["direction"] == direction}
            cohort = {code: rows[code] for code in members}
            if direction == "bullish":
                metrics_by_weight = {"speed_2m": "speed_1m", "main_inflow": "main_inflow",
                                     "turnover": "turnover", "large_inflow": "large_inflow", "amount": "amount"}
                values = {c: {key: row[metric] for key, metric in metrics_by_weight.items()}
                          for c, row in cohort.items()}
            else:
                values = {c: {"speed_2m": abs(row["speed_1m"]) if row["speed_1m"] is not None else None,
                              "main_inflow": max(0, -row["inflow_1m"]), "turnover": row["turnover"],
                              "large_inflow": row["vol_ratio"], "amount": row["amount"]} for c, row in cohort.items()}
                for code, row in cohort.items():
                    if row["is_watchlist"] and row["inflow_1m"] >= 0:
                        values[code]["main_inflow"] = max(0, -row["main_inflow"])
            ranks = {metric: percentiles({c: v[metric] for c, v in values.items() if v[metric] is not None})
                     for metric in self.weights}
            for code in cohort:
                available = sum(w for m, w in self.weights.items() if code in ranks[m])
                scores[code, direction] = round(sum(ranks[m][code] * w for m, w in self.weights.items()
                                                    if code in ranks[m]) / available, 2) if available else 50.0
        for (code, kind), signal in active_signals.items():
            signal["score"] = scores[code, signal["direction"]]
        active_by_code = {}
        for (code, _), info in active_signals.items():
            active_by_code.setdefault(code, []).append(info)
        for code, current in active_by_code.items():
            rows[code].update(max(current, key=lambda info: (info["alert_level"], SIGNAL_PRIORITY[info["signal_type"]],
                                                           info["direction"] == "bearish")))

        # Quotas count stock admission, not each timeline item. A new signal on
        # an already admitted same-direction card updates it without resetting TTL.
        accepted_pairs = set()
        for direction in ("bullish", "bearish"):
            candidates = {code for (code, _), signal in self.signal_pending.items() if signal["direction"] == direction}
            for code in sorted(candidates, key=lambda c: (c not in self.watchlist, -scores[c, direction], c)):
                existing = code in self.anomalies and self.anomaly_signals.get(code, {}).get("direction") == direction
                if code in self.watchlist or existing:
                    accepted_pairs.add((code, direction))
                elif self.minute_counts[direction] < self.cfg.max_new_per_minute:
                    self.minute_counts[direction] += 1
                    accepted_pairs.add((code, direction))
        accepted_signals = {key: info for key, info in self.signal_pending.items()
                            if (key[0], info["direction"]) in accepted_pairs}
        representatives = {}
        for (code, _), info in accepted_signals.items():
            old = representatives.get(code)
            rank = lambda item: (item["alert_level"], SIGNAL_PRIORITY[item["signal_type"]], item["direction"] == "bearish")
            if old is None or rank(info) > rank(old):
                representatives[code] = info
        fresh = sorted((code for code, info in representatives.items() if code not in self.anomalies or
                        self.anomaly_signals.get(code, {}).get("direction") != info["direction"]),
                       key=lambda c: (c not in self.watchlist, -representatives[c]["score"], c))
        for code in reversed(fresh):
            self.anomalies[code] = Entry(clock, now.isoformat())
            self.anomalies.move_to_end(code, last=False)
        self.anomaly_signals.update(representatives)
        by_code = {q.code: q for q in quotes}
        for (code, kind), info in sorted(accepted_signals.items(), key=lambda item: (
                item[0][0] not in self.watchlist, item[0][0], SIGNAL_PRIORITY[item[0][1]])):
            event("anomaly", by_code[code], score=info["score"], speed_2m=rows[code]["speed_2m"],
                  inflow_2m=rows[code]["inflow_2m"], **{k: v for k, v in info.items() if k != "score"})
            self.signal_pending.pop((code, kind), None)
        self.pending = {code for code, _ in self.signal_pending}
        self.minute_count = self.minute_counts["bullish"]
        for item in events:
            if item["event_type"] != "anomaly":
                item.update(self.signal(rows[item["code"]], item["event_type"], now))
                rows[item["code"]].update(self.signal(rows[item["code"]], item["event_type"], now))
        for item in sorted(events, key=lambda item: (item["alert_level"], SIGNAL_PRIORITY[item["signal_type"]])):
            self.timelines.setdefault(item["code"], deque(maxlen=12)).append({
                "time": now.strftime("%H:%M:%S"), "direction": item["direction"], "signal_type": item["signal_type"],
                "signal_label": item["signal_label"], "price": item["price"], "change_pct": item["pct"],
                "summary": item["summary"], "is_estimated": item["signal_estimated"]})
        # Missing a code is not evidence of expiry or a limit-up. Keep its last
        # displayed values without feeding stale rows into fresh-market ranks.
        retained = {code: {**self.rows[code], "quote_missing": True}
                    for code in set(self.anomalies) | self.watchlist if code not in rows and code in self.rows}
        self.rows = {**retained, **rows}
        for code in {item["code"] for item in events if item["is_watchlist"]}:
            choices = [item for item in events if item["code"] == code and item["is_watchlist"]]
            item = max(choices, key=lambda event: (event["alert_level"], SIGNAL_PRIORITY[event["signal_type"]]))
            self.watchlist_events[code] = Entry(clock, now.isoformat())
            self.watchlist_events.move_to_end(code, last=False)
            self.watchlist_kinds[code] = item["event_type"]
            self.watchlist_signals[code] = {key: item[key] for key in (
                "direction", "signal_type", "signal_label", "signal_tags", "reason", "speed_1m", "summary",
                "window_1m_seconds", "inflow_1m", "vol_ratio", "alert_level", "signal_estimated", "support_info")}
        for code, row in self.rows.items():
            row["timeline"] = self.timeline(code)
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
