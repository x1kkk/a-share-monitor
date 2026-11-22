"""Persist only real market snapshots; derive the most recent session close."""
from datetime import datetime, time
from pathlib import Path
import json
import logging

from config import SHANGHAI
from pre_market import save_json

log = logging.getLogger(__name__)


def close_target(now, calendar):
    if not calendar.covered(now.date()):
        return None
    minute = now.hour * 60 + now.minute
    if now.date() in calendar.sessions and minute >= 900:
        day, hour, minute = now.date(), 15, 0
    elif now.date() in calendar.sessions and 690 <= minute < 780:
        day, hour, minute = now.date(), 11, 30
    else:
        prior = [day for day in calendar.sessions if day < now.date()]
        if not prior:
            return None
        day, hour, minute = prior[-1], 15, 0
    return datetime.combine(day, time(hour, minute), SHANGHAI)


def real_snapshot(value):
    if (not isinstance(value, dict) or value.get('source') != 'tencent'
            or value.get('simulated') is not False or value.get('mode') not in {'live', 'closed'}
            or not isinstance(value.get('stock_count'), int) or value['stock_count'] <= 0):
        return False
    try:
        stamp = datetime.fromisoformat(value['time'])
        if stamp.tzinfo is None:
            return False
        for key in ('leaders', 'anomalies', 'watchlist_leaders', 'watchlist_anomalies'):
            rows = value.get(key)
            if not isinstance(rows, list) or any(not isinstance(r, dict) or r.get('simulated') for r in rows):
                return False
        return bool(value['leaders'])
    except (ValueError, KeyError, TypeError):
        return False


class SnapshotStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if data.get('version') == 1 and real_snapshot(data.get('snapshot')):
                return data['snapshot']
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        log.warning('忽略无效或模拟的历史快照，不用于休市展示')
        return None

    def save(self, snapshot):
        if not real_snapshot(snapshot):
            raise ValueError('refusing to persist mock or invalid market snapshot')
        clean = {k: v for k, v in snapshot.items() if k not in {'error', 'last_attempt'}}
        clean['events'] = []
        save_json(self.path, {'version': 1, 'snapshot': clean})
        return clean
