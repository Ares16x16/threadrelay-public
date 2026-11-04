"""Synthetic private host isolates files only after actual owned-client Closed."""
import hashlib
import json

import pytest

from test_owner_history import manual as manual_fixture,auth_state,drain
from test_owner_automatic import auto as auto_fixture,configure_route_queue,enqueue,progress,succeed,send_requests
from threadrelay.delivery.recovery import TelegramRecovery
from threadrelay.storage.outbox import Outbox,_has_native_uncertainty
from threadrelay.storage.isolation import is_isolated_delivery

manual=manual_fixture
auto=auto_fixture


class NativeHost:
    def __init__(self,env):
        self.env=env;self.source=env.client._profile_guard.path;self.calls=[];self.receipts={};self.fail_verify=False

    def quarantine(self,profile_id,digest):
        assert self.env.client._client._closed
        assert self.env.client._profile_guard._descriptor is None
        marker=(self.source/'connection-purpose.json').read_bytes()
        assert hashlib.sha256(marker).hexdigest()==digest
        assert json.loads(marker)['body']['profile_id']==profile_id
        self.calls.append(profile_id)
        recovery_id='a'*32
        archive=self.source.parent/('archived-'+recovery_id)
        self.source.rename(archive);self.source.mkdir(mode=0o700)
        info=self.source.stat()
        receipt={'recovery_id':recovery_id,'old_profile_id':profile_id,'old_marker_sha256':digest,
            'archive_id':archive.name,'new_directory_device':info.st_dev,'new_directory_inode':info.st_ino}
        self.receipts[recovery_id]=(receipt,archive)
        return dict(receipt)

    def verify(self,recovery_id):
        if self.fail_verify:raise RuntimeError('synthetic invalid native proof')
        receipt,archive=self.receipts[recovery_id]
        assert hashlib.sha256((archive/'connection-purpose.json').read_bytes()).hexdigest()==receipt['old_marker_sha256']
        info=self.source.stat()
        assert (info.st_dev,info.st_ino)==(receipt['new_directory_device'],receipt['new_directory_inode'])
        marker=self.source/'connection-purpose.json'
        if marker.exists():assert json.loads(marker.read_text())['body']['profile_id']!=receipt['old_profile_id']
        return dict(receipt)


def setup_recovery(env):
    native=NativeHost(env)
    recovery=TelegramRecovery(native.source.parent,env.auth,env.client,env.session,
        quarantine=native.quarantine,verify=native.verify,clock=env.clock.monotonic,
        reset_owners=(env.controller,env.auto_controller))
    env.auth.verify_isolations=recovery.verify_for_login
    env.client.profile_allowed=recovery.profile_allowed
    env.client._client.profile_allowed=recovery.profile_allowed
    return recovery,native


def unknown(env):
    configure_route_queue(env,queue={'mode':'immediate'})
    item=enqueue(env,'UnknownRecovery');env.library.fail_send=True;progress(env)
    assert Outbox(env.db).get(item)['state']=='unknown'
    return item


def close_native(env):
    env.library.events.append(auth_state(env,'Closed'));drain(env)


def login_again(env):
    env.library.fail_send=False
    env.auth.begin('vault:fixture',pending=True)
    env.library.events.extend([auth_state(env,'WaitTdlibParameters'),auth_state(env,'Ready')]);drain(env)
    if not env.auth._network_requested:env.auth.enable_saved_connection();drain(env)
    assert env.auth.status=='ready' and env.auth.snapshot().account_id=='123'


def test_recovery_closes_archives_and_reauthenticates_without_retrying_unknown(auto):
    env=auto;old=unknown(env);queued=enqueue(env,'PristineAfterUnknown')
    original=Outbox(env.db).get(old);epoch=env.auth.snapshot().epoch
    recovery,native=setup_recovery(env)
    assert recovery.begin(old)=={'state':'closing'}
    recovery.tick();assert native.calls==[]
    close_native(env);recovery.tick()
    assert recovery.query()=={'state':'ready_to_login'} and len(native.calls)==1
    with env.db.connection() as con:
        assert is_isolated_delivery(con,old) and not _has_native_uncertainty(con)
    assert Outbox(env.db).get(old)==original
    assert env.archive.get_delivery(old)['delivery']['state']=='unknown'
    projected={item['id']:item for item in env.auto_queue.schedule.query()['items']}
    assert projected[old]['state']=='unknown' and projected[old]['reason']=='archived_not_retried'
    login_again(env)
    assert env.auth.snapshot().epoch==epoch and env.library.client_id==2
    env.auto_controller.tick();assert len(send_requests(env))==1
    env.session.start({'feed_url':'https://www.threads.com/','mode':'auto','route_ids':['score'],'unlimited_auto_deliveries':True})
    progress(env);succeed(env,222)
    assert Outbox(env.db).get(queued)['state']=='sent' and Outbox(env.db).get(old)==original
    assert len(send_requests(env))==2
    texts=[request['input_message_content']['text']['text'] for request in send_requests(env)]
    assert 'UnknownRecovery' in texts[0] and 'UnknownRecovery' not in texts[1]


def test_recovery_without_positive_closed_never_calls_native_quarantine(auto):
    env=auto;old=unknown(env);recovery,native=setup_recovery(env)
    recovery.begin(old);env.clock.advance(31);recovery.tick()
    assert recovery.query()=={'state':'failed','reason':'native_client_not_closed'} and native.calls==[]
    with env.db.connection() as con:assert _has_native_uncertainty(con)


def test_invalid_native_verification_never_exempts_unknown(auto):
    env=auto;old=unknown(env);recovery,native=setup_recovery(env);native.fail_verify=True
    recovery.begin(old);close_native(env);recovery.tick()
    assert recovery.query()['state']=='failed'
    with env.db.connection() as con:assert _has_native_uncertainty(con) and not is_isolated_delivery(con,old)
    assert Outbox(env.db).get(old)['state']=='unknown'


def test_tampered_isolation_snapshot_cannot_exempt_unrelated_attempt(auto):
    env=auto;old=unknown(env);recovery,_=setup_recovery(env)
    recovery.begin(old);close_native(env);recovery.tick()
    with env.db.transaction() as con:
        row=con.execute("SELECT name,payload FROM local_policy WHERE name GLOB 'telegram-isolation:*'").fetchone()
        value=json.loads(row['payload']);saved=json.loads(value['delivery_payload']);saved['request_extra']='forged-unrelated-attempt'
        value['attempt']['request_extra']='forged-unrelated-attempt'
        value['delivery_payload']=json.dumps(saved)
        con.execute('UPDATE local_policy SET payload=? WHERE name=?',(json.dumps(value),row['name']))
    with env.db.connection() as con:
        with pytest.raises(ValueError):_has_native_uncertainty(con)


def test_interrupted_closing_reopen_never_reuses_native_recovery_authority(auto):
    env=auto;old=unknown(env);recovery,native=setup_recovery(env)
    recovery.begin(old)
    reopened=TelegramRecovery(native.source.parent,env.auth,env.client,env.session,
        quarantine=native.quarantine,verify=native.verify,clock=env.clock.monotonic)
    assert reopened.query()=={'state':'blocked','reason':'recovery_interrupted'}
    close_native(env);reopened.tick()
    assert native.calls==[]
    assert Outbox(env.db).get(old)['state']=='unknown'


@pytest.mark.parametrize('change',['epoch','vault_reference'])
def test_historical_isolation_never_authorizes_changed_account_binding(auto,change):
    env=auto;old=unknown(env);recovery,_=setup_recovery(env)
    recovery.begin(old);close_native(env);recovery.tick()
    if change=='epoch':env.accounts.invalidate_epoch('123')
    else:
        with env.db.transaction() as con:con.execute("UPDATE accounts SET vault_ref='vault:other' WHERE account_id='123'")
    with env.db.connection() as con:assert _has_native_uncertainty(con) and not is_isolated_delivery(con,old)
    with pytest.raises(ValueError,match='account_changed'):recovery.verify_for_login()


def test_every_new_auth_activation_revalidates_native_quarantine_before_native_create(auto):
    from threadrelay.delivery.auth import AuthFailure
    env=auto;old=unknown(env);recovery,native=setup_recovery(env)
    recovery.begin(old);close_native(env);recovery.tick();before=env.library.client_id
    native.fail_verify=True
    with pytest.raises(AuthFailure):env.auth.begin('vault:fixture',pending=True)
    assert env.library.client_id==before and env.auth.status=='closed'


def test_removed_account_does_not_turn_an_archived_unknown_into_a_new_login_blocker(auto):
    from threadrelay.delivery.auth import AuthController
    env=auto;old=unknown(env);recovery,_=setup_recovery(env)
    recovery.begin(old);close_native(env);recovery.tick()
    original=Outbox(env.db).get(old)
    epoch=env.accounts.invalidate_epoch('123')
    env.accounts.forget_local_reference('123',epoch)
    with env.db.connection() as con:
        assert is_isolated_delivery(con,old) and not _has_native_uncertainty(con)
    assert not env.accounts.has_unresolved()
    recovery.verify_for_login()
    reopened=AuthController(env.client,env.accounts,env.auth._handoff,clock=env.clock.monotonic)
    assert reopened.snapshot().account_id is None and not reopened.snapshot().revocation_pending
    reopened.verify_isolations=recovery.verify_for_login
    reopened.begin('vault:fixture',pending=env.accounts.has_unresolved())
    assert reopened.status=='authorizing'
    from threadrelay.delivery.telegram import NetworkPermit
    env.library.fail_send=False
    env.library.events.append(auth_state(env,'WaitTdlibParameters'))
    for _ in range(12):reopened.poll()
    reopened.enable_clean_auth(NetworkPermit('clean_auth',reopened.snapshot().epoch,False))
    env.library.events.append(auth_state(env,'Ready'))
    for _ in range(20):reopened.poll()
    assert reopened.status=='ready' and reopened.snapshot().epoch==epoch
    assert not reopened.snapshot().revocation_pending
    with env.db.connection() as con:assert not _has_native_uncertainty(con)
    assert Outbox(env.db).get(old)==original
    assert env.accounts.pending_revocations(), 'do not invent server-side logout confirmation'


def test_recovery_commands_are_closed_and_query_does_not_call_native(auto):
    from threadrelay.features import LocalFeatures
    from threadrelay.main import dispatch as command
    env=auto;old=unknown(env);recovery,native=setup_recovery(env)
    service=LocalFeatures(env.db);service.telegram_recovery=recovery
    query={'version':1,'id':'query-recovery','type':'QueryTelegramRecovery','payload':{}}
    assert command(query,features=service)['payload']=={'state':'blocked','reason':'not_started'}
    started=command({'version':1,'id':'begin-recovery','type':'BeginTelegramRecovery','payload':{'delivery_id':old}},features=service)
    assert started['type']=='TelegramRecoveryState' and started['payload']=={'state':'closing'}
    assert command(query,features=service)['payload']=={'state':'closing'} and native.calls==[]
