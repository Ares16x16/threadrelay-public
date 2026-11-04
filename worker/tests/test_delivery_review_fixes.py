"""Stage 7B review regressions; synthetic bindings and local SQLite only."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from test_storage import snapshot, post, delivery, NOW
from test_telegram_auth import FixtureBinding, FixtureVault
from threadrelay.delivery.telegram import TelegramClient
from threadrelay.delivery.sender import Sender
from threadrelay.session import SessionController
from threadrelay.storage.database import Database, encode
from threadrelay.storage.outbox import AccountStore, Outbox
from threadrelay.storage.posts import PostStore


def make_sender(tmp_path):
    db = Database(tmp_path / 'app.db', clock=lambda: NOW)
    AccountStore(db).activate(snapshot(account='123'))
    session = SessionController(setup_check=lambda _: {'allowed':True,'reason':None})
    session.start({'feed_url':'https://www.threads.net/', 'mode':'review'})
    binding = FixtureBinding()
    client = TelegramClient(lambda:binding, FixtureVault(), tmp_path/'sessions')
    client.initialize_offline('vault:test')
    class Auth:
        _client=client
        _lifecycle_lock=session.authority_lock
        status='ready'
        def poll(self): return client.authorization_event()
        def snapshot(self):
            return SimpleNamespace(account_id='123',epoch=1,auth_state='ready',network_state='online',revocation_pending=False)
    sender=Sender(SimpleNamespace(db=db, start_with_connection=lambda *a,**k: None, rate_delay=lambda _:0), session, Auth(), client, destination_authority=None,
                  clock=lambda:datetime(2026,10,28,tzinfo=timezone.utc))
    return db, session, binding, client, sender


def stage_pending(db, index):
    p=post(post_id=f'synthetic-{index}')
    PostStore(db).upsert(p)
    value=delivery(id=f'delivery-{index}', post_id=p['post_id'], account_id='123', account_epoch=1,
        destination_id='456', sender={'kind':'user','id':'123'}, route_refs=[{'route_id':'route-1','route_version':1,'approval_id':None}])
    Outbox(db).enqueue(value)
    return value


def register(client, index, **overrides):
    client.register_attempt(delivery_id=f'delivery-{index}',attempt_id=f'attempt-{index}',request_extra=f'extra-{index}',
        account_id='123',epoch=1,chat_id='456',sender={'kind':'user','id':'123'},destination_version=1,content_hash='synthetic',**overrides)


def test_more_than_128_completed_attempts_are_persisted_and_reclaimed(tmp_path):
    db, session, binding, client, sender=make_sender(tmp_path)
    try:
        outbox=Outbox(db)
        # Test-only transport readiness; the injected binding has no native network.
        client._offline_pending=False; client._config_started=True
        client._network_enabled=True; client._connection_ready=True
        for i in range(140):
            value=stage_pending(db,i); claim=outbox.claim_next('worker')
            with db.transaction() as con:
                con.execute('INSERT INTO routing_plans(id,post_id,payload) VALUES (?,?,?)', (f'plan-{i}', value['post_id'], encode({'destination_version':1})))
                con.execute('INSERT INTO routing_delivery_context(delivery_id,plan_id,initiated_attempt_id) VALUES (?,?,?)', (value['id'],f'plan-{i}',claim['attempt_id']))
            client.register_attempt(delivery_id=claim['id'],attempt_id=claim['attempt_id'],request_extra=claim['request_extra'],
                account_id='123',epoch=1,chat_id='456',sender={'kind':'user','id':'123'},destination_version=1,content_hash=claim['content_hash'])
            client.initiate_message(chat_id='456', text='Synthetic message', request_extra=claim['request_extra'])
            sender._originals.add((claim['id'],claim['attempt_id']))
            source={'@type':'messageSenderUser','user_id':123}
            binding.events.append({'@client_id':42,'@type':'message','@extra':claim['request_extra'],'id':-i-1,'chat_id':456,'sender_id':source}); sender.poll()
            binding.events.append({'@client_id':42,'@type':'updateMessageSendSucceeded','old_message_id':-i-1,
                'message':{'id':1000+i,'chat_id':456,'sender_id':source}}); sender.poll()
            assert outbox.get(value['id'])['state'] == 'sent'
        assert len(client._attempt_events.attempts) == 0
        assert len([request for request in binding.requests if request['@type']=='sendMessage']) == 140
        assert not sender._originals
    finally:
        db.close()


def test_full_live_capacity_does_not_claim_or_lose_pending(tmp_path):
    db, session, binding, client, sender=make_sender(tmp_path)
    try:
        value=stage_pending(db,0)
        for i in range(128): register(client,i)
        assert sender.start_next('worker') is None
        assert Outbox(db).get(value['id'])['state'] == 'pending'
        assert Outbox(db).attempts(value['id']) == []
        assert not any(r['@type']=='sendMessage' for r in binding.requests)
    finally:
        db.close()


def test_retryable_and_outstanding_query_are_not_evicted_early(tmp_path):
    db, session, binding, client, sender=make_sender(tmp_path)
    try:
        register(client,0); source={'@type':'messageSenderUser','user_id':123}
        binding.events.append({'@client_id':42,'@type':'message','@extra':'extra-0','id':-7,'chat_id':456,'sender_id':source}); client.authorization_event(); client.take_delivery_event()
        failed={'@type':'messageSendingStateFailed','can_retry':True,'retry_after':0,'need_another_sender':False,
            'need_another_reply_quote':False,'need_drop_reply':False,'required_paid_message_star_count':0}
        binding.events.append({'@client_id':42,'@type':'updateMessageSendFailed','old_message_id':-7,
            'message':{'id':-8,'chat_id':456,'sender_id':source,'sending_state':failed}}); client.authorization_event(); client.take_delivery_event()
        assert not client.retire_attempt('extra-0')
        client.verify_failed_attempt('extra-0','-8'); query=binding.requests[-1]
        assert not client.retire_attempt('extra-0',abandon_retry=True)
        assert 'extra-0' in client._attempt_events.attempts
        binding.events.append({'@client_id':42,'@type':'error','@extra':query['@extra'],'message':'discard'}); client.authorization_event()
        assert 'extra-0' not in client._attempt_events.attempts
        assert client.take_delivery_event() is None
    finally:
        db.close()


def test_retired_duplicate_cannot_bind_to_a_new_attempt(tmp_path):
    db, session, binding, client, sender=make_sender(tmp_path)
    try:
        register(client,0); source={'@type':'messageSenderUser','user_id':123}
        binding.events.append({'@client_id':42,'@type':'message','@extra':'extra-0','id':-7,'chat_id':456,'sender_id':source}); client.authorization_event(); client.take_delivery_event()
        update={'@client_id':42,'@type':'updateMessageSendSucceeded','old_message_id':-7,
            'message':{'id':11,'chat_id':456,'sender_id':source}}
        binding.events.append(update); client.authorization_event(); client.take_delivery_event()
        assert client.retire_attempt('extra-0')
        register(client,1)
        binding.events.append(update); client.authorization_event()
        binding.events.append({'@client_id':42,'@type':'message','@extra':'extra-1','id':-7,'chat_id':456,'sender_id':source}); client.authorization_event()
        assert client.take_delivery_event().kind == 'initiated'
        assert client.take_delivery_event() is None
    finally:
        db.close()


def test_timeout_reclaims_persisted_unknown_correlation(tmp_path):
    db, session, binding, client, sender=make_sender(tmp_path)
    try:
        value=stage_pending(db,0); outbox=Outbox(db); claim=outbox.claim_next('worker')
        client.register_attempt(delivery_id=claim['id'],attempt_id=claim['attempt_id'],request_extra=claim['request_extra'],
            account_id='123',epoch=1,chat_id='456',sender={'kind':'user','id':'123'},destination_version=1,content_hash=claim['content_hash'])
        sender._originals.add((claim['id'],claim['attempt_id']))
        sender.timeout(value['id'])
        assert outbox.get(value['id'])['state']=='unknown'
        assert claim['request_extra'] not in client._attempt_events.attempts
        assert not sender._originals
    finally:
        db.close()


@pytest.mark.parametrize('state', ['sending','retryable','unknown'])
def test_running_reconnect_refuses_unresolved_native_uncertainty(tmp_path, state):
    db, session, binding, client, sender=make_sender(tmp_path)
    try:
        stage_pending(db,0); outbox=Outbox(db); claim=outbox.claim_next('worker')
        if state != 'sending':
            assert outbox.transition(claim['id'],'sending',state,{'delivery_id':claim['id'],'attempt_id':claim['attempt_id'],
                'request_extra':claim['request_extra'],'temporary_message_id':'-7','final_message_id':None,'state':state})
        session.stop(); session.start({'feed_url':'https://www.threads.net/', 'mode':'review'})
        calls=[]; client.enable_network=lambda permit, **kwargs:calls.append(permit)
        with pytest.raises(ValueError): sender.enable_running_network()
        assert calls == [] and outbox.get(claim['id'])['state']==state
    finally:
        db.close()


def test_pending_only_is_not_native_uncertainty_for_reconnect(tmp_path):
    db, session, binding, client, sender=make_sender(tmp_path)
    try:
        value=stage_pending(db,0); calls=[]; client.enable_network=lambda permit, **kwargs:calls.append(permit)
        sender.enable_running_network()
        assert len(calls)==1 and calls[0].pending is False
        assert Outbox(db).get(value['id'])['state']=='pending'
    finally:
        db.close()


def test_unavailable_in_memory_native_evidence_denies_reconnect(tmp_path):
    db, session, binding, client, sender=make_sender(tmp_path)
    try:
        register(client,0)  # A live native attempt without a corresponding app row.
        calls=[]; client.enable_network=lambda permit, **kwargs:calls.append(permit)
        with pytest.raises(ValueError): sender.enable_running_network()
        assert calls == []
    finally:
        db.close()


def test_revoked_account_denies_reconnect_even_without_pending(tmp_path):
    db, session, binding, client, sender=make_sender(tmp_path)
    try:
        AccountStore(db).invalidate_epoch('123'); calls=[]; client.enable_network=lambda permit, **kwargs:calls.append(permit)
        with pytest.raises(ValueError): sender.enable_running_network()
        assert calls == []
    finally:
        db.close()


def test_raw_network_permit_cannot_override_pending_uncertainty(tmp_path):
    from threadrelay.delivery.telegram import NetworkPermit, TelegramBoundaryError
    db, session, binding, client, sender=make_sender(tmp_path)
    try:
        client._offline_pending=False; client._config_started=True
        with pytest.raises(TelegramBoundaryError):
            client.enable_network(NetworkPermit('running_reconciliation',1,True))
        assert not any(r.get('type',{}).get('@type')=='networkTypeOther' for r in binding.requests)
    finally:
        db.close()


def test_raw_empty_permit_cannot_override_live_correlation(tmp_path):
    from threadrelay.delivery.telegram import NetworkPermit, TelegramBoundaryError
    db, session, binding, client, sender=make_sender(tmp_path)
    try:
        register(client,0)
        client._offline_pending=False; client._config_started=True
        with pytest.raises(TelegramBoundaryError):
            client.enable_network(NetworkPermit('running_reconciliation',1,False))
        assert not any(r.get('type',{}).get('@type')=='networkTypeOther' for r in binding.requests)
    finally:
        db.close()


def test_uncertain_initiation_reclaims_only_after_unknown_is_durable(tmp_path):
    db, session, binding, client, sender=make_sender(tmp_path)
    try:
        stage_pending(db,0); outbox=Outbox(db); claim=outbox.claim_next('worker')
        client.register_attempt(delivery_id=claim['id'],attempt_id=claim['attempt_id'],request_extra=claim['request_extra'],
            account_id='123',epoch=1,chat_id='456',sender={'kind':'user','id':'123'},destination_version=1,content_hash=claim['content_hash'])
        sender._originals.add((claim['id'],claim['attempt_id']))
        sender._unknown(claim)
        assert outbox.get(claim['id'])['state']=='unknown'
        assert claim['request_extra'] not in client._attempt_events.attempts
        assert not sender._originals
    finally:
        db.close()


def test_terminal_event_after_durable_revocation_is_reclaimed_without_changing_unknown(tmp_path):
    db, session, binding, client, sender=make_sender(tmp_path)
    try:
        stage_pending(db,0); outbox=Outbox(db); claim=outbox.claim_next('worker')
        client.register_attempt(delivery_id=claim['id'],attempt_id=claim['attempt_id'],request_extra=claim['request_extra'],
            account_id='123',epoch=1,chat_id='456',sender={'kind':'user','id':'123'},destination_version=1,content_hash=claim['content_hash'])
        sender._originals.add((claim['id'],claim['attempt_id']))
        source={'@type':'messageSenderUser','user_id':123}
        binding.events.append({'@client_id':42,'@type':'message','@extra':claim['request_extra'],'id':-7,'chat_id':456,'sender_id':source}); sender.poll()
        AccountStore(db).invalidate_epoch('123')
        binding.events.append({'@client_id':42,'@type':'updateMessageSendSucceeded','old_message_id':-7,
            'message':{'id':11,'chat_id':456,'sender_id':source}}); sender.poll()
        assert outbox.get(claim['id'])['state']=='unknown'
        assert claim['request_extra'] not in client._attempt_events.attempts
        assert not sender._originals
    finally:
        db.close()


def test_stop_reclaims_retry_evidence_already_cancelled_by_revocation(tmp_path):
    db, session, binding, client, sender=make_sender(tmp_path)
    try:
        stage_pending(db,0); outbox=Outbox(db); claim=outbox.claim_next('worker')
        client.register_attempt(delivery_id=claim['id'],attempt_id=claim['attempt_id'],request_extra=claim['request_extra'],
            account_id='123',epoch=1,chat_id='456',sender={'kind':'user','id':'123'},destination_version=1,content_hash=claim['content_hash'])
        source={'@type':'messageSenderUser','user_id':123}
        binding.events.append({'@client_id':42,'@type':'message','@extra':claim['request_extra'],'id':-7,'chat_id':456,'sender_id':source}); client.authorization_event(); client.take_delivery_event()
        failure={'@type':'messageSendingStateFailed','can_retry':True,'retry_after':0,'need_another_sender':False,
            'need_another_reply_quote':False,'need_drop_reply':False,'required_paid_message_star_count':0}
        binding.events.append({'@client_id':42,'@type':'updateMessageSendFailed','old_message_id':-7,
            'message':{'id':-8,'chat_id':456,'sender_id':source,'sending_state':failure}}); client.authorization_event(); client.take_delivery_event()
        assert outbox.transition(claim['id'],'sending','retryable',{'delivery_id':claim['id'],'attempt_id':claim['attempt_id'],
            'request_extra':claim['request_extra'],'temporary_message_id':'-7','final_message_id':None,'state':'retryable'})
        sender._originals.add((claim['id'],claim['attempt_id']))
        sender._retry_evidence[claim['id']]={'attempt_id':claim['attempt_id'],'request_extra':claim['request_extra'],'failed_id':'-8','deadline':0,'verified':False}
        AccountStore(db).invalidate_epoch('123')
        session.stop()
        assert outbox.get(claim['id'])['state']=='cancelled'
        assert claim['request_extra'] not in client._attempt_events.attempts
        assert not sender._originals
    finally:
        db.close()


def test_reclaim_when_router_already_persisted_unknown(tmp_path):
    db, session, binding, client, sender=make_sender(tmp_path)
    try:
        stage_pending(db,0); outbox=Outbox(db); claim=outbox.claim_next('worker')
        client.register_attempt(delivery_id=claim['id'],attempt_id=claim['attempt_id'],request_extra=claim['request_extra'],
            account_id='123',epoch=1,chat_id='456',sender={'kind':'user','id':'123'},destination_version=1,content_hash=claim['content_hash'])
        sender._originals.add((claim['id'],claim['attempt_id']))
        assert outbox.transition(claim['id'],'sending','unknown',{'delivery_id':claim['id'],'attempt_id':claim['attempt_id'],
            'request_extra':claim['request_extra'],'temporary_message_id':None,'final_message_id':None,'state':'unknown'})
        sender._unknown(claim)
        assert claim['request_extra'] not in client._attempt_events.attempts
        assert not sender._originals
    finally:
        db.close()


def test_local_reconciliation_404_remains_unknown_and_blocks_reconnect(tmp_path):
    db, session, binding, client, sender=make_sender(tmp_path)
    try:
        value=stage_pending(db,0); outbox=Outbox(db); claim=outbox.claim_next('worker')
        with db.transaction() as con:
            con.execute('INSERT INTO routing_plans(id,post_id,payload) VALUES (?,?,?)', ('plan', value['post_id'], encode({'destination_version':1})))
            con.execute('INSERT INTO routing_delivery_context(delivery_id,plan_id,initiated_attempt_id) VALUES (?,?,?)', (value['id'],'plan',claim['attempt_id']))
        client.register_attempt(delivery_id=claim['id'],attempt_id=claim['attempt_id'],request_extra=claim['request_extra'],
            account_id='123',epoch=1,chat_id='456',sender={'kind':'user','id':'123'},destination_version=1,content_hash=claim['content_hash'])
        binding.events.append({'@client_id':42,'@type':'message','@extra':claim['request_extra'],'id':-7,'chat_id':456,
            'sender_id':{'@type':'messageSenderUser','user_id':123}}); sender.poll()
        client.reconcile_pending(request_extras=[claim['request_extra']]); req=binding.requests[-1]
        binding.events.append({'@client_id':42,'@type':'error','@extra':req['@extra'],'code':404}); sender.poll()
        assert outbox.get(claim['id'])['state']=='unknown'
        session.stop(); session.start({'feed_url':'https://www.threads.net/','mode':'review'})
        calls=[]; client.enable_network=lambda permit, **kwargs:calls.append(permit)
        with pytest.raises(ValueError): sender.enable_running_network()
        assert calls == [] and outbox.get(claim['id'])['state']=='unknown'
        assert not any(r['@type'] in {'sendMessage','resendMessages'} for r in binding.requests)
    finally:
        db.close()


def test_full_capacity_does_not_consume_live_retry_authority(tmp_path):
    db, session, binding, client, sender=make_sender(tmp_path)
    try:
        stage_pending(db,0); outbox=Outbox(db); claim=outbox.claim_next('worker')
        client.register_attempt(delivery_id=claim['id'],attempt_id=claim['attempt_id'],request_extra=claim['request_extra'],
            account_id='123',epoch=1,chat_id='456',sender={'kind':'user','id':'123'},destination_version=1,content_hash=claim['content_hash'])
        source={'@type':'messageSenderUser','user_id':123}
        binding.events.append({'@client_id':42,'@type':'message','@extra':claim['request_extra'],'id':-7,'chat_id':456,'sender_id':source}); client.authorization_event(); client.take_delivery_event()
        failure={'@type':'messageSendingStateFailed','can_retry':True,'retry_after':0,'need_another_sender':False,
            'need_another_reply_quote':False,'need_drop_reply':False,'required_paid_message_star_count':0}
        binding.events.append({'@client_id':42,'@type':'updateMessageSendFailed','old_message_id':-7,
            'message':{'id':-8,'chat_id':456,'sender_id':source,'sending_state':failure}}); client.authorization_event(); client.take_delivery_event()
        assert outbox.transition(claim['id'],'sending','retryable',{'delivery_id':claim['id'],'attempt_id':claim['attempt_id'],
            'request_extra':claim['request_extra'],'temporary_message_id':'-7','final_message_id':None,'state':'retryable'})
        sender._originals.add((claim['id'],claim['attempt_id']))
        sender._retry_evidence[claim['id']]={'attempt_id':claim['attempt_id'],'request_extra':claim['request_extra'],'failed_id':'-8','deadline':0,'verified':False}
        sender._retry_evidence[claim['id']]['verified']=True
        for i in range(127): register(client,i+1)
        assert sender.retry(claim['id'],'worker') is None
        assert outbox.get(claim['id'])['state']=='retryable'
        assert sender._retry_evidence[claim['id']]['attempt_id']==claim['attempt_id']
        assert claim['request_extra'] in client._attempt_events.attempts
    finally:
        db.close()


def test_deleted_delivery_with_unpersisted_outcome_keeps_reconnect_blocked(tmp_path):
    db, session, binding, client, sender=make_sender(tmp_path)
    try:
        stage_pending(db,0); outbox=Outbox(db); claim=outbox.claim_next('worker')
        client.register_attempt(delivery_id=claim['id'],attempt_id=claim['attempt_id'],request_extra=claim['request_extra'],
            account_id='123',epoch=1,chat_id='456',sender={'kind':'user','id':'123'},destination_version=1,content_hash=claim['content_hash'])
        sender._originals.add((claim['id'],claim['attempt_id']))
        source={'@type':'messageSenderUser','user_id':123}
        binding.events.append({'@client_id':42,'@type':'message','@extra':claim['request_extra'],'id':-7,'chat_id':456,'sender_id':source}); sender.poll()
        with db.transaction() as con: con.execute('DELETE FROM deliveries WHERE id=?',(claim['id'],))
        binding.events.append({'@client_id':42,'@type':'updateMessageSendSucceeded','old_message_id':-7,
            'message':{'id':11,'chat_id':456,'sender_id':source}}); sender.poll()
        assert outbox.get(claim['id']) is None
        assert claim['request_extra'] in client._attempt_events.attempts
        calls=[]; client.enable_network=lambda permit, **kwargs:calls.append(permit)
        with pytest.raises(ValueError): sender.enable_running_network()
        assert calls == []
    finally:
        db.close()


def test_revocation_between_event_read_and_transition_does_not_leak_capacity(tmp_path):
    db, session, binding, client, sender=make_sender(tmp_path)
    try:
        value=stage_pending(db,0); outbox=Outbox(db); claim=outbox.claim_next('worker')
        with db.transaction() as con:
            con.execute('INSERT INTO routing_plans(id,post_id,payload) VALUES (?,?,?)', ('plan', value['post_id'], encode({'destination_version':1})))
            con.execute('INSERT INTO routing_delivery_context(delivery_id,plan_id,initiated_attempt_id) VALUES (?,?,?)', (value['id'],'plan',claim['attempt_id']))
        client.register_attempt(delivery_id=claim['id'],attempt_id=claim['attempt_id'],request_extra=claim['request_extra'],
            account_id='123',epoch=1,chat_id='456',sender={'kind':'user','id':'123'},destination_version=1,content_hash=claim['content_hash'])
        sender._originals.add((claim['id'],claim['attempt_id']))
        source={'@type':'messageSenderUser','user_id':123}
        binding.events.append({'@client_id':42,'@type':'message','@extra':claim['request_extra'],'id':-7,'chat_id':456,'sender_id':source}); sender.poll()
        original=sender.outbox.transition
        def revoke_before_transition(*args,**kwargs):
            AccountStore(db).invalidate_epoch('123')
            return original(*args,**kwargs)
        sender.outbox.transition=revoke_before_transition
        binding.events.append({'@client_id':42,'@type':'updateMessageSendSucceeded','old_message_id':-7,
            'message':{'id':11,'chat_id':456,'sender_id':source}}); sender.poll()
        assert outbox.get(claim['id'])['state']=='unknown'
        assert claim['request_extra'] not in client._attempt_events.attempts
        assert not sender._originals
    finally:
        db.close()


def test_running_network_label_without_live_database_guard_is_not_authority(tmp_path):
    from threadrelay.delivery.telegram import NetworkPermit, TelegramBoundaryError
    db, session, binding, client, sender=make_sender(tmp_path)
    try:
        client._offline_pending=False; client._config_started=True
        with pytest.raises(TelegramBoundaryError):
            client.enable_network(NetworkPermit('running_reconciliation',1,False))
        assert not any(r.get('type',{}).get('@type')=='networkTypeOther' for r in binding.requests)
    finally:
        db.close()


def test_running_network_dispatch_holds_writer_even_for_deferred_connection(tmp_path):
    import sqlite3
    from threadrelay.delivery.telegram import NetworkPermit
    db, session, binding, client, sender=make_sender(tmp_path)
    try:
        client._offline_pending=False; client._config_started=True
        observed=[]
        original=binding.send
        def inspect(client_id,request):
            if request.get('@extra','').startswith('network:enable:'):
                other=sqlite3.connect(db.path,timeout=0)
                try:
                    other.execute('BEGIN IMMEDIATE')
                    observed.append(False)
                except sqlite3.OperationalError:
                    observed.append(True)
                finally: other.close()
            original(client_id,request)
        binding.send=inspect
        with db.connection() as con:
            con.execute('BEGIN')  # Deferred alone has no writer lock.
            client.enable_network(NetworkPermit('running_reconciliation',1,False),connection=con,running_account='123')
            con.rollback()
        assert observed == [True]
    finally:
        db.close()
