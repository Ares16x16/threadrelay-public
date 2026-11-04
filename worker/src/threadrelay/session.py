"""Fail-closed, in-memory session authority; no collector or delivery transport.

All elapsed decisions use one injected monotonic clock. UTC values accepted by
can_transmit/begin_send are external timestamps, never elapsed-time authority.
Only trusted application code supplies setup and transport callbacks, not IPC.
"""
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timezone
import math
from threading import RLock
import time
from typing import Any, Protocol, TypeVar

from .contracts import validate_contract

SessionConfig = dict[str, Any]
SessionSnapshot = dict[str, Any]
T = TypeVar("T")


class Clock(Protocol):
    def monotonic(self) -> float: ...
    def now(self) -> datetime: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class SessionController:
    """Single synchronization boundary for intake, pause/stop and send starts.

    start is always an explicit *new* session, including from Paused: counters
    and the automatic budget reset, but this object never deletes queued data.
    Default setup denies running. Future setup must check feed/model/browser,
    destination and explicit auto-mode permission before returning allowed.
    """

    def __init__(
        self, *, clock: Clock | None = None,
        setup_check: Callable[[SessionConfig], dict[str, Any]] | None = None,
        cancel_intake: Callable[[], None] = lambda: None,
        recovered: bool = False,
        on_transition: Callable[[SessionSnapshot], None] | None = None,
    ):
        self._clock = clock or SystemClock()
        self._setup_check = setup_check or (lambda _: {"allowed": False, "reason": "setup_unavailable"})
        self._cancel_intake = cancel_intake
        self._gate_close_hooks = []
        self._on_transition = on_transition
        self._lock = RLock()
        self._snapshot = {"state": "needs_attention" if recovered else "stopped", "counters": {
            "observed": 0, "matched": 0, "queued": 0, "sent": 0,
        }, "attention_reason": "crash_recovery" if recovered else None}
        self._config: SessionConfig | None = None
        self._deadline: float | None = None
        self._last_monotonic = self._clock.monotonic()
        self._automatic_started = 0
        self._observation_active = False
        self._pending_start = None

    @property
    def authority_lock(self):
        """Shared backend lock for live account/network transitions."""
        return self._lock

    @property
    def snapshot(self) -> SessionSnapshot:
        with self._lock:
            return deepcopy(self._snapshot)

    def _transition(self, state: str, reason: str | None = None) -> None:
        changed = (state, reason) != (self._snapshot['state'], self._snapshot['attention_reason'])
        self._snapshot["state"] = state
        self._snapshot["attention_reason"] = reason
        if changed and self._on_transition is not None:
            try:
                self._on_transition(deepcopy(self._snapshot))
            except Exception:
                # Observation can never prevent stop/pause authority changes.
                pass

    def add_gate_close_hook(self, hook: Callable[[], None]) -> None:
        """Register trusted synchronous shutdown of an owned transport."""
        with self._lock:
            if hook in self._gate_close_hooks:
                return
            self._gate_close_hooks.append(hook)

    def _cancel(self) -> bool:
        self._pending_start = None
        self._observation_active = False
        failed = False
        for hook in [*self._gate_close_hooks, self._cancel_intake]:
            try:
                hook()
            except Exception:
                failed = True
        if failed:
            self._transition("failed", "intake_cancellation_failed")
            return False
        return True

    def _check_setup(self, selected):
        try:
            authorization = self._setup_check(deepcopy(selected))
            validate_contract("AuthorizationResult", authorization)
        except Exception:
            self._transition("failed", "setup_check_failed")
            return False
        if not authorization["allowed"]:
            self._transition("needs_attention", authorization["reason"] or "setup_unavailable")
            return False
        return True

    def begin_start(self, config: SessionConfig):
        """Reserve an opaque start generation; setup check must be quick/inert."""
        validate_contract("SessionConfig", config)
        selected = {"mode": "review", "duration_seconds": 1800, "max_posts": 100, "max_auto_deliveries": 10, **deepcopy(config)}
        with self._lock:
            if self._snapshot["state"] in {"starting", "running", "pausing", "stopping"}:
                raise ValueError("Session is already active")
            self._transition("starting")
            self._snapshot.pop("config", None)
            token = object()
            pending = (token, selected)
            self._pending_start = pending
            allowed = self._check_setup(selected)
            if self._pending_start is not pending:
                return None
            if not allowed or self._snapshot["state"] != "starting":
                self._pending_start = None
                return None
            return token

    def complete_start(self, token) -> SessionSnapshot:
        """Promote only the same uncancelled generation after preparation."""
        with self._lock:
            pending = self._pending_start
            if pending is None or pending[0] is not token or self._snapshot["state"] != "starting":
                return self.snapshot
            selected = pending[1]
            allowed = self._check_setup(selected)
            if self._pending_start is not pending:
                return self.snapshot
            if not allowed or self._snapshot["state"] != "starting":
                self._pending_start = None
                return self.snapshot
            self._pending_start = None
            now = self._clock.monotonic()
            if not math.isfinite(now) or now < self._last_monotonic:
                self._transition("needs_attention", "clock_invalid")
                return self.snapshot
            self._last_monotonic = now
            self._config, self._deadline = selected, now + selected["duration_seconds"]
            self._snapshot["config"] = deepcopy(selected)
            self._automatic_started = 0
            self._snapshot["counters"] = {"observed": 0, "matched": 0, "queued": 0, "sent": 0}
            self._transition("running")
            self._refresh()
            return self.snapshot

    def start(self, config: SessionConfig) -> SessionSnapshot:
        token = self.begin_start(config)
        return self.complete_start(token) if token is not None else self.snapshot

    def pause(self, reason: str | None = None) -> SessionSnapshot:
        with self._lock:
            if self._snapshot["state"] in {"running", "starting"}:
                self._transition("pausing", reason)
                if self._cancel():
                    self._transition("paused", reason)
            elif reason is None and self._snapshot['state'] in {'paused', 'stopped'}:
                # History delivery can run while collection is already paused
                # or stopped. An explicit global Pause must still revoke it.
                previous = self._snapshot['state']
                self._transition('pausing')
                if self._cancel():
                    self._transition(previous)
            return self.snapshot

    def suspend_collection(self) -> SessionSnapshot:
        """Revoke capture/model work without granting or resuming any sending."""
        return self.pause(reason='view_hidden')

    def stop(self) -> SessionSnapshot:
        with self._lock:
            self._transition("stopping")  # closes the gate before cancellation.
            if self._cancel():
                self._transition("stopped")
            self._deadline = None
            return self.snapshot

    def require_attention(self, reason: str) -> SessionSnapshot:
        with self._lock:
            self._transition("needs_attention", reason)
            self._cancel()
            return self.snapshot

    def fail(self, reason: str) -> SessionSnapshot:
        with self._lock:
            self._transition("failed", reason)
            self._cancel()
            return self.snapshot

    def browser_lost(self) -> SessionSnapshot:
        # No process launch/retry exists on this path.
        return self.pause("browser_lost")

    def system_sleep(self) -> SessionSnapshot:
        return self.pause("system_sleep")

    def system_wake(self) -> SessionSnapshot:
        return self.require_attention("system_wake")

    def _limit_reason(self) -> str | None:
        """Check elapsed/cap gates without running cancellation callbacks.

        Caller holds the lifecycle lock. The guarded send recheck must not call
        external cancellation while its configuration writer is still held.
        """
        now = self._clock.monotonic()
        if not math.isfinite(now) or now < self._last_monotonic:
            return "clock_invalid"
        self._last_monotonic = now
        if not self._config.get('continuous', False) and now >= self._deadline:
            return "duration_limit"
        if not self._config.get("continuous", False) and self._snapshot["counters"]["observed"] >= self._config["max_posts"] and not self._observation_active:
            return "post_limit"
        if self._config["mode"] == "auto" and not self._config.get("unlimited_auto_deliveries", False) and self._automatic_started >= self._config["max_auto_deliveries"]:
            return "automatic_delivery_limit"
        return None

    def _refresh(self, *, reason: str | None = None) -> None:
        if self._snapshot["state"] != "running":
            return
        reason = reason or self._limit_reason()
        if reason == "clock_invalid":
            self.require_attention(reason)
        elif reason is not None:
            self.pause(reason)

    def tick(self) -> SessionSnapshot:
        with self._lock:
            self._refresh()
            return self.snapshot

    def can_intake(self) -> bool:
        with self._lock:
            self._refresh()
            return self._snapshot["state"] == "running" and not self._observation_active

    def can_process_observation(self) -> bool:
        """Only the one admitted item may finish at the intake cap; Pause still cancels."""
        with self._lock:
            self._refresh()
            return self._snapshot["state"] == "running"

    def complete_observation(self):
        with self._lock:
            self._observation_active = False
            self._refresh()

    @staticmethod
    def _validate_timestamp(now: datetime) -> None:
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None or now.utcoffset().total_seconds() != 0:
            raise ValueError("Expected a timezone-aware UTC timestamp")

    def can_transmit(self, now: datetime, *, automatic: bool = False) -> bool:
        """Observational only: callers MUST use begin_send for transmission."""
        self._validate_timestamp(now)
        with self._lock:
            self._refresh()
            return (self._snapshot["state"] == "running" and self._config["mode"] != "dry_run"
                and (self._config.get("continuous", False) or self._snapshot["counters"]["observed"] < self._config["max_posts"])
                and (not automatic or self._config["mode"] == "auto"))

    def observe_post(self, *, defer_completion=False) -> bool:
        with self._lock:
            if not self.can_intake():
                return False
            self._snapshot["counters"]["observed"] += 1
            self._observation_active = defer_completion
            self._refresh()
            return True

    def record_counter(self, name: str, count: int = 1) -> None:
        # Observations must go through the intake cap. Sent is recorded only on
        # confirmed completion (possibly after pause), never on send reservation.
        if name not in {"matched", "queued", "sent"} or type(count) is not int or count < 0:
            raise ValueError("Invalid session counter")
        with self._lock:
            self._snapshot["counters"][name] += count

    def begin_send(
        self, now: datetime, *, automatic: bool,
        authorize: Callable[[], bool], start: Callable[[], T],
        release_authorization: Callable[[], None] = lambda: None,
    ) -> T | None:
        """Atomically reauthorize and initiate one send under the lifecycle lock.

        authorize must re-read current routes/destination/content/approval (Task
        6/7). start must synchronously initiate the request and return an in-flight
        handle/future, NOT merely enqueue a later call. Await completion outside
        this lock. A transport exception/unknown outcome still consumes an auto
        reservation: no automatic budget refund and no implicit retry here.
        An optional configuration guard opened by authorize must be released by
        release_authorization, still under this lifecycle lock, before refresh
        can invoke cancellation callbacks. Release runs on denial/exception too.
        """
        with self._lock:
            if not self.can_transmit(now):
                return None
            if automatic and self._config["mode"] != "auto":
                return None
            blocked_reason = None
            try:
                if not authorize():
                    return None
                # Recheck the exact same limits without invoking cancellation
                # while an authorization/configuration transaction may be held.
                if self._snapshot["state"] != "running" or self._config["mode"] == "dry_run":
                    return None
                blocked_reason = self._limit_reason()
                if not self._config.get("continuous", False) and self._snapshot["counters"]["observed"] >= self._config["max_posts"]:
                    blocked_reason = "post_limit"
                if blocked_reason is not None:
                    return None
                if automatic:
                    self._automatic_started += 1
                return start()
            finally:
                try:
                    release_authorization()
                except BaseException:
                    # No successful permit/handle may escape a failed guard
                    # release. The guard owner must close resources even when
                    # commit fails; cancellation now runs after that cleanup.
                    self.require_attention("authorization_release_failed")
                    raise
                finally:
                    # Preserve a detected invalid/expired gate even if cleanup
                    # changes the injected clock before callbacks can run.
                    self._refresh(reason=blocked_reason)


class ParentWatchdog:
    """Poll a stable parent-liveness source; loss is terminal, never a restart."""

    def __init__(self, controller: SessionController, *, parent_alive: Callable[[], bool], shutdown: Callable[[], None]):
        self._controller, self._parent_alive, self._shutdown = controller, parent_alive, shutdown
        self._lost = False

    def poll(self) -> bool:
        if self._lost:
            return False
        try:
            alive = self._parent_alive()
        except Exception:
            alive = False
        if not alive:
            self._lost = True
            try:
                self._controller.stop()
            finally:
                self._shutdown()
            return False
        self._controller.tick()
        return True


class ParentProcess:
    """Stable private-parent probe: held HANDLE on Windows, parent identity on Unix.

    The worker never accepts a renderer-provided PID. On Unix the kernel reparents
    an orphan, so getppid identity avoids PID-reuse authorization. On Windows an
    open synchronization handle continues to refer to that exact process object.
    """

    def __init__(self):
        import os
        self._closed = False
        self._pid = os.getppid()
        self._handle = None
        self._kernel = None
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.WaitForSingleObject.restype = wintypes.DWORD
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.restype = wintypes.BOOL
            handle = kernel.OpenProcess(0x00100000, False, self._pid)  # SYNCHRONIZE only.
            if not handle:
                raise OSError("Private parent unavailable")
            self._kernel, self._handle = kernel, handle

    def alive(self) -> bool:
        import os
        if self._closed:
            return False
        if self._kernel is not None:
            return self._kernel.WaitForSingleObject(self._handle, 0) == 0x102  # WAIT_TIMEOUT.
        return self._pid > 1 and os.getppid() == self._pid

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            if self._kernel is not None:
                self._kernel.CloseHandle(self._handle)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
