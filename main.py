"""Run: uvicorn main:app --host 127.0.0.1 --port 8000 --workers 1."""
from __future__ import annotations

import asyncio
import hmac
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from config import HEADERS, SHANGHAI, Settings, settings
from engine import Engine
from fetcher import Fetcher, Simulator, demo_seed, eligible, limit_price
from notifier import Notifier
from pre_market import MarketCalendar

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
# httpx request logs contain complete URLs, including a robot webhook's secret key.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger(__name__)


class Monitor:
    def __init__(self, client: httpx.AsyncClient, cfg: Settings):
        self.cfg = cfg
        self.fetcher = Fetcher(client, cfg)
        self.calendar = MarketCalendar()
        # Separate engines keep off-hours simulation out of the real intraday state.
        self.engines = {"mock": Engine(cfg), "live": Engine(cfg)}
        self.notifier = Notifier(client, cfg)
        self.clients: set[asyncio.Queue] = set()
        self.simulator: Simulator | None = None
        self.simulator_day = None
        self.mode = "initializing"
        self.session = None
        self.last_success_clock: float | None = None
        self.snapshot = {"type": "snapshot", "status": "initializing", "events": [],
                         "anomalies": [], "leaders": [], "stock_count": 0}
        self.sequence = 0
        self.calendar_task: asyncio.Task | None = None
        self.calendar_retry_at = 0.0

    def health(self) -> dict:
        return {"status": self.snapshot.get("status", "initializing"), "mode": self.mode,
                "session": self.session, "stock_count": self.snapshot.get("stock_count", 0),
                "last_success_age_seconds": round(time.monotonic() - self.last_success_clock, 2)
                if self.last_success_clock is not None else None,
                "calendar_error": self.calendar.error, "streaks_ready": self.calendar.streaks_ready,
                "websocket_clients": len(self.clients), "notifier": self.notifier.status()}

    def broadcast(self, snapshot: dict) -> None:
        self.sequence += 1
        self.snapshot = {**snapshot, "sequence": self.sequence}
        for queue in tuple(self.clients):
            # Disconnect slow consumers rather than silently losing alert events.
            if queue.full():
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(None)
                self.clients.discard(queue)
            else:
                queue.put_nowait(self.snapshot)

    async def initialize(self) -> None:
        today = datetime.now(SHANGHAI).date()
        log.info("开始盘前初始化和真实静态快照抓取（最长 %.0f 秒）；默认 MOCK_MODE=%s",
                 self.cfg.calendar_timeout, self.cfg.mock_mode)
        results = await asyncio.gather(self.calendar.load(self.cfg, today),
                                       self.fetcher.fetch_market(bootstrap=True), return_exceptions=True)
        if isinstance(results[1], Exception):
            log.warning("初始行情失败，重试一次：%s", type(results[1]).__name__)
            try:
                await self.fetcher.fetch_market(bootstrap=True)
            except (httpx.HTTPError, TimeoutError, ValueError) as exc:
                log.warning("真实行情不可用：%s；模拟模式可使用明确标记的本地虚构快照", type(exc).__name__)
        self.calendar_retry_at = time.monotonic() + 300

    def make_simulator(self, now: datetime) -> None:
        seeds = eligible(self.fetcher.last_raw, self.calendar, now.date(), self.cfg)
        source = "real_static_snapshot"
        if not seeds:
            seeds, source = demo_seed(), "synthetic_demo_snapshot"
        self.simulator = Simulator(seeds, self.cfg, source)
        self.simulator_day = now.date()
        log.warning("进入模拟：种子来源=%s 数量=%d；模拟微信=%s", source, len(seeds), self.cfg.mock_wechat)

    async def poll(self) -> None:
        try:
            await self.initialize()
            next_tick = asyncio.get_running_loop().time()
            while True:
                started = time.monotonic()
                now = datetime.now(SHANGHAI)
                self.session = self.calendar.session(now)
                if ((self.calendar.loaded_for != now.date() or self.calendar.error)
                        and started >= self.calendar_retry_at
                        and (self.calendar_task is None or self.calendar_task.done())):
                    self.calendar_retry_at = started + 300
                    self.calendar_task = asyncio.create_task(self.calendar.load(self.cfg, now.date()))
                mode = "mock" if self.cfg.mock_mode or self.session == "closed" else "live"
                if self.session == "calendar_unavailable" and not self.cfg.mock_mode:
                    mode = "blocked"
                if mode != self.mode:
                    self.engines["live"].clear_windows()
                    self.mode = mode
                try:
                    if mode == "blocked":
                        raise ValueError("calendar unavailable: live market filtering is blocked")
                    if mode == "mock":
                        if self.simulator is None or self.simulator_day != now.date():
                            self.make_simulator(now)
                        quotes, books = self.simulator.step(now)
                        source = self.simulator.source
                    else:
                        raw = await self.fetcher.fetch_market()
                        quotes = eligible(raw, self.calendar, now.date(), self.cfg)
                        # f124 is the stock's last change, not the package receipt
                        # time. Quiet/auction stocks must survive a successful fetch.
                        if not quotes:
                            raise ValueError("no eligible quotes: check calendar and listing dates")
                        book_codes = self.engines["live"].touched_codes | {
                            q.code for q in quotes if q.price >= limit_price(q.pre_close)
                            or q.high >= limit_price(q.pre_close)}
                        books = await self.fetcher.fetch_books(book_codes, datetime.now(SHANGHAI))
                        source = "eastmoney+tencent"
                    now = datetime.now(SHANGHAI)
                    engine = self.engines[mode]
                    result = engine.update(quotes, books, now, simulated=mode == "mock",
                                           yesterday_streaks=self.calendar.yesterday_streaks
                                           if self.calendar.streaks_ready and mode == "live" else None)
                    result["events"] = self.notifier.publish(result["events"])
                    elapsed = time.monotonic() - started
                    result.update({"status": "ok", "mode": mode, "source": source,
                                   "session": self.session, "cycle_ms": round(elapsed * 1000, 1),
                                   "calendar_error": self.calendar.error, "poll_seconds": self.cfg.poll_seconds})
                    self.last_success_clock = time.monotonic()
                    self.broadcast(result)
                    log.info("轮询 #%d %s 股票=%d 异动=%d 封板=%d 告警=%d 耗时=%.0fms",
                             self.sequence, mode, len(quotes), len(result["anomalies"]),
                             sum(row["is_limit_up"] for row in result["leaders"]),
                             len(result["events"]), elapsed * 1000)
                except (httpx.HTTPError, TimeoutError, ValueError) as exc:
                    # Preserve the last good payload with a visible stale flag; no fake transitions.
                    status = "blocked" if mode == "blocked" else "stale"
                    aged = self.engines[mode].anomaly_board() if mode in self.engines else []
                    self.broadcast({**self.snapshot, "status": status, "mode": mode, "events": [],
                                    "anomalies": aged,
                                    "error": str(exc) if isinstance(exc, ValueError) else type(exc).__name__,
                                    "last_attempt": now.isoformat()})
                    log.warning("本轮不可用 mode=%s error=%s；未推进行情状态机", mode, type(exc).__name__)
                next_tick += self.cfg.poll_seconds
                current = asyncio.get_running_loop().time()
                if next_tick <= current:
                    next_tick = current + self.cfg.poll_seconds
                await asyncio.sleep(max(0, next_tick - current))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("轮询任务意外退出")
            self.broadcast({**self.snapshot, "status": "failed", "events": [], "error": "polling task exited"})
        finally:
            if self.calendar_task is not None:
                self.calendar_task.cancel()
                await asyncio.gather(self.calendar_task, return_exceptions=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with httpx.AsyncClient(headers=HEADERS, proxy=settings.http_proxy, trust_env=False,
                                 timeout=settings.http_timeout,
                                 limits=httpx.Limits(max_connections=settings.max_concurrency,
                                                    max_keepalive_connections=settings.max_concurrency)) as client:
        monitor = Monitor(client, settings)
        app.state.monitor = monitor
        tasks = [asyncio.create_task(monitor.poll()), asyncio.create_task(monitor.notifier.worker())]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if not monitor.notifier.queue.empty():
                log.warning("关闭时未发送的微信事件=%d；内存模式不会跨重启恢复", monitor.notifier.queue.qsize())


app = FastAPI(title="A股主板异动与涨停盯盘", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins),
                   allow_methods=["GET", "PUT"], allow_headers=["Content-Type", "Authorization"])


def authorize(authorization: str | None) -> None:
    expected = "Bearer " + settings.api_token
    if settings.api_token and not hmac.compare_digest((authorization or "").encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="valid Bearer token required")


@app.get("/")
async def root():
    frontend = Path(__file__).resolve().parent / "frontend" / "dist" / "index.html"
    if frontend.is_file():
        from fastapi.responses import FileResponse
        return FileResponse(frontend)
    return {"message": "后端运行中；请在 frontend 执行 npm run build 构建前端", "docs": "/docs",
            "health": "/health", "snapshot": "/api/snapshot", "websocket": "/ws",
            "deployment": "single process: uvicorn main:app --workers 1"}


@app.get("/health")
async def health(request: Request):
    return request.app.state.monitor.health()


@app.get("/api/snapshot")
async def snapshot(request: Request, authorization: str | None = Header(default=None)):
    authorize(authorization)
    # REST refresh/reconnect is state synchronization, never historical sound replay.
    return {**request.app.state.monitor.snapshot, "events": []}


class Weights(BaseModel):
    model_config = ConfigDict(extra="forbid")
    speed_2m: float = Field(ge=0, le=100, allow_inf_nan=False)
    main_inflow: float = Field(ge=0, le=100, allow_inf_nan=False)
    turnover: float = Field(ge=0, le=100, allow_inf_nan=False)
    large_inflow: float = Field(ge=0, le=100, allow_inf_nan=False)
    amount: float = Field(ge=0, le=100, allow_inf_nan=False)


@app.put("/api/weights")
async def weights(body: Weights, request: Request, authorization: str | None = Header(default=None)):
    authorize(authorization)
    if request.headers.get("origin") and request.headers["origin"] not in settings.cors_origins:
        raise HTTPException(status_code=403, detail="untrusted browser origin")
    try:
        for engine in request.app.state.monitor.engines.values():
            engine.set_weights(body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"weights": request.app.state.monitor.engines["mock"].weights,
            "effective": "next successful polling cycle"}


@app.websocket("/ws")
async def websocket(ws: WebSocket):
    origin = ws.headers.get("origin")
    if origin and origin not in settings.cors_origins:
        await ws.close(code=1008)
        return
    # Browser clients authenticate in the first message, avoiding secrets in query URLs.
    queue = asyncio.Queue(maxsize=32)
    monitor = ws.app.state.monitor
    tasks: list[asyncio.Task] = []
    try:
        await ws.accept()
        if settings.api_token:
            message = await asyncio.wait_for(ws.receive_json(), timeout=5)
            if not isinstance(message, dict):
                raise ValueError("authentication object required")
            authorize("Bearer " + str(message.get("token", "")))
        monitor.clients.add(queue)
        await asyncio.wait_for(ws.send_json({**monitor.snapshot, "events": []}), timeout=3)

        async def send() -> None:
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    payload = {"type": "heartbeat", "time": datetime.now(SHANGHAI).isoformat()}
                if payload is None:
                    await ws.close(code=1013, reason="slow consumer; reconnect for current snapshot")
                    return
                await asyncio.wait_for(ws.send_json(payload), timeout=3)

        async def receive() -> None:
            while True:
                # ASGI receive notices disconnects even while polling is initializing.
                await ws.receive_text()

        tasks = [asyncio.create_task(send()), asyncio.create_task(receive())]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if not task.cancelled():
                result = task.exception()
                if result and not isinstance(result, (WebSocketDisconnect, TimeoutError)):
                    log.debug("WebSocket closed: %s", type(result).__name__)
    except asyncio.CancelledError:
        # The ASGI server/client may cancel this endpoint on shutdown. The only
        # children belong to this connection and are cancelled/joined below.
        pass
    except (WebSocketDisconnect, TimeoutError, HTTPException, ValueError):
        try:
            await ws.close(code=1008)
        except (RuntimeError, asyncio.CancelledError):
            pass
    finally:
        monitor.clients.discard(queue)
        for task in tasks:
            task.cancel()
        if tasks:
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError:
                # AnyIO can cancel again while a TestClient cancellation scope
                # exits. Children have already received cancellation.
                pass


# API routes are registered first; only static assets live below /assets.
frontend_assets = Path(__file__).resolve().parent / "frontend" / "dist" / "assets"
if frontend_assets.is_dir():
    app.mount("/assets", StaticFiles(directory=frontend_assets), name="frontend-assets")
