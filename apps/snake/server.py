"""Small ASGI static app; run with the server's existing Uvicorn installation."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FILES = {
    '/': ('index.html', 'text/html; charset=utf-8'),
    '/index.html': ('index.html', 'text/html; charset=utf-8'),
    '/style.css': ('style.css', 'text/css; charset=utf-8'),
    '/app.mjs': ('app.mjs', 'text/javascript; charset=utf-8'),
    '/engine.mjs': ('engine.mjs', 'text/javascript; charset=utf-8'),
}
PUBLIC = {url: ((ROOT / name).read_bytes(), kind) for url, (name, kind) in FILES.items()}
HEADERS = [
    (b'cache-control', b'no-cache'),
    (b'x-content-type-options', b'nosniff'),
    (b'x-frame-options', b'DENY'),
    (b'referrer-policy', b'no-referrer'),
    (b'content-security-policy', b"default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"),
]


async def app(scope, receive, send):
    if scope['type'] == 'lifespan':
        while True:
            message = await receive()
            if message['type'] == 'lifespan.startup':
                await send({'type': 'lifespan.startup.complete'})
            elif message['type'] == 'lifespan.shutdown':
                await send({'type': 'lifespan.shutdown.complete'})
                return
    if scope['type'] != 'http':
        return
    extra = []
    if scope['method'] not in ('GET', 'HEAD'):
        status, body, kind = 405, b'Method not allowed', 'text/plain; charset=utf-8'
        extra = [(b'allow', b'GET, HEAD')]
    elif scope['path'] == '/health':
        status, body, kind = 200, json.dumps({'status': 'ok', 'app': 'snake-game'}).encode(), 'application/json'
    elif scope['path'] in PUBLIC:
        status = 200
        body, kind = PUBLIC[scope['path']]
    else:
        status, body, kind = 404, b'Not found', 'text/plain; charset=utf-8'
    headers = HEADERS + extra + [(b'content-type', kind.encode()), (b'content-length', str(len(body)).encode())]
    await send({'type': 'http.response.start', 'status': status, 'headers': headers})
    await send({'type': 'http.response.body', 'body': b'' if scope['method'] == 'HEAD' else body})
