"""No-account connection scope tests; no native load, vault, or network."""

import json
import os
import pytest
from threadrelay.delivery.connection_profile import connection_only_profile
from threadrelay.delivery.telegram import NativeJsonBinding, TelegramBoundaryError


class Export:
    def __init__(self, call):
        self.call = call

    def __call__(self, *args):
        return self.call(*args)


class LocalLibrary:
    def __init__(self):
        import ctypes
        from collections import deque

        self.calls = []
        self.events = deque()
        self.result = ctypes.create_string_buffer(b'{"@type":"ok"}')
        self.td_create_client_id = Export(lambda: 1)
        self.td_send = Export(lambda client, data: self.calls.append(json.loads(data)))

        def receive(timeout):
            if not self.events:
                return None
            self.result = ctypes.create_string_buffer(
                json.dumps(self.events.popleft()).encode()
            )
            return ctypes.addressof(self.result)

        self.td_receive = Export(receive)
        self.td_execute = Export(lambda data: ctypes.addressof(self.result))


def restricted_binding(tmp_path):
    library = LocalLibrary()
    binding = NativeJsonBinding(library, purpose="connection_only")
    guard = connection_only_profile(
        tmp_path / "native-profile", "vault:fixture", b"k" * 32
    )
    binding.bind_connection_profile(guard)
    assert binding.create() == 1
    return binding, library, guard


def test_authentication_code_resend_is_correlated_and_cannot_carry_private_or_send_fields(tmp_path):
    binding,library,guard=restricted_binding(tmp_path)
    request={'@type':'resendAuthenticationCode','reason':None,'@extra':'auth:resend:current'}
    binding.send(1,request)
    for changed in [{**request,'code':'synthetic'},{**request,'reason':{'@type':'unknownReason'}},
                    {**request,'@extra':'ordinary:unbound'}]:
        with pytest.raises(TelegramBoundaryError):binding.send(1,changed)
    assert library.calls==[request]
    guard.close()


def test_destination_metadata_scope_requires_identity_and_correlated_list_membership(tmp_path):
    binding, library, guard = restricted_binding(tmp_path)
    listing={'@type':'getChats','chat_list':{'@type':'chatListMain'},'limit':100,'@extra':'listing:chats:'+'a'*32}
    with pytest.raises(TelegramBoundaryError):binding.send(1,listing)
    binding.send(1,{'@type':'getMe','@extra':'auth:get-me'})
    library.events.append({'@type':'user','@client_id':1,'@extra':'auth:get-me','id':7,'type':{'@type':'userTypeRegular'}})
    binding.receive(0)
    target={'@type':'getChat','chat_id':8,'@extra':'listing:chat:'+'b'*32}
    with pytest.raises(TelegramBoundaryError):binding.send(1,target)
    binding.send(1,listing)
    library.events.append({'@type':'chats','@client_id':1,'@extra':listing['@extra'],'chat_ids':[8,-20], 'total_count':2})
    binding.receive(0);binding.send(1,target)
    library.events.append({'@type':'chat','@client_id':1,'@extra':target['@extra'],'id':8,'title':'Private name',
        'type':{'@type':'chatTypePrivate','user_id':8},'last_message':{'private':'must not become a request scope'}})
    binding.receive(0)
    binding.send(1,{'@type':'canSendMessageToUser','user_id':8,'only_local':False,'@extra':'setup:paid:'+'c'*32})
    group={'@type':'getChat','chat_id':-20,'@extra':'listing:chat:'+'d'*32};binding.send(1,group)
    library.events.append({'@type':'chat','@client_id':1,'@extra':group['@extra'],'id':-20,'title':'Group',
        'type':{'@type':'chatTypeBasicGroup','basic_group_id':20}})
    binding.receive(0)
    binding.send(1,{'@type':'getBasicGroup','basic_group_id':20,'@extra':'setup:group:'+'e'*32})
    member={'@type':'getChatMember','chat_id':-20,'member_id':{'@type':'messageSenderUser','user_id':7},'@extra':'listing:member:'+'f'*32}
    binding.send(1,member)
    for request in [{**member,'member_id':{'@type':'messageSenderUser','user_id':8}},
        {'@type':'getChat','chat_id':9,'@extra':'setup:chat:'+'a'*32},
        {'@type':'getChatHistory','chat_id':8},
        {'@type':'setChatMessageSender','chat_id':-20,'message_sender_id':{'@type':'messageSenderUser','user_id':7}}]:
        with pytest.raises(TelegramBoundaryError):binding.send(1,request)
    library.events.append({'@client_id':1,'@type':'updateAuthorizationState','authorization_state':{'@type':'authorizationStateWaitPhoneNumber'}})
    binding.receive(0)
    with pytest.raises(TelegramBoundaryError):binding.send(1,listing)
    binding.send(1,{'@type':'logOut'})
    with pytest.raises(TelegramBoundaryError):binding.send(1,target)
    assert not any(call['@type'] in {'getChatHistory','sendMessage','setChatMessageSender'} for call in library.calls)
    guard.close()


def test_destination_list_bounds_and_stale_native_replies_never_expand_scope(tmp_path):
    binding, library, guard = restricted_binding(tmp_path)
    binding.send(1,{'@type':'getMe','@extra':'auth:get-me'})
    library.events.append({'@type':'user','@client_id':1,'@extra':'auth:get-me','id':7,'type':{'@type':'userTypeRegular'}});binding.receive(0)
    base={'@type':'getChats','chat_list':{'@type':'chatListArchive'},'limit':2,'@extra':'listing:chats:'+'1'*32}
    for changed in [{**base,'limit':10001},{**base,'limit':True},{**base,'chat_list':{'@type':'chatListFolder','chat_folder_id':1}},
        {**base,'@extra':'renderer:arbitrary'}, {**base,'extra_unreviewed':True}]:
        with pytest.raises(TelegramBoundaryError):binding.send(1,changed)
    binding.send(1,base)
    # A foreign client and an over-limit response cannot grant getChat scope.
    for event in [dict(**{'@type':'chats','@client_id':2,'@extra':base['@extra'],'chat_ids':[8],'total_count':1}),
                  dict(**{'@type':'chats','@client_id':1,'@extra':base['@extra'],'chat_ids':[8,9,10],'total_count':3})]:
        library.events.append(event);binding.receive(0)
    with pytest.raises(TelegramBoundaryError):binding.send(1,{'@type':'getChat','chat_id':8,'@extra':'listing:chat:'+'2'*32})
    guard.close()


def test_lowest_native_transport_rejects_every_message_and_unknown_method(tmp_path):
    binding, library, guard = restricted_binding(tmp_path)
    for method in [
        "sendMessage",
        "resendMessages",
        "forwardMessages",
        "sendBotStartMessage",
        "setChatDraftMessage",
        "unknownFutureMethod",
    ]:
        with pytest.raises(TelegramBoundaryError):
            binding.send(1, {"@type": method})
    assert library.calls == []
    binding.send(1, {"@type": "getMe", "@extra": "auth:get-me"})
    assert library.calls == [{"@type": "getMe", "@extra": "auth:get-me"}]
    guard.close()


def test_restricted_binding_cannot_create_without_private_profile(tmp_path):
    library = LocalLibrary()
    binding = NativeJsonBinding(library, purpose="connection_only")
    with pytest.raises(TelegramBoundaryError):
        binding.create()
    assert library.calls == []


def test_self_metadata_requires_current_correlated_identity_and_never_history(tmp_path):
    binding, library, guard = restricted_binding(tmp_path)
    binding.send(1, {"@type": "getMe", "@extra": "auth:get-me"})
    own = {
        "@type": "createPrivateChat",
        "user_id": 7,
        "force": False,
        "@extra": "setup:saved:fixture",
    }
    with pytest.raises(TelegramBoundaryError):
        binding.send(1, own)
    library.events.append(
        {
            "@type": "user",
            "@client_id": 2,
            "@extra": "auth:get-me",
            "id": 7,
            "type": {"@type": "userTypeRegular"},
        }
    )
    binding.receive(0)
    with pytest.raises(TelegramBoundaryError):
        binding.send(1, own)
    library.events.append(
        {
            "@type": "user",
            "@client_id": 1,
            "@extra": "auth:get-me",
            "id": 7,
            "type": {"@type": "userTypeRegular"},
        }
    )
    binding.receive(0)
    binding.send(1, own)
    for changed in [
        {**own, "user_id": 8},
        {**own, "force": True},
        {"@type": "getChat", "chat_id": 8},
        {"@type": "getChatHistory", "chat_id": 7},
    ]:
        with pytest.raises(TelegramBoundaryError):
            binding.send(1, changed)
    binding.send(1, {"@type": "getChat", "chat_id": 7})
    binding.send(
        1, {"@type": "canSendMessageToUser", "user_id": 7, "only_local": False}
    )
    binding.send(1, {"@type": "logOut"})
    with pytest.raises(TelegramBoundaryError):
        binding.send(1, own)
    assert not any(
        call["@type"] in {"sendMessage", "getChatHistory"} for call in library.calls
    )
    guard.close()


def test_connection_client_uses_one_shot_key_before_native_database_and_keeps_offline_ack_gate(
    tmp_path,
):
    from threadrelay.delivery.telegram import TelegramClient, NetworkPermit

    library = LocalLibrary()
    binding = NativeJsonBinding(library, purpose="connection_only")

    class Vault:
        calls = 0

        def resolve(self, reference):
            self.calls += 1
            assert self.calls == 1
            return {"api_id": 1234, "api_hash": "a" * 32, "database_key": b"k" * 32}

    vault = Vault()
    client = TelegramClient(lambda: binding, vault, tmp_path / "telegram")
    client.initialize_offline("vault:fixture")
    assert library.calls == [
        {
            "@type": "setNetworkType",
            "type": {"@type": "networkTypeNone"},
            "@extra": "init:offline",
        }
    ]
    library.events.append(
        {
            "@client_id": 1,
            "@type": "updateAuthorizationState",
            "authorization_state": {"@type": "authorizationStateWaitTdlibParameters"},
        }
    )
    assert client.authorization_event().kind == "parameters"
    assert vault.calls == 1
    with pytest.raises(TelegramBoundaryError):
        client.enable_network(NetworkPermit("clean_auth", 0, False))
    assert not any(
        call.get("type", {}).get("@type") == "networkTypeOther"
        for call in library.calls
    )
    client.close()
    library.events.append(
        {
            "@client_id": 1,
            "@type": "updateAuthorizationState",
            "authorization_state": {"@type": "authorizationStateClosed"},
        }
    )
    assert client.authorization_event().kind == "closed"


def test_delivery_client_refuses_existing_connection_only_profile(tmp_path):
    import hashlib
    from threadrelay.delivery.telegram import TelegramClient

    root = tmp_path / "telegram"
    root.mkdir(mode=0o700)
    directory = root / hashlib.sha256(b"vault:fixture").hexdigest()
    guard = connection_only_profile(directory, "vault:fixture", b"k" * 32)
    guard.close()
    library = LocalLibrary()
    binding = NativeJsonBinding(library)
    client = TelegramClient(lambda: binding, None, root)
    with pytest.raises(TelegramBoundaryError):
        client.initialize_offline("vault:fixture")
    assert binding._owned_client is None


@pytest.mark.skipif(os.name != "posix", reason="macOS connected-local scope")
def test_new_connection_profile_persists_authenticated_never_sent_scope(tmp_path):
    directory = tmp_path / "profile"
    key = b"\x19" * 32
    guard = connection_only_profile(directory, "vault:fixture", key)
    marker = directory / "connection-purpose.json"
    body = json.loads(marker.read_text())
    assert body["body"]["purpose"] == "connection_only"
    assert body["body"]["ever_sent"] is False
    assert marker.stat().st_mode & 0o777 == 0o600
    assert key.hex() not in marker.read_text()
    guard.recheck()
    guard.close()
    resumed = connection_only_profile(directory, "vault:fixture", key)
    resumed.recheck()
    resumed.close()


@pytest.mark.skipif(os.name != "posix", reason="macOS connected-local scope")
def test_unmarked_imported_profile_is_rejected_without_mutation(tmp_path):
    directory = tmp_path / "imported"
    directory.mkdir(mode=0o700)
    (directory / "td.binlog").write_bytes(b"unknown pending native data")
    with pytest.raises(ValueError):
        connection_only_profile(directory, "vault:fixture", b"k" * 32)
    assert sorted(p.name for p in directory.iterdir()) == ["td.binlog"]


@pytest.mark.skipif(os.name != "posix", reason="macOS connected-local scope")
def test_wrong_key_or_modified_profile_purpose_cannot_resume(tmp_path):
    directory = tmp_path / "profile"
    key = b"k" * 32
    guard = connection_only_profile(directory, "vault:fixture", key)
    guard.close()
    with pytest.raises(ValueError):
        connection_only_profile(directory, "vault:fixture", b"z" * 32)
    marker = directory / "connection-purpose.json"
    value = json.loads(marker.read_text())
    value["body"]["purpose"] = "delivery"
    marker.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        connection_only_profile(directory, "vault:fixture", key)


@pytest.mark.skipif(os.name != "posix", reason="macOS connected-local scope")
def test_profile_replacement_is_denied_before_later_native_request(tmp_path):
    directory = tmp_path / "profile"
    guard = connection_only_profile(directory, "vault:fixture", b"k" * 32)
    directory.rename(tmp_path / "retained")
    directory.mkdir(mode=0o700)
    with pytest.raises(ValueError):
        guard.recheck()
    guard.close()


def test_connection_owner_progresses_auth_but_never_reconnects_or_maintains_delivery():
    from contextlib import nullcontext
    from threading import Event
    from types import SimpleNamespace
    from threadrelay.delivery.owner import DeliveryOwner

    calls = []
    auth = SimpleNamespace(
        status="authorizing",
        _network_requested=False,
        _bootstrap_account=None,
        snapshot=lambda: SimpleNamespace(epoch=0),
        enable_clean_auth=lambda permit: calls.append("auth-network"),
    )
    sender = SimpleNamespace(
        auth=auth,
        expire_deadlines=lambda: None,
        poll=lambda: None,
        maintain=lambda: calls.append("delivery-maintain"),
        prepare_destination=lambda: calls.append("prepare"),
    )
    session = SimpleNamespace(
        authority_lock=nullcontext(),
        can_transmit=lambda now: True,
        _clock=SimpleNamespace(now=lambda: 0),
    )
    owner = object.__new__(DeliveryOwner)
    owner.sender, owner.telegram, owner.session = (
        sender,
        SimpleNamespace(_client=object(), network_config_ready=True),
        session,
    )
    owner._running = Event()
    owner._account_removal = None
    owner.native_gate = lambda: False
    owner.auth_gate = lambda: True
    owner.maintenance = lambda: None
    owner._enqueue = lambda operation: calls.append("reconnect")
    owner.running({"mode": "review"}, Event())
    owner.step()
    assert calls == ["auth-network"]
