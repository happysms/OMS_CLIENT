"""Bounded background jobs, result retention and persisted run metadata."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import logging
import os
from pathlib import Path
import secrets
import threading
import time

from ..analysis import history as data_service
from ..storage.state import utc_now
from .exports import _write_csv, _write_trades_csv

class AnalysisJobs:
    def __init__(self, store, analyzer, *, backtester=None, workers=1, max_pending=8, max_cached=16, ttl=3600, max_csv_bytes=256 * 1024 * 1024):
        self.store, self.analyzer = store, analyzer
        self.backtester = backtester
        self.executor = ThreadPoolExecutor(max_workers=max(1, min(2, workers)), thread_name_prefix="analysis")
        self.slots = threading.BoundedSemaphore(max_pending)
        self.jobs = OrderedDict()
        self.lock = threading.RLock()
        self.max_cached, self.ttl, self.max_csv_bytes = max_cached, ttl, max_csv_bytes
        self.directory = store.directory / "results"
        self.directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.directory, 0o700)
        self.closed = False
        # Result files are only a cache. Avoid retaining orphaned exports after restart.
        for path in self.directory.glob("*.csv"):
            if path.is_file() and not path.is_symlink():
                path.unlink()
        for path in self.directory.glob("*.tmp"):
            if path.is_file() and not path.is_symlink():
                path.unlink()

    @staticmethod
    def metadata(job):
        return {key: value for key, value in job.items() if key not in {"result", "future", "csv_path", "finished_monotonic"}}

    def _prune(self):
        now = time.monotonic()
        completed = [(key, job) for key, job in self.jobs.items() if job["status"] in {"complete", "failed"}]
        byte_count = sum(job.get("csv_bytes", 0) for _, job in completed)
        for key, job in completed:
            expired = now - job.get("finished_monotonic", now) >= self.ttl
            if expired or len(completed) > self.max_cached or byte_count > self.max_csv_bytes:
                path = job.get("csv_path")
                if path:
                    Path(path).unlink(missing_ok=True)
                byte_count -= job.get("csv_bytes", 0)
                self.jobs.pop(key, None)
                completed = [(k, j) for k, j in completed if k != key]

    def submit(self, config, *, kind="analysis"):
        if kind not in {"analysis", "backtest"}:
            raise ValueError("지원하지 않는 실행 종류입니다.")
        if self.closed or not self.slots.acquire(blocking=False):
            raise OverflowError("조회 요청이 많습니다. 현재 분석이 끝난 뒤 다시 실행해 주세요.")
        run_id = secrets.token_hex(16)
        now = utc_now()
        job = {"id": run_id, "kind": kind, "status": "queued", "config": config, "created_at": now, "started_at": None, "completed_at": None, "error": None, "result_available": False, "progress": 0}
        with self.lock:
            self._prune()
            self.jobs[run_id] = job
            self.store.save_run(self.metadata(job))
        try:
            future = self.executor.submit(self._run, run_id)
            with self.lock:
                if run_id in self.jobs:
                    self.jobs[run_id]["future"] = future
        except Exception:
            self.slots.release()
            with self.lock:
                self.jobs.pop(run_id, None)
            raise
        return {"id": run_id, "status": "queued"}

    def _run(self, run_id):
        try:
            with self.lock:
                job = self.jobs[run_id]
                job.update(status="running", started_at=utc_now(), progress=0.1)
                self.store.save_run(self.metadata(job))
            config = job["config"]
            if job["kind"] == "backtest":
                backtester = self.backtester
                if backtester is None:
                    from ..backtesting.engine import run_backtest
                    backtester = run_backtest
                result = backtester(config)
            else:
                requested = {key: value for key, value in config.items() if key != "unit"}
                requested["include_full_data"] = True
                result = self.analyzer(requested)
            csv_path = self.directory / f"{run_id}.csv"
            if job["kind"] == "backtest":
                _write_trades_csv(csv_path, result)
            else:
                _write_csv(csv_path, result, config.get("unit", ""))
            result.pop("full", None)
            with self.lock:
                job.update(status="complete", completed_at=utc_now(), result=result, result_available=True, progress=1, csv_path=str(csv_path), csv_bytes=csv_path.stat().st_size, finished_monotonic=time.monotonic())
                self.store.save_run(self.metadata(job))
                self._prune()
        except Exception as exc:
            message = str(exc) if isinstance(exc, (data_service.DataError, ValueError)) else "분석 중 오류가 발생했습니다. 서버 로그를 확인해 주세요."
            if not isinstance(exc, (data_service.DataError, ValueError)):
                logging.getLogger(__name__).exception("Analysis job failed: %s", run_id)
            with self.lock:
                job = self.jobs.get(run_id)
                if job:
                    job.update(status="failed", completed_at=utc_now(), error=message, result_available=False, progress=1, finished_monotonic=time.monotonic())
                    self.store.save_run(self.metadata(job))
                    self._prune()
        finally:
            self.slots.release()

    def get(self, run_id):
        with self.lock:
            self._prune()
            job = self.jobs.get(run_id)
            if job:
                metadata = self.metadata(job)
                if job.get("result") is not None:
                    metadata["result"] = job["result"]
                return metadata
        metadata = self.store.run(run_id)
        if metadata:
            metadata["result_available"] = False
            if metadata["status"] in {"queued", "running"}:
                metadata.update(status="failed", error="서버가 재시작되어 분석이 중단됐습니다. 같은 조건으로 다시 실행해 주세요.")
            return metadata
        raise KeyError(run_id)

    def csv(self, run_id):
        with self.lock:
            self._prune()
            job = self.jobs.get(run_id)
            if job and job["status"] == "complete" and job.get("csv_path"):
                path = Path(job["csv_path"])
                if path.is_file():
                    return path
        if self.store.run(run_id):
            raise ValueError("결과 보관 시간이 지났거나 분석이 끝나지 않았습니다. 다시 분석한 뒤 내보내 주세요.")
        raise KeyError(run_id)

    def history(self):
        rows = self.store.history()
        with self.lock:
            self._prune()
            available = {key for key, job in self.jobs.items() if job.get("result") is not None}
        for row in rows:
            row["result_available"] = row["id"] in available
            if row["status"] in {"queued", "running"} and row["id"] not in self.jobs:
                row.update(status="failed", error="서버가 재시작되어 분석이 중단됐습니다.")
        return rows

    def close(self):
        self.closed = True
        self.executor.shutdown(wait=True, cancel_futures=False)
