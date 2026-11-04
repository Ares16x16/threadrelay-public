"""Boundary tests: removing validation must expose handler calls, never a send."""
import io
import json
from pathlib import Path
import struct

import pytest

from threadrelay.contracts import ProtocolError, validate_contract
from threadrelay.main import dispatch, encode_frame, read_frame, serve_one


def envelope(kind="QueryState", payload=None):
    return {"version": 1, "id": "test-1", "type": kind, "payload": payload or {}}


def raw_frame(payload):
    return struct.pack(">I", len(payload)) + payload


class FragmentedReader(io.BytesIO):
    def read(self, count=-1):
        return super().read(min(count, 2))


def test_rejects_unknown_command_and_oversize_frame():
    calls = []
    invalid = raw_frame(json.dumps(envelope("ExecuteShell")).encode())
    oversized = struct.pack(">I", 1_048_577)
    for frame in (invalid, oversized):
        with pytest.raises(ProtocolError):
            serve_one(io.BytesIO(frame), io.BytesIO(), lambda command: calls.append(command))
    assert calls == []


@pytest.mark.parametrize("frame", [
    b"\x00\x00", struct.pack(">I", 0), struct.pack(">I", 1) + b"\xff",
    struct.pack(">I", 2) + b"{", raw_frame(b"not json"), raw_frame(b"null"),
    raw_frame(b'{"version":1,"id":"x","type":"Stop","payload":{},"type":"Start"}'),
    raw_frame(b'{"version":1,"id":"x","type":"Stop","payload":{},"extra":true}'),
    raw_frame(b'{"version":1,"id":"x","type":"Stop","payload":{"path":"/tmp"}}'),
])
def test_malformed_frames_never_dispatch(frame):
    calls = []
    with pytest.raises(ProtocolError):
        serve_one(FragmentedReader(frame), io.BytesIO(), lambda command: calls.append(command))
    assert calls == []


def test_incremental_reader_handles_adjacent_frames_and_clean_eof():
    command = envelope()
    stream = FragmentedReader(encode_frame(command) + encode_frame(command))
    assert read_frame(stream) == command
    assert read_frame(stream) == command
    assert read_frame(stream) is None


def test_maximum_payload_boundary_uses_bytes_not_characters():
    # Valid JSON payload, exactly the permitted byte count; oversized payload is rejected.
    frame = raw_frame(b'"' + b'a' * (1_048_576 - 2) + b'"')
    assert len(read_frame(io.BytesIO(frame))) == 1_048_574
    with pytest.raises(ProtocolError):
        encode_frame("a" * 1_048_575)


def test_query_state_is_stopped_and_start_is_fail_closed():
    expected = {
        "state": "stopped", "counters": {"observed": 0, "matched": 0, "queued": 0, "sent": 0},
        "attention_reason": None,
    }
    event = dispatch(envelope())
    assert event == {"version": 1, "id": "test-1", "type": "State", "payload": expected}
    validate_contract("Event", event)
    start = dispatch(envelope("Start", {"feed_url": "https://www.threads.com/"}))
    assert start["type"] == "Error"
    assert start["payload"]["code"] == "unsupported"
    assert dispatch(envelope())["payload"] == expected


def test_worker_returns_one_valid_event_without_launching_other_work():
    output = io.BytesIO()
    assert serve_one(FragmentedReader(encode_frame(envelope())), output) is True
    event = read_frame(io.BytesIO(output.getvalue()))
    assert event["type"] == "State"
    assert event["payload"]["state"] == "stopped"
    assert serve_one(io.BytesIO(), output) is False


def test_dispatch_also_validates_direct_calls():
    with pytest.raises(ProtocolError):
        dispatch(envelope("LaunchProcess", {"path": "/tmp/no"}))


IDENTITY_FIXTURES = json.loads((Path(__file__).parents[2] / "contracts/fixtures.json").read_text())


@pytest.mark.parametrize("valid,case", [
    (bucket == "valid", case)
    for bucket in ("valid", "invalid")
    for case in IDENTITY_FIXTURES[bucket]
    if case["name"].startswith("account-")
], ids=lambda value: value["name"] if isinstance(value, dict) else str(value))
def test_identity_bound_shared_protocol(valid, case):
    if valid:
        validate_contract(case["contract"], case["value"])
    else:
        with pytest.raises(ProtocolError):
            validate_contract(case["contract"], case["value"])


def test_oversized_json_integer_is_static_protocol_rejection():
    from io import BytesIO
    import struct
    import sys
    payload = b'{"n":' + b'9' * (sys.get_int_max_str_digits() + 1) + b'}'
    with pytest.raises(ProtocolError, match='Invalid frame JSON'):
        read_frame(BytesIO(struct.pack('>I', len(payload)) + payload))
