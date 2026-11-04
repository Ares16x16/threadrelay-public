"""Synthetic TDLib boundary tests: no native library or live account."""
from pathlib import Path
from uuid import uuid4

import pytest

from threadrelay.delivery.auth import AuthController, AuthFailure
from threadrelay.delivery.telegram import TelegramClient, NativeJsonBinding, NativeUnavailable
from threadrelay.storage.database import Database
from threadrelay.storage.outbox import AccountStore


class FixtureBinding:
    def __init__(self):
        self.requests = []
        self.events = []
        self.created = 0
    def create(self):
        self.created += 1
        return 42
    def send(self, client_id, request):
        self.requests.append(request)
    def receive(self, timeout):
        return self.events.pop(0) if self.events else None


class MemorySecretHandoff:
    """Test-only transient handoff. Not a production vault or IPC transport."""
    def __init__(self):
        self.values = {}
    def put(self, value):
        ref = uuid4().hex
        self.values[ref] = value
        return ref
    def take(self, ref):
        return self.values.pop(ref)


class FixtureVault:
    """Test-only in-memory backend; never a production credential store."""
    def resolve(self, ref):
        return {"api_id": 12345, "api_hash": uuid4().hex, "database_key": uuid4().bytes + uuid4().bytes}


def state(name, **fields):
    return {"@client_id": 42, "@type": "updateAuthorizationState", "authorization_state": {"@type": "authorizationState" + name, **fields}}


def build(tmp_path):
    db = Database(tmp_path / "app.db")
    binding = FixtureBinding()
    client = TelegramClient(lambda: binding, FixtureVault(), tmp_path / "sessions")
    handoff = MemorySecretHandoff()
    auth = AuthController(client, AccountStore(db), handoff, clock=lambda: 10.0, challenge_id=lambda: uuid4().hex)
    return db, binding, client, handoff, auth


def test_queries_are_inert_and_start_requires_no_pending(tmp_path):
    db, binding, client, handoff, auth = build(tmp_path)
    assert auth.snapshot().auth_state == "logged_out"
    assert binding.created == 0
    with pytest.raises(AuthFailure, match="pending"):
        auth.begin("vault:fixture", pending=True)
    assert binding.created == 0
    db.close()


def test_offline_order_and_minimal_encrypted_parameters(tmp_path):
    db, binding, client, handoff, auth = build(tmp_path)
    auth.begin("vault:fixture", pending=False)
    assert [r["@type"] for r in binding.requests] == ["setNetworkType"]
    binding.events.append(state("WaitTdlibParameters"))
    auth.poll()
    assert [r["@type"] for r in binding.requests][:2] == ["setNetworkType", "setTdlibParameters"]
    p = binding.requests[1]
    assert "parameters" not in p and p["database_encryption_key"]
    assert all(p[k] is False for k in ("use_file_database", "use_chat_info_database", "use_message_database", "use_secret_chats"))
    assert Path(p["database_directory"]).parent == (tmp_path / "sessions").resolve()
    assert binding.requests[0]["type"]["@type"] == "networkTypeNone"
    db.close()


def test_challenges_are_single_use_and_static_errors(tmp_path):
    db, binding, client, handoff, auth = build(tmp_path)
    auth.begin("vault:fixture", pending=False)
    binding.events.append(state("WaitPhoneNumber")); auth.poll()
    phone = auth.challenge()
    ref = handoff.put(uuid4().hex)
    auth.submit(phone.challenge_id, "phone", ref)
    assert binding.requests[-1]["@type"] == "setAuthenticationPhoneNumber"
    with pytest.raises(AuthFailure, match="challenge"):
        auth.submit(phone.challenge_id, "phone", ref)
    binding.events.append(state("WaitCode")); auth.poll()
    code = auth.challenge()
    with pytest.raises(AuthFailure, match="challenge"):
        auth.submit(code.challenge_id, "password", handoff.put(uuid4().hex))
    auth.submit(code.challenge_id, "code", handoff.put(uuid4().hex))
    assert binding.requests[-1]["@type"] == "checkAuthenticationCode"
    binding.events.append(state("WaitPassword")); auth.poll()
    password = auth.challenge()
    auth.submit(password.challenge_id, "password", handoff.put(uuid4().hex))
    assert binding.requests[-1]["@type"] == "checkAuthenticationPassword"
    db.close()


def test_qr_links_private_unsupported_states_and_cancel(tmp_path):
    db, binding, client, handoff, auth = build(tmp_path)
    auth.begin("vault:fixture", pending=False)
    binding.events.append(state("WaitPhoneNumber")); auth.poll()
    auth.request_qr(auth.challenge().challenge_id)
    assert binding.requests[-1]["@type"] == "requestQrCodeAuthentication"
    private_link = "tg://" + uuid4().hex
    binding.events.append(state("WaitOtherDeviceConfirmation", link=private_link)); auth.poll()
    assert auth.challenge().kind == "qr"
    assert private_link not in repr(auth.snapshot()) + repr(auth.challenge())
    assert auth.take_private_qr() == private_link
    binding.events.append(state("WaitPremiumPurchase")); auth.poll()
    assert auth.challenge() is None
    assert auth.status == "unsupported_auth_state"
    auth.cancel()
    assert binding.requests[-1]["@type"] == "close"
    db.close()


def test_ready_requires_regular_user_and_logout_revokes_epoch(tmp_path):
    db, binding, client, handoff, auth = build(tmp_path)
    auth.begin("vault:fixture", pending=False)
    binding.events.append(state("Ready")); auth.poll()
    assert auth.snapshot().auth_state != "ready"
    assert binding.requests[-1]["@type"] == "getMe"
    binding.events.append({"@client_id":42,"@type":"user","@extra":"auth:get-me","id":123,"type":{"@type":"userTypeRegular"},"phone_number":"discard"})
    auth.poll()
    snapshot = auth.snapshot()
    assert snapshot.account_id == "123" and snapshot.auth_state == "ready"
    assert AccountStore(db).current()["epoch"] == snapshot.epoch
    auth.logout(pending=True)
    assert auth.snapshot().revocation_pending
    assert auth.snapshot().epoch == snapshot.epoch + 1
    assert binding.requests[-1]["@type"] != "logOut"
    assert AccountStore(db).current() is None
    db.close()


def test_unrelated_updates_are_discarded_and_native_gate_closed(tmp_path):
    db, binding, client, handoff, auth = build(tmp_path)
    auth.begin("vault:fixture", pending=False)
    binding.events.extend([{"@client_id":42,"@type":"updateNewMessage","message":{"text":uuid4().hex}}, {"@client_id":42,"@type":"updateUser","phone_number":uuid4().hex}])
    assert auth.poll() is None and auth.poll() is None
    assert not any(r["@type"] in {"getChatHistory", "getContacts", "downloadFile", "viewMessages"} for r in binding.requests)
    with pytest.raises(NativeUnavailable):
        NativeJsonBinding.load_verified(None)
    db.close()


def test_expired_challenge_rejects_before_secret_handoff(tmp_path):
    db = Database(tmp_path / "app.db")
    binding = FixtureBinding(); clock = [0.0]; handoff = MemorySecretHandoff()
    auth = AuthController(TelegramClient(lambda: binding, FixtureVault(), tmp_path / "sessions"), AccountStore(db), handoff, clock=lambda: clock[0], challenge_ttl=5)
    auth.begin("vault:fixture", pending=False)
    binding.events.append(state("WaitCode")); auth.poll()
    challenge = auth.challenge(); ref = handoff.put(uuid4().hex)
    clock[0] = 5.0
    with pytest.raises(AuthFailure, match="challenge"):
        auth.submit(challenge.challenge_id, "code", ref)
    assert ref in handoff.values
    db.close()


def test_account_directory_is_stable_across_offline_restart(tmp_path):
    binding = FixtureBinding()
    root = tmp_path / "sessions"
    first = TelegramClient(lambda: binding, FixtureVault(), root)
    first.initialize_offline("vault:fixture")
    binding.events.append(state("WaitTdlibParameters")); first.authorization_event()
    first_path = binding.requests[1]["database_directory"]
    first.close(); binding.events.append(state("Closed")); first.authorization_event()
    second_binding = FixtureBinding()
    second = TelegramClient(lambda: second_binding, FixtureVault(), root)
    second.initialize_offline("vault:fixture")
    second_binding.events.append(state("WaitTdlibParameters")); second.authorization_event()
    assert second_binding.requests[1]["database_directory"] == first_path


def test_revocation_pending_cannot_be_reactivated_by_get_me(tmp_path):
    db, binding, client, handoff, auth = build(tmp_path)
    auth.begin("vault:fixture", pending=False)
    binding.events.extend([state("Ready"), {"@client_id":42,"@type":"user","@extra":"auth:get-me","id":123,"type":{"@type":"userTypeRegular"}}])
    auth.poll(); auth.poll(); auth.logout(pending=True)
    client.close(); binding.events.append(state("Closed")); client.authorization_event()
    another_binding = FixtureBinding()
    another = AuthController(TelegramClient(lambda: another_binding, FixtureVault(), tmp_path / "sessions"), AccountStore(db), handoff, clock=lambda: 10.0)
    another.begin("vault:fixture", pending=False)
    another_binding.events.extend([state("Ready"), {"@client_id":42,"@type":"user","@extra":"auth:get-me","id":123,"type":{"@type":"userTypeRegular"}}])
    another.poll()
    with pytest.raises(AuthFailure, match="identity"):
        another.poll()
    assert AccountStore(db).current() is None
    db.close()


def test_wrong_code_can_be_retried_with_new_challenge_and_no_error_text(tmp_path):
    db, binding, client, handoff, auth = build(tmp_path)
    auth.begin("vault:fixture", pending=False)
    binding.events.append(state("WaitCode")); auth.poll()
    old = auth.challenge()
    auth.submit(old.challenge_id, "code", handoff.put(uuid4().hex))
    raw_error = uuid4().hex
    binding.events.append({"@client_id":42, "@type":"error", "@extra":binding.requests[-1]["@extra"], "message":raw_error})
    assert auth.poll() == "error"
    new = auth.challenge()
    assert new.kind == "code" and new.challenge_id != old.challenge_id
    assert raw_error not in repr(new) + repr(auth.snapshot()) + auth.status
    db.close()


def test_other_client_and_unsolicited_error_do_not_change_auth(tmp_path):
    db, binding, client, handoff, auth = build(tmp_path)
    auth.begin("vault:fixture", pending=False)
    binding.events.append(state("WaitCode")); auth.poll()
    challenge = auth.challenge()
    binding.events.extend([{"@client_id":999, "@type":"updateAuthorizationState", "authorization_state":{"@type":"authorizationStateClosed"}}, {"@client_id":42, "@type":"error", "@extra":"unrelated", "message":uuid4().hex}])
    assert auth.poll() is None and auth.poll() is None
    assert auth.challenge() == challenge
    db.close()


def test_qr_event_repr_never_discloses_private_link():
    from threadrelay.delivery.telegram import AuthEvent
    private = "tg://" + uuid4().hex
    assert private not in repr(AuthEvent("qr", private_qr=private))


def test_reconciliation_is_explicitly_unavailable_until_sender_stage(tmp_path):
    db, binding, client, handoff, auth = build(tmp_path)
    auth.begin("vault:fixture", pending=False)
    with pytest.raises(NativeUnavailable):
        client.reconcile_pending()
    assert [r["@type"] for r in binding.requests] == ["setNetworkType"]
    db.close()


def test_parameters_send_only_pinned_minimization_options(tmp_path):
    db, binding, client, handoff, auth = build(tmp_path)
    auth.begin("vault:fixture", pending=False)
    binding.events.append(state("WaitTdlibParameters")); auth.poll()
    options = {r["name"]: r["value"] for r in binding.requests if r["@type"] == "setOption"}
    assert options == {
        "ignore_background_updates": {"@type":"optionValueBoolean", "value":True},
        "ignore_inline_thumbnails": {"@type":"optionValueBoolean", "value":True},
        "disable_top_chats": {"@type":"optionValueBoolean", "value":True},
        "disable_network_statistics": {"@type":"optionValueBoolean", "value":True},
        "disable_persistent_network_statistics": {"@type":"optionValueBoolean", "value":True},
        "message_unload_delay": {"@type":"optionValueInteger", "value":"60"},
        "notification_group_count_max": {"@type":"optionValueInteger", "value":"0"},
    }
    db.close()


def test_closed_is_terminal_even_if_late_state_arrives(tmp_path):
    db, binding, client, handoff, auth = build(tmp_path)
    auth.begin("vault:fixture", pending=False)
    auth.cancel()
    binding.events.extend([state("Closed"), state("WaitCode")])
    assert auth.poll() == "closed"
    assert auth.poll() is None
    assert auth.snapshot().auth_state == "closed" and auth.challenge() is None
    db.close()


def test_ctypes_binding_copies_and_bounds_json_without_loading_native():
    import ctypes
    import json
    from threadrelay.delivery.telegram import TelegramBoundaryError
    class Function:
        def __init__(self, call): self.call = call
        def __call__(self, *args): return self.call(*args)
    class FakeLibrary:
        def __init__(self):
            self.sent = []
            self.buffer = ctypes.create_string_buffer(json.dumps({"@client_id": 9, "@type": "ok"}).encode())
            self.log_response = ctypes.create_string_buffer(b'{"@type":"ok"}')
            self.td_execute = Function(lambda data: ctypes.addressof(self.log_response))
            self.td_create_client_id = Function(lambda: 9)
            self.td_send = Function(lambda client, data: self.sent.append((client, bytes(data))))
            self.td_receive = Function(lambda timeout: ctypes.addressof(self.buffer))
    library = FakeLibrary()
    native = NativeJsonBinding(library)
    assert native.create() == 9
    native.send(9, {"@type":"close"})
    assert json.loads(library.sent[0][1]) == {"@type":"close"}
    result = native.receive(0)
    library.buffer.value = b'{}'
    assert result == {"@client_id":9, "@type":"ok"}
    with pytest.raises(TelegramBoundaryError): native.send(9, {"data":"x" * 1_048_576})
    with pytest.raises(TelegramBoundaryError): native.receive(2)


def test_verified_activation_atomically_refuses_pending_revocation(tmp_path):
    db, binding, client, handoff, auth = build(tmp_path)
    store = AccountStore(db)
    snapshot = {"account_id":"123", "epoch":0, "auth_state":"ready", "display_name":None, "network_state":"offline", "revocation_pending":False}
    store.activate(snapshot)
    epoch = store.invalidate_epoch("123")
    with pytest.raises(ValueError, match="revocation"):
        store.activate({**snapshot, "epoch":epoch}, require_clear_revocation=True)
    assert store.current() is None
    db.close()


def test_snapshot_queries_restore_only_offline_unverified_identity(tmp_path):
    db = Database(tmp_path / "app.db")
    store = AccountStore(db)
    store.activate({"account_id":"123", "epoch":3, "auth_state":"ready", "display_name":None, "network_state":"online", "revocation_pending":False})
    binding = FixtureBinding()
    auth = AuthController(TelegramClient(lambda: binding, FixtureVault(), tmp_path / "sessions"), store, MemorySecretHandoff(), clock=lambda: 1.0)
    snapshot = auth.snapshot()
    assert snapshot.account_id == "123" and snapshot.epoch == 3
    assert snapshot.auth_state == "unknown" and snapshot.network_state == "offline"
    assert binding.created == 0
    db.close()


def test_vault_denial_is_static_and_cannot_send_parameters(tmp_path):
    class Denied:
        def resolve(self, ref): raise RuntimeError("private vault diagnostic")
    db = Database(tmp_path / "app.db"); binding = FixtureBinding()
    auth = AuthController(TelegramClient(lambda: binding, Denied(), tmp_path / "sessions"), AccountStore(db), MemorySecretHandoff(), clock=lambda: 0.0)
    auth.begin("vault:fixture", pending=False)
    binding.events.append(state("WaitTdlibParameters"))
    with pytest.raises(AuthFailure, match="Authentication unavailable") as failure: auth.poll()
    assert "private vault diagnostic" not in str(failure.value)
    assert [r["@type"] for r in binding.requests] == ["setNetworkType"]
    db.close()


def test_unverified_or_paid_account_type_never_becomes_ready(tmp_path):
    db, binding, client, handoff, auth = build(tmp_path)
    auth.begin("vault:fixture", pending=False)
    binding.events.extend([state("Ready"), {"@client_id":42, "@type":"user", "@extra":"auth:get-me", "id":123, "type":{"@type":"userTypeBot"}}])
    auth.poll(); auth.poll()
    assert auth.snapshot().auth_state != "ready" and AccountStore(db).current() is None
    db.close()


def test_single_session_owner_rejects_second_client_until_closed(tmp_path):
    from threadrelay.delivery.telegram import TelegramBoundaryError
    first_binding = FixtureBinding(); second_binding = FixtureBinding()
    first = TelegramClient(lambda: first_binding, FixtureVault(), tmp_path / "sessions")
    second = TelegramClient(lambda: second_binding, FixtureVault(), tmp_path / "sessions")
    first.initialize_offline("vault:fixture")
    with pytest.raises(TelegramBoundaryError): second.initialize_offline("vault:fixture")
    first.close(); first_binding.events.append(state("Closed")); first.authorization_event()
    second.initialize_offline("vault:fixture")
    assert second_binding.created == 1


def test_durable_unresolved_outbox_blocks_auth_even_if_caller_says_clean(tmp_path):
    from test_storage import snapshot, post, delivery, fixture
    from threadrelay.storage.posts import PostStore
    from threadrelay.storage.outbox import Outbox
    db = Database(tmp_path / "app.db")
    accounts = AccountStore(db); accounts.activate(snapshot()); PostStore(db).upsert(post())
    Outbox(db).save_approval(fixture("Approval")); Outbox(db).enqueue(delivery())
    binding = FixtureBinding()
    auth = AuthController(TelegramClient(lambda: binding, FixtureVault(), tmp_path / "sessions"), accounts, MemorySecretHandoff(), clock=lambda: 0.0)
    with pytest.raises(AuthFailure, match="pending"):
        auth.begin("vault:fixture", pending=False)
    assert binding.created == 0
    db.close()


def test_native_binding_bounds_response_before_copy(monkeypatch):
    import ctypes
    from threadrelay.delivery.telegram import TelegramBoundaryError
    class Function:
        def __init__(self, call): self.call = call
        def __call__(self, *args): return self.call(*args)
    class Library:
        def __init__(self):
            self.buffer = ctypes.create_string_buffer(b'x' * (1_048_576 + 1))
            self.td_execute = Function(lambda _: None)
            self.td_create_client_id = Function(lambda: 9)
            self.td_send = Function(lambda *_: None)
            self.td_receive = Function(lambda _: ctypes.addressof(self.buffer))
    native = NativeJsonBinding(Library())
    original = ctypes.string_at
    def bounded_copy(pointer, size=None):
        assert size is not None, "unbounded C string copy"
        return original(pointer, size)
    monkeypatch.setattr(ctypes, "string_at", bounded_copy)
    with pytest.raises(TelegramBoundaryError, match="response"):
        native.receive(0)


def test_failed_first_send_after_native_create_keeps_session_owned(tmp_path):
    from threadrelay.delivery.telegram import TelegramBoundaryError
    class FailingBinding(FixtureBinding):
        def send(self, client_id, request): raise RuntimeError("synthetic failure")
    first = TelegramClient(FailingBinding, FixtureVault(), tmp_path / "sessions")
    second = TelegramClient(FixtureBinding, FixtureVault(), tmp_path / "sessions")
    with pytest.raises(TelegramBoundaryError): first.initialize_offline("vault:fixture")
    with pytest.raises(TelegramBoundaryError): second.initialize_offline("vault:other")


def test_clean_auth_network_stays_blocked_until_configuration_acknowledged(tmp_path):
    from threadrelay.delivery.telegram import NetworkPermit
    db, binding, client, handoff, auth = build(tmp_path)
    auth.begin("vault:fixture", pending=False)
    permit = NetworkPermit("clean_auth", 0, False)
    with pytest.raises(AuthFailure, match="Network authority"):
        auth.enable_clean_auth(permit)
    binding.events.append(state("WaitTdlibParameters")); auth.poll()
    with pytest.raises(AuthFailure, match="Network authority"):
        auth.enable_clean_auth(permit)
    for request in binding.requests:
        if request["@type"] in {"setNetworkType", "setTdlibParameters", "setOption"}:
            binding.events.append({"@client_id":42,"@type":"ok","@extra":request["@extra"]})
    while binding.events: auth.poll()
    auth.enable_clean_auth(permit)
    assert binding.requests[-1]["type"]["@type"] == "networkTypeOther"
    db.close()


def test_verified_account_persists_only_opaque_vault_reference(tmp_path):
    db, binding, client, handoff, auth = build(tmp_path)
    auth.begin("vault:fixture", pending=False)
    binding.events.extend([state("Ready"), {"@client_id":42,"@type":"user","@extra":"auth:get-me","id":123,"type":{"@type":"userTypeRegular"}}])
    auth.poll(); auth.poll()
    with db.connection() as con:
        row = con.execute("SELECT vault_ref,snapshot FROM accounts WHERE account_id='123'").fetchone()
    assert row["vault_ref"] == "vault:fixture"
    assert "api_hash" not in row["snapshot"]
    db.close()


def test_cancel_ignores_late_submission_error_and_auth_challenge(tmp_path):
    db, binding, client, handoff, auth = build(tmp_path)
    auth.begin("vault:fixture", pending=False)
    binding.events.append(state("WaitCode")); auth.poll()
    auth.submit(auth.challenge().challenge_id, "code", handoff.put(uuid4().hex))
    extra = binding.requests[-1]["@extra"]
    auth.cancel()
    binding.events.extend([{"@client_id":42,"@type":"error","@extra":extra}, state("WaitCode"), state("Closed")])
    auth.poll(); auth.poll()
    assert auth.challenge() is None and auth.status == "closing"
    assert auth.poll() == "closed"
    db.close()


def test_network_readiness_comes_only_from_sanitized_connection_update(tmp_path):
    from threadrelay.delivery.telegram import NetworkPermit
    db, binding, client, handoff, auth = build(tmp_path)
    auth.begin("vault:fixture", pending=False)
    binding.events.append(state("WaitTdlibParameters")); auth.poll()
    for request in binding.requests:
        if request["@type"] in {"setNetworkType", "setTdlibParameters", "setOption"}:
            binding.events.append({"@client_id":42,"@type":"ok","@extra":request["@extra"]})
    while binding.events: auth.poll()
    auth.enable_clean_auth(NetworkPermit("clean_auth",0,False))
    assert auth.snapshot().network_state == "connecting"
    binding.events.append({"@client_id":42,"@type":"updateConnectionState","state":{"@type":"connectionStateReady"}})
    auth.poll()
    assert auth.snapshot().network_state == "online"
    db.close()


def test_native_client_creation_rejects_failure_to_disable_native_logging():
    from threadrelay.delivery.telegram import TelegramBoundaryError
    class Function:
        def __init__(self, call): self.call = call
        def __call__(self, *args): return self.call(*args)
    class Library:
        def __init__(self):
            self.created = False
            self.td_execute = Function(lambda _: None)
            def create(): self.created = True; return 9
            self.td_create_client_id = Function(create)
            self.td_send = Function(lambda *_: None)
            self.td_receive = Function(lambda _: None)
    library = Library(); native = NativeJsonBinding(library)
    with pytest.raises(TelegramBoundaryError, match="logging"):
        native.create()
    assert not library.created


def test_nonpositive_get_me_id_never_activates_account(tmp_path):
    db, binding, client, handoff, auth = build(tmp_path)
    auth.begin("vault:fixture", pending=False)
    binding.events.extend([state("Ready"), {"@client_id":42,"@type":"user","@extra":"auth:get-me","id":0,"type":{"@type":"userTypeRegular"}}])
    auth.poll()
    with pytest.raises(AuthFailure, match="unavailable"): auth.poll()
    assert AccountStore(db).current() is None
    db.close()


def ready_fixture(tmp_path):
    db, binding, client, handoff, auth = build(tmp_path)
    auth.begin("vault:fixture", pending=False)
    binding.events.extend([state("Ready"), {"@client_id":42, "@type":"user", "@extra":"auth:get-me", "id":123, "type":{"@type":"userTypeRegular"}}])
    auth.poll(); auth.poll()
    return db, binding, client, auth


@pytest.mark.parametrize("terminal", ["LoggingOut", "Closing", "Closed"])
def test_native_terminal_state_immediately_removes_durable_readiness_and_ignores_late_auth(tmp_path, terminal):
    db, binding, client, auth = ready_fixture(tmp_path)
    original = auth.snapshot()
    binding.events.extend([state(terminal), state("Ready"), {"@client_id":42,"@type":"user","@extra":"auth:get-me","id":123,"type":{"@type":"userTypeRegular"}}])
    auth.poll()
    snapshot = auth.snapshot()
    durable = AccountStore(db).account_state("123")
    assert snapshot.auth_state == "closed" and durable["auth_state"] == "closed"
    assert snapshot.network_state == "offline" and durable["network_state"] == "offline"
    if terminal == "LoggingOut":
        assert snapshot.epoch == original.epoch + 1 and snapshot.revocation_pending
        assert durable["epoch"] == snapshot.epoch and durable["revocation_pending"]
    else:
        assert snapshot.epoch == original.epoch and not snapshot.revocation_pending
        assert durable["epoch"] == original.epoch and not durable["revocation_pending"]
    count = len(binding.requests)
    auth.poll(); auth.poll()
    assert len(binding.requests) == count and auth.snapshot().auth_state == "closed"
    db.close()


def test_unsolicited_logout_invalidates_approval_and_denies_old_route(tmp_path):
    from types import SimpleNamespace
    from test_routing import topic, route, destination, post, ScoresRuntime, plan, running
    from threadrelay.inference.decision import Classifier
    from threadrelay.routing.policy import Router
    from threadrelay.routing.approvals import ApprovalStore
    from threadrelay.storage.posts import PostStore
    from threadrelay.storage.routing import ConfigStore
    db, binding, client, auth = ready_fixture(tmp_path)
    config = ConfigStore(db); sender = {"kind":"user", "id":"123"}
    config.save_topic(topic()); config.save_route(route(account_id="123", account_epoch=0, sender=sender))
    config.save_destination(destination(account_id="123", sender=sender), account_epoch=0)
    p = post(); PostStore(db).upsert(p)
    classifier = Classifier(ScoresRuntime()); router = Router(db, classifier=classifier)
    approvals = ApprovalStore(router)
    env = SimpleNamespace(db=db, config=config, post=p, classifier=classifier, router=router, approvals=approvals)
    proposal = plan(env)[0]
    approval = approvals.approve(proposal, disclosure=approvals.disclosure(proposal))
    delivery_id = router.stage(proposal, approval=approval)
    from threadrelay.storage.outbox import Outbox
    delivery = Outbox(db).get(delivery_id)
    assert router.authorize(delivery, running())["allowed"]
    binding.events.append(state("LoggingOut")); auth.poll()
    with db.connection() as con:
        assert con.execute("SELECT valid FROM approvals WHERE id=?", (approval["id"],)).fetchone()[0] == 0
    assert not router.authorize(delivery, running())["allowed"]
    assert plan(env) == []
    db.close()


def test_explicit_logout_then_logging_out_does_not_bump_epoch_twice(tmp_path):
    db, binding, client, auth = ready_fixture(tmp_path)
    auth.logout(pending=True)
    epoch = auth.snapshot().epoch
    binding.events.extend([state("LoggingOut"), state("Closing"), state("Closed")])
    auth.poll(); auth.poll(); auth.poll()
    assert auth.snapshot().epoch == epoch and auth.snapshot().revocation_pending
    assert AccountStore(db).account_state("123")["epoch"] == epoch
    db.close()


@pytest.mark.parametrize("fail_close", [False, True])
def test_cancel_latches_client_before_close_and_never_resolves_late_parameters(tmp_path, fail_close):
    class CountingVault(FixtureVault):
        def __init__(self): self.calls = 0
        def resolve(self, ref): self.calls += 1; return super().resolve(ref)
    class ClosingBinding(FixtureBinding):
        def send(self, client_id, request):
            if fail_close and request["@type"] == "close": raise RuntimeError("synthetic failure")
            super().send(client_id, request)
    db = Database(tmp_path / "app.db"); binding = ClosingBinding(); vault = CountingVault()
    client = TelegramClient(lambda: binding, vault, tmp_path / "sessions")
    auth = AuthController(client, AccountStore(db), MemorySecretHandoff(), clock=lambda: 0.0)
    auth.begin("vault:fixture", pending=False)
    if fail_close:
        with pytest.raises(AuthFailure, match="Close unavailable"): auth.cancel()
    else:
        auth.cancel()
    count = len(binding.requests)
    binding.events.extend([state("WaitTdlibParameters"), state("Ready"), {"@client_id":42,"@type":"user","@extra":"auth:get-me","id":123,"type":{"@type":"userTypeRegular"}}])
    auth.poll(); auth.poll(); auth.poll()
    assert vault.calls == 0 and len(binding.requests) == count
    assert auth.snapshot().auth_state != "ready" and AccountStore(db).current() is None
    from threadrelay.delivery.telegram import TelegramBoundaryError
    other = TelegramClient(FixtureBinding, FixtureVault(), tmp_path / "sessions")
    with pytest.raises(TelegramBoundaryError): other.initialize_offline("vault:other")
    binding.events.append(state("Closed")); assert auth.poll() == "closed"
    other.initialize_offline("vault:other")
    db.close()


@pytest.mark.parametrize("offline_response", ["missing", "error_before_parameters", "error_after_parameters"])
def test_offline_ack_required_even_after_all_parameter_option_acks(tmp_path, offline_response):
    from threadrelay.delivery.telegram import NetworkPermit, TelegramBoundaryError
    db, binding, client, handoff, auth = build(tmp_path)
    auth.begin("vault:fixture", pending=False)
    if offline_response == "error_before_parameters":
        binding.events.append({"@client_id":42,"@type":"error","@extra":"init:offline","message":uuid4().hex}); auth.poll()
    binding.events.append(state("WaitTdlibParameters")); auth.poll()
    if offline_response == "error_before_parameters":
        assert [r["@type"] for r in binding.requests] == ["setNetworkType"]
        with pytest.raises(TelegramBoundaryError, match="Network authority"):
            client.enable_network(NetworkPermit("clean_auth", 0, False))
        db.close()
        return
    assert [r["@type"] for r in binding.requests][:2] == ["setNetworkType", "setTdlibParameters"]
    if offline_response == "error_after_parameters":
        binding.events.append({"@client_id":42,"@type":"error","@extra":"init:offline","message":uuid4().hex})
    for request in binding.requests:
        if request["@type"] in {"setTdlibParameters", "setOption"}:
            binding.events.append({"@client_id":42,"@type":"ok","@extra":request["@extra"]})
    while binding.events: auth.poll()
    # Test client directly: controller status changes must not be the only gate.
    with pytest.raises(TelegramBoundaryError, match="Network authority"):
        client.enable_network(NetworkPermit("clean_auth", 0, False))
    assert not any(r.get("type", {}).get("@type") == "networkTypeOther" for r in binding.requests)
    db.close()


def test_cancel_ready_suspends_but_preserves_epoch_vault_and_approvals(tmp_path):
    from test_storage import fixture, post
    from threadrelay.storage.posts import PostStore
    from threadrelay.storage.outbox import Outbox
    db, binding, client, auth = ready_fixture(tmp_path)
    PostStore(db).upsert(post())
    approval = {**fixture("Approval"), "account_id":"123", "account_epoch":0,
                "sender":{"kind":"user", "id":"123"}}
    Outbox(db).save_approval(approval)
    auth.cancel()
    durable = AccountStore(db).account_state("123")
    assert durable["auth_state"] == "closed" and durable["epoch"] == 0 and not durable["revocation_pending"]
    with db.connection() as con:
        account = con.execute("SELECT vault_ref,active FROM accounts WHERE account_id='123'").fetchone()
        assert account["vault_ref"] == "vault:fixture" and account["active"] == 1
        assert con.execute("SELECT valid FROM approvals WHERE id=?", (approval["id"],)).fetchone()[0] == 1
    with pytest.raises(ValueError, match="account"):
        Outbox(db).save_approval({**approval, "id":"later"})
    binding.events.append(state("Closed")); auth.poll()
    new_binding = FixtureBinding()
    new_auth = AuthController(TelegramClient(lambda: new_binding, FixtureVault(), tmp_path / "sessions"), AccountStore(db), MemorySecretHandoff(), clock=lambda: 0.0)
    new_auth.begin("vault:fixture", pending=False)
    new_binding.events.extend([state("Ready"), {"@client_id":42,"@type":"user","@extra":"auth:get-me","id":123,"type":{"@type":"userTypeRegular"}}])
    new_auth.poll(); new_auth.poll()
    assert new_auth.snapshot().auth_state == "ready" and new_auth.snapshot().epoch == 0
    db.close()


def test_offline_failure_is_not_cleared_by_late_ok(tmp_path):
    from threadrelay.delivery.telegram import NetworkPermit, TelegramBoundaryError
    db, binding, client, handoff, auth = build(tmp_path)
    auth.begin("vault:fixture", pending=False)
    binding.events.append(state("WaitTdlibParameters")); auth.poll()
    binding.events.extend([{"@client_id":42,"@type":"error","@extra":"init:offline"}, {"@client_id":42,"@type":"ok","@extra":"init:offline"}])
    for request in binding.requests:
        if request["@type"] in {"setTdlibParameters", "setOption"}:
            binding.events.append({"@client_id":42,"@type":"ok","@extra":request["@extra"]})
    while binding.events: auth.poll()
    with pytest.raises(TelegramBoundaryError): client.enable_network(NetworkPermit("clean_auth", 0, False))
    db.close()


@pytest.mark.parametrize("terminal", ["LoggingOut", "Closing", "Closed"])
def test_stale_terminal_event_never_mutates_newer_account_epoch(tmp_path, terminal):
    db, binding, client, auth = ready_fixture(tmp_path)
    store = AccountStore(db)
    store.activate({"account_id":"123", "epoch":1, "auth_state":"ready", "display_name":None, "network_state":"offline", "revocation_pending":False})
    binding.events.append(state(terminal)); auth.poll()
    current = store.current()
    assert current["epoch"] == 1 and current["auth_state"] == "ready" and not current["revocation_pending"]
    assert auth.snapshot().epoch == 0 and auth.snapshot().auth_state == "closed"
    db.close()


def test_transient_revocation_write_failure_does_not_turn_logout_into_normal_close(tmp_path, monkeypatch):
    db, binding, client, auth = ready_fixture(tmp_path)
    store = auth._accounts
    original = store.revoke_if_current
    calls = []
    def fail_once(account, epoch):
        calls.append(1)
        if len(calls) == 1: raise RuntimeError("synthetic storage error")
        return original(account, epoch)
    monkeypatch.setattr(store, "revoke_if_current", fail_once)
    binding.events.extend([state("LoggingOut"), state("Closing"), state("Closed")])
    with pytest.raises(AuthFailure, match="authority unavailable"): auth.poll()
    assert auth.snapshot().auth_state == "closed"
    auth.poll(); auth.poll()
    durable = store.account_state("123")
    assert durable["epoch"] == 1 and durable["revocation_pending"]
    assert auth.snapshot().epoch == 1 and auth.snapshot().revocation_pending
    db.close()


def test_revocation_commit_then_error_reconciles_without_second_epoch_bump(tmp_path, monkeypatch):
    db, binding, client, auth = ready_fixture(tmp_path)
    store = auth._accounts; original = store.revoke_if_current; calls = []
    def commit_then_fail(account, epoch):
        result = original(account, epoch); calls.append(1)
        if len(calls) == 1: raise RuntimeError("synthetic uncertain commit")
        return result
    monkeypatch.setattr(store, "revoke_if_current", commit_then_fail)
    binding.events.extend([state("LoggingOut"), state("Closed")])
    with pytest.raises(AuthFailure, match="authority unavailable"): auth.poll()
    assert store.account_state("123")["epoch"] == 1
    assert auth.poll() == "closed"
    assert auth.snapshot().epoch == 1 and auth.snapshot().revocation_pending
    assert store.account_state("123")["epoch"] == 1
    db.close()


def test_qr_request_error_restores_fresh_phone_challenge_without_retry(tmp_path):
    db, binding, client, handoff, auth = build(tmp_path)
    auth.begin('vault:fixture', pending=False)
    binding.events.append(state('WaitPhoneNumber')); auth.poll()
    old = auth.challenge()
    auth.request_qr(old.challenge_id)
    extra = binding.requests[-1]['@extra']
    binding.events.append({'@client_id': 42, '@type': 'error', '@extra': extra, 'message': 'private details'})
    assert auth.poll() == 'error'
    assert auth.status == 'authentication_error'
    assert auth.challenge().kind == 'phone'
    assert auth.challenge().challenge_id != old.challenge_id
    assert len([r for r in binding.requests if r['@type'] == 'requestQrCodeAuthentication']) == 1
    with pytest.raises(AuthFailure, match='challenge'):
        auth.request_qr(old.challenge_id)
    db.close()


def test_late_qr_error_cannot_replace_newer_challenge(tmp_path):
    db, binding, client, handoff, auth = build(tmp_path)
    auth.begin('vault:fixture', pending=False)
    binding.events.append(state('WaitPhoneNumber')); auth.poll()
    auth.request_qr(auth.challenge().challenge_id)
    extra = binding.requests[-1]['@extra']
    binding.events.append(state('WaitCode')); auth.poll()
    newer = auth.challenge()
    binding.events.append({'@client_id':42,'@type':'error','@extra':extra,'message':'private'})
    assert auth.poll() is None
    assert auth.challenge() == newer
    db.close()


def test_qr_synchronous_send_failure_exposes_fresh_challenge_and_ignores_late_error(tmp_path):
    db, binding, client, handoff, auth = build(tmp_path)
    auth.begin('vault:fixture', pending=False)
    binding.events.append(state('WaitPhoneNumber')); auth.poll()
    old = auth.challenge()
    original = binding.send
    sent = []
    def fail_qr(client_id, request):
        if request['@type'] == 'requestQrCodeAuthentication':
            sent.append(request['@extra'])
            raise RuntimeError('private failure')
        return original(client_id, request)
    binding.send = fail_qr
    with pytest.raises(AuthFailure): auth.request_qr(old.challenge_id)
    fresh = auth.challenge()
    assert fresh.kind == 'phone' and fresh.challenge_id != old.challenge_id
    binding.events.append({'@client_id':42,'@type':'error','@extra':sent[0],'message':'private'})
    assert auth.poll() is None
    assert auth.challenge() == fresh
    db.close()


def test_restart_retains_pending_server_revocation_in_inert_account_snapshot(tmp_path):
    db, binding, client, handoff, auth = build(tmp_path)
    auth.begin('vault:fixture', pending=False)
    binding.events.extend([state('Ready'), {'@client_id':42,'@type':'user','@extra':'auth:get-me','id':123,'type':{'@type':'userTypeRegular'}}])
    auth.poll(); auth.poll(); auth.logout(pending=True)
    replacement_binding=FixtureBinding()
    replacement=AuthController(TelegramClient(lambda:replacement_binding,FixtureVault(),tmp_path/'next-sessions'),AccountStore(db),MemorySecretHandoff(),clock=lambda:0)
    snapshot=replacement.snapshot()
    assert snapshot.account_id=='123' and snapshot.revocation_pending and snapshot.auth_state=='closed'
    assert replacement_binding.created==0
    db.close()
