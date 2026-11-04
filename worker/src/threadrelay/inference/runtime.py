"""Owned, authenticated loopback-only native decision runtime boundary.

No subprocess launcher is implemented here. Default is unavailable until a
native Task 2 supervised DependencyRoot bridge satisfies the trusted contract.
"""
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
import http.client
import json
import math
from pathlib import Path
import re
import secrets
import socket
from threading import Event, Lock, RLock, Timer
import time
from typing import Any, Protocol

from .provision import ModelManifest, ModelProvisioner


class RuntimeUnavailable(RuntimeError):
    pass


class TransportError(RuntimeError):
    pass


class _RuntimeLoading(TransportError):
    """Only a bounded, valid pinned-server HTTP 503 loading response."""


@dataclass(frozen=True)
class RuntimeLimits:
    """Conservative unmeasured safety ceilings, not machine-fit promises."""
    context_tokens: int = 4096
    max_input_bytes: int = 3072
    max_response_bytes: int = 65536
    timeout_seconds: float = 15.0
    cpu_threads: int = 4

    def __post_init__(self):
        if (type(self.context_tokens) is not int or not 2048 <= self.context_tokens <= 65536
                or type(self.max_input_bytes) is not int or not 256 <= self.max_input_bytes <= self.context_tokens - 1024
                or type(self.max_response_bytes) is not int or not 1024 <= self.max_response_bytes <= 65536
                or type(self.timeout_seconds) not in (int, float) or not math.isfinite(self.timeout_seconds)
                or not 0 < self.timeout_seconds <= 60
                or type(self.cpu_threads) is not int or not 1 <= self.cpu_threads <= 16):
            raise ValueError("invalid_runtime_limits")


@dataclass(frozen=True)
class RuntimeLaunchSpec:
    """Typed internal spec; host must not expand it into arbitrary shell args.

    Trusted native launcher verifies the approved archive/install, creates a
    same-manager Task 2 inference DependencyRoot bound to the live worker, and
    installs ownership/death guards BEFORE activation. It holds installed files
    stable through verification/load, rejects inherited LLAMA/HF overrides and
    downloader/router/tool settings, uses local -m, --alias model_id, --offline,
    --host 127.0.0.1, one slot, configured context/thread ceilings, and the token
    through a private env/file handoff (never renderer, logs or process argv).
    No wrapper may create unregistered process groups. All separate groups must
    be registered before activation; unknown ownership/resource enforcement
    fails closed. Native install/platform/crash proof remains Task 10 work.
    """
    model_file: Path
    model_id: str
    model_sha256: str
    runtime_commit: str
    context_tokens: int
    cpu_threads: int
    auth_token: str = field(repr=False)
    cancelled: Event = field(repr=False, compare=False)
    host: str = "127.0.0.1"
    offline: bool = True


class OwnedRuntimeLease(Protocol):
    """Opaque native capability, NOT reconstructible from URL, PID or IPC JSON.

    Receipt fields attest the supervised, pinned local install. poll checks the
    exact native handle. within_resource_limits checks actual resource health;
    inability to enforce/observe the configured budget must return false.
    close cancels and requests bounded owned-group shutdown; unconfirmed roots
    remain registered and report stopped:false for later cleanup.
    """
    port: int
    model_path: Path
    loaded_model_path: Path
    model_sha256: str
    runtime_commit: str
    platform: str
    archive_sha256: str
    def poll(self) -> str: ...
    def within_resource_limits(self) -> bool: ...
    def close(self) -> dict[str, Any]: ...


class AppOwnedRuntimeLauncher(Protocol):
    def launch(self, spec: RuntimeLaunchSpec) -> OwnedRuntimeLease: ...


class RuntimeHandle:
    """Opaque local activation identity, never serializable authority."""
    __slots__ = ()


def json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise TransportError("invalid_json") from None


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


class _RevocableSocket:
    """Check revocation at each actual HTTP header/body write, not before connect."""
    def __init__(self, raw, write_lock, check):
        self._raw, self._write_lock, self._check = raw, write_lock, check

    def __getattr__(self, name):
        return getattr(self._raw, name)

    def sendall(self, data, *args, **kwargs):
        with self._write_lock:
            self._check()
            return self._raw.sendall(data, *args, **kwargs)


class LoopbackClient:
    """One bounded request at a time; no proxies, DNS, redirects or cloud URLs.

    The hard deadline aborts only this owned connection. read1 plus a monotonic
    deadline prevents a trickling body from resetting the overall budget. The
    watchdog also interrupts socket header/connect waits. stop() cancellation
    shuts down this socket; it does not control any external process/service.
    """
    ALLOWED = {("GET", "/props"), ("GET", "/v1/models"), ("POST", "/v1/systemone")}

    def __init__(self, port: int, token: str, limits: RuntimeLimits, *,
                 connection_factory=None, clock: Callable[[], float] = time.monotonic,
                 timer_factory=Timer, socket_factory=socket.socket, peer_verifier=None):
        if (type(port) is not int or not 1 <= port <= 65535 or not isinstance(token, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token)):
            raise ValueError("invalid_owned_endpoint")
        self._port, self._token, self._limits = port, token, limits
        self._factory = connection_factory or http.client.HTTPConnection
        self._owns_connector = connection_factory is None
        self._socket_factory = socket_factory
        self._peer_verifier = peer_verifier
        self._clock, self._timer_factory = clock, timer_factory
        self._cancelled, self._lock, self._request_lock = Event(), Lock(), Lock()
        self._write_lock = RLock()
        self._connection = None
        self._socket = None

    @staticmethod
    def _close(connection, pinned_socket=None):
        try:
            sock = pinned_socket if pinned_socket is not None else getattr(connection, "sock", None)
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                close = getattr(sock, "close", None)
                if close is not None:
                    close()
            connection.close()
        except OSError:
            pass

    def cancel(self):
        self._cancelled.set()
        with self._lock:
            if self._connection is not None:
                self._close(self._connection, self._socket)
        # Interrupt any in-progress send via shutdown first, then wait until
        # that write exits. Future writes see the permanent revocation event.
        # No header/body send can still be running when cancellation returns.
        with self._write_lock:
            pass

    def request(self, method: str, path: str, payload=None, *, deadline: float | None = None) -> dict[str, Any]:
        if (method, path) not in self.ALLOWED or (method == "GET" and payload is not None):
            raise TransportError("unsupported_endpoint")
        if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
            raise TransportError("invalid_deadline")
        body = None if payload is None else json_bytes(payload)
        if body is not None and len(body) > self._limits.max_input_bytes:
            raise TransportError("input_limit")
        if self._cancelled.is_set():
            raise TransportError("cancelled")
        if not self._request_lock.acquire(blocking=False):
            raise TransportError("request_busy")
        connection, timer, response, pinned_socket = None, None, None, None
        owned_socket = [None]
        expired = Event()
        deadline = min(self._clock() + self._limits.timeout_seconds,
            deadline if deadline is not None else math.inf)
        def check():
            if self._cancelled.is_set():
                raise TransportError("cancelled")
            if expired.is_set() or self._clock() >= deadline:
                raise TransportError("timeout")
        try:
            check()
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise TransportError("timeout")
            connection = self._factory("127.0.0.1", self._port, timeout=remaining)
            # Never let request()/send() implicitly reconnect after cancellation.
            connection.auto_open = False
            with self._lock:
                check()
                self._connection = connection
            if self._owns_connector:
                def connect_owned(address, timeout, source_address):
                    # Publish ownership BEFORE the potentially blocking connect;
                    # fixed IPv4 avoids resolver/proxy/address iteration races.
                    raw = self._socket_factory(socket.AF_INET, socket.SOCK_STREAM)
                    owned_socket[0] = raw
                    with self._lock:
                        self._socket = raw
                        check()
                    raw.settimeout(min(timeout, max(0.001, deadline - self._clock())))
                    raw.connect(address)
                    check()
                    # A listener check before connect is insufficient: prove
                    # this exact connected peer before any token or post bytes.
                    if self._peer_verifier is None or self._peer_verifier(raw) is not True:
                        raise TransportError("connected_peer_unverified")
                    check()
                    return _RevocableSocket(raw, self._write_lock, check)
                connection._create_connection = connect_owned
            def abort():
                expired.set()
                with self._lock:
                    self._close(connection, owned_socket[0] or pinned_socket)
                with self._write_lock:
                    pass
            timer = self._timer_factory(max(0.0, deadline - self._clock()), abort)
            timer.daemon = True
            timer.start()
            check()
            # Establish the connection without request data. Even a trusted
            # injected connector that returns after revocation is closed before
            # HTTPConnection can emit Authorization or post content.
            connection.connect()
            # HTTPConnection can detach its socket when a Connection: close
            # response starts. Retain the exact socket capability independently
            # so deadline/cancel can still interrupt HTTPResponse's body reads.
            with self._lock:
                sock = getattr(connection, "sock", None)
                pinned_socket = self._socket = owned_socket[0] or sock
                check()
                if sock is not None and not isinstance(sock, _RevocableSocket):
                    connection.sock = _RevocableSocket(sock, self._write_lock, check)
            connection.request(method, path, body=body, headers={"Authorization": "Bearer " + self._token,
                "Content-Type": "application/json", "Accept": "application/json"})
            response = connection.getresponse()
            check()
            if response.status not in (200, 503):
                raise TransportError("runtime_http_error")
            if (response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json"
                    or response.getheader("Content-Encoding", "identity") != "identity"):
                raise TransportError("invalid_response_type")
            length = response.getheader("Content-Length")
            if length is not None and (not re.fullmatch(r"[0-9]{1,9}", length) or int(length) > self._limits.max_response_bytes):
                raise TransportError("response_limit")
            chunks, count = [], 0
            while True:
                # HTTPResponse.read1 closes its backing file as soon as a
                # Content-Length body is complete. With Connection: close this
                # can also close our retained socket before another iteration.
                if length is not None and count == int(length):
                    break
                remaining = deadline - self._clock()
                if expired.is_set() or remaining <= 0:
                    raise TransportError("timeout")
                if self._cancelled.is_set():
                    raise TransportError("cancelled")
                if pinned_socket is not None:
                    pinned_socket.settimeout(remaining)
                chunk = response.read1(min(8192, self._limits.max_response_bytes - count + 1))
                if not chunk:
                    break
                count += len(chunk)
                if count > self._limits.max_response_bytes:
                    raise TransportError("response_limit")
                chunks.append(chunk)
            if expired.is_set() or self._clock() >= deadline:
                raise TransportError("timeout")
            if length is not None and count != int(length):
                raise TransportError("invalid_response_length")
            try:
                value = json.loads(b"".join(chunks).decode("utf-8"), object_pairs_hook=_pairs,
                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
                if not isinstance(value, dict):
                    raise ValueError("not_object")
                check()
                if response.status == 503:
                    if value != {"error": {"code": 503, "message": "Loading model", "type": "unavailable_error"}}:
                        raise TransportError("runtime_http_error")
                    raise _RuntimeLoading("runtime_http_error")
                return value
            except (ValueError, UnicodeError, RecursionError):
                raise TransportError("invalid_response_json") from None
        except TransportError:
            raise
        except (OSError, http.client.HTTPException, ValueError):
            reason = "cancelled" if self._cancelled.is_set() else (
                "timeout" if expired.is_set() or self._clock() >= deadline else "transport_failed")
            raise TransportError(reason) from None
        finally:
            if timer is not None:
                timer.cancel()
            with self._lock:
                pinned_socket = owned_socket[0] or pinned_socket
                if self._connection is connection:
                    self._connection = None
                    self._socket = None
            if connection is not None:
                self._close(connection, pinned_socket)
            if response is not None and hasattr(response, "close"):
                response.close()
            self._request_lock.release()


def validate_noul_response(value: Any, ids: set[str], model_id: str, context_tokens: int) -> dict[str, float]:
    """Exact official noul shape, not a Boolean, choice or chat completion."""
    def invalid():
        raise RuntimeUnavailable("invalid_decision_response")
    if (not isinstance(value, dict) or set(value) != {"model", "answers", "usage"}
            or value["model"] != model_id or not isinstance(value["answers"], dict)
            or set(value["answers"]) != ids):
        invalid()
    usage = value["usage"]
    if (not isinstance(usage, dict) or set(usage) != {"input_tokens", "output_tokens"}
            or type(usage["input_tokens"]) is not int or not 0 <= usage["input_tokens"] <= context_tokens * len(ids)
            or type(usage["output_tokens"]) is not int or usage["output_tokens"] != 0):
        invalid()
    scores = {}
    for id, answer in value["answers"].items():
        if (not isinstance(answer, dict) or set(answer) != {"type", "noul"} or answer["type"] != "noul"
                or type(answer["noul"]) not in (int, float)
                or not math.isfinite(answer["noul"]) or not 0 <= answer["noul"] <= 1):
            invalid()
        scores[id] = float(answer["noul"])
    return scores


class DecisionRuntime:
    def __init__(self, *, model_file: Path, provisioner: ModelProvisioner | None = None,
                 launcher: AppOwnedRuntimeLauncher | None = None, limits: RuntimeLimits = RuntimeLimits(),
                 setup_authorized: Callable[[], bool] = lambda: False,
                 can_infer: Callable[[], bool] = lambda: False, client_factory=LoopbackClient,
                 clock: Callable[[], float] = time.monotonic, persistent=False):
        self._file, self._provisioner = model_file, provisioner or ModelProvisioner()
        self._launcher, self._limits, self._clock = launcher, limits, clock
        self._setup_authorized, self._can_infer, self._client_factory = setup_authorized, can_infer, client_factory
        self._manifest = self._provisioner.primary_manifest
        self._lease, self._client = None, None
        self._active, self._starting, self._generation = False, False, 0
        self._busy_generation = None
        self._persistent=persistent
        self._request_client=None
        self._request_lock=Lock()
        self._request_epoch=0
        self._auth_token=None
        self._cancelled, self._lock, self._cleanup_lock = Event(), RLock(), RLock()

    @property
    def limits(self) -> RuntimeLimits:
        return self._limits

    def status(self) -> dict[str, Any]:
        """Last observed lifecycle phase; this read never starts/probes a dependency."""
        with self._lock:
            if self._starting:
                phase = 'stopping' if self._cancelled.is_set() else 'starting'
            elif self._lease is not None and (not self._active or self._cancelled.is_set()):
                phase = 'stopping'
            elif self._active:
                phase = 'classifying' if self._busy_generation == self._generation else 'ready'
            else:
                phase = 'not_loaded' if self._launcher is not None else 'unavailable'
            return {'state':phase, 'model_id':self._manifest['id']}

    @property
    def metadata(self) -> dict[str, Any]:
        with self._lock:
            return {"model_id": self._manifest["id"], "model_revision": self._manifest["version"],
                "model_sha256": self._manifest["sha256"], "quantization": self._manifest["quantization"],
                "runtime_version": self._manifest["runtime_version"], "runtime_commit": self._provisioner.runtime_pin["commit"],
                "context_tokens": self._limits.context_tokens, "max_input_bytes": self._limits.max_input_bytes,
                "cpu_threads": self._limits.cpu_threads, "max_response_bytes": self._limits.max_response_bytes,
                "timeout_seconds": self._limits.timeout_seconds,
                "reading_mode": "one-way-llama.cpp", "scores_calibrated": False}

    def _owned_ready(self):
        lease = self._lease
        if lease is None or lease.poll() != "active" or not lease.within_resource_limits():
            raise RuntimeUnavailable("runtime_ownership_or_resource_lost")

    def revoke(self):
        """Immediately close intake; actual socket cleanup never blocks lifecycle locks."""
        self._cancelled.set()
        self.cancel_request()
        client = self._client
        if client is not None:
            # LoopbackClient observes its own event before any subsequent write.
            event = getattr(client, '_cancelled', None)
            if event is not None:
                event.set()
            from threading import Thread
            Thread(target=client.cancel, name='inference-revocation', daemon=True).start()

    def cancel_request(self):
        """Invalidate only an in-flight decision; the verified model stays owned."""
        with self._request_lock:
            self._request_epoch+=1
            client=self._request_client
            if client is not None:
                event=getattr(client,'_cancelled',None)
                if event is not None:event.set()
        if client is not None:
            from threading import Thread
            Thread(target=client.cancel,name='inference-request-cancellation',daemon=True).start()

    def _new_client(self,lease,token):
        if self._client_factory is LoopbackClient:
            return LoopbackClient(lease.port,token,self._limits,
                peer_verifier=getattr(lease,'verify_connection',None),clock=self._clock)
        return self._client_factory(lease.port,token,self._limits)

    def start(self, model: ModelManifest, *, cancellation=None) -> RuntimeHandle:
        model = deepcopy(model)
        with self._lock:
            if self._lease is not None:
                raise RuntimeUnavailable("runtime_already_active" if self._active else "runtime_cleanup_pending")
            if self._starting:
                raise RuntimeUnavailable("runtime_starting")
            if (cancellation is not None and cancellation.is_set()) or not self._setup_authorized():
                raise RuntimeUnavailable("setup_not_authorized")
            if self._launcher is None:
                raise RuntimeUnavailable("native_owned_runtime_launcher_unavailable")
            self._starting = True
            self._generation += 1
            generation = self._generation
            cancelled = self._cancelled = Event()
        try:
            def is_cancelled():
                return cancelled.is_set() or (cancellation is not None and cancellation.is_set())
            verification = self._provisioner.verify(model, self._file, cancelled=is_cancelled)
            if not verification["valid"]:
                raise RuntimeUnavailable(verification["reason"])
            with self._lock:
                if is_cancelled() or generation != self._generation or not self._setup_authorized():
                    raise RuntimeUnavailable("runtime_cancelled")
                pin = self._provisioner.runtime_pin
                token = secrets.token_urlsafe(32)
                spec = RuntimeLaunchSpec(self._file, model["id"], model["sha256"], pin["commit"],
                    self._limits.context_tokens, self._limits.cpu_threads, token, cancelled)
            lease = self._launcher.launch(spec)
            with self._lock:
                self._lease = lease  # retain ownership BEFORE any validation
                if is_cancelled() or generation != self._generation:
                    raise RuntimeUnavailable("runtime_cancelled")
                artifact = next((a for a in pin["artifacts"] if a["platform"] == lease.platform), None)
                if (artifact is None or artifact["sha256"] != lease.archive_sha256 or lease.runtime_commit != pin["commit"]
                        or lease.model_sha256 != model["sha256"] or lease.model_path != self._file
                        or type(lease.port) is not int or not 1 <= lease.port <= 65535):
                    raise RuntimeUnavailable("owned_runtime_identity_mismatch")
                self._owned_ready()
                self._auth_token=token
                self._client = self._new_client(lease,token)
                client = self._client
                self._manifest = deepcopy(model)
            # One budget covers readiness retries and every startup HTTP check.
            # The native launch itself retains its host-owned launch deadline.
            startup_deadline = self._clock() + self._limits.timeout_seconds
            def check_startup():
                with self._lock:
                    if (is_cancelled() or generation != self._generation or not self._setup_authorized()
                            or self._lease is not lease or self._client is not client):
                        raise RuntimeUnavailable("runtime_cancelled")
                    if self._clock() >= startup_deadline:
                        raise RuntimeUnavailable("runtime_start_timeout")
                    self._owned_ready()
                    if self._clock() >= startup_deadline:
                        raise RuntimeUnavailable("runtime_start_timeout")
            def startup_request(method, path, payload=None):
                check_startup()
                try:
                    if isinstance(client, LoopbackClient):
                        result = client.request(method, path, payload, deadline=startup_deadline)
                    else:
                        result = client.request(method, path, payload)
                except _RuntimeLoading:
                    check_startup()
                    raise
                check_startup()
                return result
            while True:
                try:
                    props = startup_request("GET", "/props")
                    break
                except _RuntimeLoading:
                    # Every retry reconnects through the same lease's connected
                    # peer verifier before releasing this activation's API key.
                    cancelled.wait(min(0.05, max(0.0, startup_deadline - self._clock())))
            build = re.fullmatch(r"b?11946-([0-9a-f]{7,40})", props.get("build_info", ""))
            if (build is None or not pin["commit"].startswith(build.group(1)) or props.get("model_path") != str(getattr(lease, "loaded_model_path", lease.model_path))
                    or type(props.get("default_generation_settings", {}).get("n_ctx")) is not int
                    or props["default_generation_settings"]["n_ctx"] < self._limits.context_tokens):
                raise RuntimeUnavailable("runtime_build_model_or_context_mismatch")
            if is_cancelled() or not self._setup_authorized():
                raise RuntimeUnavailable("runtime_cancelled")
            models = startup_request("GET", "/v1/models").get("data")
            if (not isinstance(models, list) or len(models) != 1 or models[0].get("id") != model["id"]
                    or "decisions" not in models[0].get("architecture", {}).get("output_modalities", [])
                    or "text" not in models[0].get("architecture", {}).get("input_modalities", [])):
                raise RuntimeUnavailable("native_decisions_unavailable")
            if is_cancelled() or not self._setup_authorized():
                raise RuntimeUnavailable("runtime_cancelled")
            canary = startup_request("POST", "/v1/systemone", {"state": {"post_text": "Synthetic setup canary.", "quote": None},
                "questions": {"__canary": {"type": "noul", "instructions": "Is this a synthetic canary?"}}})
            validate_noul_response(canary, {"__canary"}, model["id"], self._limits.context_tokens)
            with self._lock:
                check_startup()
                self._active = True
                return RuntimeHandle()
        except Exception as error:
            self.stop()
            if isinstance(error, RuntimeUnavailable):
                raise
            raise RuntimeUnavailable("runtime_start_failed") from None
        finally:
            with self._lock:
                self._starting = False

    def decide(self, state: dict[str, Any], questions: dict[str, Any]) -> dict[str, float]:
        generation = None
        request_epoch=None
        client=None
        try:
            # The only supported runtime operation is typed text-only noul.
            if (not isinstance(state, dict) or set(state) != {"post_text", "quote"} or not isinstance(state["post_text"], str)
                    or (state["quote"] is not None and (not isinstance(state["quote"], dict)
                        or set(state["quote"]) != {"author", "text"} or not all(isinstance(v, str) for v in state["quote"].values())))
                    or not isinstance(questions, dict) or not 1 <= len(questions) <= 100):
                raise RuntimeUnavailable("invalid_decision_input")
            for id, q in questions.items():
                if (not isinstance(id, str) or not 1 <= len(id) <= 128 or not isinstance(q, dict)
                        or set(q) != {"type", "instructions"} or q["type"] != "noul" or not isinstance(q["instructions"], str)):
                    raise RuntimeUnavailable("invalid_decision_input")
            payload = {"state": state, "questions": questions}
            if len(json_bytes(payload)) > self._limits.max_input_bytes:
                raise RuntimeUnavailable("input_limit")
            with self._request_lock:request_epoch=self._request_epoch
            if not self._can_infer():raise RuntimeUnavailable('runtime_not_available')
            with self._lock:
                if not self._active or self._cancelled.is_set():
                    raise RuntimeUnavailable("runtime_not_available")
                if self._persistent and self._busy_generation is not None:
                    raise RuntimeUnavailable('runtime_busy')
                generation=self._generation
                self._owned_ready()
                client=self._new_client(self._lease,self._auth_token) if self._persistent else self._client
                with self._request_lock:
                    if request_epoch!=self._request_epoch:raise RuntimeUnavailable('runtime_cancelled')
                    self._request_client=client
                model_id = self._manifest["id"]
                self._busy_generation = generation
            value = client.request("POST", "/v1/systemone", payload)
            scores = validate_noul_response(value, set(questions), model_id, self._limits.context_tokens)
            if not self._can_infer():raise RuntimeUnavailable('runtime_cancelled')
            with self._lock:
                if not self._active or self._cancelled.is_set() or generation != self._generation or request_epoch!=self._request_epoch:
                    raise RuntimeUnavailable("runtime_cancelled")
                self._owned_ready()
            return scores
        except Exception:
            if not self._persistent:self.stop()
            elif generation is not None:
                try:self._owned_ready()
                except Exception:self.stop()
            raise RuntimeUnavailable("inference_unavailable") from None
        finally:
            if self._persistent and client is not None:
                try:client.cancel()
                except Exception:pass
            with self._request_lock:
                if self._request_client is client:self._request_client=None
            with self._lock:
                if self._busy_generation == generation:
                    self._busy_generation = None

    def stop(self) -> dict[str, Any]:
        with self._cleanup_lock:
            with self._lock:
                self._active = False
                self._busy_generation = None
                self._generation += 1
                self._cancelled.set()
                lease, client = self._lease, self._client
                with self._request_lock:
                    request_client,self._request_client=self._request_client,None
                    self._request_epoch+=1
                self._auth_token=None
                self._client = None
            if request_client is not None and request_client is not client:
                try:request_client.cancel()
                except Exception:pass
            if client is not None:
                try:
                    client.cancel()
                except Exception:
                    pass  # owned native close still runs
            if lease is None:
                with self._lock:
                    # A native launch can still be in flight before it returns
                    # its lease. Cancellation revokes it, but is not evidence
                    # that the outstanding activation/owned group has ended.
                    return {"stopped": not self._starting,
                        "remaining": ["inference"] if self._starting else []}
            try:
                report = lease.close()
                if (not isinstance(report, dict) or set(report) != {"stopped", "remaining"}
                        or type(report["stopped"]) is not bool or report["remaining"] not in ([], ["inference"])
                        or report["stopped"] != (report["remaining"] == [])):
                    raise ValueError("invalid_shutdown_report")
            except Exception:
                report = {"stopped": False, "remaining": ["inference"]}
            with self._lock:
                if report["stopped"] and self._lease is lease:
                    self._lease = None
                if self._starting:
                    return {"stopped": False, "remaining": ["inference"]}
            return deepcopy(report)
