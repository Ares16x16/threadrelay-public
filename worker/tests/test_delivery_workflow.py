"""Synthetic durable planning; no native library, account or external send."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from test_routing import post


NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


def test_plain_template_uses_only_allowlisted_observed_values_without_recursion():
    from threadrelay.delivery.workflow_templates import render_template
    from threadrelay.contracts import validate_contract
    source=post(text='literal {{unknown}}',observed_metadata={'engagement':[{'kind':'likes','label':'1.2K'}]})
    validate_contract('ObservedPost', source)
    assert render_template('{{url}}',source)['text']==source['permalink']
    value=render_template('{{author}}\n{{text}}\nLikes: {{likes}}\n{{topic}}',source,topic='Science')
    assert value=={'text':'synthetic\nliteral {{unknown}}\nLikes: 1.2K\nScience','missing_fields':[],'error':None}


@pytest.mark.parametrize('body',['{{__class__}}','{{url.upper()}}','{{ url | exec }}','{{url','url}}'])
def test_template_unknown_or_unclosed_syntax_is_rejected(body):
    from threadrelay.delivery.workflow_templates import render_template
    with pytest.raises(ValueError):render_template(body,post())


def test_template_missing_metadata_is_explicit_and_unicode_uses_telegram_utf16_limit():
    from threadrelay.delivery.workflow_templates import render_template
    result=render_template('{{published_at}} {{likes}}',post())
    assert result['missing_fields']==['likes','published_at']
    assert '[missing: published_at]' in result['text']
    assert render_template('{{text}}',post(text='😀'*2048))['error'] is None
    assert render_template('{{text}}',post(text='😀'*2049))['error']=='message_too_long'


def test_due_times_are_concrete_and_random_is_chosen_only_during_planning():
    from threadrelay.delivery.workflow_schedule import plan_due
    now=NOW.timestamp();draws=[]
    def draw(low,high):draws.append((low,high));return 7
    assert plan_due({'mode':'fixed','timezone':'Asia/Hong_Kong','start_at':None,'interval_seconds':300},3,now,draw)==[now,now+300,now+600]
    due=plan_due({'mode':'random','timezone':'Asia/Hong_Kong','start_at':None,'minimum_seconds':5,'maximum_seconds':10},3,now,draw)
    assert due==[now,now+7,now+14] and draws==[(5,10),(5,10)]
    assert plan_due({'mode':'at_time','timezone':'Asia/Hong_Kong','at':'2026-11-02T00:00:00Z'},1,now,draw)==[now+86400]


@pytest.mark.parametrize('timing',[
    {'mode':'fixed','timezone':'Asia/Hong_Kong','start_at':None,'interval_seconds':0},
    {'mode':'random','timezone':'Asia/Hong_Kong','start_at':None,'minimum_seconds':10,'maximum_seconds':5},
    {'mode':'now','timezone':'../../private'},
    {'mode':'at_time','timezone':'Asia/Hong_Kong','at':'2026-11-02T08:00:00+08:00'},
])
def test_invalid_or_ambiguous_schedule_is_rejected(timing):
    from threadrelay.delivery.workflow_schedule import plan_due
    with pytest.raises(ValueError):plan_due(timing,1,NOW.timestamp(),lambda low,high:low)


class Clock:
    def __init__(self):self.wall=NOW.timestamp();self.elapsed=100.0
    def now(self):return datetime.fromtimestamp(self.wall,timezone.utc)
    def monotonic(self):return self.elapsed
    def advance(self,seconds):self.wall+=seconds;self.elapsed+=seconds


@pytest.fixture
def workflow_env(tmp_path):
    from pathlib import Path
    from test_telegram_auth import FixtureBinding,FixtureVault,MemorySecretHandoff,state
    from test_routing import ScoresRuntime,topic
    from threadrelay.delivery.auth import AuthController
    from threadrelay.delivery.destinations import DestinationVerifier
    from threadrelay.delivery.sender import Sender
    from threadrelay.delivery.telegram import TelegramClient
    from threadrelay.delivery.workflow import DeliveryWorkflow
    from threadrelay.inference.decision import Classifier
    from threadrelay.routing.policy import Router
    from threadrelay.session import SessionController
    from threadrelay.storage.archive import ArchiveStore
    from threadrelay.storage.database import Database
    from threadrelay.storage.outbox import AccountStore
    from threadrelay.storage.routing import ConfigStore
    migrations=Path(__file__).parents[1]/'src/threadrelay/storage/migrations'
    selected=[(index,next(migrations.glob(f'{index:03}_*.sql'))) for index in range(1,7)]
    clock=Clock();db=Database(tmp_path/'app.db',clock=clock.now,migrations=selected)
    config=ConfigStore(db);config.save_topic(topic())
    accounts=AccountStore(db);accounts.activate({'account_id':'123','epoch':0,'auth_state':'ready','display_name':None,'network_state':'online','revocation_pending':False})
    destination={'account_id':'123','chat_id':'123','title':'Synthetic own chat','audience':'private',
        'permissions':{'can_send':True,'can_select_sender':False},'sender':{'kind':'user','id':'123'},'paid_required':False}
    version=config.save_destination(destination,account_epoch=0)
    class Binding(FixtureBinding):
        fail_send=False
        paid=False
        def send(self,client,request):
            super().send(client,request)
            extra=request.get('@extra')
            if request['@type'] in {'setNetworkType','setTdlibParameters','setOption'}:
                self.events.append({'@client_id':42,'@type':'ok','@extra':extra})
                if request.get('type',{}).get('@type')=='networkTypeOther':
                    self.events.append({'@client_id':42,'@type':'updateConnectionState','state':{'@type':'connectionStateReady'}})
            elif request['@type']=='getMe':
                self.events.append({'@client_id':42,'@type':'user','@extra':extra,'id':123,'type':{'@type':'userTypeRegular'}})
            elif request['@type'] in {'createPrivateChat','getChat'}:
                self.events.append({'@client_id':42,'@type':'chat','@extra':extra,'id':123,'title':'Synthetic own chat',
                    'type':{'@type':'chatTypePrivate','user_id':123},'message_sender_id':None})
            elif request['@type']=='canSendMessageToUser':
                self.events.append({'@client_id':42,'@type':'canSendMessageToUserResultUserHasPaidMessages' if self.paid else 'canSendMessageToUserResultOk','@extra':extra})
            elif request['@type']=='sendMessage' and self.fail_send:
                raise RuntimeError('synthetic transport uncertainty')
            elif request['@type']=='close':self.events.append(state('Closed'))
    binding=Binding();session=SessionController(clock=clock)
    client=TelegramClient(lambda:binding,FixtureVault(),tmp_path/'sessions')
    auth=AuthController(client,accounts,MemorySecretHandoff(),clock=clock.monotonic,lifecycle_lock=session.authority_lock)
    verifier=DestinationVerifier(client,auth,config,session)
    router=Router(db,classifier=Classifier(ScoresRuntime()),clock=clock)
    sender=Sender(router,session,auth,client,destination_authority=verifier,clock=clock.now,monotonic=clock.monotonic)
    archive=ArchiveStore(db);gate={'native':False};draws=[]
    def draw(low,high):draws.append((low,high));return low
    workflow=DeliveryWorkflow(db,archive,auth=auth,destinations=verifier,sender=sender,native_gate=lambda:gate['native'],clock=clock,randint=draw)
    target={'account_id':'123','account_epoch':0,'destination_id':'123','destination_version':version,'sender':{'kind':'user','id':'123'}}
    env=SimpleNamespace(db=db,archive=archive,config=config,clock=clock,binding=binding,session=session,auth=auth,client=client,verifier=verifier,
        sender=sender,workflow=workflow,target=target,destination=destination,gate=gate,draws=draws)
    yield env
    workflow.close();db.close()


def configure(env,*,sources=None,timing=None,target=True,template=None,existing=None,source_policy=None):
    if sources is None:sources=[env.archive.observe(post(observed_at=env.clock.now().isoformat()))]
    request={'id':None if existing is None else existing['id'],'expected_version':None if existing is None else existing['version'],
        'revision_ids':sources,'target':env.target if target else None,'template_id':'url' if template is None else template['id'],
        'template_version':1 if template is None else template['version'],'timing':timing or {'mode':'now','timezone':'Asia/Hong_Kong'}}
    if source_policy is not None:request['source_policy']=source_policy
    return env.workflow.configure(request)


def approve(env,draft):
    preview=env.workflow.preview(draft['id'],draft['version'])
    return env.workflow.approve(draft['id'],draft['version'],preview['preview_hash'])['items']


def make_ready(env):
    from test_telegram_auth import state
    from threadrelay.delivery.telegram import NetworkPermit
    env.auth.begin('vault:fixture',pending=False)
    env.binding.events.append(state('WaitTdlibParameters'))
    while env.binding.events:env.auth.poll()
    env.auth.enable_clean_auth(NetworkPermit('clean_auth',0,False))
    env.binding.events.append(state('Ready'))
    while env.binding.events:env.auth.poll()
    env.verifier.evidence=(0,env.target['destination_version'],env.destination.copy())
    env.gate['native']=True


def test_preview_renders_metadata_from_the_immutable_validated_capture(workflow_env):
    from threadrelay.contracts import validate_contract
    env=workflow_env
    source=post(observed_metadata={'display_name':'Captured display name',
        'published_at':'2026-10-31T12:30:00Z',
        'engagement':[{'kind':'likes','label':'1.2K'},{'kind':'shares','label':'Share 3','count':3}]})
    validate_contract('ObservedPost',source)
    revision=env.archive.observe(source)
    template=env.workflow.save_template({'id':None,'expected_version':None,'name':'Captured metadata',
        'body':'{{display_name}}\n{{published_at}}\nLikes: {{likes}}\nShares: {{shares}}\n{{url}}'})
    draft=configure(env,sources=[revision],template=template)
    preview=env.workflow.preview(draft['id'],draft['version'])
    assert preview['items'][0]['text']==('Captured display name\n2026-10-31T12:30:00Z\n'
        'Likes: 1.2K\nShares: Share 3\nhttps://www.threads.net/@synthetic/post/Abc123')
    assert preview['items'][0]['missing_fields']==[]
    assert env.binding.requests==[]


def test_due_scan_advances_only_through_inspected_jobs(workflow_env):
    env=workflow_env;make_ready(env)
    revisions=[env.archive.observe(post(post_id=f'threads:Fair{i}',
        permalink=f'https://www.threads.net/@synthetic/post/Fair{i}')) for i in range(32)]
    approve(env,configure(env,sources=revisions))
    with env.db.connection() as con:
        expected=[row['id'] for row in con.execute('SELECT id FROM workflow_jobs ORDER BY seq')]
    claimed=[]
    for _ in range(2):
        assert env.workflow.preflight_due()
        item=env.workflow.claim_due()
        assert item is not None
        claimed.append(item['job_id'])
    assert claimed==expected[:2]
    assert not any(request['@type']=='sendMessage' for request in env.binding.requests)


def test_local_preview_without_target_is_useful_but_cannot_authorize_or_connect(workflow_env):
    from threadrelay.contracts import validate_contract
    env=workflow_env;draft=configure(env,target=False)
    value=env.workflow.preview(draft['id'],draft['version'])
    validate_contract('DeliveryPreview',value)
    assert value['items'][0]['text']=='https://www.threads.net/@synthetic/post/Abc123'
    assert not value['approvable'] and 'destination_required' in value['blocked_reasons']
    with pytest.raises(ValueError):env.workflow.approve(draft['id'],draft['version'],value['preview_hash'])
    validate_contract('DeliveryWorkspace',env.workflow.workspace())
    env.workflow.tick()
    assert env.binding.created==0 and env.binding.requests==[]


def test_real_workflow_dispatch_uses_the_generated_closed_event_shape(workflow_env):
    from threadrelay.features import LocalFeatures
    from threadrelay.main import dispatch
    env=workflow_env;revision=env.archive.observe(post())
    value=dispatch({'version':1,'id':'draft','type':'ConfigureDeliveryDraft','payload':{
        'id':None,'expected_version':None,'revision_ids':[revision],'target':None,
        'template_id':'url','template_version':1,'timing':{'mode':'now','timezone':'Asia/Hong_Kong'}}},
        features=LocalFeatures(env.db,workflow=env.workflow))
    assert value['type']=='DeliveryDraft' and value['payload']['draft']['revision_ids']==[revision]


def test_exact_preview_uses_immutable_source_and_stale_template_or_draft_revokes_approval(workflow_env):
    from threadrelay.storage.posts import PostStore
    env=workflow_env
    template=env.workflow.save_template({'id':None,'expected_version':None,'name':'Own text','body':'{{text}}\n{{url}}'})
    draft=configure(env,template=template)
    preview=env.workflow.preview(draft['id'],draft['version'])
    PostStore(env.db).upsert(post(text='a later edit',observed_at='2026-11-02T00:00:00Z'))
    assert env.workflow.preview(draft['id'],draft['version'])==preview
    jobs=approve(env,draft)
    assert jobs[0]['text']=='Synthetic research\nhttps://www.threads.net/@synthetic/post/Abc123'
    assert jobs[0]['status']=='blocked' and 'native_delivery_unavailable' in jobs[0]['blocked_reasons']
    env.workflow.save_template({'id':template['id'],'expected_version':1,'name':'Changed','body':'{{url}}'})
    with pytest.raises(ValueError):env.workflow.approve(draft['id'],draft['version'],preview['preview_hash'])
    job=env.workflow.query_jobs()['items'][0]
    assert job['status']=='paused' and job['requires_approval']
    assert not any(request['@type']=='sendMessage' for request in env.binding.requests)


def test_random_choices_persist_and_reopen_pauses_overdue_without_dispatch(workflow_env):
    from threadrelay.delivery.workflow import DeliveryWorkflow
    env=workflow_env
    sources=[env.archive.observe(post(post_id=f'threads:Item{i}',permalink=f'https://www.threads.net/@synthetic/post/Item{i}')) for i in range(3)]
    draft=configure(env,sources=sources,timing={'mode':'random','timezone':'Asia/Hong_Kong','start_at':None,'minimum_seconds':300,'maximum_seconds':600})
    first=env.workflow.preview(draft['id'],draft['version']);second=env.workflow.preview(draft['id'],draft['version'])
    assert first==second and env.draws==[(300,600),(300,600)]
    approve(env,draft);env.workflow.close()
    reopened=DeliveryWorkflow(env.db,env.archive,auth=env.auth,destinations=env.verifier,sender=env.sender,native_gate=lambda:False,clock=env.clock,randint=lambda lo,hi:lo)
    jobs=reopened.query_jobs()['items']
    assert len(jobs)==3 and sum(job['status']=='paused' for job in jobs)==1
    paused=next(job for job in jobs if job['status']=='paused')
    assert paused['blocked_reasons']==['restart_requires_resume']
    resumed=reopened.resume(paused['id'],paused['version'])
    assert datetime.fromisoformat(resumed['due_at']).timestamp()>env.clock.wall
    reopened.tick();reopened.close()
    assert env.binding.requests==[]


def test_due_claim_uses_existing_sender_once_while_collection_is_stopped(workflow_env):
    env=workflow_env;make_ready(env)
    draft=configure(env);job=approve(env,draft)[0]
    assert env.session.snapshot['state']=='stopped'
    env.workflow.tick();env.workflow.tick()
    sends=[request for request in env.binding.requests if request['@type']=='sendMessage']
    assert len(sends)==1 and sends[0]['input_message_content']['text']['text']=='https://www.threads.net/@synthetic/post/Abc123'
    current=env.workflow.query_jobs()['items'][0]
    assert current['status']=='sending' and current['delivery_id'] and current['attempt_id']
    env.binding.events.extend([
        {'@client_id':42,'@type':'message','@extra':sends[0]['@extra'],'id':-1,'chat_id':123,'sender_id':{'@type':'messageSenderUser','user_id':123}},
        {'@client_id':42,'@type':'updateMessageSendSucceeded','old_message_id':-1,'message':{'id':100,'chat_id':123,'sender_id':{'@type':'messageSenderUser','user_id':123}}}])
    env.sender.poll();env.sender.poll();env.workflow.tick()
    sent=env.workflow.query_jobs()['items'][0]
    assert sent['status']=='sent' and sent['id']==job['id']
    with pytest.raises(ValueError):env.workflow.resume(sent['id'],sent['version'])


def test_unknown_native_outcome_is_terminal_and_never_retried(workflow_env):
    env=workflow_env;make_ready(env);env.binding.fail_send=True
    draft=configure(env);approve(env,draft)
    env.workflow.tick();env.workflow.tick()
    job=env.workflow.query_jobs()['items'][0]
    assert job['status']=='unknown'
    assert sum(request['@type']=='sendMessage' for request in env.binding.requests)==1
    with pytest.raises(ValueError):env.workflow.resume(job['id'],job['version'])


def test_pause_cancel_and_clock_jump_are_durable_gates_before_claim(workflow_env):
    env=workflow_env;make_ready(env)
    draft=configure(env,timing={'mode':'at_time','timezone':'Asia/Hong_Kong','at':'2026-11-02T00:00:00Z'})
    job=approve(env,draft)[0];paused=env.workflow.pause(job['id'],job['version'])
    env.clock.advance(86401);env.workflow.tick()
    assert not any(request['@type']=='sendMessage' for request in env.binding.requests)
    resumed=env.workflow.resume(paused['id'],paused['version'])
    env.clock.wall+=3600  # Wall jump without the corresponding monotonic time.
    env.workflow.tick();changed=env.workflow.query_jobs()['items'][0]
    assert changed['status']=='paused' and changed['blocked_reasons']==['clock_changed']
    assert env.client._network_enabled
    cancelled=env.workflow.cancel(changed['id'],changed['version'])
    assert cancelled['status']=='cancelled'
    with pytest.raises(ValueError):env.workflow.resume(cancelled['id'],cancelled['version'])
    assert resumed['id']==job['id']


def recorded_run(env,code,*,source=None):
    env.clock.advance(1)
    source=source or post(post_id=f'threads:{code}',permalink=f'https://www.threads.net/@synthetic/post/{code}',observed_at=env.clock.now().isoformat())
    revision=env.archive.observe(source);topics=env.config.topics();classifier=env.sender.router.classifier
    run=env.archive.start_run('collection',revision,{'post':source},topics,classifier.metadata)
    result=classifier.classify(source,topics)
    env.archive.finish_run(run,'succeeded',classification=result)
    return revision,run


def rule_draft(env,delivery_mode='review'):
    return configure(env,sources=[],timing={'mode':'new_matching','timezone':'Asia/Hong_Kong',
        'topic_ids':['science'],'interval_seconds':300,'delivery_mode':delivery_mode})


def test_rule_preview_discloses_exact_topic_version_and_destination(workflow_env):
    env=workflow_env;draft=rule_draft(env)
    value=env.workflow.preview(draft['id'],draft['version'])
    assert value['topic_bindings']==[{'id':'science','version':1,'description':'Synthetic research'}]
    assert value['destination']==env.destination


def test_new_arrival_rule_enrolls_only_future_new_posts_once_and_review_uses_exact_draft(workflow_env):
    env=workflow_env;old,old_run=recorded_run(env,'Old')
    draft=rule_draft(env);rule=approve(env,draft)[0]
    assert rule['kind']=='rule'
    assert env.workflow.on_history(old,old_run)==[]
    revision,run=recorded_run(env,'Future')
    created=env.workflow.on_history(revision,run)
    assert len(created)==1 and created[0]['requires_approval'] and created[0]['status']=='draft'
    assert created[0]['rule_id']==rule['id']
    generated=env.workflow.get_draft(created[0]['draft_id'])
    assert generated['revision_ids']==[revision]
    reviewed=approve(env,generated)[0]
    assert reviewed['id']==created[0]['id'] and not reviewed['requires_approval']
    assert env.workflow.on_history(revision,run)==[]
    updated,updated_run=recorded_run(env,'Old',source=post(post_id='threads:Old',permalink='https://www.threads.net/@synthetic/post/Old',text='changed old history'))
    assert env.workflow.on_history(updated,updated_run)==[]


def test_rule_pause_and_resume_does_not_backfill_posts_seen_while_paused(workflow_env):
    env=workflow_env;rule=approve(env,rule_draft(env))[0]
    paused=env.workflow.pause(rule['id'],rule['version'])
    old,run=recorded_run(env,'WhilePaused')
    assert env.workflow.on_history(old,run)==[]
    env.workflow.resume(paused['id'],paused['version'])
    assert env.workflow.on_history(old,run)==[]
    fresh,run=recorded_run(env,'AfterResume')
    assert len(env.workflow.on_history(fresh,run))==1


class SyntheticAutomatic:
    def __init__(self):self.basis={'fixture':'one'}
    def _basis(self,connection):
        if self.basis is None:raise ValueError('no synthetic evidence')
        return self.basis,{}, {},[]


def test_automatic_arrival_requires_scoped_current_evidence_but_not_per_source_click(workflow_env):
    env=workflow_env;make_ready(env);automatic=SyntheticAutomatic();env.workflow.automatic=automatic
    rule=approve(env,rule_draft(env,'automatic'))[0]
    revision,run=recorded_run(env,'Automatic')
    job=env.workflow.on_history(revision,run)[0]
    assert not job['requires_approval'] and job['rule_id']==rule['id']
    env.workflow.tick();env.workflow.tick()
    assert sum(request['@type']=='sendMessage' for request in env.binding.requests)==1
    later,later_run=recorded_run(env,'ChangedEvidence')
    pending=env.workflow.on_history(later,later_run)[0]
    automatic.basis={'fixture':'changed'}
    env.clock.advance(301);env.workflow.tick()
    changed=next(value for value in env.workflow.query_jobs()['items'] if value['id']==pending['id'])
    assert changed['status']=='blocked' and 'automatic_binding_changed' in changed['blocked_reasons']
    assert sum(request['@type']=='sendMessage' for request in env.binding.requests)==1


def test_missing_automatic_proof_never_becomes_a_future_send_permission(workflow_env):
    env=workflow_env;make_ready(env)
    env.workflow.automatic=SyntheticAutomatic();env.workflow.automatic.basis=None
    rule=approve(env,rule_draft(env,'automatic'))[0]
    assert rule['status']=='blocked' and 'automatic_evidence_unavailable' in rule['blocked_reasons']
    revision,run=recorded_run(env,'NoProof')
    job=env.workflow.on_history(revision,run)[0]
    env.workflow.automatic.basis={'fixture':'later'}
    env.workflow.tick()
    assert env.workflow.query_jobs()['items'][0]['id']==job['id']
    assert not any(request['@type']=='sendMessage' for request in env.binding.requests)


def test_permanent_sent_outcome_deduplicates_even_after_operational_ledger_expires(workflow_env):
    env=workflow_env;make_ready(env)
    draft=configure(env);job=approve(env,draft)[0];env.workflow.tick()
    from threadrelay.storage.outbox import Outbox
    delivery=Outbox(env.db).get(env.workflow.query_jobs()['items'][0]['delivery_id'])
    Outbox(env.db).transition(delivery['id'],'sending','sent',{'delivery_id':delivery['id'],'attempt_id':delivery['attempt_id'],
        'request_extra':delivery['request_extra'],'temporary_message_id':'-1','final_message_id':'100','state':'sent'})
    env.workflow.tick()
    with env.db.transaction() as con:con.execute('DELETE FROM deliveries WHERE id=?',(delivery['id'],))
    duplicate=approve(env,configure(env))[0]
    assert duplicate['status']=='blocked' and 'already_sent' in duplicate['blocked_reasons']
    env.workflow.tick()
    assert sum(request['@type']=='sendMessage' for request in env.binding.requests)==1 and duplicate['id']!=job['id']


def test_epoch_revocation_between_durable_claim_and_initiation_preserves_unknown_ledger(workflow_env):
    from threadrelay.storage.outbox import AccountStore,Outbox
    env=workflow_env;make_ready(env);approve(env,configure(env))
    original=env.workflow.start_claim
    def revoke_then_start(claim,initiate):
        AccountStore(env.db).invalidate_epoch('123')
        return original(claim,initiate)
    env.workflow.start_claim=revoke_then_start
    env.workflow.tick()
    assert not any(request['@type']=='sendMessage' for request in env.binding.requests)
    job=env.workflow.query_jobs()['items'][0]
    delivery=Outbox(env.db).get(job['delivery_id'])
    assert delivery['state']=='unknown'
    assert Outbox(env.db).attempts(delivery['id'])[0]['state']=='unknown'
    assert job['status']=='unknown'


def test_large_unicode_batch_has_a_clear_bounded_preview_error_without_truncation(workflow_env):
    from threadrelay.main import encode_frame
    env=workflow_env
    template=env.workflow.save_template({'id':None,'expected_version':None,'name':'Full captured text','body':'{{text}}'})
    sources=[env.archive.observe(post(post_id=f'threads:Large{i}',permalink=f'https://www.threads.net/@synthetic/post/Large{i}',text='😀'*2048)) for i in range(50)]
    draft=configure(env,sources=sources,template=template)
    value=env.workflow.preview(draft['id'],draft['version'])
    assert not value['approvable'] and value['blocked_reasons']==['preview_too_large']
    assert value['items']==[]
    assert len(encode_frame({'version':1,'id':'large','type':'DeliveryPreview','payload':value}))<900000


def test_manual_old_revision_survives_topic_edits_and_operational_text_retention(workflow_env):
    from test_routing import topic
    from threadrelay.storage.posts import PostStore
    env=workflow_env;make_ready(env)
    revision,run=recorded_run(env,'Historical')
    template=env.workflow.save_template({'id':None,'expected_version':None,'name':'Captured result','body':'{{text}}\n{{topic}}\n{{url}}'})
    draft=configure(env,sources=[revision],template=template);job=approve(env,draft)[0]
    PostStore(env.db).upsert(post(post_id='threads:Historical',permalink='https://www.threads.net/@synthetic/post/Historical',text='new mutable text',observed_at='2026-11-02T00:00:00Z'))
    env.config.save_topic(topic(version=2,description='Changed current topic'))
    with env.db.transaction() as con:con.execute("DELETE FROM post_text WHERE post_id='threads:Historical'")
    env.workflow.tick()
    sends=[request for request in env.binding.requests if request['@type']=='sendMessage']
    assert len(sends)==1 and sends[0]['input_message_content']['text']['text']=='Synthetic research\nSynthetic research\nhttps://www.threads.net/@synthetic/post/Historical'
    assert env.workflow.query_jobs()['items'][0]['id']==job['id'] and env.archive.get_run(run)['status']=='succeeded'


@pytest.mark.parametrize('changes',[{'visibility':'restricted'},{'visibility':'unknown'},{'quality_flags':[{'code':'truncated','detail':''}]}])
def test_archive_presence_never_overrides_source_ineligibility(workflow_env,changes):
    env=workflow_env;make_ready(env)
    draft=configure(env,sources=[env.archive.observe(post(**changes))])
    value=env.workflow.preview(draft['id'],draft['version'])
    assert not value['approvable'] and 'source_ineligible' in value['blocked_reasons']
    with pytest.raises(ValueError):env.workflow.approve(draft['id'],draft['version'],value['preview_hash'])
    env.workflow.tick()
    assert not any(request['@type']=='sendMessage' for request in env.binding.requests)


def test_global_stop_durably_pauses_future_jobs_and_rules_before_reconnect(workflow_env):
    env=workflow_env;make_ready(env)
    approve(env,configure(env,timing={'mode':'at_time','timezone':'Asia/Hong_Kong','at':'2026-11-02T00:00:00Z'}))
    approve(env,rule_draft(env))
    env.session.stop()
    jobs=env.workflow.query_jobs()['items']
    assert len(jobs)==2 and all(job['status']=='paused' and job['blocked_reasons']==['global_stop'] for job in jobs)
    env.clock.advance(86401);env.workflow.tick()
    assert not any(request['@type']=='sendMessage' for request in env.binding.requests)


def test_only_an_approved_due_job_can_refresh_its_current_target_on_the_owned_sender(workflow_env):
    env=workflow_env;make_ready(env);draft=configure(env);approve(env,draft)
    env.verifier.invalidate()
    before=len(env.binding.requests)
    env.workflow.workspace();env.workflow.query_jobs();env.workflow.preview(draft['id'],draft['version'])
    assert len(env.binding.requests)==before
    for _ in range(6):
        env.workflow.tick();env.sender.poll()
    assert sum(request['@type']=='createPrivateChat' for request in env.binding.requests)==1
    assert sum(request['@type']=='sendMessage' for request in env.binding.requests)==1


@pytest.mark.parametrize('connection',['ready','offline','reopened'])
def test_saved_workspace_target_remains_available_for_preparation_without_live_verification(workflow_env,connection):
    from dataclasses import replace
    from threadrelay.storage.saved_destinations import SavedDestinations
    env=workflow_env;make_ready(env)
    saved=SavedDestinations(env.db);saved.require_saved_selection()
    saved.save_verified(env.destination,0,'saved_messages')
    env.verifier.invalidate();env.gate['native']=False
    if connection=='offline':
        env.auth._snapshot=replace(env.auth.snapshot(),network_state='offline')
        env.client.disable_network()
    elif connection=='reopened':
        env.auth._snapshot=replace(env.auth.snapshot(),auth_state='unknown',network_state='offline')
        env.auth.status='idle'
        env.client.disable_network()
    before=len(env.binding.requests)
    workspace=env.workflow.workspace()
    assert [(item['target']['account_id'],item['target']['destination_id']) for item in workspace['targets']]==[('123','123')]
    draft=configure(env)
    preview=env.workflow.preview(draft['id'],draft['version'])
    assert {'native_delivery_unavailable','destination_unverified'}<=set(preview['blocked_reasons'])
    if connection!='ready':assert 'transport_offline' in preview['blocked_reasons']
    if connection=='reopened':assert 'account_not_ready' in preview['blocked_reasons']
    jobs=env.workflow.approve(draft['id'],draft['version'],preview['preview_hash'])['items']
    assert len(jobs)==1 and jobs[0]['status']=='blocked'
    assert set(jobs[0]['blocked_reasons'])==set(preview['blocked_reasons'])
    env.workflow.tick()
    assert len(env.binding.requests)==before
    assert not any(request['@type']=='sendMessage' for request in env.binding.requests)


@pytest.mark.parametrize('change',['unsaved','title_changed','epoch_revoked'])
def test_workspace_preparation_still_requires_exact_saved_active_destination(workflow_env,change):
    from threadrelay.storage.saved_destinations import SavedDestinations
    env=workflow_env;make_ready(env)
    saved=SavedDestinations(env.db);saved.require_saved_selection()
    if change!='unsaved':saved.save_verified(env.destination,0,'saved_messages')
    if change=='title_changed':env.config.save_destination({**env.destination,'title':'Changed chat'},account_epoch=0)
    elif change=='epoch_revoked':env.auth._accounts.invalidate_epoch('123')
    before=len(env.binding.requests)
    assert env.workflow.workspace()['targets']==[]
    assert len(env.binding.requests)==before


def test_failed_permission_refresh_never_retries_automatically(workflow_env):
    env=workflow_env;make_ready(env);approve(env,configure(env));env.verifier.invalidate();env.binding.paid=True
    for _ in range(8):env.workflow.tick();env.sender.poll()
    job=env.workflow.query_jobs()['items'][0]
    assert job['status']=='blocked' and 'destination_verification_unavailable' in job['blocked_reasons']
    assert sum(request['@type']=='createPrivateChat' for request in env.binding.requests)==1
    assert not any(request['@type']=='sendMessage' for request in env.binding.requests)


def test_selected_unknown_audience_requires_an_exact_manual_acknowledgement(workflow_env):
    env=workflow_env;make_ready(env)
    source=post(visibility='unknown',quality_flags=[{'code':'visibility_unverified','detail':'Rendered audience unavailable'}])
    revision=env.archive.observe(source);draft=configure(env,sources=[revision])
    old=env.workflow.preview(draft['id'],draft['version'])
    assert not old['approvable']
    reviewed=configure(env,sources=[revision],existing=draft,source_policy='manual_audience_reviewed')
    preview=env.workflow.preview(reviewed['id'],reviewed['version'])
    assert preview['approvable'] and preview['source_policy']=='manual_audience_reviewed'
    assert preview['items'][0]['source_eligible']
    assert env.archive.get_revision(revision)==source
    with pytest.raises(ValueError):env.workflow.approve(draft['id'],draft['version'],old['preview_hash'])
    approve(env,reviewed);env.workflow.tick()
    assert sum(request['@type']=='sendMessage' for request in env.binding.requests)==1


@pytest.mark.parametrize('source',[
    post(visibility='restricted',quality_flags=[{'code':'visibility_unverified','detail':''}]),
    post(visibility='unknown',quality_flags=[]),
    post(visibility='unknown',quality_flags=[{'code':'visibility_unverified','detail':''},{'code':'truncated','detail':''}]),
])
def test_manual_audience_acknowledgement_never_overrides_other_quality_restrictions(workflow_env,source):
    env=workflow_env;draft=configure(env,sources=[env.archive.observe(source)],source_policy='manual_audience_reviewed')
    preview=env.workflow.preview(draft['id'],draft['version'])
    assert not preview['approvable'] and 'source_ineligible' in preview['blocked_reasons']


def test_audience_acknowledgement_never_applies_to_a_new_arrival_rule(workflow_env):
    env=workflow_env
    with pytest.raises(ValueError):configure(env,sources=[],source_policy='manual_audience_reviewed',timing={
        'mode':'new_matching','timezone':'Asia/Hong_Kong','topic_ids':['science'],'interval_seconds':300,'delivery_mode':'automatic'})
    review=approve(env,rule_draft(env))[0]
    source=post(post_id='threads:Unverified',permalink='https://www.threads.net/@synthetic/post/Unverified',visibility='unknown',
        quality_flags=[{'code':'visibility_unverified','detail':''}])
    revision,run=recorded_run(env,'Unverified',source=source)
    generated=env.workflow.on_history(revision,run)
    assert len(generated)==1 and generated[0]['rule_id']==review['id'] and generated[0]['requires_approval']
    draft=env.workflow.get_draft(generated[0]['draft_id'])
    assert draft['source_policy']=='public_only'
    assert not env.workflow.preview(draft['id'],draft['version'])['approvable']


def test_changing_manual_audience_consent_revokes_the_queued_job_and_old_preview(workflow_env):
    env=workflow_env;make_ready(env)
    source=post(visibility='unknown',quality_flags=[{'code':'visibility_unverified','detail':''}])
    revision=env.archive.observe(source)
    draft=configure(env,sources=[revision],source_policy='manual_audience_reviewed')
    preview=env.workflow.preview(draft['id'],draft['version']);job=approve(env,draft)[0]
    changed=configure(env,sources=[revision],existing=draft,source_policy='public_only')
    with pytest.raises(ValueError):env.workflow.approve(draft['id'],draft['version'],preview['preview_hash'])
    env.workflow.tick();old=next(item for item in env.workflow.query_jobs()['items'] if item['id']==job['id'])
    assert old['status']=='cancelled' and old['requires_approval']
    assert not env.workflow.preview(changed['id'],changed['version'])['approvable']
    assert not any(request['@type']=='sendMessage' for request in env.binding.requests)


def test_automatic_rule_never_enrolls_an_unknown_audience_even_with_real_shape_consent(workflow_env):
    env=workflow_env;make_ready(env);env.workflow.automatic=SyntheticAutomatic()
    approve(env,rule_draft(env,'automatic'))
    source=post(post_id='threads:UnknownAuto',permalink='https://www.threads.net/@synthetic/post/UnknownAuto',visibility='unknown',
        quality_flags=[{'code':'visibility_unverified','detail':''}])
    revision,run=recorded_run(env,'UnknownAuto',source=source)
    assert env.workflow.on_history(revision,run)==[]
    env.workflow.tick()
    assert not any(request['@type']=='sendMessage' for request in env.binding.requests)


def test_two_simultaneous_due_claimers_create_only_one_durable_attempt(workflow_env):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    env=workflow_env;make_ready(env);approve(env,configure(env));barrier=Barrier(2)
    def claim():barrier.wait(timeout=2);return env.workflow.claim_due()
    with ThreadPoolExecutor(max_workers=2) as executor:
        claims=list(executor.map(lambda _:claim(),range(2)))
    assert sum(value is not None for value in claims)==1
    with env.db.connection() as con:
        assert con.execute('SELECT COUNT(*) FROM telegram_attempts').fetchone()[0]==1
    assert not any(request['@type']=='sendMessage' for request in env.binding.requests)


def test_recovered_durable_attempt_is_unknown_and_never_dispatches_again(workflow_env):
    from threadrelay.delivery.workflow import DeliveryWorkflow
    env=workflow_env;make_ready(env);approve(env,configure(env))
    claim=env.workflow.claim_due()
    assert claim is not None
    env.workflow.close();recovery=env.db.recover()
    assert recovery['counts']['unknown']==1
    reopened=DeliveryWorkflow(env.db,env.archive,auth=env.auth,destinations=env.verifier,sender=env.sender,
        native_gate=lambda:True,clock=env.clock)
    reopened.tick();job=reopened.query_jobs()['items'][0]
    assert job['status']=='unknown' and job['blocked_reasons']==['unknown_outcome']
    with pytest.raises(ValueError):reopened.resume(job['id'],job['version'])
    reopened.close()
    assert not any(request['@type']=='sendMessage' for request in env.binding.requests)


def test_application_reopen_keeps_plans_without_starting_an_account_or_sender(tmp_path):
    from threadrelay.application import Application
    from threadrelay.storage.outbox import AccountStore
    root=tmp_path/'app'
    with Application(root) as app:
        AccountStore(app.db).activate({'account_id':'123','epoch':0,'auth_state':'ready','display_name':None,'network_state':'offline','revocation_pending':False})
        version=app.config.save_destination({'account_id':'123','chat_id':'123','title':'Synthetic own chat','audience':'private',
            'permissions':{'can_send':True,'can_select_sender':False},'sender':{'kind':'user','id':'123'},'paid_required':False},account_epoch=0)
        from threadrelay.storage.saved_destinations import SavedDestinations
        SavedDestinations(app.db).save_verified({'account_id':'123','chat_id':'123','title':'Synthetic own chat','audience':'private',
            'permissions':{'can_send':True,'can_select_sender':False},'sender':{'kind':'user','id':'123'},'paid_required':False},0,'saved_messages')
        revision=app.archive.observe(post())
        draft=app.workflow.configure({'id':None,'expected_version':None,'revision_ids':[revision],
            'target':{'account_id':'123','account_epoch':0,'destination_id':'123','destination_version':version,'sender':{'kind':'user','id':'123'}},
            'template_id':'url','template_version':1,'timing':{'mode':'now','timezone':'Asia/Hong_Kong'}})
        preview=app.workflow.preview(draft['id'],draft['version'])
        identifier=app.workflow.approve(draft['id'],draft['version'],preview['preview_hash'])['items'][0]['id']
    with Application(root) as reopened:
        job=reopened.workflow.query_jobs()['items'][0]
        assert job['id']==identifier and job['status']=='paused'
        assert reopened.archive.get_revision(revision)['post_id']=='threads:Abc123'
        assert reopened.telegram._client is None


def hide_collection(env):
    env.session._setup_check=lambda _: {'allowed':True,'reason':None}
    env.session.start({'feed_url':'https://www.threads.com/','mode':'review'})
    env.session.pause('view_hidden')


def test_collection_hide_preserves_a_live_clean_auth_transport_without_reconnecting(workflow_env):
    from test_telegram_auth import state
    from threadrelay.delivery.telegram import NetworkPermit
    env=workflow_env;env.auth.begin('vault:fixture',pending=False)
    env.binding.events.append(state('WaitTdlibParameters'))
    while env.binding.events:env.auth.poll()
    env.auth.enable_clean_auth(NetworkPermit('clean_auth',0,False))
    env.binding.events.append(state('WaitPhoneNumber'))
    while env.binding.events:env.auth.poll()
    challenge=env.auth.challenge();before=len(env.binding.requests)
    hide_collection(env)
    assert env.client._network_enabled and env.auth.challenge()==challenge
    assert not any(request.get('type',{}).get('@type')=='networkTypeNone' for request in env.binding.requests[before:])
    assert sum(request.get('type',{}).get('@type')=='networkTypeOther' for request in env.binding.requests)==1


def test_collection_hide_preserves_only_current_approved_workflow_inflight(workflow_env):
    env=workflow_env;make_ready(env);approve(env,configure(env));env.workflow.tick()
    assert env.workflow.query_jobs()['items'][0]['status']=='sending'
    hide_collection(env)
    assert env.client._network_enabled and env.workflow.query_jobs()['items'][0]['status']=='sending'
    assert sum(request['@type']=='sendMessage' for request in env.binding.requests)==1


def test_collection_hide_keeps_login_when_inflight_template_changes(workflow_env):
    env=workflow_env;make_ready(env)
    template=env.workflow.save_template({'id':None,'expected_version':None,'name':'Pinned text','body':'{{url}}'})
    approve(env,configure(env,template=template));env.workflow.tick()
    env.workflow.save_template({'id':template['id'],'expected_version':1,'name':'Changed','body':'{{author}} {{url}}'})
    hide_collection(env)
    assert env.client._network_enabled
    assert sum(request['@type']=='sendMessage' for request in env.binding.requests)==1


def test_collection_hide_discards_destination_work_without_disconnecting_account(workflow_env):
    env=workflow_env;make_ready(env);env.sender._destination_work=('legacy',env.clock.elapsed+30)
    hide_collection(env)
    assert env.client._network_enabled and env.sender._destination_work is None


@pytest.mark.parametrize('state',['sending','retryable','unknown'])
def test_collection_hide_preserves_login_but_never_grants_legacy_or_ambiguous_attempts(workflow_env,state):
    from threadrelay.storage.database import encode
    env=workflow_env;make_ready(env)
    payload={'id':'legacy','post_id':'threads:Legacy','destination_id':'123','content_hash':'a'*64,
        'account_id':'123','account_epoch':0,'sender':{'kind':'user','id':'123'},
        'route_refs':[{'route_id':'legacy','route_version':1,'approval_id':'legacy'}],'state':state,
        'attempt_id':'legacy-attempt','request_extra':'legacy-extra','message_id':None,'temporary_message_id':None,'final_message_id':None}
    with env.db.transaction() as con:
        con.execute('INSERT INTO posts VALUES (?,?,?,?)',('threads:Legacy','a'*64,env.clock.wall,env.clock.wall))
        con.execute('INSERT INTO deliveries VALUES (?,?,?,?,?,?,?,?,NULL,?,?)',('legacy','123',0,'threads:Legacy','123','a'*64,encode(payload),state,env.clock.wall,env.clock.wall))
    hide_collection(env)
    assert env.client._network_enabled
    assert env.sender.start_next('blocked') is None
    assert not any(request['@type']=='sendMessage' for request in env.binding.requests)


def test_explicit_pause_preserves_login_and_durably_pauses_history_jobs(workflow_env):
    env=workflow_env;make_ready(env)
    approve(env,configure(env,timing={'mode':'at_time','timezone':'Asia/Hong_Kong','at':'2026-11-02T00:00:00Z'}))
    env.session._setup_check=lambda _: {'allowed':True,'reason':None}
    env.session.start({'feed_url':'https://www.threads.com/','mode':'review'});env.session.pause()
    job=env.workflow.query_jobs()['items'][0]
    assert env.client._network_enabled and job['status']=='paused' and job['blocked_reasons']==['global_pause']


def test_suspend_collection_command_has_a_state_reply_and_never_calls_global_stop(workflow_env):
    from threadrelay.features import LocalFeatures
    from threadrelay.main import dispatch
    calls=[]
    coordinator=SimpleNamespace(suspend_collection=lambda:(calls.append('suspend') or {'state':'paused','counters':{'observed':0,'matched':0,'queued':0,'sent':0},'attention_reason':'view_hidden'}))
    event=dispatch({'version':1,'id':'hide','type':'SuspendCollection','payload':{}},coordinator=coordinator,features=LocalFeatures(workflow_env.db))
    assert event['type']=='State' and event['payload']['attention_reason']=='view_hidden'
    assert calls==['suspend']


@pytest.mark.parametrize('reason',['duration_limit','post_limit','automatic_delivery_limit'])
def test_natural_collection_completion_preserves_the_independent_approved_queue(workflow_env,reason):
    env=workflow_env;make_ready(env)
    future=datetime.fromtimestamp(env.clock.wall+300,timezone.utc).isoformat()
    approve(env,configure(env,timing={'mode':'at_time','timezone':'Asia/Hong_Kong','at':future}))
    env.session._setup_check=lambda _: {'allowed':True,'reason':None}
    env.session.start({'feed_url':'https://www.threads.com/','mode':'review'})
    env.session.pause(reason)
    assert env.client._network_enabled and env.workflow.query_jobs()['items'][0]['status']=='waiting'
    env.clock.advance(301);env.workflow.tick()
    assert sum(request['@type']=='sendMessage' for request in env.binding.requests)==1
    assert sum(request.get('type',{}).get('@type')=='networkTypeOther' for request in env.binding.requests)==1


def test_global_pause_after_collection_only_pause_pauses_queue_and_preserves_auth(workflow_env):
    env=workflow_env;make_ready(env)
    approve(env,configure(env,timing={'mode':'at_time','timezone':'Asia/Hong_Kong','at':'2026-11-02T00:00:00Z'}))
    hide_collection(env)
    assert env.client._network_enabled
    env.session.pause()
    assert env.client._network_enabled
    assert env.workflow.query_jobs()['items'][0]['status']=='paused'


def test_blocked_old_jobs_do_not_starve_a_later_eligible_due_job(workflow_env):
    env=workflow_env;make_ready(env)
    for index in range(16):
        revision=env.archive.observe(post(post_id=f'threads:Blocked{index}',permalink=f'https://www.threads.net/@synthetic/post/Blocked{index}'))
        approve(env,configure(env,sources=[revision]))
    destination={**env.destination,'title':'New verified title'}
    version=env.config.save_destination(destination,account_epoch=0)
    env.target={**env.target,'destination_version':version};env.destination=destination;env.verifier.evidence=(0,version,destination)
    source=env.archive.observe(post(post_id='threads:Eligible',permalink='https://www.threads.net/@synthetic/post/Eligible'))
    wanted=approve(env,configure(env,sources=[source]))[0]
    for _ in range(4):env.workflow.tick()
    job=next(item for item in env.workflow.query_jobs()['items'] if item['id']==wanted['id'])
    assert job['status']=='sending'
    assert sum(request['@type']=='sendMessage' for request in env.binding.requests)==1


@pytest.mark.parametrize('visibility',['unknown','restricted'])
def test_captured_source_saves_exact_plan_without_audience_agreement(workflow_env,visibility):
    env=workflow_env;make_ready(env)
    source=post(visibility=visibility,quality_flags=[{'code':'visibility_unverified','detail':'Captured audience label unavailable'}])
    revision=env.archive.observe(source)
    draft=configure(env,sources=[revision],source_policy='captured')
    preview=env.workflow.preview(draft['id'],draft['version'])
    assert preview['approvable'] and preview['source_policy']=='captured'
    assert preview['items'][0]['text']==source['permalink']
    assert env.archive.get_revision(revision)==source
    approve(env,draft)
    env.workflow.tick()
    assert sum(request['@type']=='sendMessage' for request in env.binding.requests)==1
