"""Exchange sessions and yesterday's streak cache; no weekday approximation in live mode."""
from __future__ import annotations

import asyncio
import bisect
import json
import logging
import os
import re
import sys
import tempfile
from html import unescape
from pathlib import Path
from dataclasses import dataclass, field
from datetime import date, datetime

import httpx

from config import SHANGHAI, Settings

log = logging.getLogger(__name__)

# A subprocess bounds AKShare's synchronous network work and can actually be killed
# on timeout (cancelling asyncio.to_thread would leave its requests running).
AK_BOOTSTRAP = r'''
import contextlib, datetime, json, sys
with contextlib.redirect_stdout(sys.stderr):
    import akshare as ak
    dates = sorted(str(x)[:10] for x in ak.tool_trade_date_hist_sina()["trade_date"])
print(json.dumps({"sessions": dates}))
'''


def save_json(path: Path, data: dict) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".market-", suffix=".tmp", delete=False) as stream:
            temporary = stream.name
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def stock_rows(rows: list[dict]) -> dict[str, dict]:
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise ValueError("invalid universe rows")
    result = {}
    for row in rows:
        code, name = str(row.get("code", "")), str(row.get("name", "")).strip()
        if not re.fullmatch(r"(?:00|60)[0-9]{4}", code) or not name or "ST" in name.upper() or "退" in name:
            continue
        item = {"code": code, "name": name, "listed": None}
        for key in ("listed", "observed_since"):
            if row.get(key):
                item[key] = date.fromisoformat(str(row[key])).isoformat()
        if code in result:
            raise ValueError("duplicated universe code")
        result[code] = item
    return result


class Universe:
    """Complete per-exchange snapshots, with a last-known-good atomic disk cache."""

    def __init__(self, client: httpx.AsyncClient, cfg: Settings, path: Path | None = None):
        self.client, self.cfg = client, cfg
        self.path = path or Path(__file__).resolve().parent / "universe_cache.json"
        self.stocks: dict[str, dict] = {}
        self.loaded_for: date | None = None
        self.semaphore = asyncio.Semaphore(cfg.max_concurrency)

    async def _gather(self, requests):
        results = await asyncio.gather(*requests, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
        return results

    async def _get(self, url: str, **kwargs):
        async with self.semaphore:
            response = await self.client.get(url, timeout=self.cfg.bootstrap_http_timeout, **kwargs)
            response.raise_for_status()
            return response.json()

    async def _exchange(self, prefix: str) -> dict[str, dict]:
        if prefix == "60":
            params = {"sqlId": "COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L", "STOCK_TYPE": "1",
                      "COMPANY_STATUS": "2,4,5,7,8", "type": "inParams", "isPagination": "true",
                      "pageHelp.pageSize": 2000, "pageHelp.pageNo": 1,
                      "pageHelp.beginPage": 1, "pageHelp.endPage": 1}
            data = await self._get("https://query.sse.com.cn/sseQuery/commonQuery.do", params=params,
                                   headers={"Referer": "https://www.sse.com.cn/assortment/stock/list/share/"})
            rows = data["result"]
            if len(rows) != int(data["pageHelp"]["total"]):
                raise ValueError("incomplete SSE code table")
            return stock_rows([{"code": r["A_STOCK_CODE"], "name": r["SEC_NAME_CN"],
                                "listed": r["LIST_DATE"]} for r in rows])
        params = {"SHOWTYPE": "JSON", "CATALOGID": "1110", "TABKEY": "tab1", "selectModule": "main"}

        async def page(n):
            data = await self._get("https://www.szse.cn/api/report/ShowReport/data",
                                   params={**params, "PAGENO": n},
                                   headers={"Referer": "https://www.szse.cn/market/product/stock/list/index.html"})
            if not isinstance(data, list) or not data or not isinstance(data[0], dict):
                raise ValueError("invalid SZSE response")
            return data[0]

        first = await page(1)
        count, total = (int(first["metadata"][key]) for key in ("pagecount", "recordcount"))
        if not 1 <= count <= 200:
            raise ValueError("unexpected SZSE page count")
        pages = [first] + await self._gather(page(n) for n in range(2, count + 1))
        if any(int(p["metadata"]["recordcount"]) != total for p in pages):
            raise ValueError("SZSE universe changed during pagination")
        rows = [r for p in pages for r in p["data"]]
        if len(rows) != total:
            raise ValueError("incomplete SZSE code table")
        return stock_rows([{"code": r["agdm"], "name": unescape(re.sub(r"<[^>]+>", "", r["agjc"])),
                            "listed": r["agssrq"]} for r in rows])

    async def _sina(self, prefix: str) -> dict[str, dict]:
        base = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center."
        node = "sh_a" if prefix == "60" else "sz_a"
        total = int(await self._get(base + "getHQNodeStockCount", params={"node": node}))
        if not 1000 <= total <= 6000:
            raise ValueError("unexpected Sina universe size")
        pages = await self._gather(self._get(base + "getHQNodeData", params={
            "node": node, "page": n, "num": 100, "sort": "symbol", "asc": 1})
            for n in range(1, (total + 99) // 100 + 1))
        if any(not isinstance(p, list) for p in pages):
            raise ValueError("invalid Sina code table")
        rows = [r for p in pages for r in p]
        if len(rows) != total or len({r["code"] for r in rows}) != total:
            raise ValueError("incomplete or duplicated Sina code table")
        return stock_rows(rows)

    async def _listing_dates(self, stocks: dict[str, dict]) -> None:
        # Sina's code table omits listing dates. Only cold/uncached symbols need
        # a one-time basic-profile lookup; never fabricate a mature listing date.
        async def profile(item):
            try:
                async with self.semaphore:
                    response = await self.client.get(
                        "https://vip.stock.finance.sina.com.cn/corp/go.php/vCI_CorpInfo/stockid/"
                        + item["code"] + ".phtml", timeout=self.cfg.bootstrap_http_timeout)
                    response.raise_for_status()
                text = re.sub(r"<[^>]+>", "", response.content.decode("gb18030", errors="replace"))
                match = re.search(r"上市日期[：:\s]*(\d{4}-\d{2}-\d{2})", unescape(text))
                if match:
                    item["listed"] = date.fromisoformat(match[1]).isoformat()
            except (httpx.HTTPError, ValueError):
                pass

        missing = [r for r in stocks.values() if not r.get("listed")]
        if missing:
            try:
                async with asyncio.timeout(self.cfg.calendar_timeout * 4):
                    await asyncio.gather(*(profile(item) for item in missing))
                    pending = [r for r in missing if not r.get("listed")]
                    if 0 < len(pending) <= 100:
                        await asyncio.gather(*(profile(item) for item in pending))
            except TimeoutError:
                log.warning("新浪上市资料补全超时，保留已验证的上市日期")
            log.info("新浪上市资料补全 %d/%d；未知日期继续严格过滤",
                     sum(bool(r.get("listed")) for r in missing), len(missing))

    async def load(self, today: date) -> dict[str, dict]:
        if self.loaded_for == today:
            return self.stocks
        cached_date = None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            cached = stock_rows(data["stocks"])
            if any(sum(c.startswith(p) for c in cached) < 1000 for p in ("00", "60")):
                raise ValueError("incomplete universe cache")
            self.stocks = cached
            cached_date = date.fromisoformat(data["updated_for"])
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError):
            log.warning("股票池缓存损坏/不完整，将尝试重新获取完整代码表")
        if cached_date == today:
            await self._listing_dates(self.stocks)
            self.loaded_for = today
            try:
                save_json(self.path, {"updated_for": today.isoformat(), "stocks": list(self.stocks.values())})
            except OSError:
                log.warning("股票池缓存写入失败", exc_info=True)
            return self.stocks

        refreshed = set()
        async def market(prefix):
            old = {c: r for c, r in self.stocks.items() if c.startswith(prefix)}
            for source in (self._exchange, self._sina):
                try:
                    async with asyncio.timeout(self.cfg.calendar_timeout):
                        stocks = await source(prefix)
                    if len(stocks) < max(1000, len(old) * 0.95):
                        raise ValueError("truncated universe")
                    refreshed.add(prefix)
                    return {c: {**old.get(c, {}), **r,
                                "listed": r.get("listed") or old.get(c, {}).get("listed"),
                                "observed_since": old.get(c, {}).get("observed_since", today.isoformat())}
                            for c, r in stocks.items()}
                except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError):
                    log.warning("%s 基础代码源 %s 不可用", prefix, source.__name__)
            if len(old) >= 1000:
                log.warning("%s 使用最近完整缓存 %d 只", prefix, len(old))
                return old
            raise ValueError(f"{prefix}: complete universe unavailable; refusing demo/partial pool")

        markets = await asyncio.gather(market("00"), market("60"), return_exceptions=True)
        for result in markets:
            if isinstance(result, BaseException):
                raise result
        stocks = {c: r for group in markets for c, r in group.items()}
        await self._listing_dates(stocks)
        try:
            save_json(self.path, {"updated_for": (today if len(refreshed) == 2 else cached_date or date.min).isoformat(),
                                  "stocks": list(stocks.values())})
        except OSError:
            log.warning("股票池缓存写入失败；使用完整内存股票池", exc_info=True)
        self.stocks, self.loaded_for = stocks, today
        return stocks


@dataclass
class MarketCalendar:
    sessions: list[date] = field(default_factory=list)
    yesterday_streaks: dict[str, int] = field(default_factory=dict)
    streaks_ready: bool = False
    loaded_for: date | None = None
    error: str | None = "calendar not initialized"
    cache_path: Path = field(default_factory=lambda: Path(__file__).resolve().parent / "market_calendar_cache.json")

    async def load(self, cfg: Settings, today: date) -> None:
        process = None
        if self.loaded_for != today:
            self.yesterday_streaks.clear()
            self.streaks_ready = False
        try:
            process_env = {key: value for key, value in os.environ.items()
                           if key.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}}
            if cfg.http_proxy:
                process_env.update(HTTP_PROXY=cfg.http_proxy, HTTPS_PROXY=cfg.http_proxy)
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-c", AK_BOOTSTRAP, today.isoformat(),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                env=process_env,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), cfg.calendar_timeout)
            if process.returncode:
                raise ValueError("AKShare calendar process failed")
            data = json.loads(stdout)
            sessions = sorted({date.fromisoformat(x) for x in data["sessions"]})
            if not sessions or sessions[-1] < today:
                raise ValueError("calendar does not cover today; update AKShare")
            self.sessions = sessions
            # Yesterday's streak is unknown without historical limit-up data.
            self.yesterday_streaks = {}
            self.streaks_ready = False
            self.loaded_for, self.error = today, None
            try:
                save_json(self.cache_path, {"sessions": [d.isoformat() for d in sessions]})
            except OSError:
                log.warning("交易日历缓存写入失败", exc_info=True)
            log.info("盘前缓存：交易日=%d 昨日连板记录=%d", len(sessions), len(self.yesterday_streaks))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.loaded_for = today
            self.error = str(exc)
            self.yesterday_streaks.clear()
            self.streaks_ready = False
            try:
                data = json.loads(self.cache_path.read_text(encoding="utf-8"))
                sessions = sorted({date.fromisoformat(x) for x in data["sessions"]})
                if sessions and sessions[-1] >= today:
                    self.sessions, self.error = sessions, None
                    log.warning("使用覆盖当日的新浪交易日历缓存")
                    return
            except (OSError, ValueError, KeyError, TypeError):
                pass
            log.warning("盘前初始化不可用：%s；实盘严格过滤关闭，模拟模式仍可运行", self.error)
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()

    def covered(self, today: date) -> bool:
        return bool(self.error is None and self.sessions and self.sessions[-1] >= today and self.loaded_for == today)

    def listing_is_mature(self, listed: date | None, today: date, minimum: int = 15) -> bool:
        if listed is None or listed > today or not self.covered(today):
            return False
        # At the 15th session (listing day inclusive) the stock is eligible.
        return bisect.bisect_right(self.sessions, today) - bisect.bisect_left(self.sessions, listed) >= minimum

    def session(self, now: datetime) -> str:
        now = now.astimezone(SHANGHAI)
        if not self.covered(now.date()):
            return "calendar_unavailable"
        if now.date() not in self.sessions:
            return "closed"
        minute = now.hour * 60 + now.minute
        if 555 <= minute < 690:
            return "morning"
        if 780 <= minute < 900:
            return "afternoon"
        return "closed"
