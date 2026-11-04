"""Explicit one-message owner test, separate from production delivery evidence.

Only the ordered Telegram owner calls begin/tick. Query never progresses work.
The current connection profile is durably consumed before the sole send. Only
correlated final success allows its distinct sent-profile reuse;
all interrupted attempts remain quarantined. No retry or production gate exists.
"""
from copy import deepcopy
import hashlib
import json
import math
import time
from uuid import uuid4

from .events import AttemptEvents
from .telegram import NativeJsonBinding, NativeUnavailable, TelegramBoundaryError, _telegram_id
from threadrelay.storage.outbox import _current_account

_PERMIT_KEY = object()


def _request(chat_id, text, extra):
    return {'@type':'sendMessage','chat_id':int(chat_id),'topic_id':None,'reply_to':None,
        'options':{'@type':'messageSendOptions','suggested_post_info':None,'disable_notification':False,
            'from_background':False,'protect_content':False,'allow_paid_broadcast':False,'paid_message_star_count':0,
            'update_order_of_installed_sticker_sets':False,'scheduling_state':None,'effect_id':'0','sending_id':0,'only_preview':False},
        'reply_markup':None,'input_message_content':{'@type':'inputMessageText',
            'text':{'@type':'formattedText','text':text,'entities':[]},
            'link_preview_options':{'@type':'linkPreviewOptions','is_disabled':True,'url':'',
                'force_small_media':False,'force_large_media':False,'show_above_text':False},'clear_draft':False},'@extra':extra}


class OwnerTestPermit:
    """Opaque owner-created, exact native request. Never a renderer DTO."""
    def __init__(self, key, profile, binding, request, recheck):
        if key is not _PERMIT_KEY:raise TelegramBoundaryError('Owner test authority unavailable')
        self._profile, self._binding = profile, deepcopy(binding)
        self._encoded = json.dumps(request, ensure_ascii=False, allow_nan=False)
        self._recheck, self._used = recheck, False

    def take_for_native(self, client_id, profile, generation, account_id):
        expected = self._binding
        if (self._used or profile is not self._profile or type(client_id) is not int
                or client_id != expected['client_id'] or type(generation) is not int
                or generation != expected['generation'] or type(account_id) is not int
                or str(account_id) != expected['account_id']):
            raise TelegramBoundaryError('Owner test authority unavailable')
        self._recheck()
        profile.recheck()
        if profile.purpose != 'owner_test_pending' or profile.owner_test_record() != expected:
            raise TelegramBoundaryError('Owner test profile unavailable')
        self._used = True  # Consume before entering native code, including errors.
        return json.loads(self._encoded)


class OwnerTestController:
    def __init__(self, client, auth, session, *, native_gate, monotonic=time.monotonic, on_state=None):
        if auth._client is not client or auth._lifecycle_lock is not session.authority_lock:
            raise ValueError('Owner test requires shared account ownership')
        if getattr(client, '_owner_test', None) is not None:
            raise ValueError('Owner test already attached')
        self.client, self.auth, self.session = client, auth, session
        self.native_gate, self.monotonic = native_gate, monotonic
        self._on_state=on_state;self._last_reported='idle'
        self._state='idle'; self._reason=None; self._binding=None; self._profile=None
        self._last_clock=None; self._deadline=None; self._verification=None
        self._facts={}; self._events=AttemptEvents(); self._consumed=False
        self._profile_transition_started=False
        self._text=None; self._temporary=None; self._final=None
        self._identity=None; self._attempt_id=None
        client._owner_test=self

    def _publish(self):
        value=self.query()
        if value['state'] != self._last_reported:
            self._last_reported=value['state']
            if self._on_state is not None:
                try:self._on_state(value)
                except Exception:pass  # Monitoring cannot change outcome or consume a permit.

    @property
    def busy(self):
        return self._state in {'verifying','sending','closing'}

    def query(self):
        """Safe observation only; no tick, lookup, profile opening or send."""
        with self.session.authority_lock:
            if self._state=='idle':
                try:
                    profile=self.client._profile_guard
                    saved=profile.owner_test_record() if profile is not None and profile.purpose=='owner_test_sent' else None
                    if saved is not None:
                        return {'state':'sent','chat_id':saved['chat_id'],'account_id':saved['account_id'],
                            'account_epoch':saved['account_epoch'],'text':self._test_text(saved['attempt_id']),
                            'request_extra':saved['request_extra'],'temporary_message_id':saved['temporary_message_id'],
                            'final_message_id':saved['final_message_id'],'reason':None}
                except (AttributeError, NativeUnavailable, TelegramBoundaryError):pass
            identity = self._identity or (None,None,None)
            return {'state':self._state,'chat_id':identity[2],'account_id':identity[0],'account_epoch':identity[1],
                'text':self._text,'request_extra':None if self._attempt_id is None else 'owner-test:'+self._attempt_id,
                'temporary_message_id':self._temporary,'final_message_id':self._final,'reason':self._reason}

    @staticmethod
    def _test_text(attempt_id):
        return f'ThreadRelay test: manual Telegram connection check. No collected post content. [{attempt_id}]'

    def _now(self):
        value=self.monotonic()
        if (type(value) not in (int,float) or not math.isfinite(value)
                or self._last_clock is not None and value < self._last_clock):
            raise TelegramBoundaryError('Owner test clock unavailable')
        self._last_clock=value
        return value

    def _context(self, *, closing=False, check_native=True):
        account=self.auth.snapshot()
        if (check_native and self.native_gate() is not True or self._identity is None
                or (account.account_id,account.epoch) != self._identity[:2] or account.revocation_pending
                or (not closing and (self.auth.status!='ready' or account.auth_state!='ready'
                    or account.network_state!='online' or not self.client.transport_ready))
                or self.client._binding is not self._binding or self.client._profile_guard is not self._profile
                or self.client._client_id != self._binding._owned_client):
            raise TelegramBoundaryError('Owner test authority unavailable')
        from threadrelay.storage.saved_destinations import SavedDestinations
        with self.auth._accounts.db.connection() as con:
            SavedDestinations.require(con,{'account_id':self._identity[0],'chat_id':self._identity[2],
                'title':'Test','sender':{'kind':'user','id':self._identity[0]}},self._identity[1])
        self._profile.recheck()
        if self._deadline is not None and self._now() >= self._deadline:
            raise TelegramBoundaryError('Owner test deadline expired')

    def begin(self, *, chat_id, account_id, account_epoch):
        with self.session.authority_lock:
            if getattr(getattr(self.client,'_owner_history',None),'busy',False):raise TelegramBoundaryError('Manual History owns verification')
            if getattr(getattr(self.client,'_owner_automatic',None),'busy',False):raise TelegramBoundaryError('Automatic delivery owns verification')
            if self._state not in {'idle','failed'} or self._consumed or self._profile_transition_started:
                raise TelegramBoundaryError('Owner test already active or consumed')
            try:
                if (_telegram_id(int(chat_id)) != chat_id or int(chat_id)==0
                        or _telegram_id(int(account_id)) != account_id or int(account_id)<=0
                        or type(account_epoch) is not int or account_epoch < 0):raise ValueError
                binding=self.client._binding; profile=self.client._profile_guard
                verifier=self.client._destination_verifier
                if (type(binding) is not NativeJsonBinding or not binding.connection_only
                        or profile is None or profile.purpose!='connection_only' or binding._profile is not profile
                        or type(binding.generation) is not int or binding.generation < 1
                        or verifier is None or verifier.pending is not None or not self.client.local_attempts_quiescent()):
                    raise ValueError
                self._identity=(account_id,account_epoch,chat_id);self._binding=binding;self._profile=profile
                self._deadline=self._now()+30;self._context()
                with self.auth._accounts.db.connection() as con:
                    _current_account(con,{'account_id':account_id,'account_epoch':account_epoch})
                self._facts={};self._reason=None;self._temporary=None;self._final=None
                self._attempt_id=uuid4().hex
                self._text=self._test_text(self._attempt_id)
                verifier.begin_group(chat_id,sender={'kind':'user','id':account_id})
                self._verification=verifier.pending;self._state='verifying'
                self._publish()
            except Exception:
                self._state='failed';self._reason='test_unavailable'
                self._publish()
                raise TelegramBoundaryError('Owner test unavailable') from None

    def _native_fact(self, raw):
        pending=self._verification
        if not isinstance(pending,dict) or raw.get('@extra') != pending.get('extra'):return
        stage=pending.get('stage');account,_,chat=self._identity
        if stage=='chat':
            kind=raw.get('type');sender=raw.get('message_sender_id')
            valid=(raw.get('@type')=='chat' and type(raw.get('id')) is int and str(raw['id'])==chat
                and raw.get('title')=='Test' and isinstance(kind,dict)
                and kind.get('@type') in {'chatTypeBasicGroup','chatTypeSupergroup'}
                and (kind['@type']=='chatTypeBasicGroup' or kind.get('is_channel') is False)
                and (sender is None or sender=={'@type':'messageSenderUser','user_id':int(account)}))
            identifier=None if not isinstance(kind,dict) else kind.get('basic_group_id' if kind.get('@type')=='chatTypeBasicGroup' else 'supergroup_id')
            self._facts.update(chat=valid,kind=None if not isinstance(kind,dict) else kind.get('@type'),group_id=identifier)
        elif stage=='group':
            basic=self._facts.get('kind')=='chatTypeBasicGroup'
            self._facts['group']=(self._facts.get('chat') is True and type(raw.get('id')) is int
                and raw['id']==self._facts.get('group_id') and raw.get('@type')==('basicGroup' if basic else 'supergroup')
                and (raw.get('is_active') is True if basic else raw.get('is_channel') is False
                    and type(raw.get('paid_message_star_count')) is int and raw['paid_message_star_count']==0
                    and raw.get('restriction_info') is None))
        elif stage=='member':
            status=raw.get('status');role=status.get('@type') if isinstance(status,dict) else None
            personal=(role in {'chatMemberStatusMember','chatMemberStatusRestricted'}
                or role=='chatMemberStatusCreator' and status.get('is_anonymous') is False
                or role=='chatMemberStatusAdministrator' and isinstance(status.get('rights'),dict) and status['rights'].get('is_anonymous') is False)
            self._facts['member']=(raw.get('@type')=='chatMember'
                and raw.get('member_id')=={'@type':'messageSenderUser','user_id':int(account)} and personal)

    def _fail(self, reason):
        self._state='unknown' if self._consumed else 'failed';self._reason=reason
        verifier=self.client._destination_verifier
        if verifier is not None and verifier.pending is self._verification:verifier.pending=None
        if self._consumed:
            self._events.expire('owner-test:'+self._attempt_id)
        self._publish()

    def tick(self):
        with self.session.authority_lock:
            if not self.busy:return
            try:
                if self._state=='verifying':
                    # A busy private capability reader grants no dispatch. Keep
                    # the bounded explicit intent queued while account/profile
                    # and Stop/revocation checks remain mandatory.
                    self._context(check_native=False)
                    if self.native_gate() is not True:return
                self._context(closing=self._state=='closing')
                if self._state!='verifying':return
                verifier=self.client._destination_verifier
                if verifier.pending is not self._verification:raise ValueError
                if verifier.pending.get('stage')!='complete':return
                snapshot,version=verifier.finish()
                if (any(self._facts.get(key) is not True for key in ('chat','group','member'))
                        or snapshot.get('title')!='Test' or snapshot.get('chat_id')!=self._identity[2]
                        or snapshot.get('account_id')!=self._identity[0]
                        or snapshot.get('sender')!={'kind':'user','id':self._identity[0]}
                        or snapshot.get('permissions',{}).get('can_send') is not True or snapshot.get('paid_required') is not False):
                    raise ValueError
                binding={'attempt_id':self._attempt_id,'request_extra':'owner-test:'+self._attempt_id,
                    'account_id':self._identity[0],'account_epoch':self._identity[1],'chat_id':self._identity[2],
                    'sender_id':self._identity[0],'destination_version':version,'profile_id':self._profile.profile_id,
                    'content_hash':hashlib.sha256(self._text.encode('utf-8')).hexdigest(),
                    'generation':self._binding.generation,'client_id':self.client._client_id}
                self._events.register(delivery_id='owner-test',attempt_id=self._attempt_id,request_extra=binding['request_extra'],
                    account_id=binding['account_id'],epoch=binding['account_epoch'],chat_id=binding['chat_id'],
                    sender={'kind':'user','id':binding['sender_id']},destination_version=version,content_hash=binding['content_hash'])
                with self.auth._accounts.db.transaction() as con:
                    _current_account(con,{'account_id':binding['account_id'],'account_epoch':binding['account_epoch']})
                    self._context()
                    if not self.client.local_attempts_quiescent():raise ValueError
                    # Uncertain markers deny further sends without ending login.
                    self._profile_transition_started=True
                    self._profile.begin_owner_test(binding)  # fsync before any native call.
                    self._consumed=True;self._state='sending';self._deadline=self._now()+30
                    permit=OwnerTestPermit(_PERMIT_KEY,self._profile,binding,
                        _request(binding['chat_id'],self._text,binding['request_extra']),self._context)
                    self._binding.send_owner_test(permit)
                    self._publish()
            except Exception:
                self._fail('test_unconfirmed' if self._consumed else 'target_unavailable')

    def consume(self, raw):
        with self.session.authority_lock:
            if (not isinstance(raw,dict) or self._binding is None or type(raw.get('@client_id')) is not int
                    or raw['@client_id']!=self._binding._owned_client):return
            if (self._consumed and self._state=='unknown' and raw.get('@type')=='updateAuthorizationState'
                    and isinstance(raw.get('authorization_state'),dict)
                    and raw['authorization_state'].get('@type')=='authorizationStateClosed'):
                self._profile.close()  # Release signer/handles without unquarantining.
                return
            if self._state=='verifying':
                self._native_fact(raw)
                return
            if self._state not in {'sending','closing'}:return
            try:
                self._context(closing=self._state=='closing')
                if raw.get('@type')=='updateAuthorizationState':
                    state=raw.get('authorization_state',{}).get('@type')
                    if state=='authorizationStateClosed':
                        if self._state=='closing' and self._final is not None:
                            self._profile.complete_owner_test_sent(self._profile.owner_test_record(),self._temporary,self._final,
                                closed_client_id=raw['@client_id'])
                            self._state='sent';self._reason=None
                        else:self._fail('closed_before_confirmation')
                        self._profile.close()
                        self._publish()
                    elif state!='authorizationStateReady' and self._state!='closing':
                        self._fail('authorization_changed')
                    return
                if self._state!='sending':return
                self._events.consume(raw)
                while self._events.events:
                    event=self._events.events.popleft()
                    if event.kind=='initiated':self._temporary=event.temporary_message_id
                    elif event.kind=='succeeded':
                        self._temporary=event.temporary_message_id;self._final=event.final_message_id
                        self._profile.complete_owner_test_sent(self._profile.owner_test_record(),self._temporary,self._final,
                            active_client_id=raw['@client_id'])
                        self._events.retire(event.request_extra)
                        self._state='sent';self._reason=None;self._deadline=None;self._publish()
                    elif event.kind in {'failed','unknown'}:self._fail('send_unconfirmed')
            except Exception:self._fail('test_unconfirmed')

    def revoke(self):
        with self.session.authority_lock:
            if self.busy:self._fail('owner_test_revoked')
