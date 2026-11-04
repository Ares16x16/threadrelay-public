"""Evaluation-only factory for the trusted host's owned inference reservation.

This is internal composition, not a CLI-selected module, path, process or URL.
The packaged host still must supply a real same-manager broker and native proof;
this module alone cannot issue a production capability or enable a benchmark.
"""
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from threading import Lock

from threadrelay.inference.decision import Classifier
from threadrelay.inference.provision import ModelProvisioner, load_catalog
from threadrelay.inference.runtime import DecisionRuntime, LoopbackClient, RuntimeUnavailable
from .runner import main as runner_main


class OwnedEvaluation:
    def __init__(self, root, broker, *, catalog=None, provisioner=None, client_factory=LoopbackClient):
        root = Path(root)
        if not root.is_absolute():
            raise ValueError('Trusted installation root required')
        self._root = root.resolve()
        self._catalog = deepcopy(load_catalog() if catalog is None else catalog)
        self._provisioner = provisioner or ModelProvisioner(catalog=self._catalog)
        self._broker, self._client_factory = broker, client_factory
        self._lock = Lock()
        self._active = False
        self._pending = None
        self._run_open = False
        self._run_reservation = None
        self._run_failed = False
        self.report_digest = None

    def begin_run(self):
        with self._lock:
            if self._run_open or self._active or self._pending is not None:
                raise RuntimeUnavailable('evaluation_owner_unavailable')
            self._run_open=True;self._run_failed=False
            self.report_digest = None
        reservation=None
        try:
            reservation=self._broker.reserve_evaluation()
            if reservation.purpose!='evaluation' or not reservation.active():raise RuntimeUnavailable('evaluation_reservation_unavailable')
            with self._lock:self._run_reservation=reservation
            return reservation
        except Exception:
            confirmed=reservation is None
            if reservation is not None:
                try: confirmed=reservation.release() is True
                except Exception: confirmed=False
            with self._lock:
                self._run_failed=True
                if not confirmed:self._pending=(None,reservation)
            return None

    def end_run(self):
        with self._lock:
            reservation=self._run_reservation
            self._run_reservation=None;self._run_open=False;self._run_failed=False
            pending=self._pending is not None
        if pending:return False
        if reservation is None:return True
        try:confirmed=reservation.release() is True
        except Exception:confirmed=False
        if not confirmed:
            with self._lock:self._pending=(None,reservation)
        return confirmed

    @contextmanager
    def factory(self, manifest):
        selected = next((r for r in self._catalog['models'] if r['manifest'] == manifest), None)
        if selected is None or Path(selected['filename']).name != selected['filename']:
            raise RuntimeUnavailable('evaluation_manifest_unrecognized')
        with self._lock:
            if self._active or self._pending is not None:
                raise RuntimeUnavailable('evaluation_owner_unavailable')
            if self._run_open and self._run_failed:
                raise RuntimeUnavailable('evaluation_reservation_unavailable')
            self._active = True
            reservation = self._run_reservation if self._run_open else None
        runtime = None
        try:
            # The outer packaged run reserves before the sampling window;
            # direct factory callers reserve here, never via user CLI input.
            if reservation is None:
                reservation = self._broker.reserve_evaluation()
            if reservation.purpose != 'evaluation' or not reservation.active():
                raise RuntimeUnavailable('evaluation_reservation_unavailable')
            runtime = DecisionRuntime(model_file=self._root / 'models' / selected['filename'],
                provisioner=self._provisioner, launcher=reservation.launcher,
                setup_authorized=reservation.active, can_infer=reservation.active,
                client_factory=self._client_factory)
            runtime.start(deepcopy(manifest))
            yield Classifier(runtime)
        finally:
            confirmed = True
            if runtime is not None:
                try:
                    confirmed = runtime.stop() == {'stopped': True, 'remaining': []}
                except Exception:
                    confirmed = False
            if confirmed and reservation is not None:
                try:
                    confirmed = reservation.release() is True
                except Exception:
                    confirmed = False
            with self._lock:
                self._active = False
                if self._run_reservation is reservation:
                    self._run_reservation = None
                if not confirmed:
                    self._pending = (runtime, reservation)
            if not confirmed:
                raise RuntimeUnavailable('evaluation_cleanup_unconfirmed')

    def retry_cleanup(self):
        with self._lock:
            if self._active:
                return False
            pending = self._pending
            if pending is None:
                return True
            self._active = True
        runtime, reservation = pending
        confirmed = False
        try:
            confirmed = runtime is None or runtime.stop() == {'stopped': True, 'remaining': []}
            if confirmed and reservation is not None:
                confirmed = reservation.release() is True
        except Exception:
            confirmed = False
        finally:
            with self._lock:
                self._active = False
                if confirmed:
                    self._pending = None
        return confirmed


def run_owned(argv, owner):
    """Packaged evaluation path, bound to one exclusive host reservation."""
    reservation=owner.begin_run()
    memory_factory=None
    hardware_provider=None
    if reservation is not None:
        make=getattr(owner._broker,'evaluation_memory_factory',None)
        if callable(make):
            try: memory_factory=make(reservation)
            except Exception: memory_factory=None
        make_hardware=getattr(owner._broker,'evaluation_hardware_provider',None)
        if callable(make_hardware):
            try:hardware_provider=make_hardware(reservation)
            except Exception:hardware_provider=None
    try:
        def capture(data):
            import hashlib
            owner.report_digest = hashlib.sha256(data).hexdigest()
        return runner_main(argv, runtime_factory=owner.factory, memory_factory=memory_factory, hardware_provider=hardware_provider, trusted_report=capture)
    finally:
        if not owner.end_run():
            raise RuntimeUnavailable('evaluation_cleanup_unconfirmed')
