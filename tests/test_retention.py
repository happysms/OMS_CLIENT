"""Retention boundaries: age, scope, links, active readers, persistence and HTTP auth."""

from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from oms_client.storage.access import DataAccessGate
from oms_client.storage.retention import MarketFileRetention, RetentionPolicy, RetentionService
from oms_client.market_data.validation import DataError

NOW = datetime(2026, 10, 27, tzinfo=timezone.utc).timestamp()


class RetentionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.roots = {name: self.base / name for name in ('legacy', 'collector')}
        for root in self.roots.values():
            root.mkdir()
        self.gate = DataAccessGate()
        self.service = RetentionService(
            self.base / 'state', self.roots, autostart=False, gate=self.gate, clock=lambda: NOW
        )
        self.addCleanup(self.service.close)

    def file(
        self, scope='legacy', kind='ob', stamp='2026/10/01/00.parquet', age=30 * 86400, ticker='BTC'
    ):
        path = self.roots[scope] / 'bithumb' / kind / ticker / stamp
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'fixture')
        os.utime(path, (NOW - age, NOW - age))
        return path

    def enable(self, **changes):
        policy = dict(enabled=True, days=7, scopes=['legacy', 'collector'])
        policy.update(changes)
        self.service.save_policy(policy)
        return self.service.policy

    def test_disabled_default_and_preview_never_delete(self):
        old = self.file()
        self.service.run_once()
        self.assertTrue(old.exists())
        self.assertEqual(self.service.last['deferred'], 'disabled')
        self.service.run_once(preview=True)
        self.assertEqual(self.service.last['count'], 1)
        self.assertEqual(self.service.last['bytes'], old.stat().st_size)
        self.assertTrue(old.exists())
        self.assertFalse(self.service.policy.enabled)

    def test_only_declared_old_files_with_old_mtime_are_removed(self):
        old = self.file()
        compressed = self.file(kind='tick', stamp='2026/10/01/01.parquet.gz')
        recent_write = self.file(stamp='2026/10/01/02.parquet', age=30)
        recent_data = self.file(stamp='2026/10/26/00.parquet')
        wrong_type = self.file(kind='private', stamp='2026/10/01/00.sqlite3')
        daily = self.file('collector', 'candles_1m', '2026/10/01.parquet')
        self.enable()
        self.service.run_once()
        self.assertEqual(self.service.last['deleted'], 3)
        for path in (old, compressed, daily):
            self.assertFalse(path.exists())
        for path in (recent_write, recent_data, wrong_type):
            self.assertTrue(path.exists())

    def test_selected_scope_and_protected_state_are_preserved(self):
        legacy = self.file()
        collector = self.file('collector', 'ob_1s')
        self.enable(scopes=['legacy'])
        self.service.run_once()
        self.assertFalse(legacy.exists())
        self.assertTrue(collector.exists())
        self.assertFalse((self.roots['collector'] / '.usdt-orderbook.lock').exists())
        protected = self.file(ticker='ACCOUNT')
        scanner = MarketFileRetention(self.roots, protected=(protected.parent,))
        candidates, _ = scanner.scan(self.service.policy, NOW)
        self.assertFalse(candidates)

    def test_links_and_symlinked_directories_are_not_candidates(self):
        outside = self.base / 'outside'
        outside.write_bytes(b'keep')
        linked = self.file()
        linked.unlink()
        linked.symlink_to(outside)
        hard = self.file(ticker='HARD')
        os.link(hard, self.base / 'alias')
        tree = self.roots['legacy'] / 'okx'
        tree.symlink_to(self.roots['collector'], target_is_directory=True)
        self.enable()
        self.service.run_once()
        self.assertEqual(self.service.last['deleted'], 0)
        self.assertEqual(outside.read_bytes(), b'keep')
        self.assertTrue(hard.exists())

    def test_changed_file_and_replaced_parent_are_rechecked_before_unlink(self):
        old = self.file()
        policy = self.enable()
        candidates, _ = self.service.scanner.scan(policy, NOW)
        old.write_bytes(b'recent replacement')
        self.assertFalse(self.service.scanner.delete(candidates[0]))
        self.assertTrue(old.exists())
        os.utime(old, (NOW - 30 * 86400, NOW - 30 * 86400))
        candidates, _ = self.service.scanner.scan(policy, NOW)
        parent = old.parent
        moved = parent.with_name('moved')
        parent.rename(moved)
        parent.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(OSError):
            self.service.scanner.delete(candidates[0])
        self.assertTrue((moved / old.name).exists())

    def test_active_reader_defers_cleanup_without_interrupting_it(self):
        old = self.file()
        self.enable()
        with self.gate.read():
            self.service.run_once()
            self.assertEqual(self.service.last['deferred'], 'data_in_use')
            self.assertTrue(old.exists())
        self.service.run_once()
        self.assertFalse(old.exists())

    def test_collector_lock_skips_its_files_but_not_other_data(self):
        legacy = self.file()
        collected = self.file('collector', 'ob_1s')
        self.enable()
        with (self.roots['collector'] / '.usdt-orderbook.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.service.run_once()
            self.assertTrue(collected.exists())
            self.assertFalse(legacy.exists())
            self.assertEqual(self.service.last['deferred'], 'collector_in_use')
        self.service.run_once()
        self.assertFalse(collected.exists())

    def test_scan_is_bounded_and_scope_validation_is_strict(self):
        for hour in range(5):
            self.file(stamp=f'2026/10/01/{hour:02d}.parquet')
        scanner = MarketFileRetention(self.roots, max_candidates=2)
        candidates, report = scanner.scan(self.service.policy, NOW)
        self.assertEqual(len(candidates), 2)
        self.assertTrue(report['truncated'])
        for changes in (
            {'days': True},
            {'days': 0},
            {'enabled': 'true'},
            {'scopes': ['/tmp']},
            {'scopes': []},
            {'scopes': ['legacy', 'legacy']},
            {'path': '/'},
        ):
            with self.subTest(changes=changes), self.assertRaises(DataError):
                RetentionPolicy.parse(
                    dict({'enabled': False, 'days': 7, 'scopes': ['legacy']}, **changes)
                )

    def test_bounded_scans_resume_past_recent_files_without_starving_old_data(self):
        expected = set()
        for hour in range(10):
            path = self.file(
                stamp=f'2026/10/01/{hour:02d}.parquet', age=30 if hour < 5 else 30 * 86400
            )
            if hour >= 5:
                expected.add(str(path.relative_to(self.roots['legacy'])))
        scanner = MarketFileRetention(self.roots, max_entries=2, max_candidates=1)
        found = set()
        try:
            for _ in range(30):
                candidates, report = scanner.scan(self.service.policy, NOW, resume=True)
                found.update(candidate.relative for candidate in candidates)
                if not report['truncated']:
                    break
            self.assertEqual(found, expected)
            self.assertIsNone(scanner._cursor)
        finally:
            scanner.close()

    def test_policy_persists_and_corruption_disables_deletion(self):
        self.enable(days=30, scopes=['collector'])
        second = RetentionService(self.service.directory, self.roots, autostart=False)
        self.addCleanup(second.close)
        self.assertEqual(second.policy, self.service.policy)
        second.path.write_text('{broken')
        third = RetentionService(self.service.directory, self.roots, autostart=False)
        self.addCleanup(third.close)
        self.assertFalse(third.policy.enabled)
        self.assertIsNotNone(third.status()['error'])

    def test_background_worker_applies_saved_enabled_policy(self):
        old = self.file()
        self.enable(scopes=['legacy'])
        worker = RetentionService(
            self.service.directory, self.roots, gate=self.gate, clock=lambda: NOW
        )
        try:
            deadline = time.monotonic() + 2
            while old.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(old.exists())
        finally:
            worker.close()
        self.assertFalse(worker.thread.is_alive())

    def test_other_cleanup_process_lock_defers_deletion(self):
        old = self.file()
        self.enable()
        with (self.service.directory / 'retention.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.service.run_once()
            self.assertEqual(self.service.last['deferred'], 'another_cleanup')
            self.assertTrue(old.exists())

    def test_turning_off_during_scan_prevents_deletion(self):
        old = self.file()
        self.enable()
        original = self.service.scanner.scan

        def change_settings(*args, **kwargs):
            result = original(*args, **kwargs)
            self.service.save_policy(dict(enabled=False, days=7, scopes=['legacy']))
            return result

        with patch.object(self.service.scanner, 'scan', side_effect=change_settings):
            self.service.run_once()
        self.assertTrue(old.exists())
        self.assertEqual(self.service.last['deferred'], 'settings_changed')

    def test_disk_metrics_include_missing_directory_and_groupable_device(self):
        self.service.scanner.roots['collector'] = self.base / 'not-created'
        disks = self.service.status()['disks']
        missing = next(d for d in disks if d['scope'] == 'collector')
        self.assertFalse(missing['exists'])
        self.assertGreater(missing['total'], 0)
        self.assertGreaterEqual(missing['free'], 0)
        self.assertLessEqual(missing['free'], missing['total'])
        json.dumps(disks, allow_nan=False)

    def test_new_reader_waits_only_for_exclusive_maintenance(self):
        entered = threading.Event()

        def reader():
            with self.gate.read():
                entered.set()

        with self.gate.maintenance() as acquired:
            self.assertTrue(acquired)
            thread = threading.Thread(target=reader)
            thread.start()
            self.assertFalse(entered.wait(0.02))
        thread.join(1)
        self.assertTrue(entered.is_set())


class RetentionApiTests(unittest.TestCase):
    def test_auth_csrf_configuration_and_preview(self):
        from oms_client.api import create_app

        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(
                os.environ,
                {
                    'DASHBOARD_DATA_ROOT': temp + '/legacy',
                    'DASHBOARD_COLLECTOR_DATA_ROOT': temp + '/collector',
                },
            ):
                app = create_app(
                    dict(TESTING=True, STATE_DIR=temp + '/state', PASSWORD='retention-test')
                )
                try:
                    client = app.test_client()
                    self.assertEqual(client.get('/api/storage').status_code, 401)
                    page = client.get('/login').get_data(as_text=True)
                    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page)[1]
                    client.post('/login', data=dict(csrf_token=csrf, password='retention-test'))
                    headers = {'X-CSRF-Token': client.get('/api/session').json['csrf_token']}
                    initial = client.get('/api/storage')
                    self.assertEqual(initial.status_code, 200)
                    self.assertFalse(initial.json['policy']['enabled'])
                    policy = dict(enabled=False, days=14, scopes=['legacy'])
                    self.assertEqual(
                        client.post('/api/storage/policy', json=policy).status_code, 403
                    )
                    self.assertEqual(
                        client.post(
                            '/api/storage/policy', json=dict(policy, path='/'), headers=headers
                        ).status_code,
                        400,
                    )
                    response = client.post('/api/storage/policy', json=policy, headers=headers)
                    self.assertEqual(response.json['policy'], policy)
                    self.assertEqual(
                        client.post(
                            '/api/storage/preview', json=policy, headers=headers
                        ).status_code,
                        202,
                    )
                    self.assertEqual(client.get('/api/storage').json['busy'], True)
                    with client.get('/') as page:
                        self.assertIn('storage-details', page.get_data(as_text=True))
                finally:
                    app.extensions['close_services']()


if __name__ == '__main__':
    unittest.main()
