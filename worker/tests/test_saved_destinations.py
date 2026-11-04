"""Explicit saved recipient selection; synthetic metadata, no network or sends."""
from dataclasses import replace

import pytest

import test_destination_listing
from threadrelay.delivery.destinations import DestinationVerifier
from threadrelay.features import DestinationFeatures
from threadrelay.routing.policy import _destination
from threadrelay.storage.saved_destinations import SavedDestinations

listing_env=test_destination_listing.listing_env


def verified(e, *, omitted_sender=False):
    verifier=DestinationVerifier(e.client,e.auth,e.config,e.session)
    adapter=DestinationFeatures(verifier,native_gate=lambda:False,metadata_gate=lambda:True,listing=e.listing)
    adapter.begin_verify({'chat_id':'-20','kind':'group','sender':{'kind':'user','id':'7'}})
    for body in (
        {'@type':'chat','id':-20,'title':'Test','type':{'@type':'chatTypeBasicGroup','basic_group_id':20},'message_sender_id':None,'permissions':{'can_send_basic_messages':True}},
        {'@type':'basicGroup','id':20,'is_active':True},
        {'@type':'chatMember','member_id':{'@type':'messageSenderUser','user_id':7},'status':{'@type':'chatMemberStatusMember'}},
    ):
        if omitted_sender:
            body.pop('message_sender_id',None)
        assert verifier.consume({**body,'@extra':verifier.pending['extra']})
        assert verifier.pending is not None, 'The exact basic group must remain verifiable'
    assert adapter.query()['state']=='verified'
    return adapter


@pytest.mark.parametrize('omitted_sender',[False,True])
def test_verification_does_not_save_and_unsaved_recipient_cannot_route(listing_env,omitted_sender):
    e=listing_env;SavedDestinations(e.db).require_saved_selection()
    adapter=verified(e,omitted_sender=omitted_sender)
    assert adapter.query_saved()=={'items':[]}
    with e.db.connection() as con:
        with pytest.raises(ValueError,match='destination_not_saved'):_destination(con,'7','-20',2)
    adapter.save_saved({'chat_id':'-20','account_id':'7','account_epoch':2})
    result=adapter.query_saved()['items']
    assert len(result)==1 and result[0]['title']=='Test' and result[0]['kind']=='group'
    assert result[0]['sender']=={'kind':'user','id':'7'}
    with e.db.connection() as con:assert _destination(con,'7','-20',2)[0]['title']=='Test'
    assert not any(r['@type']=='sendMessage' for r in e.client.requests)


@pytest.mark.parametrize('changes',[{'id':-21},{'message_sender_id':{'@type':'messageSenderUser','user_id':8}},
                                  {'message_sender_id':{'@type':'messageSenderChat','chat_id':-20}}])
def test_basic_group_wire_response_cannot_substitute_another_chat_or_sender(listing_env,changes):
    e=listing_env;verifier=DestinationVerifier(e.client,e.auth,e.config,e.session)
    adapter=DestinationFeatures(verifier,native_gate=lambda:False,metadata_gate=lambda:True,listing=e.listing)
    adapter.begin_verify({'chat_id':'-20','kind':'group','sender':{'kind':'user','id':'7'}})
    verifier.consume({'@type':'chat','@extra':verifier.pending['extra'],'id':-20,'title':'Test',
        'type':{'@type':'chatTypeBasicGroup','basic_group_id':20},
        'permissions':{'can_send_basic_messages':True},**changes})
    assert adapter.query()=={'state':'failed','snapshot':None}
    with pytest.raises(ValueError):adapter.save_saved({'chat_id':'-20','account_id':'7','account_epoch':2})
    assert adapter.query_saved()=={'items':[]}
    assert [request['@type'] for request in e.client.requests]==['getChat']


def test_save_requires_current_exact_verified_selection(listing_env):
    e=listing_env;adapter=verified(e)
    for request in ({'chat_id':'-21','account_id':'7','account_epoch':2},
                    {'chat_id':'-20','account_id':'8','account_epoch':2},
                    {'chat_id':'-20','account_id':'7','account_epoch':3}):
        with pytest.raises(ValueError):adapter.save_saved(request)
    adapter.verifier.invalidate()
    with pytest.raises(ValueError):adapter.save_saved({'chat_id':'-20','account_id':'7','account_epoch':2})
    assert adapter.query_saved()=={'items':[]}


def test_saved_metadata_is_persistent_and_bound_to_account_epoch(listing_env):
    e=listing_env;adapter=verified(e)
    adapter.save_saved({'chat_id':'-20','account_id':'7','account_epoch':2})
    assert SavedDestinations(e.db).query('7',2)['items'][0]['chat_id']=='-20'
    e.auth.account=replace(e.auth.account,epoch=3)
    assert adapter.query_saved()=={'items':[]}
    assert SavedDestinations(e.db).query('8',2)=={'items':[]}


def test_reading_saved_chats_never_starts_requests(listing_env):
    e=listing_env;adapter=verified(e)
    count=len(e.client.requests)
    for _ in range(4):assert adapter.query_saved()=={'items':[]}
    assert len(e.client.requests)==count


def test_manual_history_send_exclusively_owns_verification_and_save(listing_env):
    from types import SimpleNamespace
    e=listing_env;adapter=verified(e)
    e.client._owner_history=SimpleNamespace(busy=True)
    with pytest.raises(ValueError):adapter.save_saved({'chat_id':'-20','account_id':'7','account_epoch':2})
    with pytest.raises(ValueError):adapter.begin_verify({'chat_id':'-20','kind':'group','sender':{'kind':'user','id':'7'}})
    assert adapter.query_saved()=={'items':[]}


def test_manual_history_send_blocks_new_chat_discovery_requests(listing_env):
    from types import SimpleNamespace
    from threadrelay.features import LocalFeatures
    from threadrelay.main import dispatch
    e=listing_env
    features=LocalFeatures(e.db,listing=e.listing,session=e.session,owner_history=SimpleNamespace(busy=True))
    before=len(e.client.requests)
    result=dispatch({'version':1,'id':'refresh','type':'RefreshTelegramDestinations','payload':{'load_more':False}},features=features)
    assert result['type']=='Error'
    assert len(e.client.requests)==before


def test_verified_own_private_chat_is_saved_as_saved_messages(listing_env):
    e=listing_env;verifier=DestinationVerifier(e.client,e.auth,e.config,e.session)
    adapter=DestinationFeatures(verifier,native_gate=lambda:False,metadata_gate=lambda:True,listing=e.listing)
    adapter.begin_verify({'chat_id':'7','kind':'private','sender':None})
    for body in ({'@type':'chat','id':7,'title':'Own chat','type':{'@type':'chatTypePrivate','user_id':7},'message_sender_id':None},
                 {'@type':'canSendMessageToUserResultOk'}):
        assert verifier.consume({**body,'@extra':verifier.pending['extra']})
    assert adapter.query()['state']=='verified'
    adapter.save_saved({'chat_id':'7','account_id':'7','account_epoch':2})
    assert adapter.query_saved()['items'][0]['kind']=='saved_messages'


def test_product_saved_chat_commands_are_read_only_until_explicit_verified_save(tmp_path):
    from threadrelay.application import Application
    from threadrelay.main import dispatch
    from threadrelay.contracts import validate_contract
    with Application(tmp_path/'app') as app:
        result=dispatch({'version':1,'id':'saved','type':'QuerySavedTelegramDestinations','payload':{}},features=app.features)
        validate_contract('Event',result)
        assert result['type']=='SavedTelegramDestinations' and result['payload']=={'items':[]}
        denied=dispatch({'version':1,'id':'save','type':'SaveTelegramDestination','payload':{'chat_id':'-20','account_id':'7','account_epoch':2}},features=app.features)
        assert denied['type']=='Error' and app.telegram._client is None
        with app.db.connection() as con:
            assert con.execute("SELECT payload FROM local_policy WHERE name='saved-telegram-destinations-required'").fetchone()[0]=='true'
