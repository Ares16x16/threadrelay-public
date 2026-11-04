"""Authenticated automatic delivery provenance, isolated from live Telegram."""
import json

import pytest

from threadrelay.delivery.connection_profile import connection_only_profile
from test_owner_history_profile import history_binding


def automatic_binding(profile, *, index=1):
    attempt=f'00000000-0000-4000-8000-{index:012d}'
    return {'attempt_id':attempt,'request_extra':f'10000000-0000-4000-8000-{index:012d}',
        'delivery_id':f'20000000-0000-4000-8000-{index:012d}',
        'account_id':'7','account_epoch':2,'chat_id':'-20','sender_id':'7',
        'destination_version':1,'content_hash':'b'*64,'generation':3,'client_id':1,
        'profile_id':profile.profile_id,'plan_id':'c'*64,'policy_hash':'d'*64}


def test_automatic_reuses_live_profile_only_after_exact_durable_finalization(tmp_path):
    profile=connection_only_profile(tmp_path/'profile','vault:fixture',b'k'*32)
    first=automatic_binding(profile);second=automatic_binding(profile,index=2)
    profile.begin_automatic(first)
    assert profile.purpose=='automatic_pending' and profile.automatic_record()==first
    with pytest.raises(ValueError):profile.begin_manual_history(history_binding(profile))
    with pytest.raises(ValueError):profile.begin_automatic(second)
    profile.complete_automatic_sent(first,'-1','8',active_client_id=1)
    assert profile.purpose=='automatic_sent' and profile.automatic_record()['closed'] is False
    with pytest.raises(ValueError):profile.begin_automatic(second)
    with pytest.raises(ValueError):profile.finalize_automatic_sent({**first,'plan_id':'e'*64})
    profile.finalize_automatic_sent(first)
    with pytest.raises(ValueError):profile.begin_automatic(first)
    profile.begin_automatic(second)
    profile.complete_automatic_sent(second,'-2','9',active_client_id=1)
    profile.finalize_automatic_sent(second)
    profile.recheck();profile.close()
    reopened=connection_only_profile(tmp_path/'profile','vault:fixture',b'k'*32)
    assert reopened.automatic_record()['delivery_id']=='20000000-0000-4000-8000-000000000002'
    reopened.close()


@pytest.mark.parametrize('completed',[False,True])
def test_incomplete_or_quarantined_automatic_profile_cannot_reopen(tmp_path,completed):
    path=tmp_path/'profile';profile=connection_only_profile(path,'vault:fixture',b'k'*32)
    binding=automatic_binding(profile);profile.begin_automatic(binding)
    if completed:profile.complete_automatic_sent(binding,'-1','8',active_client_id=1)
    else:
        with pytest.raises(ValueError):connection_only_profile(path,'vault:fixture',b'k'*32)
    profile.quarantine_automatic(binding)
    assert profile.purpose=='automatic_unknown'
    profile.close()
    with pytest.raises(ValueError):connection_only_profile(path,'vault:fixture',b'k'*32)


def test_manual_and_automatic_preserve_distinct_authenticated_receipts(tmp_path):
    path=tmp_path/'profile';profile=connection_only_profile(path,'vault:fixture',b'k'*32)
    manual=history_binding(profile);profile.begin_manual_history(manual)
    profile.complete_manual_history_sent(manual,'-1','8',active_client_id=1)
    with pytest.raises(ValueError):profile.begin_automatic(automatic_binding(profile))
    profile.finalize_manual_history_sent(manual)
    manual_record=profile.manual_history_record()
    automatic=automatic_binding(profile);profile.begin_automatic(automatic)
    profile.complete_automatic_sent(automatic,'-2','9',active_client_id=1)
    with pytest.raises(ValueError):profile.begin_manual_history(history_binding(profile,suffix='c',job='two',delivery='e'))
    profile.finalize_automatic_sent(automatic)
    automatic_record=profile.automatic_record()
    second=history_binding(profile,suffix='c',job='two',delivery='e')
    profile.begin_manual_history(second)
    profile.complete_manual_history_sent(second,'-3','10',active_client_id=1)
    profile.finalize_manual_history_sent(second);profile.close()
    reopened=connection_only_profile(path,'vault:fixture',b'k'*32)
    assert reopened.purpose=='manual_history_sent'
    assert reopened.automatic_record()==automatic_record
    assert reopened.manual_history_record()['job_id']=='two'
    assert manual_record['job_id']=='job-one'
    assert json.loads((path/'connection-purpose.json').read_text())['body']['schema_version']==5
    reopened.close()


@pytest.mark.parametrize('change',[
    {'attempt_id':'x'*36},{'request_extra':'owner-history:'+'a'*32},{'delivery_id':'d'*32},
    {'plan_id':'z'*64},{'policy_hash':'x'*64},{'sender_id':'8'},{'account_epoch':True},
    {'client_id':True},{'chat_id':'0'},{'unreviewed':True},
])
def test_automatic_profile_rejects_unbound_or_malformed_authority(tmp_path,change):
    profile=connection_only_profile(tmp_path/'profile','vault:fixture',b'k'*32)
    with pytest.raises(ValueError):profile.begin_automatic({**automatic_binding(profile),**change})
    assert profile.purpose=='connection_only'
    profile.close()


def automatic_native(tmp_path):
    from test_connection_only import LocalLibrary
    from threadrelay.delivery.telegram import NativeJsonBinding
    profile=connection_only_profile(tmp_path/'profile','vault:fixture',b'k'*32)
    library=LocalLibrary();native=NativeJsonBinding(library,purpose='connection_only',generation=3)
    native.bind_connection_profile(profile);native.create();native._connection_account_id=7
    return profile,library,native


def test_native_automatic_permit_is_exact_one_use_without_generic_send_or_retry(tmp_path):
    from threadrelay.delivery.owner_automatic import OwnerAutomaticPermit,_PERMIT_KEY
    from threadrelay.delivery.owner_test import _request
    from threadrelay.delivery.telegram import TelegramBoundaryError
    profile,library,native=automatic_native(tmp_path)
    binding=automatic_binding(profile);profile.begin_automatic(binding)
    request=_request('-20','Only this exact text',binding['request_extra'])
    permit=OwnerAutomaticPermit(_PERMIT_KEY,profile,binding,request,lambda:None)
    native.send_owner_automatic(permit)
    assert len(library.calls)==1 and library.calls[0]==request
    duplicate=OwnerAutomaticPermit(_PERMIT_KEY,profile,binding,request,lambda:None)
    for action in (lambda:native.send_owner_automatic(permit),lambda:native.send_owner_automatic(duplicate),lambda:native.send_owner_history(permit),
                   lambda:native.send(1,request),lambda:native.send(1,{'@type':'resendMessages','chat_id':-20,'message_ids':[1]})):
        with pytest.raises(TelegramBoundaryError):action()
    assert len(library.calls)==1
    profile.close()


@pytest.mark.parametrize('mismatch',['client','profile','generation','account','policy','purpose'])
def test_native_automatic_rejects_foreign_or_revoked_authority_before_entry(tmp_path,mismatch):
    from threadrelay.delivery.owner_automatic import OwnerAutomaticPermit,_PERMIT_KEY
    from threadrelay.delivery.owner_test import _request
    from threadrelay.delivery.telegram import TelegramBoundaryError
    profile,library,native=automatic_native(tmp_path)
    binding=automatic_binding(profile);profile.begin_automatic(binding)
    expected={**binding}
    if mismatch=='client':expected['client_id']=2
    elif mismatch=='generation':expected['generation']=4
    elif mismatch=='account':expected['account_id']='8'
    elif mismatch=='profile':expected['profile_id']='f'*64
    if mismatch=='purpose':profile.complete_automatic_sent(binding,'-1','8',active_client_id=1)
    def recheck():
        if mismatch=='policy':raise TelegramBoundaryError('Policy changed')
    permit=OwnerAutomaticPermit(_PERMIT_KEY,profile,expected,_request('-20','Exact text',binding['request_extra']),recheck)
    with pytest.raises(TelegramBoundaryError):native.send_owner_automatic(permit)
    assert not library.calls
    profile.close()


def test_automatic_metadata_is_distinct_ordered_and_cannot_expand_to_unrelated_groups(tmp_path):
    from threadrelay.delivery.owner_automatic import OwnerAutomaticMetadataPermit,_PERMIT_KEY
    from threadrelay.delivery.owner_history import OwnerHistoryMetadataPermit,_PERMIT_KEY as MANUAL_KEY
    from threadrelay.delivery.telegram import TelegramBoundaryError
    profile,library,native=automatic_native(tmp_path)
    binding=automatic_binding(profile)
    permit=OwnerAutomaticMetadataPermit(_PERMIT_KEY,profile,binding,lambda:None)
    native.scope_owner_automatic_metadata(permit)
    with pytest.raises(TelegramBoundaryError):native.scope_owner_history_metadata(OwnerHistoryMetadataPermit(MANUAL_KEY,profile,binding,lambda:None))
    basic={'@type':'getBasicGroup','basic_group_id':20,'@extra':'owner-automatic:prepare-basic:'+'b'*32}
    with pytest.raises(TelegramBoundaryError):native.send(1,{**basic,'@extra':'owner-history:prepare-basic:'+'b'*32})
    native.send(1,basic)
    with pytest.raises(TelegramBoundaryError):native.send(1,basic)
    library.events.append({'@client_id':1,'@type':'basicGroup','id':20,'@extra':basic['@extra']});native.receive(0)
    create={'@type':'createBasicGroupChat','basic_group_id':20,'force':False,'@extra':'owner-automatic:prepare-chat:'+'c'*32}
    for changed in ({**create,'force':True},{**create,'basic_group_id':21}):
        with pytest.raises(TelegramBoundaryError):native.send(1,changed)
    native.send(1,create)
    library.events.append({'@client_id':1,'@type':'chat','id':-20,'@extra':create['@extra'],
        'type':{'@type':'chatTypeBasicGroup','basic_group_id':20}});native.receive(0)
    with pytest.raises(TelegramBoundaryError):native.send(1,{'@type':'getChat','chat_id':-21,'@extra':'setup:chat:'+'a'*32})
    native.send(1,{'@type':'getChat','chat_id':-20,'@extra':'setup:chat:'+'a'*32})
    with pytest.raises(TelegramBoundaryError):native.scope_owner_automatic_metadata(permit)
    assert [request['@type'] for request in library.calls]==['getBasicGroup','createBasicGroupChat','getChat']
    profile.close()


def test_native_automatic_entry_uncertainty_consumes_even_a_second_permit_object(tmp_path):
    from threadrelay.delivery.owner_automatic import OwnerAutomaticPermit,_PERMIT_KEY
    from threadrelay.delivery.owner_test import _request
    from threadrelay.delivery.telegram import TelegramBoundaryError
    profile,library,native=automatic_native(tmp_path)
    binding=automatic_binding(profile);profile.begin_automatic(binding)
    request=_request('-20','Exact text',binding['request_extra'])
    original=library.td_send.call
    def entered_then_failed(*args):
        original(*args)
        raise RuntimeError('Synthetic native entry uncertainty')
    library.td_send.call=entered_then_failed
    with pytest.raises(RuntimeError):native.send_owner_automatic(OwnerAutomaticPermit(_PERMIT_KEY,profile,binding,request,lambda:None))
    with pytest.raises(TelegramBoundaryError):native.send_owner_automatic(OwnerAutomaticPermit(_PERMIT_KEY,profile,binding,request,lambda:None))
    assert len(library.calls)==1
    profile.close()


def test_automatic_keeps_legacy_owner_test_receipt_within_private_marker_bound(tmp_path):
    from test_owner_test import attempt
    path=tmp_path/'profile';profile=connection_only_profile(path,'vault:fixture',b'k'*32)
    test={**attempt(),'profile_id':profile.profile_id}
    profile.begin_owner_test(test);profile.complete_owner_test_sent(test,'-1','8',closed_client_id=1)
    receipt=profile.owner_test_record();profile.close()
    profile=connection_only_profile(path,'vault:fixture',b'k'*32)
    manual=history_binding(profile);profile.begin_manual_history(manual)
    profile.complete_manual_history_sent(manual,'-2','9',active_client_id=1)
    profile.finalize_manual_history_sent(manual)
    binding=automatic_binding(profile);profile.begin_automatic(binding)
    profile.complete_automatic_sent(binding,'-3','10',active_client_id=1)
    profile.finalize_automatic_sent(binding);profile.close()
    reopened=connection_only_profile(path,'vault:fixture',b'k'*32)
    assert reopened.owner_test_record()==receipt
    assert reopened.manual_history_record()['job_id']=='job-one'
    assert reopened.automatic_record()['plan_id']=='c'*64
    assert (path/'connection-purpose.json').stat().st_size<=4096
    reopened.close()


def test_automatic_uncertain_pending_publication_closes_guard_and_denies_reopen(tmp_path,monkeypatch):
    import os
    import stat
    path=tmp_path/'profile';profile=connection_only_profile(path,'vault:fixture',b'k'*32)
    original=os.fsync
    def fail_directory(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):raise OSError('Synthetic directory fsync uncertainty')
        original(descriptor)
    monkeypatch.setattr(os,'fsync',fail_directory)
    with pytest.raises(OSError):profile.begin_automatic(automatic_binding(profile))
    with pytest.raises(ValueError):profile.recheck()
    with pytest.raises(ValueError):connection_only_profile(path,'vault:fixture',b'k'*32)


@pytest.mark.parametrize('client_id',[True,2,0,None])
def test_automatic_terminal_marker_rejects_foreign_active_client(tmp_path,client_id):
    profile=connection_only_profile(tmp_path/'profile','vault:fixture',b'k'*32)
    binding=automatic_binding(profile);profile.begin_automatic(binding)
    with pytest.raises(ValueError):profile.complete_automatic_sent(binding,'-1','8',active_client_id=client_id)
    assert profile.purpose=='automatic_pending'
    profile.close()


def test_exact_automatic_failure_is_reusable_only_after_durable_finalization(tmp_path):
    path=tmp_path/'profile';profile=connection_only_profile(path,'vault:fixture',b'k'*32)
    first=automatic_binding(profile);profile.begin_automatic(first)
    profile.complete_automatic_failed(first,'-1','9',429,'FLOOD_WAIT',15,active_client_id=1)
    assert profile.purpose=='automatic_failed'
    with pytest.raises(ValueError):profile.begin_automatic(automatic_binding(profile,index=2))
    profile.finalize_automatic_failed(first);profile.close()
    reopened=connection_only_profile(path,'vault:fixture',b'k'*32)
    assert reopened.purpose=='automatic_failed'
    assert reopened.automatic_record()['failed_message_id']=='9'
    with pytest.raises(ValueError):reopened.begin_automatic(first)
    reopened.begin_automatic(automatic_binding(reopened,index=2));reopened.close()
