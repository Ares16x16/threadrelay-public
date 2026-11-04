"""Startup source regressions over real HTTP parsing and synthetic owned sockets.

These fixtures do not load native code/weights or attest native capabilities.
"""
from collections import deque
import io
import json

import pytest

from threadrelay.inference.provision import ModelProvisioner
from threadrelay.inference.runtime import (
    DecisionRuntime, LoopbackClient, RuntimeHandle, RuntimeLimits, RuntimeUnavailable,
)
from test_inference import Client, Launcher, Lease, response
from test_model_manifest import SYNTHETIC_GGUF, model, synthetic_catalog


class WireSocket:
    def __init__(self, transport, status, body, before_response=None):
        self.transport, self.status, self.body = transport, status, body
        self.before_response = before_response
        self.writes = []
        self.closed = False
        self.timeout_values = []

    def settimeout(self, value):
        self.timeout_values.append(value)

    def setsockopt(self, *args):
        pass

    def connect(self, address):
        self.transport.addresses.append(address)

    def shutdown(self, how):
        pass

    def close(self):
        self.closed = True

    def sendall(self, data):
        self.writes.append(bytes(data))

    def makefile(self, mode):
        if self.before_response is not None:
            self.before_response()
        body = (self.body if isinstance(self.body, bytes) else
                json.dumps(self.body, separators=(',', ':')).encode())
        reason = 'OK' if self.status == 200 else 'Loading' if self.status == 503 else 'Error'
        head = (f'HTTP/1.1 {self.status} {reason}\r\nContent-Type: application/json\r\n'
                f'Content-Length: {len(body)}\r\nConnection: close\r\n\r\n').encode()
        return io.BytesIO(head + body)


class WireTransport:
    def __init__(self):
        self.responses = deque()
        self.sockets, self.addresses, self.verified = [], [], []
        self.peer_allowed = lambda sock: True

    def socket(self, *args):
        status, body, callback = self.responses.popleft()
        sock = WireSocket(self, status, body, callback)
        self.sockets.append(sock)
        return sock

    def verify(self, sock):
        self.verified.append(sock)
        return self.peer_allowed(sock)

    def add(self, status, body, before_response=None):
        self.responses.append((status, body, before_response))

    @property
    def request_lines(self):
        return [sock.writes[0].split(b'\r\n', 1)[0] for sock in self.sockets if sock.writes]


@pytest.fixture
def readiness(tmp_path, monkeypatch):
    catalog = synthetic_catalog()
    path = tmp_path / 'synthetic-source-test.gguf'
    path.write_bytes(SYNTHETIC_GGUF)
    fixture_client = Client(path, catalog)
    lease = Lease(path, catalog)
    launcher = Launcher(lease)
    wire = WireTransport()
    lease.verify_connection = wire.verify
    original_init = LoopbackClient.__init__

    def initialize(self, *args, **kwargs):
        kwargs['socket_factory'] = wire.socket
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(LoopbackClient, '__init__', initialize)
    gate = [True]
    runtime = DecisionRuntime(model_file=path, provisioner=ModelProvisioner(catalog=catalog),
                              launcher=launcher, setup_authorized=lambda: gate[0])
    try:
        yield runtime, launcher, lease, wire, fixture_client, catalog, gate
    finally:
        runtime.stop()


def add_ready(wire, fixture_client):
    wire.add(200, fixture_client.props)
    wire.add(200, fixture_client.models)
    wire.add(200, response({'__canary': 0.5}))


def test_owned_listener_loading_503_retries_props_before_one_canary(readiness):
    runtime, launcher, lease, wire, fixture_client, catalog, _ = readiness
    wire.add(503, {'error': {'code': 503, 'message': 'Loading model', 'type': 'unavailable_error'}})
    add_ready(wire, fixture_client)

    handle = runtime.start(model(catalog))

    assert isinstance(handle, RuntimeHandle)
    assert len(launcher.specs) == 1
    assert wire.request_lines == [b'GET /props HTTP/1.1', b'GET /props HTTP/1.1',
                                  b'GET /v1/models HTTP/1.1', b'POST /v1/systemone HTTP/1.1']
    assert wire.addresses == [('127.0.0.1', 49152)] * 4
    assert wire.verified == wire.sockets
    token = launcher.specs[0].auth_token.encode()
    assert all(b'Authorization: Bearer ' + token in sock.writes[0] for sock in wire.sockets)
    assert all(sock.closed for sock in wire.sockets)
    assert lease.closed == 0
    assert runtime.stop() == {'stopped': True, 'remaining': []}
    assert lease.closed == 1


def add_loading(wire, before_response=None):
    wire.add(503, {'error': {'code': 503, 'message': 'Loading model',
                             'type': 'unavailable_error'}}, before_response)


@pytest.mark.parametrize('status', [301, 302, 400, 401, 403, 404, 500, 501, 504])
def test_non_503_error_after_loading_stops_same_lease_without_retry(readiness, status):
    runtime, launcher, lease, wire, _, catalog, _ = readiness
    add_loading(wire)
    wire.add(status, {'error': {'code': status, 'message': 'Synthetic error', 'type': 'error'}})

    with pytest.raises(RuntimeUnavailable):
        runtime.start(model(catalog))

    assert wire.request_lines == [b'GET /props HTTP/1.1', b'GET /props HTTP/1.1']
    assert len(launcher.specs) == 1
    assert lease.closed == 1
    assert all(sock.closed for sock in wire.sockets)


@pytest.mark.parametrize('body', [
    b'{not-json', [], {'padding': 'x' * 65537}, {'error': 'Loading model'},
    {'error': {'code': 503, 'message': 'Different failure', 'type': 'unavailable_error'}},
])
def test_malformed_or_non_loading_503_content_is_never_retried(readiness, body):
    runtime, launcher, lease, wire, fixture_client, catalog, _ = readiness
    wire.add(503, body)
    add_ready(wire, fixture_client)

    with pytest.raises(RuntimeUnavailable):
        runtime.start(model(catalog))

    assert wire.request_lines == [b'GET /props HTTP/1.1']
    assert len(launcher.specs) == 1
    assert lease.closed == 1


@pytest.mark.parametrize('path', ['/v1/models', '/v1/systemone'])
def test_loading_status_after_props_is_fatal_without_replaying_canary(readiness, path):
    runtime, launcher, lease, wire, fixture_client, catalog, _ = readiness
    wire.add(200, fixture_client.props)
    if path == '/v1/systemone':
        wire.add(200, fixture_client.models)
    add_loading(wire)

    with pytest.raises(RuntimeUnavailable):
        runtime.start(model(catalog))

    expected = [b'GET /props HTTP/1.1', b'GET /v1/models HTTP/1.1']
    if path == '/v1/systemone':
        expected.append(b'POST /v1/systemone HTTP/1.1')
    assert wire.request_lines == expected
    assert len(launcher.specs) == 1
    assert lease.closed == 1


@pytest.mark.parametrize('change', ['ownership', 'resource', 'gate', 'revoke', 'generation', 'stop', 'cancellation'])
def test_revocation_during_loading_response_prevents_every_later_request(readiness, change):
    from threading import Event
    runtime, launcher, lease, wire, fixture_client, catalog, gate = readiness
    cancellation = Event()

    def revoke():
        if change == 'ownership':
            lease.status = 'ownership_lost'
        elif change == 'resource':
            lease.budget = False
        elif change == 'gate':
            gate[0] = False
        elif change == 'revoke':
            runtime.revoke()
        elif change == 'generation':
            runtime._generation += 1
        elif change == 'stop':
            runtime.stop()
        else:
            cancellation.set()

    add_loading(wire, revoke)
    add_ready(wire, fixture_client)

    with pytest.raises(RuntimeUnavailable):
        runtime.start(model(catalog), cancellation=cancellation)

    assert wire.request_lines == [b'GET /props HTTP/1.1']
    assert len(launcher.specs) == 1
    assert lease.closed == 1


def test_cancel_during_readiness_backoff_never_issues_another_request(readiness, monkeypatch):
    runtime, launcher, lease, wire, fixture_client, catalog, _ = readiness
    waits = []

    def install_wait():
        original = runtime._cancelled.wait

        def cancel_wait(seconds):
            waits.append(seconds)
            runtime.revoke()
            return original(seconds)

        monkeypatch.setattr(runtime._cancelled, 'wait', cancel_wait)

    add_loading(wire, install_wait)
    add_ready(wire, fixture_client)

    with pytest.raises(RuntimeUnavailable):
        runtime.start(model(catalog))

    assert len(waits) == 1
    assert wire.request_lines == [b'GET /props HTTP/1.1']
    assert len(launcher.specs) == 1
    assert lease.closed == 1


def test_retry_rechecks_connected_peer_before_releasing_key(readiness):
    runtime, launcher, lease, wire, fixture_client, catalog, _ = readiness
    wire.peer_allowed = lambda sock: len(wire.verified) == 1
    add_loading(wire)
    add_ready(wire, fixture_client)

    with pytest.raises(RuntimeUnavailable):
        runtime.start(model(catalog))

    assert len(wire.sockets) == 2
    assert wire.verified == wire.sockets
    assert wire.sockets[1].writes == []
    assert wire.request_lines == [b'GET /props HTTP/1.1']
    assert len(launcher.specs) == 1
    assert lease.closed == 1


def test_loading_responses_consume_one_absolute_startup_budget(readiness):
    runtime, launcher, lease, wire, fixture_client, catalog, _ = readiness
    now = [0.0]
    runtime._clock = lambda: now[0]

    def advance():
        now[0] += 6.0

    for _ in range(3):
        add_loading(wire, advance)
    add_ready(wire, fixture_client)

    with pytest.raises(RuntimeUnavailable):
        runtime.start(model(catalog))

    assert wire.request_lines == [b'GET /props HTTP/1.1'] * 3
    assert [sock.timeout_values[0] for sock in wire.sockets] == [15.0, 9.0, 3.0]
    assert len(launcher.specs) == 1
    assert lease.closed == 1


def test_retry_connect_exhausting_remaining_budget_never_writes_key(readiness, monkeypatch):
    runtime, launcher, lease, wire, fixture_client, catalog, _ = readiness
    now = [0.0]
    runtime._clock = lambda: now[0]
    add_loading(wire, lambda: now.__setitem__(0, 14.0))
    add_ready(wire, fixture_client)
    original = WireSocket.connect

    def delayed_connect(sock, address):
        original(sock, address)
        if len(wire.sockets) == 2:
            now[0] += 2.0

    monkeypatch.setattr(WireSocket, 'connect', delayed_connect)

    with pytest.raises(RuntimeUnavailable):
        runtime.start(model(catalog))

    assert len(wire.sockets) == 2
    assert wire.sockets[1].timeout_values[0] == 1.0
    assert wire.sockets[1].writes == []
    assert wire.verified == wire.sockets[:1]
    assert len(launcher.specs) == 1
    assert lease.closed == 1


def test_props_models_and_canary_share_startup_deadline_and_late_canary_is_discarded(readiness):
    runtime, launcher, lease, wire, fixture_client, catalog, _ = readiness
    now = [0.0]
    runtime._clock = lambda: now[0]
    add_loading(wire, lambda: now.__setitem__(0, 3.0))
    wire.add(200, fixture_client.props, lambda: now.__setitem__(0, 7.0))
    wire.add(200, fixture_client.models, lambda: now.__setitem__(0, 12.0))
    wire.add(200, response({'__canary': 0.5}), lambda: now.__setitem__(0, 16.0))

    with pytest.raises(RuntimeUnavailable):
        runtime.start(model(catalog))

    assert wire.request_lines == [b'GET /props HTTP/1.1', b'GET /props HTTP/1.1',
                                  b'GET /v1/models HTTP/1.1', b'POST /v1/systemone HTTP/1.1']
    assert [sock.timeout_values[0] for sock in wire.sockets] == [15.0, 12.0, 8.0, 3.0]
    assert len(launcher.specs) == 1
    assert lease.closed == 1
    with pytest.raises(RuntimeUnavailable):
        runtime.decide({'post_text': 'Synthetic', 'quote': None},
                       {'topic': {'type': 'noul', 'instructions': 'Synthetic?'}})
    assert len(wire.sockets) == 4


def test_startup_binds_props_to_host_guarded_model_path_not_verified_source(readiness):
    runtime, launcher, lease, wire, fixture_client, catalog, _ = readiness
    guarded = lease.model_path.parent / 'execution-staging' / ('a' * 48) / lease.model_path.name
    lease.loaded_model_path = guarded
    fixture_client.props['model_path'] = str(guarded)
    add_ready(wire, fixture_client)

    assert isinstance(runtime.start(model(catalog)), RuntimeHandle)
    assert lease.closed == 0
    assert runtime.stop() == {'stopped': True, 'remaining': []}


def test_source_path_in_props_is_rejected_when_host_loaded_guarded_snapshot(readiness):
    runtime, launcher, lease, wire, fixture_client, catalog, _ = readiness
    lease.loaded_model_path = lease.model_path.parent / 'execution-staging' / ('a' * 48) / lease.model_path.name
    add_ready(wire, fixture_client)

    with pytest.raises(RuntimeUnavailable, match='runtime_build_model_or_context_mismatch'):
        runtime.start(model(catalog))
    assert lease.closed == 1


def test_complete_real_content_length_body_does_not_reconfigure_closed_socket():
    import socket
    from threading import Thread
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(1);listener.settimeout(1)
    port=listener.getsockname()[1]
    failures=[]
    def serve():
        try:
            connection,_=listener.accept()
            with connection:
                connection.settimeout(1)
                data=b''
                while b'\r\n\r\n' not in data:
                    data+=connection.recv(4096)
                connection.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 12\r\nConnection: close\r\n\r\n{"ok": true}')
        except Exception as error:
            failures.append(type(error).__name__)
    thread=Thread(target=serve);thread.start()
    client=LoopbackClient(port,'x'*32,RuntimeLimits(),
                          peer_verifier=lambda raw:True)
    try:
        assert client.request('GET','/props')=={'ok':True}
        assert not failures
    finally:
        client.cancel();listener.close();thread.join(1)
