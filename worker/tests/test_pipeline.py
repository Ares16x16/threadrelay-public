"""Real routing and SQLite over a synthetic decision transport; no service proof."""
from threading import Event, Thread
from test_routing import env as env, running, ScoresRuntime
from threadrelay.inference.decision import Classifier
from threadrelay.storage.posts import PostStore


class NoSend:
    def __init__(self): self.calls = 0
    def start_next(self, worker_id): self.calls += 1


def test_review_pipeline_creates_review_receipt_not_delivery(env):
    from threadrelay.pipeline import Pipeline
    session = running('review')
    sender = NoSend()
    pipeline = Pipeline(session, Event(), PostStore(env.db), env.config, env.classifier, env.router, sender)
    pipeline.process(env.post)
    with env.db.connection() as con:
        assert con.execute('SELECT COUNT(*) FROM routing_plans').fetchone()[0] == 1
        assert con.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0] == 0
    assert session.snapshot['counters']['observed'] == 1
    assert session.snapshot['counters']['matched'] == 1


def test_dry_run_never_creates_plan_delivery_or_send(env):
    from threadrelay.pipeline import Pipeline
    session = running('dry_run'); sender = NoSend()
    pipeline = Pipeline(session, Event(), PostStore(env.db), env.config, env.classifier, env.router, sender)
    pipeline.process(env.post)
    with env.db.connection() as con:
        assert con.execute('SELECT COUNT(*) FROM routing_plans').fetchone()[0] == 0
        assert con.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0] == 0
    assert sender.calls == 0


def test_pause_during_inference_drops_late_result_before_planning(env):
    from threadrelay.pipeline import Pipeline
    entered, release = Event(), Event()
    class Slow(ScoresRuntime):
        def decide(self, state, questions):
            entered.set(); assert release.wait(2)
            return super().decide(state, questions)
    classifier = Classifier(Slow()); env.router.classifier = classifier
    session = running('review'); sender = NoSend()
    pipeline = Pipeline(session, Event(), PostStore(env.db), env.config, classifier, env.router, sender)
    thread = Thread(target=pipeline.process, args=(env.post,)); thread.start()
    assert entered.wait(2)
    assert session.pause()['state'] == 'paused'
    release.set(); thread.join(2); assert not thread.is_alive()
    with env.db.connection() as con:
        assert con.execute('SELECT COUNT(*) FROM routing_plans').fetchone()[0] == 0
    assert sender.calls == 0


def test_auto_pipeline_counts_only_new_staging_and_respects_current_gate(env):
    from threadrelay.pipeline import Pipeline
    from test_routing import route
    env.config.save_route(route(version=2, mode='auto'))
    session = running('auto'); sender = NoSend()
    pipeline = Pipeline(session, Event(), PostStore(env.db), env.config, env.classifier, env.router, sender)
    pipeline.process(env.post); pipeline.process(env.post)
    with env.db.connection() as con:
        assert con.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0] == 1
    assert session.snapshot['counters']['queued'] == 1


def test_auto_gate_revoked_after_plan_prevents_staging(env):
    from threadrelay.pipeline import Pipeline
    from test_routing import route
    env.config.save_route(route(version=2, mode='auto'))
    calls = iter([True, False])
    env.router.auto_send_authorized = lambda: next(calls, False)
    session = running('auto'); sender = NoSend()
    Pipeline(session, Event(), PostStore(env.db), env.config, env.classifier, env.router, sender).process(env.post)
    with env.db.connection() as con:
        assert con.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0] == 0
    assert session.snapshot['counters']['queued'] == 0


def test_prepared_session_runs_and_closes_browser_and_runtime_on_owner_thread():
    from threadrelay.pipeline import PreparedSession
    ids = []
    from threading import get_ident
    cancel = Event()
    class Browser:
        def observe(self): ids.append(('observe',get_ident())); return ['synthetic-observation']
        def scroll_next(self): ids.append(('scroll',get_ident()))
        def close(self): ids.append(('browser-close',get_ident()))
    class Runtime:
        def stop(self): ids.append(('runtime-close',get_ident())); return {'stopped':True,'remaining':[]}
    class Work:
        def process(self, post): ids.append(('process',get_ident())); cancel.set()
    class Session:
        def can_intake(self): return True
    owned = PreparedSession(Browser(), Runtime(), Work(), Session())
    owned.run(cancel)
    assert owned.close() == {'stopped':True,'remaining':[]}
    assert {thread_id for _,thread_id in ids} == {get_ident()}
    assert [name for name,_ in ids] == ['observe','process','browser-close','runtime-close']


def test_preparation_uses_actual_owner_and_cleans_after_failed_runtime_start():
    from threadrelay.pipeline import SessionPreparation, PreparationFailure
    cancel = Event(); calls = []
    class Browser:
        def open(self, path): calls.append(('open',path))
        def close(self): calls.append('browser-close')
    class Runtime:
        def start(self, manifest, *, cancellation=None): calls.append('runtime-start'); raise RuntimeError('synthetic')
        def stop(self): calls.append('runtime-stop'); return {'stopped':True,'remaining':[]}
    class Session:
        def can_intake(self): return False
    browser = Browser()
    preparation = SessionPreparation(browser_factory=lambda _:browser, runtime=Runtime(), manifest={'id':'trusted'},
        profile=__import__('pathlib').Path('/synthetic/profile'), pipeline_factory=lambda _:None, session=Session())
    import pytest
    with pytest.raises(PreparationFailure) as result:
        preparation({'feed_url':'https://www.threads.com/'}, cancel)
    assert result.value.cleanup_confirmed
    assert result.value.attention_reason == 'model_preparation_failed'
    assert calls[-2:] == ['browser-close','runtime-stop']


def test_failed_browser_open_reports_only_a_static_stage_and_cleans():
    import pytest
    from threadrelay.pipeline import PreparationFailure, SessionPreparation
    calls = []
    class Browser:
        def open(self, _): raise RuntimeError('synthetic private exception text')
        def close(self): calls.append('browser-close')
    class Runtime:
        def start(self, *_args, **_kwargs): pytest.fail('model must not start after browser failure')
        def stop(self): calls.append('runtime-stop'); return {'stopped':True,'remaining':[]}
    preparation = SessionPreparation(browser_factory=lambda _:Browser(), runtime=Runtime(), manifest={},
        profile=__import__('pathlib').Path('/synthetic/profile'), pipeline_factory=lambda _:None, session=None)
    with pytest.raises(PreparationFailure) as result:
        preparation({}, Event())
    assert result.value.attention_reason == 'browser_preparation_failed'
    assert result.value.cleanup_confirmed
    assert 'private exception text' not in str(result.value)
    assert calls == ['browser-close', 'runtime-stop']


def test_prepared_session_waits_during_recovery_and_keeps_original_duration_limit():
    from types import SimpleNamespace
    from threadrelay.pipeline import PreparedSession
    from test_session import Clock, configured, config
    clock = Clock()
    session = configured(clock)
    session.start(config(mode='dry_run',duration_seconds=3))
    calls, waits, events = [], [], []
    class Cancel:
        def is_set(self): return False
        def wait(self, seconds): waits.append(seconds); clock.advance(seconds)
    class Browser:
        def recovery_delay(self): return 15
        def recovery_status(self): return {'reason':'unknown_layout','attempt':1,'retry_in_seconds':15}
        def observe(self): calls.append('observe'); return []
        def scroll_next(self): calls.append('scroll')
    work = SimpleNamespace(on_activity=lambda kind,**values:events.append((kind,values['detail'])),process=lambda post:None)
    owned = PreparedSession(Browser(),None,work,session)
    owned.run(Cancel())
    assert calls == []
    assert waits == [1,1,1]
    assert events == [('collection_recovering','unknown_layout:1:15')]
    assert session.snapshot['state'] == 'paused' and session.snapshot['attention_reason'] == 'duration_limit'
    assert session.snapshot['counters'] == {'observed':0,'matched':0,'queued':0,'sent':0}


def test_prepared_session_recovery_resumes_same_lease_and_reports_each_schedule_once(tmp_path):
    from types import SimpleNamespace
    from threadrelay.pipeline import PreparedSession
    from threadrelay.collector.embedded import EmbeddedBrowserSession
    from test_embedded_collector import Broker, snapshot
    from test_session import Clock, configured, config
    clock = Clock()
    session = configured(clock)
    session.start(config(mode='dry_run',duration_seconds=30))
    deadline = session._deadline
    broker = Broker()
    broker.lease.frames = [snapshot(state='unknown_layout',cards=[]),snapshot(state='unknown_layout',cards=[]),snapshot()]
    browser = EmbeddedBrowserSession(app_data_dir=tmp_path,broker=broker,can_intake=session.can_intake,on_attention=session.require_attention,monotonic=clock.monotonic)
    browser.open(tmp_path/'browser-profiles/threads')
    waits, events, posts = [], [], []
    cancel = Event()
    class Cancel:
        def is_set(self): return cancel.is_set()
        def wait(self, seconds): waits.append(seconds); clock.advance(seconds)
    def process(post):
        posts.append(post)
        assert session.observe_post()
        cancel.set()
    work = SimpleNamespace(on_activity=lambda kind,**values:events.append((kind,values['detail'])),process=process)
    PreparedSession(browser,None,work,session).run(Cancel())
    assert [post['post_id'] for post in posts] == ['threads:Code_1']
    assert waits == [1,.25]
    assert [event for event in events if event[0].startswith('collection_')] == [
        ('collection_recovering','unknown_layout:1:1'),
        ('collection_recovered','validated_post'),
    ]
    assert broker.attaches == 1 and broker.lease.closed == 0
    assert broker.lease.calls == ['snapshot','snapshot','snapshot','scroll','snapshot']
    assert session._deadline == deadline and session.snapshot['counters']['observed'] == 1
    browser.close()


def test_prepared_session_stop_interrupts_recovery_wait_promptly():
    from types import SimpleNamespace
    from time import monotonic
    from threadrelay.pipeline import PreparedSession
    from test_session import configured, config
    session = configured()
    session.start(config(mode='dry_run'))
    entered, cancel = Event(), Event()
    calls = []
    class Cancel:
        def is_set(self): return cancel.is_set()
        def wait(self, seconds): entered.set(); return cancel.wait(seconds)
    class Browser:
        def recovery_delay(self): return 15
        def observe(self): calls.append('observe'); return []
        def scroll_next(self): calls.append('scroll')
    def own():
        PreparedSession(Browser(),None,SimpleNamespace(process=lambda post:None),session).run(Cancel())
    thread = Thread(target=own)
    thread.start()
    try:
        assert entered.wait(1)
        before = monotonic()
        session.stop()
        cancel.set()
        thread.join(.5)
        assert not thread.is_alive() and monotonic()-before < .5
        assert calls == []
    finally:
        cancel.set()
        thread.join(1)


def test_prepared_session_ownership_loss_during_recovery_is_not_reported_as_recovered(tmp_path):
    from types import SimpleNamespace
    from threadrelay.pipeline import PreparedSession
    from threadrelay.collector.embedded import EmbeddedBrowserSession
    from test_embedded_collector import Broker, snapshot
    from test_session import Clock, configured, config
    clock = Clock()
    session = configured(clock)
    session.start(config(mode='dry_run'))
    broker, events = Broker(), []
    broker.lease.frames = [snapshot(state='unknown_layout',cards=[])]
    browser = EmbeddedBrowserSession(app_data_dir=tmp_path,broker=broker,can_intake=session.can_intake,on_attention=session.require_attention,monotonic=clock.monotonic)
    browser.open(tmp_path/'browser-profiles/threads')
    class Cancel:
        def is_set(self): return False
        def wait(self, seconds):
            clock.advance(seconds)
            broker.lease.status = 'ownership_lost'
    work = SimpleNamespace(on_activity=lambda kind,**values:events.append(kind),process=lambda post:None)
    PreparedSession(browser,None,work,session).run(Cancel())
    assert 'collection_recovering' in events and 'collection_recovered' not in events
    assert session.snapshot['attention_reason'] == 'browser_ownership_lost'
    assert broker.lease.calls == ['snapshot'] and broker.attaches == 1
    browser.close()
