"""Synthetic Saved Messages setup through real auth/destination/protocol code.

No native library, vault, account, network, route or message send is used.
"""
from types import SimpleNamespace

import pytest

from test_routing import running
from test_telegram_auth import FixtureBinding, FixtureVault, MemorySecretHandoff, state
from threadrelay.contracts import ProtocolError, validate_contract
from threadrelay.delivery.auth import AuthController
from threadrelay.delivery.destinations import DestinationVerifier
from threadrelay.delivery.telegram import NetworkPermit, TelegramBoundaryError, TelegramClient
from threadrelay.features import DestinationFeatures, LocalFeatures
from threadrelay.main import dispatch
from threadrelay.storage.database import Database
from threadrelay.storage.outbox import AccountStore
from threadrelay.storage.routing import ConfigStore


def command(kind, payload=None):
    return {'version': 1, 'id': 'saved-setup', 'type': kind, 'payload': payload or {}}


@pytest.fixture
def setup(tmp_path):
    with Database(tmp_path / 'app.db') as db:
        binding = FixtureBinding()
        client = TelegramClient(lambda: binding, FixtureVault(), tmp_path / 'sessions')
        session = running()
        auth = AuthController(client, AccountStore(db), MemorySecretHandoff(),
                              clock=lambda: 0, lifecycle_lock=session.authority_lock)
        auth.begin('vault:fixture', pending=False)
        binding.events.append(state('WaitTdlibParameters'))
        auth.poll()
        for req in binding.requests[:]:
            if req.get('@extra', '').startswith('init:'):
                binding.events.append({'@client_id': 42, '@type': 'ok', '@extra': req['@extra']})
                auth.poll()
        auth.enable_clean_auth(NetworkPermit('clean_auth', 0, False))
        binding.events.append({'@client_id': 42, '@type': 'ok', '@extra': binding.requests[-1]['@extra']})
        auth.poll()
        binding.events.extend([state('Ready'), {'@client_id': 42, '@type': 'user',
            '@extra': 'auth:get-me', 'id': 123, 'type': {'@type': 'userTypeRegular'}},
            {'@client_id': 42, '@type': 'updateConnectionState', 'state': {'@type': 'connectionStateReady'}}])
        auth.poll(); auth.poll(); auth.poll()
        verifier = DestinationVerifier(client, auth, ConfigStore(db), session)
        gate = [True]
        features = LocalFeatures(db, destination=DestinationFeatures(verifier, native_gate=lambda: gate[0]))
        yield SimpleNamespace(db=db, binding=binding, client=client, auth=auth, session=session,
                              verifier=verifier, gate=gate, features=features)


def chat_response(req, **changes):
    return {'@client_id': 42, '@type': 'chat', '@extra': req['@extra'], 'id': 123,
            'title': 'Saved Messages', 'type': {'@type': 'chatTypePrivate', 'user_id': 123},
            'message_sender_id': None, **changes}


def test_saved_messages_derives_self_and_requires_correlated_zero_paid_verification(setup):
    e = setup
    before = len(e.binding.requests)
    result = dispatch(command('VerifySavedMessages'), features=e.features)
    assert result['type'] == 'Acknowledged'
    create = e.binding.requests[-1]
    assert {k: v for k, v in create.items() if k != '@extra'} == {
        '@type': 'createPrivateChat', 'user_id': 123, 'force': False}
    assert dispatch(command('QueryDestination'), features=e.features)['payload']['state'] == 'pending'
    # TDLib announces a newly loaded chat before the correlated return value.
    e.binding.events.append({'@client_id': 42, '@type': 'updateNewChat', 'chat': chat_response(create)})
    e.auth.poll()
    e.binding.events.append(chat_response(create, **{'@extra': 'unrelated'}))
    e.auth.poll()
    assert len(e.binding.requests) == before + 1
    e.binding.events.append(chat_response(create))
    e.auth.poll()
    paid = e.binding.requests[-1]
    assert {k: v for k, v in paid.items() if k != '@extra'} == {
        '@type': 'canSendMessageToUser', 'user_id': 123, 'only_local': False}
    assert dispatch(command('QueryDestination'), features=e.features)['payload']['state'] == 'pending'
    e.binding.events.append({'@client_id': 42, '@type': 'canSendMessageToUserResultOk', '@extra': paid['@extra']})
    e.auth.poll()
    status = dispatch(command('QueryDestination'), features=e.features)['payload']
    assert status['state'] == 'verified'
    assert status['snapshot'] == {'account_id': '123', 'chat_id': '123', 'title': 'Saved Messages',
        'audience': 'private', 'permissions': {'can_send': True, 'can_select_sender': False},
        'sender': {'kind': 'user', 'id': '123'}, 'paid_required': False}
    assert e.features.routes() == []
    assert [r['@type'] for r in e.binding.requests[before:]] == ['createPrivateChat', 'canSendMessageToUser']


@pytest.mark.parametrize('changes', [
    {'id': 456}, {'type': {'@type': 'chatTypePrivate', 'user_id': 456}},
    {'type': {'@type': 'chatTypeSupergroup', 'supergroup_id': 123, 'is_channel': False}},
    {'message_sender_id': {'@type': 'messageSenderUser', 'user_id': 456}},
    {'message_sender_id': {'@type': 'messageSenderChat', 'chat_id': 123}},
])
def test_saved_messages_rejects_foreign_chat_user_or_sender(setup, changes):
    e = setup
    dispatch(command('VerifySavedMessages'), features=e.features)
    create = e.binding.requests[-1]
    e.binding.events.append(chat_response(create, **changes))
    e.auth.poll()
    assert dispatch(command('QueryDestination'), features=e.features)['payload'] == {'state': 'failed', 'snapshot': None}
    assert e.binding.requests[-1] == create
    assert e.features.routes() == []


def test_saved_messages_does_not_accept_paid_or_unknown_permission(setup):
    e = setup
    dispatch(command('VerifySavedMessages'), features=e.features)
    e.binding.events.append(chat_response(e.binding.requests[-1]))
    e.auth.poll()
    e.binding.events.append({'@client_id': 42, '@type': 'canSendMessageToUserResultUserIsDeleted',
                             '@extra': e.binding.requests[-1]['@extra']})
    e.auth.poll()
    assert dispatch(command('QueryDestination'), features=e.features)['payload'] == {'state': 'failed', 'snapshot': None}


@pytest.mark.parametrize('closed', ['gate', 'offline', 'closed', 'stale_account'])
def test_saved_messages_never_initializes_without_current_authority(setup, closed):
    e = setup
    if closed == 'gate':
        e.gate[0] = False
    elif closed == 'offline':
        e.binding.events.append({'@client_id': 42, '@type': 'updateConnectionState',
                                 'state': {'@type': 'connectionStateConnecting'}})
        e.auth.poll()
    elif closed == 'closed':
        e.auth.cancel()
    else:
        AccountStore(e.db).activate({'account_id':'456', 'epoch':0, 'auth_state':'ready',
            'display_name':'Other', 'network_state':'online', 'revocation_pending':False}, vault_ref='vault:other')
    before = len(e.binding.requests)
    assert dispatch(command('VerifySavedMessages'), features=e.features)['type'] == 'Error'
    assert len(e.binding.requests) == before


def test_saved_messages_protocol_has_no_renderer_identity_input():
    validate_contract('Command', command('VerifySavedMessages'))
    for payload in [{'user_id': '456'}, {'chat_id': '456'}, {'force': True}, {'sender': {'kind': 'chat', 'id': '-7'}}]:
        with pytest.raises(ProtocolError):
            validate_contract('Command', command('VerifySavedMessages', payload))


def test_saved_messages_response_cannot_survive_account_revocation(setup):
    e = setup
    e.verifier.begin_saved_messages()
    request = e.binding.requests[-1]
    e.auth.cancel()
    e.binding.events.append(chat_response(request))
    e.auth.poll()
    with pytest.raises(TelegramBoundaryError):
        e.verifier.finish()


def test_delivery_refresh_reverifies_saved_messages_after_receipt_invalidation(setup):
    from datetime import datetime, timezone
    from test_routing import ScoresRuntime, approved, plan, post, route, topic
    from threadrelay.delivery.sender import Sender
    from threadrelay.inference.decision import Classifier
    from threadrelay.routing.approvals import ApprovalStore
    from threadrelay.routing.policy import Router
    from threadrelay.storage.outbox import Outbox

    e = setup
    e.verifier.begin_saved_messages()
    e.binding.events.append(chat_response(e.binding.requests[-1]))
    e.auth.poll()
    e.binding.events.append({'@client_id':42, '@type':'canSendMessageToUserResultOk',
                             '@extra':e.binding.requests[-1]['@extra']})
    e.auth.poll()
    e.verifier.finish()
    config = ConfigStore(e.db)
    config.save_topic(topic())
    config.save_route(route(destination_id='123', account_id='123', account_epoch=0,
                            sender={'kind':'user', 'id':'123'}))
    classifier = Classifier(ScoresRuntime())
    router = Router(e.db, classifier=classifier)
    env = SimpleNamespace(db=e.db, config=config, post=post(), classifier=classifier,
                          router=router, approvals=ApprovalStore(router))
    proposal = plan(env)[0]
    delivery_id = router.stage(proposal, approval=approved(env, proposal))
    # Live metadata receipts are not persisted across invalidation/recreation.
    e.verifier.invalidate()
    delivery = Sender(router, e.session, e.auth, e.client, destination_authority=e.verifier,
                      clock=lambda:datetime.now(timezone.utc))
    before = len(e.binding.requests)
    assert delivery.prepare_destination() is False
    refresh = e.binding.requests[-1]
    e.binding.events.append(chat_response(refresh))
    e.auth.poll()
    paid = e.binding.requests[-1]
    assert paid['@type'] == 'canSendMessageToUser'
    assert paid['user_id'] == 123 and paid['only_local'] is False
    e.binding.events.append({'@client_id':42, '@type':'canSendMessageToUserResultOk', '@extra':paid['@extra']})
    e.auth.poll()
    assert delivery.prepare_destination() is True
    assert delivery._prepared_delivery_id == delivery_id
    assert Outbox(e.db).get(delivery_id)['state'] == 'pending'
    assert [r['@type'] for r in e.binding.requests[before:]] == ['createPrivateChat', 'canSendMessageToUser']


@pytest.mark.parametrize('group', [False, True])
def test_nullable_fixed_personal_sender_requires_fresh_foreign_chat_permission(setup, group):
    e = setup
    before = len(e.binding.requests)
    if group:
        e.verifier.begin_group('-456', sender={'kind':'user', 'id':'123'})
        changes = {'id':-456, 'type':{'@type':'chatTypeSupergroup', 'supergroup_id':456, 'is_channel':False},
                   'permissions':{'can_send_basic_messages':True}}
    else:
        e.verifier.begin_private('456')
        changes = {'id':456, 'type':{'@type':'chatTypePrivate', 'user_id':456}}
    request = e.binding.requests[-1]
    e.binding.events.append(chat_response(request, **changes))
    e.auth.poll()
    # A null TDLib message_sender_id means the sender cannot be changed. It
    # establishes a fixed personal identity here, but never permission to send.
    assert e.verifier.evidence is None
    assert e.verifier.pending['stage'] == ('group' if group else 'paid')
    if group:
        metadata = e.binding.requests[-1]
        assert metadata['@type'] == 'getSupergroup'
        e.binding.events.append({'@client_id':42, '@type':'supergroup', '@extra':metadata['@extra'],
            'id':456, 'is_channel':False, 'paid_message_star_count':0, 'restriction_info':None, 'usernames':None})
        e.auth.poll()
        membership = e.binding.requests[-1]
        assert membership['@type'] == 'getChatMember'
        assert membership['member_id'] == {'@type':'messageSenderUser', 'user_id':123}
        e.binding.events.append({'@client_id':42, '@type':'chatMember', '@extra':membership['@extra'],
            'member_id':membership['member_id'], 'status':{'@type':'chatMemberStatusMember'}})
    else:
        paid = e.binding.requests[-1]
        assert paid['@type'] == 'canSendMessageToUser' and paid['user_id'] == 456 and paid['only_local'] is False
        e.binding.events.append({'@client_id':42, '@type':'canSendMessageToUserResultOk', '@extra':paid['@extra']})
    e.auth.poll()
    snapshot, _ = e.verifier.finish()
    assert snapshot['chat_id'] == ('-456' if group else '456')
    assert snapshot['sender'] == {'kind':'user', 'id':'123'}
    assert snapshot['permissions'] == {'can_send':True, 'can_select_sender':False}
    assert snapshot['paid_required'] is False
    assert not any(r['@type'] in {'setChatMessageSender', 'sendMessage', 'getChatHistory'}
                   for r in e.binding.requests[before:])


@pytest.mark.parametrize('group', [False, True])
@pytest.mark.parametrize('sender', ['malformed', {'@type':'messageSenderUser', 'user_id':999},
                                      {'@type':'messageSenderChat', 'chat_id':-999}])
def test_foreign_chat_malformed_or_mismatched_sender_proof_is_rejected(setup, group, sender):
    e = setup
    if group:
        e.verifier.begin_group('-456', sender={'kind':'user', 'id':'123'})
        changes = {'id':-456, 'type':{'@type':'chatTypeSupergroup', 'supergroup_id':456, 'is_channel':False}}
    else:
        e.verifier.begin_private('456')
        changes = {'id':456, 'type':{'@type':'chatTypePrivate', 'user_id':456}}
    request = e.binding.requests[-1]
    response = chat_response(request, **changes)
    response['message_sender_id'] = sender
    e.binding.events.append(response)
    e.auth.poll()
    assert e.verifier.pending is None
    assert e.verifier.evidence is None
    assert e.binding.requests[-1] == request


def test_nullable_channel_sender_is_never_inferred_as_personal(setup):
    e = setup
    e.verifier.begin_group('-456', sender={'kind':'user', 'id':'123'})
    request = e.binding.requests[-1]
    e.binding.events.append(chat_response(request, id=-456,
        type={'@type':'chatTypeSupergroup', 'supergroup_id':456, 'is_channel':True}))
    e.auth.poll()
    assert e.verifier.pending is None and e.verifier.evidence is None
    assert e.binding.requests[-1] == request


def test_auth_only_scope_can_verify_exact_saved_messages_but_never_arbitrary_destination_or_sender(setup):
    e=setup
    features=DestinationFeatures(e.verifier,native_gate=lambda:False,self_gate=lambda:True)
    with pytest.raises(NotImplementedError):
        features.begin_verify({'chat_id':'456','kind':'private','sender':None})
    with pytest.raises(NotImplementedError):
        features.begin_select({'chat_id':'123','sender':{'kind':'user','id':'123'}})
    features.begin_saved_messages()
    create=e.binding.requests[-1]
    assert create['user_id']==123 and create['force'] is False
    assert features.query()['state']=='pending'
    e.binding.events.append(chat_response(create));e.auth.poll()
    paid=e.binding.requests[-1]
    assert paid['@type']=='canSendMessageToUser' and paid['user_id']==123
    e.binding.events.append({'@client_id':42,'@type':'canSendMessageToUserResultOk','@extra':paid['@extra']});e.auth.poll()
    assert features.query()['state']=='verified'
    assert not any(request['@type'] in {'sendMessage','getChatHistory'} for request in e.binding.requests)


def test_metadata_only_foreign_destination_dispatch_can_finish_but_never_select_sender(setup):
    e = setup
    metadata_allowed = [True]
    destination = DestinationFeatures(e.verifier, native_gate=lambda:False, self_gate=lambda:True,
                                     metadata_gate=lambda:metadata_allowed[0])
    features = LocalFeatures(e.db, destination=destination)
    before = len(e.binding.requests)
    result = dispatch(command('VerifyDestination', {'chat_id':'456', 'kind':'private', 'sender':None}),
                      features=features)
    assert result['type'] == 'Acknowledged'
    assert dispatch(command('QueryDestination'), features=features)['payload'] == {'state':'pending', 'snapshot':None}
    request = e.binding.requests[-1]
    e.binding.events.append(chat_response(request, id=456, title='Named private chat',
                                         type={'@type':'chatTypePrivate', 'user_id':456}))
    e.auth.poll()
    paid = e.binding.requests[-1]
    assert paid['@type'] == 'canSendMessageToUser' and paid['user_id'] == 456 and paid['only_local'] is False
    assert dispatch(command('QueryDestination'), features=features)['payload']['state'] == 'pending'
    e.binding.events.append({'@client_id':42, '@type':'canSendMessageToUserResultOk', '@extra':paid['@extra']})
    e.auth.poll()
    verified = dispatch(command('QueryDestination'), features=features)['payload']
    assert verified['state'] == 'verified'
    assert verified['snapshot']['chat_id'] == '456'
    assert verified['snapshot']['sender'] == {'kind':'user', 'id':'123'}
    request_count = len(e.binding.requests)
    result = dispatch(command('SelectSender', {'chat_id':'456', 'sender':{'kind':'user', 'id':'123'}}),
                      features=features)
    assert result['type'] == 'Error'
    assert len(e.binding.requests) == request_count
    metadata_allowed[0] = False
    assert dispatch(command('QueryDestination'), features=features)['payload'] == {'state':'failed', 'snapshot':None}
    assert not any(request['@type'] in {'setChatMessageSender', 'sendMessage', 'getChatHistory'}
                   for request in e.binding.requests[before:])
