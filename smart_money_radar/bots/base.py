from __future__ import annotations

import signal
import time
from types import FrameType
from typing import Any

from smart_money_radar.notifications import TelegramNotifier


class BaseBot:
    """Common bot lifecycle: run loop, signal handling, graceful shutdown.

    Subclasses implement ``run_iteration()``, ``next_sleep_seconds()``,
    and the lifecycle message hooks.  Everything else — signal handlers,
    interruptible sleep, crash/stop notification — is shared.
    """

    def __init__(
        self,
        *,
        iterations: int | None = None,
        telegram_enabled: bool = True,
        notifier: TelegramNotifier | None = None,
    ) -> None:
        self.iterations = iterations
        self.telegram_enabled = telegram_enabled
        self.notifier = notifier or TelegramNotifier()
        self.stop_requested = False
        self.stop_reason: str | None = None
        self.shutdown_notified = False
        self.last_iteration_result: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # Subclass interface
    # ------------------------------------------------------------------

    def run_iteration(self) -> dict[str, Any]:
        raise NotImplementedError

    def next_sleep_seconds(self, result: dict[str, Any]) -> int:
        raise NotImplementedError

    def start_message(self) -> str | None:
        return None

    def stop_message(self) -> str | None:
        return None

    def crash_message(self, exc: Exception) -> str | None:
        return None

    def on_iteration_error(self, exc: Exception) -> None:
        """Called when ``run_iteration()`` raises.  Default: notify + re-raise."""

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run_loop(self) -> None:
        completed = 0
        previous_handlers = self.install_signal_handlers()
        try:
            start_msg = self.start_message()
            if start_msg:
                self.notify(start_msg)
            while (
                not self.stop_requested
                and (self.iterations is None or completed < self.iterations)
            ):
                try:
                    result = self.run_iteration()
                except Exception as exc:
                    crash_msg = self.crash_message(exc)
                    if crash_msg:
                        self.notify(crash_msg)
                    self.on_iteration_error(exc)
                    raise
                self.last_iteration_result = result
                completed += 1
                if self.iterations is not None and completed >= self.iterations:
                    break
                self.sleep_interruptibly(self.next_sleep_seconds(result))
            if self.stop_requested:
                stop_msg = self.stop_message()
                if stop_msg:
                    self.notify_shutdown(stop_msg)
        except KeyboardInterrupt:
            self.request_stop("KeyboardInterrupt")
            stop_msg = self.stop_message()
            if stop_msg:
                self.notify_shutdown(stop_msg)
            raise
        finally:
            self.restore_signal_handlers(previous_handlers)

    # ------------------------------------------------------------------
    # Stop / signal handling
    # ------------------------------------------------------------------

    def request_stop(self, reason: str) -> None:
        self.stop_requested = True
        self.stop_reason = self.stop_reason or reason

    def install_signal_handlers(self) -> dict[int, Any]:
        handlers: dict[int, Any] = {}
        for stop_signal in (signal.SIGINT, signal.SIGTERM):
            try:
                handlers[int(stop_signal)] = signal.getsignal(stop_signal)
                signal.signal(stop_signal, self._handle_stop_signal)
            except (ValueError, OSError):
                continue
        return handlers

    def restore_signal_handlers(self, handlers: dict[int, Any]) -> None:
        for signum, handler in handlers.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                continue

    def _handle_stop_signal(self, signum: int, frame: FrameType | None) -> None:
        reason = signal.Signals(signum).name
        if self.stop_requested:
            raise KeyboardInterrupt
        self.request_stop(reason)

    def sleep_interruptibly(self, seconds: int) -> None:
        deadline = time.monotonic() + max(0, int(seconds))
        while not self.stop_requested:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(1.0, remaining))

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    def notify(self, text: str) -> None:
        print(text, flush=True)
        if not self.telegram_enabled:
            return
        result = self.notifier.send(text)
        if result.status != "sent":
            print(f"[telegram {result.status}] {result.error or ''}", flush=True)

    def notify_shutdown(self, text: str) -> None:
        if self.shutdown_notified:
            return
        self.shutdown_notified = True
        self.notify(text)
