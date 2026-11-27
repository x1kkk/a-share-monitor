import asyncio
import unittest
from server import app


class ServerCheck(unittest.IsolatedAsyncioTestCase):
    async def test_public_files_health_head_and_private_paths(self):
        for path, method, expected in [('/', 'GET', 200), ('/app.mjs', 'GET', 200),
                ('/engine.mjs', 'GET', 200), ('/style.css', 'GET', 200),
                ('/health', 'GET', 200), ('/', 'HEAD', 200),
                ('/', 'POST', 405), ('/server.py', 'GET', 404),
                ('/engine.test.mjs', 'GET', 404), ('/../README.md', 'GET', 404)]:
            sent = []
            async def send(message):
                sent.append(message)
            await app({'type': 'http', 'path': path, 'method': method}, None, send)
            self.assertEqual(sent[0]['status'], expected, (path, method))
            headers = dict(sent[0]['headers'])
            self.assertEqual(headers[b'x-content-type-options'], b'nosniff')
            if path.endswith('.mjs') and expected == 200:
                self.assertIn(b'javascript', headers[b'content-type'])
            if method == 'HEAD':
                self.assertEqual(sent[1]['body'], b'')
            if path == '/health':
                self.assertIn(b'"app": "snake-game"', sent[1]['body'])


if __name__ == '__main__':
    unittest.main()
