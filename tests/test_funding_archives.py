"""Compressed funding is read without changing sources or retaining temp data."""

import gzip
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

import duckdb
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from oms_client.market_data import funding_archives
from oms_client.market_data.funding_archives import materialize_parquet
from oms_client.market_data.validation import DataError


class FundingArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.plain = self.root / "plain.parquet"
        pd.DataFrame({"desktopTime": [1792911579599], "fundingRate": [-0.0000429684]}).to_parquet(
            self.plain, index=False
        )
        self.archive = self.root / "snapshot.parquet.gz"
        self.archive.write_bytes(gzip.compress(self.plain.read_bytes()))

    def track_extraction(self):
        created = []
        original = tempfile.TemporaryDirectory

        def temporary_directory(**kwargs):
            directory = original(dir=self.root, **kwargs)
            created.append(Path(directory.name))
            return directory

        return created, patch.object(
            funding_archives.tempfile, "TemporaryDirectory", side_effect=temporary_directory
        )

    def test_reads_parquet_preserves_source_and_cleans_up(self):
        original = self.archive.read_bytes()
        with materialize_parquet([self.archive]) as paths:
            extracted = Path(paths[0])
            self.assertNotEqual(extracted, self.archive)
            with duckdb.connect(":memory:") as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT fundingRate FROM read_parquet(?)", [paths[0]]
                    ).fetchone(),
                    (-0.0000429684,),
                )
            self.assertEqual(stat.S_IMODE(extracted.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(extracted.parent.stat().st_mode), 0o700)
        self.assertFalse(extracted.exists())
        self.assertFalse(extracted.parent.exists())
        self.assertEqual(self.archive.read_bytes(), original)

    def test_order_plain_paths_and_repeated_archives(self):
        with materialize_parquet([self.archive, self.plain, self.archive]) as paths:
            self.assertEqual(paths[1], str(self.plain))
            self.assertNotEqual(paths[0], paths[2])
            self.assertEqual(
                [Path(path).read_bytes() for path in paths], [self.plain.read_bytes()] * 3
            )
            self.assertEqual(Path(paths[0]).name, "000000.parquet")
            self.assertEqual(Path(paths[2]).name, "000002.parquet")
        self.assertTrue(self.plain.exists())
        self.assertTrue(self.archive.exists())

    def test_plain_only_and_empty_lists_do_not_create_temporary_files(self):
        with patch.object(funding_archives.tempfile, "TemporaryDirectory") as create:
            with materialize_parquet([self.plain]) as paths:
                self.assertEqual(paths, [str(self.plain)])
            with materialize_parquet([]) as paths:
                self.assertEqual(paths, [])
            create.assert_not_called()

    def test_consumer_duckdb_failure_cleans_extracted_files(self):
        self.archive.write_bytes(gzip.compress(b"not parquet"))
        with self.assertRaises(duckdb.Error):
            with materialize_parquet([self.archive]) as paths:
                extracted = Path(paths[0])
                with duckdb.connect(":memory:") as connection:
                    connection.execute("SELECT * FROM read_parquet(?)", [paths[0]])
        self.assertFalse(extracted.exists())
        self.assertFalse(extracted.parent.exists())

    def test_corrupt_and_truncated_gzip_clean_partial_output(self):
        for data in (b"not gzip", self.archive.read_bytes()[:-5]):
            with self.subTest(data_length=len(data)):
                self.archive.write_bytes(data)
                created, tracked = self.track_extraction()
                with tracked, self.assertRaisesRegex(DataError, "손상"):
                    with materialize_parquet([self.archive]):
                        self.fail("Corrupt gzip must not reach the consumer")
                self.assertEqual(len(created), 1)
                self.assertFalse(created[0].exists())

    def test_single_archive_and_combined_stream_limits(self):
        self.archive.write_bytes(gzip.compress(b"x" * (2 * 1024 * 1024)))
        scenarios = [
            {"max_file_bytes": 1024},
            {"max_total_bytes": 1024},
            {"max_total_bytes": 3 * 1024 * 1024},
        ]
        for limits in scenarios:
            with self.subTest(limits=limits):
                created, tracked = self.track_extraction()
                paths = (
                    [self.archive, self.archive]
                    if limits.get("max_total_bytes") == 3 * 1024 * 1024
                    else [self.archive]
                )
                with tracked, self.assertRaisesRegex(DataError, "용량 제한"):
                    with materialize_parquet(paths, **limits):
                        self.fail("Oversized gzip must not reach the consumer")
                self.assertEqual(len(created), 1)
                self.assertFalse(created[0].exists())

    def test_stream_never_reads_beyond_one_chunk_or_remaining_limit_plus_one(self):
        self.archive.write_bytes(gzip.compress(b"x" * (2 * 1024 * 1024)))
        original_open = gzip.open
        requests = []

        class RecordingReader:
            def __enter__(self):
                self.reader = original_open(self.archive_path, "rb")
                return self

            def __exit__(self, *args):
                self.reader.close()

            def read(self, size):
                requests.append(size)
                return self.reader.read(size)

        reader = RecordingReader()
        reader.archive_path = self.archive
        with patch.object(funding_archives.gzip, "open", return_value=reader):
            with self.assertRaises(DataError):
                with materialize_parquet([self.archive], max_file_bytes=1024):
                    self.fail("Oversized gzip must not reach the consumer")
        self.assertEqual(requests, [1025])

    def test_later_corrupt_archive_cleans_previously_extracted_file(self):
        corrupt = self.root / "bad.parquet.gz"
        corrupt.write_bytes(b"not gzip")
        created, tracked = self.track_extraction()
        with tracked, self.assertRaises(DataError):
            with materialize_parquet([self.archive, corrupt]):
                self.fail("Corrupt gzip must not reach the consumer")
        self.assertEqual(len(created), 1)
        self.assertFalse(created[0].exists())
        self.assertTrue(self.archive.is_file())

    def test_exact_byte_limit_succeeds(self):
        size = self.plain.stat().st_size
        with materialize_parquet(
            [self.archive], max_file_bytes=size, max_total_bytes=size
        ) as paths:
            self.assertEqual(Path(paths[0]).read_bytes(), self.plain.read_bytes())

    def test_plain_and_archive_file_count_limits_apply_before_extraction(self):
        for paths in ([self.plain] * 2, [self.archive] * 2):
            with patch.object(funding_archives.tempfile, "TemporaryDirectory") as create:
                with self.assertRaisesRegex(DataError, "파일이 너무 많"):
                    with materialize_parquet(iter(paths), max_files=1):
                        self.fail("File count must be checked before extraction")
                create.assert_not_called()

    def test_invalid_inputs_have_friendly_errors(self):
        for value in (0, -1, True, 1.5, "10"):
            for limit in ("max_total_bytes", "max_file_bytes", "max_files"):
                with self.subTest(limit=limit, value=value), self.assertRaises(DataError):
                    with materialize_parquet([], **{limit: value}):
                        self.fail("Invalid limits must fail")
        for path in (self.root / "unknown.txt", self.root / "missing.parquet", None):
            with self.subTest(path=path), self.assertRaises(DataError):
                with materialize_parquet([path]):
                    self.fail("Invalid source must fail")


if __name__ == "__main__":
    unittest.main()
