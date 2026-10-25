"""Read the independent collector heartbeat without starting a feed in HTTP workers."""
import json
import math
from pathlib import Path
import time

from flask import jsonify

MAX_STATUS_BYTES = 64 * 1024
STATUS_FIELDS = {
    'version', 'collector_id', 'market', 'state', 'price_unit', 'quantity_unit',
    'sample_interval_ms', 'max_quote_age_ms', 'heartbeat_ms', 'started_at_ms',
    'uptime_seconds', 'last_sample_ms', 'last_flush_ms', 'rows_committed',
    'rows_flushed', 'pending_rows', 'missed_slots', 'errors', 'last_error',
}
EXCHANGE_FIELDS = {
    'state', 'connected', 'last_quote', 'last_sample_ms', 'rows_committed',
    'quotes_received', 'skipped_samples', 'skip_reasons', 'quote_age_ms',
}


def read_status(directory, now_ms=None):
    fallback = {'collector_id': 'usdt', 'market': 'KRW-USDT', 'available': False,
                'active': False, 'state': 'not_started', 'exchanges': {}}
    try:
        with (Path(directory) / 'status.json').open('rb') as stream:
            raw = stream.read(MAX_STATUS_BYTES + 1)
        if len(raw) > MAX_STATUS_BYTES:
            raise ValueError('Oversized status')
        status = json.loads(raw)
        if not isinstance(status, dict):
            raise ValueError('Invalid status')
        heartbeat = status.get('heartbeat_ms')
        if isinstance(heartbeat, bool) or not isinstance(heartbeat, (int, float)) or not math.isfinite(heartbeat) or heartbeat <= 0:
            raise ValueError('Invalid heartbeat')
        age = (time.time() * 1000 if now_ms is None else now_ms) - heartbeat
        result = {key: value for key, value in status.items() if key in STATUS_FIELDS}
        result.update(available=True, active=status.get('state') == 'running' and 0 <= age <= 15_000,
                      heartbeat_age_ms=round(age), exchanges={})
        exchanges = status.get('exchanges', {})
        if isinstance(exchanges, dict):
            for name in ('bithumb', 'upbit'):
                record = exchanges.get(name)
                if isinstance(record, dict):
                    result['exchanges'][name] = {key: value for key, value in record.items() if key in EXCHANGE_FIELDS}
        if status.get('state') == 'running' and not result['active']:
            result['state'] = 'stale'
        return result
    except FileNotFoundError:
        return fallback
    except (OSError, ValueError, UnicodeError):
        return {**fallback, 'state': 'unavailable'}


def register(app):
    @app.get('/api/collectors/usdt')
    def usdt_collector_status():
        return jsonify(read_status(Path(app.config['STATE_DIR']) / 'collectors' / 'usdt'))
