"""Score-policy routes through real SQLite and the fake native C boundary.

These are synthetic source/transport tests, never native acceptance or measured
classification accuracy evidence.
"""
import pytest
from test_owner_history import manual as manual_fixture, drain

from test_routing import post
from threadrelay.delivery.automatic_queue import AutomaticQueue
from threadrelay.delivery.owner_automatic import OwnerAutomaticController
from threadrelay.features import LocalFeatures
from threadrelay.routing.automatic import AutomaticPolicy
from threadrelay.storage.posts import PostStore
from threadrelay.storage.outbox import Outbox

manual=manual_fixture


@pytest.fixture
def auto(manual):
    env=manual
    env.session._setup_check=lambda _: {'allowed':True,'reason':None}
    env.session.start({'feed_url':'https://www.threads.com/','mode':'auto','max_auto_deliveries':10})
    automatic=AutomaticPolicy(env.db,env.sender.router.classifier)
    automatic.save({'confirmed':True,'method':'topic_score'})
    router=env.sender.router
    router.auto_send_authorized=automatic.authorized;router.auto_route_authorized=automatic.route_authorized;router.auto_source_authorized=automatic.source_authorized
    features=LocalFeatures(env.db,router=router,automatic=automatic)
    route={'id':'score','version':1,'topic_ids':['science'],'mode':'auto','enabled':True,
        'account_id':'123','account_epoch':0,'destination_id':'-20','sender':{'kind':'user','id':'123'}}
    features.save_route(route,expected_auto_receipt=features.preview_auto_route(route)['receipt'])
    queue=AutomaticQueue(router,automatic,env.sender,env.clock)
    controller=OwnerAutomaticController(env.client,env.auth,env.session,queue,native_gate=lambda:env.gate[0],monotonic=env.clock.monotonic)
    env.session.add_gate_close_hook(controller.revoke)
    env.automatic,env.auto_queue,env.auto_controller,env.route=automatic,queue,controller,route
    env.client._client._owner_automatic=controller
    yield env


def enqueue(env,suffix='Auto',**source_changes):
    source=post(post_id='threads:'+suffix,permalink='https://www.threads.net/@synthetic/post/'+suffix,**source_changes)
    env.archive.observe(source);PostStore(env.db).upsert(source)
    router=env.sender.router
    proposal=router.plan(source,router.classifier.classify_bound(source,env.config.topics()),session_mode='auto')[0]
    assert proposal['approval_required'] is False
    return router.stage(proposal)


def progress(env):
    for _ in range(30):
        drain(env);env.auto_controller.tick()
        if env.auto_controller._state in {'sending','unknown','failed'}:return
    raise AssertionError(env.auto_controller._state)


def send_requests(env):return [request for request in env.library.calls if request['@type']=='sendMessage']


def succeed(env,final=100):
    controller=env.auto_controller;binding=controller._marker_binding
    def message(id,pending=False):
        value={'@type':'message','id':id,'chat_id':-20,'sender_id':{'@type':'messageSenderUser','user_id':123},
            'is_outgoing':True,'content':{'@type':'messageText','text':{'@type':'formattedText','text':controller._plan['text'],'entities':[]}}}
        if pending:value['sending_state']={'@type':'messageSendingStatePending'}
        return value
    env.library.events.append({'@client_id':env.library.client_id,'@extra':binding['request_extra'],**message(-final,True)})
    env.library.events.append({'@client_id':env.library.client_id,'@type':'updateMessageSendSucceeded','old_message_id':-final,'message':message(final)})
    drain(env)


def test_two_automatic_intents_share_authenticated_client_and_have_real_immutable_results(auto):
    first=enqueue(auto);progress(auto)
    assert auto.auto_controller._state=='sending'
    assert len(send_requests(auto))==1
    assert auto.client._profile_guard.purpose=='automatic_pending'
    succeed(auto)
    assert auto.auto_controller._state=='sent'
    assert Outbox(auto.db).get(first)['state']=='sent'
    assert auto.archive.get_delivery(first)['delivery']['final_message_id']=='100'
    assert auto.client._profile_guard.purpose=='automatic_sent'
    assert auto.auth.status=='ready' and auto.client.transport_ready
    second=enqueue(auto,'AutoSecond');progress(auto);succeed(auto,101)
    assert Outbox(auto.db).get(second)['state']=='sent'
    assert len(send_requests(auto))==2 and auto.library.client_id==1


@pytest.mark.parametrize('change',['pause','policy','route','topic','source','account','permission','paid','sender'])
def test_changed_authority_before_native_cannot_dispatch(auto,change):
    delivery=enqueue(auto)
    auto.auto_controller.tick()
    if change=='pause':auto.session.pause()
    if change=='policy':auto.automatic.save({'confirmed':True,'method':'topic_score'})
    if change=='route':auto.config.save_route({**auto.route,'version':2,'enabled':False})
    if change=='topic':auto.config.save_topic({**auto.config.topics()[0],'version':2,'minimum_score':.8})
    if change=='source':
        source=post(text='Changed retained body');PostStore(auto.db).upsert(source)
        # Exact same identity as staged source, changed content.
        staged=post(post_id='threads:Auto',permalink='https://www.threads.net/@synthetic/post/Auto',text='Changed')
        PostStore(auto.db).upsert(staged)
    if change=='account':auto.accounts.invalidate_epoch('123')
    if change=='permission':auto.library.member_changes={'status':{'@type':'chatMemberStatusLeft'}}
    if change=='paid':auto.library.supergroup=True;auto.library.group_id=20;auto.library.chat_changes={'type':{'@type':'chatTypeSupergroup','supergroup_id':20,'is_channel':True}}
    if change=='sender':auto.library.chat_changes={'message_sender_id':{'@type':'messageSenderChat','chat_id':-33}}
    progress(auto)
    assert not send_requests(auto)
    assert Outbox(auto.db).get(delivery)['state'] in {'pending','cancelled'}


def test_unknown_native_result_quarantines_profile_and_never_retries(auto):
    delivery=enqueue(auto);auto.library.fail_send=True;progress(auto)
    assert auto.auto_controller._state=='unknown'
    assert Outbox(auto.db).get(delivery)['state']=='unknown'
    assert auto.client._profile_guard.purpose=='automatic_unknown'
    assert not auto.auto_queue.profile_ready(auto.client._profile_guard.profile_id)
    for _ in range(4):auto.auto_controller.tick()
    assert len(send_requests(auto))==1
    assert auto.auth.status=='ready' and auto.client.transport_ready
    assert not any(request['@type'] in {'close','logOut'} for request in auto.library.calls)


def test_automatic_metadata_failure_preserves_authenticated_client(auto):
    enqueue(auto);auto.library.chat_changes={'title':'Changed group'}
    progress(auto)
    assert auto.auto_controller._state=='failed'
    assert not send_requests(auto)
    assert auto.auth.status=='ready' and auto.client.transport_ready and auto.library.client_id==1
    assert auto.client._profile_guard.purpose=='connection_only'
    assert not any(request['@type'] in {'close','logOut'} for request in auto.library.calls)


@pytest.mark.parametrize('native_state',['connectionStateConnecting','connectionStateWaitingForNetwork'])
def test_reconnect_during_pristine_preflight_retries_fresh_metadata_before_one_send(auto,native_state):
    configure_route_queue(auto)
    queued=enqueue(auto,'ReconnectPreflight')
    owner=auto.auto_controller
    owner.tick()
    auto.library.events.insert(0,{'@client_id':auto.library.client_id,'@type':'updateConnectionState',
        'state':{'@type':native_state}})
    while auto.library.events:auto.auth.poll()
    auto.auth.poll();owner.tick()
    assert not send_requests(auto)
    assert owner._state=='verifying'
    assert auto.auto_queue.schedule.get(queued)['reason'] is None
    assert Outbox(auto.db).get(queued)['attempt_id'] is None
    before=len(auto.library.calls)
    for _ in range(10):owner.tick()
    assert len(auto.library.calls)==before
    auto.library.events.append({'@client_id':auto.library.client_id,'@type':'updateConnectionState',
        'state':{'@type':'connectionStateReady'}})
    while auto.library.events:auto.auth.poll()
    auto.auth.poll();auto.clock.advance(1)
    progress(auto)
    assert owner._state=='sending' and len(send_requests(auto))==1


def test_metadata_invalidation_restarts_only_bounded_pristine_preflight(auto):
    configure_route_queue(auto)
    queued=enqueue(auto,'MetadataRefresh')
    owner=auto.auto_controller;owner.tick()
    drain(auto);owner.tick()  # start fresh destination verification
    auto.library.events.insert(0,{'@client_id':auto.library.client_id,'@type':'updateBasicGroup','basic_group':{}})
    drain(auto);owner.tick()
    assert owner._state=='verifying' and not send_requests(auto)
    assert Outbox(auto.db).get(queued)['attempt_id'] is None
    auto.clock.advance(1);progress(auto)
    assert owner._state=='sending' and len(send_requests(auto))==1


def test_ready_connection_update_during_member_verification_restarts_fresh_checks(auto):
    configure_route_queue(auto)
    queued=enqueue(auto,'ReadyUpdateDuringMember')
    owner=auto.auto_controller;owner.tick();drain(auto);owner.tick()
    # Deliver getChat and group metadata, retaining the correlated getChatMember
    # reply in the native queue. Even an already-Ready update invalidates it.
    while auto.verifier.pending['stage']!='member':auto.auth.poll()
    auto.library.events.insert(0,{'@client_id':auto.library.client_id,'@type':'updateConnectionState',
        'state':{'@type':'connectionStateReady'}})
    drain(auto);owner.tick()
    assert owner._state=='verifying' and not send_requests(auto)
    assert auto.auto_queue.schedule.get(queued)['reason'] is None
    assert Outbox(auto.db).get(queued)['attempt_id'] is None
    auto.clock.advance(1);progress(auto)
    assert owner._state=='sending' and len(send_requests(auto))==1


@pytest.mark.parametrize('stage',['chat','member'])
@pytest.mark.parametrize('code',[500,408])
def test_correlated_metadata_error_allows_explicit_retry_without_any_prior_attempt(auto,stage,code):
    configure_route_queue(auto)
    queued=enqueue(auto,'MetadataRequestError')
    owner=auto.auto_controller;owner.tick();drain(auto);owner.tick()
    while auto.verifier.pending['stage']!=stage:auto.auth.poll()
    extra=auto.verifier.pending['extra']
    auto.library.events.clear()
    auto.library.events.append({'@client_id':auto.library.client_id,'@extra':extra,
        '@type':'error','code':code,'message':'Synthetic metadata unavailable'})
    drain(auto);owner.tick()
    assert owner._state=='failed' and not send_requests(auto)
    assert auto.auto_queue.schedule.get(queued)['reason']=='preflight_unavailable'
    assert Outbox(auto.db).get(queued)['attempt_id'] is None
    auto.auto_queue.schedule.pause('score',False)  # explicit Retry checks
    assert auto.auto_queue.schedule.get(queued)['reason'] is None
    for _ in range(30):
        drain(auto);owner.tick()
        if owner._state=='sending':break
    assert owner._state=='sending' and len(send_requests(auto))==1


def test_missing_verification_ownership_restarts_fresh_checks_before_one_send(auto):
    configure_route_queue(auto)
    queued=enqueue(auto,'MissingVerification')
    owner=auto.auto_controller;owner.tick();drain(auto);owner.tick()
    auto.verifier.invalidate();owner._verification=None
    owner.tick()
    assert owner._state=='verifying' and not send_requests(auto)
    assert auto.auto_queue.schedule.get(queued)['reason'] is None
    assert Outbox(auto.db).get(queued)['attempt_id'] is None
    auto.clock.advance(1);progress(auto)
    assert owner._state=='sending' and len(send_requests(auto))==1


def test_repeated_missing_verification_ownership_exhausts_bounded_retry_without_send(auto):
    configure_route_queue(auto)
    queued=enqueue(auto,'RepeatedMissingVerification')
    owner=auto.auto_controller;owner.tick()
    for delay in (1,2,3):
        drain(auto);owner.tick()
        auto.verifier.invalidate();owner._verification=None;owner.tick()
        assert owner._state=='verifying' and not send_requests(auto)
        auto.clock.advance(delay);owner.tick()
    drain(auto);owner.tick()
    auto.verifier.invalidate();owner._verification=None;owner.tick()
    assert owner._state=='failed' and not send_requests(auto)
    assert auto.auto_queue.schedule.get(queued)['reason']=='preflight_retry_exhausted'
    assert Outbox(auto.db).get(queued)['attempt_id'] is None


def test_generic_preflight_error_records_only_static_stage_class_and_frame(auto,monkeypatch):
    configure_route_queue(auto)
    queued=enqueue(auto,'DiagnosticPrivacy')
    owner=auto.auto_controller;owner.tick();drain(auto)
    def fail(*args,**kwargs):raise AttributeError('PRIVATE credentials, metadata and raw identities')
    monkeypatch.setattr(auto.verifier,'begin_group',fail)
    owner.tick()
    assert owner._state=='failed' and not send_requests(auto)
    assert auto.auto_queue.schedule.get(queued)['reason']=='verification_failed'
    with auto.db.connection() as con:codes=[r[0] for r in con.execute('SELECT code FROM diagnostics')]
    assert 'auto_preflight_stage_verifier_begin' in codes
    assert 'auto_preflight_error_attribute' in codes
    assert 'auto_preflight_frame_owner_tick' in codes
    assert all('PRIVATE' not in code and 'credentials' not in code for code in codes)


def test_basic_group_preparation_error_has_exact_safe_stage_diagnostic(auto,monkeypatch):
    import json
    configure_route_queue(auto)
    queued=enqueue(auto,'ColdBasicGroup')
    original=auto.library.td_send.call
    def native_send(client_id,data):
        request=json.loads(data)
        if request['@type']=='getBasicGroup' and not auto.library.info_ready:
            auto.library.events.append({'@client_id':client_id,'@extra':request['@extra'],
                '@type':'error','code':400,'message':'Group not found'})
        else:original(client_id,data)
    monkeypatch.setattr(auto.library.td_send,'call',native_send)
    auto.library.info_ready=False;auto.library.dialog_ready=False
    progress(auto)
    assert auto.auto_controller._state=='failed' and not send_requests(auto)
    assert auto.auto_queue.schedule.get(queued)['reason']=='preflight_unavailable'
    assert Outbox(auto.db).get(queued)['attempt_id'] is None
    with auto.db.connection() as con:codes=[r[0] for r in con.execute('SELECT code FROM diagnostics')]
    assert 'auto_preflight_prepare_basic_error' in codes
    assert 'auto_preflight_native_error_400' in codes


def test_permanent_destination_mismatch_has_exact_reason_without_metadata_retry(auto):
    configure_route_queue(auto)
    queued=enqueue(auto,'DifferentNativeTitle')
    auto.library.chat_changes={'title':'Different group'}
    progress(auto)
    assert auto.auto_controller._state=='failed'
    assert auto.auto_queue.schedule.get(queued)['reason']=='destination_title_changed'
    assert not send_requests(auto)
    before=len(auto.library.calls);auto.clock.advance(60)
    for _ in range(5):auto.auto_controller.tick()
    assert len(auto.library.calls)==before


def test_unproven_personal_sender_stays_blocked_with_exact_diagnostic(auto):
    configure_route_queue(auto)
    queued=enqueue(auto,'UnprovenSender')
    auto.library.member_changes={'status':{'@type':'chatMemberStatusAdministrator','rights':{'is_anonymous':True}}}
    progress(auto)
    assert auto.auto_controller._state=='failed'
    assert auto.auto_queue.schedule.get(queued)['reason']=='destination_sender_unverified'
    assert Outbox(auto.db).get(queued)['attempt_id'] is None and not send_requests(auto)


def test_preflight_reconnect_timeout_leaves_pristine_item_blocked_without_send(auto):
    configure_route_queue(auto)
    queued=enqueue(auto,'ReconnectTimeout')
    owner=auto.auto_controller;owner.tick()
    auto.library.events.insert(0,{'@client_id':auto.library.client_id,'@type':'updateConnectionState',
        'state':{'@type':'connectionStateConnecting'}})
    while auto.library.events:auto.auth.poll()
    auto.auth.poll();owner.tick();auto.clock.advance(31);owner.tick()
    assert owner._state=='failed'
    assert auto.auto_queue.schedule.get(queued)['reason']=='timed_out'
    assert Outbox(auto.db).get(queued)['attempt_id'] is None
    assert not send_requests(auto)


def test_repeated_native_metadata_invalidation_has_a_fixed_retry_budget(auto):
    configure_route_queue(auto)
    queued=enqueue(auto,'RepeatedRefresh')
    owner=auto.auto_controller;owner.tick()
    for delay in (1,2,3):
        drain(auto);owner.tick()
        auto.library.events.insert(0,{'@client_id':auto.library.client_id,'@type':'updateBasicGroup','basic_group':{}})
        drain(auto);owner.tick()
        assert owner._state=='verifying' and not send_requests(auto)
        before=len(auto.library.calls)
        for _ in range(5):owner.tick()
        assert len(auto.library.calls)==before
        auto.clock.advance(delay);owner.tick()
    drain(auto);owner.tick()
    auto.library.events.insert(0,{'@client_id':auto.library.client_id,'@type':'updateBasicGroup','basic_group':{}})
    drain(auto);owner.tick()
    assert owner._state=='failed'
    assert auto.auto_queue.schedule.get(queued)['reason']=='preflight_retry_exhausted'
    assert Outbox(auto.db).get(queued)['attempt_id'] is None and not send_requests(auto)


def test_pause_during_preflight_retry_never_resumes_the_session_or_sends(auto):
    configure_route_queue(auto)
    queued=enqueue(auto,'PauseDuringRefresh')
    owner=auto.auto_controller;owner.tick();drain(auto);owner.tick()
    auto.library.events.insert(0,{'@client_id':auto.library.client_id,'@type':'updateBasicGroup','basic_group':{}})
    drain(auto);owner.tick();auto.session.pause();auto.clock.advance(10)
    for _ in range(5):owner.tick()
    assert auto.session.snapshot['state']=='paused'
    assert owner._state=='failed' and not send_requests(auto)
    assert Outbox(auto.db).get(queued)['attempt_id'] is None


def test_hard_auto_budget_allows_exact_last_terminal_but_never_another_start(auto):
    auto.session.stop();auto.session.start({'feed_url':'https://www.threads.com/','mode':'auto','max_auto_deliveries':1})
    assert auto.auth.status=='ready' and auto.client.transport_ready
    first=enqueue(auto);progress(auto)
    assert len(send_requests(auto))==1
    assert auto.session.snapshot['attention_reason']=='automatic_delivery_limit'
    succeed(auto)
    assert Outbox(auto.db).get(first)['state']=='sent'
    assert auto.auto_controller._state=='sent'
    for _ in range(3):auto.auto_controller.tick()
    assert len(send_requests(auto))==1


def test_cross_lane_incomplete_manual_receipt_blocks_auto_without_native(auto):
    delivery=enqueue(auto)
    profile_id=auto.client._profile_guard.profile_id
    with auto.db.transaction() as con:
        con.execute('INSERT INTO local_policy(name,payload) VALUES (?,?)',('manual-history-incomplete:'+profile_id,'{}'))
    assert not auto.auto_queue.profile_ready(profile_id)
    auto.auto_controller.tick()
    assert not send_requests(auto)
    assert Outbox(auto.db).get(delivery)['state']=='pending'


@pytest.mark.parametrize('boundary',['profile_complete','db_complete','finish','profile_finalize'])
def test_uncertain_durable_completion_retains_deny_latch(auto,monkeypatch,boundary):
    delivery=enqueue(auto);progress(auto)
    owner=auto.auto_controller
    def fail(*args,**kwargs):raise OSError('synthetic durability failure')
    if boundary=='profile_complete':monkeypatch.setattr(owner._profile,'complete_automatic_sent',fail)
    if boundary=='db_complete':monkeypatch.setattr(auto.auto_queue,'complete',fail)
    if boundary=='finish':
        original=auto.auto_queue.finish
        def committed(*args):original(*args);raise OSError('lost committed return')
        monkeypatch.setattr(auto.auto_queue,'finish',committed)
    if boundary=='profile_finalize':monkeypatch.setattr(owner._profile,'finalize_automatic_sent',fail)
    succeed(auto)
    assert owner._state=='unknown'
    assert not auto.auto_queue.profile_ready(owner._profile.profile_id)
    assert len(send_requests(auto))==1
    assert auto.archive.get_delivery(delivery)['delivery']['state'] in {'sent','unknown'}


def test_bounded_scan_advances_past_stale_pending_to_current_auto_intent(auto):
    for index in range(100):enqueue(auto,'Old'+str(index))
    auto.config.save_topic({**auto.config.topics()[0],'version':2,'description':'Current synthetic topic'})
    features=LocalFeatures(auto.db,router=auto.sender.router,automatic=auto.automatic)
    features.save_route(auto.route,expected_auto_receipt=features.preview_auto_route(auto.route)['receipt'])
    # Use increasing wall time so the new receipt is after the stale batch.
    auto.clock.wall+=1
    delivery=enqueue(auto,'CurrentAuto')
    assert auto.auto_queue.due() is None
    selected=auto.auto_queue.due()
    assert selected is not None and selected['job_id']==delivery
    auto.auto_controller.begin(selected);progress(auto)
    assert len(send_requests(auto))==1


def test_unknown_audience_link_requires_exact_score_route_choice_and_remains_unknown(auto):
    route={**auto.route,'version':2,'allow_unverified_source':True}
    features=LocalFeatures(auto.db,router=auto.sender.router,automatic=auto.automatic)
    features.save_route(route,expected_auto_receipt=features.preview_auto_route(route)['receipt'])
    delivery=enqueue(auto,'UnknownAudience',visibility='unknown',quality_flags=[{'code':'visibility_unverified','detail':'Source audience is not visible'}])
    progress(auto);succeed(auto)
    request=send_requests(auto)[0]
    assert request['input_message_content']['text']['text']=='https://www.threads.net/@synthetic/post/UnknownAudience'
    assert Outbox(auto.db).get(delivery)['state']=='sent'
    assert PostStore(auto.db).get('threads:UnknownAudience')['visibility']=='unknown'


def test_inflight_profile_cannot_be_reopened_after_restart(auto):
    from threadrelay.delivery.connection_profile import connection_only_profile
    enqueue(auto);progress(auto)
    profile=auto.client._profile_guard
    profile.close()
    with pytest.raises(ValueError):connection_only_profile(profile.path,'vault:fixture',b'k'*32)
    replacement=AutomaticQueue(auto.sender.router,auto.automatic,auto.sender,auto.clock)
    assert not replacement.profile_ready(profile.profile_id)
    assert len(send_requests(auto))==1


def configure_route_queue(env, **changes):
    env.automatic.route_driven = True
    env.session._config['route_ids'] = ['score']
    env.session._snapshot['config']['route_ids'] = ['score']
    env.sender.router.selected_route_ids = lambda: env.session.snapshot['config'].get('route_ids', [])
    with env.db.transaction() as con:
        con.execute("DELETE FROM local_policy WHERE name='automatic' OR name LIKE 'auto-route:%'")
    env.route = {**env.route, 'version':2, 'message_template':'{{author}}\n{{text}}\n{{url}}', 'queue':{'mode':'interval','interval_seconds':60}, **changes}
    LocalFeatures(env.db, router=env.sender.router, automatic=env.automatic).save_route(env.route)


def test_selected_route_queue_no_policy_custom_text_and_interval(auto):
    configure_route_queue(auto)
    # An unrelated route to the same target must not contribute or coalesce.
    auto.config.save_route({**auto.route,'id':'unselected','version':1,'mode':'review'})
    first=enqueue(auto, 'RouteFirst', visibility='restricted')
    second=enqueue(auto, 'RouteSecond', visibility='unknown')
    plan=auto.auto_queue.plan(first)
    assert plan['text'].startswith('synthetic\nSynthetic research\n')
    assert plan['delivery']['route_refs']==[{'route_id':'score','route_version':2,'approval_id':None}]
    with pytest.raises(ValueError, match='waiting'):
        auto.auto_queue.plan(second)
    auto.auto_queue.schedule.pause('score', True)
    with pytest.raises(ValueError, match='paused'):
        auto.auto_queue.plan(first)
    auto.auto_queue.schedule.pause('score', False)
    progress(auto);succeed(auto)
    assert Outbox(auto.db).get(first)['state']=='sent'
    with pytest.raises(ValueError, match='waiting'):
        auto.auto_queue.plan(second)
    assert auto.auto_queue.schedule.query()['items'][0]['text']==plan['text']


def test_selected_route_template_error_and_route_edit_never_retargets(auto):
    configure_route_queue(auto, message_template='{{views}} {{url}}')
    blocked=enqueue(auto, 'MissingValues')
    assert auto.auto_queue.schedule.query()['items'][0]['reason']=='missing_template_fields:views'
    with pytest.raises(ValueError):auto.auto_queue.plan(blocked)
    configure_route_queue(auto, version=3, message_template='{{url}}')
    queued=enqueue(auto, 'BeforeEdit')
    saved=auto.auto_queue.schedule.get(queued)
    auto.config.save_route({**auto.route,'version':4,'message_template':'changed {{url}}'})
    with pytest.raises(ValueError):auto.auto_queue.plan(queued)
    assert auto.auto_queue.schedule.get(queued)['text']==saved['text']


def test_selected_route_change_cannot_dispatch_saved_item(auto):
    configure_route_queue(auto)
    queued=enqueue(auto, 'SelectedOnly')
    auto.session._config['route_ids']=['another']
    auto.session._snapshot['config']['route_ids']=['another']
    with pytest.raises(ValueError, match='selected_route_changed'):
        auto.auto_queue.plan(queued)
    assert send_requests(auto)==[]


@pytest.mark.parametrize('action',['pause','stop'])
def test_collection_pause_or_stop_finishes_only_existing_route_attempt_without_relogin(auto,action):
    configure_route_queue(auto)
    first=enqueue(auto,'AlreadyNative');progress(auto)
    assert auto.auto_controller._state=='sending'
    client=auto.client._client;profile=auto.client._profile_guard
    second=enqueue(auto,'LaterQueued')
    getattr(auto.session,action)()
    assert auto.auto_controller._state=='sending'
    succeed(auto)
    assert Outbox(auto.db).get(first)['state']=='sent'
    assert Outbox(auto.db).get(second)['state']=='pending'
    auto.clock.advance(120)
    for _ in range(4):auto.auto_controller.tick()
    assert len(send_requests(auto))==1
    assert auto.auth.status=='ready' and auto.client.transport_ready
    assert auto.client._client is client and auto.client._profile_guard is profile and auto.library.client_id==1
    assert not any(request['@type'] in {'close','logOut'} for request in auto.library.calls)


def test_generic_sender_cannot_claim_or_prepare_scheduled_route_delivery(auto):
    configure_route_queue(auto)
    delivery=enqueue(auto,'ScheduledOnly')
    assert Outbox(auto.db).claim_next('generic', exclude_scheduled=True) is None
    before=len(auto.library.calls)
    assert auto.sender.prepare_destination() is False
    assert auto.sender.start_next('generic') is None
    assert Outbox(auto.db).get(delivery)['state']=='pending'
    assert Outbox(auto.db).get(delivery)['attempt_id'] is None
    assert len(auto.library.calls)==before


@pytest.mark.parametrize('action',['pause','stop','route_pause'])
def test_paused_metadata_preflight_preserves_unstarted_route_item_for_resume(auto,action):
    configure_route_queue(auto)
    delivery=enqueue(auto,'MetadataPaused')
    auto.auto_controller.tick()
    assert auto.auto_controller._state=='verifying'
    if action=='route_pause':
        auto.auto_queue.schedule.pause('score',True)
        auto.auto_controller.tick()
    else:getattr(auto.session,action)()
    assert Outbox(auto.db).get(delivery)['state']=='pending'
    assert auto.auto_queue.schedule.get(delivery)['cancelled']==0
    assert not send_requests(auto)
    assert auto.auth.status=='ready' and auto.client.transport_ready
    if action=='route_pause':auto.auto_queue.schedule.pause('score',False)
    else:auto.session.start({'feed_url':'https://www.threads.com/','mode':'auto','route_ids':['score']})
    for _ in range(30):
        drain(auto);auto.auto_controller.tick()
        if auto.auto_controller._state=='sending':break
    assert auto.auto_controller._state=='sending'
    succeed(auto)
    assert Outbox(auto.db).get(delivery)['state']=='sent'
    assert len(send_requests(auto))==1 and auto.library.client_id==1


def test_route_queue_cancel_and_edit_are_visible_without_retarget(auto):
    configure_route_queue(auto)
    cancelled=enqueue(auto, 'CancelledRoute')
    auto.auto_queue.schedule.cancel(cancelled)
    assert Outbox(auto.db).get(cancelled)['state']=='cancelled'
    old=enqueue(auto, 'OldRoute')
    old_text=auto.auto_queue.schedule.get(old)['text']
    LocalFeatures(auto.db, router=auto.sender.router, automatic=auto.automatic).save_route({**auto.route,'version':3,'message_template':'new {{url}}'})
    items={item['id']:item for item in auto.auto_queue.schedule.query()['items']}
    assert items[old]['state']=='blocked' and items[old]['reason']=='route_configuration_changed'
    assert items[old]['text']==old_text
    assert items[cancelled]['state']=='cancelled'
    later=enqueue(auto, 'NewRoute')
    assert auto.auto_queue.schedule.get(later)['text'].startswith('new https://')


def test_route_queue_lost_completion_return_never_replays(auto, monkeypatch):
    configure_route_queue(auto)
    queued=enqueue(auto, 'LostRouteReturn')
    progress(auto)
    finish=auto.auto_queue.finish
    def lost_return(*args):
        finish(*args)
        raise OSError('synthetic lost return')
    monkeypatch.setattr(auto.auto_queue,'finish',lost_return)
    succeed(auto)
    assert auto.auto_controller._state=='unknown'
    assert not auto.auto_queue.profile_ready(auto.client._profile_guard.profile_id)
    for _ in range(4):auto.auto_controller.tick()
    assert len(send_requests(auto))==1
    assert Outbox(auto.db).get(queued)['state'] in {'sent','unknown'}


def test_route_queue_ipc_read_pause_cancel_validates_contract(auto):
    from threadrelay.main import dispatch
    from threadrelay.contracts import validate_contract
    configure_route_queue(auto)
    queued=enqueue(auto, 'QueueIpc')
    features=LocalFeatures(auto.db, router=auto.sender.router, automatic=auto.automatic)
    def command(kind,payload):
        value={'version':1,'id':'route-queue-ipc','type':kind,'payload':payload}
        validate_contract('Command',value)
        result=dispatch(value,features=features)
        validate_contract('Event',result)
        return result
    result=command('QueryRouteQueue',{'route_id':'score'})
    assert result['type']=='RouteQueue' and result['payload']['items'][0]['id']==queued
    assert command('SetRouteQueuePaused',{'route_id':'score','paused':True})['type']=='Acknowledged'
    assert command('QueryRouteQueue',{'route_id':'score'})['payload']['paused']
    assert command('CancelRouteQueueItem',{'delivery_id':queued})['type']=='Acknowledged'
    assert command('QueryRouteQueue',{})['payload']['items'][0]['state']=='cancelled'


@pytest.mark.parametrize('change',['source','topic'])
def test_stale_head_is_blocked_and_later_valid_item_can_progress(auto,change):
    configure_route_queue(auto)
    stale=enqueue(auto,'StaleHead')
    if change=='source':
        source=post(post_id='threads:StaleHead',permalink='https://www.threads.net/@synthetic/post/StaleHead',text='Changed retained source')
        PostStore(auto.db).upsert(source)
    else:
        auto.config.save_topic({**auto.config.topics()[0],'version':2,'description':'Updated synthetic research'})
    auto.clock.advance(120)
    later=enqueue(auto,'FreshBehindStale')
    plan=auto.auto_queue.due()
    assert plan is not None and plan['job_id']==later
    items={item['id']:item for item in auto.auto_queue.schedule.query()['items']}
    assert items[stale]['state']=='blocked'
    assert items[stale]['reason']=='classification_not_current'
    assert send_requests(auto)==[]


def test_preflight_failure_blocks_only_live_unstarted_route_queue(auto):
    configure_route_queue(auto)
    queued=enqueue(auto,'PreflightReason')
    plan=auto.auto_queue.plan(queued)
    auto.auto_queue.schedule.pause('score',True)
    auto.auto_queue.preflight_failed(plan,'verification_failed')
    assert auto.auto_queue.schedule.get(queued)['reason'] is None
    assert Outbox(auto.db).get(queued)['state']=='pending'
    auto.auto_queue.schedule.pause('score',False)
    auto.auto_queue.preflight_failed(plan,'verification_failed')
    assert auto.auto_queue.schedule.query()['items'][0]['state']=='blocked'
    assert auto.auto_queue.schedule.get(queued)['reason']=='verification_failed'


def test_selected_route_match_survives_another_topics_abstention(auto):
    from test_routing import topic
    auto.config.save_topic(topic('uncertain'))
    auto.sender.router.classifier._runtime.decide = lambda state,questions: {key:.95 if key=='science' else .5 for key in questions}
    configure_route_queue(auto,topic_ids=['science','uncertain'])
    delivery=enqueue(auto,'MixedTopicDecision')
    assert auto.auto_queue.plan(delivery)['text']
    progress(auto);succeed(auto)
    assert Outbox(auto.db).get(delivery)['state']=='sent'
    assert len(send_requests(auto))==1


def test_selected_route_abstention_without_a_match_never_queues(auto):
    configure_route_queue(auto)
    auto.sender.router.classifier._runtime.decide=lambda state,questions:{key:.5 for key in questions}
    source=post();PostStore(auto.db).upsert(source)
    router=auto.sender.router
    assert router.plan(source,router.classifier.classify_bound(source,auto.config.topics()),session_mode='auto')==[]
    assert auto.auto_queue.schedule.query()['items']==[]


def test_unselected_topic_match_cannot_queue_an_abstaining_selected_route(auto):
    from test_routing import topic
    auto.config.save_topic(topic('unselected'))
    configure_route_queue(auto)
    auto.sender.router.classifier._runtime.decide=lambda state,questions:{key:.95 if key=='unselected' else .5 for key in questions}
    source=post();PostStore(auto.db).upsert(source)
    router=auto.sender.router
    assert router.plan(source,router.classifier.classify_bound(source,auto.config.topics()),session_mode='auto')==[]
    assert auto.auto_queue.schedule.query()['items']==[]
    assert not send_requests(auto)


def test_selected_route_interval_is_not_overridden_by_legacy_two_per_minute(auto):
    configure_route_queue(auto,queue={'mode':'interval','interval_seconds':1})
    for index in range(3):
        delivery=enqueue(auto,'FastSelected'+str(index))
        progress(auto)
        assert auto.auto_controller._state=='sending'
        succeed(auto,100+index)
        assert Outbox(auto.db).get(delivery)['state']=='sent'
        auto.clock.advance(1)
    assert len(send_requests(auto))==3


def fail_native(env, *, code=429, error='FLOOD_WAIT_15', wait=15, failed=777, wrong_text=False):
    owner=env.auto_controller;binding=owner._marker_binding
    def message(identifier,state):
        return {'@type':'message','id':identifier,'chat_id':-20,'sender_id':{'@type':'messageSenderUser','user_id':123},'is_outgoing':True,
            'content':{'@type':'messageText','text':{'@type':'formattedText','text':'Changed text' if wrong_text else owner._plan['text'],'entities':[]}},'sending_state':state}
    env.library.events.append({'@client_id':env.library.client_id,'@extra':binding['request_extra'],**message(-100,{'@type':'messageSendingStatePending'})})
    env.library.events.append({'@client_id':env.library.client_id,'@type':'updateMessageSendFailed','old_message_id':-100,
        'error':{'@type':'error','code':code,'message':error},'message':message(failed,{'@type':'messageSendingStateFailed',
            'error':{'@type':'error','code':code,'message':error},'can_retry':True,'retry_after':wait,'need_another_sender':False,
            'need_another_reply_quote':False,'need_drop_reply':False,'required_paid_message_star_count':0})})
    drain(env)


def test_proven_native_failure_keeps_login_and_honors_provider_wait_without_retry(auto):
    configure_route_queue(auto,queue={'mode':'immediate'})
    first=enqueue(auto,'ProviderWaitFirst');progress(auto);fail_native(auto)
    assert Outbox(auto.db).get(first)['state']=='failed'
    assert auto.auto_controller._state=='failed'
    assert auto.client._profile_guard.purpose=='automatic_failed'
    assert auto.auto_queue.profile_ready(auto.client._profile_guard.profile_id)
    second=enqueue(auto,'ProviderWaitSecond')
    items={item['id']:item for item in auto.auto_queue.schedule.query()['items']}
    assert items[first]['reason']=='telegram_flood_wait:15'
    assert items[second]['state']=='waiting' and items[second]['reason']=='telegram_flood_wait:15'
    for _ in range(4):auto.auto_controller.tick()
    assert len(send_requests(auto))==1
    auto.clock.advance(15)
    for _ in range(30):
        drain(auto);auto.auto_controller.tick()
        if auto.auto_controller._state=='sending':break
    assert auto.auto_controller._state=='sending'
    succeed(auto,101)
    assert Outbox(auto.db).get(second)['state']=='sent' and len(send_requests(auto))==2
    assert auto.auth.status=='ready' and auto.library.client_id==1


def test_lost_failed_commit_return_remains_quarantined_and_never_retries(auto,monkeypatch):
    configure_route_queue(auto,queue={'mode':'immediate'})
    first=enqueue(auto,'FailedCommitLost');progress(auto)
    complete=auto.auto_queue.complete_failed
    def lost(*args):
        complete(*args)
        raise OSError('synthetic lost return')
    monkeypatch.setattr(auto.auto_queue,'complete_failed',lost)
    fail_native(auto)
    assert Outbox(auto.db).get(first)['state']=='failed'
    assert auto.auto_controller._state=='unknown'
    assert not auto.auto_queue.profile_ready(auto.client._profile_guard.profile_id)
    auto.clock.advance(60)
    for _ in range(4):auto.auto_controller.tick()
    assert len(send_requests(auto))==1


def test_direct_provider_flood_rejection_reports_wait_without_unknown_quarantine(auto):
    configure_route_queue(auto,queue={'mode':'immediate'})
    delivery=enqueue(auto,'DirectProviderError');progress(auto)
    binding=auto.auto_controller._marker_binding
    auto.library.events.append({'@client_id':auto.library.client_id,'@type':'error','@extra':binding['request_extra'],'code':429,'message':'FLOOD_WAIT_20'})
    drain(auto)
    assert Outbox(auto.db).get(delivery)['state']=='failed'
    assert auto.auto_queue.profile_ready(auto.client._profile_guard.profile_id)
    assert auto.auto_queue.schedule.query()['items'][0]['reason']=='telegram_flood_wait:20'
    auto.clock.advance(30)
    for _ in range(3):auto.auto_controller.tick()
    assert len(send_requests(auto))==1


def test_definite_failure_can_restore_saved_account_without_replaying_failed_message(auto):
    from test_saved_account_restore import adapter,disconnect
    from test_owner_history import auth_state
    configure_route_queue(auto,queue={'mode':'immediate'})
    delivery=enqueue(auto,'FailedRestore');progress(auto);fail_native(auto)
    profile=auto.client._profile_guard
    (profile.path/'td.binlog').write_bytes(b'synthetic encrypted auth database')
    auto.session.stop();disconnect(auto)
    adapter(auto).restore()
    auto.library.events.extend([auth_state(auto,'WaitTdlibParameters'),auth_state(auto,'Ready')]);drain(auto)
    if not auto.auth._network_requested:auto.auth.enable_saved_connection();drain(auto)
    assert auto.auth.status=='ready'
    assert auto.client._profile_guard.purpose=='automatic_failed'
    assert Outbox(auto.db).get(delivery)['state']=='failed'
    assert len(send_requests(auto))==1


def test_failed_provider_wait_is_durable_and_original_failed_message_is_never_requeued(auto,tmp_path):
    from threadrelay.delivery.route_schedule import RouteSchedule
    configure_route_queue(auto,queue={'mode':'immediate'})
    failed=enqueue(auto,'DurableProviderWait');progress(auto);fail_native(auto,wait=30,error='SLOWMODE_WAIT_30')
    queued=enqueue(auto,'AfterDurableProviderWait')
    from threadrelay.storage.database import Database
    backup=tmp_path/'restarted.sqlite3';auto.db.backup(backup)
    restarted=Database(backup,clock=auto.clock.now)
    restored=RouteSchedule(restarted)
    assert not restored.ready(queued)
    assert restored.query()['items'][-1]['reason']=='telegram_slowmode_wait:30'
    with restarted.connection() as con:
        assert restored.provider_wait(con,'123','-99') is None
        assert restored.provider_wait(con,'123','-20') is not None
    assert Outbox(auto.db).get(failed)['state']=='failed'
    with pytest.raises(ValueError,match='pristine_automatic_intent_required'):auto.auto_queue.plan(failed)
    auto.clock.advance(30)
    assert restored.ready(queued)
    assert Outbox(auto.db).get(failed)['state']=='failed'
    restarted.close()


@pytest.mark.parametrize('boundary',['profile_complete','finish','profile_finalize'])
def test_failed_receipt_durability_errors_never_release_uncertain_native_attempt(auto,monkeypatch,boundary):
    configure_route_queue(auto,queue={'mode':'immediate'})
    queued=enqueue(auto,'FailedDurability'+boundary);progress(auto)
    def fail(*args,**kwargs):raise OSError('synthetic failed receipt boundary')
    if boundary=='profile_complete':monkeypatch.setattr(auto.auto_controller._profile,'complete_automatic_failed',fail)
    if boundary=='finish':monkeypatch.setattr(auto.auto_queue,'finish',fail)
    if boundary=='profile_finalize':monkeypatch.setattr(auto.auto_controller._profile,'finalize_automatic_failed',fail)
    fail_native(auto)
    assert auto.auto_controller._state=='unknown'
    assert not auto.auto_queue.profile_ready(auto.client._profile_guard.profile_id)
    auto.clock.advance(60)
    for _ in range(3):auto.auto_controller.tick()
    assert len(send_requests(auto))==1
    assert Outbox(auto.db).get(queued)['state'] in {'failed','unknown'}


@pytest.mark.parametrize('code,message',[(500,'Request aborted'),(401,'Unauthorized'),(404,'Not Found'),(408,'Timeout')])
def test_ambiguous_direct_error_never_becomes_definite_failed_receipt(auto,code,message):
    configure_route_queue(auto,queue={'mode':'immediate'})
    delivery=enqueue(auto,'AmbiguousError'+str(code));progress(auto)
    binding=auto.auto_controller._marker_binding
    auto.library.events.append({'@client_id':auto.library.client_id,'@type':'error','@extra':binding['request_extra'],'code':code,'message':message})
    drain(auto)
    assert Outbox(auto.db).get(delivery)['state']=='unknown'
    assert not auto.auto_queue.profile_ready(auto.client._profile_guard.profile_id)
    assert len(send_requests(auto))==1


@pytest.mark.parametrize('mismatch',['wrong_content','reused_temporary'])
def test_wrong_content_failure_and_reused_temporary_id_never_release_unknown(auto,mismatch):
    configure_route_queue(auto,queue={'mode':'immediate'})
    delivery=enqueue(auto,'FailureWrongContent');progress(auto);fail_native(auto,wrong_text=mismatch=='wrong_content',failed=-100 if mismatch=='reused_temporary' else 777)
    assert Outbox(auto.db).get(delivery)['state']=='unknown'
    assert not auto.auto_queue.profile_ready(auto.client._profile_guard.profile_id)
    assert len(send_requests(auto))==1


def test_provider_flood_wait_also_blocks_manual_history_sends(auto):
    from test_owner_history import job
    from threadrelay.delivery.owner_history import ManualHistoryPreviewError
    configure_route_queue(auto,queue={'mode':'immediate'})
    enqueue(auto,'WaitBeforeManual');progress(auto);fail_native(auto)
    auto.session.stop()
    item=job(auto)
    with pytest.raises(ManualHistoryPreviewError):auto.controller.preview({'job_id':item['id'],'version':item['version']})
    assert len(send_requests(auto))==1


@pytest.mark.parametrize('reason',['verification_failed','preflight_unavailable','timed_out','preflight_retry_exhausted','destination_verification_interrupted','destination_verification_incomplete'])
def test_explicit_queue_resume_rechecks_only_pristine_preflight_failures(auto,reason):
    configure_route_queue(auto)
    queued=enqueue(auto,'RetryPreflight')
    auto.auto_queue.schedule.block_unstarted(queued,reason)
    auto.auto_queue.schedule.pause('score',False)
    assert auto.auto_queue.schedule.get(queued)['reason'] is None
    assert Outbox(auto.db).get(queued)['state']=='pending'
    assert send_requests(auto)==[]
    progress(auto)
    assert len(send_requests(auto))==1


def test_explicit_queue_resume_preserves_stale_and_initiated_items(auto):
    configure_route_queue(auto)
    stale=enqueue(auto,'StaleNotRetried')
    auto.auto_queue.schedule.block_unstarted(stale,'classification_not_current')
    auto.auto_queue.schedule.pause('score',False)
    assert auto.auto_queue.schedule.get(stale)['reason']=='classification_not_current'
    started=enqueue(auto,'StartedNotRetried');progress(auto)
    with auto.db.transaction() as con:
        con.execute("UPDATE route_queue SET reason='verification_failed' WHERE delivery_id=?",(started,))
    auto.auto_queue.schedule.pause('score',False)
    assert auto.auto_queue.schedule.get(started)['reason']=='verification_failed'
    assert len(send_requests(auto))==1
