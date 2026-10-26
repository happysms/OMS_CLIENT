"""Durable premium rollups: weighting, idempotency, correction and recovery."""
import fcntl
import json
import os
import re
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from oms_client.analysis.premiums import PremiumService, completed_end, hourly_statistics
from oms_client.storage.premiums import HOUR, PremiumStore
from oms_client.api import create_app


class PremiumTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        environment = patch.dict(os.environ, DASHBOARD_DATA_ROOT=str(self.root / 'data'),
                                 DASHBOARD_COLLECTOR_DATA_ROOT=str(self.root / 'collector'))
        environment.start()
        self.addCleanup(environment.stop)
        self.origin = 1792886400000
        self.now = self.origin + 2 * HOUR + 180000
        self.service = PremiumService(self.root / 'state', autostart=False, clock=lambda: self.now / 1000)
        self.addCleanup(self.service.close)
        self.write('bithumb', 'BTC', [0, 1000], [10100, 20400])
        self.write('okx', 'BTC', [0, 1000], [10, 10])
        self.write('bithumb', 'USDT', [0, 1000], [1000, 2000])
        self.write('bithumb', 'BTC', [HOUR], [11000], hour='01')
        self.write('okx', 'BTC', [HOUR], [10], hour='01')
        self.write('bithumb', 'USDT', [HOUR], [1000], hour='01')

    def write(self, exchange, ticker, offsets, prices, hour='00'):
        path = self.root / 'data' / exchange / 'ob' / ticker / '2026/10/25' / f'{hour}.parquet'
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(dict(desktopTime=[self.origin + value for value in offsets],
                          b1_p=prices, a1_p=prices)).to_parquet(path, index=False)
        return path

    def test_hourly_and_rolling_weight_by_observed_seconds(self):
        self.service.refresh()
        item = self.service.status()['items'][0]
        self.assertAlmostEqual(item['windows']['3']['mean_pct'], 13 / 3)
        self.assertEqual(item['windows']['3']['samples'], 3)
        self.assertEqual(item['windows']['7']['samples'], 3)
        self.assertFalse(item['windows']['3']['complete'])
        hours = self.service.store.hourly('BTC', self.origin + 2 * HOUR)['items'][-2:]
        self.assertAlmostEqual(hours[0]['mean_pct'], 1.5)
        self.assertAlmostEqual(hours[1]['mean_pct'], 10)
        self.assertEqual([row['samples'] for row in hours], [2, 1])
        json.dumps(self.service.status(), allow_nan=False)

    def test_rerun_restart_and_changed_file_replace_instead_of_double_count(self):
        self.service.refresh()
        with patch('oms_client.analysis.premiums.read_prices', side_effect=AssertionError('unchanged source read')):
            self.service.refresh()
        restored = PremiumService(self.root / 'state', autostart=False, clock=lambda: self.now / 1000)
        self.addCleanup(restored.close)
        self.assertEqual(restored.status()['items'][0]['windows']['3']['samples'], 3)
        self.write('bithumb', 'BTC', [0, 1000], [10200, 20600])
        restored.refresh()
        value = restored.status()['items'][0]['windows']['3']
        self.assertEqual(value['samples'], 3)
        self.assertAlmostEqual(value['mean_pct'], 5)

    def test_missing_raw_retention_does_not_erase_accumulated_hours(self):
        self.service.refresh()
        (self.root / 'data/bithumb/ob/BTC/2026/10/25/00.parquet').unlink()
        self.service.refresh()
        self.assertEqual(self.service.status()['items'][0]['windows']['3']['samples'], 3)
        self.assertEqual(self.service.store.get('job')['retained_hours'], 1)

    def test_latest_complete_hour_grace_and_future_hours_excluded(self):
        self.assertEqual(completed_end(self.origin + HOUR + 119999), self.origin)
        self.assertEqual(completed_end(self.origin + HOUR + 120000), self.origin + HOUR)
        self.now = self.origin + HOUR + 180000
        self.service.refresh()
        self.assertEqual(self.service.status()['items'][0]['windows']['3']['samples'], 2)

    def test_failed_source_is_reported_then_recovers(self):
        with patch('oms_client.analysis.premiums.read_prices', side_effect=OSError('source unavailable')):
            self.service.refresh()
        self.assertGreater(self.service.status()['items'][0]['error_hours'], 0)
        self.assertIsNone(self.service.status()['items'][0]['windows']['3']['mean_pct'])
        self.service.refresh()
        self.assertEqual(self.service.status()['items'][0]['error_hours'], 0)
        self.assertEqual(self.service.status()['items'][0]['windows']['3']['samples'], 3)

    def test_interrupted_same_hour_auto_resumes_and_completed_run_skips(self):
        self.service.store.set('last_attempt_end', completed_end(self.now))
        self.service.store.set('job', {'state': 'running'})
        self.assertTrue(self.service.refresh(automatic=True))
        self.assertFalse(self.service.refresh(automatic=True))

    def test_process_lock_prevents_duplicate_writer(self):
        with (self.root / 'state/premiums.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertFalse(self.service.refresh())
            self.assertIsNone(self.service.store.get('job'))

    def test_late_reference_file_fills_previously_empty_hour(self):
        path = self.root / 'data/bithumb/ob/USDT/2026/10/25/01.parquet'
        path.unlink()
        self.service.refresh()
        self.assertEqual(self.service.status()['items'][0]['windows']['3']['samples'], 2)
        self.write('bithumb', 'USDT', [HOUR], [1000], hour='01')
        self.service.refresh()
        self.assertEqual(self.service.status()['items'][0]['windows']['3']['samples'], 3)

    def test_changed_hour_failure_does_not_invalidate_unchanged_hour(self):
        self.service.refresh()
        self.write('bithumb', 'BTC', [HOUR], [12000], hour='01')
        with patch('oms_client.analysis.premiums.read_prices', side_effect=OSError('source unavailable')):
            self.service.refresh()
        item = self.service.status()['items'][0]
        self.assertEqual(item['error_hours'], 1)
        self.assertEqual(item['windows']['3']['samples'], 2)

    def test_aligned_seconds_only_no_forward_fill_and_negative_preserved(self):
        b = pd.Series([9900, 10100, 10200], index=[self.origin, self.origin + 1000, self.origin + 2000])
        o = pd.Series([10, 10], index=[self.origin, self.origin + 2000])
        fx = pd.Series([1000, 1000], index=[self.origin, self.origin + 1000])
        rows = hourly_statistics(b, o, fx, [self.origin], {self.origin: 'test'})
        self.assertEqual(rows[0]['samples'], 1)
        self.assertAlmostEqual(rows[0]['total'], -1)

    def test_three_and_seven_day_boundaries_differ(self):
        end = self.origin + 2 * HOUR
        hours = [end - 168 * HOUR - HOUR, end - 168 * HOUR, end - 72 * HOUR - HOUR, end - 72 * HOUR, end]
        self.service.store.save('BTC', [dict(hour_ms=hour, total=value, samples=1,
                                            minimum=value, maximum=value, fingerprint='test')
                                       for hour, value in zip(hours, [1000, 1, 2, 3, 1000])])
        value = self.service.store.summary(end, ['BTC'])['items'][0]
        self.assertEqual(value['windows']['3']['mean_pct'], 3)
        self.assertEqual(value['windows']['7']['mean_pct'], 2)

    def test_api_auth_csrf_csv_and_persisted_switch(self):
        app = create_app(dict(TESTING=True, STATE_DIR=str(self.root / 'state'), PASSWORD='test-password-only'))
        self.addCleanup(app.extensions['close_services'])
        api_service = app.extensions['premium_service']
        api_service.clock = lambda: self.now / 1000
        client = app.test_client()
        self.assertEqual(client.get('/api/premiums').status_code, 401)
        login = client.get('/login').text
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', login)[1]
        client.post('/login', data={'password': 'test-password-only', 'csrf_token': csrf})
        token = client.get('/api/session').json['csrf_token']
        self.assertEqual(client.post('/api/premiums/settings', json={'enabled': False}).status_code, 403)
        headers = {'X-CSRF-Token': token}
        self.assertEqual(client.post('/api/premiums/settings', json={'enabled': False}, headers=headers).status_code, 200)
        self.assertFalse(PremiumStore(self.root / 'state').get('enabled'))
        self.assertEqual(client.post('/api/premiums/settings', json={'enabled': 1}, headers=headers).status_code, 400)
        self.assertEqual(client.get('/api/premiums/hourly?ticker=../../etc').status_code, 400)
        self.service.refresh()
        self.assertEqual(client.get('/api/premiums').json['items'][0]['windows']['7']['samples'], 3)
        self.assertIn('mean_3d_pct', client.get('/api/premiums/csv').text)
        self.assertEqual(len(client.get('/api/premiums/hourly?ticker=BTC').json['items']), 168)
        self.assertEqual(client.post('/api/premiums/refresh', json={}, headers=headers).status_code, 202)


if __name__ == '__main__':
    unittest.main()
