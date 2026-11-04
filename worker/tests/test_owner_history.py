"""Explicit manual History composition; fake native exports, no live services."""
import json
from types import SimpleNamespace

import pytest

from test_connection_only import LocalLibrary
from test_delivery_workflow import Clock
from test_routing import ScoresRuntime, post, topic
from test_telegram_auth import MemorySecretHandoff
from threadrelay.application import LazyTelegramClient
from threadrelay.delivery.auth import AuthController
from threadrelay.delivery.destinations import DestinationVerifier
from threadrelay.delivery.owner_history import OwnerHistoryController
from threadrelay.delivery.sender import Sender
from threadrelay.delivery.telegram import NativeJsonBinding, NetworkPermit, TelegramBoundaryError, TelegramClient
from threadrelay.delivery.workflow import DeliveryWorkflow
from threadrelay.inference.decision import Classifier
from threadrelay.routing.policy import Router
from threadrelay.session import SessionController
from threadrelay.storage.archive import ArchiveStore
from threadrelay.storage.database import Database
from threadrelay.storage.outbox import AccountStore
from threadrelay.storage.routing import ConfigStore
from threadrelay.storage.saved_destinations import SavedDestinations


@pytest.fixture
def manual(tmp_path):
    clock=Clock();db=Database(tmp_path/'app.db',clock=clock.now)
    config=ConfigStore(db);config.save_topic(topic())
    accounts=AccountStore(db)
    accounts.activate({'account_id':'123','epoch':0,'auth_state':'ready','display_name':None,'network_state':'offline','revocation_pending':False})
    library=LocalLibrary();library.client_id=0;library.before_send=None;library.fail_send=False;library.chat_changes={};library.member_changes={}
    library.chat_id=-20;library.group_id=20;library.supergroup=False;library.list_contains_target=True;library.extra_chat_ids=[]
    library.dialog_ready=False;library.info_ready=False
    def execute(_):
        import ctypes
        library.result=ctypes.create_string_buffer(b'{"@type":"ok"}')
        return ctypes.addressof(library.result)
    library.td_execute.call=execute
    def create():
        library.client_id+=1;library.dialog_ready=False;library.info_ready=False
        return library.client_id
    library.td_create_client_id.call=create
    original_send=library.td_send.call
    def native_send(client_id,data):
        request=json.loads(data);method=request['@type'];extra=request.get('@extra')
        if method=='sendMessage' and library.before_send:library.before_send(request)
        original_send(client_id,data)
        if method=='sendMessage' and library.fail_send:raise RuntimeError('synthetic native uncertainty')
        body=None
        if method in {'setNetworkType','setTdlibParameters','setOption'}:body={'@type':'ok'}
        elif method=='getMe':body={'@type':'user','id':123,'type':{'@type':'userTypeRegular'}}
        elif method=='loadChats':
            if library.list_contains_target:library.dialog_ready=True;library.info_ready=True
            body={'@type':'ok'}
        elif method=='getChats':
            ids=([library.chat_id] if library.dialog_ready else [])+library.extra_chat_ids
            body={'@type':'chats','chat_ids':ids,'total_count':len(ids)}
        elif method in {'getChat','createBasicGroupChat','createSupergroupChat'}:
            if method!='getChat' and library.info_ready:library.dialog_ready=True
            body=({'@type':'chat','id':library.chat_id,'title':'Saved group',
                'type':({'@type':'chatTypeSupergroup','supergroup_id':library.group_id,'is_channel':False} if library.supergroup else {'@type':'chatTypeBasicGroup','basic_group_id':library.group_id}),
                'permissions':{'can_send_basic_messages':True}} if library.dialog_ready else {'@type':'error','code':400,'message':'Chat not found'})
        elif method=='getBasicGroup':
            library.info_ready=True
            body={'@type':'basicGroup','id':library.group_id,'is_active':True}
        elif method=='getSupergroup':body={'@type':'supergroup','id':library.group_id,'is_channel':False,'paid_message_star_count':0}
        elif method=='getChatMember':body={'@type':'chatMember','member_id':{'@type':'messageSenderUser','user_id':123},'status':{'@type':'chatMemberStatusMember'}}
        if method in {'getChat','createBasicGroupChat','createSupergroupChat'}:body.update(library.chat_changes)
        if method=='getChatMember':body.update(library.member_changes)
        if body is not None:library.events.append({'@client_id':client_id,'@extra':extra,**body})
        if method=='setNetworkType' and request['type']['@type']=='networkTypeOther':
            library.events.append({'@client_id':client_id,'@type':'updateConnectionState','state':{'@type':'connectionStateReady'}})
    library.td_send.call=native_send
    class Vault:
        def resolve(self,_):return {'api_id':12345,'api_hash':'synthetic','database_key':b'k'*32}
    client=LazyTelegramClient(lambda:TelegramClient(lambda:NativeJsonBinding(library,purpose='connection_only',generation=3),Vault(),tmp_path/'sessions'))
    session=SessionController(clock=clock)
    auth=AuthController(client,accounts,MemorySecretHandoff(),clock=clock.monotonic,lifecycle_lock=session.authority_lock)
    verifier=DestinationVerifier(client,auth,config,session)
    router=Router(db,classifier=Classifier(ScoresRuntime()),clock=clock)
    sender=Sender(router,session,auth,client,destination_authority=verifier,clock=clock.now,monotonic=clock.monotonic)
    archive=ArchiveStore(db)
    workflow=DeliveryWorkflow(db,archive,auth=auth,destinations=verifier,sender=sender,native_gate=lambda:False,clock=clock)
    gate=[True]
    controller=OwnerHistoryController(client,auth,session,workflow,native_gate=lambda:gate[0],monotonic=clock.monotonic)
    env=SimpleNamespace(db=db,config=config,accounts=accounts,library=library,client=client,session=session,auth=auth,sender=sender,
        verifier=verifier,workflow=workflow,archive=archive,controller=controller,clock=clock,gate=gate)
    connect(env)
    verifier.begin_group('-20',sender={'kind':'user','id':'123'})
    drain(env)
    destination,version=verifier.finish()
    store=SavedDestinations(db);store.require_saved_selection();store.save_verified(destination,0,'group')
    env.target={'account_id':'123','account_epoch':0,'destination_id':'-20','destination_version':version,'sender':{'kind':'user','id':'123'}}
    env.destination=destination
    yield env
    workflow.close()
    if client._client is not None and client._profile_guard is not None:client._profile_guard.close()
    if client._client is not None:
        from threadrelay.delivery.telegram import _OWNED_ROOTS
        _OWNED_ROOTS.discard(client._root)
    db.close()


def drain(env):
    while env.library.events:env.sender.poll()
    env.sender.poll()  # The real owner drains through one empty receive before tick.


def auth_state(env,name,client_id=None):
    return {'@client_id':client_id or env.library.client_id,'@type':'updateAuthorizationState','authorization_state':{'@type':'authorizationState'+name}}


def connect(env,*,discover=True):
    env.auth.begin('vault:fixture',pending=False)
    env.library.events.append(auth_state(env,'WaitTdlibParameters'));drain(env)
    env.auth.enable_clean_auth(NetworkPermit('clean_auth',0,False));drain(env)
    env.library.events.append(auth_state(env,'Ready'));drain(env)
    if discover:
        env.client._binding.send(env.library.client_id,{'@type':'loadChats','chat_list':{'@type':'chatListMain'},'limit':100,'@extra':'listing:load:'+'b'*32})
        drain(env)
        env.client._binding.send(env.library.client_id,{'@type':'getChats','chat_list':{'@type':'chatListMain'},'limit':100,'@extra':'listing:chats:'+'a'*32})
        drain(env)


def job(env,suffix='Manual',**changes):
    source=post(post_id='threads:'+suffix,permalink='https://www.threads.net/@synthetic/post/'+suffix)
    revision=env.archive.observe(source)
    request={'id':None,'expected_version':None,'revision_ids':[revision],'target':env.target,'template_id':'url','template_version':1,
        'timing':{'mode':'now','timezone':'Asia/Hong_Kong'},**changes}
    draft=env.workflow.configure(request);preview=env.workflow.preview(draft['id'],draft['version'])
    return env.workflow.approve(draft['id'],draft['version'],preview['preview_hash'])['items'][0]


def preview(env,item):return env.controller.preview({'job_id':item['id'],'version':item['version']})


def begin(env,item,exact=None):
    exact=exact or preview(env,item)
    env.controller.begin({'job_id':item['id'],'version':item['version'],'preview_receipt':exact['preview_receipt']})
    return exact


def dispatch(env,item):
    begin(env,item);drive(env)


def drive(env):
    for _ in range(8):
        drain(env);env.controller.tick();env.workflow.tick()
        if env.controller.query()['state']!='verifying':break


def result(env,*,client=None,chat=-20,sender=123):
    sends=[request for request in env.library.calls if request['@type']=='sendMessage']
    extra=sends[-1]['@extra'];identifier=client or env.library.client_id
    env.library.events.extend([
        {'@client_id':identifier,'@type':'message','@extra':extra,'id':-len(sends),'chat_id':chat,'sender_id':{'@type':'messageSenderUser','user_id':sender}},
        {'@client_id':identifier,'@type':'updateMessageSendSucceeded','old_message_id':-len(sends),'message':{'id':7+len(sends),'chat_id':chat,'sender_id':{'@type':'messageSenderUser','user_id':sender}}},
    ]);drain(env)


def close(env):env.library.events.append(auth_state(env,'Closed'));drain(env)


def test_exact_manual_preview_is_inert_and_dispatch_commits_before_one_native_call(manual):
    env=manual;item=job(env);env.verifier.invalidate();before=len(env.library.calls)
    exact=preview(env,item)
    assert exact['job_id']==item['id'] and exact['text']=='https://www.threads.net/@synthetic/post/Manual'
    assert len(exact['preview_receipt'])==64 and len(env.library.calls)==before
    def committed(request):
        assert request['chat_id']==-20 and request['input_message_content']['text']=={'@type':'formattedText','text':exact['text'],'entities':[]}
        assert request['options']['paid_message_star_count']==0 and request['options']['allow_paid_broadcast'] is False
        assert json.loads((env.client._profile_guard.path/'connection-purpose.json').read_text())['body']['purpose']=='manual_history_pending'
        with env.db.connection() as con:
            row=con.execute('SELECT * FROM workflow_jobs WHERE id=?',(item['id'],)).fetchone()
            assert row['status']=='sending' and row['attempt_id']
            assert con.execute('SELECT initiated_attempt_id FROM workflow_delivery_context WHERE job_id=?',(item['id'],)).fetchone()[0]==row['attempt_id']
    env.library.before_send=committed
    begin(env,item,exact);assert env.controller.query()['state']=='verifying'
    drive(env)
    assert env.controller.query()['state']=='sending'
    sends=[request for request in env.library.calls if request['@type']=='sendMessage'];assert len(sends)==1
    for _ in range(3):env.controller.query();env.controller.tick();env.workflow.tick()
    assert len([request for request in env.library.calls if request['@type']=='sendMessage'])==1
    with pytest.raises(TelegramBoundaryError):env.client._binding.send(env.library.client_id,sends[0])


def test_manual_sent_requires_matching_native_success_and_keeps_owned_client_ready(manual):
    env=manual;item=job(env);dispatch(env,item)
    result(env,client=999);assert env.controller.query()['state']=='sending'
    result(env,chat=-21);assert env.controller.query()['state']=='sending'
    result(env);assert env.controller.query()['state']=='sent'
    assert env.auth.status=='ready' and env.client.transport_ready
    assert env.controller.query()['state']=='sent'
    assert env.workflow.query_jobs()['items'][0]['status']=='sent'
    assert env.client._profile_guard.purpose=='manual_history_sent'


@pytest.mark.parametrize('action',['pause','stop','attention'])
def test_collection_controls_keep_same_authenticated_client(manual,action):
    env=manual;client=env.client._client;profile=env.client._profile_guard
    env.session._setup_check=lambda _: {'allowed':True,'reason':None}
    env.session.start({'feed_url':'https://www.threads.com/','mode':'review'})
    if action=='attention':env.session.require_attention('model_unavailable')
    else:getattr(env.session,action)()
    drain(env)
    assert env.auth.status=='ready' and env.client.transport_ready
    assert env.client._client is client and env.client._profile_guard is profile
    assert env.library.client_id==1
    assert not any(request['@type'] in {'close','logOut'} for request in env.library.calls)
    assert not any(request['@type']=='setNetworkType' and request['type']['@type']=='networkTypeNone'
        for request in env.library.calls[1:])
    assert not env.session.can_transmit(env.clock.now())


def test_unknown_manual_outcome_blocks_more_sends_without_disconnecting_login(manual):
    env=manual;item=job(env);env.library.fail_send=True;dispatch(env,item)
    assert env.controller.query()['state']=='unknown'
    drain(env)
    assert env.auth.status=='ready' and env.client.transport_ready
    assert not any(request['@type'] in {'close','logOut'} for request in env.library.calls)
    for _ in range(3):env.controller.tick();env.workflow.tick()
    assert len([request for request in env.library.calls if request['@type']=='sendMessage'])==1


def test_metadata_rejection_keeps_login_and_explicit_account_cancel_still_closes(manual):
    env=manual;item=job(env);env.library.chat_changes={'title':'Changed group'}
    dispatch(env,item)
    assert env.controller.query()['state']=='failed'
    assert env.auth.status=='ready' and env.client.transport_ready
    assert env.client._profile_guard.purpose=='connection_only'
    assert not any(request['@type'] in {'sendMessage','close','logOut'} for request in env.library.calls)
    env.auth.cancel()
    assert env.library.calls[-1]=={'@type':'close','@extra':'auth:close'}
    close(env)
    assert env.auth.status=='closed' and not env.client.transport_ready


def test_collection_resume_keeps_existing_network_while_exact_receipt_is_pending(manual):
    env=manual;item=job(env);dispatch(env,item)
    env.session._setup_check=lambda _: {'allowed':True,'reason':None}
    env.session.pause()
    env.session.start({'feed_url':'https://www.threads.com/','mode':'review'})
    before=len(env.library.calls)
    env.sender.enable_running_network()
    assert len(env.library.calls)==before
    assert env.auth.status=='ready' and env.client.transport_ready and env.library.client_id==1
    result(env)
    assert env.controller.query()['state']=='sent'


@pytest.mark.parametrize('stage',['before_dispatch','sending','sent'])
def test_manual_revocation_never_sends_or_retries_after_uncertainty(manual,stage):
    env=manual;item=job(env);begin(env,item)
    if stage!='before_dispatch':drive(env)
    if stage=='sent':result(env)
    env.controller.revoke()
    expected='failed' if stage=='before_dispatch' else 'sent' if stage=='sent' else 'unknown'
    assert env.controller.query()['state']==expected
    before=len([request for request in env.library.calls if request['@type']=='sendMessage'])
    for _ in range(3):env.controller.tick();env.workflow.tick()
    assert len([request for request in env.library.calls if request['@type']=='sendMessage'])==before
    if expected=='unknown':
        assert env.workflow.query_jobs()['items'][0]['status']=='unknown'
        with pytest.raises(TelegramBoundaryError):begin(env,item)


@pytest.mark.parametrize('change',['receipt','expired','template','saved_title','schedule'])
def test_changed_manual_preflight_never_mints_native_authority(manual,change):
    env=manual
    item=job(env,**({'timing':{'mode':'at_time','timezone':'Asia/Hong_Kong','at':'2026-11-02T00:00:00Z'}} if change=='schedule' else {}))
    if change=='schedule':
        with pytest.raises(TelegramBoundaryError):preview(env,item)
        return
    exact=preview(env,item)
    if change=='receipt':exact['preview_receipt']='0'*64
    elif change=='expired':env.clock.advance(61)
    elif change=='template':
        env.config.save_destination({**env.destination,'paid_required':True},account_epoch=0)
    else:env.config.save_destination({**env.destination,'title':'Changed group'},account_epoch=0)
    before=len(env.library.calls)
    with pytest.raises(TelegramBoundaryError):begin(env,item,exact)
    assert len(env.library.calls)==before


@pytest.mark.parametrize('failure',['dispatch','timeout','profile_pending','profile_terminal'])
def test_possible_dispatch_or_durability_failure_is_unknown_and_quarantined(manual,monkeypatch,failure):
    env=manual;item=job(env)
    if failure=='dispatch':env.library.fail_send=True
    if failure=='profile_pending':
        def fail(_):raise OSError('synthetic fsync uncertainty')
        monkeypatch.setattr(env.client._profile_guard,'begin_manual_history',fail)
    dispatch(env,item)
    if failure=='timeout':env.clock.advance(31);env.controller.tick()
    elif failure=='profile_terminal':
        def fail(*args,**kwargs):raise OSError('synthetic terminal fsync uncertainty')
        monkeypatch.setattr(env.client._profile_guard,'complete_manual_history_sent',fail);result(env)
    assert env.controller.query()['state']=='unknown'
    assert env.workflow.query_jobs()['items'][0]['status']=='unknown'
    count=len([request for request in env.library.calls if request['@type']=='sendMessage'])
    for _ in range(2):env.controller.tick();env.workflow.tick()
    assert len([request for request in env.library.calls if request['@type']=='sendMessage'])==count


def test_distinct_manual_jobs_reuse_authenticated_client_with_fresh_confirmation(manual):
    env=manual;first=job(env,'First');second=job(env,'Second')
    client_id=env.client._client_id;binding=env.client._binding;profile=env.client._profile_guard
    dispatch(env,first);result(env)
    assert env.controller.query()['state']=='sent'
    assert env.auth.status=='ready' and env.client.transport_ready
    assert env.client._profile_guard is profile and env.client._binding is binding
    before=len([request for request in env.library.calls if request['@type']=='sendMessage'])
    second=next(value for value in env.workflow.query_jobs()['items'] if value['id']==second['id'])
    exact=preview(env,second)
    env.controller.tick();env.workflow.tick()
    assert len([request for request in env.library.calls if request['@type']=='sendMessage'])==before
    begin(env,second,exact);drive(env)
    assert env.controller.query()['state']=='sending'
    result(env)
    assert env.controller.query()['state']=='sent'
    assert all(item['status']=='sent' for item in env.workflow.query_jobs()['items'])
    assert env.client._client_id==client_id and env.library.client_id==1
    assert env.client._profile_guard is profile and env.client._binding is binding
    assert not any(request['@type'] in {'close','logOut'} for request in env.library.calls)
    assert env.workflow.manual_history_profile_ready(profile.profile_id)
    with pytest.raises(TelegramBoundaryError):begin(env,second,exact)
    with pytest.raises(TelegramBoundaryError):env.client._binding.send(client_id,{'@type':'sendMessage','chat_id':-20})


def test_claim_commit_uncertainty_persists_unknown_and_never_enters_native(manual,monkeypatch):
    env=manual;item=job(env);claim=env.workflow.claim_manual_history
    def committed_then_failed(*args):
        claim(*args)
        raise OSError('synthetic commit response loss')
    monkeypatch.setattr(env.workflow,'claim_manual_history',committed_then_failed)
    dispatch(env,item)
    assert env.controller.query()['state']=='unknown'
    assert env.workflow.query_jobs()['items'][0]['status']=='unknown'
    assert not any(request['@type']=='sendMessage' for request in env.library.calls)


def test_rejected_verification_begin_does_not_leave_a_live_manual_intent(manual,monkeypatch):
    env=manual;item=job(env);verify=env.verifier.begin_group
    def unavailable(*args,**kwargs):raise TelegramBoundaryError('synthetic metadata failure')
    monkeypatch.setattr(env.verifier,'begin_group',unavailable)
    begin(env,item);drive(env)
    assert env.controller.query()['state']=='failed'
    assert not any(request['@type']=='sendMessage' for request in env.library.calls)
    monkeypatch.setattr(env.verifier,'begin_group',verify)
    item=next(value for value in env.workflow.query_jobs()['items'] if value['id']==item['id'])
    dispatch(env,item)
    assert env.controller.query()['state']=='sending'


@pytest.mark.parametrize('change',['foreign_chat','foreign_sender','anonymous_member','channel'])
def test_manual_native_metadata_cannot_substitute_target_or_sender(manual,change):
    env=manual;item=job(env)
    if change=='foreign_chat':env.library.chat_changes={'id':-21}
    elif change=='foreign_sender':env.library.chat_changes={'message_sender_id':{'@type':'messageSenderUser','user_id':456}}
    elif change=='channel':env.library.chat_changes={'type':{'@type':'chatTypeSupergroup','supergroup_id':20,'is_channel':True}}
    else:env.library.member_changes={'status':{'@type':'chatMemberStatusAdministrator','rights':{'is_anonymous':True}}}
    dispatch(env,item)
    assert env.controller.query()['state']=='failed'
    assert not any(request['@type']=='sendMessage' for request in env.library.calls)


def test_manual_permit_is_one_use_even_after_native_exception(manual,monkeypatch):
    env=manual;item=job(env);permits=[];send=env.client._binding.send_owner_history
    def capture(permit):permits.append(permit);return send(permit)
    monkeypatch.setattr(env.client._binding,'send_owner_history',capture)
    env.library.fail_send=True;dispatch(env,item)
    assert env.controller.query()['state']=='unknown' and len(permits)==1
    with pytest.raises(TelegramBoundaryError):send(permits[0])
    assert len([request for request in env.library.calls if request['@type']=='sendMessage'])==1


def test_manual_restart_cannot_reopen_or_retry_pending_native_profile(manual):
    from threadrelay.delivery.connection_profile import connection_only_profile
    env=manual;item=job(env);dispatch(env,item)
    path=env.client._profile_guard.path
    env.client._profile_guard.close();env.workflow.close();env.db.recover()
    reopened=DeliveryWorkflow(env.db,env.archive,auth=env.auth,destinations=env.verifier,sender=env.sender,native_gate=lambda:False,clock=env.clock)
    assert reopened.query_jobs()['items'][0]['status']=='unknown'
    with pytest.raises(ValueError):connection_only_profile(path,'vault:fixture',b'k'*32)
    reopened.tick()
    assert len([request for request in env.library.calls if request['@type']=='sendMessage'])==1
    reopened.close()


def test_manual_rate_limit_and_changed_template_keep_preflight_inert(manual):
    env=manual
    template=env.workflow.save_template({'id':None,'expected_version':None,'name':'Exact','body':'{{url}}'})
    item=job(env,template_id=template['id'],template_version=1)
    exact=preview(env,item)
    env.workflow.save_template({'id':template['id'],'expected_version':1,'name':'Changed','body':'Changed {{url}}'})
    with pytest.raises(TelegramBoundaryError):begin(env,item,exact)
    fresh=job(env,'Rate')
    env.sender.router._started[('123','-20')]=[env.clock.monotonic(),env.clock.monotonic()]
    before=len(env.library.calls)
    with pytest.raises(TelegramBoundaryError):preview(env,fresh)
    assert len(env.library.calls)==before


def test_manual_status_stays_bound_to_original_confirmed_job_version(manual):
    env=manual;item=job(env);begin(env,item)
    assert env.controller.query()['job_version']==item['version']
    drive(env)
    assert env.controller.query()['state']=='sending' and env.controller.query()['job_version']==item['version']
    result(env)
    assert env.controller.query()['state']=='sent' and env.controller.query()['job_version']==item['version']


def test_sqlite_completion_failure_quarantines_session_after_native_success(manual,monkeypatch):
    from threadrelay.delivery.connection_profile import connection_only_profile
    env=manual;item=job(env);dispatch(env,item)
    path=env.client._profile_guard.path
    def unavailable(*args):raise OSError('synthetic completion durability failure')
    monkeypatch.setattr(env.workflow,'complete_manual_history',unavailable)
    result(env)
    assert env.controller.query()['state']=='unknown'
    with pytest.raises(ValueError):connection_only_profile(path,'vault:fixture',b'k'*32)


def test_native_authority_revocation_after_dispatch_blocks_resend_without_closing_login(manual):
    env=manual;item=job(env);dispatch(env,item);env.gate[0]=False
    env.controller.tick()
    assert env.controller.query()['state']=='unknown'
    assert env.workflow.query_jobs()['items'][0]['status']=='unknown'
    assert not any(request['@type']=='close' for request in env.library.calls)
    assert env.auth.status=='ready' and env.client.transport_ready


@pytest.mark.parametrize('failure_point',['profile','sqlite','finish'])
def test_published_terminal_marker_with_lost_return_still_denies_new_manual_permit(manual,monkeypatch,failure_point):
    env=manual;item=job(env);other=job(env,'Other');dispatch(env,item)
    complete=(env.client._profile_guard.complete_manual_history_sent if failure_point=='profile' else
        env.workflow.complete_manual_history if failure_point=='sqlite' else env.workflow.finish_manual_history)
    def published_then_lost(*args,**kwargs):
        complete(*args,**kwargs)
        env.client._profile_guard.close()
        raise OSError('synthetic published terminal response loss')
    if failure_point=='profile':monkeypatch.setattr(env.client._profile_guard,'complete_manual_history_sent',published_then_lost)
    elif failure_point=='sqlite':monkeypatch.setattr(env.workflow,'complete_manual_history',published_then_lost)
    else:monkeypatch.setattr(env.workflow,'finish_manual_history',published_then_lost)
    result(env)
    assert env.controller.query()['state']=='unknown'
    assert env.workflow.manual_history_profile_ready(env.client._profile_guard.profile_id) is False
    # Even a new controller and a valid authenticated Sent profile cannot mint
    # another permit while the independently durable completion latch remains.
    env.client._owner_history=None
    replacement=OwnerHistoryController(env.client,env.auth,env.session,env.workflow,native_gate=lambda:True,monotonic=env.clock.monotonic)
    close(env)
    from threadrelay.delivery.auth import AuthFailure
    previous_client=env.library.client_id
    with pytest.raises(AuthFailure):connect(env)
    assert env.library.client_id==previous_client
    with pytest.raises(TelegramBoundaryError):replacement.preview({'job_id':other['id'],'version':other['version']})
    assert len([request for request in env.library.calls if request['@type']=='sendMessage'])==1


def test_saved_group_after_reconnect_needs_no_bulk_chat_rediscovery(manual):
    env=manual;first=job(env,'First');second=job(env,'Second')
    dispatch(env,first);result(env);env.client.close();close(env)
    connect(env,discover=False)
    assert not env.client._binding._connection_listed_chats and not env.client._binding._connection_groups
    before=sum(request['@type']=='getChats' for request in env.library.calls)
    second=next(value for value in env.workflow.query_jobs()['items'] if value['id']==second['id'])
    dispatch(env,second)
    assert env.controller.query()['state']=='sending'
    result(env);close(env)
    assert env.controller.query()['state']=='sent'
    assert sum(request['@type']=='getChats' for request in env.library.calls)==before


@pytest.mark.parametrize('terminal',['sent','unknown'])
def test_stale_begin_cannot_replace_terminal_manual_outcome(manual,terminal):
    env=manual;item=job(env);exact=begin(env,item);drive(env)
    if terminal=='sent':result(env);close(env)
    else:env.controller.revoke()
    assert env.controller.query()['state']==terminal
    with pytest.raises(TelegramBoundaryError):env.controller.begin({'job_id':item['id'],'version':item['version'],'preview_receipt':exact['preview_receipt']})
    assert env.controller.query()['state']==terminal


@pytest.mark.parametrize('action',['pause','stop'])
def test_collection_controls_preserve_exact_manual_receipt_even_when_collection_was_stopped(manual,action):
    env=manual;env.session.add_gate_close_hook(env.controller.revoke)
    item=job(env);dispatch(env,item)
    getattr(env.session,action)()
    assert env.controller.query()['state']=='sending'
    result(env)
    assert env.controller.query()['state']=='sent'
    assert env.workflow.query_jobs()['items'][0]['status']=='sent'
    assert env.auth.status=='ready' and env.client.transport_ready
    assert len([request for request in env.library.calls if request['@type']=='sendMessage'])==1
    assert len([request for request in env.library.calls if request['@type']=='sendMessage'])==1


def test_connection_test_cannot_steal_manual_verification(manual):
    from threadrelay.delivery.owner_test import OwnerTestController
    env=manual;test=OwnerTestController(env.client,env.auth,env.session,native_gate=lambda:True,monotonic=env.clock.monotonic)
    item=job(env);begin(env,item);pending=env.verifier.pending
    with pytest.raises(TelegramBoundaryError):test.begin(chat_id='-20',account_id='123',account_epoch=0)
    assert test.query()['state']=='idle' and env.verifier.pending is pending


def test_real_manual_controller_roundtrips_closed_ipc_contracts(manual):
    from threadrelay.features import LocalFeatures
    from threadrelay.main import dispatch as ipc
    env=manual;item=job(env);features=LocalFeatures(env.db,owner_history=env.controller)
    def command(kind,payload):return {'version':1,'id':'manual-ipc','type':kind,'payload':payload}
    result=ipc(command('PreviewManualHistorySend',{'job_id':item['id'],'version':item['version']}),features=features)
    assert result['type']=='ManualHistoryPreview'
    request={'job_id':item['id'],'version':item['version'],'preview_receipt':result['payload']['preview_receipt']}
    assert ipc(command('SendManualHistoryOnce',request),features=features)['type']=='Acknowledged'
    drive(env)
    state=ipc(command('QueryManualHistorySend',{}),features=features)
    assert state['type']=='ManualHistorySendState' and state['payload']['state']=='sending'
    assert state['payload']['job_version']==item['version']


def test_job_cannot_publish_sent_before_authenticated_native_success_receipt_is_durable(manual,monkeypatch):
    env=manual;item=job(env);dispatch(env,item)
    complete=env.client._profile_guard.complete_manual_history_sent
    def durable_before_projection(*args,**kwargs):
        assert env.workflow.query_jobs()['items'][0]['status']=='sending'
        return complete(*args,**kwargs)
    monkeypatch.setattr(env.client._profile_guard,'complete_manual_history_sent',durable_before_projection)
    result(env)
    assert env.controller.query()['state']=='sent'


def saved_target(env,chat_id,*,supergroup=False):
    env.library.chat_id=int(chat_id);env.library.supergroup=supergroup
    env.library.group_id=-1000000000000-int(chat_id) if supergroup else -int(chat_id)
    destination={**env.destination,'chat_id':chat_id}
    version=env.config.save_destination(destination,account_epoch=0)
    SavedDestinations(env.db).save_verified(destination,0,'group')
    env.destination=destination;env.target={**env.target,'destination_id':chat_id,'destination_version':version}
    env.client.close();close(env);connect(env,discover=False)
    assert env.library.dialog_ready is False and env.library.info_ready is False


def test_cold_saved_basic_group_prepares_native_dialog_before_offline_get_chat(manual):
    env=manual;saved_target(env,'-725000001');item=job(env,'ColdBasic')
    before=len(env.library.calls);dispatch(env,item)
    requests=env.library.calls[before:]
    assert env.controller.query()['state']=='sending'
    assert [request['@type'] for request in requests[:3]]==['getBasicGroup','createBasicGroupChat','getChat']
    assert requests[0]['basic_group_id']==725000001
    assert requests[1]['basic_group_id']==725000001 and requests[1]['force'] is False
    assert not any(request['@type'] in {'loadChats','createNewBasicGroupChat','getChatHistory'} for request in requests)
    result(env,chat=-725000001);close(env)
    assert env.controller.query()['state']=='sent'


@pytest.mark.parametrize('found',[True,False])
def test_cold_supergroup_uses_bounded_id_bootstrap_and_requires_exact_target(manual,found):
    env=manual;saved_target(env,'-1000000000020',supergroup=True);env.library.list_contains_target=found
    env.library.extra_chat_ids=[-98765]
    item=job(env,'ColdSuper');before=len(env.library.calls);dispatch(env,item)
    requests=env.library.calls[before:];loads=[request for request in requests if request['@type']=='loadChats']
    assert 1<=len(loads)<=2 and all(request['limit']==100 for request in loads)
    assert not any(request['@type'] in {'getChatHistory','createNewSupergroupChat'} for request in requests)
    assert not env.client._binding._connection_listed_chats
    with pytest.raises(TelegramBoundaryError):env.client._binding.send(env.library.client_id,{'@type':'getChat','chat_id':-98765,'@extra':'setup:chat:'+'d'*32})
    if found:
        assert env.controller.query()['state']=='sending'
        assert next(i for i,request in enumerate(requests) if request['@type']=='getChats')<next(i for i,request in enumerate(requests) if request['@type']=='getChat')
    else:
        assert env.controller.query()['state']=='failed'
        assert not any(request['@type']=='sendMessage' for request in requests)


def test_background_workflow_does_not_rewrite_only_the_explicitly_owned_job(manual):
    env=manual;item=job(env,'Owned');other=job(env,'Unowned')
    assert item['blocked_reasons']==['native_delivery_unavailable']
    begin(env,item);other_rechecked=False
    for _ in range(8):
        drain(env);env.controller.tick();env.workflow.tick()
        unowned=next(value for value in env.workflow.query_jobs()['items'] if value['id']==other['id'])
        other_rechecked|='destination_unverified' in unowned['blocked_reasons']
        if env.controller.query()['state']!='verifying':break
    assert env.controller.query()['state']=='sending'
    latest=next(value for value in env.workflow.query_jobs()['items'] if value['id']==other['id'])
    assert latest['version']>other['version'] and other_rechecked


def test_cancel_after_preparation_prevents_further_target_metadata_requests(manual):
    env=manual;item=job(env);begin(env,item);drain(env)
    assert env.controller._preparation['stage']=='ready'
    env.workflow.cancel(item['id'],item['version'])
    before=len(env.library.calls);env.controller.tick()
    assert env.controller.query()['state']=='failed'
    assert len(env.library.calls)==before

@pytest.mark.parametrize('condition,reason',[
    ('offline','connection_required'),('connecting','connecting'),('gate','build_unavailable'),
    ('busy','busy'),('unknown','outcome_unknown'),('stale','job_changed'),
])
def test_manual_preview_reports_fixed_recovery_reason_without_native_effects(manual,condition,reason):
    from dataclasses import replace
    from threadrelay.features import LocalFeatures
    from threadrelay.main import dispatch as ipc
    env=manual;item=job(env)
    if condition in {'offline','connecting'}:
        env.auth._snapshot=replace(env.auth.snapshot(),network_state=condition)
    elif condition=='gate':env.gate[0]=False
    elif condition=='busy':env.controller._state='verifying'
    elif condition=='unknown':env.controller._state='unknown'
    elif condition=='stale':item['version']+=1
    before=len(env.library.calls)
    result=ipc({'version':1,'id':'explain','type':'PreviewManualHistorySend',
        'payload':{'job_id':item['id'],'version':item['version']}},features=LocalFeatures(env.db,owner_history=env.controller))
    assert result['type']=='Error'
    assert result['payload']=={'code':'invalid_request','message':'manual_history:'+reason}
    assert len(env.library.calls)==before


def test_delayed_terminal_of_previous_job_cannot_complete_next_job_on_same_client(manual):
    env=manual;first=job(env,'First');second=job(env,'Second')
    dispatch(env,first);result(env)
    second=next(value for value in env.workflow.query_jobs()['items'] if value['id']==second['id'])
    dispatch(env,second)
    sends=[request for request in env.library.calls if request['@type']=='sendMessage']
    assert sends[0]['@extra']!=sends[1]['@extra']
    # A delayed first-job terminal and initial response are valid native events,
    # but cannot establish the second job's outcome, before or after its response.
    stale={'@client_id':env.library.client_id,'@type':'updateMessageSendSucceeded','old_message_id':-1,
        'message':{'id':8,'chat_id':-20,'sender_id':{'@type':'messageSenderUser','user_id':123}}}
    response={'@client_id':env.library.client_id,'@type':'message','@extra':sends[1]['@extra'],'id':-2,
        'chat_id':-20,'sender_id':{'@type':'messageSenderUser','user_id':123}}
    env.library.events.extend([stale,response,stale]);drain(env)
    assert env.controller.query()['state']=='sending'
    assert next(value for value in env.workflow.query_jobs()['items'] if value['id']==second['id'])['status']=='sending'
    result(env)
    assert env.controller.query()['state']=='sent'
    assert len([request for request in env.library.calls if request['@type']=='sendMessage'])==2
    assert env.auth.status=='ready'


@pytest.mark.parametrize('mismatch',['client','chat','sender','temporary','final'])
def test_next_job_requires_exact_native_terminal_identity(manual,mismatch):
    env=manual;first=job(env,'First');second=job(env,'Second')
    dispatch(env,first);result(env)
    second=next(value for value in env.workflow.query_jobs()['items'] if value['id']==second['id'])
    dispatch(env,second)
    sends=[request for request in env.library.calls if request['@type']=='sendMessage']
    response={'@client_id':env.library.client_id,'@type':'message','@extra':sends[1]['@extra'],'id':-2,
        'chat_id':-20,'sender_id':{'@type':'messageSenderUser','user_id':123}}
    terminal={'@client_id':env.library.client_id,'@type':'updateMessageSendSucceeded','old_message_id':-2,
        'message':{'id':9,'chat_id':-20,'sender_id':{'@type':'messageSenderUser','user_id':123}}}
    if mismatch=='client':terminal['@client_id']=999
    elif mismatch=='chat':terminal['message']['chat_id']=-21
    elif mismatch=='sender':terminal['message']['sender_id']['user_id']=456
    elif mismatch=='temporary':terminal['old_message_id']=-999
    else:terminal['message']['id']=True
    env.library.events.extend([response,terminal]);drain(env)
    assert env.controller.query()['state']=='sending'
    result(env)
    assert env.controller.query()['state']=='sent'
    assert env.client.transport_ready


@pytest.mark.parametrize('stage',['pending','profile','sqlite','finish'])
def test_crash_between_terminal_publications_never_authorizes_incomplete_profile(manual,monkeypatch,stage):
    from threadrelay.delivery.connection_profile import connection_only_profile
    env=manual;item=job(env);dispatch(env,item)
    profile=env.client._profile_guard;path=profile.path;profile_id=profile.profile_id
    class Crash(BaseException):pass
    if stage!='pending':
        owner=profile if stage=='profile' else env.workflow
        name={'profile':'complete_manual_history_sent','sqlite':'complete_manual_history','finish':'finish_manual_history'}[stage]
        original=getattr(owner,name)
        def committed_then_crashed(*args,**kwargs):
            original(*args,**kwargs)
            raise Crash()
        monkeypatch.setattr(owner,name,committed_then_crashed)
        with pytest.raises(Crash):result(env)
    profile.close();env.workflow.close();env.db.recover()
    recovered=DeliveryWorkflow(env.db,env.archive,auth=env.auth,destinations=env.verifier,sender=env.sender,native_gate=lambda:False,clock=env.clock)
    try:
        assert recovered.manual_history_profile_ready(profile_id) is (stage=='finish')
        assert recovered.query_jobs()['items'][0]['status']==('unknown' if stage in {'pending','profile'} else 'sent')
        if stage=='pending':
            with pytest.raises(ValueError):connection_only_profile(path,'vault:fixture',b'k'*32)
        else:
            reopened=connection_only_profile(path,'vault:fixture',b'k'*32)
            assert reopened.manual_history_record()['closed'] is False
            reopened.close()
        recovered.tick()
        assert len([request for request in env.library.calls if request['@type']=='sendMessage'])==1
    finally:recovered.close()


def test_network_revocation_after_first_success_requires_connection_before_next_job(manual):
    env=manual;first=job(env,'First');second=job(env,'Second')
    dispatch(env,first);result(env)
    second=next(value for value in env.workflow.query_jobs()['items'] if value['id']==second['id'])
    env.client.close();close(env)
    with pytest.raises(TelegramBoundaryError):preview(env,second)
    assert env.controller.query()['state']=='sent'
    assert len([request for request in env.library.calls if request['@type']=='sendMessage'])==1
    connect(env,discover=False)
    dispatch(env,second);result(env)
    assert env.controller.query()['state']=='sent'
    assert env.auth.status=='ready' and env.library.client_id==2


def test_automatic_incomplete_latch_denies_manual_profile_reuse(manual):
    env=manual;profile=env.client._profile_guard
    with env.db.transaction() as con:
        con.execute('INSERT INTO local_policy(name,payload) VALUES (?,?)',('automatic-incomplete:'+profile.profile_id,'{}'))
    assert not env.workflow.manual_history_profile_ready(profile.profile_id)
    with pytest.raises(TelegramBoundaryError):preview(env,job(env))


def test_active_automatic_metadata_vetoes_manual_preview_and_connection_test(manual):
    from threadrelay.delivery.owner_test import OwnerTestController
    env=manual;item=job(env);env.client._owner_automatic=SimpleNamespace(busy=True)
    calls=len(env.library.calls)
    with pytest.raises(TelegramBoundaryError):preview(env,item)
    test=OwnerTestController(env.client,env.auth,env.session,native_gate=lambda:True,monotonic=env.clock.monotonic)
    with pytest.raises(TelegramBoundaryError):test.begin(chat_id='-20',account_id='123',account_epoch=0)
    assert len(env.library.calls)==calls and env.verifier.pending is None
