"""Private host control uses synthetic ephemeral bytes, never native evidence."""
from pathlib import Path
import secrets
import socket
from threading import Thread
import pytest
from threadrelay.main import encode_frame, read_frame


def test_scoped_vault_resolve_is_one_shot_private_and_revocable():
    from threadrelay.broker_channel import HostControl
    host, child = socket.socketpair()
    host.settimeout(1); child.settimeout(1)
    stream = host.makefile('rwb', buffering=0)
    key = secrets.token_bytes(32); api_hash = secrets.token_hex(16)
    def serve():
        request = read_frame(stream)
        assert request == {'type':'ResolveAccount','generation':7}
        stream.write(encode_frame({'type':'AccountCredentials','generation':7,'api_id':123,
                                  'api_hash':api_hash,'database_key':key.hex()}))
    thread = Thread(target=serve); thread.start()
    broker = HostControl(child, generation=7)
    try:
        assert broker.resolve('vault:threadrelay_account') == {'api_id':123, 'api_hash':api_hash, 'database_key':key}
        with pytest.raises(Exception): broker.resolve('vault:threadrelay_account')
        with pytest.raises(Exception): broker.resolve('vault:anything_else')
    finally:
        broker.close(); host.close(); stream.close(); thread.join(1)


def test_bad_response_permanently_revokes_control():
    from threadrelay.broker_channel import HostControl
    host, child = socket.socketpair(); host.settimeout(1); child.settimeout(1)
    stream = host.makefile('rwb', buffering=0)
    def serve():
        read_frame(stream)
        stream.write(encode_frame({'type':'AccountCredentials','generation':99}))
    thread = Thread(target=serve); thread.start(); broker = HostControl(child, generation=7)
    try:
        with pytest.raises(Exception): broker.resolve('vault:threadrelay_account')
        with pytest.raises(Exception): broker.resolve('vault:threadrelay_account')
    finally:
        broker.close(); host.close(); stream.close(); thread.join(1)


def test_known_account_denial_keeps_shared_collection_control_open():
    from threadrelay.broker_channel import HostControl
    from threadrelay.delivery.telegram import NativeUnavailable
    host,child=socket.socketpair();host.settimeout(1);child.settimeout(1)
    stream=host.makefile('rwb',buffering=0);requests=[]
    def serve():
        requests.append(read_frame(stream))
        stream.write(encode_frame({'type':'AccountUnavailable','generation':7}))
        requests.append(read_frame(stream))
        stream.write(encode_frame({'type':'Capabilities','generation':7,'browser':True,
            'inference':True,'telegram':False,'telegram_auth':True,'automatic':False}))
    thread=Thread(target=serve);thread.start();broker=HostControl(child,generation=7)
    try:
        with pytest.raises(NativeUnavailable):broker.resolve('vault:threadrelay_account')
        assert not broker._closed
        assert broker.capabilities()=={'browser':True,'inference':True,'telegram':False,'telegram_auth':True,'automatic':False}
        with pytest.raises(NativeUnavailable):broker.resolve('vault:threadrelay_account')
        assert [item['type'] for item in requests]==['ResolveAccount','Capabilities']
    finally:
        broker.close();host.close();stream.close();thread.join(1)


@pytest.mark.parametrize('response',[{'type':'AccountUnavailable','generation':7,'extra':'invalid'},
    {'type':'AccountUnavailable','generation':99},{'type':'AccountUnavailable'}])
def test_malformed_account_denial_still_revokes_private_control(response):
    from threadrelay.broker_channel import HostControl
    from threadrelay.delivery.telegram import NativeUnavailable
    host,child=socket.socketpair();host.settimeout(1);child.settimeout(1)
    stream=host.makefile('rwb',buffering=0)
    def serve():
        read_frame(stream);stream.write(encode_frame(response))
    thread=Thread(target=serve);thread.start();broker=HostControl(child,generation=7)
    try:
        with pytest.raises(NativeUnavailable):broker.resolve('vault:threadrelay_account')
        assert broker._closed
    finally:
        broker.close();host.close();stream.close();thread.join(1)


def test_private_inference_client_uses_fixed_catalog_and_generation_bound_lease(tmp_path):
    from threading import Event
    from threadrelay.broker_channel import HostControl
    from threadrelay.inference.provision import load_catalog
    from threadrelay.inference.native_launcher import NativeInferenceLauncher
    from threadrelay.inference.runtime import RuntimeLaunchSpec
    catalog=load_catalog(); record=catalog['models'][0]; manifest=record['manifest']
    host, child=socket.socketpair(); host.settimeout(1); child.settimeout(1)
    stream=host.makefile('rwb',buffering=0); token=secrets.token_urlsafe(32); lease_id=secrets.token_hex(32)
    requests=[]
    def serve():
        for _ in range(3):
            request=read_frame(stream); requests.append(request)
            if request['type']=='StartPinnedInference':
                result={'type':'InferenceStarted','generation':7,'lease':lease_id,'port':49152,
                    'model_id':manifest['id'],'model_sha256':manifest['sha256'],
                    'runtime_commit':catalog['runtime']['commit'],'platform':'windows-x64',
                    'archive_sha256':catalog['runtime']['artifacts'][0]['sha256'],
                    'loaded_model_path':str(tmp_path/'models'/record['filename'])}
            elif request['type']=='PollInference':
                result={'type':'InferenceStatus','generation':7,'lease':lease_id,'active':True}
            else:
                result={'type':'InferenceClosed','generation':7,'lease':lease_id,'report':{'stopped':True,'remaining':[]}}
            stream.write(encode_frame(result))
    thread=Thread(target=serve);thread.start(); broker=HostControl(child,generation=7,root=tmp_path)
    try:
        spec=RuntimeLaunchSpec(tmp_path/'models'/record['filename'],manifest['id'],manifest['sha256'],
            catalog['runtime']['commit'],4096,4,token,Event())
        lease=NativeInferenceLauncher(tmp_path,broker).launch(spec)
        assert lease.port==49152 and lease.model_path==spec.model_file
        assert lease.poll()=='active' and lease.close()=={'stopped':True,'remaining':[]}
        assert all(r['generation']==7 for r in requests)
        assert not {'path','executable','url','argv','env'} & requests[0].keys()
    finally:
        broker.close(); host.close(); stream.close(); thread.join(1)


def test_readiness_does_not_wait_for_a_busy_native_exchange():
    from threadrelay.broker_channel import HostControl
    host, child=socket.socketpair()
    broker=HostControl(child,generation=7)
    try:
        broker._lock.acquire()
        try: assert broker.capabilities() == {}
        finally: broker._lock.release()
    finally:
        broker.close(); host.close()


def test_capability_probe_accepts_a_slow_current_reply_without_retry():
    from threading import Event
    from threadrelay.broker_channel import HostControl
    host,child=socket.socketpair();host.settimeout(2)
    stream=host.makefile('rwb',buffering=0);requests=[]
    def serve():
        requests.append(read_frame(stream))
        Event().wait(.35)  # Native proof checks can outlast a UI polling tick.
        try:
            stream.write(encode_frame({'type':'Capabilities','generation':7,
                'browser':False,'inference':False,'telegram':False,'telegram_auth':True,'automatic':False}))
        except OSError:
            pass  # The pre-fix short deadline closes the socket before this reply.
    thread=Thread(target=serve);thread.start();broker=HostControl(child,generation=7)
    try:
        assert broker.capabilities().get('telegram_auth') is True
        assert broker.begin_account()=='vault:threadrelay_account'
        assert requests==[{'type':'Capabilities','generation':7}]
    finally:
        broker.close();thread.join(1);host.close();stream.close()


def test_capability_timeout_requires_a_new_generation_and_never_replays_begin():
    from threading import Event
    from threadrelay.broker_channel import HostControl
    from threadrelay.delivery.telegram import NativeUnavailable
    host,child=socket.socketpair();host.settimeout(2)
    stream=host.makefile('rwb',buffering=0)
    closed=Event();requests=[]
    def serve_expired():
        requests.append(read_frame(stream))
        assert read_frame(stream) is None
        closed.set()
    thread=Thread(target=serve_expired);thread.start();expired=HostControl(child,generation=7)
    try:
        assert expired.capabilities()=={}
        assert closed.wait(1)
        with pytest.raises(NativeUnavailable):expired.begin_account()
        assert expired.capabilities()=={}
        assert requests==[{'type':'Capabilities','generation':7}]
    finally:
        expired.close();host.close();stream.close();thread.join(1)
    host,child=socket.socketpair();host.settimeout(2)
    stream=host.makefile('rwb',buffering=0)
    def serve_new():
        assert read_frame(stream)=={'type':'Capabilities','generation':8}
        stream.write(encode_frame({'type':'Capabilities','generation':8,
            'browser':False,'inference':False,'telegram':False,'telegram_auth':True,'automatic':False}))
    thread=Thread(target=serve_new);thread.start();fresh=HostControl(child,generation=8)
    try:
        assert fresh.capabilities()['telegram_auth'] is True
        assert fresh.begin_account()=='vault:threadrelay_account'
    finally:
        fresh.close();host.close();stream.close();thread.join(1)


def test_windows_style_blocked_stream_cannot_block_readiness_or_shutdown():
    from threading import Event
    from threadrelay.broker_channel import HostControl
    entered, release, finished=Event(), Event(), Event()
    class BlockedStream:
        def write(self, data): return len(data)
        def flush(self): pass
        def read(self, count): entered.set(); release.wait(5); return b''
        def close(self): release.set()
    broker=HostControl(None,generation=7,stream=BlockedStream())
    result=[]
    thread=Thread(target=lambda:(result.append(broker.capabilities()),finished.set()))
    thread.start()
    try:
        assert entered.wait(1) and finished.wait(2)
        assert result==[{}]
        broker.close()
    finally:
        release.set();thread.join(1)


def test_private_browser_lease_has_separate_owned_driver_bytes_and_scoped_status():
    from threadrelay.broker_channel import HostControl
    control_host,control_child=socket.socketpair(); driver_host,driver_child=socket.socketpair()
    for sock in (control_host,control_child,driver_host,driver_child): sock.settimeout(1)
    stream=control_host.makefile('rwb',buffering=0); lease_id=secrets.token_hex(32); requests=[]
    def serve():
        for _ in range(5):
            request=read_frame(stream);requests.append(request)
            if request['type']=='Capabilities':
                value={'type':'Capabilities','generation':7,'browser':True,'inference':False,'telegram':False,'telegram_auth':False,'automatic':False}
            elif request['type']=='OpenDedicatedBrowser':
                value={'type':'BrowserOpened','generation':7,'lease':lease_id,'source_session_id':lease_id}
            elif request['type']=='PollBrowser':
                value={'type':'BrowserStatus','generation':7,'lease':lease_id,'active':True}
            else:
                value={'type':'BrowserClosed','generation':7,'lease':lease_id,'report':{'stopped':True,'remaining':[]}}
            stream.write(encode_frame(value))
    thread=Thread(target=serve);thread.start(); broker=HostControl(control_child,generation=7,driver=driver_child)
    try:
        assert broker.capabilities()['browser'] and broker.owned_driver_transport_available()
        lease=broker.launch_browser()
        driver_host.sendall(b'private-driver')
        assert lease.driver.read(14)==b'private-driver'
        assert lease.poll()=='active' and lease.pipe_owned()
        assert lease.close()=={'stopped':True,'remaining':[]}
        assert requests[1]=={'type':'OpenDedicatedBrowser','generation':7}
    finally:
        broker.close();control_host.close();driver_host.close();stream.close();thread.join(1)


def test_evaluation_reservation_is_private_scoped_and_released_once(tmp_path):
    from threadrelay.broker_channel import HostControl
    host,child=socket.socketpair();host.settimeout(1);child.settimeout(1)
    stream=host.makefile('rwb',buffering=0); reservation=secrets.token_hex(32); requests=[]
    def serve():
        for _ in range(3):
            request=read_frame(stream);requests.append(request)
            if request['type']=='ReserveEvaluation':
                value={'type':'EvaluationReserved','generation':8,'reservation':reservation}
            elif request['type']=='PollEvaluation':
                value={'type':'EvaluationStatus','generation':8,'reservation':reservation,'active':True}
            else:
                value={'type':'EvaluationReleased','generation':8,'reservation':reservation,'released':True}
            stream.write(encode_frame(value))
    thread=Thread(target=serve);thread.start(); broker=HostControl(child,generation=8,root=tmp_path)
    try:
        lease=broker.reserve_evaluation()
        assert lease.purpose=='evaluation' and lease.active() and lease.launcher is not None
        assert lease.release() is True and not lease.active()
        assert [r['type'] for r in requests]==['ReserveEvaluation','PollEvaluation','ReleaseEvaluation']
        assert all(r['generation']==8 for r in requests)
    finally:
        broker.close();host.close();stream.close();thread.join(1)


def test_inference_peer_check_binds_connected_loopback_tuple_to_lease():
    from threadrelay.broker_channel import HostControl, _InferenceLease
    host,child=socket.socketpair();host.settimeout(1);child.settimeout(1);stream=host.makefile('rwb',buffering=0)
    identifier=secrets.token_hex(32); seen=[]
    def serve():
        request=read_frame(stream);seen.append(request)
        stream.write(encode_frame({'type':'InferencePeer','generation':7,'lease':identifier,'verified':True}))
    thread=Thread(target=serve);thread.start();control=HostControl(child,generation=7)
    class Socket:
        def getsockname(self):return ('127.0.0.1',12345)
        def getpeername(self):return ('127.0.0.1',49152)
    value={'lease':identifier,'port':49152,'model_sha256':'x','runtime_commit':'x','platform':'x','archive_sha256':'x','loaded_model_path':'/synthetic'}
    try:
        lease=_InferenceLease(control,value,Path('/synthetic'))
        assert lease.verify_connection(Socket())
        assert seen==[{'type':'VerifyInferencePeer','generation':7,'lease':identifier,'client_port':12345,'server_port':49152}]
    finally:
        control.close();host.close();stream.close();thread.join(1)


def test_evaluation_sampler_uses_separate_generation_bound_observer_channel(tmp_path):
    from threadrelay.broker_channel import HostControl, _EvaluationReservation
    main_host,main_child=socket.socketpair();obs_host,obs_child=socket.socketpair()
    for sock in (main_host,main_child,obs_host,obs_child):sock.settimeout(1)
    stream=obs_host.makefile('rwb',buffering=0);identifier=secrets.token_hex(32);seen=[]
    def serve():
        for _ in range(2):
            request=read_frame(stream);seen.append(request)
            stream.write(encode_frame({'type':'EvaluationMemory','generation':7,'reservation':identifier,
                'worker_rss_bytes':100,'owned_runtime_rss_bytes':200,'accelerator_bytes':None}))
    thread=Thread(target=serve);thread.start(); observer=HostControl(obs_child,generation=7)
    control=HostControl(main_child,generation=7,root=tmp_path,observer=observer)
    try:
        reservation=_EvaluationReservation(control,identifier)
        factory=control.evaluation_memory_factory(reservation)
        sampler=factory();sampler._sample()
        assert sampler._total==300
        assert all(r=={'type':'SampleEvaluation','generation':7,'reservation':identifier} for r in seen)
    finally:
        control.close();main_host.close();obs_host.close();stream.close();thread.join(1)


def test_hardware_provider_keeps_unknown_acceleration_when_host_cannot_prove_it(tmp_path):
    from threadrelay.broker_channel import HostControl, _EvaluationReservation
    host,child=socket.socketpair();host.settimeout(1);child.settimeout(1);stream=host.makefile('rwb',buffering=0)
    identifier=secrets.token_hex(32)
    def serve():
        _request=read_frame(stream)
        stream.write(encode_frame({'type':'ObservationUnavailable','generation':7,'reservation':identifier}))
    thread=Thread(target=serve);thread.start();control=HostControl(child,generation=7,root=tmp_path)
    try:
        from threadrelay.evaluation.hardware import UNAVAILABLE
        provider=control.evaluation_hardware_provider(_EvaluationReservation(control,identifier))
        assert provider()['acceleration_mode']==UNAVAILABLE
    finally:
        control.close();host.close();stream.close();thread.join(1)


@pytest.mark.parametrize('path_kind', ['guarded', 'source', 'outside', 'parent', 'wrong_model', 'short_generation'])
def test_private_macos_loaded_model_receipt_is_bound_to_guard_namespace(tmp_path, path_kind):
    from threading import Event
    from threadrelay.broker_channel import HostControl
    from threadrelay.inference.provision import load_catalog
    catalog=load_catalog(); record=catalog['models'][0]
    source=tmp_path/'models'/record['filename']
    guarded=tmp_path/'execution-staging'/('a'*48)/record['filename']
    paths={'guarded':str(guarded),'source':str(source),'outside':str(Path('/tmp')/record['filename']),
        'parent':str(tmp_path/'execution-staging'/('a'*48)/'..'/record['filename']),
        'wrong_model':str(guarded.with_name('other.gguf')),
        'short_generation':str(tmp_path/'execution-staging'/'abc'/record['filename'])}
    host,child=socket.socketpair()
    broker=HostControl(child,generation=7,root=tmp_path)
    receipt={'type':'InferenceStarted','generation':7,'lease':'a'*64,'port':49152,
        'model_id':record['manifest']['id'],'model_sha256':record['manifest']['sha256'],
        'runtime_commit':catalog['runtime']['commit'],'platform':'macos-arm64',
        'archive_sha256':catalog['runtime']['artifacts'][1]['sha256'],'loaded_model_path':paths[path_kind]}
    broker._exchange=lambda *a,**k:receipt
    try:
        if path_kind=='guarded':
            lease=broker.launch_inference(model_id=record['manifest']['id'],context_tokens=4096,cpu_threads=4,
                auth_token='x'*32,cancelled=Event())
            assert lease.model_path==source
            assert lease.loaded_model_path==guarded
        else:
            with pytest.raises(Exception):
                broker.launch_inference(model_id=record['manifest']['id'],context_tokens=4096,cpu_threads=4,
                    auth_token='x'*32,cancelled=Event())
    finally:
        broker.close();host.close()


def test_exchange_budget_controls_socket_read_timeout_not_constructor_default():
    import time
    from threadrelay.broker_channel import HostControl
    host,child=socket.socketpair();host.settimeout(1)
    stream=host.makefile('rwb',buffering=0)
    broker=HostControl(child,generation=7)
    child.settimeout(.01)
    failures=[]
    def serve():
        try:
            read_frame(stream)
            time.sleep(.05)
            stream.write(encode_frame({'type':'DelayedSyntheticReply','generation':7}))
        except Exception as error:
            failures.append(type(error).__name__)
    thread=Thread(target=serve);thread.start()
    try:
        assert broker._exchange('SyntheticBudgetProbe',timeout=.3)=={'type':'DelayedSyntheticReply','generation':7}
        assert not failures
    finally:
        broker.close();host.close();stream.close();thread.join(1)


def test_embedded_host_lease_uses_only_fixed_generation_bound_operations(tmp_path):
    from threadrelay.broker_channel import HostControl
    from test_embedded_collector import snapshot
    host, child=socket.socketpair();broker=HostControl(child,generation=7,root=tmp_path)
    requests=[]
    def exchange(kind, values=None, **limits):
        requests.append((kind,values,limits))
        base={'generation':7,'webview_generation':12}
        if kind=='EmbeddedThreadsAvailable':return {'type':'EmbeddedThreadsAvailability','generation':7,'available':True}
        if kind=='AttachEmbeddedThreads':return {'type':'EmbeddedThreadsAttached',**base,'source_session_id':'a'*64}
        if kind=='SnapshotEmbeddedThreads':return {'type':'EmbeddedThreadsSnapshot',**base,'payload':snapshot()}
        if kind=='ScrollEmbeddedThreads':return {'type':'EmbeddedThreadsScrolled',**base}
        if kind=='PollEmbeddedThreads':return {'type':'EmbeddedThreadsStatus',**base,'active':True}
        if kind=='ReleaseEmbeddedThreads':return {'type':'EmbeddedThreadsReleased',**base,'report':{'stopped':True,'remaining':[]}}
        raise AssertionError(kind)
    broker._exchange=exchange
    try:
        lease=broker.attach_embedded_threads()
        assert lease.webview_generation==12 and lease.source_session_id=='a'*64
        assert lease.snapshot()['state']=='ready';lease.scroll();assert lease.poll()=='active'
        assert lease.close()=={'stopped':True,'remaining':[]}
        assert [v[0] for v in requests]==['AttachEmbeddedThreads','SnapshotEmbeddedThreads','ScrollEmbeddedThreads','PollEmbeddedThreads','ReleaseEmbeddedThreads']
        assert all(values=={'webview_generation':12} for _,values,_ in requests[1:])
        assert requests[1][2]['max_response_bytes']==1048576
    finally:broker.close();host.close()


@pytest.mark.parametrize('change',[{'webview_generation':True},{'webview_generation':0},{'source_session_id':'x'}, {'generation':8},{'extra':'untrusted'}])
def test_embedded_attach_rejects_bad_host_identity(tmp_path,change):
    from threadrelay.broker_channel import HostControl
    host,child=socket.socketpair();broker=HostControl(child,generation=7,root=tmp_path)
    broker._exchange=lambda *a,**k:{'type':'EmbeddedThreadsAttached','generation':7,'webview_generation':12,'source_session_id':'a'*64,**change}
    try:
        with pytest.raises(Exception):broker.attach_embedded_threads()
    finally:broker.close();host.close()


def test_embedded_stale_snapshot_never_changes_lease_identity(tmp_path):
    from threadrelay.broker_channel import HostControl
    from test_embedded_collector import snapshot
    host,child=socket.socketpair();broker=HostControl(child,generation=7,root=tmp_path)
    replies=iter([{'type':'EmbeddedThreadsAttached','generation':7,'webview_generation':12,'source_session_id':'a'*64},
        {'type':'EmbeddedThreadsSnapshot','generation':7,'webview_generation':13,'payload':snapshot()}])
    broker._exchange=lambda *a,**k:next(replies)
    try:
        lease=broker.attach_embedded_threads()
        with pytest.raises(Exception):lease.snapshot()
        assert lease.poll()=='ownership_lost' and lease.webview_generation==12
    finally:broker.close();host.close()


def test_embedded_snapshot_uses_real_bounded_frame_above_default_control_cap(tmp_path):
    from threadrelay.broker_channel import HostControl
    from test_embedded_collector import snapshot
    from test_collector import raw_snapshot
    host,child=socket.socketpair();host.settimeout(2)
    stream=host.makefile('rwb',buffering=0)
    received=[];failures=[]
    payload=snapshot(cards=[raw_snapshot(text='x'*9000)])
    def serve():
        try:
            received.append(read_frame(stream))
            host.sendall(encode_frame({'type':'EmbeddedThreadsAttached','generation':7,'webview_generation':12,'source_session_id':'a'*64}))
            received.append(read_frame(stream))
            host.sendall(encode_frame({'type':'EmbeddedThreadsSnapshot','generation':7,'webview_generation':12,'payload':payload}))
        except Exception as error:
            failures.append(type(error).__name__)
    thread=Thread(target=serve);thread.start();broker=HostControl(child,generation=7,root=tmp_path)
    try:
        lease=broker.attach_embedded_threads()
        assert lease.snapshot()['cards'][0]['text']=='x'*9000
        assert received==[{'type':'AttachEmbeddedThreads','generation':7},
            {'type':'SnapshotEmbeddedThreads','generation':7,'webview_generation':12}]
        assert not failures
    finally:
        broker.close();stream.close();host.close();thread.join(2)


def test_private_tdlib_receipt_preserves_auth_only_scope_without_delivery_capability(monkeypatch,tmp_path):
    from threadrelay.broker_channel import HostControl
    from threadrelay.delivery.telegram import NativeJsonBinding,NativeUnavailable
    host,child=socket.socketpair();broker=HostControl(child,generation=7)
    path=tmp_path/'libtdjson.dylib'
    receipt={'type':'TdlibPrepared','generation':7,'library':str(path),'purpose':'connection_only'}
    broker._exchange=lambda kind:dict(receipt)
    monkeypatch.setattr(NativeJsonBinding,'load_verified',lambda capability:capability)
    try:
        capability=broker.load_tdlib()
        assert capability._purpose=='connection_only'
        for purpose in ['arbitrary',None]:
            receipt['purpose']=purpose
            with pytest.raises(NativeUnavailable):broker.load_tdlib()
    finally:broker.close();host.close()
