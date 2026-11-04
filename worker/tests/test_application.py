"""Offline composition uses the real stores and no native service."""
from threadrelay.main import dispatch


def command(kind, payload=None):
    return {'version': 1, 'id': 'local', 'type': kind, 'payload': payload or {}}


def test_application_persists_local_features_without_native_activation(tmp_path):
    from threadrelay.application import Application
    topic = {'id': 'local', 'version': 1, 'description': 'Local topic',
             'positive_examples': ['yes'], 'negative_examples': ['no'], 'exclusions': [], 'enabled': True}
    with Application(tmp_path) as app:
        assert dispatch(command('QueryTopics'), controller=app.session, features=app.features)['payload'] == []
        assert dispatch(command('QueryReview'), controller=app.session, features=app.features)['payload'] == []
        assert dispatch(command('QueryHistory'), controller=app.session, features=app.features)['payload'] == []
        assert dispatch(command('QueryAccount'), controller=app.session, features=app.features)['payload']['snapshot']['auth_state'] == 'logged_out'
        assert dispatch(command('SaveTopic', topic), controller=app.session, features=app.features)['type'] == 'Acknowledged'
        assert app.telegram._binding is None
        assert app.runtime._lease is None
    with Application(tmp_path) as app:
        assert dispatch(command('QueryTopics'), controller=app.session, features=app.features)['payload'] == [topic]
        assert app.telegram._binding is None
        assert app.runtime._lease is None


def test_explicit_start_is_fail_closed_and_pause_stop_never_activate_dependencies(tmp_path):
    from threadrelay.application import Application
    with Application(tmp_path) as app:
        result = dispatch(command('Start', {'feed_url': 'https://www.threads.com/'}), controller=app.session, features=app.features)
        assert result['type'] == 'State'
        assert result['payload']['state'] == 'needs_attention'
        assert result['payload']['attention_reason'] == 'browser_unavailable'
        assert dispatch(command('Pause'), controller=app.session, features=app.features)['type'] == 'State'
        assert dispatch(command('Stop'), controller=app.session, features=app.features)['payload']['state'] == 'stopped'
        assert app.telegram._binding is None and app.runtime._lease is None


def test_repeated_start_returns_error_without_dropping_worker(tmp_path):
    from threadrelay.application import Application
    from threadrelay.session import SessionController
    with Application(tmp_path) as app:
        app.session = SessionController(setup_check=lambda _: {'allowed': True, 'reason': None})
        start = command('Start', {'feed_url': 'https://www.threads.com/'})
        assert dispatch(start, controller=app.session, features=app.features)['payload']['state'] == 'running'
        assert dispatch(start, controller=app.session, features=app.features)['type'] == 'Error'
        assert dispatch(command('QueryState'), controller=app.session, features=app.features)['payload']['state'] == 'running'


def test_private_bootstrap_rejects_untrusted_shapes_and_uses_host_root(tmp_path):
    from io import BytesIO
    import pytest
    from threadrelay.bootstrap import read_bootstrap
    from threadrelay.main import encode_frame
    good = {'version': 1, 'type': 'LocalWorkerBootstrap', 'data_root': str(tmp_path), 'generation': 7}
    assert read_bootstrap(BytesIO(encode_frame(good))) == (tmp_path, 7)
    for bad in [{**good, 'extra': True}, {**good, 'data_root': 'relative'}, {**good, 'generation': 0}, {**good, 'type': 'Start'}]:
        with pytest.raises(ValueError):
            read_bootstrap(BytesIO(encode_frame(bad)))


def test_private_worker_serves_real_framed_local_requests_and_reopens(tmp_path):
    from io import BytesIO
    from threadrelay.main import encode_frame, read_frame
    from threadrelay.packaged_worker import serve_local
    topic = {'id': 'a', 'version': 1, 'description': 'Saved', 'positive_examples': ['y'],
             'negative_examples': [], 'exclusions': [], 'enabled': True}
    source = BytesIO(b''.join(encode_frame(command(k, p)) for k, p in [
        ('SaveTopic', topic), ('QueryTopics', {}), ('QueryHistory', {})]))
    output = BytesIO()
    serve_local(tmp_path, source, output)
    output.seek(0)
    assert read_frame(output)['type'] == 'Acknowledged'
    assert read_frame(output)['payload'] == [topic]
    assert read_frame(output)['payload'] == []
    output = BytesIO()
    serve_local(tmp_path, BytesIO(encode_frame(command('QueryTopics'))), output)
    output.seek(0)
    assert read_frame(output)['payload'] == [topic]


def test_initialize_is_closed_empty_command():
    import pytest
    from threadrelay.contracts import ProtocolError, validate_contract
    validate_contract('Command', command('InitializeLocalWorkspace'))
    with pytest.raises(ProtocolError):
        validate_contract('Command', command('InitializeLocalWorkspace', {'path': '/tmp/no'}))


def test_private_stop_closes_generation_without_processing_later_frames(tmp_path):
    from io import BytesIO
    from threadrelay.main import encode_frame, read_frame
    from threadrelay.packaged_worker import serve_local
    source = BytesIO(encode_frame(command('Stop')) + encode_frame(command('QueryTopics')))
    output = BytesIO()
    serve_local(tmp_path, source, output)
    output.seek(0)
    assert read_frame(output)['payload']['state'] == 'stopped'
    assert read_frame(output) is None


def test_application_dispatch_uses_shared_coordinator_and_stays_closed(tmp_path):
    from threadrelay.application import Application
    with Application(tmp_path) as app:
        assert app.coordinator.session is app.session
        event = dispatch(command('Start', {'feed_url': 'https://www.threads.com/'}),
                         controller=app.session, features=app.features, coordinator=app.coordinator)
        assert event['payload']['state'] == 'needs_attention'
        assert dispatch(command('Stop'), controller=app.session, features=app.features,
                        coordinator=app.coordinator)['payload']['state'] == 'stopped'


def test_application_uses_one_generation_bound_private_handoff(tmp_path):
    from threadrelay.application import Application
    with Application(tmp_path, generation=11) as app:
        assert app.auth._handoff is app.secure_channel
        assert app.secure_channel.pending_count == 0


def test_worker_private_channel_is_distinct_and_closed_on_stop(tmp_path):
    import socket
    from threading import Thread
    from threadrelay.main import encode_frame, read_frame
    from threadrelay.packaged_worker import serve_local
    host, worker = socket.socketpair()
    host_secure, worker_secure = socket.socketpair()
    host.settimeout(1); host_secure.settimeout(1)
    source = worker.makefile('rwb', buffering=0)
    secure = worker_secure.makefile('rwb', buffering=0)
    thread = Thread(target=serve_local, args=(tmp_path, source, source),
        kwargs={'generation':9, 'secure_stream':secure, 'secure_shutdown':lambda:worker_secure.shutdown(socket.SHUT_RDWR)})
    thread.start()
    try:
        request = {'type':'SubmitSecret','generation':9,'epoch':0,'challenge_id':'none',
                   'kind':'code','handle':'d'*64,'value':'synthetic-value'}
        host_secure.sendall(encode_frame(request))
        assert read_frame(host_secure.makefile('rb', buffering=0)) == {'type':'PrivateResult','ok':False}
        host.sendall(encode_frame(command('Stop')))
        assert read_frame(host.makefile('rb', buffering=0))['payload']['state'] == 'stopped'
        thread.join(2); assert not thread.is_alive()
    finally:
        host.close(); host_secure.close(); worker.close(); worker_secure.close()


def test_packaged_entrypoint_redacts_unexpected_bootstrap_failure(monkeypatch, capsys):
    from io import BytesIO
    import socket
    from threadrelay import packaged_worker
    class FakeSocket:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def makefile(self, *args, **kwargs): return BytesIO()
    monkeypatch.setattr(socket, 'socket', lambda **_: FakeSocket())
    def fail(_): raise LookupError('sensitive diagnostic detail')
    monkeypatch.setattr(packaged_worker, 'read_startup', fail)
    assert packaged_worker.main() == 2
    assert 'sensitive diagnostic detail' not in capsys.readouterr().err


def test_local_and_dry_run_paths_do_not_construct_telegram_client(tmp_path, monkeypatch):
    from threadrelay import application
    def forbidden(*_args, **_kwargs):
        raise AssertionError('Telegram client must stay lazy')
    monkeypatch.setattr(application, 'TelegramClient', forbidden)
    with application.Application(tmp_path) as app:
        assert dispatch(command('QueryAccount'), controller=app.session, features=app.features)['type'] == 'Account'
        event = dispatch(command('Start', {'feed_url':'https://www.threads.com/', 'mode':'dry_run'}),
            controller=app.session, features=app.features, coordinator=app.coordinator)
        assert event['payload']['state'] == 'needs_attention'
        assert dispatch(command('QueryTopics'), controller=app.session, features=app.features)['payload'] == []


def test_default_model_location_comes_from_pinned_catalog_without_opening_it(tmp_path):
    from threadrelay.application import Application
    from threadrelay.inference.provision import load_catalog
    catalog = load_catalog()
    filename = next(m['filename'] for m in catalog['models'] if m['manifest']['id'] == catalog['primary_model_id'])
    with Application(tmp_path) as app:
        assert app.runtime._file == tmp_path / 'models' / filename
        assert not app.runtime._file.exists()
        assert app.runtime._lease is None


def test_readiness_is_inert_mode_specific_and_does_not_block_sqlite(tmp_path):
    from threadrelay.application import Application
    with Application(tmp_path) as app:
        request = command('QueryReadiness', {'feed_url':'https://www.threads.com/', 'mode':'dry_run'})
        event = dispatch(request, controller=app.session, features=app.features,
                         coordinator=app.coordinator, readiness=app.readiness)
        assert event['type'] == 'Readiness'
        assert event['payload']['mode'] == 'dry_run'
        assert event['payload']['browser'] == event['payload']['inference'] == 'unavailable'
        assert event['payload']['telegram'] == 'unavailable'
        assert app.telegram._client is None
        assert dispatch(command('QueryTopics'), controller=app.session, features=app.features)['payload'] == []


def test_pipeline_factory_reuses_the_long_lived_application_components(tmp_path):
    from threading import Event
    from threadrelay.application import Application
    with Application(tmp_path) as app:
        pipeline = app.pipeline(Event())
        assert pipeline.session is app.session and pipeline.router is app.router
        assert pipeline.posts is app.posts and pipeline.classifier is app.classifier
        assert pipeline.config is app.config and pipeline.sender is not app.sender


def test_application_composes_owned_adapters_without_launching_for_local_work(tmp_path):
    from threadrelay.application import Application
    from threadrelay.collector.embedded import EmbeddedBrowserSession
    from threadrelay.inference.native_launcher import NativeInferenceLauncher
    from threadrelay.pipeline import SessionPreparation
    class Broker:
        def capabilities(self):
            return {'browser': False, 'inference': False, 'telegram': False, 'automatic': False}
        def __getattr__(self, name):
            raise AssertionError(f'no native action during local work: {name}')
    with Application(tmp_path, broker=Broker()) as app:
        assert app.browser_launcher is None
        assert isinstance(app.browser, EmbeddedBrowserSession)
        assert isinstance(app.runtime._launcher, NativeInferenceLauncher)
        assert isinstance(app.coordinator.prepare, SessionPreparation)
        assert app.readiness({'feed_url':'https://www.threads.com/', 'mode':'dry_run'})['can_start'] is False
        assert dispatch(command('QueryTopics'), controller=app.session, features=app.features)['payload'] == []


def test_windows_descriptor_is_bounded_exact_and_not_a_secret_channel():
    from io import BytesIO
    import struct
    import pytest
    from threadrelay.bootstrap import read_windows_descriptor
    descriptor = struct.pack('<4sBBHQQQQQQQ', b'TRHP', 1, 6, 0, 7, 10, 11, 12, 13, 14, 15)
    assert read_windows_descriptor(BytesIO(descriptor)) == (7, (10, 11, 12, 13, 14, 15))
    for bad in (descriptor[:-1], descriptor.replace(b'TRHP', b'NOPE'),
                struct.pack('<4sBBHQQQQQQQ', b'TRHP', 1, 6, 0, 7, 10, 10, 12, 13, 14, 15),
                struct.pack('<4sBBHQQQQQQQ', b'TRHP', 1, 6, 0, 0, 10, 11, 12, 13, 14, 15)):
        with pytest.raises(ValueError): read_windows_descriptor(BytesIO(bad))


def test_readiness_does_not_claim_missing_pinned_model_is_available(tmp_path):
    from threadrelay.application import Application
    class Broker:
        def capabilities(self):return {'browser':True,'inference':True,'telegram':False,'automatic':False}
        def embedded_threads_available(self):return True
    with Application(tmp_path,broker=Broker()) as app:
        result=app.readiness({'feed_url':'https://www.threads.com/','mode':'dry_run'})
        assert result['browser']=='ready' and result['inference']=='unavailable'
        assert not result['can_start'] and result['reason']=='inference_unavailable'


def test_login_open_is_framed_responsive_and_late_open_is_cleaned(tmp_path, monkeypatch):
    import socket
    from threading import Event, Thread
    import threadrelay.application as module
    from threadrelay.packaged_worker import serve_local
    from threadrelay.main import encode_frame, read_frame
    entered, release, closed = Event(), Event(), Event()
    class SlowBrowser:
        def __init__(self, **kwargs): pass
        def open(self, profile): entered.set(); assert release.wait(3)
        def close(self): closed.set()
    monkeypatch.setattr(module, 'BrowserSession', SlowBrowser)
    parent, child = socket.socketpair()
    parent.settimeout(2)
    stream = child.makefile('rwb', buffering=0)
    worker = Thread(target=serve_local, args=(tmp_path, stream, stream), daemon=True)
    worker.start()
    wire = parent.makefile('rwb', buffering=0)
    wire.write(encode_frame({**command('OpenThreadsLogin'), 'id':'login'}))
    assert read_frame(wire)['type'] == 'Acknowledged'
    assert entered.wait(2)
    wire.write(encode_frame({**command('QueryState'), 'id':'query'}))
    assert read_frame(wire)['id'] == 'query'
    wire.write(encode_frame({**command('Pause'), 'id':'pause'}))
    assert read_frame(wire)['id'] == 'pause'
    release.set(); assert closed.wait(2)
    wire.write(encode_frame({**command('Stop'), 'id':'stop'}))
    assert read_frame(wire)['id'] == 'stop'
    worker.join(3); assert not worker.is_alive()
    wire.close(); parent.close(); stream.close(); child.close()


def test_ordered_delivery_owner_polls_initialized_client_while_stopped_and_paused(tmp_path):
    from threading import Event, get_ident
    from threadrelay.application import Application
    class Client:
        def disable_network(self): pass
    with Application(tmp_path) as app:
        calls, observed = [], Event()
        app.telegram._client = Client()
        def poll():
            calls.append((app.session.snapshot['state'], get_ident()))
            observed.set()
        app.sender.poll = poll
        assert observed.wait(2)
        assert calls[0][0] == 'stopped'
        observed.clear()
        app.session._setup_check = lambda _: {'allowed': True, 'reason': None}
        app.session.start({'feed_url':'https://www.threads.com/', 'mode':'dry_run'})
        app.coordinator.pause()
        assert observed.wait(2)
        assert any(state == 'paused' for state, _ in calls)
        assert len({thread for _, thread in calls}) == 1


def test_actual_auth_controller_advances_offline_events_on_delivery_owner(tmp_path):
    import time
    from threadrelay.application import Application
    from threadrelay.delivery.telegram import TelegramClient
    from test_telegram_auth import FixtureBinding, FixtureVault, state
    binding = FixtureBinding()
    with Application(tmp_path) as app:
        app.telegram._factory = lambda: TelegramClient(lambda: binding, FixtureVault(), tmp_path / 'sessions')
        app.auth.begin('vault:fixture', pending=False)
        binding.events.append(state('WaitTdlibParameters'))
        binding.events.append(state('WaitPhoneNumber'))
        end = time.monotonic() + 3
        while app.auth.challenge() is None and time.monotonic() < end:
            time.sleep(.01)
        assert app.session.snapshot['state'] == 'stopped'
        assert app.auth.challenge().kind == 'phone'
        assert [request['@type'] for request in binding.requests][:2] == ['setNetworkType','setTdlibParameters']


def test_running_promotion_queues_guarded_reconnect_on_ordered_owner(tmp_path):
    from threading import Event, get_ident
    from threadrelay.application import Application
    connected, calls = Event(), []
    class Broker:
        def capabilities(self): return {'telegram':True}
    with Application(tmp_path, broker=Broker()) as app:
        from types import SimpleNamespace
        app.telegram._client = SimpleNamespace(_closed=False,network_config_ready=False,disable_network=lambda:None)
        app.auth.status = 'ready'
        app.auth._network_requested = True
        app.sender.poll = lambda: None
        app.sender.maintain = lambda: None
        app.session._setup_check = lambda _: {'allowed': True, 'reason': None}
        def reconnect(): calls.append((app.session.snapshot['state'], get_ident())); connected.set()
        app.sender.enable_running_network = reconnect
        class Lease:
            def run(self, cancelled): cancelled.wait(3)
            def close(self): return {'stopped': True, 'remaining': []}
        app.coordinator.prepare = lambda config, cancelled: Lease()
        app.coordinator.start({'feed_url':'https://www.threads.com/', 'mode':'review'})
        assert connected.wait(2)
        assert calls == [('running', app.delivery_owner._thread.ident)]
        app.coordinator.pause()
        assert app.coordinator.wait_cleanup(2)


def test_delivery_queue_never_replays_old_cancelled_generation(tmp_path):
    from threading import Event
    from threadrelay.application import Application
    entered, release, sent = Event(), Event(), []
    class Client:
        def disable_network(self): pass
    with Application(tmp_path) as app:
        app.telegram._client = Client()
        def poll(): entered.set(); release.wait(3)
        app.sender.poll = poll
        app.sender.start_next = lambda worker: sent.append(worker)
        assert entered.wait(2)
        old, new = Event(), Event()
        app.delivery_owner.bind(old).start_next('old')
        old.set()
        app.delivery_owner.bind(new).start_next('new')
        release.set()
        import time
        end = time.monotonic() + 2
        while not sent and time.monotonic() < end: time.sleep(.01)
        assert sent == []  # a wakeup grants no Running authority, old or new


def test_real_application_promotes_with_consumed_but_live_browser_lease(tmp_path, monkeypatch):
    import time
    import threadrelay.application as module
    class Broker:
        lane, active = True, False
        def embedded_threads_available(self):return self.lane or self.active
        def capabilities(self):
            return {'browser':self.lane or self.active, 'inference':True, 'telegram':False, 'automatic':False}
    broker = Broker()
    class Browser:
        def __init__(self, **kwargs): pass
        def open(self, profile): broker.lane = False; broker.active = True
        def observe(self): return []
        def scroll_next(self): pass
        def close(self): broker.active = False
    monkeypatch.setattr(module, 'BrowserSession', Browser)
    with module.Application(tmp_path, broker=broker) as app:
        app.runtime._file.parent.mkdir(parents=True, exist_ok=True)
        app.runtime._file.write_bytes(b'x')
        app._model_size = 1
        app.runtime.start = lambda manifest,*,cancellation=None: None
        app.runtime.stop = lambda: {'stopped':True,'remaining':[]}
        config = {'feed_url':'https://www.threads.com/', 'mode':'dry_run'}
        assert app.readiness(config)['can_start']
        app.coordinator.start(config)
        end = time.monotonic() + 2
        while app.session.snapshot['state'] == 'starting' and time.monotonic() < end: time.sleep(.01)
        assert app.session.snapshot['state'] == 'running'
        assert not broker.lane and broker.active and app.readiness(config)['can_start']
        app.coordinator.pause(); assert app.coordinator.wait_cleanup(2)
        assert not app.readiness(config)['can_start']


def test_application_embedded_selection_never_falls_back_to_chrome(tmp_path):
    from threadrelay.application import Application
    from threadrelay.collector.embedded import EmbeddedBrowserSession
    class Broker:
        def capabilities(self):return {'browser':True,'inference':False,'telegram':False,'automatic':False}
        def embedded_threads_available(self):return False
        def launch_browser(self):raise AssertionError('Separate Chrome must not be selected')
    with Application(tmp_path,broker=Broker()) as app:
        assert isinstance(app.browser,EmbeddedBrowserSession)
        assert app.browser_launcher is None
        readiness=app.readiness({'feed_url':'https://www.threads.com/','mode':'dry_run'})
        assert readiness['browser']=='unavailable' and readiness['reason']=='browser_unavailable'


def test_auto_readiness_explains_previous_unknown_and_missing_connection(tmp_path):
    from threadrelay.application import Application
    from test_routing import route
    with Application(tmp_path) as app:
        app.config.save_route(route(mode='auto'))
        selected={'feed_url':'https://www.threads.com/','mode':'auto','route_ids':['route-a'],'unlimited_auto_deliveries':True}
        assert app.readiness(selected)['reason']=='telegram_connection_required'
        with app.db.transaction() as con:
            con.execute('INSERT INTO local_policy VALUES (?,?)',('automatic-incomplete:fixture','{}'))
        assert app.readiness(selected)['reason']=='automatic_outcome_unknown'
        result=dispatch(command('Start',selected),controller=app.session,features=app.features)
        assert result['type']=='State' and result['payload']['attention_reason']=='automatic_outcome_unknown'
        assert app.telegram._client is None
