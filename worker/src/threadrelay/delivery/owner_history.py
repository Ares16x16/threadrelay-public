"""One explicitly confirmed History job on the connection-only native owner.

No production capability, background enrollment, reconnect or retry is granted.
Every possible dispatch consumes a durable exact claim and profile transition;
only exact correlated native success and durable completion can produce Sent.
Known success keeps the authenticated client alive for a separately confirmed job.
"""
from copy import deepcopy
from datetime import timedelta
import hashlib
import hmac
import json
import math
import secrets
import time

from .events import AttemptEvents
from .owner_test import _request
from .telegram import NativeJsonBinding, TelegramBoundaryError, _group_metadata_identity
from threadrelay.routing.policy import _destination
from threadrelay.storage.outbox import Outbox

_PERMIT_KEY=object()


class ManualHistoryPreviewError(TelegramBoundaryError):
    """Fixed recovery category; never native or storage exception text."""
    REASONS=frozenset({'connection_required','connecting','account_changed','busy',
        'outcome_unknown','pending_delivery','build_unavailable','job_changed'})

    def __init__(self,reason):
        if reason not in self.REASONS:raise ValueError('Invalid manual recovery reason')
        self.reason=reason
        super().__init__('Manual History preflight unavailable')


class OwnerHistoryPermit:
    """Backend-owned exact request, not an IPC DTO or general send authority."""
    def __init__(self,key,profile,binding,request,recheck):
        if key is not _PERMIT_KEY:raise TelegramBoundaryError('Manual History unavailable')
        self._profile,self._binding=profile,deepcopy(binding)
        self._request=json.dumps(request,ensure_ascii=False,allow_nan=False)
        self._recheck,self._used=recheck,False

    def take_for_native(self,client_id,profile,generation,account_id):
        expected=self._binding
        if (self._used or profile is not self._profile or type(client_id) is not int or client_id!=expected['client_id']
                or type(generation) is not int or generation!=expected['generation']
                or type(account_id) is not int or str(account_id)!=expected['account_id']):
            raise TelegramBoundaryError('Manual History unavailable')
        self._recheck();profile.recheck()
        if profile.purpose!='manual_history_pending' or profile.manual_history_record()!=expected:
            raise TelegramBoundaryError('Manual History profile unavailable')
        self._used=True
        return json.loads(self._request)


class OwnerHistoryMetadataPermit:
    """One exact saved-group lookup for the confirmed job, without discovery."""
    def __init__(self,key,profile,binding,recheck):
        if key is not _PERMIT_KEY:raise TelegramBoundaryError('Manual History metadata unavailable')
        self._profile,self._binding=profile,deepcopy(binding)
        self._recheck,self._used=recheck,False

    def take_for_native(self,client_id,profile,generation,account_id):
        expected=self._binding
        if (self._used or profile is not self._profile or type(client_id) is not int or client_id!=expected['client_id']
                or type(generation) is not int or generation!=expected['generation']
                or type(account_id) is not int or str(account_id)!=expected['account_id']):
            raise TelegramBoundaryError('Manual History metadata unavailable')
        self._recheck();profile.recheck()
        if profile.purpose not in {'connection_only','owner_test_sent','manual_history_sent','automatic_sent','automatic_failed'}:
            raise TelegramBoundaryError('Manual History metadata unavailable')
        self._used=True
        return int(expected['chat_id'])


class OwnerHistoryController:
    def __init__(self,client,auth,session,workflow,*,native_gate,monotonic=time.monotonic):
        if (auth._client is not client or auth._lifecycle_lock is not session.authority_lock
                or workflow.auth is not auth or workflow.sender.client is not client
                or getattr(client,'_owner_history',None) is not None):
            raise ValueError('Manual History requires the shared ordered owner')
        self.client,self.auth,self.session,self.workflow=client,auth,session,workflow
        self.native_gate,self.monotonic=native_gate,monotonic
        self._state='idle';self._reason=None;self._preview=None;self._plan=None
        self._binding=self._profile=self._verification=self._claim=None
        self._deadline=self._last_clock=None;self._facts={};self._events=AttemptEvents()
        self._consumed=self._transition_started=self._claim_started=False
        self._temporary=self._final=None
        self._marker_binding=None
        self._preparation=None;self._list_index=0
        client._owner_history=self

    @property
    def busy(self):return self._state in {'verifying','sending','closing'}

    def reset_after_isolation(self,delivery_id):
        if self._marker_binding is None or self._marker_binding['delivery_id']!=delivery_id:return
        if self.busy:raise TelegramBoundaryError('Manual delivery still active')
        self._state='idle';self._reason=None;self._plan=None;self._claim=None
        self._preview=None
        self._binding=self._profile=self._verification=None;self._events=AttemptEvents()

    @property
    def active_job_id(self):return self._plan['job_id'] if self.busy and self._plan is not None else None

    def query(self):
        with self.session.authority_lock:
            return {'state':self._state,'job_id':None if self._plan is None else self._plan['job_id'],
                'job_version':None if self._plan is None else self._plan['job_version'],'reason':self._reason}

    def _now(self):
        now=self.monotonic()
        if type(now) not in (int,float) or not math.isfinite(now) or self._last_clock is not None and now<self._last_clock:
            raise TelegramBoundaryError('Manual History clock unavailable')
        self._last_clock=now
        return now

    def _available(self):
        account=self.auth.snapshot();binding=self.client._binding;profile=self.client._profile_guard
        other=getattr(self.client,'_owner_test',None);automatic=getattr(self.client,'_owner_automatic',None)
        listing=getattr(self.client,'_destination_listing',None)
        if self._state=='unknown':raise ManualHistoryPreviewError('outcome_unknown')
        if self.native_gate() is not True:raise ManualHistoryPreviewError('build_unavailable')
        if account.revocation_pending:raise ManualHistoryPreviewError('account_changed')
        if account.network_state=='connecting':raise ManualHistoryPreviewError('connecting')
        if self.auth.status!='ready' or account.auth_state!='ready' or account.network_state!='online':
            raise ManualHistoryPreviewError('connection_required')
        if (self.busy or other is not None and other.busy or automatic is not None and automatic.busy
                or listing is not None and listing.state=='loading'
                or self.client._destination_verifier.pending is not None):raise ManualHistoryPreviewError('busy')
        if profile is not None and not self.workflow.manual_history_profile_ready(profile.profile_id):
            raise ManualHistoryPreviewError('outcome_unknown')
        if not self.client.local_attempts_quiescent():raise ManualHistoryPreviewError('pending_delivery')
        if (not self.client.transport_ready
                or type(binding) is not NativeJsonBinding or not binding.connection_only or binding.generation<1
                or profile is None or profile.purpose not in {'connection_only','owner_test_sent','manual_history_sent','automatic_sent','automatic_failed'}
                or binding._profile is not profile or self.client._client_id!=binding._owned_client):
            raise ManualHistoryPreviewError('connection_required')
        profile.recheck()
        return binding,profile,account

    def _check_provider_wait(self, target):
        from .route_schedule import RouteSchedule
        with self.workflow.db.connection() as con:
            if RouteSchedule(self.workflow.db).provider_wait(con,target['account_id'],target['destination_id']):
                raise ManualHistoryPreviewError('busy')

    def preview(self,request):
        with self.session.authority_lock:
            try:
                binding,profile,account=self._available()
                try:plan=self.workflow.manual_history_plan(request['job_id'],request['version'])
                except ValueError:raise ManualHistoryPreviewError('job_changed') from None
                self._check_provider_wait(plan['target'])
                _group_metadata_identity(plan['target']['destination_id'])
                if (account.account_id,account.epoch)!=(plan['target']['account_id'],plan['target']['account_epoch']):
                    raise ManualHistoryPreviewError('account_changed')
                expiry=self._now()+30;receipt=secrets.token_hex(32)
                self._preview={'plan':deepcopy(plan),'binding':binding,'profile':profile,'expiry':expiry,'receipt':receipt,
                    'client_id':self.client._client_id,'generation':binding.generation}
                return {key:deepcopy(plan[key]) for key in ('job_id','job_version','revision_id','text','destination','target')} | {
                    'preview_receipt':receipt,'expires_at':(self.workflow.clock.now()+timedelta(seconds=30)).isoformat()}
            except ManualHistoryPreviewError:
                self._preview=None
                raise
            except Exception:
                self._preview=None
                raise TelegramBoundaryError('Manual History preflight unavailable') from None

    def begin(self,request):
        with self.session.authority_lock:
            started=False
            try:
                exact=self._preview
                if (exact is None or set(request)!={'job_id','version','preview_receipt'}
                        or request['job_id']!=exact['plan']['job_id'] or request['version']!=exact['plan']['job_version']
                        or not isinstance(request['preview_receipt'],str) or not hmac.compare_digest(request['preview_receipt'],exact['receipt'])
                        or self._now()>=exact['expiry']):raise ValueError
                binding,profile,account=self._available()
                if (binding is not exact['binding'] or profile is not exact['profile']
                        or self.client._client_id!=exact['client_id'] or binding.generation!=exact['generation']
                        or self.workflow.manual_history_plan(request['job_id'],request['version'])!=exact['plan']):raise ValueError
                if self._binding is not binding:self._events=AttemptEvents()
                self._preview=None;self._plan=deepcopy(exact['plan']);self._binding,self._profile=binding,profile
                self._claim=None;self._facts={};self._temporary=self._final=None
                self._marker_binding=None
                self._preparation=None;self._list_index=0;self._verification=None
                self._consumed=self._transition_started=self._claim_started=False
                self._state='verifying';self._reason=None;self._deadline=self._now()+30
                started=True
                target=self._plan['target']
                metadata={'job_id':self._plan['job_id'],'job_version':self._plan['job_version'],
                    'profile_id':profile.profile_id,'client_id':self.client._client_id,'generation':binding.generation,
                    'account_id':target['account_id'],'account_epoch':target['account_epoch'],'chat_id':target['destination_id']}
                binding.scope_owner_history_metadata(OwnerHistoryMetadataPermit(_PERMIT_KEY,profile,metadata,self._metadata_context))
                kind,native_id=_group_metadata_identity(target['destination_id'])
                if kind=='chatTypeBasicGroup':self._prepare_send('basic','getBasicGroup',basic_group_id=native_id)
                else:self._prepare_list()
            except Exception:
                self._preview=None
                if started:self._fail('preflight_unavailable')
                elif self._state in {'idle','failed'}:self._state='failed';self._reason='preflight_unavailable'
                raise TelegramBoundaryError('Manual History preflight unavailable') from None

    def _context(self,*,closing=False,check_native=True):
        account=self.auth.snapshot();target=self._plan['target']
        if not closing and self._state=='verifying':self._check_provider_wait(target)
        if ((check_native and self.native_gate() is not True) or account.revocation_pending
                or (account.account_id,account.epoch)!=(target['account_id'],target['account_epoch'])
                or self.client._binding is not self._binding or self.client._profile_guard is not self._profile
                or self.client._client_id!=self._binding._owned_client
                or not closing and (self.auth.status!='ready' or account.auth_state!='ready'
                    or account.network_state!='online' or not self.client.transport_ready)):
            raise TelegramBoundaryError('Manual History authority changed')
        with self.workflow.db.connection() as con:
            destination,version=_destination(con,target['account_id'],target['destination_id'],target['account_epoch'])
            if destination!=self._plan['destination'] or version!=target['destination_version']:raise ValueError
        self._profile.recheck()
        if self._deadline is not None and self._now()>=self._deadline:raise TimeoutError

    def _metadata_context(self):
        self._context()
        if (self._state!='verifying' or self.workflow.manual_history_plan(self._plan['job_id'],self._plan['job_version'])!=self._plan):
            raise TelegramBoundaryError('Manual History metadata unavailable')

    def _prepare_send(self,stage,method,**fields):
        self._metadata_context()
        prefix={'basic':'owner-history:prepare-basic:','create':'owner-history:prepare-chat:',
            'load':'owner-history:prepare-load:','ids':'owner-history:prepare-ids:'}[stage]
        extra=prefix+secrets.token_hex(16)
        self._preparation={'stage':stage,'extra':extra}
        self.client._send({'@type':method,'@extra':extra,**fields})

    def _prepare_list(self):
        # Missing supergroup hashes require bounded native dialog loading.
        # Only IDs are inspected; no per-chat enumeration or content is retained.
        name='chatListMain' if self._list_index==0 else 'chatListArchive'
        self._prepare_send('load','loadChats',chat_list={'@type':name},limit=100)

    def _consume_preparation(self,raw):
        pending=self._preparation
        if pending is None or raw.get('@extra')!=pending.get('extra'):return
        self._metadata_context();stage=pending['stage'];kind,native_id=_group_metadata_identity(self._plan['target']['destination_id'])
        if stage=='basic':
            if raw.get('@type')!='basicGroup' or type(raw.get('id')) is not int or raw['id']!=native_id or raw.get('is_active') is not True:
                raise ValueError
            self._prepare_send('create','createBasicGroupChat',basic_group_id=native_id,force=False)
        elif stage=='create':
            chat_type=raw.get('type');sender=raw.get('message_sender_id')
            if (raw.get('@type')!='chat' or type(raw.get('id')) is not int
                    or str(raw['id'])!=self._plan['target']['destination_id'] or raw.get('title')!=self._plan['destination']['title']
                    or not isinstance(chat_type,dict) or chat_type.get('@type')!=kind or chat_type.get('basic_group_id')!=native_id
                    or sender not in (None,{'@type':'messageSenderUser','user_id':int(self._plan['target']['account_id'])})):
                raise ValueError
            self._preparation={'stage':'ready'}
        elif stage=='load':
            # 404 means exhausted; its already loaded prefix may contain target.
            if raw.get('@type')!='ok' and not (raw.get('@type')=='error' and raw.get('code')==404):raise ValueError
            name='chatListMain' if self._list_index==0 else 'chatListArchive'
            self._prepare_send('ids','getChats',chat_list={'@type':name},limit=100)
        elif stage=='ids':
            ids=raw.get('chat_ids')
            if (raw.get('@type')!='chats' or not isinstance(ids,list) or len(ids)>100
                    or any(type(value) is not int or value==0 or abs(value)>=2**53 for value in ids)
                    or len(set(ids))!=len(ids) or type(raw.get('total_count')) is not int or raw['total_count']<len(ids)):
                raise ValueError
            if int(self._plan['target']['destination_id']) in ids:self._preparation={'stage':'ready'}
            elif self._list_index==0:self._list_index=1;self._prepare_list()
            else:raise ValueError

    def _native_fact(self,raw):
        pending=self._verification
        if pending is None or raw.get('@extra')!=pending.get('extra'):return
        stage=pending.get('stage');target=self._plan['target'];account=int(target['account_id'])
        if stage=='chat':
            kind=raw.get('type',{});sender=raw.get('message_sender_id')
            self._facts['chat']=(raw.get('@type')=='chat' and type(raw.get('id')) is int
                and str(raw['id'])==target['destination_id'] and raw.get('title')==self._plan['destination']['title']
                and isinstance(kind,dict) and kind.get('@type') in {'chatTypeBasicGroup','chatTypeSupergroup'}
                and (kind['@type']=='chatTypeBasicGroup' or kind.get('is_channel') is False)
                and (sender is None or sender=={'@type':'messageSenderUser','user_id':account}))
        elif stage=='member':
            status=raw.get('status');role=status.get('@type') if isinstance(status,dict) else None
            personal=(role in {'chatMemberStatusMember','chatMemberStatusRestricted'}
                or role=='chatMemberStatusCreator' and status.get('is_anonymous') is False
                or role=='chatMemberStatusAdministrator' and isinstance(status.get('rights'),dict) and status['rights'].get('is_anonymous') is False)
            self._facts['member']=(raw.get('@type')=='chatMember'
                and raw.get('member_id')=={'@type':'messageSenderUser','user_id':account} and personal)

    def _mark_unknown(self):
        if self._transition_started and self._marker_binding is not None:
            try:self._profile.quarantine_manual_history(self._marker_binding)
            except Exception:pass  # Durable incomplete DB latch still denies new permits.
        if self._claim is not None:
            try:self.workflow.unknown_claim(self._claim)
            except Exception:self.workflow._uncertain.add(self._claim['job_id'])
        elif self._claim_started and self._plan is not None:
            try:self.workflow.unknown_manual_history(self._plan['job_id'])
            except Exception:self.workflow._uncertain.add(self._plan['job_id'])

    def _fail(self,reason):
        possible=self._consumed or self._transition_started or self._claim_started
        self._state='unknown' if possible else 'failed';self._reason=reason
        if self._binding is not None:self._binding.clear_owner_history_metadata()
        verifier=self.client._destination_verifier
        if verifier.pending is self._verification:verifier.invalidate()
        if possible:self._mark_unknown()

    def tick(self):
        with self.session.authority_lock:
            if not self.busy:return
            try:
                self._context(closing=self._state=='closing',check_native=False)
                if self.native_gate() is not True:
                    if self._state=='verifying':return
                    self._fail('authority_changed');return
                if self._state!='verifying':return
                verifier=self.client._destination_verifier
                if self._preparation is not None:
                    if self._preparation.get('stage')!='ready' or getattr(self.client,'_last_receive_had_event',True):return
                    self._metadata_context()
                    self._preparation=None
                    verifier.begin_group(self._plan['target']['destination_id'],sender=self._plan['target']['sender'])
                    self._verification=verifier.pending
                    return
                if verifier.pending is not self._verification:raise ValueError
                if verifier.pending.get('stage')!='complete':return
                snapshot,version=verifier.finish()
                self._binding.clear_owner_history_metadata()
                if (not all(self._facts.get(key) is True for key in ('chat','member'))
                        or snapshot!=self._plan['destination'] or version!=self._plan['target']['destination_version']):raise ValueError
                self._claim_started=True
                self._claim=self.workflow.claim_manual_history(self._plan['job_id'],self._plan['job_version'],self._plan,self._profile.profile_id)
                delivery=self._claim['delivery'];target=self._plan['target']
                binding={'job_id':self._claim['job_id'],'job_version':self._claim['job_version'],'delivery_id':delivery['id'],
                    'attempt_id':delivery['attempt_id'],'request_extra':delivery['request_extra'],'account_id':target['account_id'],
                    'account_epoch':target['account_epoch'],'chat_id':target['destination_id'],'sender_id':target['account_id'],
                    'destination_version':version,'profile_id':self._profile.profile_id,
                    'content_hash':hashlib.sha256(self._plan['text'].encode('utf-8')).hexdigest(),
                    'generation':self._binding.generation,'client_id':self.client._client_id}
                self._events.register(delivery_id=delivery['id'],attempt_id=binding['attempt_id'],request_extra=binding['request_extra'],
                    account_id=binding['account_id'],epoch=binding['account_epoch'],chat_id=binding['chat_id'],
                    sender=target['sender'],destination_version=version,content_hash=binding['content_hash'])
                self._transition_started=True
                self._marker_binding=binding
                self._profile.begin_manual_history(binding)
                self._consumed=True;self._state='sending';self._deadline=self._now()+30
                permit=OwnerHistoryPermit(_PERMIT_KEY,self._profile,binding,
                    _request(binding['chat_id'],self._plan['text'],binding['request_extra']),self._context)
                self.workflow.initiate_manual_history(self._claim,lambda:self._binding.send_owner_history(permit))
            except TimeoutError:self._fail('timed_out')
            except Exception:self._fail('dispatch_unconfirmed' if self._claim_started else 'verification_failed')

    def consume(self,raw):
        with self.session.authority_lock:
            if (self._binding is None or not isinstance(raw,dict) or type(raw.get('@client_id')) is not int
                    or raw['@client_id']!=self._binding._owned_client):return
            closed=(raw.get('@type')=='updateAuthorizationState' and isinstance(raw.get('authorization_state'),dict)
                and raw['authorization_state'].get('@type')=='authorizationStateClosed')
            if self._state=='unknown':
                if closed:self._profile.close()
                return
            if self._state=='verifying':
                try:
                    if self._preparation is not None:self._consume_preparation(raw)
                    else:self._native_fact(raw)
                except Exception:self._fail('verification_failed')
                return
            if self._state not in {'sending','closing'}:return
            try:
                self._context(closing=self._state=='closing')
                if raw.get('@type')=='updateAuthorizationState':
                    if closed or raw.get('authorization_state',{}).get('@type')!='authorizationStateReady':
                        self._fail('authority_changed')
                        if closed:self._profile.close()
                    return
                if self._state!='sending':return
                self._events.consume(raw)
                while self._events.events:
                    event=self._events.events.popleft()
                    if event.kind=='initiated':
                        self._temporary=event.temporary_message_id
                        if not Outbox(self.workflow.db).transition(event.delivery_id,'sending','sending',{
                                'delivery_id':event.delivery_id,'attempt_id':event.attempt_id,'request_extra':event.request_extra,
                                'temporary_message_id':self._temporary,'final_message_id':None,'state':'sending'}):raise ValueError
                    elif event.kind=='succeeded':
                        self._temporary=event.temporary_message_id;self._final=event.final_message_id
                        try:
                            self._profile.complete_manual_history_sent(self._marker_binding,self._temporary,self._final,
                                active_client_id=raw['@client_id'])
                            self.workflow.complete_manual_history(self._claim,self._temporary,self._final)
                            self.workflow.finish_manual_history(self._claim)
                            self._profile.finalize_manual_history_sent(self._marker_binding)
                            self._events.retire(event.request_extra)
                            self._state='sent';self._reason=None;self._deadline=None
                        except Exception:self._fail('durability_unconfirmed')
                    elif event.kind in {'failed','unknown'}:self._fail('dispatch_unconfirmed')
            except TimeoutError:self._fail('timed_out')
            except Exception:self._fail('authority_changed')

    def revoke(self):
        with self.session.authority_lock:
            self._preview=None
            if self.session.snapshot['state'] in {'pausing','stopping'} and self.transport_preservable():
                return  # Collection stopped; observe only the existing exact native attempt.
            if self.busy:self._fail('authority_changed')

    def transport_preservable(self):
        try:
            if self._state!='sending' or self._claim is None:return False
            self._context()
            current=Outbox(self.workflow.db).get(self._claim['delivery']['id'])
            return bool(current and current['state']=='sending'
                and current['attempt_id']==self._claim['delivery']['attempt_id']
                and self._profile.purpose=='manual_history_pending'
                and self._profile.manual_history_record()==self._marker_binding)
        except Exception:return False
