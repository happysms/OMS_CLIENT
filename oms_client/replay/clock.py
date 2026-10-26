"""Wall time changes presentation speed, never market-event timestamps."""
import time


class PlaybackClock:
    def __init__(self, start_ms, speed=1, monotonic=time.monotonic):
        self.monotonic = monotonic
        self.anchor_ms = float(start_ms)
        self.anchor_wall = monotonic()
        if type(speed) is not int or not 1 <= speed <= 32:
            raise ValueError('배속은 1~32의 정수로 선택해 주세요.')
        self.speed = speed
        self.paused = False

    def now(self):
        return self.anchor_ms if self.paused else self.anchor_ms + (self.monotonic() - self.anchor_wall) * 1000 * self.speed

    def set(self, *, speed=None, paused=None):
        current = self.now()
        if speed is not None:
            if type(speed) is not int or not 1 <= speed <= 32:
                raise ValueError('배속은 1~32의 정수로 선택해 주세요.')
            self.speed = speed
        if paused is not None:
            self.paused = paused
        self.anchor_ms, self.anchor_wall = current, self.monotonic()

    def seek_anchor(self, virtual_ms):
        self.anchor_ms, self.anchor_wall = float(virtual_ms), self.monotonic()
