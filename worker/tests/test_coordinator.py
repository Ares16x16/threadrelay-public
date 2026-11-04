"""Generation cancellation with deterministic synthetic dependency boundaries."""
from threading import Event, get_ident

from threadrelay.session import SessionController


def test_navigation_suspend_cancels_late_preparation_with_collection_only_reason():
    from threadrelay.coordinator import Coordinator
    entered, release, closed = Event(), Event(), Event()
    reasons, promoted = [], []
    class Lease:
        def close(self):
            closed.set()
            return {'stopped': True, 'remaining': []}
    def prepare(config, cancelled):
        entered.set()
        assert release.wait(2)
        assert cancelled.is_set()
        return Lease()
    session = SessionController(setup_check=lambda _: {'allowed': True, 'reason': None})
    session.add_gate_close_hook(lambda: reasons.append(session.snapshot['attention_reason']))
    owner = Coordinator(session, prepare=prepare, on_running=lambda *_: promoted.append(True))
    owner.start({'feed_url': 'https://www.threads.com/'})
    assert entered.wait(2)
    suspended = owner.suspend_collection()
    assert suspended['state'] == 'paused' and suspended['attention_reason'] == 'view_hidden'
    release.set()
    assert closed.wait(2) and owner.wait_cleanup(2)
    assert promoted == [] and reasons == ['view_hidden']
    assert session.snapshot['state'] == 'paused'


def test_pause_during_preparation_revokes_late_completion_and_cleans_on_owner_thread():
    from threadrelay.coordinator import Coordinator
    entered, release, closed = Event(), Event(), Event()
    threads = []
    class Lease:
        def close(self):
            threads.append(get_ident())
            closed.set()
            return {'stopped': True, 'remaining': []}
    def prepare(config, cancelled):
        threads.append(get_ident())
        entered.set()
        assert release.wait(2)
        return Lease()
    session = SessionController(setup_check=lambda _: {'allowed': True, 'reason': None})
    owner = Coordinator(session, prepare=prepare)
    assert owner.start({'feed_url': 'https://www.threads.com/'})['state'] == 'starting'
    assert entered.wait(2)
    assert owner.pause()['state'] == 'paused'
    release.set()
    assert closed.wait(2)
    assert session.snapshot['state'] == 'paused'
    assert threads[0] == threads[1]


def test_unconfirmed_cleanup_blocks_new_start_and_stopped_queries_stay_responsive():
    import pytest
    entered, release, closing, finished = Event(), Event(), Event(), Event()
    class Lease:
        def close(self):
            closing.set()
            assert release.wait(2)
            finished.set()
            return {'stopped': False, 'remaining': ['inference']}
    class Session(SessionController):
        def complete_start(self, token):
            result = super().complete_start(token)
            entered.set()
            return result
    session = Session(setup_check=lambda _: {'allowed': True, 'reason': None})
    from threadrelay.coordinator import Coordinator
    owner = Coordinator(session, prepare=lambda *_: Lease())
    owner.start({'feed_url': 'https://www.threads.com/'})
    assert entered.wait(2)
    assert owner.stop()['state'] == 'stopped'
    assert closing.wait(2)
    assert session.snapshot['state'] == 'stopped'
    with pytest.raises(ValueError):
        owner.start({'feed_url': 'https://www.threads.com/'})
    release.set()
    assert finished.wait(2)
    assert not owner.wait_cleanup(2)
    with pytest.raises(ValueError):
        owner.start({'feed_url': 'https://www.threads.com/'})
    assert session.snapshot['state'] == 'needs_attention'


def test_idle_tick_closes_owner_at_deadline():
    from datetime import datetime, timezone
    from threadrelay.coordinator import Coordinator
    class Clock:
        value = 0
        def monotonic(self): return self.value
        def now(self): return datetime.now(timezone.utc)
    clock = Clock()
    running, closed = Event(), Event()
    class Session(SessionController):
        def complete_start(self, token):
            result = super().complete_start(token); running.set(); return result
    class Lease:
        def close(self): closed.set(); return {'stopped': True, 'remaining': []}
    session = Session(clock=clock, setup_check=lambda _: {'allowed': True, 'reason': None})
    owner = Coordinator(session, prepare=lambda *_: Lease())
    owner.start({'feed_url': 'https://www.threads.com/', 'duration_seconds': 1})
    assert running.wait(2)
    clock.value = 1
    assert owner.tick()['state'] == 'paused'
    assert closed.wait(2)


def test_missing_lease_never_promotes_to_running():
    from threadrelay.coordinator import Coordinator
    done = Event()
    class Session(SessionController):
        def require_attention(self, reason):
            result = super().require_attention(reason); done.set(); return result
    session = Session(setup_check=lambda _: {'allowed': True, 'reason': None})
    owner = Coordinator(session, prepare=lambda *_: None)
    owner.start({'feed_url': 'https://www.threads.com/'})
    assert done.wait(2)
    assert session.snapshot['state'] != 'running'
    assert not owner.wait_cleanup(2)


def test_schema_valid_but_unapproved_feed_never_reaches_preparation():
    import pytest
    from threadrelay.coordinator import Coordinator
    prepared = []
    session = SessionController(setup_check=lambda _: {'allowed': True, 'reason': None})
    owner = Coordinator(session, prepare=lambda *args: prepared.append(args))
    with pytest.raises(ValueError):
        owner.start({'feed_url': 'https://www.threads.com/arbitrary/path'})
    assert not prepared
    assert session.snapshot['state'] == 'stopped'


def test_owned_work_runs_and_closes_on_same_thread_and_early_exit_revokes_session():
    from threadrelay.coordinator import Coordinator
    finished = Event(); thread_ids = []
    class Lease:
        def run(self, cancelled): thread_ids.append(get_ident())
        def close(self): thread_ids.append(get_ident()); finished.set(); return {'stopped': True, 'remaining': []}
    session = SessionController(setup_check=lambda _: {'allowed': True, 'reason': None})
    owner = Coordinator(session, prepare=lambda *_: Lease())
    owner.start({'feed_url': 'https://www.threads.com/'})
    assert finished.wait(2) and owner.wait_cleanup(2)
    assert thread_ids[0] == thread_ids[1]
    assert session.snapshot['state'] == 'needs_attention'
    assert session.snapshot['attention_reason'] == 'collection_stopped'


def test_unconfirmed_lease_remains_owned_for_same_thread_cleanup_retry():
    from threadrelay.coordinator import Coordinator
    ready, first_close = Event(), Event(); threads = []
    class Lease:
        def close(self):
            threads.append(get_ident()); first_close.set()
            return {'stopped':len(threads)>1, 'remaining':[] if len(threads)>1 else ['browser']}
    class Session(SessionController):
        def complete_start(self, token):
            result = super().complete_start(token); ready.set(); return result
    session = Session(setup_check=lambda _: {'allowed':True,'reason':None})
    owner = Coordinator(session, prepare=lambda *_: Lease())
    owner.start({'feed_url':'https://www.threads.com/'}); assert ready.wait(2)
    owner.stop(); assert first_close.wait(2) and not owner.wait_cleanup(2)
    assert owner.retry_cleanup(2)
    assert len(threads) == 2 and threads[0] == threads[1]


def test_failed_preparation_retains_lease_for_same_thread_cleanup_retry():
    from threadrelay.coordinator import Coordinator
    from threadrelay.pipeline import PreparationFailure
    close = Event(); ids = []
    class Lease:
        def close(self):
            ids.append(get_ident()); close.set()
            return {'stopped':len(ids)>1, 'remaining':[] if len(ids)>1 else ['browser']}
    lease = Lease()
    def prepare(*_):
        raise PreparationFailure(cleanup_confirmed=False, lease=lease)
    session = SessionController(setup_check=lambda _: {'allowed':True,'reason':None})
    owner = Coordinator(session, prepare=prepare)
    owner.start({'feed_url':'https://www.threads.com/'})
    assert close.wait(2) and not owner.wait_cleanup(2)
    assert owner.retry_cleanup(2)
    assert len(ids) == 2 and ids[0] == ids[1]


def test_preparation_stage_is_preserved_after_confirmed_cleanup():
    from threadrelay.coordinator import Coordinator
    from threadrelay.pipeline import PreparationFailure
    for stage in ('browser', 'model'):
        def prepare(*_, stage=stage):
            raise PreparationFailure(cleanup_confirmed=True, stage=stage)
        session = SessionController(setup_check=lambda _: {'allowed':True,'reason':None})
        owner = Coordinator(session, prepare=prepare)
        owner.start({'feed_url':'https://www.threads.com/'})
        assert owner.wait_cleanup(2)
        assert session.snapshot['state'] == 'needs_attention'
        assert session.snapshot['attention_reason'] == stage + '_preparation_failed'


def test_running_collection_error_is_not_reported_as_preparation_failure():
    from threadrelay.coordinator import Coordinator
    class Lease:
        def run(self, _): raise RuntimeError('synthetic private exception text')
        def close(self): return {'stopped':True,'remaining':[]}
    session = SessionController(setup_check=lambda _: {'allowed':True,'reason':None})
    owner = Coordinator(session, prepare=lambda *_:Lease())
    owner.start({'feed_url':'https://www.threads.com/'})
    assert owner.wait_cleanup(2)
    assert session.snapshot['attention_reason'] == 'collection_failed'
