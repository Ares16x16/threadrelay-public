"""Generation-bound preparation owner; native capabilities remain separately gated.

Preparation and cleanup run on the same owner thread, never while the lifecycle
lock is held. The coordinator never turns a stale result into Running authority.
"""
from copy import deepcopy
from threading import Event, RLock, Thread

from .collector.browser import _feed_url
from .contracts import validate_contract
from .pipeline import PreparationFailure


class Coordinator:
    def __init__(self, session, *, prepare, on_running=lambda config, cancelled: None):
        self.session = session
        self.prepare = prepare
        self.on_running = on_running
        self._lock = RLock()
        self._owner = None
        self._cleanup_unconfirmed = False
        self._settled = Event()
        self._settled.set()

    def start(self, config):
        validate_contract('SessionConfig', config)
        _feed_url(config['feed_url'])
        with self._lock:
            if self._owner is not None or self._cleanup_unconfirmed:
                raise ValueError('Dependency cleanup pending')
            token = self.session.begin_start(config)
            if token is None:
                return self.session.snapshot
            cancel = Event()
            owner = (token, cancel, Event(), "session")
            self._owner = owner
            self._settled.clear()
            Thread(target=self._prepare_and_own, args=(owner, deepcopy(config)),
                   name='local-dependency-owner', daemon=True).start()
            return self.session.snapshot

    def _prepare_and_own(self, owner, config):
        lease = None
        failed = False
        stage = 'preparation'
        try:
            lease = self.prepare(config, owner[1])
            if lease is None or not callable(getattr(lease, 'close', None)):
                raise ValueError('Owned dependency lease unavailable')
            with self._lock:
                if self._owner is owner and not owner[1].is_set():
                    snapshot = self.session.complete_start(owner[0])
                    if snapshot['state'] != 'running':
                        owner[1].set()
                else:
                    owner[1].set()
            if not owner[1].is_set():
                stage = 'collection'
                self.on_running(config, owner[1])
            if not owner[1].is_set():
                run = getattr(lease, 'run', None)
                if callable(run):
                    run(owner[1])
                    with self._lock:
                        if self._owner is owner and not owner[1].is_set() and self.session.snapshot['state'] == 'running':
                            self.session.require_attention('collection_stopped')
                else:
                    owner[1].wait()
        except Exception as error:
            failed = not (isinstance(error, PreparationFailure) and error.cleanup_confirmed)
            if isinstance(error, PreparationFailure) and error.lease is not None:
                lease = error.lease
            with self._lock:
                if self._owner is owner and not owner[1].is_set():
                    reason = error.attention_reason if isinstance(error, PreparationFailure) else stage + '_failed'
                    self.session.require_attention(reason)
        finally:
            self._cleanup(owner, lease, failed)

    def _cleanup(self, owner, lease, failed):
        # Keep cleanup on the same owner thread when confirmation fails.
        while True:
            confirmed = lease is None and not failed
            if lease is not None:
                try:
                    report = lease.close()
                    confirmed = report == {'stopped': True, 'remaining': []}
                except Exception:
                    confirmed = False
            with self._lock:
                self._cleanup_unconfirmed = not confirmed
                if not confirmed:
                    self.session.require_attention('dependency_cleanup_unconfirmed')
                elif self._owner is owner:
                    self._owner = None
                self._settled.set()
            if confirmed:
                break
            owner[2].wait()
            owner[2].clear()

    def open_login(self, factory):
        """Queue explicit login on the sole browser owner; never block IPC."""
        with self._lock:
            if self._owner is not None or self._cleanup_unconfirmed:
                raise ValueError('Dependency cleanup pending')
            if self.session.snapshot['state'] in {'starting', 'running', 'pausing', 'stopping'}:
                raise ValueError('Session is active')
            owner = (object(), Event(), Event(), 'login')
            self._owner = owner
            self._settled.clear()
            Thread(target=self._login_and_own, args=(owner, factory),
                   name='local-dependency-owner', daemon=True).start()

    def _login_and_own(self, owner, factory):
        lease = None
        failed = False
        try:
            lease = factory()
            if not owner[1].is_set():
                lease.open()
            owner[1].wait()
        except Exception:
            with self._lock:
                if self._owner is owner and not owner[1].is_set():
                    self.session.require_attention('threads_login_unavailable')
        finally:
            self._cleanup(owner, lease, failed)

    def wait_cleanup(self, timeout):
        if not self._settled.wait(timeout):
            return False
        with self._lock:
            return not self._cleanup_unconfirmed

    def retry_cleanup(self, timeout):
        with self._lock:
            if self._owner is None:
                return not self._cleanup_unconfirmed
            if not self._cleanup_unconfirmed:
                return False
            self._settled.clear()
            self._owner[2].set()
        return self.wait_cleanup(timeout)

    def tick(self):
        with self._lock:
            snapshot = self.session.tick()
            if snapshot['state'] not in {'starting', 'running'} and self._owner is not None and self._owner[3] == 'session':
                self._owner[1].set()
            return snapshot

    def pause(self):
        with self._lock:
            snapshot = self.session.pause()
            if self._owner is not None:
                self._owner[1].set()
            return snapshot

    def suspend_collection(self):
        with self._lock:
            snapshot = self.session.suspend_collection()
            if self._owner is not None:
                self._owner[1].set()
            return snapshot

    def stop(self):
        with self._lock:
            snapshot = self.session.stop()
            if self._owner is not None:
                self._owner[1].set()
            return snapshot
