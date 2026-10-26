"""Concurrent data readers with short, nonblocking maintenance windows."""
from contextlib import contextmanager
from functools import wraps
import threading


class DataAccessGate:
    def __init__(self):
        self.condition = threading.Condition()
        self.readers = 0
        self.writing = False

    @contextmanager
    def read(self):
        with self.condition:
            self.condition.wait_for(lambda: not self.writing)
            self.readers += 1
        try:
            yield
        finally:
            with self.condition:
                self.readers -= 1
                self.condition.notify_all()

    @contextmanager
    def maintenance(self):
        with self.condition:
            acquired = not self.readers and not self.writing
            if acquired:
                self.writing = True
        try:
            yield acquired
        finally:
            if acquired:
                with self.condition:
                    self.writing = False
                    self.condition.notify_all()

    def protect(self, function):
        @wraps(function)
        def guarded(*args, **kwargs):
            with self.read():
                return function(*args, **kwargs)
        return guarded


DATA_ACCESS = DataAccessGate()
