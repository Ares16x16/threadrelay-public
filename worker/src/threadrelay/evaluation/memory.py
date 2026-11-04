"""Trusted scoped process-RSS observations, never heap/lifetime maxima or PID scans.

The native host binds observer to this worker and the exact live supervisor-owned
runtime generation (including all its registered process groups). The observer
must return bounded, local, current RSS reads; arbitrary IDs/paths are not CLI
inputs. Sampling gives a lower bound, not an exact OS high-water mark. Accelerator
memory is separate, because RSS and accelerator/unified allocations may overlap.
"""
from dataclasses import dataclass
import math
from threading import Event, Lock, Thread
from typing import Callable, Protocol


@dataclass(frozen=True)
class MemoryObservation:
    worker_rss_bytes: int | None
    owned_runtime_rss_bytes: int | None
    accelerator_bytes: int | None = None


@dataclass(frozen=True)
class MemoryScope:
    covers_worker: bool
    covers_all_owned_runtime_processes: bool
    accelerator_mode: str  # cpu_only | separate | unavailable

    def __post_init__(self):
        if (type(self.covers_worker) is not bool or type(self.covers_all_owned_runtime_processes) is not bool
                or self.accelerator_mode not in ("cpu_only", "separate", "unavailable")):
            raise ValueError("invalid_memory_scope")


class SamplingHandle(Protocol):
    def stop(self) -> bool: ...


class SamplingScheduler(Protocol):
    def start(self, callback: Callable[[], None], interval_seconds: float) -> SamplingHandle: ...


class _ThreadHandle:
    def __init__(self, callback, interval):
        self._stop = Event()
        def run():
            while not self._stop.is_set():
                callback()
                if self._stop.wait(interval):
                    break
        self._thread = Thread(target=run, name="threadrelay-scoped-memory", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=.25)
        return not self._thread.is_alive()


class _ThreadScheduler:
    def start(self, callback, interval_seconds):
        return _ThreadHandle(callback, interval_seconds)


def unavailable_memory(reason="trusted_scoped_memory_observer_unavailable"):
    return {"peak_memory_bytes": None, "peak_memory_unavailable_reason": reason,
        "memory_measurement": {"method": "sampled_simultaneous_process_RSS_peak_lower_bound",
            "scope": "unavailable", "window": "unavailable", "interval_seconds": None,
            "sample_count": 0, "successful_sample_count": 0, "worker_coverage": False,
            "owned_runtime_coverage": False, "peak_worker_rss_bytes": None,
            "peak_owned_runtime_rss_bytes": None, "accelerator_coverage": "unavailable",
            "peak_accelerator_memory_bytes": None,
            "limitations": "No scoped observer. Never substitute Python heap or process-lifetime maxima."}}


class ScopedMemorySampler:
    """Single-use, constant-space collector with bounded scheduler shutdown.

    Scheduler is injectable to make cadence deterministic, not a unit-test sleep.
    Missing/failed process samples invalidate the aggregate rather than passing a
    partial sample as worker+runtime peak. Observer exceptions are redacted. An
    unconfirmed sampler shutdown invalidates the result; late samples are ignored.
    """
    def __init__(self, observer: Callable[[], MemoryObservation], *, scope: MemoryScope,
                 interval_seconds: float = .05, scheduler: SamplingScheduler | None = None,
                 window="classification_calls"):
        if (type(interval_seconds) not in (int, float) or not math.isfinite(interval_seconds)
                or not .01 <= interval_seconds <= 1
                or window not in ("classification_calls", "cold_preparation_classification_cleanup")):
            raise ValueError("invalid_memory_sampling_config")
        self._observer, self._scope = observer, scope
        self._interval, self._scheduler, self._window = interval_seconds, scheduler or _ThreadScheduler(), window
        self._lock, self._in_sample = Lock(), Lock()
        self._entered, self._closed, self._handle = False, False, None
        self._samples = self._successful = 0
        self._worker = self._runtime = self._total = self._accelerator = None
        self._reason = None if scope.covers_worker and scope.covers_all_owned_runtime_processes else "incomplete_owned_process_scope"
        self._accelerator_missing = scope.accelerator_mode == "unavailable"

    @property
    def window(self):
        return self._window

    def _sample(self):
        if not self._in_sample.acquire(blocking=False):
            return
        try:
            # Atomic admission follows acquisition of the sampling gate. A
            # callback paused before that acquisition cannot pass a stale
            # closed check. An admitted observer is now in flight; never hold
            # the state lock during its bounded native read, so close remains
            # bounded and can invalidate a late result without deadlocking.
            with self._lock:
                if self._closed:
                    return
            try:
                observation = self._observer()
                if not isinstance(observation, MemoryObservation):
                    raise ValueError("invalid")
                values = (observation.worker_rss_bytes, observation.owned_runtime_rss_bytes, observation.accelerator_bytes)
                if any(v is not None and (type(v) is not int or v < 0) for v in values):
                    raise ValueError("invalid")
                if self._scope.accelerator_mode == "cpu_only" and observation.accelerator_bytes is not None:
                    raise ValueError("invalid")
                reason = "incomplete_process_RSS_observation" if None in values[:2] else None
            except Exception:
                observation, reason = None, "invalid_memory_observation"
            with self._lock:
                if self._closed:
                    return
                self._samples += 1
                self._reason = self._reason or reason
                if observation is None:
                    return
                worker, runtime, accelerator = (observation.worker_rss_bytes,
                    observation.owned_runtime_rss_bytes, observation.accelerator_bytes)
                if worker is not None:
                    self._worker = max(self._worker or 0, worker)
                if runtime is not None:
                    self._runtime = max(self._runtime or 0, runtime)
                if reason is None:
                    self._successful += 1
                    self._total = max(self._total or 0, worker + runtime)
                if self._scope.accelerator_mode == "separate":
                    if accelerator is None:
                        self._accelerator_missing = True
                    else:
                        self._accelerator = max(self._accelerator or 0, accelerator)
        finally:
            self._in_sample.release()

    def __enter__(self):
        if self._entered:
            raise ValueError("memory_sampler_is_single_use")
        self._entered = True
        try:
            self._handle = self._scheduler.start(self._sample, self._interval)
        except Exception:
            self._reason = "memory_scheduler_unavailable"
        return self

    def __exit__(self, *args):
        try:
            stopped = self._handle is not None and self._handle.stop() is True
        except Exception:
            stopped = False
        with self._lock:
            self._closed = True
            if not stopped:
                self._reason = self._reason or "memory_sampler_shutdown_unconfirmed"

    def result(self):
        with self._lock:
            if not self._closed:
                raise ValueError("memory_scope_not_finished")
            reason = self._reason or ("no_scoped_memory_samples" if self._total is None else None)
            accelerator_coverage = ("not_applicable_cpu_only" if self._scope.accelerator_mode == "cpu_only"
                else "unavailable" if self._accelerator_missing or self._accelerator is None else "separately_observed_not_added_to_RSS")
            return {"peak_memory_bytes": self._total if reason is None else None,
                "peak_memory_unavailable_reason": reason,
                "memory_measurement": {"method": "sampled_simultaneous_process_RSS_peak_lower_bound",
                    "scope": "worker_and_all_owned_runtime_processes" if self._scope.covers_worker and self._scope.covers_all_owned_runtime_processes else "partial_owned_process_scope",
                    "window": self._window, "interval_seconds": self._interval,
                    "sample_count": self._samples, "successful_sample_count": self._successful,
                    "worker_coverage": self._scope.covers_worker,
                    "owned_runtime_coverage": self._scope.covers_all_owned_runtime_processes,
                    "peak_worker_rss_bytes": self._worker, "peak_owned_runtime_rss_bytes": self._runtime,
                    "accelerator_coverage": accelerator_coverage,
                    "peak_accelerator_memory_bytes": self._accelerator if accelerator_coverage == "separately_observed_not_added_to_RSS" else None,
                    "limitations": "Sampled lower bound; short-lived peaks may be missed. Component maxima need not be simultaneous. Shared RSS pages may be counted per process; accelerator/unified memory is not added to RSS."}}
