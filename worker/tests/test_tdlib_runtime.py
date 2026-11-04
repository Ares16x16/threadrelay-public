"""Opt-in target-native TDLib smoke harness. Never writes acceptance evidence.

Identity/clean-client checks are NOT persistent-pending/no-flush acceptance.
Those separately observed cases are explicitly reported Not run, not asserted
from an empty queue or a build receipt. See docs/verification/native-matrix.md.
"""

import ctypes
import hashlib
import json
from pathlib import Path
import platform
import time
import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def native_fixture(request):
    if not request.config.getoption("--tdlib-native") or not request.config.getoption(
        "--tdlib-live"
    ):
        pytest.skip(
            "Not run: explicit native/runtime + controlled live-test opt-ins required"
        )
    target = {
        ("Windows", "AMD64"): "windows-x64",
        ("Darwin", "arm64"): "macos-arm64",
    }.get((platform.system(), platform.machine()))
    if target is None:
        pytest.skip("Not run: Windows x64/macOS Apple silicon target required")
    fixture = request.config.getoption("--tdlib-fixture")
    if not fixture:
        pytest.skip("Not run: exact controlled fixture authorization required")
    value = json.loads(Path(fixture).read_text())
    if (
        set(value)
        != {"schema_version", "target", "installed_resources", "authorization", "scope"}
        or value["schema_version"] != 1
        or value["target"] != target
    ):
        pytest.fail("Invalid controlled fixture")
    if value["authorization"] != "explicitly-authorized-native-smoke" or value[
        "scope"
    ] != ["identity", "clean-offline-close"]:
        pytest.fail("Exact smoke authorization scope required; no sends are performed")
    root = Path(value["installed_resources"])
    if not root.is_absolute():
        pytest.fail("Absolute installed resource root required")
    manifest = json.loads((ROOT / "runtimes/tdlib-manifest.json").read_text())
    pin = manifest["targets"][target]
    if pin["artifact_sha256"] is None:
        pytest.skip("Not run: reviewed target-native library hash is absent")
    library = root / "native/tdlib" / pin["library"]
    if not library.is_file() or library.is_symlink():
        pytest.fail("Native library missing")
    with library.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != pin["artifact_sha256"]:
            pytest.fail("Native library identity mismatch")
    return library, manifest


def load(native_fixture):
    from threadrelay.delivery.telegram import NativeJsonBinding

    path, _ = native_fixture
    kwargs = {"winmode": 0x100 | 0x800} if platform.system() == "Windows" else {}
    # Isolated, explicitly authorized native test loading. It never grants a
    # production host capability or modifies its compiled target inventories.
    return NativeJsonBinding(ctypes.CDLL(str(path), **kwargs))


def test_pinned_native_abi_and_version(native_fixture):
    from threadrelay.delivery.telegram import _copy_native_json

    binding = load(native_fixture)
    _, manifest = native_fixture
    for option, expected in [
        ("version", manifest["version"]),
        ("commit_hash", manifest["commit"]),
    ]:
        pointer = binding._library.td_execute(
            json.dumps({"@type": "getOption", "name": option}).encode()
        )
        assert pointer
        result = json.loads(_copy_native_json(pointer))
        assert result == {"@type": "optionValueString", "value": expected}


def test_clean_client_queues_offline_request_and_owned_close_without_initializing(native_fixture):
    binding = load(native_fixture)
    client = binding.create()
    binding.send(
        client,
        {
            "@type": "setNetworkType",
            "type": {"@type": "networkTypeNone"},
            "@extra": "native-smoke:offline",
        },
    )
    # Pinned TDLib queues pre-init network options until parameters. This
    # no-credentials smoke observes only request queuing and owned Closed;
    # it does NOT establish offline acknowledgement or no-pending-send-flush.
    try:
        pass  # Deliberately do not initialize a database or wait for impossible Ok.
    finally:
        binding.send(client, {"@type": "close", "@extra": "native-smoke:close"})
        closed = False
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            event = binding.receive(0.25)
            if (
                event
                and event.get("@client_id") == client
                and event.get("authorization_state", {}).get("@type")
                == "authorizationStateClosed"
            ):
                closed = True
                break
        assert closed, "Native client closure unconfirmed"


def test_persistent_pending_no_flush_is_not_established_by_smoke(native_fixture):
    pytest.skip(
        "Not run: persistent pending queue, exact authorized account/destination, independent network/destination observation and lifecycle matrix require target acceptance; clean-client smoke cannot prove no-flush"
    )


def test_no_init_smoke_matches_pinned_queued_option_without_ack(monkeypatch):
    """No-service harness regression, explicitly not native offline/no-flush proof."""
    calls=[]
    class QueuedPin:
        def create(self):return 42
        def send(self,client,request):calls.append(request)
        def receive(self,timeout):
            assert calls[-1]['@type']=='close'
            return {'@client_id':42,'@type':'updateAuthorizationState','authorization_state':{'@type':'authorizationStateClosed'}}
    monkeypatch.setattr(__import__(__name__), 'load', lambda _:QueuedPin())
    test_clean_client_queues_offline_request_and_owned_close_without_initializing(None)
    assert [r['@type'] for r in calls]==['setNetworkType','close']
    assert calls[0]['type']=={'@type':'networkTypeNone'}
