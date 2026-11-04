"""Stage 8 local feature boundary; synthetic local data only."""
from test_routing import env as env
from datetime import datetime, timezone
from threadrelay.main import dispatch
from threadrelay.storage.database import Database
from threadrelay.features import LocalFeatures


def command(kind, payload=None):
    return {'version': 1, 'id': 'fixture', 'type': kind, 'payload': payload or {}}


def test_local_topics_are_persisted_and_queried_through_validated_ipc(tmp_path):
    with Database(tmp_path / 'app.db') as db:
        service = LocalFeatures(db)
        topic = {'id': 'topic-a', 'version': 1, 'description': 'Local sample',
                 'positive_examples': ['yes'], 'negative_examples': ['no'], 'exclusions': [], 'enabled': True}
        assert dispatch(command('QueryTopics'), features=service)['payload'] == []
        assert dispatch(command('SaveTopic', topic), features=service)['type'] == 'Acknowledged'
        assert dispatch(command('QueryTopics'), features=service)['payload'] == [topic]
        conflicting = {**topic, 'description': 'Changed without a new version'}
        assert dispatch(command('SaveTopic', conflicting), features=service)['type'] == 'Error'
        assert dispatch(command('QueryTopics'), features=service)['payload'] == [topic]


def test_history_query_does_not_invent_activity(tmp_path):
    with Database(tmp_path / 'app.db') as db:
        result = dispatch(command('QueryHistory'), features=LocalFeatures(db))
        assert result['type'] == 'History'
        assert result['payload'] == []


def test_default_feature_commands_are_honestly_unsupported():
    assert dispatch(command('QueryTopics'))['type'] == 'Error'


def test_account_query_is_inert_and_secure_submission_rejects_stale_epoch(tmp_path):
    from test_telegram_auth import build, state
    from threadrelay.features import AccountFeatures
    from threadrelay.delivery.auth import AuthFailure
    import pytest
    db, binding, client, handoff, auth = build(tmp_path)
    adapter = AccountFeatures(auth, vault_reference=lambda: 'vault:fixture', native_gate=lambda: True, discard_secret_ref=lambda ref: handoff.values.pop(ref, None), wall_clock=lambda:datetime.now(timezone.utc))
    service = LocalFeatures(db, account=adapter)
    result = dispatch(command('QueryAccount'), features=service)
    assert result['payload']['snapshot']['auth_state'] == 'logged_out'
    assert binding.created == 0
    assert dispatch(command('BeginTelegramLogin'), features=service)['type'] == 'Acknowledged'
    binding.events.append(state('WaitCode')); auth.poll()
    challenge = auth.challenge()
    ref = handoff.put('synthetic')
    with pytest.raises(AuthFailure):
        adapter.submit_secure(challenge.challenge_id, 'code', 99, ref)
    assert ref not in handoff.values
    ref = handoff.put('synthetic')
    adapter.submit_secure(challenge.challenge_id, 'code', auth.snapshot().epoch, ref)
    assert ref not in handoff.values
    db.close()


def test_review_projection_and_staging_are_bound_to_current_disclosure(env):
    from dataclasses import asdict
    from test_routing import plan
    from threadrelay.storage.outbox import Outbox
    service = LocalFeatures(env.db, router=env.router)
    proposal = plan(env)[0]
    event = dispatch(command('QueryReview'), features=service)
    assert event['type'] == 'Review'
    item = event['payload'][0]
    assert item['proposal'] == proposal
    assert item['outgoing_text'] == env.post['permalink']
    assert item['classification']['model_version']
    assert item['topics'] == [{'id':'science','version':1,'description':'Synthetic research'}]
    disclosure = asdict(env.approvals.disclosure(proposal))
    bad = {**disclosure, 'audience': 'public'}
    assert dispatch(command('ApproveReview', {'proposal': proposal, 'disclosure': bad}), features=service)['type'] == 'Error'
    assert dispatch(command('ApproveReview', {'proposal': proposal, 'disclosure': disclosure}), features=service)['type'] == 'Acknowledged'
    history = dispatch(command('QueryHistory'), features=service)['payload']
    assert len(history) == 1 and history[0]['delivery']['state'] == 'pending'
    assert Outbox(env.db).get(history[0]['delivery']['id'])['state'] == 'pending'
    assert dispatch(command('QueryReview'), features=service)['payload'] == []


def test_stale_route_invalidates_review_and_renderer_cannot_enable_auto(env):
    from dataclasses import asdict
    from test_routing import plan, route
    service = LocalFeatures(env.db, router=env.router)
    proposal = plan(env)[0]
    disclosure = asdict(env.approvals.disclosure(proposal))
    env.config.save_route(route(version=2, enabled=False))
    assert dispatch(command('QueryReview'), features=service)['payload'] == []
    assert dispatch(command('ApproveReview', {'proposal': proposal, 'disclosure': disclosure}), features=service)['type'] == 'Error'
    assert dispatch(command('SaveRoute', route(version=3, mode='auto')), features=service)['type'] == 'Error'


def test_secure_submission_fails_closed_when_native_gate_is_revoked(tmp_path):
    from test_telegram_auth import build, state
    from threadrelay.features import AccountFeatures
    import pytest
    db, binding, client, handoff, auth = build(tmp_path)
    gate=[True]
    adapter=AccountFeatures(auth,vault_reference=lambda:'vault:fixture',native_gate=lambda:gate[0],discard_secret_ref=lambda ref:handoff.values.pop(ref,None),wall_clock=lambda:datetime.now(timezone.utc))
    adapter.begin(); binding.events.append(state('WaitCode')); auth.poll()
    challenge=auth.challenge(); ref=handoff.put('synthetic'); before=len(binding.requests);gate[0]=False
    with pytest.raises(NotImplementedError): adapter.submit_secure(challenge.challenge_id,'code',auth.snapshot().epoch,ref)
    assert ref not in handoff.values and len(binding.requests)==before
    db.close()


def test_query_account_reports_prior_pending_revocation_after_switch(tmp_path):
    from threadrelay.storage.outbox import AccountStore
    from threadrelay.features import AccountFeatures
    from test_telegram_auth import FixtureBinding, FixtureVault, MemorySecretHandoff
    from threadrelay.delivery.auth import AuthController
    from threadrelay.delivery.telegram import TelegramClient
    with Database(tmp_path/'app.db') as db:
        accounts=AccountStore(db)
        def snapshot(id):
            return {'account_id':id,'epoch':1,'auth_state':'ready','display_name':None,'network_state':'offline','revocation_pending':False}
        accounts.activate(snapshot('123')); accounts.activate(snapshot('456'))
        client=TelegramClient(lambda:FixtureBinding(),FixtureVault(),tmp_path/'sessions')
        auth=AuthController(client,accounts,MemorySecretHandoff(),clock=lambda:0)
        adapter=AccountFeatures(auth,vault_reference=lambda:'vault:fixture',native_gate=lambda:False,discard_secret_ref=lambda _:None,wall_clock=lambda:datetime.now(timezone.utc))
        result=dispatch(command('QueryAccount'),features=LocalFeatures(db,account=adapter))
        assert result['payload']['snapshot']['account_id']=='456'
        assert result['payload']['pending_revocations']==['123']


def test_oversized_local_query_returns_static_error_without_breaking_ipc():
    from io import BytesIO
    from threadrelay.main import serve_one, encode_frame, read_frame
    class Huge:
        def topics(self):
            return [{'id':str(i),'version':1,'description':'x'*10000,'positive_examples':['y'*100000], 'negative_examples':[], 'exclusions':[], 'enabled':True} for i in range(12)]
        def routes(self): return []
        def history(self): return []
        def review(self): return []
        def account(self): return {}
    source=BytesIO(encode_frame(command('QueryTopics'))); target=BytesIO()
    assert serve_one(source,target,lambda c:dispatch(c,features=Huge()))
    target.seek(0); result=read_frame(target)
    assert result['type']=='Error' and 'x'*100 not in str(result)


def test_destination_verification_uses_single_auth_receive_and_explicit_query(tmp_path):
    from test_telegram_auth import FixtureBinding, FixtureVault, MemorySecretHandoff, state
    from threadrelay.delivery.auth import AuthController
    from threadrelay.delivery.telegram import TelegramClient, NetworkPermit
    from threadrelay.delivery.destinations import DestinationVerifier
    from threadrelay.features import DestinationFeatures
    from threadrelay.storage.outbox import AccountStore
    from threadrelay.storage.routing import ConfigStore
    from test_routing import running
    with Database(tmp_path/'app.db') as db:
        binding=FixtureBinding(); client=TelegramClient(lambda:binding,FixtureVault(),tmp_path/'sessions'); session=running()
        auth=AuthController(client,AccountStore(db),MemorySecretHandoff(),clock=lambda:0,lifecycle_lock=session.authority_lock)
        auth.begin('vault:fixture',pending=False); binding.events.append(state('WaitTdlibParameters'));auth.poll()
        for req in binding.requests[:]:
            if req.get('@extra','').startswith('init:'):
                binding.events.append({'@client_id':42,'@type':'ok','@extra':req['@extra']});auth.poll()
        auth.enable_clean_auth(NetworkPermit('clean_auth',0,False));binding.events.append({'@client_id':42,'@type':'ok','@extra':binding.requests[-1]['@extra']});auth.poll()
        binding.events.extend([state('Ready'),{'@client_id':42,'@type':'user','@extra':'auth:get-me','id':123,'type':{'@type':'userTypeRegular'}},{'@client_id':42,'@type':'updateConnectionState','state':{'@type':'connectionStateReady'}}]);auth.poll();auth.poll();auth.poll()
        verifier=DestinationVerifier(client,auth,ConfigStore(db),session)
        service=LocalFeatures(db,destination=DestinationFeatures(verifier,native_gate=lambda:True))
        assert dispatch(command('VerifyDestination',{'chat_id':'456','kind':'private','sender':None}),features=service)['type']=='Acknowledged'
        assert dispatch(command('QueryDestination'),features=service)['payload']['state']=='pending'
        req=binding.requests[-1];binding.events.append({'@client_id':42,'@type':'chat','@extra':req['@extra'],'id':456,'title':'Fixture','type':{'@type':'chatTypePrivate','user_id':789},'message_sender_id':{'@type':'messageSenderUser','user_id':123}});auth.poll()
        req=binding.requests[-1];binding.events.append({'@client_id':42,'@type':'canSendMessageToUserResultOk','@extra':req['@extra']});auth.poll()
        status=dispatch(command('QueryDestination'),features=service)['payload']
        assert status['state']=='verified' and status['snapshot']['chat_id']=='456'


def test_destination_query_fails_closed_when_gate_or_live_receipt_disappears():
    from types import SimpleNamespace
    from threading import RLock
    from threadrelay.features import DestinationFeatures
    gate=[True]
    verifier=SimpleNamespace(session=SimpleNamespace(authority_lock=RLock()),evidence=(1,1,{}),pending=None)
    adapter=DestinationFeatures(verifier,native_gate=lambda:gate[0]); adapter.phase='verified'; adapter.snapshot={'fixture':True}
    gate[0]=False
    assert adapter.query()=={'state':'failed','snapshot':None}


def test_dedicated_threads_login_only_opens_after_explicit_command(tmp_path):
    from threadrelay.features import ThreadsFeatures
    class Browser:
        def __init__(self): self.paths=[]
        def open(self,path): self.paths.append(path)
    browser=Browser()
    with Database(tmp_path/'app.db') as db:
        service=LocalFeatures(db,threads=ThreadsFeatures(browser, app_data_dir=tmp_path))
        assert dispatch(command('QueryTopics'),features=service)['type']=='Topics'
        assert browser.paths==[]
        assert dispatch(command('OpenThreadsLogin'),features=service)['type']=='Acknowledged'
        assert browser.paths==[tmp_path/'browser-profiles'/'threads']


def test_secure_handoff_disposal_failure_is_static_and_not_success(tmp_path):
    from test_telegram_auth import build, state
    from threadrelay.features import AccountFeatures
    from threadrelay.delivery.auth import AuthFailure
    import pytest
    db,binding,client,handoff,auth=build(tmp_path)
    def broken(_): raise RuntimeError('private vault failure')
    adapter=AccountFeatures(auth,vault_reference=lambda:'vault:fixture',native_gate=lambda:True,discard_secret_ref=broken,wall_clock=lambda:datetime.now(timezone.utc))
    adapter.begin();binding.events.append(state('WaitCode'));auth.poll()
    ref=handoff.put('synthetic');challenge=auth.challenge()
    with pytest.raises(AuthFailure,match='Secure input unavailable') as exc: adapter.submit_secure(challenge.challenge_id,'code',auth.snapshot().epoch,ref)
    assert 'private vault failure' not in str(exc.value)
    db.close()


def test_account_projection_derives_utc_expiry_from_remaining_monotonic_ttl(tmp_path):
    from datetime import datetime, timezone, timedelta
    from test_telegram_auth import FixtureBinding, FixtureVault, MemorySecretHandoff, state
    from threadrelay.features import AccountFeatures
    from threadrelay.delivery.auth import AuthController
    from threadrelay.delivery.telegram import TelegramClient
    from threadrelay.storage.outbox import AccountStore
    now=[0.0];wall=datetime(2026,1,1,tzinfo=timezone.utc);binding=FixtureBinding()
    with Database(tmp_path/'app.db') as db:
        auth=AuthController(TelegramClient(lambda:binding,FixtureVault(),tmp_path/'sessions'),AccountStore(db),MemorySecretHandoff(),clock=lambda:now[0],challenge_ttl=5)
        adapter=AccountFeatures(auth,vault_reference=lambda:'vault:fixture',native_gate=lambda:True,discard_secret_ref=lambda _:None,wall_clock=lambda:wall)
        adapter.begin();binding.events.append(state('WaitCode'));auth.poll()
        now[0]=2.0
        projected=adapter.query()['challenge']
        assert datetime.fromisoformat(projected['expires_at'])==wall+timedelta(seconds=3)
        now[0]=5.0
        assert adapter.query()['challenge'] is None


def test_local_disable_preserves_identity_and_revokes_queued_route_authority(env):
    from test_routing import route, plan, approved, running
    from threadrelay.storage.outbox import Outbox, AccountStore
    service=LocalFeatures(env.db,router=env.router); proposal=plan(env)[0]
    delivery_id=env.router.stage(proposal,approval=approved(env,proposal))
    original=route(); disabled={**original,'version':2,'enabled':False}
    assert dispatch(command('SaveRoute',disabled),features=service)['type']=='Acknowledged'
    assert service.routes()==[disabled]
    assert env.router.authorize(Outbox(env.db).get(delivery_id),running())['allowed'] is False
    # The same local revocation path does not require a ready account.
    AccountStore(env.db).suspend_if_current(original['account_id'],original['account_epoch'])
    disabled_again={**disabled,'version':3}
    assert dispatch(command('SaveRoute',disabled_again),features=service)['type']=='Acknowledged'
    assert service.routes()==[disabled_again]


def test_account_expiry_projection_samples_wall_before_final_monotonic_remaining(tmp_path):
    from datetime import datetime, timezone, timedelta
    from test_telegram_auth import FixtureBinding, FixtureVault, MemorySecretHandoff, state
    from threadrelay.features import AccountFeatures
    from threadrelay.delivery.auth import AuthController
    from threadrelay.delivery.telegram import TelegramClient
    from threadrelay.storage.outbox import AccountStore
    mono=[0.0];wall=datetime(2026,1,1,tzinfo=timezone.utc);binding=FixtureBinding()
    with Database(tmp_path/'app.db') as db:
        auth=AuthController(TelegramClient(lambda:binding,FixtureVault(),tmp_path/'sessions'),AccountStore(db),MemorySecretHandoff(),clock=lambda:mono[0],challenge_ttl=5)
        def wall_clock(): mono[0]=3.0;return wall
        adapter=AccountFeatures(auth,vault_reference=lambda:'vault:fixture',native_gate=lambda:True,discard_secret_ref=lambda _:None,wall_clock=wall_clock)
        adapter.begin();binding.events.append(state('WaitCode'));auth.poll();mono[0]=2.0
        assert datetime.fromisoformat(adapter.query()['challenge']['expires_at'])==wall+timedelta(seconds=2)
