"""Private saved-session restore uses only the existing profile and exact identity."""
from datetime import datetime,timezone

import pytest

from test_owner_history import manual as manual_fixture,job,dispatch,result,drain,auth_state
from threadrelay.features import AccountFeatures
from threadrelay.main import dispatch as dispatch_command
from threadrelay.delivery.auth import AuthFailure

manual=manual_fixture


def adapter(env,*,reference='vault:fixture'):
    return AccountFeatures(env.auth,vault_reference=lambda:reference,native_gate=lambda:True,
        discard_secret_ref=lambda _:None,wall_clock=lambda:datetime.now(timezone.utc))


def disconnect(env):
    env.auth.cancel();env.library.events.append(auth_state(env,'Closed'));drain(env)


def test_missing_saved_profile_restore_is_inert_and_does_not_begin_new_login(manual):
    env=manual;disconnect(env);before=env.library.client_id
    adapter(env).restore()
    assert env.library.client_id==before and env.auth.status=='closed'


def test_completed_saved_profile_restores_same_account_without_sending_or_new_challenges(manual):
    env=manual;item=job(env);dispatch(env,item);result(env)
    profile=env.client._profile_guard
    (profile.path/'td.binlog').write_bytes(b'synthetic encrypted auth database')
    epoch=env.auth.snapshot().epoch
    disconnect(env);before=len([request for request in env.library.calls if request['@type']=='sendMessage'])
    adapter(env).restore()
    env.library.events.extend([auth_state(env,'WaitTdlibParameters'),auth_state(env,'Ready')]);drain(env)
    # The ordered owner grants only saved-account connection authority.
    if not env.auth._network_requested:env.auth.enable_saved_connection();drain(env)
    assert env.auth.status=='ready' and env.auth.snapshot().epoch==epoch
    assert env.auth.snapshot().account_id=='123' and env.client.transport_ready
    assert env.auth.challenge() is None and env.session.snapshot['state']=='stopped'
    assert len([request for request in env.library.calls if request['@type']=='sendMessage'])==before
    assert not any(request['@type']=='resendMessages' for request in env.library.calls)


def test_restore_command_without_saved_account_is_acknowledged_and_inert(tmp_path):
    from threadrelay.application import Application
    with Application(tmp_path) as app:
        app.features.account_features.native_gate=lambda:True
        event=dispatch_command({'version':1,'id':'restore','type':'RestoreTelegramSession','payload':{}},features=app.features)
        assert event['type']=='Acknowledged' and event['payload']=={'operation':'RestoreTelegramSession'}
        assert app.telegram._client is None and app.session.snapshot['state']=='stopped'


def test_saved_restore_reference_cannot_select_a_different_profile(manual):
    env=manual;(env.client._profile_guard.path/'td.binlog').write_bytes(b'synthetic encrypted auth database')
    disconnect(env);before=env.library.client_id
    with pytest.raises(AuthFailure):adapter(env,reference='vault:other').restore()
    assert env.library.client_id==before


@pytest.mark.parametrize('lane',['automatic','manual-history'])
def test_incomplete_durable_receipt_blocks_restore_before_native_creation(manual,lane):
    env=manual;(env.client._profile_guard.path/'td.binlog').write_bytes(b'synthetic encrypted auth database')
    profile_id=env.client._profile_guard.profile_id;disconnect(env)
    with env.db.transaction() as con:
        con.execute('INSERT INTO local_policy(name,payload) VALUES (?,?)',(lane+'-incomplete:'+profile_id,'{}'))
    before=env.library.client_id
    with pytest.raises(AuthFailure):adapter(env).restore()
    assert env.library.client_id==before and not env.client.transport_ready


def test_pending_profile_marker_cannot_restore_even_without_an_outbox_attempt(manual):
    from test_owner_test import attempt
    env=manual;profile=env.client._profile_guard
    (profile.path/'td.binlog').write_bytes(b'synthetic encrypted auth database')
    profile.begin_owner_test({**attempt(),'account_id':'123','sender_id':'123','profile_id':profile.profile_id})
    disconnect(env);profile.close();before=env.library.client_id
    with pytest.raises(AuthFailure):adapter(env).restore()
    assert env.library.client_id==before and env.auth.status=='authentication_unavailable'
    assert not env.client.transport_ready


def test_native_uncertainty_added_after_reconnect_begin_blocks_network_activation(tmp_path):
    import json
    from test_final_integration import assembled,ready,stage_in_app
    from test_telegram_auth import state
    app,binding,_=assembled(tmp_path)
    try:
        ready(app,binding);identifier=stage_in_app(app)
        app.features.account_features.cancel()
        for _ in range(3):app.delivery_owner.step()
        app.features.account_features.begin()
        with app.db.transaction() as con:
            payload=json.loads(con.execute('SELECT payload FROM deliveries WHERE id=?',(identifier,)).fetchone()[0])
            payload['request_extra']='possibly-entered-native'
            con.execute('UPDATE deliveries SET payload=? WHERE id=?',(json.dumps(payload),identifier))
        before=sum(request.get('type',{}).get('@type')=='networkTypeOther' for request in binding.requests)
        binding.events.append(state('WaitTdlibParameters'))
        with pytest.raises(AuthFailure):
            for _ in range(30):app.delivery_owner.step()
        assert app.auth.status=='authentication_error' and not app.telegram.transport_ready
        assert sum(request.get('type',{}).get('@type')=='networkTypeOther' for request in binding.requests)==before
        assert not any(request['@type'] in {'sendMessage','resendMessages'} for request in binding.requests)
    finally:app.close()


def test_begin_dispatch_reports_allowlisted_unknown_outcome_without_reading_credentials(manual):
    from threadrelay.features import LocalFeatures
    env=manual;env.library.fail_send=True
    dispatch(env,job(env));disconnect(env)
    credentials=[]
    account=adapter(env);account.vault_reference=lambda:credentials.append(True)
    before=env.library.client_id
    event=dispatch_command({'version':1,'id':'blocked-login','type':'BeginTelegramLogin','payload':{}},
        features=LocalFeatures(env.db,account=account))
    assert event['type']=='Error' and event['payload']=={'code':'invalid_request','message':'telegram_auth:outcome_unknown'}
    assert credentials==[] and env.library.client_id==before


@pytest.mark.parametrize('raw_error',['telegram_auth:outcome_unknown','synthetic private native details'])
def test_other_login_errors_remain_generic_even_if_exception_text_looks_allowlisted(manual,raw_error):
    from threadrelay.features import LocalFeatures
    env=manual;account=adapter(env)
    def fail():raise ValueError(raw_error)
    account.begin=fail
    event=dispatch_command({'version':1,'id':'ordinary-error','type':'BeginTelegramLogin','payload':{}},
        features=LocalFeatures(env.db,account=account))
    assert event['type']=='Error'
    assert event['payload']=={'code':'invalid_request','message':'The operation could not be completed. Review current details and try again.'}


@pytest.mark.parametrize('uncertain',[False,True])
def test_login_preflight_is_side_effect_free_and_checks_uncertainty_before_native_preparation(manual,uncertain):
    from threadrelay.features import LocalFeatures
    env=manual
    if uncertain:
        env.library.fail_send=True;dispatch(env,job(env))
    account=adapter(env)
    def forbidden():raise AssertionError('Preflight touched native credentials or auth')
    account.begin=account.vault_reference=account.native_gate=forbidden
    before=env.auth.snapshot();state=env.auth.status;calls=len(env.library.calls)
    event=dispatch_command({'version':1,'id':'preflight','type':'CheckTelegramLogin','payload':{}},
        features=LocalFeatures(env.db,account=account))
    if uncertain:assert event['type']=='Error' and event['payload']=={'code':'invalid_request','message':'telegram_auth:outcome_unknown'}
    else:assert event['type']=='Acknowledged' and event['payload']=={'operation':'CheckTelegramLogin'}
    assert env.auth.snapshot()==before and env.auth.status==state and len(env.library.calls)==calls
