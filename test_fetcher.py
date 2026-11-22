"""Transport recovery and complete-snapshot checks: python -m unittest test_fetcher."""
import asyncio
import json
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, call, patch

import httpx

from config import Settings
from fetcher import Fetcher, demo_seed


def row(code):
    return {"f12": code, "f14": "sample", "f2": 10, "f18": 10,
            "f26": "20100101", **{k: 1 for k in
            ("f3", "f5", "f6", "f8", "f21", "f62", "f66", "f72")}}


ROWS = [row("600000"), row("600001"), row("600002")]


class FetcherChecks(unittest.IsolatedAsyncioTestCase):
    def settings(self, **kwargs):
        return replace(Settings(), http_proxy=None, wechat_webhook_url="", page_size=2, **kwargs)

    async def test_full_large_page_is_one_request(self):
        requests = []

        def response(request):
            requests.append(request)
            return httpx.Response(200, json={"data": {"total": 3, "diff": ROWS}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            fetcher = Fetcher(client, self.settings())
            self.assertEqual(len(await fetcher._fetch_eastmoney()), 3)
            self.assertEqual(fetcher.last_total, 3)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.params["pz"], "3500")
        self.assertEqual(requests[0].headers["Connection"], "close")

    async def test_capped_large_page_reuses_first_page(self):
        requests = []

        def response(request):
            requests.append((request.url.params["pn"], request.url.params["pz"]))
            batch = ROWS[:2] if request.url.params["pn"] == "1" else ROWS[2:]
            return httpx.Response(200, json={"data": {"total": 3, "diff": batch}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            fetcher = Fetcher(client, self.settings())
            self.assertEqual(len(await fetcher._fetch_eastmoney()), 3)
            self.assertFalse(fetcher._large_page_supported)
            self.assertEqual(len(await fetcher._fetch_eastmoney()), 3)
        self.assertEqual(requests, [("1", "3500"), ("2", "2"), ("1", "2"), ("2", "2")])

    async def test_failed_large_page_falls_back(self):
        requests = []

        def response(request):
            requests.append(request.url.params["pz"])
            if requests[-1] == "3500":
                return httpx.Response(503)
            batch = ROWS[:2] if request.url.params["pn"] == "1" else ROWS[2:]
            return httpx.Response(200, json={"data": {"total": 3, "diff": batch}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            self.assertEqual(len(await Fetcher(client, self.settings())._fetch_eastmoney()), 3)
        self.assertEqual(requests, ["3500", "2", "2"])

    async def test_transport_errors_retry_with_bounded_backoff(self):
        errors = [httpx.RemoteProtocolError, httpx.ConnectError, httpx.ReadTimeout]
        requests = []

        def response(request):
            requests.append(request)
            if errors:
                raise errors.pop(0)("injected disconnect", request=request)
            return httpx.Response(200, json={"data": {"total": 1, "diff": ROWS[:1]}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            with patch("fetcher.asyncio.sleep", new_callable=AsyncMock) as sleep:
                self.assertEqual((await Fetcher(client, self.settings())._page(1))[0], 1)
                self.assertEqual(sleep.await_args_list, [call(0.1), call(0.2), call(0.3)])
        self.assertEqual(len(requests), 4)
        self.assertTrue(all(r.headers["Connection"] == "close" for r in requests))

    async def test_exhausted_page_preserves_last_complete_snapshot(self):
        second_page_attempts = 0

        def response(request):
            nonlocal second_page_attempts
            if request.url.params["pn"] == "2":
                second_page_attempts += 1
                raise httpx.RemoteProtocolError("injected disconnect", request=request)
            return httpx.Response(200, json={"data": {"total": 3, "diff": ROWS[:2]}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            fetcher = Fetcher(client, self.settings())
            old = demo_seed()
            fetcher.last_raw, fetcher.last_total = old, 3
            with patch("fetcher.asyncio.sleep", new_callable=AsyncMock):
                with self.assertRaises(httpx.RemoteProtocolError):
                    await fetcher._fetch_eastmoney()
            self.assertIs(fetcher.last_raw, old)
            self.assertEqual(fetcher.last_total, 3)
        self.assertEqual(second_page_attempts, 4)

    async def test_duplicate_missing_and_changed_pages_are_rejected(self):
        for total, batch in ((3, ROWS[:1]), (3, []), (4, ROWS[2:])):
            with self.subTest(total=total, batch=batch):
                def response(request):
                    data = {"total": 3, "diff": ROWS[:2]} if request.url.params["pn"] == "1" else {
                        "total": total, "diff": batch}
                    return httpx.Response(200, json={"data": data})

                async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
                    fetcher = Fetcher(client, self.settings())
                    old = demo_seed()
                    fetcher.last_raw = old
                    with self.assertRaises(ValueError):
                        await fetcher._fetch_eastmoney()
                    self.assertIs(fetcher.last_raw, old)

    async def test_deadline_cancels_pending_pages(self):
        active = 0

        async def response(request):
            nonlocal active
            if request.url.params["pn"] == "1":
                return httpx.Response(200, json={"data": {"total": 3, "diff": ROWS[:2]}})
            active += 1
            try:
                await asyncio.Event().wait()
            finally:
                active -= 1

        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            fetcher = Fetcher(client, self.settings(eastmoney_deadline=0.03))
            with self.assertRaises(TimeoutError):
                await fetcher._fetch_eastmoney()
            self.assertEqual(active, 0)
            self.assertEqual(fetcher.last_raw, [])

    async def test_protocol_error_retries_on_backup_tunnel(self):
        def disconnected(request):
            raise httpx.RemoteProtocolError("injected tunnel rotation", request=request)

        def response(request):
            return httpx.Response(200, json={"data": {"total": 1, "diff": ROWS[:1]}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(disconnected)) as primary:
            backup = httpx.AsyncClient(transport=httpx.MockTransport(response))
            cfg = self.settings(http_proxy_backup="http://backup.invalid:15818")
            with patch("fetcher.httpx.AsyncClient", return_value=backup) as constructor:
                self.assertEqual((await Fetcher(primary, cfg)._page(1))[0], 1)
                self.assertEqual(constructor.call_args.kwargs["proxy"], cfg.http_proxy_backup)
            self.assertTrue(backup.is_closed)
            self.assertFalse(primary.is_closed)

    async def test_real_socket_disconnect_recovers_on_new_connection(self):
        headers, connections = [], 0

        async def serve(reader, writer):
            nonlocal connections
            connections += 1
            number = connections
            try:
                headers.append(await reader.readuntil(b"\r\n\r\n"))
                if number == 1:
                    return  # EOF before response headers reproduces RemoteProtocolError.
                body = json.dumps({"data": {"total": 1, "diff": ROWS[:1]}}).encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode()
                             + b"\r\n\r\n" + body)
                await writer.drain()
                await reader.read()  # Client must close even if server permits keep-alive.
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        async with server:
            port = server.sockets[0].getsockname()[1]
            cfg = self.settings(eastmoney_url=f"http://127.0.0.1:{port}")
            async with httpx.AsyncClient(trust_env=False, limits=httpx.Limits(
                    max_connections=12, max_keepalive_connections=0)) as client:
                fetcher = Fetcher(client, cfg)
                self.assertEqual((await fetcher._page(1))[0], 1)
                self.assertEqual((await fetcher._page(1))[0], 1)
        self.assertEqual(connections, 3)
        self.assertTrue(all(b"connection: close" in h.lower() for h in headers))


if __name__ == "__main__":
    unittest.main()
