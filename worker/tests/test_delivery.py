"""Synthetic TDLib delivery boundary tests; no native account or network."""
from test_routing import env as env

from threadrelay.delivery.telegram import TelegramClient
from test_telegram_auth import FixtureBinding, FixtureVault


def pump_delivery(client):
    event = client.take_delivery_event()
    if event is not None:
        return event
    client.authorization_event()
    return client.take_delivery_event()


def test_correlated_response_is_not_success_and_success_update_wins_out_of_order(tmp_path):
    binding = FixtureBinding()
    client = TelegramClient(lambda: binding, FixtureVault(), tmp_path / 'sessions')
    client.initialize_offline('vault:test')
    client.register_attempt(delivery_id='delivery', attempt_id='attempt', request_extra='extra',
        account_id='123', epoch=2, chat_id='456', sender={'kind':'user','id':'123'},
        destination_version=1, content_hash='abc')
    # TDLib update can precede the response carrying the temporary ID.
    binding.events.append({'@client_id':42, '@type':'updateMessageSendSucceeded', 'old_message_id':-7,
        'message':{'id':8,'chat_id':456,'sender_id':{'@type':'messageSenderUser','user_id':123},'content':{'secret':'discard'}}})
    assert pump_delivery(client) is None
    binding.events.append({'@client_id':42,'@type':'message','@extra':'extra','id':-7,'chat_id':456,
        'sender_id':{'@type':'messageSenderUser','user_id':123},'content':{'secret':'discard'}})
    first = pump_delivery(client)
    second = pump_delivery(client)
    assert first.kind == 'initiated' and first.temporary_message_id == '-7'
    assert second.kind == 'succeeded' and second.final_message_id == '8'
    assert 'secret' not in repr(first) + repr(second)


def test_failure_and_delete_are_bounded_to_exact_own_temporary_id(tmp_path):
    binding = FixtureBinding(); client = TelegramClient(lambda: binding, FixtureVault(), tmp_path / 'sessions')
    client.initialize_offline('vault:test')
    client.register_attempt(delivery_id='d', attempt_id='a', request_extra='x', account_id='123', epoch=2,
        chat_id='456', sender={'kind':'user','id':'123'}, destination_version=1, content_hash='hash')
    binding.events.append({'@client_id':42,'@type':'message','@extra':'x','id':-7,'chat_id':456,
        'sender_id':{'@type':'messageSenderUser','user_id':123}})
    assert pump_delivery(client).kind == 'initiated'
    binding.events.append({'@client_id':42,'@type':'updateDeleteMessages','chat_id':999,'message_ids':[-7]})
    assert pump_delivery(client) is None
    binding.events.append({'@client_id':42,'@type':'updateMessageSendFailed','old_message_id':-7,
        'message':{'id':-8,'chat_id':456,'sender_id':{'@type':'messageSenderUser','user_id':123},
            'sending_state':{'@type':'messageSendingStateFailed','can_retry':True,'retry_after':2.0,
                'need_another_sender':False,'need_another_reply_quote':False,'need_drop_reply':False,'required_paid_message_star_count':0}},
        'error':{'message':'secret error'}})
    event = pump_delivery(client)
    assert event.kind == 'failed' and event.failed_message_id == '-8' and event.can_retry and event.retry_after == 2.0
    assert 'secret error' not in repr(event)
    binding.events.append({'@client_id':42,'@type':'updateMessageSendFailed','old_message_id':-7})
    assert pump_delivery(client) is None


def test_delete_before_confirmation_is_unknown(tmp_path):
    binding = FixtureBinding(); client = TelegramClient(lambda: binding, FixtureVault(), tmp_path / 'sessions')
    client.initialize_offline('vault:test')
    client.register_attempt(delivery_id='d', attempt_id='a', request_extra='x', account_id='123', epoch=2,
        chat_id='456', sender={'kind':'user','id':'123'}, destination_version=1, content_hash='hash')
    binding.events.append({'@client_id':42,'@type':'message','@extra':'x','id':-7,'chat_id':456,
        'sender_id':{'@type':'messageSenderUser','user_id':123}})
    pump_delivery(client)
    binding.events.append({'@client_id':42,'@type':'updateDeleteMessages','chat_id':456,'message_ids':[-7], 'from_cache':False})
    assert pump_delivery(client).kind == 'unknown'


def test_sender_claims_then_initiates_under_current_authorities(tmp_path):
    from test_routing import ScoresRuntime, topic, route, post, plan, running, approved
    from types import SimpleNamespace
    from datetime import datetime, timezone
    from threadrelay.delivery.auth import AuthController
    from threadrelay.delivery.telegram import NetworkPermit
    from test_telegram_auth import MemorySecretHandoff, state
    from threadrelay.storage.database import Database
    from threadrelay.storage.outbox import AccountStore, Outbox
    from threadrelay.storage.routing import ConfigStore
    from threadrelay.storage.posts import PostStore
    from threadrelay.routing.policy import Router
    from threadrelay.routing.approvals import ApprovalStore
    from threadrelay.inference.decision import Classifier
    from threadrelay.delivery.sender import Sender
    from threadrelay.delivery.destinations import DestinationVerifier
    now = datetime(2026, 10, 28, tzinfo=timezone.utc)
    with Database(tmp_path / 'app.db', clock=lambda: now) as db:
        binding = FixtureBinding(); client = TelegramClient(lambda: binding, FixtureVault(), tmp_path / 'sessions')
        session = running()
        auth = AuthController(client, AccountStore(db), MemorySecretHandoff(), clock=lambda: 0.0, lifecycle_lock=session.authority_lock)
        auth.begin('vault:test', pending=False)
        binding.events.append(state('WaitTdlibParameters')); auth.poll()
        for request in binding.requests[:]:
            extra = request.get('@extra', '')
            if extra.startswith('init:'):
                binding.events.append({'@client_id':42, '@type':'ok', '@extra':extra})
                auth.poll()
        auth.enable_clean_auth(NetworkPermit('clean_auth', 0, False))
        binding.events.append({'@client_id':42,'@type':'ok','@extra':binding.requests[-1]['@extra']}); auth.poll()
        binding.events.extend([state('Ready'), {'@client_id':42,'@type':'user','@extra':'auth:get-me','id':123,'type':{'@type':'userTypeRegular'}},
            {'@client_id':42,'@type':'updateConnectionState','state':{'@type':'connectionStateReady'}}])
        auth.poll(); auth.poll(); auth.poll()
        config = ConfigStore(db); config.save_topic(topic())
        sender = {'kind':'user','id':'123'}
        config.save_route(route(destination_id='456',account_id='123',account_epoch=0,sender=sender))
        verifier = DestinationVerifier(client, auth, config, session)
        verifier.begin_private('456')
        check = binding.requests[-1]
        binding.events.append({'@client_id':42,'@type':'chat','@extra':check['@extra'],'id':456,'title':'Synthetic target',
            'type':{'@type':'chatTypePrivate','user_id':789},'message_sender_id':{'@type':'messageSenderUser','user_id':123}}); auth.poll()
        check = binding.requests[-1]
        binding.events.append({'@client_id':42,'@type':'canSendMessageToUserResultOk','@extra':check['@extra']}); auth.poll()
        verifier.finish()
        p = post(quote={'author':'quoted-fixture','text':'quoted fixture prose','permalink':None}); PostStore(db).upsert(p); classifier = Classifier(ScoresRuntime()); router = Router(db,classifier=classifier)
        env = SimpleNamespace(db=db, config=config, post=p, classifier=classifier, router=router, approvals=ApprovalStore(router))
        proposal = plan(env)[0]
        from threadrelay.features import LocalFeatures
        preview = LocalFeatures(db, router=router).review()[0]['outgoing_text']
        delivery_id = router.stage(proposal, approval=approved(env, proposal))
        delivery = Sender(router, session, auth, client, destination_authority=verifier, clock=lambda: now)
        result = delivery.start_next('worker')
        assert result is not None and Outbox(db).get(delivery_id)['state'] == 'sending'
        req = binding.requests[-1]
        assert req['@type'] == 'sendMessage' and req['chat_id'] == 456 and req['topic_id'] is None
        assert req['options']['paid_message_star_count'] == 0 and req['options']['allow_paid_broadcast'] is False
        assert req['input_message_content']['clear_draft'] is False
        # The approved default is the canonical link only, never copied prose.
        assert req['input_message_content']['text']['text'] == preview == p['permalink']
        assert p['text'] not in preview and p['quote']['text'] not in preview
        assert Outbox(db).attempts(delivery_id)[0]['request_extra'] == req['@extra']
        binding.events.append({'@client_id':42,'@type':'message','@extra':req['@extra'],'id':-7,'chat_id':456,
            'sender_id':{'@type':'messageSenderUser','user_id':123}})
        delivery.poll()
        assert Outbox(db).get(delivery_id)['state'] == 'sending'
        assert Outbox(db).get(delivery_id)['temporary_message_id'] == '-7'
        # The application-owned receive actor continues resolving a previously
        # initiated attempt after intake/network authority is paused.
        from threadrelay.delivery.owner import DeliveryOwner
        import time
        session.pause()
        receive_owner = DeliveryOwner(delivery, SimpleNamespace(_client=client), session)
        binding.events.append({'@client_id':42,'@type':'updateMessageSendSucceeded','old_message_id':-7,
            'message':{'id':8,'chat_id':456,'sender_id':{'@type':'messageSenderUser','user_id':123}}})
        deadline = time.monotonic() + 2
        while Outbox(db).get(delivery_id)['state'] != 'sent' and time.monotonic() < deadline:
            time.sleep(.01)
        assert receive_owner.close()
        assert Outbox(db).get(delivery_id)['state'] == 'sent'
        assert Outbox(db).get(delivery_id)['final_message_id'] == '8'
        assert not any(r['@type'] in {'getChatHistory','getContacts','downloadFile','viewMessages'} for r in binding.requests)
        session.stop()
        assert client.transport_ready and auth.status=='ready'
        before=len(binding.requests)
        session.start({'feed_url':'https://www.threads.net/','mode':'review'})
        assert client.transport_ready and len(binding.requests)==before


def test_explicit_private_destination_setup_requires_zero_paid_server_evidence(tmp_path):
    from threadrelay.delivery.destinations import DestinationVerifier
    from threadrelay.delivery.auth import AuthController
    from threadrelay.delivery.telegram import NetworkPermit
    from test_telegram_auth import MemorySecretHandoff, state
    from threadrelay.storage.database import Database
    from threadrelay.storage.outbox import AccountStore
    from threadrelay.storage.routing import ConfigStore
    from threadrelay.session import SessionController
    with Database(tmp_path / 'app.db') as db:
        session = SessionController(); binding = FixtureBinding()
        client = TelegramClient(lambda: binding, FixtureVault(), tmp_path / 'sessions')
        auth = AuthController(client, AccountStore(db), MemorySecretHandoff(), clock=lambda: 0, lifecycle_lock=session.authority_lock)
        auth.begin('vault:test', pending=False)
        binding.events.append(state('WaitTdlibParameters')); auth.poll()
        for request in binding.requests[:]:
            if request.get('@extra','').startswith('init:'):
                binding.events.append({'@client_id':42,'@type':'ok','@extra':request['@extra']}); auth.poll()
        auth.enable_clean_auth(NetworkPermit('clean_auth', 0, False))
        binding.events.append({'@client_id':42,'@type':'ok','@extra':binding.requests[-1]['@extra']}); auth.poll()
        binding.events.extend([state('Ready'), {'@client_id':42,'@type':'user','@extra':'auth:get-me','id':123,'type':{'@type':'userTypeRegular'}},
            {'@client_id':42,'@type':'updateConnectionState','state':{'@type':'connectionStateReady'}}])
        auth.poll(); auth.poll(); auth.poll()
        verifier = DestinationVerifier(client, auth, ConfigStore(db), session)
        verifier.begin_private('456')
        chat_request = binding.requests[-1]
        assert chat_request['@type'] == 'getChat'
        binding.events.append({'@client_id':42,'@type':'chat','@extra':chat_request['@extra'],'id':456,
            'title':'Synthetic target','type':{'@type':'chatTypePrivate','user_id':789},
            'message_sender_id':{'@type':'messageSenderUser','user_id':123}, 'last_message':{'text':'discard me'}})
        auth.poll()
        paid_request = binding.requests[-1]
        assert paid_request['@type'] == 'canSendMessageToUser' and paid_request['only_local'] is False
        binding.events.append({'@client_id':42,'@type':'canSendMessageToUserResultOk','@extra':paid_request['@extra']})
        auth.poll()
        snapshot, version = verifier.finish()
        assert snapshot['chat_id'] == '456' and snapshot['paid_required'] is False
        assert 'discard me' not in repr(snapshot)
        claim = {'account_id':'123','account_epoch':0,'destination_id':'456','sender':{'kind':'user','id':'123'}}
        assert verifier.is_current(claim, {'destination':snapshot,'destination_version':version})
        changed = snapshot.copy(); changed['permissions'] = snapshot['permissions']
        changed['permissions']['can_send'] = False
        assert not verifier.is_current(claim, {'destination':changed,'destination_version':version})
        binding.events.append({'@client_id':42,'@type':'updateConnectionState','state':{'@type':'connectionStateWaitingForNetwork'}})
        auth.poll()
        assert not verifier.is_current(claim, {'destination':snapshot,'destination_version':version})


def test_clean_auth_network_enable_holds_database_writer(tmp_path):
    import sqlite3
    from threadrelay.delivery.auth import AuthController
    from threadrelay.delivery.telegram import NetworkPermit
    from test_telegram_auth import MemorySecretHandoff, state
    from threadrelay.storage.database import Database
    from threadrelay.storage.outbox import AccountStore
    with Database(tmp_path / 'app.db') as db:
        class InspectBinding(FixtureBinding):
            locked = False
            def send(self, client_id, request):
                super().send(client_id, request)
                if request.get('@extra','').startswith('network:enable:'):
                    con = sqlite3.connect(db.path, timeout=0)
                    try:
                        con.execute('BEGIN IMMEDIATE')
                    except sqlite3.OperationalError:
                        self.locked = True
                    finally:
                        con.close()
        binding = InspectBinding(); client = TelegramClient(lambda: binding, FixtureVault(), tmp_path / 'sessions')
        auth = AuthController(client, AccountStore(db), MemorySecretHandoff(), clock=lambda: 0)
        auth.begin('vault:test', pending=False)
        binding.events.append(state('WaitTdlibParameters')); auth.poll()
        for request in binding.requests[:]:
            if request.get('@extra','').startswith('init:'):
                binding.events.append({'@client_id':42,'@type':'ok','@extra':request['@extra']}); auth.poll()
        auth.enable_clean_auth(NetworkPermit('clean_auth', 0, False))
        binding.events.append({'@client_id':42,'@type':'ok','@extra':binding.requests[-1]['@extra']}); auth.poll()
        assert binding.locked


def test_send_error_and_timeout_are_unknown_never_retryable(tmp_path):
    binding = FixtureBinding(); client = TelegramClient(lambda: binding, FixtureVault(), tmp_path / 'sessions')
    client.initialize_offline('vault:test')
    client.register_attempt(delivery_id='d', attempt_id='a', request_extra='x', account_id='123', epoch=2,
        chat_id='456', sender={'kind':'user','id':'123'}, destination_version=1, content_hash='hash')
    binding.events.append({'@client_id':42,'@type':'error','@extra':'x','message':'private raw error'})
    result = pump_delivery(client)
    assert result.kind == 'unknown' and not result.can_retry and 'private raw error' not in repr(result)
    client.expire_attempt('x')
    assert client.take_delivery_event() is None


def test_retry_claim_requires_exact_failed_attempt_and_creates_new_durable_attempt(env):
    from test_routing import staged
    from threadrelay.storage.outbox import Outbox
    delivery, _ = staged(env)
    outbox = Outbox(env.db); first = outbox.claim_next('worker')
    with env.db.transaction() as con:
        con.execute('UPDATE routing_delivery_context SET initiated_attempt_id=? WHERE delivery_id=?', (first['attempt_id'], first['id']))
    metadata = {'delivery_id':first['id'],'attempt_id':first['attempt_id'],'request_extra':first['request_extra'],
        'temporary_message_id':'-7','final_message_id':None,'state':'retryable'}
    assert outbox.transition(first['id'], 'sending', 'retryable', metadata)
    assert outbox.claim_retry(first['id'], 'wrong', 'worker') is None
    second = outbox.claim_retry(first['id'], first['attempt_id'], 'worker')
    assert second['state'] == 'sending' and second['attempt_id'] != first['attempt_id']
    assert second['temporary_message_id'] is None and len(outbox.attempts(first['id'])) == 2
    assert outbox.claim_retry(first['id'], first['attempt_id'], 'worker') is None


def test_retry_verification_uses_exact_own_failed_message_locally(tmp_path):
    binding = FixtureBinding(); client = TelegramClient(lambda: binding, FixtureVault(), tmp_path / 'sessions')
    client.initialize_offline('vault:test')
    client.register_attempt(delivery_id='d', attempt_id='a', request_extra='x', account_id='123', epoch=2,
        chat_id='456', sender={'kind':'user','id':'123'}, destination_version=1, content_hash='hash')
    binding.events.append({'@client_id':42,'@type':'message','@extra':'x','id':-7,'chat_id':456,
        'sender_id':{'@type':'messageSenderUser','user_id':123}}); pump_delivery(client)
    failure = {'@type':'messageSendingStateFailed','can_retry':True,'retry_after':2.0,'need_another_sender':False,
        'need_another_reply_quote':False,'need_drop_reply':False,'required_paid_message_star_count':0}
    binding.events.append({'@client_id':42,'@type':'updateMessageSendFailed','old_message_id':-7,
        'message':{'id':-8,'chat_id':456,'sender_id':{'@type':'messageSenderUser','user_id':123},'sending_state':failure}})
    assert pump_delivery(client).can_retry
    client.verify_failed_attempt('x', '-8')
    import pytest
    with pytest.raises(Exception): client.verify_failed_attempt('x', '-8')
    request = binding.requests[-1]
    assert request['@type'] == 'getMessageLocally' and request['chat_id'] == 456 and request['message_id'] == -8
    binding.events.append({'@client_id':42,'@type':'message','@extra':request['@extra'],'id':-8,'chat_id':456,
        'sender_id':{'@type':'messageSenderUser','user_id':123},'sending_state':failure,'paid_message_star_count':0,'content':{'text':'discard'}})
    assert pump_delivery(client).kind == 'retry_verified'


def test_retry_local_message_must_prove_original_zero_paid(tmp_path):
    binding = FixtureBinding(); client = TelegramClient(lambda: binding, FixtureVault(), tmp_path / 'sessions')
    client.initialize_offline('vault:test')
    client.register_attempt(delivery_id='d', attempt_id='a', request_extra='x', account_id='123', epoch=2,
        chat_id='456', sender={'kind':'user','id':'123'}, destination_version=1, content_hash='hash')
    sender = {'@type':'messageSenderUser','user_id':123}
    failure = {'@type':'messageSendingStateFailed','can_retry':True,'retry_after':0,'need_another_sender':False,
        'need_another_reply_quote':False,'need_drop_reply':False,'required_paid_message_star_count':0}
    binding.events.append({'@client_id':42,'@type':'message','@extra':'x','id':-7,'chat_id':456,'sender_id':sender}); pump_delivery(client)
    binding.events.append({'@client_id':42,'@type':'updateMessageSendFailed','old_message_id':-7,
        'message':{'id':-8,'chat_id':456,'sender_id':sender,'sending_state':failure}}); pump_delivery(client)
    client.verify_failed_attempt('x', '-8'); request=binding.requests[-1]
    binding.events.append({'@client_id':42,'@type':'message','@extra':request['@extra'],'id':-8,'chat_id':456,
        'sender_id':sender,'sending_state':failure})
    assert pump_delivery(client).kind == 'retry_invalidated'


def test_retry_is_existing_message_only_after_local_evidence_and_delay(tmp_path):
    from test_routing import ScoresRuntime, topic, route, destination, post, plan, running, approved
    from types import SimpleNamespace
    from datetime import datetime, timezone
    from threadrelay.delivery.auth import AuthController
    from threadrelay.delivery.telegram import NetworkPermit
    from test_telegram_auth import MemorySecretHandoff, state
    from threadrelay.storage.database import Database
    from threadrelay.storage.outbox import AccountStore, Outbox
    from threadrelay.storage.routing import ConfigStore
    from threadrelay.storage.posts import PostStore
    from threadrelay.routing.policy import Router
    from threadrelay.routing.approvals import ApprovalStore
    from threadrelay.inference.decision import Classifier
    from threadrelay.delivery.sender import Sender
    now = datetime(2026, 10, 28, tzinfo=timezone.utc)
    with Database(tmp_path / 'app.db', clock=lambda: now) as db:
        binding = FixtureBinding(); client = TelegramClient(lambda: binding, FixtureVault(), tmp_path / 'sessions')
        session = running()
        auth = AuthController(client, AccountStore(db), MemorySecretHandoff(), clock=lambda: 0.0, lifecycle_lock=session.authority_lock)
        auth.begin('vault:test', pending=False)
        binding.events.append(state('WaitTdlibParameters')); auth.poll()
        for request in binding.requests[:]:
            extra = request.get('@extra', '')
            if extra.startswith('init:'):
                binding.events.append({'@client_id':42, '@type':'ok', '@extra':extra})
                auth.poll()
        auth.enable_clean_auth(NetworkPermit('clean_auth', 0, False))
        binding.events.append({'@client_id':42,'@type':'ok','@extra':binding.requests[-1]['@extra']}); auth.poll()
        binding.events.extend([state('Ready'), {'@client_id':42,'@type':'user','@extra':'auth:get-me','id':123,'type':{'@type':'userTypeRegular'}},
            {'@client_id':42,'@type':'updateConnectionState','state':{'@type':'connectionStateReady'}}])
        auth.poll(); auth.poll(); auth.poll()
        config = ConfigStore(db); config.save_topic(topic())
        sender = {'kind':'user','id':'123'}
        config.save_route(route(destination_id='456',account_id='123',account_epoch=0,sender=sender))
        config.save_destination(destination(account_id='123',chat_id='456',sender=sender), account_epoch=0)
        p = post(); PostStore(db).upsert(p); classifier = Classifier(ScoresRuntime()); router = Router(db,classifier=classifier)
        env = SimpleNamespace(db=db, config=config, post=p, classifier=classifier, router=router, approvals=ApprovalStore(router))
        proposal = plan(env)[0]; delivery_id = router.stage(proposal, approval=approved(env, proposal))
        class VerifiedDestination:
            def is_current(self, delivery, proposal):
                return True
        elapsed = [0.0]
        delivery = Sender(router, session, auth, client, destination_authority=VerifiedDestination(), clock=lambda: now, monotonic=lambda: elapsed[0])
        result = delivery.start_next('worker')
        assert result is not None and Outbox(db).get(delivery_id)['state'] == 'sending'
        req = binding.requests[-1]
        assert req['@type'] == 'sendMessage' and req['chat_id'] == 456 and req['topic_id'] is None
        assert req['options']['paid_message_star_count'] == 0 and req['options']['allow_paid_broadcast'] is False
        assert req['input_message_content']['clear_draft'] is False
        assert Outbox(db).attempts(delivery_id)[0]['request_extra'] == req['@extra']
        binding.events.append({'@client_id':42,'@type':'message','@extra':req['@extra'],'id':-7,'chat_id':456,
            'sender_id':{'@type':'messageSenderUser','user_id':123}})
        delivery.poll()
        assert Outbox(db).get(delivery_id)['state'] == 'sending'
        assert Outbox(db).get(delivery_id)['temporary_message_id'] == '-7'
        failure = {'@type':'messageSendingStateFailed','can_retry':True,'retry_after':2.0,'need_another_sender':False,
            'need_another_reply_quote':False,'need_drop_reply':False,'required_paid_message_star_count':0}
        binding.events.append({'@client_id':42,'@type':'updateMessageSendFailed','old_message_id':-7,
            'message':{'id':-8,'chat_id':456,'sender_id':{'@type':'messageSenderUser','user_id':123},'sending_state':failure}})
        delivery.poll()
        assert Outbox(db).get(delivery_id)['state'] == 'retryable'
        delivery.prepare_retry(delivery_id)
        local = binding.requests[-1]
        binding.events.append({'@client_id':42,'@type':'message','@extra':local['@extra'],'id':-8,'chat_id':456,
            'sender_id':{'@type':'messageSenderUser','user_id':123}, 'sending_state':failure,'paid_message_star_count':0})
        delivery.poll()
        assert delivery.retry(delivery_id, 'worker') is None
        elapsed[0] = 2.0
        retried = delivery.retry(delivery_id, 'worker')
        assert retried is not None
        request = binding.requests[-1]
        assert request['@type'] == 'resendMessages' and request['message_ids'] == [-8]
        assert request['paid_message_star_count'] == 0 and request['quote'] is None
        assert sum(r['@type'] == 'sendMessage' for r in binding.requests) == 1
        binding.events.append({'@client_id':42,'@type':'messages','@extra':request['@extra'],'messages':[{
            '@type':'message','id':-9,'chat_id':456,'sender_id':{'@type':'messageSenderUser','user_id':123}}]})
        delivery.poll()
        assert Outbox(db).get(delivery_id)['state'] == 'sending'
        assert Outbox(db).get(delivery_id)['temporary_message_id'] == '-9'
        binding.events.append({'@client_id':42,'@type':'updateMessageSendSucceeded','old_message_id':-9,
            'message':{'id':10,'chat_id':456,'sender_id':{'@type':'messageSenderUser','user_id':123}}})
        delivery.poll()
        assert Outbox(db).get(delivery_id)['final_message_id'] == '10'



def test_deletion_of_failed_message_invalidates_retry_evidence(tmp_path):
    binding = FixtureBinding(); client = TelegramClient(lambda: binding, FixtureVault(), tmp_path / 'sessions')
    client.initialize_offline('vault:test')
    client.register_attempt(delivery_id='d', attempt_id='a', request_extra='x', account_id='123', epoch=2,
        chat_id='456', sender={'kind':'user','id':'123'}, destination_version=1, content_hash='hash')
    sender={'@type':'messageSenderUser','user_id':123}
    failure={'@type':'messageSendingStateFailed','can_retry':True,'retry_after':0,'need_another_sender':False,
        'need_another_reply_quote':False,'need_drop_reply':False,'required_paid_message_star_count':0}
    binding.events.append({'@client_id':42,'@type':'message','@extra':'x','id':-7,'chat_id':456,'sender_id':sender}); pump_delivery(client)
    binding.events.append({'@client_id':42,'@type':'updateMessageSendFailed','old_message_id':-7,
        'message':{'id':-8,'chat_id':456,'sender_id':sender,'sending_state':failure}}); pump_delivery(client)
    binding.events.append({'@client_id':42,'@type':'updateDeleteMessages','chat_id':456,'message_ids':[-8]})
    assert pump_delivery(client).kind == 'retry_invalidated'


def test_sender_timeout_terminalizes_without_fresh_resend(env):
    from test_routing import staged, running, NOW
    from threadrelay.delivery.sender import Sender
    from threadrelay.storage.outbox import Outbox
    class Client:
        def expire_attempt(self, extra): self.expired = extra
        def retire_attempt(self, extra, *, abandon_retry=False): pass
        def disable_network(self): pass
    class Auth:
        def __init__(self, client, lock): self._client, self._lifecycle_lock = client, lock
    session=running(); client=Client(); auth=Auth(client,session.authority_lock)
    sender=Sender(env.router,session,auth,client,destination_authority=None,clock=lambda:NOW)
    delivery,_=staged(env); claim=Outbox(env.db).claim_next('worker')
    sender.timeout(claim['id'])
    assert Outbox(env.db).get(claim['id'])['state'] == 'unknown'
    assert Outbox(env.db).claim_next('worker') is None
    assert sender.retry(claim['id'], 'worker') is None


def test_lost_failed_message_evidence_becomes_unknown(env):
    from test_routing import staged
    from threadrelay.storage.outbox import Outbox
    delivery,_=staged(env); outbox=Outbox(env.db); claim=outbox.claim_next('worker')
    metadata={'delivery_id':claim['id'],'attempt_id':claim['attempt_id'],'request_extra':claim['request_extra'],
        'temporary_message_id':'-7','final_message_id':None,'state':'retryable'}
    assert outbox.transition(claim['id'],'sending','retryable',metadata)
    assert not outbox.unknown_retry(claim['id'], 'wrong')
    assert outbox.unknown_retry(claim['id'], claim['attempt_id'])
    assert outbox.get(claim['id'])['state'] == 'unknown'


def test_delivery_dequeue_never_pulls_native_receive(tmp_path):
    binding = FixtureBinding(); client = TelegramClient(lambda: binding, FixtureVault(), tmp_path / 'sessions')
    client.initialize_offline('vault:test')
    client.register_attempt(delivery_id='d', attempt_id='a', request_extra='x', account_id='123', epoch=2,
        chat_id='456', sender={'kind':'user','id':'123'}, destination_version=1, content_hash='hash')
    binding.events.append({'@client_id':42,'@type':'message','@extra':'x','id':-7,'chat_id':456,
        'sender_id':{'@type':'messageSenderUser','user_id':123}})
    assert client.delivery_event() is None
    assert len(binding.events) == 1
    client.authorization_event()
    assert client.delivery_event().kind == 'initiated'


def test_explicit_channel_destination_requires_posting_rights_and_selected_sender(tmp_path):
    from threadrelay.delivery.destinations import DestinationVerifier
    from threadrelay.delivery.auth import AuthController
    from threadrelay.delivery.telegram import NetworkPermit
    from test_telegram_auth import MemorySecretHandoff, state
    from threadrelay.storage.database import Database
    from threadrelay.storage.outbox import AccountStore
    from threadrelay.storage.routing import ConfigStore
    from threadrelay.session import SessionController
    with Database(tmp_path / 'app.db') as db:
        session = SessionController(); binding = FixtureBinding()
        client = TelegramClient(lambda: binding, FixtureVault(), tmp_path / 'sessions')
        auth = AuthController(client, AccountStore(db), MemorySecretHandoff(), clock=lambda: 0, lifecycle_lock=session.authority_lock)
        auth.begin('vault:test', pending=False)
        binding.events.append(state('WaitTdlibParameters')); auth.poll()
        for request in binding.requests[:]:
            if request.get('@extra','').startswith('init:'):
                binding.events.append({'@client_id':42,'@type':'ok','@extra':request['@extra']}); auth.poll()
        auth.enable_clean_auth(NetworkPermit('clean_auth', 0, False))
        binding.events.append({'@client_id':42,'@type':'ok','@extra':binding.requests[-1]['@extra']}); auth.poll()
        binding.events.extend([state('Ready'), {'@client_id':42,'@type':'user','@extra':'auth:get-me','id':123,'type':{'@type':'userTypeRegular'}},
            {'@client_id':42,'@type':'updateConnectionState','state':{'@type':'connectionStateReady'}}])
        auth.poll(); auth.poll(); auth.poll()
        verifier = DestinationVerifier(client, auth, ConfigStore(db), session)
        verifier.begin_group('456', sender={'kind':'chat','id':'456'}, sender_selected=True)
        req = binding.requests[-1]
        binding.events.append({'@client_id':42,'@type':'chat','@extra':req['@extra'],'id':456,'title':'Synthetic channel',
            'type':{'@type':'chatTypeSupergroup','supergroup_id':789,'is_channel':True},
            'message_sender_id':{'@type':'messageSenderChat','chat_id':456}}); auth.poll()
        req = binding.requests[-1]; assert req['@type'] == 'getSupergroup'
        binding.events.append({'@client_id':42,'@type':'supergroup','@extra':req['@extra'],'id':789,
            'is_channel':True, 'paid_message_star_count':0, 'restriction_info':None, 'usernames':{'active_usernames':['synthetic']}}); auth.poll()
        req = binding.requests[-1]; assert req['@type'] == 'getChatMember'
        binding.events.append({'@client_id':42,'@type':'chatMember','@extra':req['@extra'],
            'member_id':{'@type':'messageSenderUser','user_id':123},
            'status':{'@type':'chatMemberStatusAdministrator','rights':{'can_post_messages':True}}}); auth.poll()
        req = binding.requests[-1]; assert req['@type'] == 'getChatAvailableMessageSenders'
        binding.events.append({'@client_id':42,'@type':'chatMessageSenders','@extra':req['@extra'],
            'senders':[{'sender':{'@type':'messageSenderChat','chat_id':456},'needs_premium':False}]}); auth.poll()
        snapshot, version = verifier.finish()
        assert snapshot['audience'] == 'public' and snapshot['sender'] == {'kind':'chat','id':'456'}
        claim = {'account_id':'123','account_epoch':0,'destination_id':'456','sender':snapshot['sender']}
        assert verifier.is_current(claim, {'destination':snapshot,'destination_version':version})



def test_transport_shutdown_failure_still_cancels_intake():
    from threadrelay.session import SessionController
    called = []
    session = SessionController(setup_check=lambda _: {'allowed':True,'reason':None}, cancel_intake=lambda:called.append('intake'))
    session.start({'feed_url':'https://www.threads.net/','mode':'review'})
    def fail():
        called.append('network')
        raise RuntimeError('unavailable')
    session.add_gate_close_hook(fail)
    assert session.stop()['state'] == 'failed'
    assert called == ['network','intake']


def test_reconciliation_queries_only_exact_correlated_own_temporary_id(tmp_path):
    binding = FixtureBinding(); client = TelegramClient(lambda: binding, FixtureVault(), tmp_path / 'sessions')
    client.initialize_offline('vault:test')
    client.register_attempt(delivery_id='d', attempt_id='a', request_extra='x', account_id='123', epoch=2,
        chat_id='456', sender={'kind':'user','id':'123'}, destination_version=1, content_hash='hash')
    binding.events.append({'@client_id':42,'@type':'message','@extra':'x','id':-7,'chat_id':456,
        'sender_id':{'@type':'messageSenderUser','user_id':123}}); pump_delivery(client)
    client.reconcile_pending(request_extras=['x'])
    request = binding.requests[-1]
    assert request['@type'] == 'getMessageLocally' and request['chat_id'] == 456 and request['message_id'] == -7
    binding.events.append({'@client_id':42,'@type':'error','@extra':request['@extra'],'code':404,'message':'private'})
    event = pump_delivery(client)
    assert event.kind == 'unknown' and 'private' not in repr(event)
    assert not any(r['@type'] in {'getChatHistory','searchChatMessages','getContacts'} for r in binding.requests)


def test_queued_deletion_invalidates_retry_before_dispatch(tmp_path):
    from test_routing import ScoresRuntime, topic, route, destination, post, plan, running, approved
    from types import SimpleNamespace
    from datetime import datetime, timezone
    from threadrelay.delivery.auth import AuthController
    from threadrelay.delivery.telegram import NetworkPermit
    from test_telegram_auth import MemorySecretHandoff, state
    from threadrelay.storage.database import Database
    from threadrelay.storage.outbox import AccountStore, Outbox
    from threadrelay.storage.routing import ConfigStore
    from threadrelay.storage.posts import PostStore
    from threadrelay.routing.policy import Router
    from threadrelay.routing.approvals import ApprovalStore
    from threadrelay.inference.decision import Classifier
    from threadrelay.delivery.sender import Sender
    now = datetime(2026, 10, 28, tzinfo=timezone.utc)
    with Database(tmp_path / 'app.db', clock=lambda: now) as db:
        binding = FixtureBinding(); client = TelegramClient(lambda: binding, FixtureVault(), tmp_path / 'sessions')
        session = running()
        auth = AuthController(client, AccountStore(db), MemorySecretHandoff(), clock=lambda: 0.0, lifecycle_lock=session.authority_lock)
        auth.begin('vault:test', pending=False)
        binding.events.append(state('WaitTdlibParameters')); auth.poll()
        for request in binding.requests[:]:
            extra = request.get('@extra', '')
            if extra.startswith('init:'):
                binding.events.append({'@client_id':42, '@type':'ok', '@extra':extra})
                auth.poll()
        auth.enable_clean_auth(NetworkPermit('clean_auth', 0, False))
        binding.events.append({'@client_id':42,'@type':'ok','@extra':binding.requests[-1]['@extra']}); auth.poll()
        binding.events.extend([state('Ready'), {'@client_id':42,'@type':'user','@extra':'auth:get-me','id':123,'type':{'@type':'userTypeRegular'}},
            {'@client_id':42,'@type':'updateConnectionState','state':{'@type':'connectionStateReady'}}])
        auth.poll(); auth.poll(); auth.poll()
        config = ConfigStore(db); config.save_topic(topic())
        sender = {'kind':'user','id':'123'}
        config.save_route(route(destination_id='456',account_id='123',account_epoch=0,sender=sender))
        config.save_destination(destination(account_id='123',chat_id='456',sender=sender), account_epoch=0)
        p = post(); PostStore(db).upsert(p); classifier = Classifier(ScoresRuntime()); router = Router(db,classifier=classifier)
        env = SimpleNamespace(db=db, config=config, post=p, classifier=classifier, router=router, approvals=ApprovalStore(router))
        proposal = plan(env)[0]; delivery_id = router.stage(proposal, approval=approved(env, proposal))
        class VerifiedDestination:
            def is_current(self, delivery, proposal):
                return True
        elapsed = [0.0]
        delivery = Sender(router, session, auth, client, destination_authority=VerifiedDestination(), clock=lambda: now, monotonic=lambda: elapsed[0])
        result = delivery.start_next('worker')
        assert result is not None and Outbox(db).get(delivery_id)['state'] == 'sending'
        req = binding.requests[-1]
        assert req['@type'] == 'sendMessage' and req['chat_id'] == 456 and req['topic_id'] is None
        assert req['options']['paid_message_star_count'] == 0 and req['options']['allow_paid_broadcast'] is False
        assert req['input_message_content']['clear_draft'] is False
        assert Outbox(db).attempts(delivery_id)[0]['request_extra'] == req['@extra']
        binding.events.append({'@client_id':42,'@type':'message','@extra':req['@extra'],'id':-7,'chat_id':456,
            'sender_id':{'@type':'messageSenderUser','user_id':123}})
        delivery.poll()
        assert Outbox(db).get(delivery_id)['state'] == 'sending'
        assert Outbox(db).get(delivery_id)['temporary_message_id'] == '-7'
        failure = {'@type':'messageSendingStateFailed','can_retry':True,'retry_after':2.0,'need_another_sender':False,
            'need_another_reply_quote':False,'need_drop_reply':False,'required_paid_message_star_count':0}
        binding.events.append({'@client_id':42,'@type':'updateMessageSendFailed','old_message_id':-7,
            'message':{'id':-8,'chat_id':456,'sender_id':{'@type':'messageSenderUser','user_id':123},'sending_state':failure}})
        delivery.poll()
        assert Outbox(db).get(delivery_id)['state'] == 'retryable'
        delivery.prepare_retry(delivery_id)
        local = binding.requests[-1]
        binding.events.append({'@client_id':42,'@type':'message','@extra':local['@extra'],'id':-8,'chat_id':456,
            'sender_id':{'@type':'messageSenderUser','user_id':123}, 'sending_state':failure,'paid_message_star_count':0})
        delivery.poll()
        assert delivery.retry(delivery_id, 'worker') is None
        elapsed[0] = 2.0
        binding.events.append({'@client_id':42,'@type':'updateDeleteMessages','chat_id':456,'message_ids':[-8]})
        assert delivery.retry(delivery_id, 'worker') is None
        assert Outbox(db).get(delivery_id)['state'] == 'unknown'
        assert not any(r['@type'] == 'resendMessages' for r in binding.requests)



def test_attempt_registration_rejects_unsafe_and_mismatched_bindings(tmp_path):
    import pytest
    binding = FixtureBinding(); client = TelegramClient(lambda: binding, FixtureVault(), tmp_path / 'sessions')
    client.initialize_offline('vault:test')
    base=dict(delivery_id='d', attempt_id='a', request_extra='x', account_id='123', epoch=2,
        chat_id='456', sender={'kind':'user','id':'123'}, destination_version=1, content_hash='hash')
    for changes in ({'chat_id':'9007199254740992'}, {'account_id':'0123'}, {'epoch':-1},
                    {'sender':{'kind':'user','id':'999'}}, {'request_extra':'x'*129}):
        with pytest.raises(ValueError): client.register_attempt(**{**base, **changes})


def test_remote_logout_holds_database_writer_for_unresolved_check(tmp_path):
    import sqlite3
    from threadrelay.delivery.auth import AuthController
    from threadrelay.delivery.telegram import NetworkPermit
    from test_telegram_auth import MemorySecretHandoff, state
    from threadrelay.storage.database import Database
    from threadrelay.storage.outbox import AccountStore
    with Database(tmp_path / 'app.db') as db:
        class InspectBinding(FixtureBinding):
            locked = False
            def send(self, client_id, request):
                super().send(client_id, request)
                if request.get('@extra') == 'auth:logout':
                    con = sqlite3.connect(db.path, timeout=0)
                    try:
                        con.execute('BEGIN IMMEDIATE')
                    except sqlite3.OperationalError:
                        self.locked = True
                    finally:
                        con.close()
        binding = InspectBinding(); client = TelegramClient(lambda: binding, FixtureVault(), tmp_path / 'sessions')
        auth = AuthController(client, AccountStore(db), MemorySecretHandoff(), clock=lambda: 0)
        auth.begin('vault:test', pending=False)
        binding.events.append(state('WaitTdlibParameters')); auth.poll()
        for request in binding.requests[:]:
            if request.get('@extra','').startswith('init:'):
                binding.events.append({'@client_id':42,'@type':'ok','@extra':request['@extra']}); auth.poll()
        auth.enable_clean_auth(NetworkPermit('clean_auth', 0, False))
        binding.events.append({'@client_id':42,'@type':'ok','@extra':binding.requests[-1]['@extra']}); auth.poll()
        binding.events.extend([state('Ready'), {'@client_id':42,'@type':'user','@extra':'auth:get-me','id':123,'type':{'@type':'userTypeRegular'}}])
        auth.poll(); auth.poll()
        auth.logout(pending=False, permit=NetworkPermit('clean_auth', 1, False))
        assert binding.locked



def test_restart_without_in_memory_retry_evidence_is_unknown(tmp_path):
    from test_storage import snapshot, post, fixture, delivery, attempt, NOW
    from threadrelay.storage.database import Database
    from threadrelay.storage.posts import PostStore
    from threadrelay.storage.outbox import AccountStore, Outbox
    path=tmp_path/'app.db'
    with Database(path,clock=lambda:NOW) as db:
        AccountStore(db).activate(snapshot()); PostStore(db).upsert(post()); outbox=Outbox(db)
        outbox.save_approval(fixture('Approval')); outbox.enqueue(delivery())
        claim=outbox.claim_next('worker')
        assert outbox.transition(claim['id'],'sending','retryable',attempt(claim,'retryable',temporary_message_id='-7'))
    with Database(path,clock=lambda:NOW) as reopened:
        assert Outbox(reopened).get(claim['id'])['state'] == 'unknown'
        assert Outbox(reopened).claim_next('worker') is None


def test_stopped_session_does_not_claim_pending_delivery(tmp_path):
    from test_routing import ScoresRuntime, topic, route, destination, post, plan, running, approved
    from types import SimpleNamespace
    from datetime import datetime, timezone
    from threadrelay.delivery.auth import AuthController
    from threadrelay.delivery.telegram import NetworkPermit
    from test_telegram_auth import MemorySecretHandoff, state
    from threadrelay.storage.database import Database
    from threadrelay.storage.outbox import AccountStore, Outbox
    from threadrelay.storage.routing import ConfigStore
    from threadrelay.storage.posts import PostStore
    from threadrelay.routing.policy import Router
    from threadrelay.routing.approvals import ApprovalStore
    from threadrelay.inference.decision import Classifier
    from threadrelay.delivery.sender import Sender
    now = datetime(2026, 10, 28, tzinfo=timezone.utc)
    with Database(tmp_path / 'app.db', clock=lambda: now) as db:
        binding = FixtureBinding(); client = TelegramClient(lambda: binding, FixtureVault(), tmp_path / 'sessions')
        session = running()
        auth = AuthController(client, AccountStore(db), MemorySecretHandoff(), clock=lambda: 0.0, lifecycle_lock=session.authority_lock)
        auth.begin('vault:test', pending=False)
        binding.events.append(state('WaitTdlibParameters')); auth.poll()
        for request in binding.requests[:]:
            extra = request.get('@extra', '')
            if extra.startswith('init:'):
                binding.events.append({'@client_id':42, '@type':'ok', '@extra':extra})
                auth.poll()
        auth.enable_clean_auth(NetworkPermit('clean_auth', 0, False))
        binding.events.append({'@client_id':42,'@type':'ok','@extra':binding.requests[-1]['@extra']}); auth.poll()
        binding.events.extend([state('Ready'), {'@client_id':42,'@type':'user','@extra':'auth:get-me','id':123,'type':{'@type':'userTypeRegular'}},
            {'@client_id':42,'@type':'updateConnectionState','state':{'@type':'connectionStateReady'}}])
        auth.poll(); auth.poll(); auth.poll()
        config = ConfigStore(db); config.save_topic(topic())
        sender = {'kind':'user','id':'123'}
        config.save_route(route(destination_id='456',account_id='123',account_epoch=0,sender=sender))
        config.save_destination(destination(account_id='123',chat_id='456',sender=sender), account_epoch=0)
        p = post(); PostStore(db).upsert(p); classifier = Classifier(ScoresRuntime()); router = Router(db,classifier=classifier)
        env = SimpleNamespace(db=db, config=config, post=p, classifier=classifier, router=router, approvals=ApprovalStore(router))
        proposal = plan(env)[0]; delivery_id = router.stage(proposal, approval=approved(env, proposal))
        class VerifiedDestination:
            def is_current(self, delivery, proposal):
                return True
        delivery = Sender(router, session, auth, client, destination_authority=VerifiedDestination(), clock=lambda: now)
        session.stop()
        assert delivery.start_next('worker') is None
        assert Outbox(db).get(delivery_id)['state'] == 'pending'
        assert not any(r['@type'] == 'sendMessage' for r in binding.requests)



def test_stop_invalidates_retry_evidence_before_reconnect(tmp_path):
    from test_routing import ScoresRuntime, topic, route, destination, post, plan, running, approved
    from types import SimpleNamespace
    from datetime import datetime, timezone
    from threadrelay.delivery.auth import AuthController
    from threadrelay.delivery.telegram import NetworkPermit
    from test_telegram_auth import MemorySecretHandoff, state
    from threadrelay.storage.database import Database
    from threadrelay.storage.outbox import AccountStore, Outbox
    from threadrelay.storage.routing import ConfigStore
    from threadrelay.storage.posts import PostStore
    from threadrelay.routing.policy import Router
    from threadrelay.routing.approvals import ApprovalStore
    from threadrelay.inference.decision import Classifier
    from threadrelay.delivery.sender import Sender
    now = datetime(2026, 10, 28, tzinfo=timezone.utc)
    with Database(tmp_path / 'app.db', clock=lambda: now) as db:
        binding = FixtureBinding(); client = TelegramClient(lambda: binding, FixtureVault(), tmp_path / 'sessions')
        session = running()
        auth = AuthController(client, AccountStore(db), MemorySecretHandoff(), clock=lambda: 0.0, lifecycle_lock=session.authority_lock)
        auth.begin('vault:test', pending=False)
        binding.events.append(state('WaitTdlibParameters')); auth.poll()
        for request in binding.requests[:]:
            extra = request.get('@extra', '')
            if extra.startswith('init:'):
                binding.events.append({'@client_id':42, '@type':'ok', '@extra':extra})
                auth.poll()
        auth.enable_clean_auth(NetworkPermit('clean_auth', 0, False))
        binding.events.append({'@client_id':42,'@type':'ok','@extra':binding.requests[-1]['@extra']}); auth.poll()
        binding.events.extend([state('Ready'), {'@client_id':42,'@type':'user','@extra':'auth:get-me','id':123,'type':{'@type':'userTypeRegular'}},
            {'@client_id':42,'@type':'updateConnectionState','state':{'@type':'connectionStateReady'}}])
        auth.poll(); auth.poll(); auth.poll()
        config = ConfigStore(db); config.save_topic(topic())
        sender = {'kind':'user','id':'123'}
        config.save_route(route(destination_id='456',account_id='123',account_epoch=0,sender=sender))
        config.save_destination(destination(account_id='123',chat_id='456',sender=sender), account_epoch=0)
        p = post(); PostStore(db).upsert(p); classifier = Classifier(ScoresRuntime()); router = Router(db,classifier=classifier)
        env = SimpleNamespace(db=db, config=config, post=p, classifier=classifier, router=router, approvals=ApprovalStore(router))
        proposal = plan(env)[0]; delivery_id = router.stage(proposal, approval=approved(env, proposal))
        class VerifiedDestination:
            def is_current(self, delivery, proposal):
                return True
        elapsed = [0.0]
        delivery = Sender(router, session, auth, client, destination_authority=VerifiedDestination(), clock=lambda: now, monotonic=lambda: elapsed[0])
        result = delivery.start_next('worker')
        assert result is not None and Outbox(db).get(delivery_id)['state'] == 'sending'
        req = binding.requests[-1]
        assert req['@type'] == 'sendMessage' and req['chat_id'] == 456 and req['topic_id'] is None
        assert req['options']['paid_message_star_count'] == 0 and req['options']['allow_paid_broadcast'] is False
        assert req['input_message_content']['clear_draft'] is False
        assert Outbox(db).attempts(delivery_id)[0]['request_extra'] == req['@extra']
        binding.events.append({'@client_id':42,'@type':'message','@extra':req['@extra'],'id':-7,'chat_id':456,
            'sender_id':{'@type':'messageSenderUser','user_id':123}})
        delivery.poll()
        assert Outbox(db).get(delivery_id)['state'] == 'sending'
        assert Outbox(db).get(delivery_id)['temporary_message_id'] == '-7'
        failure = {'@type':'messageSendingStateFailed','can_retry':True,'retry_after':2.0,'need_another_sender':False,
            'need_another_reply_quote':False,'need_drop_reply':False,'required_paid_message_star_count':0}
        binding.events.append({'@client_id':42,'@type':'updateMessageSendFailed','old_message_id':-7,
            'message':{'id':-8,'chat_id':456,'sender_id':{'@type':'messageSenderUser','user_id':123},'sending_state':failure}})
        delivery.poll()
        assert Outbox(db).get(delivery_id)['state'] == 'retryable'
        delivery.prepare_retry(delivery_id)
        local = binding.requests[-1]
        binding.events.append({'@client_id':42,'@type':'message','@extra':local['@extra'],'id':-8,'chat_id':456,
            'sender_id':{'@type':'messageSenderUser','user_id':123}, 'sending_state':failure,'paid_message_star_count':0})
        delivery.poll()
        assert delivery.retry(delivery_id, 'worker') is None
        elapsed[0] = 2.0
        session.stop()
        assert Outbox(db).get(delivery_id)['state'] == 'unknown'
        assert delivery.retry(delivery_id, 'worker') is None
        assert not any(r['@type'] == 'resendMessages' for r in binding.requests)



def test_sender_selection_requires_nonpremium_choice_and_reread(tmp_path):
    from threadrelay.delivery.destinations import DestinationVerifier
    from threadrelay.delivery.auth import AuthController
    from threadrelay.delivery.telegram import NetworkPermit
    from test_telegram_auth import MemorySecretHandoff, state
    from threadrelay.storage.database import Database
    from threadrelay.storage.outbox import AccountStore
    from threadrelay.storage.routing import ConfigStore
    from threadrelay.session import SessionController
    with Database(tmp_path / 'app.db') as db:
        session = SessionController(); binding = FixtureBinding()
        client = TelegramClient(lambda: binding, FixtureVault(), tmp_path / 'sessions')
        auth = AuthController(client, AccountStore(db), MemorySecretHandoff(), clock=lambda: 0, lifecycle_lock=session.authority_lock)
        auth.begin('vault:test', pending=False)
        binding.events.append(state('WaitTdlibParameters')); auth.poll()
        for request in binding.requests[:]:
            if request.get('@extra','').startswith('init:'):
                binding.events.append({'@client_id':42,'@type':'ok','@extra':request['@extra']}); auth.poll()
        auth.enable_clean_auth(NetworkPermit('clean_auth', 0, False))
        binding.events.append({'@client_id':42,'@type':'ok','@extra':binding.requests[-1]['@extra']}); auth.poll()
        binding.events.extend([state('Ready'), {'@client_id':42,'@type':'user','@extra':'auth:get-me','id':123,'type':{'@type':'userTypeRegular'}},
            {'@client_id':42,'@type':'updateConnectionState','state':{'@type':'connectionStateReady'}}])
        auth.poll(); auth.poll(); auth.poll()
        verifier = DestinationVerifier(client, auth, ConfigStore(db), session)
        selected={'kind':'chat','id':'456'}
        verifier.select_sender('456', selected)
        req=binding.requests[-1]; assert req['@type'] == 'getChatAvailableMessageSenders'
        binding.events.append({'@client_id':42,'@type':'chatMessageSenders','@extra':req['@extra'],
            'senders':[{'sender':{'@type':'messageSenderChat','chat_id':456},'needs_premium':False}]}); auth.poll()
        req=binding.requests[-1]; assert req['@type'] == 'setChatMessageSender'
        binding.events.append({'@client_id':42,'@type':'updateChatMessageSender','chat_id':456,'message_sender_id':{'@type':'messageSenderChat','chat_id':456}}); auth.poll()
        binding.events.append({'@client_id':42,'@type':'ok','@extra':req['@extra']}); auth.poll()
        req=binding.requests[-1]; assert req['@type'] == 'getChat'
        binding.events.append({'@client_id':42,'@type':'chat','@extra':req['@extra'],'id':456,
            'message_sender_id':{'@type':'messageSenderChat','chat_id':456}}); auth.poll()
        assert verifier.finish_selection() == selected



def test_receive_failure_never_leaves_ready_authority(tmp_path):
    from test_routing import ScoresRuntime, topic, route, destination, post, plan, running, approved
    from types import SimpleNamespace
    from datetime import datetime, timezone
    from threadrelay.delivery.auth import AuthController
    from threadrelay.delivery.telegram import NetworkPermit
    from test_telegram_auth import MemorySecretHandoff, state
    from threadrelay.storage.database import Database
    from threadrelay.storage.outbox import AccountStore
    from threadrelay.storage.routing import ConfigStore
    from threadrelay.storage.posts import PostStore
    from threadrelay.routing.policy import Router
    from threadrelay.routing.approvals import ApprovalStore
    from threadrelay.inference.decision import Classifier
    from threadrelay.delivery.sender import Sender
    now = datetime(2026, 10, 28, tzinfo=timezone.utc)
    with Database(tmp_path / 'app.db', clock=lambda: now) as db:
        binding = FixtureBinding(); client = TelegramClient(lambda: binding, FixtureVault(), tmp_path / 'sessions')
        session = running()
        auth = AuthController(client, AccountStore(db), MemorySecretHandoff(), clock=lambda: 0.0, lifecycle_lock=session.authority_lock)
        auth.begin('vault:test', pending=False)
        binding.events.append(state('WaitTdlibParameters')); auth.poll()
        for request in binding.requests[:]:
            extra = request.get('@extra', '')
            if extra.startswith('init:'):
                binding.events.append({'@client_id':42, '@type':'ok', '@extra':extra})
                auth.poll()
        auth.enable_clean_auth(NetworkPermit('clean_auth', 0, False))
        binding.events.append({'@client_id':42,'@type':'ok','@extra':binding.requests[-1]['@extra']}); auth.poll()
        binding.events.extend([state('Ready'), {'@client_id':42,'@type':'user','@extra':'auth:get-me','id':123,'type':{'@type':'userTypeRegular'}},
            {'@client_id':42,'@type':'updateConnectionState','state':{'@type':'connectionStateReady'}}])
        auth.poll(); auth.poll(); auth.poll()
        config = ConfigStore(db); config.save_topic(topic())
        sender = {'kind':'user','id':'123'}
        config.save_route(route(destination_id='456',account_id='123',account_epoch=0,sender=sender))
        config.save_destination(destination(account_id='123',chat_id='456',sender=sender), account_epoch=0)
        p = post(); PostStore(db).upsert(p); classifier = Classifier(ScoresRuntime()); router = Router(db,classifier=classifier)
        env = SimpleNamespace(db=db, config=config, post=p, classifier=classifier, router=router, approvals=ApprovalStore(router))
        proposal = plan(env)[0]; _delivery_id = router.stage(proposal, approval=approved(env, proposal))
        class VerifiedDestination:
            def is_current(self, delivery, proposal):
                return True
        delivery = Sender(router, session, auth, client, destination_authority=VerifiedDestination(), clock=lambda: now)
        import pytest
        original = binding.receive
        def fail(_): raise OSError('unavailable')
        binding.receive = fail
        with pytest.raises(Exception): delivery.poll()
        assert binding.requests[-1]['@type'] == 'close'
        binding.receive = original
        try: delivery.start_next('worker')
        except ValueError: pass
        assert not any(r['@type'] == 'sendMessage' for r in binding.requests)



def test_unsupported_auth_state_never_retains_send_authority(tmp_path):
    from test_routing import ScoresRuntime, topic, route, destination, post, plan, running, approved
    from types import SimpleNamespace
    from datetime import datetime, timezone
    from threadrelay.delivery.auth import AuthController
    from threadrelay.delivery.telegram import NetworkPermit
    from test_telegram_auth import MemorySecretHandoff, state
    from threadrelay.storage.database import Database
    from threadrelay.storage.outbox import AccountStore
    from threadrelay.storage.routing import ConfigStore
    from threadrelay.storage.posts import PostStore
    from threadrelay.routing.policy import Router
    from threadrelay.routing.approvals import ApprovalStore
    from threadrelay.inference.decision import Classifier
    from threadrelay.delivery.sender import Sender
    now = datetime(2026, 10, 28, tzinfo=timezone.utc)
    with Database(tmp_path / 'app.db', clock=lambda: now) as db:
        binding = FixtureBinding(); client = TelegramClient(lambda: binding, FixtureVault(), tmp_path / 'sessions')
        session = running()
        auth = AuthController(client, AccountStore(db), MemorySecretHandoff(), clock=lambda: 0.0, lifecycle_lock=session.authority_lock)
        auth.begin('vault:test', pending=False)
        binding.events.append(state('WaitTdlibParameters')); auth.poll()
        for request in binding.requests[:]:
            extra = request.get('@extra', '')
            if extra.startswith('init:'):
                binding.events.append({'@client_id':42, '@type':'ok', '@extra':extra})
                auth.poll()
        auth.enable_clean_auth(NetworkPermit('clean_auth', 0, False))
        binding.events.append({'@client_id':42,'@type':'ok','@extra':binding.requests[-1]['@extra']}); auth.poll()
        binding.events.extend([state('Ready'), {'@client_id':42,'@type':'user','@extra':'auth:get-me','id':123,'type':{'@type':'userTypeRegular'}},
            {'@client_id':42,'@type':'updateConnectionState','state':{'@type':'connectionStateReady'}}])
        auth.poll(); auth.poll(); auth.poll()
        config = ConfigStore(db); config.save_topic(topic())
        sender = {'kind':'user','id':'123'}
        config.save_route(route(destination_id='456',account_id='123',account_epoch=0,sender=sender))
        config.save_destination(destination(account_id='123',chat_id='456',sender=sender), account_epoch=0)
        p = post(); PostStore(db).upsert(p); classifier = Classifier(ScoresRuntime()); router = Router(db,classifier=classifier)
        env = SimpleNamespace(db=db, config=config, post=p, classifier=classifier, router=router, approvals=ApprovalStore(router))
        proposal = plan(env)[0]; _delivery_id = router.stage(proposal, approval=approved(env, proposal))
        class VerifiedDestination:
            def is_current(self, delivery, proposal):
                return True
        delivery = Sender(router, session, auth, client, destination_authority=VerifiedDestination(), clock=lambda: now)
        binding.events.append(state('WaitPremiumPurchase'))
        delivery.poll()
        try: delivery.start_next('worker')
        except ValueError: pass
        assert not any(r['@type'] == 'sendMessage' for r in binding.requests)



def test_new_auth_challenge_suspends_old_ready_authority(tmp_path):
    from test_routing import ScoresRuntime, topic, route, destination, post, plan, running, approved
    from types import SimpleNamespace
    from datetime import datetime, timezone
    from threadrelay.delivery.auth import AuthController
    from threadrelay.delivery.telegram import NetworkPermit
    from test_telegram_auth import MemorySecretHandoff, state
    from threadrelay.storage.database import Database
    from threadrelay.storage.outbox import AccountStore
    from threadrelay.storage.routing import ConfigStore
    from threadrelay.storage.posts import PostStore
    from threadrelay.routing.policy import Router
    from threadrelay.routing.approvals import ApprovalStore
    from threadrelay.inference.decision import Classifier
    from threadrelay.delivery.sender import Sender
    now = datetime(2026, 10, 28, tzinfo=timezone.utc)
    with Database(tmp_path / 'app.db', clock=lambda: now) as db:
        binding = FixtureBinding(); client = TelegramClient(lambda: binding, FixtureVault(), tmp_path / 'sessions')
        session = running()
        auth = AuthController(client, AccountStore(db), MemorySecretHandoff(), clock=lambda: 0.0, lifecycle_lock=session.authority_lock)
        auth.begin('vault:test', pending=False)
        binding.events.append(state('WaitTdlibParameters')); auth.poll()
        for request in binding.requests[:]:
            extra = request.get('@extra', '')
            if extra.startswith('init:'):
                binding.events.append({'@client_id':42, '@type':'ok', '@extra':extra})
                auth.poll()
        auth.enable_clean_auth(NetworkPermit('clean_auth', 0, False))
        binding.events.append({'@client_id':42,'@type':'ok','@extra':binding.requests[-1]['@extra']}); auth.poll()
        binding.events.extend([state('Ready'), {'@client_id':42,'@type':'user','@extra':'auth:get-me','id':123,'type':{'@type':'userTypeRegular'}},
            {'@client_id':42,'@type':'updateConnectionState','state':{'@type':'connectionStateReady'}}])
        auth.poll(); auth.poll(); auth.poll()
        config = ConfigStore(db); config.save_topic(topic())
        sender = {'kind':'user','id':'123'}
        config.save_route(route(destination_id='456',account_id='123',account_epoch=0,sender=sender))
        config.save_destination(destination(account_id='123',chat_id='456',sender=sender), account_epoch=0)
        p = post(); PostStore(db).upsert(p); classifier = Classifier(ScoresRuntime()); router = Router(db,classifier=classifier)
        env = SimpleNamespace(db=db, config=config, post=p, classifier=classifier, router=router, approvals=ApprovalStore(router))
        proposal = plan(env)[0]; _delivery_id = router.stage(proposal, approval=approved(env, proposal))
        class VerifiedDestination:
            def is_current(self, delivery, proposal):
                return True
        delivery = Sender(router, session, auth, client, destination_authority=VerifiedDestination(), clock=lambda: now)
        binding.events.append(state('WaitCode'))
        delivery.poll()
        assert session.snapshot['state'] == 'needs_attention'
        try: delivery.start_next('worker')
        except ValueError: pass
        assert not any(r['@type'] == 'sendMessage' for r in binding.requests)




def test_sender_selection_stops_on_durable_revocation(tmp_path):
    from threadrelay.delivery.destinations import DestinationVerifier
    from threadrelay.delivery.auth import AuthController
    from threadrelay.delivery.telegram import NetworkPermit
    from test_telegram_auth import MemorySecretHandoff, state
    from threadrelay.storage.database import Database
    from threadrelay.storage.outbox import AccountStore
    from threadrelay.storage.routing import ConfigStore
    from threadrelay.session import SessionController
    with Database(tmp_path / 'app.db') as db:
        session = SessionController(); binding = FixtureBinding()
        client = TelegramClient(lambda: binding, FixtureVault(), tmp_path / 'sessions')
        auth = AuthController(client, AccountStore(db), MemorySecretHandoff(), clock=lambda: 0, lifecycle_lock=session.authority_lock)
        auth.begin('vault:test', pending=False)
        binding.events.append(state('WaitTdlibParameters')); auth.poll()
        for request in binding.requests[:]:
            if request.get('@extra','').startswith('init:'):
                binding.events.append({'@client_id':42,'@type':'ok','@extra':request['@extra']}); auth.poll()
        auth.enable_clean_auth(NetworkPermit('clean_auth', 0, False))
        binding.events.append({'@client_id':42,'@type':'ok','@extra':binding.requests[-1]['@extra']}); auth.poll()
        binding.events.extend([state('Ready'), {'@client_id':42,'@type':'user','@extra':'auth:get-me','id':123,'type':{'@type':'userTypeRegular'}},
            {'@client_id':42,'@type':'updateConnectionState','state':{'@type':'connectionStateReady'}}])
        auth.poll(); auth.poll(); auth.poll()
        verifier = DestinationVerifier(client, auth, ConfigStore(db), session)
        selected={'kind':'chat','id':'456'}
        verifier.select_sender('456', selected)
        req=binding.requests[-1]; assert req['@type'] == 'getChatAvailableMessageSenders'
        AccountStore(db).invalidate_epoch('123')
        binding.events.append({'@client_id':42,'@type':'chatMessageSenders','@extra':req['@extra'],
            'senders':[{'sender':{'@type':'messageSenderChat','chat_id':456},'needs_premium':False}]})
        try: auth.poll()
        except Exception: pass
        assert not any(r['@type']=='setChatMessageSender' for r in binding.requests)



def test_network_ready_requires_current_enable_acknowledgment(tmp_path):
    from threadrelay.delivery.auth import AuthController
    from threadrelay.delivery.telegram import NetworkPermit
    from test_telegram_auth import MemorySecretHandoff, state
    from threadrelay.storage.database import Database
    from threadrelay.storage.outbox import AccountStore
    with Database(tmp_path/'app.db') as db:
        binding=FixtureBinding(); client=TelegramClient(lambda:binding,FixtureVault(),tmp_path/'sessions')
        auth=AuthController(client,AccountStore(db),MemorySecretHandoff(),clock=lambda:0)
        auth.begin('vault:test',pending=False)
        binding.events.append(state('WaitTdlibParameters')); auth.poll()
        for req in binding.requests[:]:
            if req.get('@extra','').startswith('init:'):
                binding.events.append({'@client_id':42,'@type':'ok','@extra':req['@extra']}); auth.poll()
        auth.enable_clean_auth(NetworkPermit('clean_auth',0,False))
        binding.events.append({'@client_id':42,'@type':'updateConnectionState','state':{'@type':'connectionStateReady'}}); auth.poll()
        assert not client.transport_ready
        binding.events.append({'@client_id':42,'@type':'error','@extra':binding.requests[-1]['@extra'],'message':'discard'}); auth.poll()
        assert not client.transport_ready
        assert binding.requests[-1]['@type'] == 'close'


def test_premium_sender_selection_is_refused(tmp_path):
    from threadrelay.delivery.destinations import DestinationVerifier
    from threadrelay.delivery.auth import AuthController
    from threadrelay.delivery.telegram import NetworkPermit
    from test_telegram_auth import MemorySecretHandoff, state
    from threadrelay.storage.database import Database
    from threadrelay.storage.outbox import AccountStore
    from threadrelay.storage.routing import ConfigStore
    from threadrelay.session import SessionController
    with Database(tmp_path / 'app.db') as db:
        session = SessionController(); binding = FixtureBinding()
        client = TelegramClient(lambda: binding, FixtureVault(), tmp_path / 'sessions')
        auth = AuthController(client, AccountStore(db), MemorySecretHandoff(), clock=lambda: 0, lifecycle_lock=session.authority_lock)
        auth.begin('vault:test', pending=False)
        binding.events.append(state('WaitTdlibParameters')); auth.poll()
        for request in binding.requests[:]:
            if request.get('@extra','').startswith('init:'):
                binding.events.append({'@client_id':42,'@type':'ok','@extra':request['@extra']}); auth.poll()
        auth.enable_clean_auth(NetworkPermit('clean_auth', 0, False))
        binding.events.append({'@client_id':42,'@type':'ok','@extra':binding.requests[-1]['@extra']}); auth.poll()
        binding.events.extend([state('Ready'), {'@client_id':42,'@type':'user','@extra':'auth:get-me','id':123,'type':{'@type':'userTypeRegular'}},
            {'@client_id':42,'@type':'updateConnectionState','state':{'@type':'connectionStateReady'}}])
        auth.poll(); auth.poll(); auth.poll()
        verifier = DestinationVerifier(client, auth, ConfigStore(db), session)
        selected={'kind':'chat','id':'456'}
        verifier.select_sender('456', selected)
        req=binding.requests[-1]; assert req['@type'] == 'getChatAvailableMessageSenders'
        binding.events.append({'@client_id':42,'@type':'chatMessageSenders','@extra':req['@extra'],
            'senders':[{'sender':{'@type':'messageSenderChat','chat_id':456},'needs_premium':True}]}); auth.poll()
        assert not any(r['@type']=='setChatMessageSender' for r in binding.requests)
        import pytest
        with pytest.raises(Exception): verifier.finish_selection()



def test_paid_private_destination_is_refused(tmp_path):
    from threadrelay.delivery.destinations import DestinationVerifier
    from threadrelay.delivery.auth import AuthController
    from threadrelay.delivery.telegram import NetworkPermit
    from test_telegram_auth import MemorySecretHandoff, state
    from threadrelay.storage.database import Database
    from threadrelay.storage.outbox import AccountStore
    from threadrelay.storage.routing import ConfigStore
    from threadrelay.session import SessionController
    with Database(tmp_path / 'app.db') as db:
        session = SessionController(); binding = FixtureBinding()
        client = TelegramClient(lambda: binding, FixtureVault(), tmp_path / 'sessions')
        auth = AuthController(client, AccountStore(db), MemorySecretHandoff(), clock=lambda: 0, lifecycle_lock=session.authority_lock)
        auth.begin('vault:test', pending=False)
        binding.events.append(state('WaitTdlibParameters')); auth.poll()
        for request in binding.requests[:]:
            if request.get('@extra','').startswith('init:'):
                binding.events.append({'@client_id':42,'@type':'ok','@extra':request['@extra']}); auth.poll()
        auth.enable_clean_auth(NetworkPermit('clean_auth', 0, False))
        binding.events.append({'@client_id':42,'@type':'ok','@extra':binding.requests[-1]['@extra']}); auth.poll()
        binding.events.extend([state('Ready'), {'@client_id':42,'@type':'user','@extra':'auth:get-me','id':123,'type':{'@type':'userTypeRegular'}},
            {'@client_id':42,'@type':'updateConnectionState','state':{'@type':'connectionStateReady'}}])
        auth.poll(); auth.poll(); auth.poll()
        verifier = DestinationVerifier(client, auth, ConfigStore(db), session)
        verifier.begin_private('456')
        chat_request = binding.requests[-1]
        assert chat_request['@type'] == 'getChat'
        binding.events.append({'@client_id':42,'@type':'chat','@extra':chat_request['@extra'],'id':456,
            'title':'Synthetic target','type':{'@type':'chatTypePrivate','user_id':789},
            'message_sender_id':{'@type':'messageSenderUser','user_id':123}, 'last_message':{'text':'discard me'}})
        auth.poll()
        paid_request = binding.requests[-1]
        assert paid_request['@type'] == 'canSendMessageToUser' and paid_request['only_local'] is False
        binding.events.append({'@client_id':42,'@type':'canSendMessageToUserResultUserHasPaidMessages',
            '@extra':paid_request['@extra'],'outgoing_paid_message_star_count':1}); auth.poll()
        import pytest
        with pytest.raises(Exception): verifier.finish()



def test_unmatched_sender_and_other_client_updates_are_discarded(tmp_path):
    binding = FixtureBinding(); client = TelegramClient(lambda: binding, FixtureVault(), tmp_path/'sessions')
    client.initialize_offline('vault:test')
    client.register_attempt(delivery_id='d', attempt_id='a', request_extra='x', account_id='123', epoch=2,
        chat_id='456', sender={'kind':'user','id':'123'}, destination_version=1, content_hash='hash')
    for event in [
        {'@client_id':999,'@type':'message','@extra':'x','id':-7,'chat_id':456,'sender_id':{'@type':'messageSenderUser','user_id':123}},
        {'@client_id':42,'@type':'message','@extra':'x','id':-7,'chat_id':456,'sender_id':{'@type':'messageSenderUser','user_id':999}},
        {'@client_id':42,'@type':'updateUser','phone_number':'private'},
        {'@client_id':42,'@type':'updateNewMessage','message':{'content':'private'}}]:
        binding.events.append(event)
        assert pump_delivery(client) is None


def test_delete_before_initial_response_becomes_unknown_in_order(tmp_path):
    binding=FixtureBinding(); client=TelegramClient(lambda:binding,FixtureVault(),tmp_path/'sessions')
    client.initialize_offline('vault:test')
    client.register_attempt(delivery_id='d',attempt_id='a',request_extra='x',account_id='123',epoch=2,
        chat_id='456',sender={'kind':'user','id':'123'},destination_version=1,content_hash='hash')
    binding.events.append({'@client_id':42,'@type':'updateDeleteMessages','chat_id':456,'message_ids':[-7]})
    assert pump_delivery(client) is None
    binding.events.append({'@client_id':42,'@type':'message','@extra':'x','id':-7,'chat_id':456,
        'sender_id':{'@type':'messageSenderUser','user_id':123}})
    assert pump_delivery(client).kind == 'initiated'
    assert client.take_delivery_event().kind == 'unknown'


def test_zero_message_identifier_cannot_confirm_delivery(tmp_path):
    binding=FixtureBinding(); client=TelegramClient(lambda:binding,FixtureVault(),tmp_path/'sessions')
    client.initialize_offline('vault:test')
    client.register_attempt(delivery_id='d',attempt_id='a',request_extra='x',account_id='123',epoch=2,
        chat_id='456',sender={'kind':'user','id':'123'},destination_version=1,content_hash='hash')
    binding.events.append({'@client_id':42,'@type':'message','@extra':'x','id':-7,'chat_id':456,
        'sender_id':{'@type':'messageSenderUser','user_id':123}}); pump_delivery(client)
    binding.events.append({'@client_id':42,'@type':'updateMessageSendSucceeded','old_message_id':-7,
        'message':{'id':0,'chat_id':456,'sender_id':{'@type':'messageSenderUser','user_id':123}}})
    assert pump_delivery(client) is None


def test_sender_selection_confirmation_rejects_newer_account_epoch(tmp_path):
    from threadrelay.delivery.destinations import DestinationVerifier
    from threadrelay.delivery.auth import AuthController
    from threadrelay.delivery.telegram import NetworkPermit
    from test_telegram_auth import MemorySecretHandoff, state
    from threadrelay.storage.database import Database
    from threadrelay.storage.outbox import AccountStore
    from threadrelay.storage.routing import ConfigStore
    from threadrelay.session import SessionController
    with Database(tmp_path / 'app.db') as db:
        session = SessionController(); binding = FixtureBinding()
        client = TelegramClient(lambda: binding, FixtureVault(), tmp_path / 'sessions')
        auth = AuthController(client, AccountStore(db), MemorySecretHandoff(), clock=lambda: 0, lifecycle_lock=session.authority_lock)
        auth.begin('vault:test', pending=False)
        binding.events.append(state('WaitTdlibParameters')); auth.poll()
        for request in binding.requests[:]:
            if request.get('@extra','').startswith('init:'):
                binding.events.append({'@client_id':42,'@type':'ok','@extra':request['@extra']}); auth.poll()
        auth.enable_clean_auth(NetworkPermit('clean_auth', 0, False))
        binding.events.append({'@client_id':42,'@type':'ok','@extra':binding.requests[-1]['@extra']}); auth.poll()
        binding.events.extend([state('Ready'), {'@client_id':42,'@type':'user','@extra':'auth:get-me','id':123,'type':{'@type':'userTypeRegular'}},
            {'@client_id':42,'@type':'updateConnectionState','state':{'@type':'connectionStateReady'}}])
        auth.poll(); auth.poll(); auth.poll()
        verifier = DestinationVerifier(client, auth, ConfigStore(db), session)
        selected={'kind':'chat','id':'456'}
        verifier.select_sender('456', selected)
        req=binding.requests[-1]; assert req['@type'] == 'getChatAvailableMessageSenders'
        binding.events.append({'@client_id':42,'@type':'chatMessageSenders','@extra':req['@extra'],
            'senders':[{'sender':{'@type':'messageSenderChat','chat_id':456},'needs_premium':False}]}); auth.poll()
        req=binding.requests[-1]; assert req['@type'] == 'setChatMessageSender'
        binding.events.append({'@client_id':42,'@type':'updateChatMessageSender','chat_id':456,'message_sender_id':{'@type':'messageSenderChat','chat_id':456}}); auth.poll()
        binding.events.append({'@client_id':42,'@type':'ok','@extra':req['@extra']}); auth.poll()
        req=binding.requests[-1]; assert req['@type'] == 'getChat'
        binding.events.append({'@client_id':42,'@type':'chat','@extra':req['@extra'],'id':456,
            'message_sender_id':{'@type':'messageSenderChat','chat_id':456}}); auth.poll()
        AccountStore(db).invalidate_epoch('123')
        import pytest
        with pytest.raises(Exception): verifier.finish_selection()




def test_retry_local_query_timeout_is_unknown(tmp_path):
    from test_routing import ScoresRuntime, topic, route, destination, post, plan, running, approved
    from types import SimpleNamespace
    from datetime import datetime, timezone
    from threadrelay.delivery.auth import AuthController
    from threadrelay.delivery.telegram import NetworkPermit
    from test_telegram_auth import MemorySecretHandoff, state
    from threadrelay.storage.database import Database
    from threadrelay.storage.outbox import AccountStore, Outbox
    from threadrelay.storage.routing import ConfigStore
    from threadrelay.storage.posts import PostStore
    from threadrelay.routing.policy import Router
    from threadrelay.routing.approvals import ApprovalStore
    from threadrelay.inference.decision import Classifier
    from threadrelay.delivery.sender import Sender
    now = datetime(2026, 10, 28, tzinfo=timezone.utc)
    with Database(tmp_path / 'app.db', clock=lambda: now) as db:
        binding = FixtureBinding(); client = TelegramClient(lambda: binding, FixtureVault(), tmp_path / 'sessions')
        session = running()
        auth = AuthController(client, AccountStore(db), MemorySecretHandoff(), clock=lambda: 0.0, lifecycle_lock=session.authority_lock)
        auth.begin('vault:test', pending=False)
        binding.events.append(state('WaitTdlibParameters')); auth.poll()
        for request in binding.requests[:]:
            extra = request.get('@extra', '')
            if extra.startswith('init:'):
                binding.events.append({'@client_id':42, '@type':'ok', '@extra':extra})
                auth.poll()
        auth.enable_clean_auth(NetworkPermit('clean_auth', 0, False))
        binding.events.append({'@client_id':42,'@type':'ok','@extra':binding.requests[-1]['@extra']}); auth.poll()
        binding.events.extend([state('Ready'), {'@client_id':42,'@type':'user','@extra':'auth:get-me','id':123,'type':{'@type':'userTypeRegular'}},
            {'@client_id':42,'@type':'updateConnectionState','state':{'@type':'connectionStateReady'}}])
        auth.poll(); auth.poll(); auth.poll()
        config = ConfigStore(db); config.save_topic(topic())
        sender = {'kind':'user','id':'123'}
        config.save_route(route(destination_id='456',account_id='123',account_epoch=0,sender=sender))
        config.save_destination(destination(account_id='123',chat_id='456',sender=sender), account_epoch=0)
        p = post(); PostStore(db).upsert(p); classifier = Classifier(ScoresRuntime()); router = Router(db,classifier=classifier)
        env = SimpleNamespace(db=db, config=config, post=p, classifier=classifier, router=router, approvals=ApprovalStore(router))
        proposal = plan(env)[0]; delivery_id = router.stage(proposal, approval=approved(env, proposal))
        class VerifiedDestination:
            def is_current(self, delivery, proposal):
                return True
        elapsed = [0.0]
        delivery = Sender(router, session, auth, client, destination_authority=VerifiedDestination(), clock=lambda: now, monotonic=lambda: elapsed[0])
        result = delivery.start_next('worker')
        assert result is not None and Outbox(db).get(delivery_id)['state'] == 'sending'
        req = binding.requests[-1]
        assert req['@type'] == 'sendMessage' and req['chat_id'] == 456 and req['topic_id'] is None
        assert req['options']['paid_message_star_count'] == 0 and req['options']['allow_paid_broadcast'] is False
        assert req['input_message_content']['clear_draft'] is False
        assert Outbox(db).attempts(delivery_id)[0]['request_extra'] == req['@extra']
        binding.events.append({'@client_id':42,'@type':'message','@extra':req['@extra'],'id':-7,'chat_id':456,
            'sender_id':{'@type':'messageSenderUser','user_id':123}})
        delivery.poll()
        assert Outbox(db).get(delivery_id)['state'] == 'sending'
        assert Outbox(db).get(delivery_id)['temporary_message_id'] == '-7'
        failure = {'@type':'messageSendingStateFailed','can_retry':True,'retry_after':2.0,'need_another_sender':False,
            'need_another_reply_quote':False,'need_drop_reply':False,'required_paid_message_star_count':0}
        binding.events.append({'@client_id':42,'@type':'updateMessageSendFailed','old_message_id':-7,
            'message':{'id':-8,'chat_id':456,'sender_id':{'@type':'messageSenderUser','user_id':123},'sending_state':failure}})
        delivery.poll()
        assert Outbox(db).get(delivery_id)['state'] == 'retryable'
        delivery.prepare_retry(delivery_id)
        _local = binding.requests[-1]
        delivery.timeout(delivery_id)
        assert Outbox(db).get(delivery_id)['state'] == 'unknown'
        assert delivery.retry(delivery_id, 'worker') is None
        assert not any(r['@type'] == 'resendMessages' for r in binding.requests)


def test_failure_without_original_zero_paid_receipt_is_unknown(tmp_path):
    from test_routing import ScoresRuntime, topic, route, destination, post, plan, running, approved
    from types import SimpleNamespace
    from datetime import datetime, timezone
    from threadrelay.delivery.auth import AuthController
    from threadrelay.delivery.telegram import NetworkPermit
    from test_telegram_auth import MemorySecretHandoff, state
    from threadrelay.storage.database import Database
    from threadrelay.storage.outbox import AccountStore, Outbox
    from threadrelay.storage.routing import ConfigStore
    from threadrelay.storage.posts import PostStore
    from threadrelay.routing.policy import Router
    from threadrelay.routing.approvals import ApprovalStore
    from threadrelay.inference.decision import Classifier
    from threadrelay.delivery.sender import Sender
    now = datetime(2026, 10, 28, tzinfo=timezone.utc)
    with Database(tmp_path / 'app.db', clock=lambda: now) as db:
        binding = FixtureBinding(); client = TelegramClient(lambda: binding, FixtureVault(), tmp_path / 'sessions')
        session = running()
        auth = AuthController(client, AccountStore(db), MemorySecretHandoff(), clock=lambda: 0.0, lifecycle_lock=session.authority_lock)
        auth.begin('vault:test', pending=False)
        binding.events.append(state('WaitTdlibParameters')); auth.poll()
        for request in binding.requests[:]:
            extra = request.get('@extra', '')
            if extra.startswith('init:'):
                binding.events.append({'@client_id':42, '@type':'ok', '@extra':extra})
                auth.poll()
        auth.enable_clean_auth(NetworkPermit('clean_auth', 0, False))
        binding.events.append({'@client_id':42,'@type':'ok','@extra':binding.requests[-1]['@extra']}); auth.poll()
        binding.events.extend([state('Ready'), {'@client_id':42,'@type':'user','@extra':'auth:get-me','id':123,'type':{'@type':'userTypeRegular'}},
            {'@client_id':42,'@type':'updateConnectionState','state':{'@type':'connectionStateReady'}}])
        auth.poll(); auth.poll(); auth.poll()
        config = ConfigStore(db); config.save_topic(topic())
        sender = {'kind':'user','id':'123'}
        config.save_route(route(destination_id='456',account_id='123',account_epoch=0,sender=sender))
        config.save_destination(destination(account_id='123',chat_id='456',sender=sender), account_epoch=0)
        p = post(); PostStore(db).upsert(p); classifier = Classifier(ScoresRuntime()); router = Router(db,classifier=classifier)
        env = SimpleNamespace(db=db, config=config, post=p, classifier=classifier, router=router, approvals=ApprovalStore(router))
        proposal = plan(env)[0]; delivery_id = router.stage(proposal, approval=approved(env, proposal))
        class VerifiedDestination:
            def is_current(self, delivery, proposal):
                return True
        elapsed = [0.0]
        delivery = Sender(router, session, auth, client, destination_authority=VerifiedDestination(), clock=lambda: now, monotonic=lambda: elapsed[0])
        result = delivery.start_next('worker')
        assert result is not None and Outbox(db).get(delivery_id)['state'] == 'sending'
        req = binding.requests[-1]
        assert req['@type'] == 'sendMessage' and req['chat_id'] == 456 and req['topic_id'] is None
        assert req['options']['paid_message_star_count'] == 0 and req['options']['allow_paid_broadcast'] is False
        assert req['input_message_content']['clear_draft'] is False
        assert Outbox(db).attempts(delivery_id)[0]['request_extra'] == req['@extra']
        binding.events.append({'@client_id':42,'@type':'message','@extra':req['@extra'],'id':-7,'chat_id':456,
            'sender_id':{'@type':'messageSenderUser','user_id':123}})
        delivery.poll()
        assert Outbox(db).get(delivery_id)['state'] == 'sending'
        assert Outbox(db).get(delivery_id)['temporary_message_id'] == '-7'
        failure = {'@type':'messageSendingStateFailed','can_retry':True,'retry_after':2.0,'need_another_sender':False,
            'need_another_reply_quote':False,'need_drop_reply':False,'required_paid_message_star_count':0}
        delivery._originals.clear()
        binding.events.append({'@client_id':42,'@type':'updateMessageSendFailed','old_message_id':-7,
            'message':{'id':-8,'chat_id':456,'sender_id':{'@type':'messageSenderUser','user_id':123},'sending_state':failure}})
        delivery.poll()
        assert Outbox(db).get(delivery_id)['state'] == 'unknown'
        assert delivery.retry(delivery_id, 'worker') is None
