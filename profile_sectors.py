"""Daily, independently refreshed company-page sector overrides."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path

import httpx

from config import HEADERS, SHANGHAI
from pre_market import save_json

log = logging.getLogger(__name__)
PROFILE = 'https://vip.stock.finance.sina.com.cn/corp/go.php/vCI_CorpOtherInfo/stockid/{}.phtml'


class _Tables(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables, self.rows, self.cells, self.cell = [], None, [], None

    def handle_starttag(self, tag, attrs):
        if tag == 'table' and 'comInfo1' in dict(attrs).get('class', '').split():
            self.rows = []
        if self.rows is not None:
            if tag == 'tr':
                self.cells = []
            elif tag in ('td', 'th'):
                self.cell = ''

    def handle_data(self, data):
        if self.cell is not None:
            self.cell += data

    def handle_endtag(self, tag):
        if self.rows is None:
            return
        if tag in ('td', 'th') and self.cell is not None:
            self.cells.append(self.cell.strip())
            self.cell = None
        elif tag == 'tr':
            self.rows.append(self.cells)
        elif tag == 'table':
            self.tables.append(self.rows)
            self.rows, self.cell = None, None


def profile_rows(text):
    parser = _Tables()
    parser.feed(text)
    sections = {}
    for rows in parser.tables:
        if not rows or not rows[0]:
            continue
        kind = {'所属行业板块': 'industry', '所属概念板块': 'concept'}.get(rows[0][0])
        if kind is None:
            continue
        if kind in sections:
            raise ValueError('duplicate company sector table')
        names = [r[0] for r in rows[1:] if len(r) == 2 and r[1] == '点击查看']
        if any(not 0 < len(n) <= 80 or any(c in n for c in ',|\r\n') for n in names):
            raise ValueError('invalid company sector name')
        sections[kind] = sorted(set(names))
    if set(sections) != {'industry', 'concept'} or len(sections['industry']) != 1:
        raise ValueError('incomplete company sector page')
    return {'industry': sections['industry'][0], 'raw_tags': ','.join(sections['concept'])}


class CompanySectors:
    def __init__(self, directory: Path, on_change):
        self.path = directory / 'company_sectors.json'
        self.universe_path = directory / 'universe_cache.json'
        self.on_change = on_change
        self.profiles = {}
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('profiles'), dict):
                raise ValueError('invalid company sectors cache')
            for code, row in data['profiles'].items():
                if not re.fullmatch(r'(?:00|60)[0-9]{4}', code) or not isinstance(row, dict):
                    raise ValueError('invalid company sector code')
                date.fromisoformat(row['updated_for'])
                if (not isinstance(row.get('industry'), str) or not 0 < len(row['industry']) <= 80
                        or not isinstance(row.get('raw_tags'), str) or len(row['raw_tags']) > 20000):
                    raise ValueError('invalid company sectors')
            self.profiles = data['profiles']
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError):
            log.warning('公司资料缓存无效，等待后台更新')

    async def refresh(self, client, today):
        data = json.loads(self.universe_path.read_text(encoding='utf-8'))
        stocks = data['stocks']
        if not isinstance(stocks, list):
            raise ValueError('invalid universe cache')
        codes = sorted({r['code'] for r in stocks if isinstance(r, dict)
                        and isinstance(r.get('code'), str) and re.fullmatch(r'(?:00|60)[0-9]{4}', r['code'])})
        stamp = today.isoformat()
        pending = [c for c in codes if self.profiles.get(c, {}).get('updated_for') != stamp]

        async def fetch(code):
            response = await client.get(PROFILE.format(code), timeout=8)
            response.raise_for_status()
            return {**profile_rows(response.content.decode('gb18030')), 'updated_for': stamp}

        # Four independent requests at a time; checkpoint each completed batch.
        for offset in range(0, len(pending), 4):
            batch = pending[offset:offset + 4]
            results = await asyncio.gather(*(fetch(c) for c in batch), return_exceptions=True)
            updated = dict(self.profiles)
            for code, row in zip(batch, results):
                if not isinstance(row, Exception):
                    updated[code] = row
            if updated != self.profiles:
                save_json(self.path, {'version': 1, 'profiles': updated})
                self.profiles = updated
                self.on_change()

    async def run(self):
        async with httpx.AsyncClient(trust_env=False, headers={**HEADERS, 'Referer': 'https://finance.sina.com.cn/'},
                                     limits=httpx.Limits(max_connections=4, max_keepalive_connections=4)) as client:
            while True:
                try:
                    await self.refresh(client, datetime.now(SHANGHAI).date())
                except (httpx.HTTPError, OSError, ValueError, TypeError, KeyError):
                    log.warning('公司资料后台更新失败，保留旧缓存', exc_info=True)
                await asyncio.sleep(3600)
