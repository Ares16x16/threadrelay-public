"""Explicit, ephemeral chat metadata discovery; never history or send authority."""
from collections import OrderedDict
import re
import time
from uuid import uuid4

from .telegram import TelegramBoundaryError, _telegram_id
from threadrelay.storage.outbox import _current_account

BATCH = 100
MAX_LIST = 10000
MAX_TOTAL = 20000


class DestinationListing:
    def __init__(self, client, auth, config, session, *, native_gate, clock=time.monotonic):
        if auth._client is not client or auth._lifecycle_lock is not session.authority_lock:
            raise ValueError('Destination listing requires shared account ownership')
        if client._destination_listing is not None:
            raise ValueError('Destination listing already attached')
        self.client, self.auth, self.config, self.session = client, auth, config, session
        self.native_gate, self.clock = native_gate, clock
        self._reset()
        client._destination_listing = self

    def _reset(self):
        self.state = 'idle'
        self._binding = None
        self._generation = uuid4().hex
        self._items = OrderedDict()
        self._lists = [dict(name=name, limit=0, ended=False, done=False, capped=False) for name in ('main', 'archive')]
        self._list_index = 0
        self._pending = None
        self._queue = []
        self._failure_stage = None
        self._failure_reason = None

    def _fail(self, stage, reason):
        self._pending = None
        self._queue.clear()
        self.state = 'failed'
        self._failure_stage, self._failure_reason = stage, reason

    def _context(self):
        account = self.auth.snapshot()
        if (self.auth.status != 'ready' or account.auth_state != 'ready' or not account.account_id or account.revocation_pending):
            return account, 'login_required'
        if account.network_state != 'online':
            return account, 'connection_required'
        try:
            if not self.client.transport_ready:
                return account, 'connection_required'
        except Exception:
            return account, 'connection_required'
        try:
            native_ready = self.native_gate() is True
        except Exception:
            native_ready = False
        if not native_ready:
            return account, 'native_unavailable'
        return account, None

    def _current(self):
        account, reason = self._context()
        if reason is None and self._binding != (account.account_id, account.epoch):
            reason = 'refresh_required'
        if reason is None:
            try:
                with self.config.db.connection() as con:
                    _current_account(con, {'account_id': account.account_id, 'account_epoch': account.epoch})
            except Exception:
                reason = 'refresh_required'
        return account, reason

    def _send(self, method, stage, **values):
        account, reason = self._current()
        if reason:
            self._reset()
            raise TelegramBoundaryError('Destination browser requires a current connection')
        extra = 'listing:' + stage + ':' + uuid4().hex
        pending = {'extra': extra, 'stage': stage, 'deadline': self.clock() + 30, **values}
        request = {'@type': method, '@extra': extra}
        if stage in ('load', 'chats'):
            current = self._lists[self._list_index]
            request.update(chat_list={'@type': 'chatListMain' if current['name'] == 'main' else 'chatListArchive'},
                           limit=BATCH if stage == 'load' else current['limit'])
        elif stage == 'chat':
            request['chat_id'] = int(values['chat_id'])
        elif stage == 'member':
            request.update(chat_id=int(values['item']['chat_id']), member_id={'@type': 'messageSenderUser', 'user_id': int(account.account_id)})
        self._pending = pending
        try:
            with self.config.db.transaction() as con:
                _current_account(con, {'account_id': account.account_id, 'account_epoch': account.epoch})
                self.client._send(request)
        except Exception:
            self._fail(stage, 'request_unavailable')
            raise TelegramBoundaryError('Destination metadata request unavailable') from None

    def refresh(self, load_more=False):
        if type(load_more) is not bool:
            raise ValueError('Invalid destination refresh')
        with self.session.authority_lock:
            account, reason = self._context()
            if reason:
                self._reset()
                raise TelegramBoundaryError('Connect Telegram before browsing destinations')
            if load_more:
                _, reason = self._current()
                if reason or self.state != 'ready' or self._pending is not None:
                    raise TelegramBoundaryError('Refresh the destination list first')
            else:
                self._reset()
                self._binding = (account.account_id, account.epoch)
            while self._list_index < 2 and self._lists[self._list_index]['done']:
                self._list_index += 1
            if self._list_index >= 2:
                self.state = 'ready'
                return
            current = self._lists[self._list_index]
            current['limit'] = min(MAX_LIST, current['limit'] + BATCH)
            self.state = 'loading'
            if current['ended']:
                self._send('getChats', 'chats')
            else:
                self._send('loadChats', 'load')

    def tick(self):
        with self.session.authority_lock:
            if self._binding is None:
                return
            _, reason = self._current()
            if reason:
                self._reset()
            elif self._pending is not None and self.clock() >= self._pending['deadline']:
                self._fail(self._pending['stage'], 'timeout')

    def _advance(self):
        if self._queue:
            self._send('getChat', 'chat', chat_id=self._queue.pop(0))
        else:
            self._pending = None
            self.state = 'ready'

    def _item(self, raw):
        identifier = _telegram_id(raw.get('id'))
        title, kind = raw.get('title'), raw.get('type')
        if identifier == '0' or not isinstance(title, str) or len(title) > 500 or not isinstance(kind, dict):
            raise ValueError
        native = kind.get('@type')
        sender = raw.get('message_sender_id')
        chosen = None
        if isinstance(sender, dict):
            if sender.get('@type') == 'messageSenderUser' and _telegram_id(sender.get('user_id')) == self._binding[0]:
                chosen = {'kind': 'user', 'id': self._binding[0]}
            elif sender.get('@type') == 'messageSenderChat' and _telegram_id(sender.get('chat_id')) != '0':
                chosen = {'kind': 'chat', 'id': _telegram_id(sender['chat_id'])}
        membership = False
        if native == 'chatTypePrivate':
            user_id = _telegram_id(kind.get('user_id'))
            if int(user_id) <= 0:
                raise ValueError
            target = 'saved_messages' if user_id == self._binding[0] and identifier == self._binding[0] else 'private'
            if sender is None:
                chosen = {'kind': 'user', 'id': self._binding[0]}
            if target == 'saved_messages':
                title = 'Saved Messages'
        elif native == 'chatTypeBasicGroup':
            if int(_telegram_id(kind.get('basic_group_id'))) <= 0:
                raise ValueError
            target = 'group'
            if sender is None:
                chosen = {'kind': 'user', 'id': self._binding[0]}
        elif native == 'chatTypeSupergroup':
            if int(_telegram_id(kind.get('supergroup_id'))) <= 0 or type(kind.get('is_channel')) is not bool:
                raise ValueError
            target = 'channel' if kind['is_channel'] else 'group'
            membership = sender is None and not kind['is_channel']
        elif native == 'chatTypeSecret':
            target = 'secret'
        else:
            target = 'unsupported'
        return {'chat_id': identifier, 'title': title, 'kind': target,
                'can_verify': target in {'saved_messages', 'private', 'group', 'channel'} and chosen is not None,
                'sender': chosen}, membership

    def consume(self, raw):
        with self.session.authority_lock:
            typ = raw.get('@type')
            if typ == 'updateAuthorizationState' and raw.get('authorization_state', {}).get('@type') != 'authorizationStateReady':
                self._reset()
                return False
            if typ == 'updateConnectionState' and raw.get('state', {}).get('@type') != 'connectionStateReady':
                self._reset()
                return False
            pending = self._pending
            if pending is None or raw.get('@extra') != pending['extra']:
                return False
            _, reason = self._current()
            if reason:
                self._reset()
                return True
            if self.clock() >= pending['deadline']:
                self._fail(pending['stage'], 'timeout')
                return True
            try:
                if pending['stage'] == 'load':
                    if typ == 'error' and raw.get('code') == 404:
                        self._lists[self._list_index]['ended'] = True
                    elif typ != 'ok':
                        raise ValueError
                    self._send('getChats', 'chats')
                elif pending['stage'] == 'chats':
                    current = self._lists[self._list_index]
                    values, total = raw.get('chat_ids'), raw.get('total_count')
                    if (typ != 'chats' or not isinstance(values, list) or len(values) > current['limit']
                            or type(total) is not int or total < 0):
                        raise ValueError
                    identifiers = [_telegram_id(value) for value in values]
                    if '0' in identifiers or len(set(identifiers)) != len(identifiers):
                        raise ValueError
                    if len(set(self._items) | set(identifiers)) > MAX_TOTAL:
                        for listing in self._lists:
                            listing.update(done=True, capped=True)
                        self._queue.clear()
                        self._pending = None
                        self.state = 'ready'
                        return True
                    # TDLib documents total_count as approximate. Only an
                    # under-full prefix after loadChats 404 establishes the
                    # loaded list boundary; a full prefix needs another page.
                    covered = len(identifiers) < current['limit']
                    current['done'] = current['ended'] and covered
                    if current['limit'] == MAX_LIST and not current['done']:
                        current.update(done=True, capped=True)
                    self._queue = [value for value in identifiers if value not in self._items]
                    self._advance()
                elif pending['stage'] == 'chat':
                    if typ != 'chat' or _telegram_id(raw.get('id')) != pending['chat_id']:
                        raise ValueError
                    item, membership = self._item(raw)
                    if membership:
                        self._send('getChatMember', 'member', item=item)
                    else:
                        self._items[item['chat_id']] = item
                        self._advance()
                elif pending['stage'] == 'member':
                    member, status = raw.get('member_id'), raw.get('status')
                    if (typ != 'chatMember' or not isinstance(member, dict) or member.get('@type') != 'messageSenderUser'
                            or _telegram_id(member.get('user_id')) != self._binding[0] or not isinstance(status, dict)):
                        raise ValueError
                    item = pending['item']
                    role = status.get('@type')
                    personal = role in {'chatMemberStatusMember', 'chatMemberStatusRestricted'}
                    if role == 'chatMemberStatusCreator':
                        personal = status.get('is_anonymous') is False
                    elif role == 'chatMemberStatusAdministrator':
                        rights = status.get('rights')
                        personal = isinstance(rights, dict) and rights.get('is_anonymous') is False
                    if personal:
                        item = {**item, 'sender': {'kind': 'user', 'id': self._binding[0]}, 'can_verify': True}
                    self._items[item['chat_id']] = item
                    self._advance()
                else:
                    raise ValueError
            except Exception:
                if self.state != 'failed':
                    self._fail(pending['stage'], 'invalid_response')
            return True

    def sender_matches(self, chat_id, sender):
        with self.session.authority_lock:
            _, reason = self._current()
            item = self._items.get(chat_id)
            return reason is None and item is not None and item['can_verify'] and item['sender'] == sender

    def query(self, cursor=None, limit=30, search=''):
        if type(limit) is not int or not 1 <= limit <= 50 or not isinstance(search, str) or len(search) > 200:
            raise ValueError('Invalid destination page')
        if cursor is not None and (not isinstance(cursor, str) or re.fullmatch(r'[a-f0-9]{32}:[0-9]{1,5}', cursor) is None):
            raise ValueError('Invalid destination cursor')
        with self.session.authority_lock:
            account, reason = self._context()
            if self._binding is not None:
                _, current_reason = self._current()
                if current_reason:
                    reason = current_reason
                    self._reset()
            if reason:
                return {'state': 'blocked', 'account_id': account.account_id, 'account_epoch': account.epoch,
                        'items': [], 'next_cursor': None, 'can_load_more': False, 'exhausted': False, 'capped': False, 'blocked_reason': reason,
                        'failure_stage': None, 'failure_reason': None}
            offset = 0
            if cursor is not None:
                generation, position = cursor.split(':')
                if generation != self._generation:
                    raise ValueError('Stale destination cursor')
                offset = int(position)
            needle = search.casefold()
            matches = [item for item in self._items.values() if needle in item['title'].casefold() or needle in item['chat_id']]
            values = matches[offset:offset + limit]
            capped = any(value['capped'] for value in self._lists)
            complete = all(value['done'] for value in self._lists)
            return {'state': self.state, 'account_id': account.account_id, 'account_epoch': account.epoch,
                    'items': [dict(item, sender=None if item['sender'] is None else dict(item['sender'])) for item in values],
                    'next_cursor': self._generation + ':' + str(offset + limit) if offset + limit < len(matches) else None,
                    'can_load_more': self.state == 'ready' and not complete, 'exhausted': complete and not capped,
                    'capped': capped, 'blocked_reason': None,
                    'failure_stage': self._failure_stage if self.state == 'failed' else None,
                    'failure_reason': self._failure_reason if self.state == 'failed' else None}
