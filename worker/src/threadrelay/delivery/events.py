"""Bounded, content-free correlation for this client's own send attempts."""
from dataclasses import dataclass
from collections import deque
import math
import hashlib
import re
from uuid import uuid4


@dataclass(frozen=True)
class DeliveryEvent:
    kind: str
    delivery_id: str
    attempt_id: str
    request_extra: str
    account_id: str
    epoch: int
    chat_id: str
    sender_kind: str
    sender_id: str
    destination_version: int
    content_hash: str
    temporary_message_id: str | None = None
    final_message_id: str | None = None
    failed_message_id: str | None = None
    can_retry: bool = False
    retry_after: float = 0
    error_code: int | None = None
    error_reason: str | None = None
    failed_content_hash: str | None = None
    failure_proven: bool = False
    request_rejected: bool = False


def failure_details(error, sending_state):
    """Project provider failures to bounded codes; never persist raw prose."""
    code = error.get('code') if isinstance(error, dict) and error.get('@type') == 'error' else None
    if type(code) is not int or not 0 <= code <= 999:
        return None, None, 0.0
    message = error.get('message')
    match = re.fullmatch(r'(FLOOD(?:_PREMIUM)?_WAIT|SLOWMODE_WAIT)_(\d{1,8})', message) if isinstance(message, str) else None
    reason = match.group(1) if match else 'TELEGRAM_ERROR'
    delay = float(match.group(2)) if match else 0.0
    if isinstance(sending_state, dict):
        retry = sending_state.get('retry_after')
        if type(retry) in (int, float) and math.isfinite(retry) and 0 <= retry <= 31536000:
            delay = max(delay, float(retry))
    if code in {420, 429} and reason == 'TELEGRAM_ERROR' and delay > 0:
        reason = 'FLOOD_WAIT'
    return code, reason, delay


class AttemptEvents:
    def __init__(self):
        self.attempts = {}
        self.pending = deque(maxlen=128)
        self.events = deque(maxlen=512)
        self.local_queries = {}
        self.reconcile_queries = {}
        self.reservations = set()
        # Retired request/temporary IDs suppress delayed duplicates without
        # retaining raw messages or keeping completed attempts live forever.
        self.tombstones = deque(maxlen=256)

    def reserve(self):
        if len(self.attempts) + len(self.reservations) >= 128:
            return None
        token = uuid4().hex
        self.reservations.add(token)
        return token

    def cancel_reservation(self, token):
        self.reservations.discard(token)

    def retire(self, request_extra, *, abandon_retry=False):
        attempt = self.attempts.get(request_extra)
        if attempt is None or not attempt['terminal']:
            return False
        if attempt.get('retryable') and not abandon_retry:
            return False
        if abandon_retry:
            attempt['retryable'] = False
        attempt['retire_requested'] = True
        if request_extra in self.local_queries.values() or request_extra in self.reconcile_queries.values():
            return False
        self.attempts.pop(request_extra)
        self.tombstones.append((request_extra, attempt['chat_id'], attempt['temporary']))
        return True

    def register(self, *, reservation=None, **binding):
        try:
            expected = {'delivery_id','attempt_id','request_extra','account_id','epoch','chat_id','sender','destination_version','content_hash'}
            if set(binding) != expected: raise ValueError
            for key in ('delivery_id','attempt_id','request_extra','content_hash'):
                if not isinstance(binding[key], str) or not 1 <= len(binding[key]) <= 128: raise ValueError
            for key in ('account_id','chat_id'):
                value = binding[key]
                if not isinstance(value, str) or self._id(int(value)) != value or int(value) == 0: raise ValueError
            sender = binding['sender']
            if not isinstance(sender, dict) or set(sender) != {'kind','id'} or sender['kind'] not in {'user','chat'}: raise ValueError
            if not isinstance(sender['id'], str) or self._id(int(sender['id'])) != sender['id'] or int(sender['id']) == 0: raise ValueError
            if sender['kind'] == 'user' and sender['id'] != binding['account_id']: raise ValueError
            if int(binding['account_id']) < 1 or type(binding['epoch']) is not int or binding['epoch'] < 0: raise ValueError
            if type(binding['destination_version']) is not int or binding['destination_version'] < 1: raise ValueError
            if reservation is not None:
                if reservation not in self.reservations: raise ValueError
            elif len(self.attempts) + len(self.reservations) >= 128: raise ValueError
            if binding['request_extra'] in self.attempts or any(t[0] == binding['request_extra'] for t in self.tombstones): raise ValueError
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ValueError('Attempt correlation unavailable') from None
        if reservation is not None: self.reservations.remove(reservation)
        self.attempts[binding['request_extra']] = dict(binding, sender=sender.copy(), temporary=None, terminal=False)

    @staticmethod
    def _id(value):
        return str(value) if type(value) is int and value != 0 and -(2**53-1) <= value <= 2**53-1 else None

    @classmethod
    def _sender(cls, sender):
        if not isinstance(sender, dict): return None
        typ = sender.get('@type')
        if typ == 'messageSenderUser': return ('user', cls._id(sender.get('user_id')))
        if typ == 'messageSenderChat': return ('chat', cls._id(sender.get('chat_id')))
        return None

    def _event(self, attempt, kind, **fields):
        self.events.append(DeliveryEvent(kind, attempt['delivery_id'], attempt['attempt_id'],
            attempt['request_extra'], attempt['account_id'], attempt['epoch'], attempt['chat_id'],
            attempt['sender']['kind'], attempt['sender']['id'], attempt['destination_version'],
            attempt['content_hash'], **fields))

    def reconcile(self, request_extra, query_extra):
        attempt = self.attempts.get(request_extra)
        if (attempt is None or attempt['terminal'] or attempt['temporary'] is None
            or request_extra in self.reconcile_queries.values() or len(self.reconcile_queries) >= 128):
            raise ValueError('Own attempt unavailable')
        self.reconcile_queries[query_extra] = request_extra
        return attempt

    def verify_failed(self, request_extra, failed_id, query_extra):
        attempt = self.attempts.get(request_extra)
        if (attempt is None or attempt.get('failed_id') != failed_id or not attempt.get('retryable')
            or query_extra in self.local_queries or request_extra in self.local_queries.values()
            or len(self.local_queries) >= 128):
            raise ValueError('Failed message unavailable')
        self.local_queries[query_extra] = request_extra
        return attempt

    @staticmethod
    def _retry_state(state):
        if not isinstance(state, dict) or state.get('@type') != 'messageSendingStateFailed': return None
        delay = state.get('retry_after')
        if (state.get('can_retry') is True and type(delay) in (int,float) and math.isfinite(delay) and delay >= 0
            and all(state.get(k) is False for k in ('need_another_sender','need_another_reply_quote','need_drop_reply'))
            and type(state.get('required_paid_message_star_count')) is int and state['required_paid_message_star_count'] == 0):
            return float(delay)
        return None

    def expire(self, request_extra):
        attempt = self.attempts.get(request_extra)
        if attempt is not None and not attempt['terminal']:
            attempt['terminal'] = True
            self._event(attempt, 'unknown', temporary_message_id=attempt['temporary'])

    def consume(self, raw):
        typ = raw.get('@type')
        extra = raw.get('@extra')
        if extra in self.reconcile_queries:
            # Native cache availability (including a 404) cannot establish the
            # final outcome or authorize a new send in the all-false profile.
            original = self.reconcile_queries.pop(extra)
            attempt = self.attempts[original]
            if attempt.get('retire_requested'):
                self.retire(original, abandon_retry=True)
            else:
                self.expire(original)
            return True
        if extra in self.local_queries:
            original = self.local_queries.pop(extra)
            attempt = self.attempts[original]
            if attempt.get('retire_requested'):
                self.retire(original, abandon_retry=True)
                return True
            delay = self._retry_state(raw.get('sending_state'))
            valid = (typ == 'message' and self._id(raw.get('id')) == attempt.get('failed_id')
                and self._id(raw.get('chat_id')) == attempt['chat_id']
                and self._sender(raw.get('sender_id')) == (attempt['sender']['kind'], attempt['sender']['id'])
                and type(raw.get('paid_message_star_count')) is int and raw['paid_message_star_count'] == 0
                and delay is not None and attempt.get('retryable'))
            if valid:
                self._event(attempt, 'retry_verified', temporary_message_id=attempt['temporary'],
                    failed_message_id=attempt['failed_id'], can_retry=True, retry_after=delay)
            else:
                attempt['retryable'] = False
                self._event(attempt, 'retry_invalidated', temporary_message_id=attempt['temporary'])
            return True
        if typ == 'error' and raw.get('@extra') in self.attempts:
            attempt=self.attempts[raw['@extra']]
            if not attempt['terminal']:
                attempt['terminal']=True
                code,reason,wait=failure_details(raw,None)
                rejected=(attempt['temporary'] is None and code in {400,403,420,429}
                    and not any(candidate['chat']==attempt['chat_id'] for candidate in self.pending))
                self._event(attempt,'failed' if rejected else 'unknown',temporary_message_id=attempt['temporary'],
                    error_code=code,error_reason=reason,retry_after=wait,failure_proven=rejected,request_rejected=rejected)
            return True
        if typ == 'messages' and extra in self.attempts:
            messages = raw.get('messages')
            if not isinstance(messages, list) or len(messages) != 1 or not isinstance(messages[0], dict):
                self.expire(extra)
                return True
            message = messages[0]
            # Never retain the raw object: recursively project only IDs/sender.
            return self.consume({'@type':'message', '@extra':extra, 'id':message.get('id'),
                                 'chat_id':message.get('chat_id'), 'sender_id':message.get('sender_id')})
        if typ == 'message' and raw.get('@extra') in self.attempts:
            attempt = self.attempts[raw['@extra']]
            mid = self._id(raw.get('id'))
            if (attempt['terminal'] or mid is None or self._id(raw.get('chat_id')) != attempt['chat_id'] or
                self._sender(raw.get('sender_id')) != (attempt['sender']['kind'], attempt['sender']['id'])):
                return True
            if attempt['temporary'] is not None: return True
            attempt['temporary'] = mid
            self._event(attempt, 'initiated', temporary_message_id=mid)
            for candidate in list(self.pending):
                if (candidate['old'] == mid and candidate['chat'] == attempt['chat_id']
                    and (candidate['kind'] == 'unknown' or candidate['sender'] == (attempt['sender']['kind'], attempt['sender']['id']))):
                    self._terminal(attempt, candidate)
                    self.pending.remove(candidate)
                    break
            return True
        if typ in {'updateMessageSendSucceeded', 'updateMessageSendFailed'}:
            message = raw.get('message')
            if not isinstance(message, dict): return True
            candidate = {'old': self._id(raw.get('old_message_id')), 'chat': self._id(message.get('chat_id')),
                'sender': self._sender(message.get('sender_id')), 'id': self._id(message.get('id')),
                'kind': 'succeeded' if typ.endswith('Succeeded') else 'failed'}
            if any(candidate[k] is None for k in ('old', 'chat', 'sender', 'id')): return True
            if any(t[1] == candidate['chat'] and t[2] == candidate['old'] for t in self.tombstones): return True
            if candidate['kind'] == 'failed':
                state = message.get('sending_state')
                code, reason, wait = failure_details(raw.get('error') or (state.get('error') if isinstance(state, dict) else None), state)
                content = message.get('content')
                text = content.get('text', {}).get('text') if isinstance(content, dict) and content.get('@type') == 'messageText' and isinstance(content.get('text'), dict) else None
                candidate.update(error_code=code, error_reason=reason, failure_wait=wait,
                    failed_content_hash=hashlib.sha256(text.encode('utf-8')).hexdigest() if isinstance(text, str) else None,
                    failure_proven=bool(code is not None and isinstance(state, dict) and state.get('@type') == 'messageSendingStateFailed' and message.get('is_outgoing') is True and candidate['id']!=candidate['old']))
                delay = self._retry_state(state)
                candidate['can_retry'] = delay is not None
                candidate['retry_after'] = delay if delay is not None else 0
            possible = [a for a in self.attempts.values() if not a['terminal'] and a['chat_id'] == candidate['chat']
                and (a['sender']['kind'], a['sender']['id']) == candidate['sender']]
            for attempt in possible:
                if attempt['temporary'] == candidate['old']:
                    self._terminal(attempt, candidate)
                    return True
            if any(a['temporary'] is None for a in possible): self.pending.append(candidate)
            return True
        if typ == 'updateDeleteMessages':
            chat, ids = self._id(raw.get('chat_id')), raw.get('message_ids')
            if not isinstance(ids, list): return True
            ids = {self._id(i) for i in ids[:1024]}
            ids.difference_update(t[2] for t in self.tombstones if t[1] == chat)
            if chat is not None and any(not a['terminal'] and a['chat_id'] == chat and a['temporary'] is None for a in self.attempts.values()):
                for mid in ids:
                    if mid is not None:
                        self.pending.append({'old':mid, 'chat':chat, 'sender':None, 'id':None, 'kind':'unknown'})
            for attempt in self.attempts.values():
                if attempt.get('retryable') and attempt['chat_id'] == chat and attempt.get('failed_id') in ids:
                    attempt['retryable'] = False
                    self._event(attempt, 'retry_invalidated', temporary_message_id=attempt['temporary'])
                if not attempt['terminal'] and attempt['chat_id'] == chat and attempt['temporary'] in ids and attempt['temporary'] is not None:
                    attempt['terminal'] = True
                    self._event(attempt, 'unknown', temporary_message_id=attempt['temporary'])
            return True
        return False

    def _terminal(self, attempt, candidate):
        if attempt['terminal']: return
        attempt['terminal'] = True
        fields = {'temporary_message_id': candidate['old']}
        if candidate['kind'] == 'succeeded': fields['final_message_id'] = candidate['id']
        elif candidate['kind'] == 'failed':
            attempt['failed_id'], attempt['retryable'] = candidate['id'], candidate['can_retry']
            fields.update(failed_message_id=candidate['id'], can_retry=candidate['can_retry'], retry_after=max(candidate['retry_after'],candidate.get('failure_wait',0)),
                error_code=candidate.get('error_code'),error_reason=candidate.get('error_reason'),
                failed_content_hash=candidate.get('failed_content_hash'),failure_proven=candidate.get('failure_proven',False))
        self._event(attempt, candidate['kind'], **fields)
