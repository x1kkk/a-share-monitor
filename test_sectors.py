"""Offline sector cache regressions: python -m unittest test_sectors."""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import httpx

from sectors import SectorCache, catalog_rows

TODAY = date(2026, 11, 18)


def catalog(kind):
    prefix, count = ('hangye_', 84) if kind == 'industry' else ('gn_', 175)
    return {f'{prefix}{i}': f'{prefix}{i},板块{i},1' for i in range(count)}


def old_board():
    return dict(kind='industry', name='旧行业', total=1, codes=['600001'], updated_for='2026-11-17')


class SectorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'sectors.json'

    def test_catalog(self):
        for kind, count in [('industry', 84), ('concept', 175)]:
            rows = catalog_rows('var data=' + json.dumps(catalog(kind)) + ';', kind)
            self.assertEqual(len(rows), count)
            self.assertEqual(next(iter(rows.values()))['name'], '板块0')
        for text in ['{}', 'bad', '{"gn_0":"gn_1,bad,1"}', '{"bad":"bad,name,1"}']:
            with self.assertRaises(ValueError):
                catalog_rows(text, 'concept')

    async def test_missing_and_duplicate_pages(self):
        for duplicate in [False, True]:
            def handler(request):
                if request.url.path.endswith('getHQNodeStockCount'):
                    return httpx.Response(200, json='101')
                page = request.url.params['page']
                codes = [f'600{i:03d}' for i in range(100)] if page == '1' else (['600000'] if duplicate else [])
                return httpx.Response(200, json=[{'code': c} for c in codes])
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with self.assertRaises(ValueError):
                    await SectorCache(self.path)._members(client, 'hangye_0')

    async def test_failure_preserves_old_and_retry_then_restart(self):
        cache = SectorCache(self.path)
        cache.boards = {'hangye_0': old_board()}
        calls, failed = [], {'node': 'hangye_0'}

        def handler(request):
            calls.append(str(request.url))
            if request.url.path.endswith('newFLJK.php'):
                kind = 'industry' if request.url.params['param'] == 'industry' else 'concept'
                return httpx.Response(200, content=('var data=' + json.dumps(catalog(kind), ensure_ascii=False)).encode('gb18030'))
            node = request.url.params['node']
            if request.url.path.endswith('getHQNodeStockCount'):
                return httpx.Response(200, json='1')
            return httpx.Response(200, json=[] if node == failed['node'] else [{'code': '600002'}])

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await cache.refresh(client, TODAY)
            self.assertEqual(cache.boards['hangye_0'], old_board())
            self.assertIsNone(cache.completed_for)
            # The only stale existing board retries; successful boards do not.
            calls.clear()
            failed['node'] = None
            await cache.refresh(client, TODAY)
            member_calls = [c for c in calls if 'getHQNodeData' in c]
            self.assertEqual(len(member_calls), 1)
            self.assertEqual(cache.lookup('600002')['industry'] != '', True)
            calls.clear()
            restarted = SectorCache(self.path)
            self.assertEqual(restarted.index, cache.index)
            await restarted.refresh(client, TODAY)
            self.assertEqual(calls, [])

    async def test_new_failed_board_does_not_mark_complete(self):
        cache = SectorCache(self.path)
        failed = True
        def handler(request):
            if request.url.path.endswith('newFLJK.php'):
                kind = 'industry' if request.url.params['param'] == 'industry' else 'concept'
                return httpx.Response(200, content=json.dumps(catalog(kind)).encode())
            if request.url.path.endswith('getHQNodeStockCount'):
                return httpx.Response(200, json='1')
            return httpx.Response(200, json=[] if failed and request.url.params['node'] == 'gn_0' else [{'code': '600001'}])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await cache.refresh(client, TODAY)
            self.assertNotIn('gn_0', cache.boards)
            restarted = SectorCache(self.path)
            failed = False
            await restarted.refresh(client, TODAY)
            self.assertIn('gn_0', restarted.boards)
            self.assertEqual(restarted.completed_for, TODAY.isoformat())

    async def test_failed_catalog_retries_with_current_boards(self):
        cache = SectorCache(self.path)
        cache.boards = {'hangye_0': {**old_board(), 'updated_for': TODAY.isoformat()}}
        calls = []
        def handler(request):
            calls.append(request.url.params['param'])
            # Invalid catalog data avoids HTTP retry sleeps.
            return httpx.Response(200, content=b'{}')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await cache.refresh(client, TODAY)
            await cache.refresh(client, TODAY)
        self.assertEqual(len(calls), 4)
        self.assertIsNone(cache.completed_for)
        self.assertIn('hangye_0', cache.boards)

    async def test_atomic_write_failure_keeps_memory_and_disk(self):
        original = {'version': 1, 'boards': {'hangye_0': old_board()}}
        self.path.write_text(json.dumps(original), encoding='utf-8')
        cache = SectorCache(self.path)
        async def unavailable(*args):
            raise ValueError('catalog unavailable')
        with patch.object(cache, '_catalog', unavailable), patch('pre_market.os.replace', side_effect=OSError('disk error')):
            with self.assertRaises(OSError):
                await cache.refresh(None, TODAY)
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8')), original)
        self.assertEqual(cache.boards, original['boards'])
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_invalid_cache_does_not_crash(self):
        for data in [[], {'version': 1, 'boards': {'hangye_0': {**old_board(), 'codes': [{}]}}}]:
            self.path.write_text(json.dumps(data), encoding='utf-8')
            self.assertEqual(SectorCache(self.path).boards, {})


if __name__ == '__main__':
    unittest.main()
