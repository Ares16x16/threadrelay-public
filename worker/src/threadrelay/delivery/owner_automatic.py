"""Explicit score-policy automatic delivery on the existing authenticated owner.

The route's signed opt-in and current Router authority authorize each exact
pristine intent. Independent private permits never grant generic sends/retries.
Native terminal correlation, profile fsync and a durable DB latch precede Sent.
"""
from copy import deepcopy
import hashlib
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
_PREFLIGHT_RETRY_DELAYS=(1,2,3)


class _PreflightRetry(TelegramBoundaryError):
    pass


class _PreflightDenied(TelegramBoundaryError):
    pass

class OwnerAutomaticPermit:
    """Backend-owned exact request, not an IPC DTO or general send authority."""
    def __init__(self,key,profile,binding,request,recheck):
        if key is not _PERMIT_KEY:raise TelegramBoundaryError('Automatic delivery unavailable')
        self._profile,self._binding=profile,deepcopy(binding)
        self._request=json.dumps(request,ensure_ascii=False,allow_nan=False)
        self._recheck,self._used=recheck,False

    def take_for_native(self,client_id,profile,generation,account_id):
        expected=self._binding
        if (self._used or profile is not self._profile or type(client_id) is not int or client_id!=expected['client_id']
                or type(generation) is not int or generation!=expected['generation']
                or type(account_id) is not int or str(account_id)!=expected['account_id']):
            raise TelegramBoundaryError('Automatic delivery unavailable')
        self._recheck();profile.recheck()
        if profile.purpose!='automatic_pending' or profile.automatic_record()!=expected:
            raise TelegramBoundaryError('Automatic delivery profile unavailable')
        self._used=True
        return json.loads(self._request)


class OwnerAutomaticMetadataPermit:
    """One exact saved-group lookup for the confirmed job, without discovery."""
    def __init__(self,key,profile,binding,recheck):
        if key is not _PERMIT_KEY:raise TelegramBoundaryError('Automatic delivery metadata unavailable')
        self._profile,self._binding=profile,deepcopy(binding)
        self._recheck,self._used=recheck,False

    def take_for_native(self,client_id,profile,generation,account_id):
        expected=self._binding
        if (self._used or profile is not self._profile or type(client_id) is not int or client_id!=expected['client_id']
                or type(generation) is not int or generation!=expected['generation']
                or type(account_id) is not int or str(account_id)!=expected['account_id']):
            raise TelegramBoundaryError('Automatic delivery metadata unavailable')
        self._recheck();profile.recheck()
        if profile.purpose not in {'connection_only','owner_test_sent','manual_history_sent','automatic_sent','automatic_failed'}:
            raise TelegramBoundaryError('Automatic delivery metadata unavailable')
        self._used=True
        return int(expected['chat_id'])


class OwnerAutomaticController:
    def __init__(self,client,auth,session,queue,*,native_gate,monotonic=time.monotonic):
        if (auth._client is not client or auth._lifecycle_lock is not session.authority_lock
                or queue.auth is not auth or queue.sender.client is not client
                or getattr(client,'_owner_automatic',None) is not None):
            raise ValueError('Automatic delivery requires the shared ordered owner')
        self.client,self.auth,self.session,self.queue=client,auth,session,queue
        self.native_gate,self.monotonic=native_gate,monotonic
        self._state='idle';self._reason=None;self._preview=None;self._plan=None
        self._binding=self._profile=self._verification=self._claim=None
        self._deadline=self._last_clock=None;self._facts={};self._events=AttemptEvents()
        self._consumed=self._transition_started=self._claim_started=False
        self._temporary=self._final=None
        self._marker_binding=None
        self._preparation=None;self._list_index=0
        self._retry_at=None;self._retry_count=0;self._fact_failure=None
        self._diagnostic_stage='idle'
        client._owner_automatic=self

    @property
    def busy(self):return self._state in {'verifying','sending','closing'}

    def reset_after_isolation(self,delivery_id):
        if self._marker_binding is None or self._marker_binding['delivery_id']!=delivery_id:return
        if self.busy:raise TelegramBoundaryError('Automatic delivery still active')
        self._state='idle';self._reason=None;self._plan=None;self._claim=None
        self._binding=self._profile=self._verification=None;self._events=AttemptEvents()

    @property
    def active_job_id(self):return self._plan['job_id'] if self.busy and self._plan is not None else None

    def _now(self):
        now=self.monotonic()
        if type(now) not in (int,float) or not math.isfinite(now) or self._last_clock is not None and now<self._last_clock:
            raise TelegramBoundaryError('Automatic delivery clock unavailable')
        self._last_clock=now
        return now

    def _available(self):
        account=self.auth.snapshot();binding=self.client._binding;profile=self.client._profile_guard
        manual=getattr(self.client,'_owner_history',None)
        other=getattr(self.client,'_owner_test',None);listing=getattr(self.client,'_destination_listing',None)
        if not self.session.can_transmit(self.queue.clock.now(),automatic=True):raise TelegramBoundaryError('session_not_running')
        if self._state=='unknown':raise TelegramBoundaryError('outcome_unknown')
        if self.native_gate() is not True:raise TelegramBoundaryError('build_unavailable')
        if account.revocation_pending:raise TelegramBoundaryError('account_changed')
        if account.network_state=='connecting':raise TelegramBoundaryError('connecting')
        if self.auth.status!='ready' or account.auth_state!='ready' or account.network_state!='online':
            raise TelegramBoundaryError('connection_required')
        if (self.busy or manual is not None and manual.busy or other is not None and other.busy or listing is not None and listing.state=='loading'
                or self.client._destination_verifier.pending is not None):raise TelegramBoundaryError('busy')
        if profile is not None and not self.queue.profile_ready(profile.profile_id):
            raise TelegramBoundaryError('outcome_unknown')
        if not self.client.local_attempts_quiescent():raise TelegramBoundaryError('pending_delivery')
        if (not self.client.transport_ready
                or type(binding) is not NativeJsonBinding or not binding.connection_only or binding.generation<1
                or profile is None or profile.purpose not in {'connection_only','owner_test_sent','manual_history_sent','automatic_sent','automatic_failed'}
                or binding._profile is not profile or self.client._client_id!=binding._owned_client):
            raise TelegramBoundaryError('connection_required')
        profile.recheck()
        return binding,profile,account

    def begin(self,plan):
        with self.session.authority_lock:
            started=False
            try:
                binding,profile,account=self._available()
                if self.queue.plan(plan['job_id'],plan['job_version'])!=plan:raise ValueError
                if self._binding is not binding:self._events=AttemptEvents()
                self._preview=None;self._plan=deepcopy(plan);self._binding,self._profile=binding,profile
                self._claim=None;self._facts={};self._temporary=self._final=None
                self._marker_binding=None
                self._preparation=None;self._list_index=0;self._verification=None
                self._retry_at=None;self._retry_count=0;self._fact_failure=None
                self._consumed=self._transition_started=self._claim_started=False
                self._state='verifying';self._reason=None;self._deadline=self._now()+30
                started=True
                self._start_metadata()
            except Exception as error:
                self._preview=None
                if started:
                    self._record_failure(error)
                    self._fail(str(error) if isinstance(error,_PreflightDenied) else 'preflight_unavailable')
                elif self._state in {'idle','failed'}:self._state='failed';self._reason='preflight_unavailable'
                raise TelegramBoundaryError('Automatic delivery preflight unavailable') from None

    def _start_metadata(self):
        self._diagnostic_stage='scope_metadata'
        self._facts={};self._fact_failure=None;self._verification=None;self._preparation=None;self._list_index=0
        target=self._plan['target']
        metadata={'job_id':self._plan['job_id'],'job_version':self._plan['job_version'],
            'profile_id':self._profile.profile_id,'client_id':self.client._client_id,'generation':self._binding.generation,
            'account_id':target['account_id'],'account_epoch':target['account_epoch'],'chat_id':target['destination_id']}
        self._binding.scope_owner_automatic_metadata(OwnerAutomaticMetadataPermit(_PERMIT_KEY,self._profile,metadata,self._metadata_context))
        kind,native_id=_group_metadata_identity(target['destination_id'])
        if kind=='chatTypeBasicGroup':self._prepare_send('basic','getBasicGroup',basic_group_id=native_id)
        else:self._prepare_list()

    def _context(self,*,closing=False,check_native=True,check_connection=True):
        account=self.auth.snapshot();target=self._plan['target']
        if ((check_native and self.native_gate() is not True) or account.revocation_pending
                or (account.account_id,account.epoch)!=(target['account_id'],target['account_epoch'])
                or self.client._binding is not self._binding or self.client._profile_guard is not self._profile
                or self.client._client_id!=self._binding._owned_client
                or not closing and (self.auth.status!='ready' or account.auth_state!='ready')):
            raise TelegramBoundaryError('Automatic delivery authority changed')
        if not closing and check_connection and (account.network_state!='online' or not self.client.transport_ready):
            if self._state=='verifying' and account.network_state in {'connecting','offline'}:
                raise _PreflightRetry('connection_reconnecting')
            raise TelegramBoundaryError('Automatic delivery connection unavailable')
        if not closing:self.queue.recheck_active(self._plan,in_flight=self._state=='sending')
        with self.queue.db.connection() as con:
            destination,version=_destination(con,target['account_id'],target['destination_id'],target['account_epoch'])
            if destination!=self._plan['destination'] or version!=target['destination_version']:raise ValueError
        self._profile.recheck()
        if self._deadline is not None and self._now()>=self._deadline:raise TimeoutError

    def _metadata_context(self):
        self._context()
        if (self._state!='verifying' or self.queue.plan(self._plan['job_id'],self._plan['job_version'])!=self._plan):
            raise TelegramBoundaryError('Automatic delivery metadata unavailable')

    def _prepare_send(self,stage,method,**fields):
        self._diagnostic_stage='prepare_'+stage
        self._metadata_context()
        prefix={'basic':'owner-automatic:prepare-basic:','create':'owner-automatic:prepare-chat:',
            'load':'owner-automatic:prepare-load:','ids':'owner-automatic:prepare-ids:'}[stage]
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
        self._diagnostic_stage='prepare_'+stage
        if raw.get('@type')=='error' and not (stage=='load' and raw.get('code')==404):
            self._record_code({'basic':'auto_preflight_prepare_basic_error','create':'auto_preflight_prepare_create_error',
                'load':'auto_preflight_prepare_load_error','ids':'auto_preflight_prepare_ids_error'}.get(stage,'auto_preflight_prepare_unknown_error'))
            self._record_native_error(raw)
            raise _PreflightDenied('preflight_unavailable')
        if stage=='basic':
            if raw.get('@type')=='basicGroup' and raw.get('is_active') is False:
                self._record_code('auto_preflight_prepare_basic_inactive')
                raise _PreflightDenied('destination_chat_mismatch')
            if raw.get('@type')!='basicGroup' or type(raw.get('id')) is not int or raw['id']!=native_id or raw.get('is_active') is not True:
                raise ValueError
            self._prepare_send('create','createBasicGroupChat',basic_group_id=native_id,force=False)
        elif stage=='create':
            chat_type=raw.get('type');sender=raw.get('message_sender_id')
            if raw.get('@type')=='chat' and raw.get('title')!=self._plan['destination']['title']:
                raise _PreflightDenied('destination_title_changed')
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
        self._diagnostic_stage={'chat':'verify_chat','group':'verify_group','member':'verify_member'}.get(pending.get('stage'),'verify_other')
        if raw.get('@type')=='error':
            self._record_native_error(raw)
            raise _PreflightDenied('preflight_unavailable')
        stage=pending.get('stage');target=self._plan['target'];account=int(target['account_id'])
        if stage=='chat':
            kind=raw.get('type',{});sender=raw.get('message_sender_id')
            self._facts['chat']=(raw.get('@type')=='chat' and type(raw.get('id')) is int
                and str(raw['id'])==target['destination_id'] and raw.get('title')==self._plan['destination']['title']
                and isinstance(kind,dict) and kind.get('@type') in {'chatTypeBasicGroup','chatTypeSupergroup'}
                and (kind['@type']=='chatTypeBasicGroup' or kind.get('is_channel') is False)
                and (sender is None or sender=={'@type':'messageSenderUser','user_id':account}))
            if not self._facts['chat']:
                self._fact_failure=('destination_title_changed' if raw.get('@type')=='chat' and raw.get('title')!=self._plan['destination']['title']
                    else 'destination_chat_mismatch')
        elif stage=='member':
            status=raw.get('status');role=status.get('@type') if isinstance(status,dict) else None
            personal=(role in {'chatMemberStatusMember','chatMemberStatusRestricted'}
                or role=='chatMemberStatusCreator' and status.get('is_anonymous') is False
                or role=='chatMemberStatusAdministrator' and isinstance(status.get('rights'),dict) and status['rights'].get('is_anonymous') is False)
            self._facts['member']=(raw.get('@type')=='chatMember'
                and raw.get('member_id')=={'@type':'messageSenderUser','user_id':account} and personal)
            if not self._facts['member']:
                self._fact_failure=('destination_sender_unverified' if raw.get('@type')=='chatMember'
                    and raw.get('member_id')=={'@type':'messageSenderUser','user_id':account} and not personal
                    else 'destination_member_mismatch')

    def _record_code(self,code):
        try:self.queue.db.record_diagnostic(code)
        except Exception:pass  # Diagnostics cannot create or recover send authority.

    def _record_native_error(self,raw):
        code=raw.get('code')
        allowed={400,401,403,404,408,429,500,502,503,504}
        self._record_code('auto_preflight_native_error_'+str(code) if type(code) is int and code in allowed
            else 'auto_preflight_native_error_other')

    def _record_failure(self,error):
        # Fixed labels only. Never retain exception text, native payloads,
        # identifiers, paths, traceback locals or arbitrary class/frame names.
        stages={'idle','scope_metadata','context','prepare_basic','prepare_create','prepare_load','prepare_ids',
            'verifier_begin','verifier_finish','verify_chat','verify_group','verify_member','verify_other','claim'}
        stage=self._diagnostic_stage if self._diagnostic_stage in stages else 'other'
        self._record_code('auto_preflight_stage_'+stage)
        classes={'AttributeError':'attribute','ValueError':'value','TypeError':'type','KeyError':'key',
            'TimeoutError':'timeout','OSError':'os','RuntimeError':'runtime','TelegramBoundaryError':'boundary',
            'RoutingDenied':'routing','_PreflightDenied':'denied','_PreflightRetry':'retry'}
        self._record_code('auto_preflight_error_'+classes.get(type(error).__name__,'other'))
        frames={('threadrelay.delivery.owner_automatic',name):'owner_'+label for name,label in
            [('tick','tick'),('begin','begin'),('_consume_preparation','prepare'),('_native_fact','fact'),
             ('_context','context'),('_metadata_context','metadata'),('_start_metadata','scope'),('_prepare_send','prepare_send')]}
        frames.update({('threadrelay.delivery.telegram','_connection_request'):'native_request',
            ('threadrelay.delivery.telegram','send'):'native_send',('threadrelay.delivery.telegram','_send'):'client_send',
            ('threadrelay.delivery.destinations','_setup_send'):'verifier_send',('threadrelay.delivery.destinations','_begin'):'verifier_begin',
            ('threadrelay.delivery.destinations','finish'):'verifier_finish',('threadrelay.delivery.automatic_queue','plan'):'queue_plan',
            ('threadrelay.routing.policy','_receipt'):'router_receipt'})
        frame='unknown';origin=error;seen=set()
        for _ in range(4):
            if id(origin) in seen:break
            seen.add(id(origin));trace=origin.__traceback__
            while trace is not None:
                key=(trace.tb_frame.f_globals.get('__name__'),trace.tb_frame.f_code.co_name)
                frame=frames.get(key,frame);trace=trace.tb_next
            nested=origin.__cause__ or origin.__context__
            if nested is None:break
            origin=nested
        self._record_code('auto_preflight_cause_'+classes.get(type(origin).__name__,'other'))
        self._record_code('auto_preflight_frame_'+frame)

    def _retry_preflight(self,reason):
        # Only metadata reads can repeat. Claim/profile transition uncertainty
        # always takes the existing Unknown path and never reaches this wait.
        if self._consumed or self._transition_started or self._claim_started:
            self._fail('dispatch_unconfirmed');return
        if self._fact_failure is not None:
            self._fail(self._fact_failure);return
        if self._retry_count>=len(_PREFLIGHT_RETRY_DELAYS):
            self._fail('preflight_retry_exhausted');return
        self._binding.clear_owner_automatic_metadata()
        verifier=self.client._destination_verifier
        if verifier.pending is self._verification:verifier.invalidate()
        self._verification=None;self._preparation=None
        try:stamp=self._now()
        except Exception:
            self._fail('authority_changed');return
        self._retry_at=stamp+_PREFLIGHT_RETRY_DELAYS[self._retry_count]
        self._retry_count+=1;self._reason=reason

    def _mark_unknown(self):
        if self._transition_started and self._marker_binding is not None:
            try:self._profile.quarantine_automatic(self._marker_binding)
            except Exception:pass  # Durable incomplete DB latch still denies new permits.
        if self._claim is not None:
            try:self.queue.unknown(self._claim)
            except Exception:self.queue._uncertain.add(self._claim['job_id'])
        elif self._claim_started and self._plan is not None:
            try:self.queue.unknown_delivery(self._plan['job_id'])
            except Exception:self.queue._uncertain.add(self._plan['job_id'])

    def _fail(self,reason):
        possible=self._consumed or self._transition_started or self._claim_started
        self._state='unknown' if possible else 'failed';self._reason=reason
        self._retry_at=None
        if self._binding is not None:self._binding.clear_owner_automatic_metadata()
        verifier=self.client._destination_verifier
        if verifier.pending is self._verification:verifier.invalidate()
        if possible:self._mark_unknown()
        elif self._plan is not None:self.queue.preflight_failed(self._plan,reason)

    def tick(self):
        with self.session.authority_lock:
            if not self.busy:
                if self._state=='unknown':return
                plan=self.queue.due()
                if plan is None:return
                try:self.begin(plan)
                except Exception:return
                return
            try:
                self._diagnostic_stage='context'
                self._context(closing=self._state=='closing',check_native=False,check_connection=self._retry_at is None)
                if self.native_gate() is not True:
                    if self._state=='verifying':return
                    self._fail('authority_changed');return
                if self._state!='verifying':return
                if self._retry_at is not None:
                    if self._now()<self._retry_at:return
                    account=self.auth.snapshot()
                    if account.network_state in {'connecting','offline'}:return
                    self._retry_at=None;self._reason=None
                    self._context();self._start_metadata()
                    return
                verifier=self.client._destination_verifier
                if self._preparation is not None:
                    if self._preparation.get('stage')!='ready' or getattr(self.client,'_last_receive_had_event',True):return
                    self._metadata_context()
                    self._preparation=None
                    self._diagnostic_stage='verifier_begin'
                    verifier.begin_group(self._plan['target']['destination_id'],sender=self._plan['target']['sender'])
                    self._verification=verifier.pending
                    return
                if not isinstance(self._verification,dict) or verifier.pending is not self._verification:
                    if self._fact_failure is not None:raise _PreflightDenied(self._fact_failure)
                    raise _PreflightRetry('destination_metadata_changed')
                if verifier.pending.get('stage')!='complete':return
                self._diagnostic_stage='verifier_finish'
                snapshot,version=verifier.finish()
                self._binding.clear_owner_automatic_metadata()
                if not all(self._facts.get(key) is True for key in ('chat','member')):
                    raise _PreflightDenied(self._fact_failure or 'destination_verification_incomplete')
                if snapshot!=self._plan['destination'] or version!=self._plan['target']['destination_version']:
                    raise _PreflightDenied('destination_snapshot_changed')
                self._claim_started=True
                self._diagnostic_stage='claim'
                self._claim=self.queue.claim(self._plan['job_id'],self._plan['job_version'],self._plan,self._profile.profile_id)
                delivery=self._claim['delivery'];target=self._plan['target']
                binding={'delivery_id':delivery['id'],'plan_id':self._plan['proposal']['plan_id'],'policy_hash':self._plan['policy_hash'],
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
                self._profile.begin_automatic(binding)
                self._consumed=True;self._state='sending';self._deadline=self._now()+30
                permit=OwnerAutomaticPermit(_PERMIT_KEY,self._profile,binding,
                    _request(binding['chat_id'],self._plan['text'],binding['request_extra']),self._context)
                self.queue.initiate(self._claim,lambda:self._binding.send_owner_automatic(permit))
            except TimeoutError:self._fail('timed_out')
            except _PreflightRetry as error:self._retry_preflight(str(error))
            except Exception as error:
                self._record_failure(error)
                self._fail('dispatch_unconfirmed' if self._claim_started else str(error) if isinstance(error,_PreflightDenied) else 'verification_failed')

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
                    if self._retry_at is not None:return
                    typ=raw.get('@type','')
                    network=raw.get('state')
                    if typ=='updateConnectionState' and isinstance(network,dict) and network.get('@type') in {'connectionStateConnecting','connectionStateConnectingToProxy','connectionStateUpdating','connectionStateWaitingForNetwork'}:
                        self._retry_preflight('connection_reconnecting');return
                    if self._verification is not None and typ=='updateConnectionState' and isinstance(network,dict) and network.get('@type')=='connectionStateReady':
                        self._metadata_context()
                        self._retry_preflight('destination_metadata_changed');return
                    if self._verification is not None and (typ.startswith('updateChat') or typ in {'updateUser','updateUserFullInfo','updateBasicGroup','updateSupergroup','updateSupergroupFullInfo'}):
                        self._retry_preflight('destination_metadata_changed');return
                    if self._preparation is not None:self._consume_preparation(raw)
                    else:self._native_fact(raw)
                except _PreflightRetry as error:self._retry_preflight(str(error))
                except Exception as error:
                    self._record_failure(error)
                    self._fail(str(error) if isinstance(error,_PreflightDenied) else 'verification_failed')
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
                        if not Outbox(self.queue.db).transition(event.delivery_id,'sending','sending',{
                                'delivery_id':event.delivery_id,'attempt_id':event.attempt_id,'request_extra':event.request_extra,
                                'temporary_message_id':self._temporary,'final_message_id':None,'state':'sending'}):raise ValueError
                    elif event.kind=='succeeded':
                        self._temporary=event.temporary_message_id;self._final=event.final_message_id
                        try:
                            self._profile.complete_automatic_sent(self._marker_binding,self._temporary,self._final,
                                active_client_id=raw['@client_id'])
                            self.queue.complete(self._claim,self._temporary,self._final)
                            self.queue.finish(self._claim)
                            self._profile.finalize_automatic_sent(self._marker_binding)
                            self._events.retire(event.request_extra)
                            self._state='sent';self._reason=None;self._deadline=None
                        except Exception:self._fail('durability_unconfirmed')
                    elif event.kind=='failed' and event.failure_proven and (event.request_rejected or event.failed_content_hash==self._marker_binding['content_hash']):
                        try:
                            self._profile.complete_automatic_failed(self._marker_binding,event.temporary_message_id,
                                event.failed_message_id,event.error_code,event.error_reason,event.retry_after,active_client_id=raw['@client_id'])
                            self.queue.complete_failed(self._claim,event)
                            self.queue.finish(self._claim)
                            self._profile.finalize_automatic_failed(self._marker_binding)
                            self._events.retire(event.request_extra,abandon_retry=True)
                            self._state='failed';self._reason='telegram_rejected';self._deadline=None
                        except Exception:self._fail('durability_unconfirmed')
                    elif event.kind in {'failed','unknown'}:
                        self.queue.note_unknown_provider_error(self._claim,event)
                        self._fail('dispatch_unconfirmed')
            except TimeoutError:self._fail('timed_out')
            except Exception:self._fail('authority_changed')

    def revoke(self):
        with self.session.authority_lock:
            self._preview=None
            if self._state=='sending' and self.transport_preservable():
                return  # No new starts; finish only the exact already-native attempt.
            if self.busy:self._fail('authority_changed')

    def transport_preservable(self):
        """Only the exact in-flight receipt may finish after an intake limit."""
        try:
            if self._state!='sending' or self._claim is None:return False
            self._context()
            current=Outbox(self.queue.db).get(self._claim['delivery']['id'])
            return bool(current and current['state']=='sending'
                and current['attempt_id']==self._claim['delivery']['attempt_id']
                and self._profile.purpose=='automatic_pending'
                and self._profile.automatic_record()==self._marker_binding)
        except Exception:return False
