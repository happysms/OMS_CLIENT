"""Collector liveness reports stale heartbeats instead of a stale running state."""
import json
from pathlib import Path
import tempfile
import unittest

from oms_client.api.routes.collectors import MAX_STATUS_BYTES, read_status


class CollectorStatusTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def write(self, **values):
        status = {'heartbeat_ms': 100_000, 'state': 'running', 'collector_id': 'usdt',
                  'data_root': '/private/path', 'state_dir': '/private/state',
                  'exchanges': {'bithumb': {'connected': True, 'rows_committed': 3, 'private_key': 'hidden'}}}
        status.update(values)
        (self.directory / 'status.json').write_text(json.dumps(status))

    def test_live_snapshot_and_paths_are_filtered(self):
        self.write()
        result = read_status(self.directory, now_ms=105_000)
        self.assertTrue(result['active'])
        self.assertTrue(result['available'])
        self.assertEqual(result['heartbeat_age_ms'], 5000)
        self.assertNotIn('data_root', result)
        self.assertNotIn('state_dir', result)
        self.assertNotIn('private_key', result['exchanges']['bithumb'])

    def test_old_future_and_stopped_heartbeats_are_inactive(self):
        for now, state, expected in [(116_000, 'running', 'stale'), (99_000, 'running', 'stale'), (105_000, 'stopped', 'stopped')]:
            with self.subTest(now=now, state=state):
                self.write(state=state)
                result = read_status(self.directory, now_ms=now)
                self.assertFalse(result['active'])
                self.assertEqual(result['state'], expected)

    def test_missing_corrupt_and_oversized_status_do_not_break_dashboard(self):
        self.assertEqual(read_status(self.directory)['state'], 'not_started')
        for raw in ('{', '[]', '{"heartbeat_ms":true}', '{"heartbeat_ms":NaN}', 'x' * (MAX_STATUS_BYTES + 1)):
            with self.subTest(raw=raw[:30]):
                (self.directory / 'status.json').write_text(raw)
                result = read_status(self.directory)
                self.assertFalse(result['available'])
                self.assertEqual(result['state'], 'unavailable')
