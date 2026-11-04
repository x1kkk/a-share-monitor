"""Pure in-memory state machine. Run `python engine.py` for boundary checks."""
from __future__ import annotations

import math
import time
from collections import OrderedDict, deque
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal

from config import DEFAULT_WEIGHTS, SHANGHAI, Settings
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
        self.histories: dict[str, deque] = {}
        self.limits: dict[str, LimitState] = {}
        self.anomalies: OrderedDict[str, Entry] = OrderedDict()
        self.previous_condition: dict[str, bool] = {}
        self.pending: set[str] = set()
        self.minute: str | None = None
        self.minute_count = 0
        self.day = None
        self.rows: dict[str, dict] = {}

    def clear_windows(self) -> None:
        self.histories.clear()
        self.previous_condition.clear()
        self.pending.clear()

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
            elif code in self.rows:
                board.append({**self.rows[code], "entered_at": entry.entered_at,
                              "age_seconds": round(age, 2),
                              "status": "active" if age < self.cfg.active_seconds else "expired"})
        return board

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
            self.__init__(self.cfg)
            self.weights, self.day = weights, now.date()
        minute = now.strftime("%Y%m%d%H%M")
        if minute != self.minute:
            self.minute, self.minute_count = minute, 0
        events, rows = [], {}
        codes = {q.code for q in quotes}
        for code in list(self.anomalies):
            if clock - self.anomalies[code].entered_clock >= self.cfg.remove_seconds:
                del self.anomalies[code]
        for code in set(self.histories) - codes:
            self.histories.pop(code, None)
            self.previous_condition.pop(code, None)
            self.pending.discard(code)

        def event(kind: str, q: Quote, **extra: object) -> None:
            events.append({"code": q.code, "name": q.name, "event_type": kind,
                           "time": now.isoformat(), "price": q.price, "pct": q.pct,
                           "simulated": simulated, **extra})

        for q in quotes:
            history = self.histories.setdefault(q.code, deque(maxlen=60))
            # No cross-session, long-gap, ex-rights or cash-counter-reset deltas.
            if history and (clock - history[-1][0] > 10 or q.pre_close != history[-1][3]
                            or q.main_inflow < history[-1][2] - 1e10):
                history.clear()
            while history and clock - history[0][0] > 120:
                history.popleft()
            history.append((clock, q.price, q.main_inflow, q.pre_close))
            old = history[0]
            speed = (q.price - old[1]) / q.pre_close * 100
            inflow = q.main_inflow - old[2]
            ceiling = limit_price(q.pre_close)
            at_limit = Decimal(str(q.price)) >= Decimal(str(ceiling))
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
            seal_amount = q.price * book.buy_lots * 100 if sealed and book else (0.0 if not sealed else None)
            row = asdict(q)
            row["listed"] = q.listed.isoformat() if q.listed else None
            row.update({"speed_2m": round(speed, 6), "inflow_2m": inflow,
                        "window_seconds": round(clock - old[0], 2), "limit_up_price": ceiling,
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
            qualifies = speed >= 2.0 - 1e-9 and inflow > 0 and not at_limit
            if qualifies and not self.previous_condition.get(q.code, False) and q.code not in self.anomalies:
                self.pending.add(q.code)
            if not qualifies:
                self.pending.discard(q.code)
            self.previous_condition[q.code] = qualifies

        ranks = {metric: percentiles({code: row[metric] for code, row in rows.items()})
                 for metric in self.weights}
        for code, row in rows.items():
            row["score"] = round(sum(ranks[m][code] * w / 100 for m, w in self.weights.items()), 2)
        candidates = sorted((c for c in self.pending if c in rows and c not in self.anomalies),
                            key=lambda c: (-rows[c]["score"], c))
        accepted = candidates[:max(0, self.cfg.max_new_per_minute - self.minute_count)]
        # Reverse insertion keeps the strongest of a simultaneous batch at the top.
        for code in reversed(accepted):
            self.anomalies[code] = Entry(clock, now.isoformat())
            self.anomalies.move_to_end(code, last=False)
            self.pending.discard(code)
        self.minute_count += len(accepted)
        by_code = {q.code: q for q in quotes}
        for code in accepted:
            event("anomaly", by_code[code], score=rows[code]["score"],
                  speed_2m=rows[code]["speed_2m"], inflow_2m=rows[code]["inflow_2m"])
        # Missing a code is not evidence of expiry or a limit-up. Keep its last
        # displayed values without feeding stale rows into fresh-market ranks.
        retained = {code: {**self.rows[code], "quote_missing": True}
                    for code in self.anomalies if code not in rows and code in self.rows}
        self.rows = {**retained, **rows}
        board = self.anomaly_board(clock)
        sealed = sorted((row for row in rows.values() if row["is_limit_up"]),
                        key=lambda row: (row["first_seal_ts"], row["code"]))
        others = sorted((row for row in rows.values() if not row["is_limit_up"]),
                        key=lambda row: (-row["pct"], row["code"]))
        # If >150 stocks seal, retaining every sealed stock takes precedence over the cap.
        leaders = sealed + others[:max(0, 150 - len(sealed))]
        return {"type": "snapshot", "time": now.isoformat(), "simulated": simulated,
                "stock_count": len(rows), "anomalies": board, "leaders": leaders,
                "weights": self.weights.copy(), "events": events,
                "minute_admitted": self.minute_count, "pending_count": len(self.pending)}


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
