"""Authenticated one-message History profile transitions; no native/network."""
import json

import pytest

from threadrelay.delivery.connection_profile import connection_only_profile


def history_binding(profile, *, suffix='a', job='job-one', delivery='d'):
    return {'attempt_id':suffix*32,'request_extra':'owner-history:'+suffix*32,
        'account_id':'7','account_epoch':2,'chat_id':'-20','sender_id':'7',
        'destination_version':1,'content_hash':'b'*64,'generation':3,'client_id':1,
        'profile_id':profile.profile_id,'job_id':job,'job_version':4,'delivery_id':delivery*32}


def test_history_pending_is_durable_and_can_never_reopen(tmp_path):
    path=tmp_path/'profile';profile=connection_only_profile(path,'vault:fixture',b'k'*32)
    binding=history_binding(profile)
    profile.begin_manual_history(binding)
    body=json.loads((path/'connection-purpose.json').read_text())['body']
    assert body['purpose']=='manual_history_pending' and body['ever_sent'] is True
    assert body['manual_history']==binding
    assert profile.manual_history_record()==binding
    assert profile.owner_test_record() is None
    profile.close()
    with pytest.raises(ValueError):connection_only_profile(path,'vault:fixture',b'k'*32)


def test_history_only_exact_completion_can_enable_a_separately_reopened_send(tmp_path):
    path=tmp_path/'profile';profile=connection_only_profile(path,'vault:fixture',b'k'*32)
    binding=history_binding(profile);profile.begin_manual_history(binding)
    for kwargs in ({'closed_client_id':2},{'closed_client_id':True}):
        with pytest.raises(ValueError):profile.complete_manual_history_sent(binding,'-1','8',**kwargs)
    with pytest.raises(ValueError):profile.complete_manual_history_sent({**binding,'job_id':'other'},'-1','8',closed_client_id=1)
    with pytest.raises(ValueError):profile.complete_manual_history_sent(binding,'-1','0',closed_client_id=1)
    profile.complete_manual_history_sent(binding,'-1','8',closed_client_id=1)
    assert profile.purpose=='manual_history_sent'
    with pytest.raises(ValueError):profile.begin_manual_history(history_binding(profile,suffix='c',job='job-two',delivery='e'))
    profile.close()
    resumed=connection_only_profile(path,'vault:fixture',b'k'*32)
    record=resumed.manual_history_record()
    assert record=={**binding,'temporary_message_id':'-1','final_message_id':'8','closed':True}
    with pytest.raises(ValueError):resumed.begin_owner_test({})
    second=history_binding(resumed,suffix='c',job='job-two',delivery='e')
    resumed.begin_manual_history(second)
    assert resumed.purpose=='manual_history_pending'
    assert resumed.manual_history_record()==second
    resumed.close()


@pytest.mark.parametrize('active',[False,True])
def test_history_preserves_previous_test_receipt_and_ever_sent_purpose(tmp_path,active):
    from test_owner_test import attempt
    path=tmp_path/'profile';profile=connection_only_profile(path,'vault:fixture',b'k'*32)
    test={**attempt(),'profile_id':profile.profile_id}
    profile.begin_owner_test(test);profile.complete_owner_test_sent(test,'-1','8',closed_client_id=1)
    test_record=profile.owner_test_record();profile.close()
    resumed=connection_only_profile(path,'vault:fixture',b'k'*32)
    binding=history_binding(resumed);resumed.begin_manual_history(binding)
    assert resumed.owner_test_record()==test_record
    resumed.complete_manual_history_sent(binding,'-2','9',**({'active_client_id':1} if active else {'closed_client_id':1}))
    if active:resumed.finalize_manual_history_sent(binding)
    resumed.close()
    final=connection_only_profile(path,'vault:fixture',b'k'*32)
    assert final.owner_test_record()==test_record
    assert final.manual_history_record()['final_message_id']=='9'
    body=json.loads((path/'connection-purpose.json').read_text())['body']
    assert body['schema_version']==(4 if active else 3) and body['ever_sent'] is True
    assert (path/'connection-purpose.json').stat().st_size<=4096
    final.close()


@pytest.mark.parametrize('change',[
    {'request_extra':'owner-test:'+'a'*32},{'sender_id':'8'},{'chat_id':'0'},
    {'account_id':'-7'},{'account_epoch':True},{'generation':True},
    {'job_id':''},{'job_id':'x'*129},{'job_version':True},{'job_version':0},
    {'delivery_id':'private-detail'},{'content_hash':'z'*64},{'unreviewed':True},
])
def test_history_profile_rejects_unbound_or_malformed_native_authority(tmp_path,change):
    profile=connection_only_profile(tmp_path/'profile','vault:fixture',b'k'*32)
    with pytest.raises(ValueError):profile.begin_manual_history({**history_binding(profile),**change})
    assert profile.purpose=='connection_only'
    profile.close()


def test_uncertain_history_marker_publish_is_closed_and_quarantined(tmp_path,monkeypatch):
    import os
    import stat
    path=tmp_path/'profile';profile=connection_only_profile(path,'vault:fixture',b'k'*32)
    fsync=os.fsync
    def fail_directory(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):raise OSError('synthetic publish uncertainty')
        fsync(descriptor)
    monkeypatch.setattr(os,'fsync',fail_directory)
    with pytest.raises(OSError):profile.begin_manual_history(history_binding(profile))
    with pytest.raises(ValueError):profile.recheck()
    with pytest.raises(ValueError):connection_only_profile(path,'vault:fixture',b'k'*32)


def test_owned_closed_keeps_pending_history_marker_open_for_exact_completion(tmp_path):
    from test_connection_only import LocalLibrary
    from threadrelay.delivery.telegram import NativeJsonBinding
    profile=connection_only_profile(tmp_path/'profile','vault:fixture',b'k'*32)
    library=LocalLibrary();native=NativeJsonBinding(library,purpose='connection_only',generation=3)
    native.bind_connection_profile(profile);native.create()
    binding=history_binding(profile);profile.begin_manual_history(binding)
    library.events.append({'@client_id':1,'@type':'updateAuthorizationState',
        'authorization_state':{'@type':'authorizationStateClosed'}})
    native.receive(0)
    profile.recheck()
    profile.complete_manual_history_sent(binding,'-1','8',closed_client_id=1)
    assert profile.manual_history_record()['closed'] is True
    profile.close()


def test_sole_receive_delivers_owned_events_to_history_controller_before_auth_closure(tmp_path):
    from test_telegram_auth import build,state
    db,binding,client,_,auth=build(tmp_path)
    auth.begin('vault:fixture',pending=False)
    observed=[]
    class HistoryReceiver:
        def consume(self,event):
            observed.append((event['@client_id'],client._closed))
    client._owner_history=HistoryReceiver()
    binding.events.append({**state('Closed'),'@client_id':43})
    auth.poll()
    assert observed==[]
    binding.events.append(state('Closed'));auth.poll()
    assert observed==[(42,False)]
    assert client._closed and auth.status=='closed'
    db.close()


@pytest.mark.parametrize('completed',[False,True])
def test_manual_history_can_quarantine_an_exact_pending_or_completed_attempt(tmp_path,completed):
    path=tmp_path/'profile';profile=connection_only_profile(path,'vault:fixture',b'k'*32)
    binding=history_binding(profile);profile.begin_manual_history(binding)
    if completed:profile.complete_manual_history_sent(binding,'-1','8',closed_client_id=1)
    profile.quarantine_manual_history(binding)
    assert profile.purpose=='manual_history_unknown'
    record=profile.manual_history_record()
    assert record['attempt_id']==binding['attempt_id'] and record['job_id']=='job-one'
    if completed:assert record['final_message_id']=='8' and record['closed'] is True
    profile.close()
    with pytest.raises(ValueError):connection_only_profile(path,'vault:fixture',b'k'*32)


@pytest.mark.parametrize('completed',[False,True])
def test_manual_history_cannot_quarantine_an_unrelated_job_or_attempt(tmp_path,completed):
    profile=connection_only_profile(tmp_path/'profile','vault:fixture',b'k'*32)
    binding=history_binding(profile);profile.begin_manual_history(binding)
    if completed:profile.complete_manual_history_sent(binding,'-1','8',closed_client_id=1)
    with pytest.raises(ValueError):profile.quarantine_manual_history({**binding,'job_id':'unrelated'})
    assert profile.purpose==('manual_history_sent' if completed else 'manual_history_pending')
    profile.close()


def metadata_native(tmp_path,chat='-20'):
    from test_connection_only import LocalLibrary
    from threadrelay.delivery.owner_history import OwnerHistoryMetadataPermit,_PERMIT_KEY
    from threadrelay.delivery.telegram import NativeJsonBinding
    profile=connection_only_profile(tmp_path/'profile','vault:fixture',b'k'*32)
    library=LocalLibrary();native=NativeJsonBinding(library,purpose='connection_only',generation=3)
    native.bind_connection_profile(profile);native.create();native._connection_account_id=7
    permit=OwnerHistoryMetadataPermit(_PERMIT_KEY,profile,
        {'chat_id':chat,'client_id':1,'generation':3,'account_id':'7'},lambda:None)
    native.scope_owner_history_metadata(permit)
    return native,library,profile,permit


def test_exact_basic_group_preparation_is_ordered_bounded_and_never_creates_a_new_group(tmp_path):
    from threadrelay.delivery.telegram import TelegramBoundaryError
    native,library,profile,permit=metadata_native(tmp_path)
    lookup={'@type':'getChat','chat_id':-20,'@extra':'setup:chat:'+'a'*32}
    with pytest.raises(TelegramBoundaryError):native.send(1,lookup)
    basic={'@type':'getBasicGroup','basic_group_id':20,'@extra':'owner-history:prepare-basic:'+'b'*32}
    native.send(1,basic)
    with pytest.raises(TelegramBoundaryError):native.send(1,basic)
    library.events.append({'@client_id':1,'@type':'basicGroup','id':20,'@extra':basic['@extra']});native.receive(0)
    create={'@type':'createBasicGroupChat','basic_group_id':20,'force':False,'@extra':'owner-history:prepare-chat:'+'c'*32}
    for changed in ({**create,'force':True},{**create,'basic_group_id':21},{**create,'@type':'createNewBasicGroupChat'}):
        with pytest.raises(TelegramBoundaryError):native.send(1,changed)
    native.send(1,create)
    with pytest.raises(TelegramBoundaryError):native.send(1,create)
    library.events.append({'@client_id':1,'@type':'chat','id':-20,'@extra':create['@extra'],
        'type':{'@type':'chatTypeBasicGroup','basic_group_id':20}});native.receive(0)
    native.send(1,lookup)
    for request in (lookup,basic,create,{'@type':'sendMessage','chat_id':-20}):
        with pytest.raises(TelegramBoundaryError):native.send(1,request)
    with pytest.raises(TelegramBoundaryError):native.scope_owner_history_metadata(permit)
    assert [request['@type'] for request in library.calls]==['getBasicGroup','createBasicGroupChat','getChat']
    profile.close()


@pytest.mark.parametrize('stage',['basic','chat'])
def test_foreign_group_preparation_reply_never_grants_the_saved_target_lookup(tmp_path,stage):
    from threadrelay.delivery.telegram import TelegramBoundaryError
    native,library,profile,_=metadata_native(tmp_path)
    basic={'@type':'getBasicGroup','basic_group_id':20,'@extra':'owner-history:prepare-basic:'+'b'*32}
    native.send(1,basic)
    library.events.append({'@client_id':1,'@type':'basicGroup','id':21 if stage=='basic' else 20,'@extra':basic['@extra']});native.receive(0)
    create={'@type':'createBasicGroupChat','basic_group_id':20,'force':False,'@extra':'owner-history:prepare-chat:'+'c'*32}
    if stage=='chat':
        native.send(1,create)
        library.events.append({'@client_id':1,'@type':'chat','id':-20,'@extra':create['@extra'],
            'type':{'@type':'chatTypeSupergroup','supergroup_id':20,'is_channel':False}});native.receive(0)
    with pytest.raises(TelegramBoundaryError):native.send(1,{'@type':'getChat','chat_id':-20,'@extra':'setup:chat:'+'a'*32})
    with pytest.raises(TelegramBoundaryError):native.send(1,create)
    profile.close()


@pytest.mark.parametrize('chat,want',[
    ('-725000001',('chatTypeBasicGroup',725000001)),
    ('-999999999999',('chatTypeBasicGroup',999999999999)),
    ('-1000000000001',('chatTypeSupergroup',1)),
])
def test_group_metadata_identity_uses_the_pinned_dialog_id_ranges(chat,want):
    from threadrelay.delivery.telegram import _group_metadata_identity
    assert _group_metadata_identity(chat)==want


@pytest.mark.parametrize('chat',['0','7','-1000000000000','-2000000000000','-1997852516352','-01',True,None])
def test_group_metadata_identity_rejects_secret_invalid_and_noncanonical_ids(chat):
    from threadrelay.delivery.telegram import _group_metadata_identity,TelegramBoundaryError
    with pytest.raises(TelegramBoundaryError):_group_metadata_identity(chat)


def test_supergroup_bootstrap_admits_only_the_exact_target_after_two_bounded_list_pairs(tmp_path):
    from threadrelay.delivery.telegram import TelegramBoundaryError
    native,library,profile,_=metadata_native(tmp_path,'-1000000000020')
    target={'@type':'getChat','chat_id':-1000000000020,'@extra':'setup:chat:'+'a'*32}
    with pytest.raises(TelegramBoundaryError):native.send(1,target)
    for index,name in enumerate(('main','archive')):
        chat_list={'@type':'chatListMain' if name=='main' else 'chatListArchive'}
        load={'@type':'loadChats','chat_list':chat_list,'limit':100,'@extra':'owner-history:prepare-load:'+str(index)*32}
        ids={'@type':'getChats','chat_list':chat_list,'limit':100,'@extra':'owner-history:prepare-ids:'+str(index)*32}
        with pytest.raises(TelegramBoundaryError):native.send(1,ids)
        for changed in ({**load,'limit':101},{**load,'chat_list':{'@type':'chatListArchive' if name=='main' else 'chatListMain'}}):
            with pytest.raises(TelegramBoundaryError):native.send(1,changed)
        native.send(1,load)
        with pytest.raises(TelegramBoundaryError):native.send(1,load)
        library.events.append({'@client_id':1,'@extra':load['@extra'],'@type':'error','code':404});native.receive(0)
        native.send(1,ids)
        assert ids['@extra'] not in native._connection_list_pending
        with pytest.raises(TelegramBoundaryError):native.send(1,ids)
        values=[-40] if name=='main' else [-41,-1000000000020]
        library.events.append({'@client_id':1,'@extra':ids['@extra'],'@type':'chats','chat_ids':values,'total_count':len(values)});native.receive(0)
        assert not native._connection_listed_chats
        for other in (-40,-41):
            with pytest.raises(TelegramBoundaryError):native.send(1,{**target,'chat_id':other})
    native.send(1,target)
    with pytest.raises(TelegramBoundaryError):native.send(1,target)
    assert [request['@type'] for request in library.calls]==['loadChats','getChats','loadChats','getChats','getChat']
    profile.close()


@pytest.mark.parametrize('reply',[
    {'@type':'chats','chat_ids':[-1000000000020]*2,'total_count':2},
    {'@type':'chats','chat_ids':[True,-1000000000020],'total_count':2},
    {'@type':'chats','chat_ids':[-1000000000020],'total_count':-1},
    {'@type':'error','code':400},
])
def test_invalid_supergroup_bootstrap_reply_never_admits_metadata(tmp_path,reply):
    from threadrelay.delivery.telegram import TelegramBoundaryError
    native,library,profile,_=metadata_native(tmp_path,'-1000000000020')
    load={'@type':'loadChats','chat_list':{'@type':'chatListMain'},'limit':100,'@extra':'owner-history:prepare-load:'+'a'*32}
    ids={'@type':'getChats','chat_list':{'@type':'chatListMain'},'limit':100,'@extra':'owner-history:prepare-ids:'+'b'*32}
    native.send(1,load)
    library.events.append({'@client_id':1,'@extra':load['@extra'],'@type':'ok'});native.receive(0)
    native.send(1,ids)
    library.events.append({'@client_id':1,'@extra':ids['@extra'],**reply});native.receive(0)
    with pytest.raises(TelegramBoundaryError):native.send(1,{'@type':'getChat','chat_id':-1000000000020,'@extra':'setup:chat:'+'c'*32})
    assert not native._connection_listed_chats
    profile.close()


def test_active_history_completion_stays_guarded_until_database_finish(tmp_path):
    path=tmp_path/'profile';profile=connection_only_profile(path,'vault:fixture',b'k'*32)
    binding=history_binding(profile);profile.begin_manual_history(binding)
    second=history_binding(profile,suffix='c',job='job-two',delivery='e')
    profile.complete_manual_history_sent(binding,'-1','8',active_client_id=1)
    assert profile.manual_history_record()['closed'] is False
    with pytest.raises(ValueError):profile.begin_manual_history(second)
    with pytest.raises(ValueError):profile.finalize_manual_history_sent({**binding,'job_id':'other'})
    profile.finalize_manual_history_sent(binding)
    profile.begin_manual_history(second)
    assert profile.manual_history_record()==second
    profile.complete_manual_history_sent(second,'-2','9',active_client_id=1)
    profile.finalize_manual_history_sent(second);profile.close()
    resumed=connection_only_profile(path,'vault:fixture',b'k'*32)
    assert resumed.manual_history_record()['closed'] is False
    assert resumed.manual_history_record()['job_id']=='job-two'
    resumed.close()


@pytest.mark.parametrize('identity',[
    {'active_client_id':2},{'active_client_id':True},{'active_client_id':0},
    {'active_client_id':1,'closed_client_id':1},{},
])
def test_live_history_completion_rejects_foreign_or_ambiguous_client_identity(tmp_path,identity):
    profile=connection_only_profile(tmp_path/'profile','vault:fixture',b'k'*32)
    binding=history_binding(profile);profile.begin_manual_history(binding)
    with pytest.raises(ValueError):profile.complete_manual_history_sent(binding,'-1','8',**identity)
    assert profile.purpose=='manual_history_pending'
    profile.close()
