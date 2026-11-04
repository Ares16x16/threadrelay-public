"""Synthetic code transport metadata and recovery; never a real login."""
import pytest

from test_telegram_auth import build,state
from threadrelay.delivery.auth import AuthFailure
from threadrelay.features import AccountFeatures
from datetime import datetime,timezone


def code_event(**changes):
    info={'phone_number':'+12025550123','type':{'@type':'authenticationCodeTypeTelegramMessage','length':5},
        'next_type':{'@type':'authenticationCodeTypeSms','length':5},'timeout':12}
    return state('WaitCode',code_info={**info,**changes})


def test_code_delivery_is_private_and_resend_obeys_server_wait_and_one_shot_challenge(tmp_path):
    db,binding,client,handoff,auth=build(tmp_path);now=[10.0];auth._clock=lambda:now[0]
    auth.begin('vault:fixture',pending=False);binding.events.append(code_event());auth.poll()
    challenge=auth.challenge()
    assert getattr(challenge,'code_delivery',None)=={'method':'telegram','next_method':'sms','resend_after_seconds':12}
    feature=AccountFeatures(auth,vault_reference=lambda:'vault:fixture',native_gate=lambda:True,
        discard_secret_ref=lambda _:None,wall_clock=lambda:datetime.now(timezone.utc))
    assert '+12025550123' not in str(feature.query())
    before=len(binding.requests)
    with pytest.raises(AuthFailure):auth.resend_code(challenge.challenge_id)
    assert len(binding.requests)==before
    now[0]+=12;auth.resend_code(challenge.challenge_id)
    assert binding.requests[-1]=={'@type':'resendAuthenticationCode','reason':None,'@extra':'auth:resend:'+challenge.challenge_id}
    assert auth.challenge() is None
    with pytest.raises(AuthFailure):auth.resend_code(challenge.challenge_id)
    assert len(binding.requests)==before+1
    binding.events.append(code_event(type={'@type':'authenticationCodeTypeSms'},next_type=None,timeout=0));auth.poll()
    assert auth.challenge().challenge_id!=challenge.challenge_id
    assert auth.challenge().code_delivery=={'method':'sms','next_method':None,'resend_after_seconds':None}
    with pytest.raises(AuthFailure):auth.resend_code(auth.challenge().challenge_id)
    db.close()


@pytest.mark.parametrize('step',['WaitCode','WaitPassword'])
def test_qr_can_replace_a_waiting_code_or_password_without_restarting_login(tmp_path,step):
    db,binding,client,handoff,auth=build(tmp_path)
    auth.begin('vault:fixture',pending=False);binding.events.append(state(step));auth.poll()
    old=auth.challenge();auth.request_qr(old.challenge_id)
    assert binding.requests[-1]['@type']=='requestQrCodeAuthentication'
    assert auth.challenge() is None
    extra=binding.requests[-1]['@extra']
    binding.events.append({'@client_id':42,'@type':'error','@extra':extra,'message':'synthetic'})
    auth.poll();assert auth.challenge().kind==old.kind
    db.close()


def test_expired_login_step_has_actionable_status_instead_of_waiting_forever(tmp_path):
    db,binding,client,handoff,auth=build(tmp_path);now=[10.0];auth._clock=lambda:now[0]
    auth.begin('vault:fixture',pending=False);binding.events.append(state('WaitPassword'));auth.poll()
    now[0]+=301;assert auth.challenge() is None
    assert auth.status=='challenge_expired'
    db.close()


@pytest.mark.parametrize('message,wait',[('FLOOD_WAIT_30',30),('private arbitrary error',None)])
def test_resend_failure_never_replays_and_preserves_safe_recovery(tmp_path,message,wait):
    db,binding,client,handoff,auth=build(tmp_path);now=[10.0];auth._clock=lambda:now[0]
    auth.begin('vault:fixture',pending=False);binding.events.append(code_event(timeout=0));auth.poll()
    old=auth.challenge();auth.resend_code(old.challenge_id)
    extra=binding.requests[-1]['@extra']
    binding.events.append({'@client_id':42,'@type':'error','@extra':extra,'message':message});auth.poll()
    current=auth.challenge();assert current.kind=='code' and current.challenge_id!=old.challenge_id
    assert current.code_delivery['resend_after_seconds']==wait
    with pytest.raises(AuthFailure):auth.resend_code(current.challenge_id)
    assert sum(r['@type']=='resendAuthenticationCode' for r in binding.requests)==1
    auth.request_qr(current.challenge_id)
    assert binding.requests[-1]['@type']=='requestQrCodeAuthentication'
    db.close()
