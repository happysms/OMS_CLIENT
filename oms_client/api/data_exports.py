"""Bounded asynchronous CSV conversions and short-lived private download files."""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import logging
import os
from pathlib import Path
import secrets
import shutil
import threading
import time

from ..market_data.csv_export import MAX_BYTES, MAX_ROWS, export_csv, filename, normalize_config
from ..market_data.validation import DataError
from ..storage.state import utc_now


class DataExportJobs:
    def __init__(self, directory, *, exporter=None, max_pending=4, max_cached=8, ttl=3600, max_bytes=MAX_BYTES, max_rows=MAX_ROWS):
        self.directory = Path(directory) / "data_exports"
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self.directory, 0o700)
        self.exporter = exporter or (lambda config, path: export_csv(config, path, max_rows=max_rows, max_bytes=max_bytes))
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="csv-export")
        self.slots = threading.BoundedSemaphore(max_pending)
        self.max_cached, self.ttl, self.max_bytes = max_cached, ttl, max_bytes
        self.jobs = OrderedDict()
        self.lock = threading.RLock()
        self.closed = False
        for pattern in ["*.csv", ".csv-*.tmp"]:
            for path in self.directory.glob(pattern):
                if path.is_file() and not path.is_symlink():
                    path.unlink()
        for path in self.directory.glob(".csv-sort-*"):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)

    def _prune(self):
        completed = [(key, job) for key, job in self.jobs.items() if job["status"] in {"complete", "failed"}]
        total = sum(job.get("bytes") or 0 for _, job in completed)
        for key, job in completed:
            if time.monotonic() - job["finished_monotonic"] >= self.ttl or len(completed) > self.max_cached or total > self.max_bytes:
                (self.directory / f"{key}.csv").unlink(missing_ok=True)
                total -= job.get("bytes") or 0
                self.jobs.pop(key, None)
                completed = [(other, item) for other, item in completed if other != key]

    @staticmethod
    def _metadata(job):
        result = {key: value for key, value in job.items() if key != "finished_monotonic"}
        result["download_available"] = result["status"] == "complete"
        return result

    def submit(self, payload):
        config = normalize_config(payload)
        with self.lock:
            if self.closed or not self.slots.acquire(blocking=False):
                raise OverflowError("CSV 요청이 많습니다. 현재 변환이 끝난 뒤 다시 실행해 주세요.")
            self._prune()
            identifier = secrets.token_hex(16)
            self.jobs[identifier] = {"id": identifier, "status": "queued", "config": config, "created_at": utc_now(), "started_at": None, "completed_at": None, "expires_at": None, "error": None, "rows": None, "columns": None, "filename": None, "bytes": None, "source_rows": None}
            try:
                self.executor.submit(self._run, identifier)
            except Exception:
                self.slots.release()
                self.jobs.pop(identifier, None)
                raise
        return {"id": identifier, "status": "queued"}

    def _run(self, identifier):
        path = self.directory / f"{identifier}.csv"
        try:
            with self.lock:
                job = self.jobs[identifier]
                job.update(status="running", started_at=utc_now())
                config = job["config"].copy()
            result = self.exporter(config, path)
            os.chmod(path, 0o600)
            size = path.stat().st_size
            if size > self.max_bytes:
                raise DataError("CSV 파일 크기 한도를 초과했습니다. 기간을 줄여 다시 내보내 주세요.")
            with self.lock:
                job.update(status="complete", completed_at=utc_now(), expires_at=(datetime.now(timezone.utc) + timedelta(seconds=self.ttl)).isoformat(), rows=result["rows"], columns=result["columns"], source_rows=result.get("source_rows", result["rows"]), filename=filename(config), bytes=size, finished_monotonic=time.monotonic())
                self._prune()
        except Exception as exc:
            path.unlink(missing_ok=True)
            if not isinstance(exc, (DataError, ValueError)):
                logging.getLogger(__name__).exception("CSV export failed: %s", identifier)
            with self.lock:
                job = self.jobs.get(identifier)
                if job:
                    job.update(status="failed", error=str(exc) if isinstance(exc, (DataError, ValueError)) else "CSV 변환 중 오류가 발생했습니다. 서버 로그를 확인해 주세요.", completed_at=utc_now(), finished_monotonic=time.monotonic())
                    self._prune()
        finally:
            self.slots.release()

    def get(self, identifier):
        with self.lock:
            self._prune()
            if identifier not in self.jobs:
                raise KeyError(identifier)
            return self._metadata(self.jobs[identifier])

    def csv(self, identifier):
        with self.lock:
            job = self.get(identifier)
            path = self.directory / f"{identifier}.csv"
            if job["status"] != "complete" or not path.is_file():
                raise ValueError("CSV 변환이 끝나지 않았거나 실패했습니다. 다시 내보내 주세요.")
            return path, job["filename"]

    def close(self):
        with self.lock:
            self.closed = True
        self.executor.shutdown(wait=True, cancel_futures=False)
