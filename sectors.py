"""Low-frequency industry/concept membership cache, independent of live quotes."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from datetime import date, datetime
from pathlib import Path

import httpx

from config import HEADERS, SHANGHAI
from pre_market import save_json

log = logging.getLogger(__name__)
BASE = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center."
CATALOG = "https://money.finance.sina.com.cn/q/view/newFLJK.php"


def catalog_rows(text: str, kind: str) -> dict[str, dict]:
    if kind not in {"industry", "concept"}:
        raise ValueError("invalid sector kind")
    try:
        data = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid sector catalog") from exc
    if not isinstance(data, dict) or not data or len(data) > 1000:
        raise ValueError("empty or unexpected sector catalog")
    prefix = "hangye_" if kind == "industry" else "gn_"
    result = {}
    for node, value in data.items():
        if not isinstance(value, str) or not re.fullmatch(prefix + r"[A-Za-z0-9_]+", node):
            raise ValueError("invalid sector identifier")
        fields = value.split(",")
        if len(fields) < 3 or fields[0] != node or not 0 < len(fields[1].strip()) <= 80:
            raise ValueError("invalid sector name")
        result[node] = {"kind": kind, "name": fields[1].strip()}
    return result


def validate_board(board: dict) -> None:
    if not isinstance(board, dict) or board.get("kind") not in {"industry", "concept"}:
        raise ValueError("invalid cached sector")
    if not isinstance(board.get("name"), str) or not 0 < len(board["name"]) <= 80:
        raise ValueError("invalid cached sector name")
    date.fromisoformat(board["updated_for"])
    codes = board["codes"]
    if (not isinstance(codes, list) or any(not isinstance(c, str) for c in codes) or len(codes) != len(set(codes))
            or any(not isinstance(c, str) or not re.fullmatch(r"(?:00|60)\d{4}", c) for c in codes)
            or type(board.get("total")) is not int or not len(codes) <= board["total"] <= 10000):
        raise ValueError("invalid cached sector members")


class SectorCache:
    def __init__(self, path: Path | None = None):
        self.path = path or Path(__file__).resolve().parent / "sector_cache.json"
        self.boards: dict[str, dict] = {}
        self.index: dict[str, dict[str, str]] = {}
        self.completed_for: str | None = None
        self.semaphore = asyncio.Semaphore(4)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("boards"), dict):
                raise ValueError("invalid sector cache")
            for node, board in data["boards"].items():
                validate_board(board)
                prefix = "hangye_" if board["kind"] == "industry" else "gn_"
                if not re.fullmatch(prefix + r"[A-Za-z0-9_]+", node):
                    raise ValueError("invalid cached sector identifier")
            completed = data.get("completed_for")
            if completed is not None:
                date.fromisoformat(completed)
                if not data["boards"] or any(b["updated_for"] != completed for b in data["boards"].values()):
                    raise ValueError("invalid sector completion date")
            self.boards = data["boards"]
            self.completed_for = completed
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError):
            log.warning("行业/概念缓存无效，等待后台补全，不影响行情")
        self._reindex()

    def _reindex(self) -> None:
        index, tags = {}, {}
        for node, board in sorted(self.boards.items()):
            for code in board["codes"]:
                item = index.setdefault(code, {"industry": "", "raw_tags": ""})
                if board["kind"] == "industry":
                    item["industry"] = board["name"]
                else:
                    tags.setdefault(code, set()).add(board["name"])
        for code, names in tags.items():
            index[code]["raw_tags"] = ",".join(sorted(names))
        self.index = index

    def lookup(self, code: str) -> dict[str, str]:
        return self.index.get(code, {"industry": "", "raw_tags": ""})

    async def _get(self, client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
        for attempt in range(3):
            try:
                async with self.semaphore:
                    response = await client.get(url, timeout=5.0, **kwargs)
                    response.raise_for_status()
                return response
            except httpx.HTTPError:
                if attempt == 2:
                    raise
                await asyncio.sleep(0.2 * 2 ** attempt)

    async def _catalog(self, client: httpx.AsyncClient, kind: str) -> dict[str, dict]:
        response = await self._get(client, CATALOG, params={"param": "industry" if kind == "industry" else "class"})
        rows = catalog_rows(response.content.decode("gb18030"), kind)
        old_count = sum(b["kind"] == kind for b in self.boards.values())
        if len(rows) < max(40 if kind == "industry" else 80, old_count * 0.8):
            raise ValueError("truncated sector catalog")
        return rows

    async def _members(self, client: httpx.AsyncClient, node: str) -> tuple[int, list[str]]:
        count = (await self._get(client, BASE + "getHQNodeStockCount", params={"node": node})).json()
        if not re.fullmatch(r"[0-9]+", str(count)):
            raise ValueError("invalid sector size")
        total = int(count)
        if not 1 <= total <= 10000:
            raise ValueError("empty or unexpected sector size")
        old = self.boards.get(node)
        if old and total < old["total"] * 0.8:
            raise ValueError("unexpectedly truncated sector")
        codes = []
        for page in range(1, math.ceil(total / 100) + 1):
            rows = (await self._get(client, BASE + "getHQNodeData", params={
                "node": node, "page": page, "num": 100, "sort": "symbol", "asc": 1})).json()
            if (not isinstance(rows, list) or len(rows) != min(100, total - (page - 1) * 100)
                    or any(not isinstance(r, dict) for r in rows)):
                raise ValueError("invalid sector members")
            for row in rows:
                code = row.get("code")
                if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code):
                    raise ValueError("invalid sector stock code")
                codes.append(code)
        if len(codes) != total or len(set(codes)) != total:
            raise ValueError("incomplete or duplicated sector pages")
        return total, sorted(c for c in codes if c.startswith(("00", "60")))

    async def refresh(self, client: httpx.AsyncClient, today: date) -> None:
        stamp = today.isoformat()
        # A complete same-day refresh does no network work on hourly wakeups.
        if self.completed_for == stamp:
            return
        catalogs = await asyncio.gather(*(self._catalog(client, kind) for kind in ("industry", "concept")),
                                        return_exceptions=True)
        boards = dict(self.boards)
        pending = {}
        for kind, catalog in zip(("industry", "concept"), catalogs):
            if isinstance(catalog, Exception):
                log.warning("%s 板块目录不可用，保留上次缓存", kind)
                continue
            boards = {n: b for n, b in boards.items() if b["kind"] != kind or n in catalog}
            pending.update({n: b for n, b in catalog.items() if boards.get(n, {}).get("updated_for") != stamp})

        async def board(node, meta):
            total, codes = await self._members(client, node)
            return {**meta, "total": total, "codes": codes, "updated_for": stamp}

        results = await asyncio.gather(*(board(n, b) for n, b in pending.items()), return_exceptions=True)
        failed = 0
        for node, result in zip(pending, results):
            if isinstance(result, Exception):
                failed += 1
            else:
                boards[node] = result
        if not boards:
            raise ValueError("no sector metadata available")
        completed = stamp if failed == 0 and all(isinstance(c, dict) for c in catalogs) else None
        save_json(self.path, {"version": 1, "source": "sina_csrc_and_concept",
                              "completed_for": completed, "boards": boards})
        self.completed_for = completed
        self.boards = boards
        self._reindex()
        log.info("行业/概念缓存：板块=%d 行业覆盖=%d 概念覆盖=%d 本次失败=%d",
                 len(boards), sum(bool(v["industry"]) for v in self.index.values()),
                 sum(bool(v["raw_tags"]) for v in self.index.values()), failed)

    async def run(self) -> None:
        async with httpx.AsyncClient(trust_env=False, headers={**HEADERS, "Referer": "https://finance.sina.com.cn/"},
                                     limits=httpx.Limits(max_connections=4, max_keepalive_connections=4)) as client:
            while True:
                try:
                    async with asyncio.timeout(180):
                        await self.refresh(client, datetime.now(SHANGHAI).date())
                except (httpx.HTTPError, TimeoutError, ValueError, OSError):
                    log.warning("行业/概念后台更新失败，继续使用缓存，不影响腾讯行情", exc_info=True)
                await asyncio.sleep(3600)
