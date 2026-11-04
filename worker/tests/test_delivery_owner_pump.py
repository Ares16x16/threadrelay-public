"""Bounded single-owner receive progress; synthetic native updates only."""
from types import SimpleNamespace

import pytest

from test_final_integration import assembled, ready
from test_telegram_auth import state
from threadrelay.delivery.owner import DeliveryOwner


def irrelevant(count):
    return [{'@client_id':42,'@type':'updateUser','user':{'id':index,'unrelated':'discard'}} for index in range(count)]


def test_startup_update_backlog_does_not_expire_a_queued_chat_listing_response(tmp_path):
    app,binding,_=assembled(tmp_path)
    stamp=[0.]
    try:
        ready(app,binding)
        app.delivery_owner._receive_clock=lambda:0.
        app.destination_listing.clock=lambda:stamp[0]
        original=binding.send
        def send(client,request):
            typ=request['@type'];extra=request.get('@extra')
            if typ in {'loadChats','getChats','getChat'}:
                binding.requests.append(request)
                if typ=='loadChats':
                    binding.events.extend(irrelevant(1000))
                    binding.events.append({'@client_id':42,'@type':'ok','@extra':extra})
                elif typ=='getChats':
                    binding.events.append({'@client_id':42,'@type':'chats','@extra':extra,'chat_ids':[-20],'total_count':1})
                else:
                    binding.events.append({'@client_id':42,'@type':'chat','@extra':extra,'id':-20,'title':'Test',
                        'type':{'@type':'chatTypeBasicGroup','basic_group_id':20},'message_sender_id':None})
            else:original(client,request)
        binding.send=send
        app.destination_listing.refresh()
        # Model the existing idle owner's two .1-second waits, without sleeping.
        # One receive per step exhausts the real30s deadline before its queued Ok.
        for _ in range(155):
            app.delivery_owner.step();stamp[0]+=.2
            if app.destination_listing.state=='ready':break
        result=app.destination_listing.query()
        assert result['state']=='ready'
        assert [item['title'] for item in result['items']]==['Test']
        assert stamp[0]<30
        assert 'unrelated' not in str(result)
        assert not any(request['@type']=='sendMessage' for request in binding.requests)
    finally:app.close()


def test_qr_scan_password_continuation_is_not_starved_by_unrelated_updates(tmp_path):
    app,binding,_=assembled(tmp_path)
    try:
        app.delivery_owner._receive_clock=lambda:0.
        app.features.account_features.begin()
        binding.events.extend([state('WaitTdlibParameters'),state('WaitPhoneNumber')])
        for _ in range(30):app.delivery_owner.step()
        phone=app.auth.challenge();assert phone.kind=='phone'
        app.auth.request_qr(phone.challenge_id)
        binding.events.extend([state('WaitOtherDeviceConfirmation',link='tg://synthetic-private-token'),
                               *irrelevant(1000),state('WaitPassword')])
        for _ in range(6):app.delivery_owner.step()
        assert app.auth.challenge().kind=='password'
        assert app.auth.status=='authorizing' and app.telegram.transport_ready
        assert len([request for request in binding.requests if request['@type']=='requestQrCodeAuthentication'])==1
        assert not any(request['@type']=='sendMessage' for request in binding.requests)
    finally:app.close()


@pytest.mark.parametrize('budget', ['events','time'])
def test_receive_pump_yields_with_an_endless_native_queue(budget):
    owner=object.__new__(DeliveryOwner)
    stamp=[0.];calls=[]
    def poll():
        calls.append(True)
        if budget=='time':stamp[0]+=.011
    owner.sender=SimpleNamespace(poll=poll)
    owner.telegram=SimpleNamespace(_last_receive_had_event=True)
    owner._receive_clock=lambda:stamp[0]
    assert owner._pump_receive() is False
    assert len(calls)==(256 if budget=='events' else 2)


def test_receive_pump_stops_on_the_first_empty_poll():
    owner=object.__new__(DeliveryOwner);calls=[]
    owner.sender=SimpleNamespace(poll=lambda:calls.append(True))
    owner.telegram=SimpleNamespace(_last_receive_had_event=False)
    owner._receive_clock=lambda:0.
    assert owner._pump_receive() is True
    assert calls==[True]


def test_batched_poll_preserves_revocation_before_late_ready_and_never_reconnects(tmp_path):
    app,binding,_=assembled(tmp_path)
    try:
        ready(app,binding)
        app.delivery_owner._receive_clock=lambda:0.
        epoch=app.auth.snapshot().epoch
        before=len(binding.requests)
        binding.events.extend([*irrelevant(20),state('LoggingOut'),state('Ready'),
            {'@client_id':42,'@type':'user','@extra':'auth:get-me','id':123,'type':{'@type':'userTypeRegular'}}])
        app.delivery_owner.step()
        assert app.auth.snapshot().revocation_pending and app.auth.snapshot().epoch==epoch+1
        assert app.auth.snapshot().auth_state=='closed' and not app.telegram.transport_ready
        assert not any(request['@type'] in {'getMe','sendMessage','resendMessages'}
            or request.get('type',{}).get('@type')=='networkTypeOther' for request in binding.requests[before:])
    finally:app.close()
