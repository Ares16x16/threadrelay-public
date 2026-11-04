"""Explicit metadata discovery over synthetic TDLib events; no account or send."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from threadrelay.delivery.telegram import TelegramBoundaryError
from threadrelay.delivery.auth import AccountReadiness
from threadrelay.session import SessionController
from threadrelay.storage.database import Database
from threadrelay.storage.outbox import AccountStore
from threadrelay.storage.routing import ConfigStore


@pytest.fixture
def listing_env(tmp_path):
    from threadrelay.delivery.destination_listing import DestinationListing
    session=SessionController();db=Database(tmp_path/'listing.db');config=ConfigStore(db)
    account=AccountReadiness(account_id='7',epoch=2,auth_state='ready',display_name=None,network_state='online',revocation_pending=False)
    AccountStore(db).activate(account.__dict__)
    class Client:
        transport_ready=True
        _destination_listing=None
        _destination_verifier=None
        def __init__(self):self.requests=[]
        def _send(self,request):self.requests.append(request)
    client=Client()
    class Auth:
        status='ready'
        _client=client
        _lifecycle_lock=session.authority_lock
        def snapshot(self):return self.account
    auth=Auth();auth.account=account;clock={'now':10.0};gate={'ready':True}
    listing=DestinationListing(client,auth,config,session,native_gate=lambda:gate['ready'],clock=lambda:clock['now'])
    env=SimpleNamespace(db=db,config=config,session=session,client=client,auth=auth,listing=listing,clock=clock,gate=gate)
    yield env
    db.close()


def respond(env,typ,**fields):
    request=env.client.requests[-1]
    assert env.listing.consume({'@type':typ,'@extra':request['@extra'],**fields})


def chat(identifier,title,kind,**extra):
    return {'id':identifier,'title':title,'type':kind,'message_sender_id':None,
        'last_message':{'content':{'text':{'text':'must never leave native response'}}},
        'draft_message':{'input_message_text':'private draft'},**extra}


def test_queries_never_start_native_or_enable_network(listing_env):
    e=listing_env
    assert e.listing.query()['state']=='idle'
    assert e.client.requests==[]
    e.auth.status='authorizing';e.auth.account=replace(e.auth.account,auth_state='authorizing')
    assert e.listing.query()['blocked_reason']=='login_required'
    with pytest.raises(TelegramBoundaryError):e.listing.refresh()
    assert e.client.requests==[]


def test_native_gate_failure_is_a_static_blocker_without_leaking_error_or_names(listing_env):
    e=listing_env
    def unavailable():raise RuntimeError('private native detail')
    e.listing.native_gate=unavailable
    value=e.listing.query()
    assert value['state']=='blocked' and value['blocked_reason']=='native_unavailable'
    assert 'private native detail' not in str(value) and e.client.requests==[]


def test_explicit_refresh_projects_named_private_group_and_channel_without_message_data(listing_env):
    e=listing_env;e.listing.refresh()
    assert e.client.requests[-1]['@type']=='loadChats'
    respond(e,'ok');assert e.client.requests[-1]['@type']=='getChats'
    respond(e,'chats',chat_ids=[7,-20,-30],total_count=3)
    respond(e,'chat',**chat(7,'My own chat',{'@type':'chatTypePrivate','user_id':7}))
    respond(e,'chat',**chat(-20,'中文討論組',{'@type':'chatTypeBasicGroup','basic_group_id':20}))
    respond(e,'chat',**chat(-30,'Announcements',{'@type':'chatTypeSupergroup','supergroup_id':30,'is_channel':True},message_sender_id={'@type':'messageSenderChat','chat_id':-30}))
    page=e.listing.query(limit=2)
    assert [item['title'] for item in page['items']]==['Saved Messages','中文討論組']
    assert [item['chat_id'] for item in page['items']]==['7','-20']
    assert page['items'][1]['kind']=='group' and page['items'][1]['can_verify']
    assert page['next_cursor'] and page['can_load_more'] and not page['exhausted']
    second=e.listing.query(cursor=page['next_cursor'],limit=2)
    assert second['items'][0]['kind']=='channel' and second['items'][0]['sender']=={'kind':'chat','id':'-30'}
    assert 'last_message' not in str(page) and 'private draft' not in str(page)
    assert not any(r['@type'] in {'getChatHistory','sendMessage','setNetworkType'} for r in e.client.requests)


def test_search_and_cursor_are_snapshot_bound_and_epoch_change_clears_names(listing_env):
    e=listing_env;e.listing.refresh();respond(e,'ok');respond(e,'chats',chat_ids=[7,8],total_count=2)
    respond(e,'chat',**chat(7,'Own',{'@type':'chatTypePrivate','user_id':7}))
    respond(e,'chat',**chat(8,'朋友 Alice',{'@type':'chatTypePrivate','user_id':8}))
    first=e.listing.query(limit=1)
    assert e.listing.query(search='ALICE')['items'][0]['chat_id']=='8'
    e.auth.account=replace(e.auth.account,epoch=3)
    value=e.listing.query(cursor=first['next_cursor'],limit=1)
    assert value['items']==[] and value['state']=='blocked'
    e.auth.account=replace(e.auth.account,epoch=2)
    assert e.listing.query()['items']==[]


def test_main_and_archive_exhaustion_require_correlated_404_and_cap_is_honest(listing_env):
    e=listing_env;e.listing.refresh();respond(e,'error',code=404,message='discard raw')
    respond(e,'chats',chat_ids=[7],total_count=1);respond(e,'chat',**chat(7,'Own',{'@type':'chatTypePrivate','user_id':7}))
    assert e.listing.query()['can_load_more'] and not e.listing.query()['exhausted']
    e.listing.refresh(load_more=True)
    assert e.client.requests[-1]['chat_list']=={'@type':'chatListArchive'}
    respond(e,'error',code=404,message='discard raw');respond(e,'chats',chat_ids=[],total_count=0)
    value=e.listing.query()
    assert value['exhausted'] and not value['can_load_more'] and not value['capped']


def test_approximate_total_never_truncates_a_full_loaded_prefix(listing_env,monkeypatch):
    import threadrelay.delivery.destination_listing as module
    monkeypatch.setattr(module,'BATCH',1)
    e=listing_env;e.listing.refresh();respond(e,'error',code=404);respond(e,'chats',chat_ids=[7],total_count=0)
    respond(e,'chat',**chat(7,'Own',{'@type':'chatTypePrivate','user_id':7}))
    e.listing.refresh(load_more=True)
    assert e.client.requests[-1]['@type']=='getChats'
    assert e.client.requests[-1]['chat_list']=={'@type':'chatListMain'}
    assert e.client.requests[-1]['limit']==2


def test_late_wrong_correlations_limits_and_connection_loss_do_not_admit_metadata(listing_env):
    e=listing_env;e.listing.refresh()
    assert not e.listing.consume({'@type':'chat','@extra':'foreign','id':8,'title':'unrelated'})
    respond(e,'ok');respond(e,'chats',chat_ids=[8],total_count=1)
    pending=e.client.requests[-1]
    e.auth.account=replace(e.auth.account,network_state='offline')
    e.listing.consume({'@type':'chat','@extra':pending['@extra'],**chat(8,'Must discard',{'@type':'chatTypePrivate','user_id':8})})
    assert e.listing.query()['items']==[]
    e.auth.account=replace(e.auth.account,network_state='online')
    assert e.listing.query()['items']==[]
    e.listing.refresh();e.clock['now']+=31;e.listing.tick()
    assert e.listing.query()['state']=='failed'
    for kwargs in ({'limit':0},{'limit':51},{'cursor':'invalid'},{'search':'x'*201}):
        with pytest.raises(ValueError):e.listing.query(**kwargs)


@pytest.mark.parametrize('elapsed',[30,31])
def test_correlated_response_after_deadline_fails_before_owner_tick(listing_env,elapsed):
    e=listing_env;e.listing.refresh()
    pending=e.client.requests[-1]
    e.clock['now']+=elapsed
    # The delivery owner polls before tick(), so consume must enforce the same
    # deadline itself rather than let a late reply continue the native chain.
    assert e.listing.consume({'@type':'ok','@extra':pending['@extra']})
    assert e.client.requests==[pending]
    assert e.listing.query()['state']=='failed'
    assert e.listing.query()['items']==[]
    assert e.listing._pending is None and e.listing._queue==[]


def reach_stage(env,stage):
    env.listing.refresh()
    if stage=='load':return
    respond(env,'ok')
    if stage=='chats':return
    respond(env,'chats',chat_ids=[-100],total_count=1)
    if stage=='chat':return
    respond(env,'chat',**chat(-100,'Synthetic private title',{'@type':'chatTypeSupergroup','supergroup_id':100,'is_channel':False}))


@pytest.mark.parametrize('stage',['load','chats','chat','member'])
def test_timeout_reports_only_static_failure_stage_and_reason(listing_env,stage):
    e=listing_env;reach_stage(e,stage);request_count=len(e.client.requests)
    e.clock['now']+=31;e.listing.tick()
    page=e.listing.query()
    assert page['state']=='failed' and page['failure_stage']==stage and page['failure_reason']=='timeout'
    assert len(e.client.requests)==request_count
    assert 'Synthetic private title' not in str(page)
    e.listing.refresh()
    assert e.listing.query()['failure_stage'] is None and e.listing.query()['failure_reason'] is None


@pytest.mark.parametrize('stage',['load','chats','chat','member'])
def test_rejected_reply_diagnostics_never_echo_native_error_or_metadata(listing_env,stage):
    e=listing_env;reach_stage(e,stage)
    respond(e,'error',code=500,message='private native error and identity')
    page=e.listing.query()
    assert page['state']=='failed' and page['failure_stage']==stage and page['failure_reason']=='invalid_response'
    assert 'private native error' not in str(page) and 'Synthetic private title' not in str(page)


def test_native_request_failure_keeps_the_failed_next_stage_without_private_exception(listing_env):
    e=listing_env;e.listing.refresh()
    def unavailable(_):raise RuntimeError('private native path and account')
    e.client._send=unavailable
    respond(e,'ok')
    page=e.listing.query()
    assert page['state']=='failed' and page['failure_stage']=='chats' and page['failure_reason']=='request_unavailable'
    assert 'private native path' not in str(page)
    e.auth.status='closed'
    page=e.listing.query()
    assert page['state']=='blocked' and page['failure_stage'] is None and page['failure_reason'] is None


def test_nonfailed_queries_have_no_failure_diagnostic(listing_env):
    e=listing_env
    assert e.listing.query()['failure_stage'] is None and e.listing.query()['failure_reason'] is None
    e.listing.refresh();respond(e,'ok');respond(e,'chats',chat_ids=[],total_count=0)
    page=e.listing.query()
    assert page['state']=='ready' and page['failure_stage'] is None and page['failure_reason'] is None


@pytest.mark.parametrize('anonymous',[True,False])
def test_null_supergroup_sender_requires_current_nonanonymous_self_membership(listing_env,anonymous):
    e=listing_env;e.listing.refresh();respond(e,'ok');respond(e,'chats',chat_ids=[-100],total_count=1)
    respond(e,'chat',**chat(-100,'Group',{'@type':'chatTypeSupergroup','supergroup_id':100,'is_channel':False}))
    assert e.client.requests[-1]['@type']=='getChatMember'
    respond(e,'chatMember',member_id={'@type':'messageSenderUser','user_id':7},
        status={'@type':'chatMemberStatusAdministrator','rights':{'is_anonymous':anonymous}})
    item=e.listing.query()['items'][0]
    assert item['can_verify'] is (not anonymous)
    assert item['sender']==(None if anonymous else {'kind':'user','id':'7'})


def test_chat_limit_is_reported_as_capped_not_complete(listing_env,monkeypatch):
    import threadrelay.delivery.destination_listing as module
    monkeypatch.setattr(module,'MAX_LIST',2)
    e=listing_env;e.listing.refresh();respond(e,'ok');respond(e,'chats',chat_ids=[7,8],total_count=8)
    respond(e,'chat',**chat(7,'Own',{'@type':'chatTypePrivate','user_id':7}))
    respond(e,'chat',**chat(8,'Other',{'@type':'chatTypePrivate','user_id':8}))
    assert e.listing.query()['capped'] and not e.listing.query()['exhausted']
    e.listing.refresh(load_more=True);respond(e,'error',code=404);respond(e,'chats',chat_ids=[],total_count=0)
    value=e.listing.query()
    assert value['capped'] and not value['exhausted'] and not value['can_load_more']


def test_global_chat_limit_keeps_existing_names_and_reports_cap(listing_env,monkeypatch):
    import threadrelay.delivery.destination_listing as module
    monkeypatch.setattr(module,'MAX_TOTAL',1)
    e=listing_env;e.listing.refresh();respond(e,'ok');respond(e,'chats',chat_ids=[7],total_count=8)
    respond(e,'chat',**chat(7,'Own',{'@type':'chatTypePrivate','user_id':7}))
    e.listing.refresh(load_more=True);respond(e,'ok');respond(e,'chats',chat_ids=[7,8],total_count=8)
    value=e.listing.query()
    assert [item['chat_id'] for item in value['items']]==['7']
    assert value['state']=='ready' and value['capped'] and not value['exhausted'] and not value['can_load_more']
    assert e.client.requests[-1]['@type']=='getChats'


@pytest.mark.parametrize('group',[False,True])
@pytest.mark.parametrize('omitted_sender',[False,True])
def test_nullable_fixed_personal_sender_can_be_verified_without_sender_mutation(listing_env,group,omitted_sender):
    from threadrelay.delivery.destinations import DestinationVerifier
    e=listing_env;verifier=DestinationVerifier(e.client,e.auth,e.config,e.session)
    if group:verifier.begin_group('-20',sender={'kind':'user','id':'7'})
    else:verifier.begin_private('8')
    request=e.client.requests[-1]
    raw=chat(-20 if group else 8,'Named chat',{'@type':'chatTypeBasicGroup','basic_group_id':20} if group else {'@type':'chatTypePrivate','user_id':8},permissions={'can_send_basic_messages':True})
    if omitted_sender:raw.pop('message_sender_id')
    assert verifier.consume({'@type':'chat','@extra':request['@extra'],**raw})
    assert verifier.pending is not None
    request=e.client.requests[-1]
    if group:
        assert request['@type']=='getBasicGroup'
        verifier.consume({'@type':'basicGroup','@extra':request['@extra'],'id':20,'is_active':True})
        request=e.client.requests[-1];assert request['@type']=='getChatMember'
        verifier.consume({'@type':'chatMember','@extra':request['@extra'],'member_id':{'@type':'messageSenderUser','user_id':7},'status':{'@type':'chatMemberStatusMember'}})
    else:
        assert request['@type']=='canSendMessageToUser'
        verifier.consume({'@type':'canSendMessageToUserResultOk','@extra':request['@extra']})
    snapshot,_=verifier.finish()
    assert snapshot['sender']=={'kind':'user','id':'7'}
    assert not any(request['@type']=='setChatMessageSender' for request in e.client.requests)


@pytest.mark.parametrize('anonymous',[True,False])
@pytest.mark.parametrize('omitted_nullable',[False,True])
def test_null_supergroup_personal_verification_rechecks_anonymous_membership(listing_env,anonymous,omitted_nullable):
    from threadrelay.delivery.destinations import DestinationVerifier
    e=listing_env;verifier=DestinationVerifier(e.client,e.auth,e.config,e.session)
    verifier.begin_group('-100',sender={'kind':'user','id':'7'})
    raw=chat(-100,'Supergroup',{'@type':'chatTypeSupergroup','supergroup_id':100,'is_channel':False},permissions={'can_send_basic_messages':True})
    if omitted_nullable:raw.pop('message_sender_id')
    request=e.client.requests[-1];verifier.consume({'@type':'chat','@extra':request['@extra'],**raw})
    request=e.client.requests[-1];assert request['@type']=='getSupergroup'
    raw={'@type':'supergroup','@extra':request['@extra'],'id':100,'is_channel':False,'paid_message_star_count':0,'restriction_info':None,'usernames':None}
    if omitted_nullable:
        raw.pop('restriction_info');raw.pop('usernames')
    verifier.consume(raw)
    request=e.client.requests[-1];verifier.consume({'@type':'chatMember','@extra':request['@extra'],'member_id':{'@type':'messageSenderUser','user_id':7},'status':{'@type':'chatMemberStatusAdministrator','rights':{'is_anonymous':anonymous}}})
    if anonymous:
        with pytest.raises(TelegramBoundaryError):verifier.finish()
        assert verifier.evidence is None
    else:
        assert verifier.finish()[0]['sender']=={'kind':'user','id':'7'}


@pytest.mark.parametrize('restriction',[None,{'@type':'restrictionInfo','restriction_reasons':[]}])
def test_private_supergroup_omitted_metadata_does_not_allow_a_restriction(listing_env,restriction):
    from threadrelay.delivery.destinations import DestinationVerifier
    e=listing_env;verifier=DestinationVerifier(e.client,e.auth,e.config,e.session)
    verifier.begin_group('-100',sender={'kind':'user','id':'7'})
    verifier.consume({'@type':'chat','@extra':verifier.pending['extra'],
        **chat(-100,'Supergroup',{'@type':'chatTypeSupergroup','supergroup_id':100,'is_channel':False},
            message_sender_id={'@type':'messageSenderUser','user_id':7},permissions={'can_send_basic_messages':True})})
    raw={'@type':'supergroup','@extra':verifier.pending['extra'],'id':100,'is_channel':False,'paid_message_star_count':0}
    if restriction is not None:raw['restriction_info']=restriction
    verifier.consume(raw)
    if restriction is not None:
        assert verifier.pending is None and verifier.evidence is None
        assert e.client.requests[-1]['@type']=='getSupergroup'
    else:
        assert verifier.pending is not None, 'Omitted nullable metadata means no restriction or public usernames'
        assert e.client.requests[-1]['@type']=='getChatMember'
        verifier.consume({'@type':'chatMember','@extra':verifier.pending['extra'],
            'member_id':{'@type':'messageSenderUser','user_id':7},'status':{'@type':'chatMemberStatusMember'}})
        snapshot,_=verifier.finish()
        assert snapshot['audience']=='private' and snapshot['sender']=={'kind':'user','id':'7'}
