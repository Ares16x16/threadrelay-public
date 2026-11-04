"""One explicitly authorized synthetic Test message; no native/account/network."""
from copy import deepcopy
import json
from types import SimpleNamespace
from threading import RLock

import pytest

from test_connection_only import LocalLibrary
from threadrelay.delivery.connection_profile import connection_only_profile
from threadrelay.delivery.owner_test import OwnerTestController
from threadrelay.delivery.telegram import NativeJsonBinding, TelegramBoundaryError


def test_profile_pending_is_durable_before_dispatch_and_cannot_reopen(tmp_path):
    profile = connection_only_profile(tmp_path / 'profile', 'vault:fixture', b'k' * 32)
    binding = {**attempt(), 'profile_id':profile.profile_id}
    profile.begin_owner_test(binding)
    body = json.loads((profile.path / 'connection-purpose.json').read_text())['body']
    assert body['purpose'] == 'owner_test_pending' and body['ever_sent'] is True
    profile.close()
    with pytest.raises(ValueError):
        connection_only_profile(tmp_path / 'profile', 'vault:fixture', b'k' * 32)


def attempt():
    return {'attempt_id':'a'*32,'request_extra':'owner-test:'+'a'*32,'account_id':'7','account_epoch':2,
            'chat_id':'-20','sender_id':'7','destination_version':1,'content_hash':'b'*64,
            'generation':3,'client_id':1,'profile_id':'c'*64}


def test_only_success_with_owned_closed_can_resume_distinct_sent_profile(tmp_path):
    path = tmp_path / 'profile'
    profile = connection_only_profile(path, 'vault:fixture', b'k' * 32)
    binding = {**attempt(), 'profile_id':profile.profile_id}
    profile.begin_owner_test(binding)
    with pytest.raises(ValueError):
        profile.complete_owner_test_sent(binding, '-1', '8', closed_client_id=2)
    profile.complete_owner_test_sent(binding, '-1', '8', closed_client_id=1)
    profile.close()
    resumed = connection_only_profile(path, 'vault:fixture', b'k' * 32)
    assert resumed.purpose == 'owner_test_sent'
    assert resumed.owner_test_record()['final_message_id'] == '8'
    with pytest.raises(ValueError):resumed.begin_owner_test(binding)
    resumed.close()


class Verifier:
    def __init__(self):self.pending=None;self.evidence=None
    def begin_group(self, chat_id, *, sender):
        if self.pending is not None:raise ValueError('busy')
        self.pending={'stage':'chat','extra':'setup:chat:'+'d'*32,'chat_id':chat_id,'sender':sender}
    def finish(self):
        snapshot={'account_id':'7','chat_id':'-20','title':'Test','audience':'private',
                  'permissions':{'can_send':True,'can_select_sender':False},'sender':{'kind':'user','id':'7'},'paid_required':False}
        self.evidence=(2,1,deepcopy(snapshot));self.pending=None
        return snapshot,1


def fixture(tmp_path):
    library=LocalLibrary();native=NativeJsonBinding(library,purpose='connection_only',generation=3)
    profile=connection_only_profile(tmp_path/'profile','vault:fixture',b'k'*32)
    native.bind_connection_profile(profile);native.create();native._connection_account_id=7
    account=SimpleNamespace(account_id='7',epoch=2,auth_state='ready',network_state='online',revocation_pending=False)
    verifier=Verifier();lock=RLock()
    client=SimpleNamespace(_binding=native,_profile_guard=profile,_client_id=1,_destination_verifier=verifier,
                           _owner_test=None,transport_ready=True,_closing=False,_closed=False,
                           local_attempts_quiescent=lambda:True)
    closed=[]
    client.close=lambda:closed.append(True)
    auth=SimpleNamespace(_client=client,_lifecycle_lock=lock,status='ready',snapshot=lambda:account)
    from threadrelay.storage.database import Database
    from threadrelay.storage.outbox import AccountStore
    db=Database(tmp_path/'app.db')
    auth._accounts=AccountStore(db)
    auth._accounts.activate({'account_id':'7','epoch':2,'auth_state':'ready','network_state':'online','display_name':None,'revocation_pending':False})
    session=SimpleNamespace(authority_lock=lock)
    clock=[0.0];gate=[True]
    controller=OwnerTestController(client,auth,session,native_gate=lambda:gate[0],monotonic=lambda:clock[0])
    return SimpleNamespace(controller=controller,client=client,native=native,profile=profile,library=library,
                           verifier=verifier,account=account,clock=clock,gate=gate,closed=closed)


def test_product_test_send_requires_an_explicit_saved_exact_chat(tmp_path):
    from threadrelay.storage.saved_destinations import SavedDestinations
    env=fixture(tmp_path);store=SavedDestinations(env.controller.auth._accounts.db)
    store.require_saved_selection()
    with pytest.raises(TelegramBoundaryError):env.controller.begin(chat_id='-20',account_id='7',account_epoch=2)
    assert not env.library.calls and env.verifier.pending is None
    snapshot={'account_id':'7','chat_id':'-20','title':'Test','audience':'private',
              'permissions':{'can_send':True,'can_select_sender':False},'sender':{'kind':'user','id':'7'},'paid_required':False}
    store.save_verified(snapshot,2,'group')
    env.controller.begin(chat_id='-20',account_id='7',account_epoch=2)
    assert env.controller.query()['state']=='verifying' and not env.library.calls


def verified(env, *, change_chat=None, change_group=None, change_member=None, begun=False, omitted_nullable=False):
    if not begun:env.controller.begin(chat_id='-20',account_id='7',account_epoch=2)
    pending=env.verifier.pending
    values=[
        {'@type':'chat','id':-20,'title':'Test','type':{'@type':'chatTypeSupergroup','supergroup_id':20,'is_channel':False},
         'message_sender_id':None,'permissions':{'can_send_basic_messages':True}},
        {'@type':'supergroup','id':20,'is_channel':False,'paid_message_star_count':0,'restriction_info':None,'usernames':None},
        {'@type':'chatMember','member_id':{'@type':'messageSenderUser','user_id':7},
         'status':{'@type':'chatMemberStatusAdministrator','rights':{'is_anonymous':False}}},
    ]
    for stage,value,change in zip(('chat','group','member'),values,(change_chat,change_group,change_member)):
        if change:value.update(change)
        if omitted_nullable:
            for field in ('message_sender_id','restriction_info','usernames'):
                if value.get(field) is None:value.pop(field,None)
        pending['stage']=stage;pending['extra']='setup:'+stage+':'+'d'*32
        env.controller.consume({'@client_id':1,'@extra':pending['extra'],**value})
    pending['stage']='complete';env.controller.tick()


@pytest.mark.parametrize('basic',[False,True])
def test_exact_personal_test_group_accepts_tdlib_omitted_nullable_objects(tmp_path,basic):
    env=fixture(tmp_path)
    changes=({'change_chat':{'type':{'@type':'chatTypeBasicGroup','basic_group_id':20}},
              'change_group':{'@type':'basicGroup','is_active':True}} if basic else {})
    verified(env,omitted_nullable=True,**changes)
    assert env.controller.query()['state']=='sending'
    assert len(env.library.calls)==1 and env.library.calls[0]['@type']=='sendMessage'
    assert env.library.calls[0]['chat_id']==-20


def response(env, kind, *, client=1, chat=-20, sender=7):
    extra=env.controller.query()['request_extra']
    if kind=='message':return {'@client_id':client,'@type':'message','@extra':extra,'id':-1,'chat_id':chat,'sender_id':{'@type':'messageSenderUser','user_id':sender}}
    return {'@client_id':client,'@type':kind,'old_message_id':-1,
            'message':{'id':8,'chat_id':chat,'sender_id':{'@type':'messageSenderUser','user_id':sender}}}


def test_native_send_is_exact_one_plain_message_and_query_never_dispatches(tmp_path):
    env=fixture(tmp_path)
    assert env.controller.query()['state']=='idle' and not env.library.calls
    original=env.library.td_send.call
    def dispatch(*args):
        assert json.loads((env.profile.path/'connection-purpose.json').read_text())['body']['purpose']=='owner_test_pending'
        return original(*args)
    env.library.td_send.call=dispatch
    permits=[];send=env.native.send_owner_test
    def capture(permit):permits.append(permit);send(permit)
    env.native.send_owner_test=capture
    verified(env)
    request=env.library.calls[-1]
    assert request['@type']=='sendMessage' and request['chat_id']==-20
    assert request['input_message_content']['text']['text'].startswith('ThreadRelay test:')
    assert request['input_message_content']['text']['entities']==[]
    assert request['options']['paid_message_star_count']==0 and request['options']['allow_paid_broadcast'] is False
    assert json.loads((env.profile.path/'connection-purpose.json').read_text())['body']['purpose']=='owner_test_pending'
    for _ in range(3):env.controller.query();env.controller.tick()
    assert len(env.library.calls)==1
    with pytest.raises(TelegramBoundaryError):env.native.send(1,request)
    with pytest.raises(TelegramBoundaryError):send(permits[0])
    assert len(env.library.calls)==1
    with pytest.raises(TelegramBoundaryError):env.controller.begin(chat_id='-20',account_id='7',account_epoch=2)


def test_confirmed_owner_test_keeps_active_client_and_allows_later_history_marker(tmp_path):
    from test_owner_history_profile import history_binding
    env=fixture(tmp_path);verified(env)
    env.controller.consume(response(env,'message'))
    env.controller.consume(response(env,'updateMessageSendSucceeded'))
    assert env.controller.query()['state']=='sent' and env.closed==[]
    assert env.profile.owner_test_record()['closed'] is False
    env.profile.close()
    resumed=connection_only_profile(env.profile.path,'vault:fixture',b'k'*32)
    assert resumed.purpose=='owner_test_sent'
    binding=history_binding(resumed)
    resumed.begin_manual_history(binding)
    resumed.complete_manual_history_sent(binding,'-2','9',active_client_id=binding['client_id'])
    resumed.finalize_manual_history_sent(binding)
    resumed.close()
    reopened=connection_only_profile(env.profile.path,'vault:fixture',b'k'*32)
    assert reopened.purpose=='manual_history_sent' and reopened.owner_test_record()['closed'] is False
    reopened.close()


@pytest.mark.parametrize('changes',[
    {'change_chat':{'title':'test'}},
    {'change_chat':{'type':{'@type':'chatTypePrivate','user_id':20}}},
    {'change_chat':{'type':{'@type':'chatTypeSupergroup','supergroup_id':20,'is_channel':True}}},
    {'change_chat':{'message_sender_id':{'@type':'messageSenderUser','user_id':8}}},
    {'change_group':{'restriction_info':{'@type':'restrictionInfo','restriction_reasons':[]}}},
    {'change_group':{'paid_message_star_count':1}},
    {'change_member':{'status':{'@type':'chatMemberStatusAdministrator','rights':{'is_anonymous':True}}}},
])
def test_unsupported_target_never_consumes_profile_or_sends(tmp_path,changes):
    env=fixture(tmp_path);verified(env,**changes)
    assert env.controller.query()['state']=='failed' and not env.library.calls
    assert env.profile.purpose=='connection_only'


def test_initiated_is_not_sent_and_closed_without_correlated_success_is_unknown(tmp_path):
    env=fixture(tmp_path);verified(env)
    env.controller.consume(response(env,'message'))
    assert env.controller.query()['state']=='sending'
    env.controller.consume({'@client_id':1,'@type':'updateAuthorizationState','authorization_state':{'@type':'authorizationStateClosed'}})
    assert env.controller.query()['state']=='unknown'


def test_success_requires_full_correlation_and_persists_before_active_terminal_mark(tmp_path):
    env=fixture(tmp_path);verified(env)
    for foreign in [response(env,'updateMessageSendSucceeded',client=2),response(env,'updateMessageSendSucceeded',chat=-21),response(env,'updateMessageSendSucceeded',sender=8)]:
        env.controller.consume(foreign)
    assert env.controller.query()['state']=='sending'
    # A final update can precede the response containing the temporary ID.
    env.controller.consume(response(env,'updateMessageSendSucceeded'))
    env.controller.consume(response(env,'message'))
    assert env.controller.query()['state']=='sent' and env.closed==[]
    assert env.profile.purpose=='owner_test_sent'
    assert env.profile.owner_test_record()['closed'] is False
    env.controller.consume({'@client_id':2,'@type':'updateAuthorizationState','authorization_state':{'@type':'authorizationStateClosed'}})
    assert env.controller.query()['state']=='sent'
    resumed=connection_only_profile(env.profile.path,'vault:fixture',b'k'*32)
    assert resumed.purpose=='owner_test_sent';resumed.close()


def test_basic_group_personal_test_is_supported_without_supergroup_assumptions(tmp_path):
    env=fixture(tmp_path)
    verified(env,change_chat={'type':{'@type':'chatTypeBasicGroup','basic_group_id':20}},
             change_group={'@type':'basicGroup','is_active':True})
    assert env.controller.query()['state']=='sending' and len(env.library.calls)==1


def test_directory_sync_failure_cannot_dispatch_or_restore_never_sent_purpose(tmp_path,monkeypatch):
    import stat
    import threadrelay.delivery.connection_profile as module
    env=fixture(tmp_path);original=module.os.fsync
    def sync(descriptor):
        if stat.S_ISDIR(module.os.fstat(descriptor).st_mode):raise OSError('synthetic directory sync failure')
        return original(descriptor)
    monkeypatch.setattr(module.os,'fsync',sync)
    verified(env)
    assert not env.library.calls and env.controller.query()['state']=='failed'
    assert env.closed==[]
    with pytest.raises(TelegramBoundaryError):env.controller.begin(chat_id='-20',account_id='7',account_epoch=2)
    assert json.loads((env.profile.path/'connection-purpose.json').read_text())['body']['purpose']=='owner_test_pending'
    with pytest.raises(ValueError):connection_only_profile(env.profile.path,'vault:fixture',b'k'*32)


def test_native_dispatch_error_is_unknown_and_never_rearmed(tmp_path):
    env=fixture(tmp_path)
    def broken(*_):raise RuntimeError('synthetic ABI failure')
    env.library.td_send.call=broken
    verified(env)
    assert env.controller.query()['state']=='unknown' and env.profile.purpose=='owner_test_pending'
    with pytest.raises(TelegramBoundaryError):env.controller.begin(chat_id='-20',account_id='7',account_epoch=2)


@pytest.mark.parametrize('reason',['timeout','revoked','epoch','clock'])
def test_uncertainty_is_terminal_without_resend_or_reopen(tmp_path,reason):
    env=fixture(tmp_path);verified(env)
    if reason=='timeout':env.clock[0]=31
    elif reason=='revoked':env.gate[0]=False
    elif reason=='epoch':env.account.epoch=3
    else:env.clock[0]=-1
    env.controller.tick()
    assert env.controller.query()['state']=='unknown'
    env.controller.consume(response(env,'message'));env.controller.consume(response(env,'updateMessageSendSucceeded'))
    assert env.controller.query()['state']=='unknown' and len(env.library.calls)==1
    env.profile.close()
    with pytest.raises(ValueError):connection_only_profile(env.profile.path,'vault:fixture',b'k'*32)


def test_busy_native_capability_defers_unconsumed_intent_until_ready(tmp_path):
    env=fixture(tmp_path)
    env.controller.begin(chat_id='-20',account_id='7',account_epoch=2)
    env.gate[0]=False
    env.controller.tick()
    assert env.controller.query()['state']=='verifying' and not env.library.calls
    env.gate[0]=True;verified(env,begun=True)
    assert env.controller.query()['state']=='sending' and len(env.library.calls)==1


def test_busy_native_capability_does_not_extend_the_original_deadline(tmp_path):
    env=fixture(tmp_path);env.controller.begin(chat_id='-20',account_id='7',account_epoch=2)
    env.gate[0]=False
    env.clock[0]=31;env.controller.tick()
    assert env.controller.query()['state']=='failed' and env.profile.purpose=='connection_only'


def test_observer_failure_cannot_allow_unknown_resend_or_disconnect_login(tmp_path):
    env=fixture(tmp_path)
    def broken(_):raise RuntimeError('monitor unavailable')
    env.controller._on_state=broken
    verified(env);env.controller.revoke()
    assert env.controller.query()['state']=='unknown' and env.closed==[]
    with pytest.raises(TelegramBoundaryError):env.controller.begin(chat_id='-20',account_id='7',account_epoch=2)
    env.controller.consume({'@client_id':1,'@type':'updateAuthorizationState','authorization_state':{'@type':'authorizationStateClosed'}})
    assert env.profile._signer is None and env.controller.query()['state']=='unknown'


@pytest.mark.parametrize('transition_failure',[None,'directory_sync','replacement'])
def test_real_client_and_verifier_keep_authenticated_client_after_test_completion_or_marker_failure(tmp_path,monkeypatch,transition_failure):
    from test_telegram_auth import MemorySecretHandoff
    from threadrelay.delivery.auth import AuthController
    from threadrelay.delivery.destinations import DestinationVerifier
    from threadrelay.delivery.telegram import NetworkPermit, TelegramClient
    from threadrelay.storage.database import Database
    from threadrelay.storage.outbox import AccountStore
    from threadrelay.storage.routing import ConfigStore

    library=LocalLibrary();binding=NativeJsonBinding(library,purpose='connection_only',generation=3)
    class Vault:
        def resolve(self,_):return {'api_id':1234,'api_hash':'a'*32,'database_key':b'k'*32}
    client=TelegramClient(lambda:binding,Vault(),tmp_path/'telegram')
    session=SimpleNamespace(authority_lock=RLock())
    with Database(tmp_path/'real.db') as db:
        auth=AuthController(client,AccountStore(db),MemorySecretHandoff(),lifecycle_lock=session.authority_lock,clock=lambda:0.)
        def emit(value):
            library.events.append({'@client_id':1,**value});auth.poll()
        def state(name):return {'@type':'updateAuthorizationState','authorization_state':{'@type':'authorizationState'+name}}
        auth.begin('vault:fixture',pending=False);emit(state('WaitTdlibParameters'))
        for request in library.calls[:]:
            if request.get('@extra','').startswith('init:'):emit({'@type':'ok','@extra':request['@extra']})
        auth.enable_clean_auth(NetworkPermit('clean_auth',auth.snapshot().epoch,False))
        emit({'@type':'ok','@extra':library.calls[-1]['@extra']});emit(state('Ready'))
        emit({'@type':'user','@extra':'auth:get-me','id':7,'type':{'@type':'userTypeRegular'}})
        emit({'@type':'updateConnectionState','state':{'@type':'connectionStateReady'}})
        assert auth.status=='ready' and client.transport_ready
        DestinationVerifier(client,auth,ConfigStore(db),session)
        controller=OwnerTestController(client,auth,session,native_gate=lambda:True,monotonic=lambda:0.)
        listing={'@type':'getChats','chat_list':{'@type':'chatListMain'},'limit':100,'@extra':'listing:chats:'+'f'*32}
        client._send(listing);emit({'@type':'chats','@extra':listing['@extra'],'chat_ids':[-20],'total_count':1})
        controller.begin(chat_id='-20',account_id='7',account_epoch=auth.snapshot().epoch)
        emit({'@type':'chat','@extra':library.calls[-1]['@extra'],'id':-20,'title':'Test',
              'type':{'@type':'chatTypeSupergroup','supergroup_id':20,'is_channel':False},'message_sender_id':None,
              'permissions':{'can_send_basic_messages':True}})
        emit({'@type':'supergroup','@extra':library.calls[-1]['@extra'],'id':20,'is_channel':False,
              'paid_message_star_count':0,'restriction_info':None,'usernames':None})
        emit({'@type':'chatMember','@extra':library.calls[-1]['@extra'],'member_id':{'@type':'messageSenderUser','user_id':7},
              'status':{'@type':'chatMemberStatusMember'}})
        if transition_failure:
            import stat
            import threadrelay.delivery.connection_profile as module
            if transition_failure=='directory_sync':
                original=module.os.fsync
                def sync(descriptor):
                    if stat.S_ISDIR(module.os.fstat(descriptor).st_mode):raise OSError('synthetic directory sync failure')
                    return original(descriptor)
                monkeypatch.setattr(module.os,'fsync',sync)
            else:
                def replace(*args,**kwargs):raise OSError('synthetic marker replacement failure')
                monkeypatch.setattr(module.os,'replace',replace)
            controller.tick()
            assert controller.query()['state']=='failed'
            assert not client._closing and client.transport_ready and auth.status=='ready'
            assert not any(request['@type']=='close' for request in library.calls)
            assert not any(request['@type']=='sendMessage' for request in library.calls)
            with pytest.raises(TelegramBoundaryError):controller.begin(chat_id='-20',account_id='7',account_epoch=auth.snapshot().epoch)
            with pytest.raises((ValueError,TelegramBoundaryError)):binding.send(1,{'@type':'getAuthorizationState'})
            emit({'@type':'updateConnectionState','state':{'@type':'connectionStateReady'}})
            assert auth.status=='ready' and not client._closed
            client.close();emit(state('Closed'))
            return
        controller.tick();assert controller.query()['state']=='sending'
        extra=controller.query()['request_extra']
        emit({'@type':'message','@extra':extra,'id':-1,'chat_id':-20,'sender_id':{'@type':'messageSenderUser','user_id':7}})
        assert controller.query()['state']=='sending'
        emit({'@type':'updateMessageSendSucceeded','old_message_id':-1,
              'message':{'id':8,'chat_id':-20,'sender_id':{'@type':'messageSenderUser','user_id':7}}})
        assert controller.query()['state']=='sent' and not client._closing and client.transport_ready
        assert auth.status=='ready' and client._client_id==1
        assert not any(request['@type']=='close' for request in library.calls)
        client.close();emit(state('Closing'));emit(state('Closed'))
        assert client._closed
        # The normal client initializer accepts this distinct terminal purpose;
        # no profile copy, logout, deletion or new authentication was performed.
        replacement=NativeJsonBinding(LocalLibrary(),purpose='connection_only',generation=4)
        resumed=TelegramClient(lambda:replacement,Vault(),tmp_path/'telegram')
        resumed.initialize_offline('vault:fixture')
        assert resumed._profile_guard.purpose=='owner_test_sent'
        with pytest.raises(TelegramBoundaryError):replacement.send(1,{'@type':'sendMessage'})
        resumed.close()


def test_corrupt_profile_allows_only_hardcoded_close_for_exact_owned_native_client(tmp_path):
    env=fixture(tmp_path)
    env.profile.close()
    for identifier in (None,True,0,2):
        with pytest.raises(TelegramBoundaryError):env.native.close_owned_connection(identifier)
    with pytest.raises((ValueError,TelegramBoundaryError)):env.native.send(1,{'@type':'close','@extra':'auth:close'})
    with pytest.raises((ValueError,TelegramBoundaryError)):env.native.send(1,{'@type':'sendMessage'})
    assert env.library.calls==[]
    env.native.close_owned_connection(1)
    assert env.library.calls==[{'@type':'close','@extra':'auth:close'}]
