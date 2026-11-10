"""Asynchronous quote adapters and a labelled, reproducible off-hours simulator."""
from __future__ import annotations

import asyncio
import logging
import math
import random
import re
import time
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP

import httpx

from config import Settings, SHANGHAI
from pre_market import MarketCalendar, Universe

log = logging.getLogger(__name__)


def number(value: object, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (ValueError, TypeError):
        return default


def limit_price(pre_close: float) -> float:
    return float((Decimal(str(pre_close)) * Decimal("1.10")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class Quote:
    code: str
    name: str
    price: float
    pct: float
    volume: float
    amount: float
    turnover: float
    pre_close: float
    float_cap: float
    listed: date | None
    main_inflow: float
    super_inflow: float
    large_inflow: float
    industry: str = ""
    raw_tags: str = ""
    high: float = 0.0
    source_ts: float | None = None
    flow_source: str = "eastmoney"
    split_estimated: bool = False
    flow_epoch: int = 0
    observed_since: date | None = None


@dataclass(frozen=True)
class Book:
    price: float
    buy_lots: float
    sell_lots: float
    ts: float


def parse_quote(row: dict, *, now: datetime | None = None) -> Quote | None:
    code, name = str(row.get("f12", "")), str(row.get("f14", "")).strip()
    if not re.fullmatch(r"(?:60|00)\d{4}", code) or not name or "ST" in name.upper():
        return None
    snapshot_time = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    minute = snapshot_time.hour * 60 + snapshot_time.minute
    # During auction Eastmoney uses '-' (not numeric zero) before the first
    # trade. Only normalize absent counters for genuinely untraded quotes;
    # missing flows on a traded quote remain invalid below.
    missing = (None, "", "-")
    if 555 <= minute < 570 and (row.get("f5") in missing or number(row.get("f5"), -1) == 0):
        row = dict(row)
        for key in ("f5", "f6", "f8", "f62", "f66", "f72"):
            if row.get(key) in missing:
                row[key] = 0
    price, volume, previous = number(row.get("f2")), number(row.get("f5"), -1), number(row.get("f18"))
    # Indicative auction prices are valid before the first trade (volume == 0).
    if price <= 0 or volume < 0 or previous <= 0:
        return None
    # A missing cash-flow field must not turn yesterday's negative value into fake inflow.
    if any(not math.isfinite(number(row.get(key), math.nan))
           for key in ("f3", "f6", "f8", "f21", "f62", "f66", "f72")):
        return None
    try:
        listed = datetime.strptime(str(row.get("f26", "")), "%Y%m%d").date()
    except ValueError:
        listed = None
    return Quote(
        code, name, price, number(row.get("f3")), volume, number(row.get("f6")),
        number(row.get("f8")), previous, number(row.get("f21")), listed,
        number(row.get("f62")), number(row.get("f66")), number(row.get("f72")),
        str(row.get("f100") or ""), str(row.get("f103") or ""),
        number(row.get("f15")), number(row.get("f124")) or None,
    )


def parse_books(text: str) -> dict[str, Book]:
    result = {}
    for code, body in re.findall(r'v_(?:sh|sz)(\d{6})="([^"]*)"', text):
        fields = body.split("~")
        if len(fields) <= 30:
            continue
        try:
            ts = datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(tzinfo=SHANGHAI).timestamp()
            price, buy, sell = (number(fields[i], -1) for i in (3, 10, 20))
            if price > 0 and buy >= 0 and sell >= 0:
                result[code] = Book(price, buy, sell, ts)
        except ValueError:
            continue
    return result


def parse_tencent(text: str, stocks: dict[str, dict]) -> tuple[dict[str, Quote], dict[str, Book], dict[str, tuple[float, float]]]:
    quotes, counters, seen = {}, {}, set()
    for market, code, body in re.findall(r'v_(sh|sz)(\d{6})="([^"]*)"', text):
        if code not in stocks or market != ("sh" if code.startswith("60") else "sz") or code in seen:
            raise ValueError("unexpected or duplicated Tencent quote")
        seen.add(code)
        fields = body.split("~")
        if len(fields) <= 44 or fields[2] != code:
            raise ValueError("truncated Tencent quote")
        name = fields[1].strip()
        if "ST" in name.upper() or "退" in name:
            continue
        price, previous = (number(fields[i], -1) for i in (3, 4))
        if price == 0:  # Explicit suspended/untraded stock, not a missing response.
            continue
        if not name or price <= 0 or previous <= 0:
            raise ValueError("invalid Tencent price")
        values = {i: number(fields[i], math.nan) for i in (6, 32, 33, 37, 38, 44)}
        if any(not math.isfinite(v) or (i != 32 and v < 0) for i, v in values.items()):
            raise ValueError("invalid Tencent numeric field")
        ts = datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(tzinfo=SHANGHAI).timestamp()
        meta = stocks[code]
        quotes[code] = Quote(code, name, price, values[32], values[6], values[37] * 10000,
                             values[38], previous, values[44] * 1e8,
                             date.fromisoformat(meta["listed"]) if meta.get("listed") else None,
                             0, 0, 0, high=values[33], source_ts=ts,
                             flow_source="snapshot_estimate", split_estimated=True,
                             observed_since=date.fromisoformat(meta["observed_since"])
                             if meta.get("observed_since") else None)
        counters[code] = tuple(number(fields[i], -1) for i in (7, 8))
    if seen != stocks.keys():
        raise ValueError(f"incomplete Tencent snapshot: {len(seen)}/{len(stocks)}")
    return quotes, parse_books(text), counters


def parse_flows(text: str, today: date) -> dict[str, float]:
    result = {}
    for market, code, body in re.findall(r'v_ff_(sh|sz)(\d{6})="([^"]*)"', text):
        fields = body.split("~")
        if (len(fields) <= 13 or fields[0] != market + code
                or fields[13] != today.strftime("%Y%m%d") or code in result):
            continue
        value = number(fields[3], math.nan)
        if math.isfinite(value):
            result[code] = value * 10000
    return result


class Fetcher:
    def __init__(self, client: httpx.AsyncClient, cfg: Settings):
        self.client, self.cfg = client, cfg
        self.semaphore = asyncio.Semaphore(cfg.max_concurrency)
        self.last_raw: list[Quote] = []
        self.last_total: int | None = None
        self.universe = Universe(client, cfg)
        self.last_books: dict[str, Book] = {}
        self.source = "tencent"
        self._flow_state: dict[str, tuple[Quote, float, float]] = {}
        self._ff_retry_at = 0.0
        self._snapshot_at: float | None = None

    async def _page(self, page: int, request_timeout: float | None = None) -> tuple[int, list[dict]]:
        params = {"pn": page, "pz": self.cfg.page_size, "po": 0, "np": 1,
                  "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                  "fltt": 2, "invt": 2, "fid": "f12", "fs": self.cfg.market_filter,
                  "fields": self.cfg.fields}
        async with self.semaphore:
            response = await self.client.get(self.cfg.eastmoney_url, params=params,
                                             timeout=request_timeout or self.cfg.http_timeout)
            response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get("diff"), list):
            raise ValueError("Eastmoney response has no diff list")
        if any(not isinstance(row, dict) for row in data["diff"]):
            raise ValueError("invalid quote row")
        try:
            total = int(data.get("total", 0))
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid market total") from exc
        return total, data["diff"]

    async def _fetch_eastmoney(self, *, bootstrap: bool = False) -> list[Quote]:
        # Reject partial/duplicate pages; never feed a partial market into percentiles.
        deadline = self.cfg.bootstrap_deadline if bootstrap else self.cfg.eastmoney_deadline
        request_timeout = self.cfg.bootstrap_http_timeout if bootstrap else self.cfg.http_timeout
        async with asyncio.timeout(deadline):
            if self.last_total is None:
                total, first = await self._page(1, request_timeout)
                pages = [(total, first)]
                start_page = 2
            else:
                total, pages, start_page = self.last_total, [], 1
            if total <= 0:
                raise ValueError("empty market snapshot")
            page_count = math.ceil(total / self.cfg.page_size)
            if page_count > self.cfg.max_pages:
                raise ValueError("unexpected market size")
            pages += await asyncio.gather(*(self._page(p, request_timeout) for p in range(start_page, page_count + 1)),
                                          return_exceptions=True)
            rows = []
            for page in pages:
                if isinstance(page, BaseException):
                    raise page
                other_total, batch = page
                if other_total != total:
                    self.last_total = None
                    raise ValueError("market universe changed during pagination")
                rows.extend(batch)
            codes = [str(row.get("f12", "")) for row in rows]
            if len(rows) != total or len(set(codes)) != total:
                raise ValueError("incomplete or duplicated market pages")
            snapshot_time = datetime.now(SHANGHAI)
            quotes = [q for row in rows if (q := parse_quote(row, now=snapshot_time)) is not None]
            if not quotes:
                raise ValueError("no tradable main-board quotes")
            self.last_raw = quotes
            self.last_total = total
            return quotes

    async def _tencent_batch(self, codes: list[str], *, flow: bool = False,
                             timeout: float | None = None) -> str:
        symbols = ",".join(("ff_" if flow else "") + ("sh" if c.startswith("60") else "sz") + c
                           for c in codes)
        async with self.semaphore:
            response = await self.client.get(self.cfg.tencent_url + symbols,
                                             headers={"Referer": "https://gu.qq.com/"},
                                             timeout=timeout or self.cfg.http_timeout)
            response.raise_for_status()
        return response.content.decode("gb18030", errors="replace")

    async def _flows(self, batches: list[list[str]], today: date) -> dict[str, float]:
        if time.monotonic() < self._ff_retry_at:
            return {}
        try:
            async with asyncio.timeout(self.cfg.book_deadline):
                results = await asyncio.gather(*(self._tencent_batch(b, flow=True,
                                                timeout=self.cfg.book_deadline) for b in batches),
                                               return_exceptions=True)
            flows = {c: v for text in results if isinstance(text, str)
                     for c, v in parse_flows(text, today).items()}
        except (TimeoutError, httpx.HTTPError, ValueError):
            flows = {}
        if not flows:
            # ff_ currently often returns none_match. Probe at most once a minute.
            self._ff_retry_at = time.monotonic() + 60
        return flows

    def _with_flow(self, q: Quote, counters: tuple[float, float], main: float | None) -> Quote:
        old_state = self._flow_state.get(q.code)
        buy, sell = counters
        old, old_buy, old_sell = old_state if old_state else (None, 0, 0)
        reset = bool(old and (datetime.fromtimestamp(q.source_ts, SHANGHAI).date() !=
                              datetime.fromtimestamp(old.source_ts, SHANGHAI).date()
                              or q.pre_close != old.pre_close or q.amount < old.amount
                              or q.volume < old.volume or buy < old_buy or sell < old_sell))
        if old and q.source_ts < old.source_ts:
            raise ValueError("out-of-order Tencent snapshot")
        source = "tencent_ff" if main is not None else "snapshot_estimate"
        epoch = (old.flow_epoch + int(reset or old.flow_source != source)) if old else 0
        if reset:
            old = None
        delta_amount = max(0, q.amount - old.amount) if old else q.amount
        if main is None:
            db, ds = (buy - old_buy, sell - old_sell) if old else (buy, sell)
            signed = delta_amount * (db - ds) / (db + ds) if db >= 0 and ds >= 0 and db + ds > 0 else 0
            main = (old.main_inflow if old else 0) + signed
        # ponytail: snapshot turnover cannot identify order size; an explicit
        # heuristic split replaces unavailable tick-level classifications.
        share = 0.4 + 0.2 * min(delta_amount / 1e6, 1)
        super_flow = old.super_inflow + (main - old.main_inflow) * share if old else main * share
        return replace(q, main_inflow=main, super_inflow=super_flow, large_inflow=main - super_flow,
                       flow_source=source, flow_epoch=epoch)

    async def fetch_market(self, *, bootstrap: bool = False) -> list[Quote]:
        today = datetime.now(SHANGHAI).date()
        stocks = await self.universe.load(today)
        ordered = sorted(stocks)
        batches = [ordered[i:i + 650] for i in range(0, len(ordered), 650)]
        deadline = self.cfg.bootstrap_deadline if bootstrap else self.cfg.eastmoney_deadline
        timeout = self.cfg.bootstrap_http_timeout if bootstrap else self.cfg.http_timeout
        # Discard the entire round on any missing/duplicate quote. Do not mutate
        # flow baselines or books until every quote batch has been validated.
        async with asyncio.timeout(deadline):
            quote_task = asyncio.gather(*(self._tencent_batch(b, timeout=timeout) for b in batches),
                                        return_exceptions=True)
            texts, flows = await asyncio.gather(quote_task, self._flows(batches, today))
            parsed, books, counters = {}, {}, {}
            for batch, text in zip(batches, texts):
                if isinstance(text, BaseException):
                    raise text
                group, group_books, group_counters = parse_tencent(text, {c: stocks[c] for c in batch})
                parsed.update(group)
                books.update(group_books)
                counters.update(group_counters)
            quotes = [self._with_flow(q, counters[c], flows.get(c) if
                      datetime.fromtimestamp(q.source_ts, SHANGHAI).date() == today else None)
                      for c, q in parsed.items()]
            if not quotes:
                raise ValueError("no tradable main-board quotes")
            self._flow_state = {q.code: (q, *counters[q.code]) for q in quotes}
            self.last_raw, self.last_books, self.last_total = quotes, books, len(stocks)
            self._snapshot_at = time.monotonic()
            return quotes

    async def fetch_books(self, codes: set[str], now: datetime) -> dict[str, Book]:
        if not codes:
            return {}
        if self._snapshot_at is not None:
            if time.monotonic() - self._snapshot_at > self.cfg.poll_seconds + self.cfg.http_timeout:
                return {}
            return {c: b for c, b in self.last_books.items()
                    if c in codes and abs(now.timestamp() - b.ts) <= 15}

        async def batch(items: list[str]) -> dict[str, Book]:
            symbols = ",".join(("sh" if c.startswith("60") else "sz") + c for c in items)
            async with self.semaphore:
                response = await self.client.get(self.cfg.tencent_url + symbols)
                response.raise_for_status()
            books = parse_books(response.content.decode("gb18030", errors="replace"))
            return {code: book for code, book in books.items()
                    if code in items and abs(now.timestamp() - book.ts) <= 15}

        try:
            async with asyncio.timeout(self.cfg.book_deadline):
                ordered = sorted(codes)
                results = await asyncio.gather(*(batch(ordered[i:i + 80]) for i in range(0, len(ordered), 80)),
                                               return_exceptions=True)
                for result in results:
                    if isinstance(result, BaseException):
                        raise result
                return {code: book for group in results for code, book in group.items()}
        except (httpx.HTTPError, TimeoutError, ValueError):
            log.warning("腾讯盘口超时/异常：本轮封单指标为空，不沿用旧盘口")
            return {}


def eligible(quotes: list[Quote], calendar: MarketCalendar, today: date, cfg: Settings,
             *, current_session: bool = False) -> list[Quote]:
    return [q for q in quotes if (not current_session or q.source_ts is None or
                                 datetime.fromtimestamp(q.source_ts, SHANGHAI).date() == today)
            and calendar.listing_is_mature(q.listed or q.observed_since,
                                           today, cfg.minimum_listing_sessions)]


def demo_seed() -> list[Quote]:
    # Real codes/names only; ALL prices, dates, volumes and cash flows below are fictional.
    stocks = [("600000", "浦发银行"), ("600036", "招商银行"), ("600519", "贵州茅台"),
              ("601318", "中国平安"), ("601398", "工商银行"), ("601857", "中国石油"),
              ("601888", "中国中免"), ("603259", "药明康德"), ("000001", "平安银行"),
              ("000002", "万科A"), ("000333", "美的集团"), ("000651", "格力电器"),
              ("000858", "五粮液"), ("002415", "海康威视"), ("002594", "比亚迪"),
              ("003816", "中国广核"), ("600276", "恒瑞医药"), ("601166", "兴业银行"),
              ("600030", "中信证券"), ("601012", "隆基绿能"), ("600309", "万华化学"),
              ("601899", "紫金矿业"), ("002475", "立讯精密"), ("600900", "长江电力")]
    return [Quote(code, name, 10.0 + i, 0.0, 100000, 1e8, 1.0, 10.0 + i,
                  1e10, date(2010, 1, 1), 0.0, 0.0, 0.0, "演示行业", "", 10.0 + i)
            for i, (code, name) in enumerate(stocks)]


class Simulator:
    def __init__(self, seeds: list[Quote], cfg: Settings, source: str):
        self.rng = random.Random(cfg.mock_seed)
        self.source = source
        self.base = {q.code: q for q in seeds}
        self.current = dict(self.base)
        self.tick = 0
        codes = list(self.base)
        self.cohort = self.rng.sample(codes, min(cfg.mock_cohort, len(codes)))
        self.sealed = set(self.cohort[:max(1, len(self.cohort) // 2)])

    def step(self, now: datetime) -> tuple[list[Quote], dict[str, Book]]:
        self.tick += 1
        # First tick is the baseline. Then a random subset rises by 0.8%-2.5%.
        phase = self.tick % 240
        stimulated = self.rng.sample(self.cohort, min(3, len(self.cohort))) if 2 <= phase <= 14 else []
        books = {}
        for code, base in self.base.items():
            old = self.current[code]
            ceiling = limit_price(base.pre_close)
            price, flow = old.price, old.main_inflow
            if code in self.cohort and (self.tick == 1 or phase == 0):
                price = round(base.pre_close * 0.99, 2)
                flow = 0.0
            elif code in stimulated:
                price = min(round(price + base.pre_close * self.rng.uniform(0.008, 0.025), 2), ceiling - 0.01)
                flow += self.rng.uniform(2e6, 12e6)
            if code in self.sealed and 16 <= phase <= 20:
                price = ceiling
                flow += 8e6
            elif code in self.sealed and phase == 21:
                price = round(ceiling - base.pre_close * 0.015, 2)
            elif code in self.sealed and 25 <= phase <= 30:
                price = ceiling
            elif code in self.sealed and phase == 31:
                price = round(ceiling - base.pre_close * 0.02, 2)
            # Afterwards hold steady: unsealed anomalies can turn gray and disappear.
            q = replace(base, price=price, pct=round((price / base.pre_close - 1) * 100, 4),
                        main_inflow=flow, super_inflow=flow * 0.6, large_inflow=flow * 0.4,
                        amount=base.amount + self.tick * 1e5, volume=base.volume + self.tick * 100,
                        high=max(old.high, price), source_ts=now.timestamp())
            self.current[code] = q
            books[code] = Book(price, self.rng.randint(1000, 30000) if price >= ceiling else 100,
                               0 if price >= ceiling else 500, now.timestamp())
        return list(self.current.values()), books
