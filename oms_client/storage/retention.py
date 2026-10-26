"""Opt-in retention of declared market files; account and result stores are excluded."""
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading
import time

from ..market_data.file_browser import COLLECTOR_KINDS, LEGACY_KINDS, _is_file, _validate_parts
from ..market_data.validation import DataError
from .access import DATA_ACCESS


@dataclass(frozen=True)
class RetentionPolicy:
    enabled: bool = False
    days: int = 7
    scopes: tuple[str, ...] = ('legacy', 'collector')

    @classmethod
    def parse(cls, payload):
        if not isinstance(payload, dict) or set(payload) != {'enabled', 'days', 'scopes'}:
            raise DataError('자동 정리 여부·보관 일수·대상 저장소를 지정하세요.')
        if type(payload['enabled']) is not bool or type(payload['days']) is not int or not 1 <= payload['days'] <= 3650:
            raise DataError('자동 정리는 참/거짓, 보관 기간은 1~3650일 정수로 지정하세요.')
        scopes = payload['scopes']
        if (not isinstance(scopes, list) or not scopes or len(scopes) > 2
                or any(not isinstance(value, str) or value not in {'legacy', 'collector'} for value in scopes)
                or len(set(scopes)) != len(scopes)):
            raise DataError('정리 대상 저장소를 선택하세요.')
        return cls(payload['enabled'], payload['days'], tuple(scope for scope in ('legacy', 'collector') if scope in scopes))

    def report(self):
        return dict(enabled=self.enabled, days=self.days, scopes=list(self.scopes))


@dataclass(frozen=True)
class Candidate:
    scope: str
    relative: str
    size: int
    device: int
    inode: int
    modified_ns: int


class MarketFileRetention:
    """Bounded metadata scan and descriptor-relative deletion, without following links."""
    def __init__(self, roots, protected=(), *, max_entries=200000, max_candidates=1000, scan_seconds=5):
        self.roots = {name: Path(path).resolve() for name, path in roots.items()}
        self.protected = tuple(Path(path).resolve() for path in protected)
        self.max_entries, self.max_candidates, self.scan_seconds = max_entries, max_candidates, scan_seconds
        self._cursor = None
        self._cursor_policy = None

    @staticmethod
    def period_end(parts):
        if parts[1] == 'candles_1m':
            return datetime(int(parts[3]), int(parts[4]), int(parts[5][:2]), tzinfo=timezone.utc) + timedelta(days=1)
        return datetime(int(parts[3]), int(parts[4]), int(parts[5]), int(parts[6][:2]), tzinfo=timezone.utc) + timedelta(hours=1)

    def _entries(self, policy, excluded_collector_kinds):
        def walk(scope, directory, parts):
            try:
                if directory.is_symlink() or any(directory.is_relative_to(path) for path in self.protected):
                    return
                with os.scandir(directory) as entries:
                    for entry in entries:
                        child = (*parts, entry.name)
                        allowed = True
                        try:
                            _validate_parts(child)
                        except ValueError:
                            allowed = False
                        kinds = COLLECTOR_KINDS if scope == 'collector' else LEGACY_KINDS
                        if len(child) >= 2 and child[1] not in kinds:
                            allowed = False
                        if scope == 'collector' and len(child) >= 2 and child[1] in excluded_collector_kinds:
                            allowed = False
                        path = Path(entry.path)
                        if any(path.is_relative_to(protected) for protected in self.protected):
                            allowed = False
                        # Yield every entry so ignored files also count against the budget.
                        yield scope, child, entry if allowed else False
                        if allowed and not _is_file(child) and entry.is_dir(follow_symlinks=False):
                            yield from walk(scope, path, child)
            except FileNotFoundError:
                return
            except OSError:
                yield scope, parts, None
        for scope in policy.scopes:
            yield from walk(scope, self.roots[scope], ())

    def close(self):
        if self._cursor is not None:
            self._cursor.close()
            self._cursor = None

    def scan(self, policy, now, stopped=lambda: False, *, excluded_collector_kinds=(), resume=False):
        cutoff = now - policy.days * 86400
        candidates, examined, truncated, errors = [], 0, False, 0
        deadline = time.monotonic() + self.scan_seconds
        signature = (policy, tuple(sorted(excluded_collector_kinds)))
        if resume:
            if self._cursor is None or self._cursor_policy != signature:
                self.close()
                self._cursor = self._entries(policy, excluded_collector_kinds)
                self._cursor_policy = signature
            entries = self._cursor
        else:
            entries = self._entries(policy, excluded_collector_kinds)
        try:
            while True:
                if (examined >= self.max_entries or len(candidates) >= self.max_candidates
                        or time.monotonic() > deadline or stopped()):
                    truncated = True
                    break
                try:
                    scope, parts, entry = next(entries)
                except StopIteration:
                    if resume:
                        self.close()
                    break
                examined += 1
                if entry is None:
                    errors += 1
                    continue
                if entry is False or not _is_file(parts):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                    end = self.period_end(parts).timestamp()
                    if (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                            and info.st_mtime < cutoff and end <= cutoff):
                        candidates.append(Candidate(scope, '/'.join(parts), info.st_size,
                                                    info.st_dev, info.st_ino, info.st_mtime_ns))
                except FileNotFoundError:
                    continue
                except (OSError, ValueError, OverflowError):
                    errors += 1
        finally:
            if not resume:
                entries.close()
        return candidates, dict(examined=examined, truncated=truncated, scan_errors=errors,
                                cutoff_ms=int(cutoff * 1000), count=len(candidates),
                                bytes=sum(candidate.size for candidate in candidates))

    def delete(self, candidate):
        root = self.roots[candidate.scope]
        parts = tuple(candidate.relative.split('/'))
        _validate_parts(parts)
        if not _is_file(parts) or any(root.joinpath(*parts).is_relative_to(path) for path in self.protected):
            return False
        with ExitStack() as stack:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            descriptor = os.open(root, flags)
            stack.callback(os.close, descriptor)
            for part in parts[:-1]:
                descriptor = os.open(part, flags, dir_fd=descriptor)
                stack.callback(os.close, descriptor)
            info = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
            identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or identity != (candidate.device, candidate.inode, candidate.size, candidate.modified_ns)):
                return False
            os.unlink(parts[-1], dir_fd=descriptor)
        return True


class RetentionService:
    def __init__(self, directory, roots, *, autostart=True, gate=DATA_ACCESS, clock=time.time):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'retention.json'
        self.scanner = MarketFileRetention(roots, protected=(self.directory,))
        self.gate, self.clock = gate, clock
        self.lock = threading.RLock()
        self.stop_event, self.wake = threading.Event(), threading.Event()
        self.pending = None
        self.busy = False
        self.revision = 0
        self.last = None
        self.error = None
        self.policy = RetentionPolicy()
        try:
            self.policy = RetentionPolicy.parse(json.loads(self.path.read_text()))
        except FileNotFoundError:
            pass
        except (ValueError, OSError):
            self.error = '저장된 정리 설정을 읽지 못해 자동 정리를 껐습니다. 설정을 다시 저장하세요.'
        self.thread = threading.Thread(target=self._loop, name='market-file-retention', daemon=True)
        if autostart:
            self.thread.start()

    def disks(self):
        result = []
        paths = {'system': Path('/'), **self.scanner.roots, 'state': self.directory}
        for name, path in paths.items():
            anchor = path
            try:
                while not anchor.exists() and anchor != anchor.parent:
                    anchor = anchor.parent
                usage = shutil.disk_usage(anchor)
                result.append(dict(scope=name, path=str(path), exists=path.exists(),
                                   device=str(anchor.stat().st_dev), total=usage.total,
                                   used=usage.used, free=usage.free, used_percent=round(usage.used / usage.total * 100, 2)))
            except OSError:
                result.append(dict(scope=name, path=str(path), error='저장 공간 정보를 읽을 수 없습니다.'))
        return result

    def status(self):
        with self.lock:
            return dict(policy=self.policy.report(), busy=self.busy or self.pending is not None,
                        last=self.last, error=self.error, disks=self.disks(), interval_seconds=600)

    def save_policy(self, payload):
        policy = RetentionPolicy.parse(payload)
        with self.lock:
            descriptor, name = tempfile.mkstemp(prefix='.retention-', dir=self.directory)
            try:
                with os.fdopen(descriptor, 'w') as stream:
                    json.dump(policy.report(), stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, self.path)
            finally:
                Path(name).unlink(missing_ok=True)
            self.policy = policy
            self.revision += 1
            self.error = None
        self.wake.set()
        return self.status()

    def preview(self, payload):
        policy = RetentionPolicy.parse(payload)
        with self.lock:
            if self.busy or self.pending is not None:
                raise DataError('파일 확인 또는 정리가 진행 중입니다. 완료 후 다시 시도하세요.')
            self.pending = policy
        self.wake.set()
        return self.status()

    def run_once(self, policy=None, *, preview=False):
        with self.lock:
            if self.busy:
                return
            self.busy = True
            policy = policy or self.policy
            revision = self.revision
        report = dict(mode='preview' if preview else 'cleanup', policy=policy.report(),
                      started_ms=int(self.clock()*1000), deleted=0, deleted_bytes=0, skipped=0, errors=0, deferred=None)
        try:
            if not preview and not policy.enabled:
                report['deferred'] = 'disabled'
                return
            with (self.directory / 'retention.lock').open('a') as lock, ExitStack() as collector_locks:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    report['deferred'] = 'another_cleanup'
                    return
                blocked = set()
                if not preview and 'collector' in policy.scopes:
                    root = self.scanner.roots['collector']
                    if root.is_dir():
                        for kind, filename in (('ob_1s', '.usdt-orderbook.lock'), ('candles_1m', '.usdt-candles-1m.lock')):
                            try:
                                descriptor = os.open(root / filename, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                                collector_locks.callback(os.close, descriptor)
                                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            except OSError:
                                blocked.add(kind)
                    if blocked:
                        report['deferred'] = 'collector_in_use'
                        report['protected_kinds'] = sorted(blocked)
                candidates, summary = self.scanner.scan(policy, self.clock(), self.stop_event.is_set,
                                                        excluded_collector_kinds=blocked, resume=not preview)
                report.update(summary, samples=[dict(scope=c.scope, path=c.relative, bytes=c.size) for c in candidates[:50]])
                if preview:
                    return
                # Short exclusive batches never interrupt an already-running data reader.
                for offset in range(0, len(candidates), 50):
                    with self.gate.maintenance() as acquired:
                        if not acquired:
                            report['deferred'] = 'data_in_use'
                            break
                        with self.lock:
                            if self.stop_event.is_set() or revision != self.revision:
                                report['deferred'] = 'settings_changed'
                                break
                            for candidate in candidates[offset:offset+50]:
                                try:
                                    if self.scanner.delete(candidate):
                                        report['deleted'] += 1
                                        report['deleted_bytes'] += candidate.size
                                    else:
                                        report['skipped'] += 1
                                except FileNotFoundError:
                                    report['skipped'] += 1
                                except OSError:
                                    report['errors'] += 1
        except Exception:
            report['errors'] += 1
            report['deferred'] = 'failed'
        finally:
            report['finished_ms'] = int(self.clock()*1000)
            with self.lock:
                self.last = report
                self.busy = False

    def _loop(self):
        next_run = 0
        while not self.stop_event.is_set():
            with self.lock:
                preview, self.pending = self.pending, None
                policy = self.policy
            if preview is not None:
                self.run_once(preview, preview=True)
            elif policy.enabled and time.monotonic() >= next_run:
                self.run_once()
                next_run = time.monotonic() + 600
            notified = self.wake.wait(60)
            self.wake.clear()
            if notified:
                next_run = 0

    def close(self):
        self.stop_event.set()
        self.wake.set()
        if self.thread.is_alive():
            self.thread.join(timeout=6)
        if not self.thread.is_alive():
            self.scanner.close()
