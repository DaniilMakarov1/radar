from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(max(0.0, float(seconds)))


class FakeClock:
    def __init__(self, start: datetime, *, monotonic_start: float = 0.0) -> None:
        self._now = start if start.tzinfo else start.replace(tzinfo=UTC)
        self._monotonic = float(monotonic_start)

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        delta = max(0.0, float(seconds))
        self._now = self._now + timedelta(seconds=delta)
        self._monotonic += delta
