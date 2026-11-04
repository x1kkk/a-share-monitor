"""Focused regression checks: python -m unittest -v test_backend."""
from __future__ import annotations

import asyncio
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import main
from config import SHANGHAI, Settings
from engine import Engine, self_check
from fetcher import demo_seed
from pre_market import MarketCalendar


class StateChecks(unittest.TestCase):
    def test_engine_boundaries(self):
        self_check()


class LiveLoopChecks(unittest.IsolatedAsyncioTestCase):
    async def test_auction_quiet_old_quote_is_not_filtered(self):
        cfg = replace(Settings(), mock_mode=False, wechat_webhook_url="", api_token="", poll_seconds=0.01)
        monitor = main.Monitor(None, cfg)
        now = datetime(2026, 11, 4, 9, 27, tzinfo=SHANGHAI)
        # 09:25 price with no new update at 09:27; volume is still zero.
        quiet = replace(demo_seed()[0], volume=0, source_ts=(now - timedelta(minutes=2)).timestamp())
        monitor.calendar = MarketCalendar(sessions=[quiet.listed + timedelta(days=i) for i in range(20)]
                                          + [now.date()], loaded_for=now.date(), error=None)
        monitor.initialize = AsyncMock()
        monitor.fetcher.fetch_market = AsyncMock(return_value=[quiet])
        monitor.fetcher.fetch_books = AsyncMock(return_value={})
        queue = asyncio.Queue()
        monitor.clients.add(queue)
        with patch("main.datetime") as clock:
            clock.now.return_value = now
            task = asyncio.create_task(monitor.poll())
            try:
                result = await asyncio.wait_for(queue.get(), timeout=1)
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["mode"], "live")
                self.assertEqual(result["stock_count"], 1)
                self.assertEqual(result["leaders"][0]["source_ts"], quiet.source_ts)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_ws_server_cancellation_joins_children(self):
        cfg = replace(Settings(), wechat_webhook_url="", api_token="")
        monitor = main.Monitor(None, cfg)
        sent = asyncio.Event()

        class Socket:
            headers = {}
            app = SimpleNamespace(state=SimpleNamespace(monitor=monitor))

            async def accept(self):
                pass

            async def send_json(self, data):
                sent.set()

            async def receive_text(self):
                await asyncio.Event().wait()

            async def close(self, **kwargs):
                pass

        before = set(asyncio.all_tasks())
        with patch.object(main, "settings", cfg):
            task = asyncio.create_task(main.websocket(Socket()))
            await asyncio.wait_for(sent.wait(), timeout=1)
            await asyncio.sleep(0)  # permit send/receive children to start
            task.cancel()
            await asyncio.wait_for(task, timeout=1)
        await asyncio.sleep(0)
        self.assertFalse(task.cancelled())
        self.assertFalse(monitor.clients)
        self.assertFalse(set(asyncio.all_tasks()) - before)


class WebSocketChecks(unittest.TestCase):
    def test_real_asgi_disconnect_and_authenticated_weights(self):
        cfg = replace(Settings(), wechat_webhook_url="", api_token="regression-token")
        monitor = main.Monitor(None, cfg)

        @asynccontextmanager
        async def lifespan(app):
            app.state.monitor = monitor
            yield

        with patch.object(main, "settings", cfg), patch.object(main.app.router, "lifespan_context", lifespan):
            with TestClient(main.app) as client:
                for _ in range(3):
                    with client.websocket_connect("/ws", headers={"origin": "http://localhost:5173"}) as ws:
                        ws.send_json({"token": cfg.api_token})
                        snapshot = ws.receive_json()
                        self.assertEqual(snapshot["events"], [])
                        ws.send_text("ping")
                    self.assertFalse(monitor.clients)
                weights = dict(monitor.engines["live"].weights)
                self.assertEqual(client.put("/api/weights", json=weights).status_code, 401)
                headers = {"authorization": "Bearer " + cfg.api_token, "origin": "http://localhost:5173"}
                response = client.put("/api/weights", headers=headers, json={**weights, "speed_2m": 70})
                self.assertEqual(response.status_code, 200)
                self.assertAlmostEqual(sum(response.json()["weights"].values()), 100)
                self.assertEqual(client.put("/api/weights", headers=headers,
                                           json={key: 0 for key in weights}).status_code, 422)
                self.assertEqual(client.put("/api/weights", headers={**headers, "origin": "https://evil.invalid"},
                                           json=weights).status_code, 403)


if __name__ == "__main__":
    unittest.main()
