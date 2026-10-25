"""CSV exports preserve complete raw rows and enforce independent disk/job bounds."""
import csv
import io
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from oms_client.api.data_exports import DataExportJobs
from oms_client.market_data.csv_export import export_csv, filename, normalize_config
from oms_client.market_data.validation import DataError


class CsvConversionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {"DASHBOARD_DATA_ROOT": str(self.root / "data"), "DASHBOARD_COLLECTOR_DATA_ROOT": str(self.root / "collector")})
        env.start()
        self.addCleanup(env.stop)
        self.origin = int(pd.Timestamp("2026-10-25T00:00:00Z").timestamp() * 1000)
        self.write("00", {
            "desktopTime": [self.origin + 2000, self.origin, self.origin + 1000, self.origin + 1000, self.origin + 5000],
            "serverTime": [self.origin + value for value in [2000, 0, 1000, 1001, 5000]],
            "b1_p": [18.0, 10.0, 0.0, 25.0, 50.0], "a1_p": [22.0, 12.0, 20.0, 24.0, 51.0],
            "b5_q": [-2.0, 2.0, 3.0, 4.0, 5.0],
            "note": ["=SUM(1,2)", "한글", "+cmd", "@formula", "boundary"],
        })
        self.output = self.root / "out.csv"

    def write(self, hour, values):
        path = self.root / "data/bithumb/ob/BTC/2026/10/25" / f"{hour}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(values).to_parquet(path, index=False)

    def config(self, **changes):
        return {"exchange": "bithumb", "ticker": "btc", "start": "2026-10-25T09:00:00", "end": "2026-10-25T09:00:05", "mode": "raw", **changes}

    def read(self):
        content = self.output.read_bytes()
        self.assertTrue(content.startswith(b"\xef\xbb\xbf"))
        return list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))

    def test_raw_preserves_all_columns_duplicate_invalid_quotes_and_half_open_bounds(self):
        result = export_csv(self.config(), self.output)
        rows = self.read()
        self.assertEqual(result["rows"], 4)
        self.assertEqual(result["source_rows"], 4)
        self.assertEqual(result["columns"], ["desktopTime", "serverTime", "b1_p", "a1_p", "b5_q", "note"])
        self.assertEqual([int(row["desktopTime"]) for row in rows], [self.origin, self.origin + 1000, self.origin + 1000, self.origin + 2000])
        self.assertEqual([float(row["b1_p"]) for row in rows], [10, 0, 25, 18])
        self.assertEqual(rows[0]["note"], "한글")
        self.assertEqual(rows[-1]["note"], "'=SUM(1,2)")
        self.assertEqual(rows[-1]["b5_q"], "-2.0")
        self.assertEqual(result["bytes"], self.output.stat().st_size)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)

    def test_raw_unions_schema_changes_across_hour_files(self):
        self.write("01", {"desktopTime": [self.origin + 3600000], "serverTime": [1], "b1_p": [2], "a1_p": [3], "extra": ["new"]})
        result = export_csv(self.config(end="2026-10-25T10:00:01"), self.output)
        rows = self.read()
        self.assertEqual(result["rows"], 6)
        self.assertIn("extra", result["columns"])
        self.assertEqual(rows[-1]["extra"], "new")
        self.assertEqual(rows[-1]["b5_q"], "")

    def test_aggregated_export_is_valid_quote_mean_with_kst_and_source_count(self):
        result = export_csv(self.config(mode="aggregated", interval="1min"), self.output)
        rows = self.read()
        self.assertEqual(result["rows"], 1)
        self.assertEqual(result["source_rows"], 2)
        self.assertEqual(rows[0]["time_kst"], "2026-10-25T09:00:00+09:00")
        self.assertEqual(rows[0]["ticker"], "BTC")
        self.assertEqual(rows[0]["currency"], "KRW")
        self.assertEqual(float(rows[0]["mid"]), 15.5)
        self.assertEqual(float(rows[0]["spread"]), 3)
        self.assertEqual(int(rows[0]["source_rows"]), 2)

    def test_raw_is_not_limited_to_chart_samples_or_fetch_batch(self):
        count = 7001
        self.write("00", {"desktopTime": [self.origin + i for i in range(count)], "value": list(range(count))})
        result = export_csv(self.config(end="2026-10-25T09:00:10"), self.output)
        self.assertEqual(result["rows"], count)
        self.assertEqual(len(self.read()), count)

    def test_row_and_byte_limits_fail_without_partial_download(self):
        for limits in [{"max_rows": 3}, {"max_bytes": 20}]:
            with self.subTest(limits=limits), self.assertRaises(DataError):
                export_csv(self.config(), self.output, **limits)
            self.assertFalse(self.output.exists())
            self.assertEqual(list(self.root.glob(".csv-*.tmp")), [])

    def test_empty_range_missing_source_and_unsafe_selection_fail(self):
        for changes in [{"start": "2026-10-25T09:00:06", "end": "2026-10-25T09:00:07"}, {"ticker": "MISSING"}, {"ticker": "../../private"}]:
            with self.subTest(changes=changes), self.assertRaises(DataError):
                export_csv(self.config(**changes), self.output)
            self.assertFalse(self.output.exists())

    def test_request_limits_reject_unknown_fields_and_excessive_work(self):
        for changes in [{"path": "/private"}, {"mode": "other"}, {"interval": "0s"}, {"end": "2026-12-01T09:00"}, {"start": "bad"}, {"mode": "aggregated", "interval": "1s", "end": "2026-10-29T09:00"}]:
            with self.subTest(changes=changes), self.assertRaises(DataError):
                normalize_config(self.config(**changes))

    def test_colon_symbol_filename_is_portable_and_kst_stamped(self):
        config = normalize_config(self.config(exchange="hyperliquid", ticker="xyz:AAOI"))
        name = filename(config)
        self.assertIn("xyz_AAOI-raw-20261025-090000", name)
        self.assertNotIn(":", name)


class CsvJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {"exchange": "bithumb", "ticker": "BTC", "start": "2026-10-25T09:00", "end": "2026-10-25T10:00"}

    def jobs(self, **kwargs):
        service = DataExportJobs(self.temp.name, **kwargs)
        self.addCleanup(service.close)
        return service

    @staticmethod
    def exporter(config, path):
        path.write_bytes(b"\xef\xbb\xbfx\r\n1\r\n")
        return {"rows": 1, "columns": ["x"], "source_rows": 1}

    def wait(self, service, identifier):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = service.get(identifier)
            if result["status"] in {"complete", "failed"}:
                return result
            time.sleep(0.005)
        self.fail("CSV job did not finish")

    def test_job_metadata_download_and_ttl_remove_private_files(self):
        service = self.jobs(exporter=self.exporter, ttl=0.1)
        identifier = service.submit(self.config)["id"]
        result = self.wait(service, identifier)
        self.assertEqual(result["rows"], 1)
        self.assertEqual(result["columns"], ["x"])
        self.assertTrue(result["download_available"])
        self.assertTrue(result["expires_at"])
        path, name = service.csv(identifier)
        self.assertTrue(name.endswith(".csv"))
        self.assertEqual(stat.S_IMODE(service.directory.stat().st_mode), 0o700)
        with patch("oms_client.api.data_exports.time.monotonic", return_value=time.monotonic() + 1):
            with self.assertRaises(KeyError):
                service.get(identifier)
        self.assertFalse(path.exists())

    def test_failure_removes_exporter_partial_file(self):
        def broken(config, path):
            path.write_text("partial")
            raise DataError("테스트 실패")
        service = self.jobs(exporter=broken)
        identifier = service.submit(self.config)["id"]
        result = self.wait(service, identifier)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "테스트 실패")
        self.assertFalse(result["download_available"])
        self.assertEqual(list(service.directory.glob("*.csv")), [])
        with self.assertRaises(ValueError):
            service.csv(identifier)

    def test_queue_rejects_overload_and_does_not_offer_running_csv(self):
        release = threading.Event()
        self.addCleanup(release.set)
        def blocking(config, path):
            release.wait(3)
            return self.exporter(config, path)
        service = self.jobs(exporter=blocking, max_pending=1)
        identifier = service.submit(self.config)["id"]
        with self.assertRaises(OverflowError):
            service.submit(self.config)
        with self.assertRaises(ValueError):
            service.csv(identifier)
        release.set()
        self.assertEqual(self.wait(service, identifier)["status"], "complete")

    def test_retention_prunes_oldest_and_startup_cleans_orphans(self):
        service = self.jobs(exporter=self.exporter, max_cached=1)
        first = service.submit(self.config)["id"]
        self.wait(service, first)
        second = service.submit(self.config)["id"]
        self.wait(service, second)
        with self.assertRaises(KeyError):
            service.get(first)
        self.assertFalse((service.directory / f"{first}.csv").exists())
        service.close()
        restarted = self.jobs(exporter=self.exporter)
        self.assertFalse(list(restarted.directory.glob("*.csv")))


if __name__ == "__main__":
    unittest.main()
