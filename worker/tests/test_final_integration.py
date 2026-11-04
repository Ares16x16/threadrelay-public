"""No-service regressions of the production composition (not native acceptance)."""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from threading import Event
from types import SimpleNamespace

import pytest

from threadrelay.application import Application
from threadrelay.pipeline import Pipeline
from threadrelay.features import LocalFeatures
from threadrelay.storage.posts import PostStore
from threadrelay.storage.outbox import Outbox
import test_routing
from test_routing import running, plan, route

from test_pipeline import NoSend

env = test_routing.env


def test_final_admitted_observation_is_processed_without_send(env):
    session = running('review', max_posts=1)
    plans = Pipeline(session, Event(), PostStore(env.db), env.config, env.classifier, env.router, NoSend()).process(env.post)
    assert plans and plans[0]['approval_required']
    assert session.snapshot['counters']['observed'] == 1
    assert session.snapshot['counters']['matched'] == 1
    assert session.snapshot['state'] == 'paused'
    assert not session.can_transmit(datetime.now(timezone.utc))


def test_review_session_strengthens_auto_route(env):
    env.config.save_route(route(version=2, mode='auto'))
    session = running('review')
    proposals = Pipeline(session, Event(), PostStore(env.db), env.config, env.classifier, env.router, NoSend()).process(env.post)
    assert proposals[0]['approval_required']
    assert proposals[0]['route']['mode'] == 'auto'
    assert len(LocalFeatures(env.db, router=env.router).review()) == 1


def test_review_queued_counter_is_exactly_once_and_wakes_owner(env):
    session = running('review')
    wake = []
    features = LocalFeatures(env.db, router=env.router, session=session, wake_delivery=lambda: wake.append(True))
    proposal = plan(env)[0]
    disclosure = asdict(env.approvals.disclosure(proposal))
    features.approve_review(proposal, disclosure)
    features.approve_review(proposal, disclosure)
    assert session.snapshot['counters']['queued'] == 1
    assert len(wake) == 1


def test_confirmed_retention_applies_on_application_maintenance(tmp_path, env):
    old = {**env.post, 'observed_at': (datetime.now(timezone.utc)-timedelta(days=400)).isoformat()}
    with Application(tmp_path / 'app') as app:
        app.posts.upsert(old)
        app.maintenance()
        assert app.posts.get(old['post_id']) is not None  # proposal is not deletion consent
        app.features.save_retention({'text_days':7,'dedup_days':30,'diagnostic_days':7,'confirmed':True})
        app.maintenance(force=True)
        assert app.posts.get(old['post_id']) is None
    with Application(tmp_path / 'app') as app:
        assert app.features.retention()['confirmed'] is True


def test_proof_derived_account_gate_and_clean_auth_progression(tmp_path):
    from test_telegram_auth import FixtureBinding, FixtureVault, state
    connected = Event()
    class Binding(FixtureBinding):
        def send(self, client_id, request):
            super().send(client_id, request)
            if request.get('@extra', '').startswith('init:'):
                self.events.append({'@client_id':42,'@type':'ok','@extra':request['@extra']})
            if request.get('type', {}).get('@type') == 'networkTypeOther':
                connected.set()
    binding = Binding()
    class Broker(FixtureVault):
        def capabilities(self): return dict(browser=False,inference=False,telegram=True,automatic=False)
        def load_tdlib(self): return binding
        def begin_account(self): return 'vault:synthetic'
    with Application(tmp_path, broker=Broker()) as app:
        assert app.features.account_features.native_gate()
        assert app.features.destination_features.native_gate()
        app.features.account_features.begin()
        binding.events.append(state('WaitTdlibParameters'))
        assert connected.wait(3)
        assert app.telegram.network_config_ready


def test_unproved_account_gate_remains_closed(tmp_path):
    with Application(tmp_path) as app:
        assert not app.features.account_features.native_gate()
        assert app.telegram._client is None


def assembled(tmp_path):
    """Real Application/owner/auth/sender/router with only native/vault replaced."""
    from test_telegram_auth import FixtureBinding, FixtureVault
    from test_routing import ScoresRuntime, topic
    from threadrelay.inference.decision import Classifier
    class Binding(FixtureBinding):
        def send(self, client, request):
            super().send(client, request)
            kind, extra = request['@type'], request.get('@extra')
            if kind in {'setNetworkType','setTdlibParameters','setOption'}:
                self.events.append({'@client_id':42,'@type':'ok','@extra':extra})
                if request.get('type',{}).get('@type') == 'networkTypeOther':
                    self.events.append({'@client_id':42,'@type':'updateConnectionState','state':{'@type':'connectionStateReady'}})
            elif kind == 'getMe':
                self.events.append({'@client_id':42,'@type':'user','@extra':extra,'id':123,'type':{'@type':'userTypeRegular'}})
            elif kind == 'getChat':
                self.events.append({'@client_id':42,'@type':'chat','@extra':extra,'id':456,'title':'Synthetic target','type':{'@type':'chatTypePrivate','user_id':789},'message_sender_id':{'@type':'messageSenderUser','user_id':123}})
            elif kind == 'canSendMessageToUser':
                self.events.append({'@client_id':42,'@type':'canSendMessageToUserResultOk','@extra':extra})
            elif kind == 'close':
                from test_telegram_auth import state
                self.events.append(state('Closed'))
    binding=Binding()
    class Broker(FixtureVault):
        removed=0
        def capabilities(self):return dict(browser=False,inference=False,telegram=True,automatic=False)
        def begin_account(self):return 'vault:synthetic'
        def load_tdlib(self):return binding
        def remove_account(self):self.removed+=1
    broker=Broker();app=Application(tmp_path,broker=broker)
    assert app.delivery_owner.close()  # deterministic single owner; never a second receive loop
    app.session._setup_check=lambda _:dict(allowed=True,reason=None)
    app.classifier=Classifier(ScoresRuntime());app.router.classifier=app.classifier
    app.config.save_topic(topic())
    return app,binding,broker


def ready(app,binding):
    from test_telegram_auth import state
    app.features.account_features.begin()
    binding.events.extend([state('WaitTdlibParameters'),state('Ready')])
    for _ in range(30): app.delivery_owner.step()
    assert app.auth.status=='ready' and app.telegram.transport_ready


def test_clean_auth_native_enable_failure_clears_challenge_and_requires_closed_before_begin(tmp_path):
    from test_telegram_auth import state
    from threadrelay.delivery.auth import AuthFailure
    app,binding,_=assembled(tmp_path)
    try:
        app.features.account_features.begin()
        binding.events.extend([state('WaitTdlibParameters'),state('WaitPhoneNumber')])
        while binding.events: app.auth.poll()
        old=app.auth.challenge()
        original_send=binding.send
        def fail_enable(client,request):
            if request.get('type',{}).get('@type')=='networkTypeOther':
                raise RuntimeError('private synthetic native failure')
            original_send(client,request)
        binding.send=fail_enable
        with pytest.raises(AuthFailure,match='Network authority unavailable'):
            app.delivery_owner.step()
        assert app.features.account()['status']=='authentication_error'
        assert app.features.account()['challenge'] is None
        assert app.auth.snapshot().network_state=='offline'
        assert app.telegram._closing
        with pytest.raises(AuthFailure): app.features.account_features.begin()
        with pytest.raises(AuthFailure): app.auth.request_qr(old.challenge_id)
        binding.send=original_send
        binding.events.extend([state('WaitPhoneNumber'),state('Ready')])
        for _ in range(4):app.delivery_owner.step()
        assert app.auth.status=='authentication_error' and app.auth.challenge() is None
        assert not any(request['@type']=='getMe' for request in binding.requests)
        assert not any(request.get('type',{}).get('@type')=='networkTypeOther' for request in binding.requests)
        app.features.account_features.cancel()
        with pytest.raises(AuthFailure): app.features.account_features.begin()
        for _ in range(4):app.delivery_owner.step()
        assert app.auth.status=='closed'
        app.features.account_features.begin()
        binding.events.extend([state('WaitTdlibParameters'),state('WaitPhoneNumber')])
        for _ in range(30):app.delivery_owner.step()
        assert binding.created==2 and app.auth.status=='authorizing'
        assert app.auth.challenge().challenge_id!=old.challenge_id
        assert app.telegram.transport_ready
    finally:app.close()


@pytest.mark.parametrize('challenge_kind',['phone','qr'])
def test_collection_stop_keeps_clean_auth_challenge_until_explicit_account_cancel(tmp_path,challenge_kind):
    from test_telegram_auth import state
    from threadrelay.delivery.auth import AuthFailure
    app,binding,_=assembled(tmp_path)
    try:
        app.features.account_features.begin()
        challenge=(state('WaitPhoneNumber') if challenge_kind=='phone' else
            state('WaitOtherDeviceConfirmation',link='tg://login?token=SYNTHETIC'))
        binding.events.extend([state('WaitTdlibParameters'),challenge])
        for _ in range(30):app.delivery_owner.step()
        assert app.auth.status=='authorizing' and app.telegram.transport_ready
        assert app.auth.challenge().kind==challenge_kind
        app.session.stop()
        for _ in range(4):app.delivery_owner.step()
        assert app.features.account()['status']=='authorizing'
        assert app.features.account()['challenge']['kind']==challenge_kind
        assert app.telegram.transport_ready and binding.created==1
        assert sum(request.get('type',{}).get('@type')=='networkTypeOther' for request in binding.requests)==1
        assert not any(request['@type']=='getMe' for request in binding.requests)
        with pytest.raises(AuthFailure):app.features.account_features.begin()
        app.features.account_features.cancel()
        for _ in range(4):app.delivery_owner.step()
        assert app.auth.status=='closed'
        app.features.account_features.begin()
        binding.events.extend([state('WaitTdlibParameters'),state('WaitPhoneNumber')])
        for _ in range(30):app.delivery_owner.step()
        assert app.telegram.transport_ready and binding.created==2
    finally:app.close()


def test_clean_auth_enable_failure_suspends_saved_readiness_without_revoking_epoch(tmp_path):
    from threadrelay.delivery.auth import AuthFailure
    from threadrelay.delivery.telegram import NetworkPermit
    app,binding,_=assembled(tmp_path)
    try:
        ready(app,binding)
        epoch=app.auth.snapshot().epoch
        app.telegram.disable_network()
        while binding.events:app.auth.poll()
        original_send=binding.send
        def fail_enable(client,request):
            if request.get('type',{}).get('@type')=='networkTypeOther':
                raise RuntimeError('private synthetic native failure')
            original_send(client,request)
        binding.send=fail_enable
        with pytest.raises(AuthFailure):app.auth.enable_clean_auth(NetworkPermit('clean_auth',epoch,False))
        assert app.auth.status=='authentication_error'
        assert app.auth.snapshot().auth_state=='closed'
        saved=app.auth._accounts.account_state('123')
        assert saved['auth_state']=='closed' and saved['network_state']=='offline'
        assert saved['epoch']==epoch and not saved['revocation_pending']
        assert not any(request['@type'] in {'logOut','close','sendMessage'} for request in binding.requests)
    finally:app.close()


def stage_in_app(app,index=1):
    from test_routing import route,post
    from threadrelay.routing.approvals import ApprovalStore
    app.config.save_route(route(destination_id='456',account_id='123',account_epoch=app.auth.snapshot().epoch,sender={'kind':'user','id':'123'}))
    app.config.save_destination({'account_id':'123','chat_id':'456','title':'Synthetic target','audience':'private','permissions':{'can_send':True,'can_select_sender':False},'sender':{'kind':'user','id':'123'},'paid_required':False},account_epoch=app.auth.snapshot().epoch)
    # The fixture owner explicitly saves this exact recipient before routing.
    from threadrelay.storage.saved_destinations import SavedDestinations
    SavedDestinations(app.db).save_verified({'account_id':'123','chat_id':'456','title':'Synthetic target','audience':'private','permissions':{'can_send':True,'can_select_sender':False},'sender':{'kind':'user','id':'123'},'paid_required':False},app.auth.snapshot().epoch,'private')
    p=post(post_id=f'threads:Owned{index}',permalink=f'https://www.threads.net/@fixture/post/Owned{index}')
    app.posts.upsert(p)
    proposal=app.router.plan(p,app.classifier.classify_bound(p,app.config.topics()),session_mode='review')[0]
    app.features.approve_review(proposal,asdict(ApprovalStore(app.router).disclosure(proposal)))
    with app.db.connection() as con:
        return con.execute('SELECT id FROM deliveries WHERE post_id=?',(p['post_id'],)).fetchone()[0]


def test_owner_pumps_approval_without_any_new_observation_and_times_out(tmp_path):
    app,binding,_=assembled(tmp_path)
    try:
        ready(app,binding)
        app.session.start({'feed_url':'https://www.threads.com/','mode':'review'})
        cancelled=Event();app.delivery_owner._running=cancelled
        identifier=stage_in_app(app)
        for _ in range(12):app.delivery_owner.step()
        assert Outbox(app.db).get(identifier)['state']=='sending'
        assert sum(r['@type']=='sendMessage' for r in binding.requests)==1
        attempt,deadline=app.sender._deadlines[identifier]
        app.sender.monotonic=lambda:deadline
        app.delivery_owner.step()
        assert Outbox(app.db).get(identifier)['state']=='unknown'
        for _ in range(5):app.delivery_owner.step()
        assert sum(r['@type']=='sendMessage' for r in binding.requests)==1
    finally:app.close()


def test_owner_rate_denial_is_proven_unstarted_and_releases_after_window(tmp_path):
    app,binding,_=assembled(tmp_path)
    try:
        ready(app,binding)
        app.session.start({'feed_url':'https://www.threads.com/','mode':'review'})
        app.delivery_owner._running=Event()
        ids=[stage_in_app(app,i) for i in range(3)]
        stamp=[app.clock.monotonic()]
        app.sender.monotonic=lambda:stamp[0]
        app.router.clock=SimpleNamespace(monotonic=lambda:stamp[0])
        app.sender.attempt_timeout=120
        for _ in range(20):app.delivery_owner.step()
        states=[Outbox(app.db).get(i)['state'] for i in ids]
        assert states.count('sending')==2 and states.count('pending')==1
        pending=next(i for i in ids if Outbox(app.db).get(i)['state']=='pending')
        with app.db.connection() as con:
            assert con.execute('SELECT initiated_attempt_id FROM routing_delivery_context WHERE delivery_id=?',(pending,)).fetchone()[0] is None
        stamp[0]+=61
        for _ in range(5):app.delivery_owner.step()
        assert Outbox(app.db).get(pending)['state']=='sending'
        assert sum(r['@type']=='sendMessage' for r in binding.requests)==3
    finally:app.close()


def test_cancel_retry_reuses_credentials_after_observed_closed(tmp_path):
    from test_telegram_auth import state
    app,binding,_=assembled(tmp_path)
    try:
        app.features.account_features.begin()
        app.features.account_features.cancel()
        for _ in range(3):app.delivery_owner.step()
        assert app.auth.status=='closed'
        app.features.account_features.begin()
        binding.events.extend([state('WaitTdlibParameters'),state('Ready')])
        for _ in range(30):app.delivery_owner.step()
        assert app.auth.status=='ready'
    finally:app.close()


def test_saved_pending_bootstrap_and_offline_removal_revoke_epochs(tmp_path):
    from test_telegram_auth import state
    app,binding,broker=assembled(tmp_path)
    try:
        ready(app,binding)
        identifier=stage_in_app(app)
        epoch=app.auth.snapshot().epoch
        app.features.account_features.cancel()
        for _ in range(3):app.delivery_owner.step()
        app.features.account_features.begin()
        app.session.start({'feed_url':'https://www.threads.com/','mode':'review'})
        app.delivery_owner.step('reconnect',Event())
        binding.events.extend([state('WaitTdlibParameters'),state('Ready')])
        for _ in range(35):app.delivery_owner.step()
        assert Outbox(app.db).get(identifier)['state']=='sending'
        app.delivery_owner.account_action('remove_local_offline')
        for _ in range(5):app.delivery_owner.step()
        assert broker.removed==1 and app.auth.snapshot().account_id is None
        assert app.auth._accounts.pending_revocations()[0]['epoch']==epoch+1
        assert Outbox(app.db).get(identifier)['state']=='unknown'
    finally:app.close()


def test_owner_lost_final_and_late_result_stay_unknown(tmp_path):
    app,binding,_=assembled(tmp_path)
    try:
        ready(app,binding);app.session.start({'feed_url':'https://www.threads.com/','mode':'review'});app.delivery_owner._running=Event()
        identifier=stage_in_app(app)
        for _ in range(12):app.delivery_owner.step()
        claim=Outbox(app.db).get(identifier)
        source={'@type':'messageSenderUser','user_id':123}
        binding.events.append({'@client_id':42,'@type':'message','@extra':claim['request_extra'],'id':-7,'chat_id':456,'sender_id':source})
        app.delivery_owner.step()
        assert Outbox(app.db).get(identifier)['temporary_message_id']=='-7'
        deadline=app.sender._deadlines[identifier][1]
        app.sender.monotonic=lambda:deadline
        app.delivery_owner.step()
        assert Outbox(app.db).get(identifier)['state']=='unknown'
        app.sender.monotonic=app.clock.monotonic
        binding.events.append({'@client_id':42,'@type':'updateMessageSendSucceeded','old_message_id':-7,'message':{'id':8,'chat_id':456,'sender_id':source}})
        app.delivery_owner.step()
        assert Outbox(app.db).get(identifier)['state']=='unknown'
        assert sum(r['@type']=='sendMessage' for r in binding.requests)==1
    finally:app.close()


def test_server_logout_needs_correlated_ok_and_closed_before_secure_cleanup(tmp_path):
    from test_telegram_auth import state
    app,binding,broker=assembled(tmp_path)
    try:
        ready(app,binding)
        app.delivery_owner.account_action('server_logout')
        assert app.auth.snapshot().revocation_pending
        assert broker.removed==0
        binding.events.extend([{'@client_id':42,'@type':'ok','@extra':'unrelated'},state('LoggingOut')])
        for _ in range(3):app.delivery_owner.step()
        assert app.auth.snapshot().revocation_pending and broker.removed==0
        binding.events.extend([{'@client_id':42,'@type':'ok','@extra':'auth:logout'},state('Closed')])
        for _ in range(4):app.delivery_owner.step()
        assert not app.auth._accounts.pending_revocations()
        assert broker.removed==1 and app.auth.status=='server_revoked'
    finally:app.close()


def test_offline_replace_can_authenticate_same_identity_without_erasing_old_revocation(tmp_path):
    app,binding,broker=assembled(tmp_path)
    try:
        ready(app,binding)
        old=app.auth.snapshot().epoch
        app.delivery_owner.account_action('remove_local_offline')
        for _ in range(4):app.delivery_owner.step()
        ready(app,binding)
        assert app.auth.snapshot().account_id=='123' and app.auth.snapshot().epoch==old+1
        assert app.auth._accounts.pending_revocations()[0]['epoch']==old+1
        assert broker.removed==1
    finally:app.close()


def test_auto_policy_requires_trusted_real_evidence_and_exact_explicit_route_opt_in(env):
    from copy import deepcopy
    import pytest
    from test_evaluation import native_runtime, record, topic as evaluation_topic
    from threadrelay.evaluation.runner import evaluate
    from threadrelay.inference.decision import Classifier
    from threadrelay.routing.automatic import AutomaticPolicy
    # Real-shaped fixture through DI only; never a native attestation or catalog entry.
    topic=evaluation_topic('science');env.config.save_topic({**topic,'version':2})
    topics=env.config.topics()
    data={'records':[record('fixture'+lang,expected=('science',),language=lang) for lang in ('en','zh','mixed','other')],'split':'test','synthetic':False,'topics':topics}
    runtime=native_runtime([]);runtime.decide=lambda state,questions:{key:1 for key in questions}
    classifier=Classifier(runtime)
    report=evaluate(data,classifier)
    evidence=[None]
    automatic=AutomaticPolicy(env.db,classifier,lambda:evidence[0])
    assert not automatic.authorized()
    policy={'confirmed':True,'min_precision':1,'min_recall':1,'min_positive':1,'min_predicted':1,'languages':['en']}
    automatic.save(policy)
    assert not automatic.authorized()
    evidence[0]=report
    assert not automatic.authorized()  # partial policy cannot cover arbitrary multilingual posts
    policy['languages']=['en','zh','mixed','other'];automatic.save(policy)
    assert automatic.authorized()
    evidence[0]=evaluate({**data,'records':data['records'][:1]},classifier)
    assert not automatic.authorized()  # missing language slices never enable multilingual Auto
    evidence[0]=report
    old_metadata=dict(runtime._metadata);runtime._metadata['runtime_commit']='different-fixture-runtime'
    assert not automatic.authorized()
    runtime._metadata=old_metadata
    selected=route(mode='auto',version=2)
    assert not automatic.route_authorized(selected)
    features=LocalFeatures(env.db,router=env.router,automatic=automatic)
    with pytest.raises(ValueError):features.save_route(selected)
    features.save_route(selected,expected_auto_receipt=features.preview_auto_route(selected)['receipt'])
    assert automatic.route_authorized(selected)
    from test_routing import destination
    env.config.save_destination(destination(audience='public'),account_epoch=selected['account_epoch'])
    assert not automatic.route_authorized(selected)  # audience changes require renewed explicit route opt-in
    features.save_route(selected,expected_auto_receipt=features.preview_auto_route(selected)['receipt'])
    assert automatic.route_authorized(selected)
    assert not automatic.route_authorized({**selected,'version':3})
    synthetic=deepcopy(report);synthetic['versions']['synthetic_dataset']='true';evidence[0]=synthetic
    assert not automatic.authorized()
    evidence[0]=report
    automatic.save(policy)  # reaffirmation invalidates prior route consent too
    assert not automatic.route_authorized(selected)
    env.config.save_topic({**topics[0],'version':3})
    assert not automatic.authorized()


def test_bulk_approval_validates_each_independent_disclosure(env):
    from test_routing import post
    from threadrelay.routing.approvals import ApprovalStore
    first=plan(env)[0]
    second_post=post(post_id='threads:Second',permalink='https://www.threads.net/@synthetic/post/Second')
    PostStore(env.db).upsert(second_post)
    second=env.router.plan(second_post,env.classifier.classify_bound(second_post,env.config.topics()))[0]
    disclosure=asdict(ApprovalStore(env.router).disclosure(first))
    features=LocalFeatures(env.db,router=env.router)
    result=features.bulk_review({'items':[{'proposal':first,'disclosure':disclosure},{'proposal':second,'disclosure':disclosure}]})
    assert [r['status'] for r in result['results']]==['staged','rejected']
    import pytest
    with pytest.raises(ValueError):features.bulk_review({'items':[{'proposal':first,'disclosure':disclosure}]*21})


def test_sample_composition_never_touches_posts_router_or_sender(tmp_path,monkeypatch):
    from test_routing import ScoresRuntime,topic
    import threadrelay.application as module
    class Runtime(ScoresRuntime):
        def __init__(self,**kwargs):self.active=kwargs['can_infer'];self.stopped=False
        def start(self,manifest,*,cancellation=None):assert self.active()
        def stop(self):self.stopped=True;return {'stopped':True,'remaining':[]}
        def revoke(self):pass
        def cancel_request(self):pass
        def status(self):return {'state':'not_loaded','model_id':'synthetic'}
    monkeypatch.setattr(module,'DecisionRuntime',Runtime)
    with Application(tmp_path) as app:
        app.config.save_topic(topic())
        result=app.test_sample({'text':'Synthetic research sample'})
        assert result['sending_possible'] is False
        with app.db.connection() as con:
            for table in ('posts','routing_plans','approvals','deliveries'):
                assert con.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]==0
        assert app.telegram._client is None and app.session.snapshot['state']=='stopped'


def test_owner_permitted_existing_retry_delay_and_pause_cancellation(tmp_path):
    app,binding,_=assembled(tmp_path)
    try:
        ready(app,binding);app.session.start({'feed_url':'https://www.threads.com/','mode':'review'});app.delivery_owner._running=Event()
        stamp=[app.clock.monotonic()];app.sender.monotonic=lambda:stamp[0];app.router.clock=SimpleNamespace(monotonic=lambda:stamp[0])
        identifier=stage_in_app(app)
        for _ in range(12):app.delivery_owner.step()
        claim=Outbox(app.db).get(identifier);source={'@type':'messageSenderUser','user_id':123}
        failure={'@type':'messageSendingStateFailed','can_retry':True,'retry_after':2.0,'need_another_sender':False,'need_another_reply_quote':False,'need_drop_reply':False,'required_paid_message_star_count':0}
        binding.events.extend([{'@client_id':42,'@type':'message','@extra':claim['request_extra'],'id':-7,'chat_id':456,'sender_id':source},
            {'@client_id':42,'@type':'updateMessageSendFailed','old_message_id':-7,'message':{'id':-8,'chat_id':456,'sender_id':source,'sending_state':failure}}])
        app.delivery_owner.step();app.delivery_owner.step()
        assert Outbox(app.db).get(identifier)['state']=='retryable'
        assert not any(r['@type']=='getMessageLocally' for r in binding.requests)
        stamp[0]+=2;app.delivery_owner.step()
        query=binding.requests[-1];assert query['@type']=='getMessageLocally'
        binding.events.append({'@client_id':42,'@type':'message','@extra':query['@extra'],'id':-8,'chat_id':456,'sender_id':source,'sending_state':failure,'paid_message_star_count':0})
        app.delivery_owner.step()
        assert not any(r['@type']=='resendMessages' for r in binding.requests)
        stamp[0]+=2;app.delivery_owner.step()
        assert sum(r['@type']=='resendMessages' for r in binding.requests)==1
        assert sum(r['@type']=='sendMessage' for r in binding.requests)==1
        # A second definitely failed native attempt loses retry authority on Pause.
        retried=Outbox(app.db).get(identifier)
        binding.events.extend([{'@client_id':42,'@type':'messages','@extra':retried['request_extra'],'messages':[{'@type':'message','id':-9,'chat_id':456,'sender_id':source}]},
            {'@client_id':42,'@type':'updateMessageSendFailed','old_message_id':-9,'message':{'id':-10,'chat_id':456,'sender_id':source,'sending_state':failure}}])
        app.delivery_owner.step();app.delivery_owner.step();app.session.pause()
        stamp[0]+=100
        for _ in range(5):app.delivery_owner.step()
        assert Outbox(app.db).get(identifier)['state']=='unknown'
        assert sum(r['@type']=='resendMessages' for r in binding.requests)==1
    finally:app.close()


def test_saved_account_reconnects_while_stopped_with_pristine_pending_but_cannot_send(tmp_path):
    from test_telegram_auth import state
    app,binding,_=assembled(tmp_path)
    ready(app,binding);identifier=stage_in_app(app);epoch=app.auth.snapshot().epoch
    app.features.account_features.cancel()
    for _ in range(3):app.delivery_owner.step()
    app.close()
    reopened,binding,_=assembled(tmp_path)
    try:
        assert reopened.telegram._client is None
        assert reopened.auth.snapshot().epoch==epoch
        assert Outbox(reopened.db).get(identifier)['state']=='pending'
        reopened.features.account_features.begin()
        binding.events.extend([state('WaitTdlibParameters'),state('Ready')])
        for _ in range(35):reopened.delivery_owner.step()
        assert reopened.auth.status=='ready' and reopened.telegram.transport_ready
        assert reopened.auth.snapshot().account_id=='123' and reopened.auth.snapshot().epoch==epoch
        assert reopened.session.snapshot['state']=='stopped'
        assert Outbox(reopened.db).get(identifier)['state']=='pending'
        assert not any(r['@type'] in {'sendMessage','resendMessages'} for r in binding.requests)
        reopened.session.start({'feed_url':'https://www.threads.com/','mode':'review'})
        reopened.delivery_owner.step('reconnect',Event())
        for _ in range(35):reopened.delivery_owner.step()
        assert Outbox(reopened.db).get(identifier)['state']=='sending'
        assert sum(r['@type']=='sendMessage' for r in binding.requests)==1
    finally:reopened.close()


@pytest.mark.parametrize('field',['attempt_id','request_extra','temporary_message_id','final_message_id'])
def test_saved_reconnect_rejects_pending_with_any_native_attempt_identity(tmp_path,field):
    import json
    app,binding,_=assembled(tmp_path)
    try:
        ready(app,binding);identifier=stage_in_app(app)
        app.features.account_features.cancel()
        for _ in range(3):app.delivery_owner.step()
        with app.db.transaction() as con:
            payload=json.loads(con.execute('SELECT payload FROM deliveries WHERE id=?',(identifier,)).fetchone()[0])
            payload[field]='unexpected-native-id'
            con.execute('UPDATE deliveries SET payload=? WHERE id=?',(json.dumps(payload),identifier))
        before=binding.created
        with pytest.raises(Exception):app.features.account_features.begin()
        assert binding.created==before and not app.telegram.transport_ready
    finally:app.close()


def test_final_nth_observation_completes_but_pause_during_last_inference_revokes(env):
    from test_routing import post
    session=running('review',max_posts=3)
    pipeline=Pipeline(session,Event(),PostStore(env.db),env.config,env.classifier,env.router,NoSend())
    for index in range(3):
        assert pipeline.process(post(post_id=f'threads:Cap{index}',permalink=f'https://www.threads.net/@synthetic/post/Cap{index}'))
    assert pipeline.process(env.post) is None
    assert session.snapshot['counters']['observed']==session.snapshot['counters']['matched']==3
    for operation in ('pause','stop'):
        current=running('review',max_posts=1)
        original=env.classifier.classify_bound
        class Interrupted:
            metadata = env.classifier.metadata
            def classify_bound(self,*args):
                result=original(*args);getattr(current,operation)();return result
        assert Pipeline(current,Event(),PostStore(env.db),env.config,Interrupted(),env.router,NoSend()).process(env.post) is None
        assert current.snapshot['counters']['matched']==0


def test_failed_local_cleanup_is_visible_bounded_and_explicitly_retryable(tmp_path):
    app,binding,broker=assembled(tmp_path)
    try:
        ready(app,binding)
        calls=[]
        def unavailable():calls.append(True);raise RuntimeError('fixture cleanup unavailable')
        app.delivery_owner.remove_local=unavailable
        app.delivery_owner.account_action('remove_local_offline')
        for _ in range(5):app.delivery_owner.step()
        assert len(calls)==1 and app.auth.status=='cleanup_pending'
        assert app.auth.snapshot().revocation_pending and broker.removed==0
        app.delivery_owner.remove_local=broker.remove_account
        app.delivery_owner.account_action('remove_local_offline')
        for _ in range(3):app.delivery_owner.step()
        assert broker.removed==1 and app.auth.status=='locally_removed'
    finally:app.close()


def test_confirmed_offline_cleanup_intent_survives_worker_reopen(tmp_path):
    app,binding,_=assembled(tmp_path)
    ready(app,binding)
    app.delivery_owner.account_action('remove_local_offline')
    # Simulate shutdown after persisted consent but before completion. No other
    # worker/client survives the fixture's confirmed close / profile release.
    app.close()
    reopened,binding,broker=assembled(tmp_path)
    try:
        for _ in range(3):reopened.delivery_owner.step()
        assert broker.removed==1 and reopened.auth.snapshot().account_id is None
        assert reopened.auth._accounts.pending_revocations()
        assert not binding.requests  # never initialize/connect for offline removal
    finally:reopened.close()


def test_application_retention_expires_body_but_preserves_inflight_correlation(tmp_path):
    app,binding,_=assembled(tmp_path)
    try:
        ready(app,binding);app.session.start({'feed_url':'https://www.threads.com/','mode':'review'});app.delivery_owner._running=Event()
        identifier=stage_in_app(app)
        for _ in range(12):app.delivery_owner.step()
        claim=Outbox(app.db).get(identifier)
        with app.db.transaction() as con:
            con.execute('UPDATE posts SET last_seen=? WHERE post_id=?',((datetime.now(timezone.utc)-timedelta(days=400)).timestamp(),claim['post_id']))
        app.features.save_retention({'text_days':7,'dedup_days':30,'diagnostic_days':7,'confirmed':True});app.maintenance(force=True)
        with app.db.connection() as con:
            assert con.execute('SELECT 1 FROM post_text WHERE post_id=?',(claim['post_id'],)).fetchone() is None
        assert Outbox(app.db).get(identifier)['state']=='sending'
        source={'@type':'messageSenderUser','user_id':123}
        binding.events.extend([{'@client_id':42,'@type':'message','@extra':claim['request_extra'],'id':-7,'chat_id':456,'sender_id':source},
            {'@client_id':42,'@type':'updateMessageSendSucceeded','old_message_id':-7,'message':{'id':8,'chat_id':456,'sender_id':source}}])
        app.delivery_owner.step();app.delivery_owner.step()
        assert Outbox(app.db).get(identifier)['state']=='sent'
    finally:app.close()


def test_late_retryable_outcome_after_pause_never_reacquires_retry_authority(tmp_path):
    app,binding,_=assembled(tmp_path)
    try:
        ready(app,binding);app.session.start({'feed_url':'https://www.threads.com/','mode':'review'});app.delivery_owner._running=Event()
        identifier=stage_in_app(app)
        for _ in range(12):app.delivery_owner.step()
        claim=Outbox(app.db).get(identifier);source={'@type':'messageSenderUser','user_id':123}
        binding.events.append({'@client_id':42,'@type':'message','@extra':claim['request_extra'],'id':-7,'chat_id':456,'sender_id':source})
        app.delivery_owner.step();app.session.pause()
        failure={'@type':'messageSendingStateFailed','can_retry':True,'retry_after':1.0,'need_another_sender':False,'need_another_reply_quote':False,'need_drop_reply':False,'required_paid_message_star_count':0}
        binding.events.append({'@client_id':42,'@type':'updateMessageSendFailed','old_message_id':-7,'message':{'id':-8,'chat_id':456,'sender_id':source,'sending_state':failure}})
        for _ in range(3):app.delivery_owner.step()
        assert Outbox(app.db).get(identifier)['state']=='unknown'
        assert not app.sender._retry_evidence
        assert not any(r['@type']=='resendMessages' for r in binding.requests)
    finally:app.close()


def test_auto_route_under_auto_and_dry_run_modes_has_truthful_receipts(env):
    from test_routing import post
    env.config.save_route(route(version=2,mode='auto'))
    for mode in ('auto','dry_run'):
        current=running(mode);sender=NoSend()
        p=post(post_id='threads:Mode'+mode,permalink='https://www.threads.net/@synthetic/post/Mode'+mode)
        result=Pipeline(current,Event(),PostStore(env.db),env.config,env.classifier,env.router,sender).process(p)
        with env.db.connection() as con:
            deliveries=con.execute('SELECT COUNT(*) FROM deliveries WHERE post_id=?',(p['post_id'],)).fetchone()[0]
            receipts=con.execute('SELECT COUNT(*) FROM routing_plans WHERE post_id=?',(p['post_id'],)).fetchone()[0]
        if mode=='auto':
            assert result and not result[0]['approval_required'] and deliveries==receipts==1
        else:
            assert result is None and deliveries==receipts==0 and sender.calls==0


def test_logout_never_enables_network_with_unacknowledged_initialization(tmp_path):
    import pytest
    app,binding,_=assembled(tmp_path)
    try:
        ready(app,binding)
        before=sum(r.get('type',{}).get('@type')=='networkTypeOther' for r in binding.requests)
        app.telegram._client._config_pending.add('fixture-unacknowledged')
        with pytest.raises(Exception):app.delivery_owner.account_action('server_logout')
        assert app.auth.snapshot().revocation_pending
        assert sum(r.get('type',{}).get('@type')=='networkTypeOther' for r in binding.requests)==before
        assert not any(r['@type']=='logOut' for r in binding.requests)
    finally:app.close()


def test_owned_evaluator_receipt_callback_binds_exact_bytes_and_never_synthetic(tmp_path):
    from contextlib import contextmanager
    from test_evaluation import dataset,write_jsonl,native_runtime
    from threadrelay.inference.decision import Classifier
    from threadrelay.evaluation.runner import main
    @contextmanager
    def factory(_manifest):
        runtime=native_runtime([{'research':1,'art':0}])
        yield Classifier(runtime)
    for synthetic in (True,False):
        # DI-only fixture tests receipt plumbing, never a real benchmark or
        # installed host receipt. Repository datasets remain marked synthetic.
        data={**dataset(),'synthetic':synthetic}
        path=tmp_path/f'fixture-{synthetic}.jsonl';output=tmp_path/f'fixture-{synthetic}.json'
        write_jsonl(path,data);captured=[]
        assert main(['--dataset',str(path),'--output',str(output)],runtime_factory=factory,trusted_report=captured.append)==0
        assert captured==([] if synthetic else [output.read_bytes()])


def test_owner_destination_refresh_timeout_never_requeries_or_sends(tmp_path):
    app,binding,_=assembled(tmp_path)
    try:
        ready(app,binding);app.session.start({'feed_url':'https://www.threads.com/','mode':'review'});app.delivery_owner._running=Event()
        identifier=stage_in_app(app);original=binding.send
        def lost(client,request):
            original(client,request)
            if request['@type']=='getChat':binding.events[:]=[e for e in binding.events if e.get('@extra')!=request.get('@extra')]
        binding.send=lost
        stamp=[app.clock.monotonic()];app.sender.monotonic=lambda:stamp[0]
        app.delivery_owner.step();stamp[0]+=31
        for _ in range(5):app.delivery_owner.step()
        assert app.session.snapshot['state']!='running'
        assert Outbox(app.db).get(identifier)['state']=='pending'
        assert sum(r['@type']=='getChat' for r in binding.requests)==1
        assert not any(r['@type']=='sendMessage' for r in binding.requests)
    finally:app.close()


@pytest.mark.parametrize('response', ['ok_only', 'connecting_then_ready'])
def test_first_start_keeps_the_already_ready_native_connection(tmp_path, response):
    # Both legitimate TDLib responses to a redundant network reset used to
    # strand Pending or revoke Running. Neither reset should be requested.
    app,binding,_=assembled(tmp_path / response)
    try:
        ready(app,binding)
        identifier=stage_in_app(app)
        before=sum(r.get('type',{}).get('@type')=='networkTypeOther' for r in binding.requests)
        original_send=binding.send
        def send(client, request):
            offset=len(binding.events)
            original_send(client, request)
            if request.get('type',{}).get('@type')=='networkTypeOther':
                if response=='ok_only':
                    binding.events[offset:]=[e for e in binding.events[offset:] if e.get('@type')!='updateConnectionState']
                else:
                    binding.events.insert(offset+1,{'@client_id':42,'@type':'updateConnectionState','state':{'@type':'connectionStateConnecting'}})
        binding.send=send
        app.session.start({'feed_url':'https://www.threads.com/','mode':'review'})
        app.delivery_owner.step('reconnect',Event())
        for _ in range(40):app.delivery_owner.step()
        assert app.session.snapshot['state']=='running'
        assert app.telegram.transport_ready
        assert Outbox(app.db).get(identifier)['state']=='sending'
        assert sum(r['@type']=='sendMessage' for r in binding.requests)==1
        assert sum(r.get('type',{}).get('@type')=='networkTypeOther' for r in binding.requests)==before
        # Repeating collection activation grants no new network or send permit.
        app.sender.enable_running_network()
        assert app.telegram.transport_ready
        assert sum(r['@type']=='sendMessage' for r in binding.requests)==1
        assert sum(r.get('type',{}).get('@type')=='networkTypeOther' for r in binding.requests)==before
    finally:app.close()


@pytest.mark.parametrize('phase', ['before_parameters', 'before_identity', 'after_identity'])
def test_saved_bootstrap_accepts_initial_offline_before_and_after_identity(tmp_path, phase):
    from test_telegram_auth import state
    app,binding,_=assembled(tmp_path / phase)
    try:
        ready(app,binding)
        identifier=stage_in_app(app)
        app.features.account_features.cancel()
        for _ in range(3):app.delivery_owner.step()
        assert app.auth.status=='closed'
        original_send=binding.send
        offline={'@client_id':42,'@type':'updateConnectionState','state':{'@type':'connectionStateWaitingForNetwork'}}
        def send(client,request):
            offset=len(binding.events)
            original_send(client,request)
            if request['@type']=='getMe' and phase in {'before_identity','after_identity'}:
                binding.events.insert(offset+(phase=='after_identity'),offline)
        binding.send=send
        app.session.start({'feed_url':'https://www.threads.com/','mode':'review'})
        app.delivery_owner.step('reconnect',Event())
        if phase=='before_parameters':binding.events.append(offline)
        binding.events.extend([state('WaitTdlibParameters'),state('Ready')])
        for _ in range(40):app.delivery_owner.step()
        assert app.session.snapshot['state']=='running'
        assert app.auth.status=='ready' and app.telegram.transport_ready
        assert Outbox(app.db).get(identifier)['state']=='sending'
        assert sum(r['@type']=='sendMessage' for r in binding.requests)==1
        # Initial bootstrap is not a permanent exemption from actual loss.
        binding.events.append(offline)
        app.delivery_owner.step()
        assert app.session.snapshot['attention_reason']=='telegram_authority_changed'
        assert not app.telegram.transport_ready
    finally:app.close()


@pytest.mark.parametrize('interruption', ['epoch_change', 'online_then_offline', 'parameters_error', 'enable_error', 'pause'])
def test_saved_bootstrap_exception_never_survives_lost_authority(tmp_path, interruption):
    from test_telegram_auth import state
    app,binding,_=assembled(tmp_path)
    try:
        ready(app,binding)
        identifier=stage_in_app(app)
        app.features.account_features.cancel()
        for _ in range(3):app.delivery_owner.step()
        app.session.start({'feed_url':'https://www.threads.com/','mode':'review'})
        app.delivery_owner.step('reconnect',Event())
        offline={'@client_id':42,'@type':'updateConnectionState','state':{'@type':'connectionStateWaitingForNetwork'}}
        if interruption=='epoch_change':
            app.auth._accounts.invalidate_epoch('123')
            binding.events.append(offline)
        elif interruption=='online_then_offline':
            binding.events.extend([{'@client_id':42,'@type':'updateConnectionState','state':{'@type':'connectionStateReady'}},offline])
        elif interruption=='pause':
            app.session.pause('user')
            binding.events.append(offline)
        original_send=binding.send
        def send(client,request):
            offset=len(binding.events)
            original_send(client,request)
            if ((interruption=='parameters_error' and request['@type']=='setTdlibParameters')
                or (interruption=='enable_error' and request.get('type',{}).get('@type')=='networkTypeOther')):
                binding.events[offset:]=[{'@client_id':42,'@type':'error','@extra':request['@extra'],'code':500,'message':'synthetic failure'}]
        binding.send=send
        if interruption in {'epoch_change','online_then_offline'}:
            for _ in range(2):app.delivery_owner.step()
            assert app.session.snapshot['attention_reason']=='telegram_authority_changed'
        binding.events.extend([state('WaitTdlibParameters'),state('Ready')])
        if interruption=='epoch_change':
            from threadrelay.delivery.auth import AuthFailure
            with pytest.raises(AuthFailure, match='Saved account generation changed'):
                for _ in range(40):app.delivery_owner.step()
        else:
            for _ in range(40):app.delivery_owner.step()
        assert not app.telegram.transport_ready
        assert Outbox(app.db).get(identifier)['state']==('cancelled' if interruption=='epoch_change' else 'pending')
        assert not any(r['@type']=='sendMessage' for r in binding.requests)
        if interruption=='pause':assert app.session.snapshot['state']=='paused'
    finally:app.close()
