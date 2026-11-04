"""Explicit, bounded target verification; no unsolicited content is retained.

Unsupported/ambiguous metadata fails closed; no server response itself is
a durable authority. Only explicit known-chat setup initiates lookups.
"""
from uuid import uuid4
from copy import deepcopy
from .telegram import TelegramBoundaryError, _telegram_id
from threadrelay.storage.outbox import _current_account


class DestinationVerifier:
    def __init__(self, client, auth, config, session):
        if auth._client is not client or auth._lifecycle_lock is not session.authority_lock:
            raise ValueError('Destination authority must share the live client and lifecycle lock')
        if client._destination_verifier is not None:
            raise ValueError('Destination verifier already attached')
        self.client, self.auth, self.config, self.session = client, auth, config, session
        self.pending = None
        self.evidence = None
        client._destination_verifier = self

    def _setup_send(self, request, pending):
        try:
            account = self.auth.snapshot()
            if (self.auth.status != 'ready' or account.auth_state != 'ready' or account.network_state != 'online'
                or account.account_id != pending['account_id'] or account.epoch != pending['epoch']
                or account.revocation_pending or not self.client.transport_ready):
                raise ValueError
            with self.config.db.transaction() as con:
                _current_account(con, {'account_id':pending['account_id'], 'account_epoch':pending['epoch']})
                self.client._send(request)
        except Exception:
            self.pending = None
            raise TelegramBoundaryError('Destination setup unavailable') from None

    def invalidate(self):
        self.evidence = None
        self.pending = None

    def select_sender(self, chat_id, sender):
        """Explicit user choice; this changes TDLib's selected chat sender."""
        with self.session.authority_lock:
            account = self.auth.snapshot()
            if (self.pending is not None or self.auth.status != 'ready' or account.auth_state != 'ready' or account.network_state != 'online'
                or account.revocation_pending or not self.client.transport_ready):
                raise TelegramBoundaryError('Sender selection unavailable')
            try:
                if _telegram_id(int(chat_id)) != chat_id or int(chat_id) == 0: raise ValueError
                if not isinstance(sender, dict) or set(sender) != {'kind','id'} or sender['kind'] not in {'user','chat'}: raise ValueError
                if _telegram_id(int(sender['id'])) != sender['id'] or int(sender['id']) == 0: raise ValueError
                if sender['kind'] == 'user' and sender['id'] != account.account_id: raise ValueError
            except (TypeError, ValueError, KeyError, TelegramBoundaryError):
                raise TelegramBoundaryError('Invalid sender selection') from None
            self.evidence = None
            extra = 'setup:select:available:' + uuid4().hex
            self.pending = {'extra':extra, 'account_id':account.account_id, 'epoch':account.epoch,
                'chat_id':chat_id, 'stage':'select_available', 'sender':sender.copy()}
            self._setup_send({'@type':'getChatAvailableMessageSenders','chat_id':int(chat_id),'@extra':extra}, self.pending)

    def finish_selection(self):
        with self.session.authority_lock:
            p, account = self.pending, self.auth.snapshot()
            if (p is None or p['stage'] != 'select_complete' or account.account_id != p['account_id']
                or account.epoch != p['epoch'] or self.auth.status != 'ready' or account.auth_state != 'ready' or account.network_state != 'online'
                or account.revocation_pending or not self.client.transport_ready):
                self.pending = None
                raise TelegramBoundaryError('Sender selection unavailable')
            try:
                with self.config.db.transaction() as con:
                    _current_account(con, {'account_id':p['account_id'], 'account_epoch':p['epoch']})
            except Exception:
                self.pending = None
                raise TelegramBoundaryError('Sender selection unavailable') from None
            self.pending = None
            return p['sender'].copy()

    @staticmethod
    def _native_sender(sender):
        return ({'@type':'messageSenderUser','user_id':int(sender['id'])} if sender['kind'] == 'user'
                else {'@type':'messageSenderChat','chat_id':int(sender['id'])})

    def begin_private(self, chat_id):
        with self.session.authority_lock:
            # Delivery refresh derives the same own-chat semantics after its
            # ephemeral receipt is invalidated or the sender is recreated.
            # Only exact current self identity can enter this path; the saved
            # setup rechecks readiness and the durable account before sending.
            if chat_id == self.auth.snapshot().account_id:
                return self.begin_saved_messages()
            return self._begin(chat_id, group=False, sender=None, sender_selected=False)

    def begin_saved_messages(self):
        """Prepare only the verified personal user's own chat, without sending.

        getChat is offline and a fresh isolated TDLib profile need not know its
        own chat yet. No renderer identity, forced metadata or chat enumeration
        participates in this narrowly scoped preparation.
        """
        with self.session.authority_lock:
            account = self.auth.snapshot()
            if (self.pending is not None or self.auth.status != 'ready'
                or account.auth_state != 'ready' or account.network_state != 'online'
                or account.revocation_pending or not self.client.transport_ready):
                raise TelegramBoundaryError('Destination setup unavailable')
            try:
                user_id = int(account.account_id)
                if user_id <= 0 or _telegram_id(user_id) != account.account_id:
                    raise ValueError
            except (TypeError, ValueError, TelegramBoundaryError):
                raise TelegramBoundaryError('Destination setup unavailable') from None
            self.evidence = None
            extra = 'setup:saved:' + uuid4().hex
            self.pending = {'extra':extra, 'account_id':account.account_id, 'epoch':account.epoch,
                'chat_id':account.account_id, 'stage':'chat', 'group':False, 'sender':None,
                'sender_selected':False, 'saved_messages':True}
            self._setup_send({'@type':'createPrivateChat', 'user_id':user_id,
                              'force':False, '@extra':extra}, self.pending)

    def begin_group(self, chat_id, *, sender, sender_selected=False):
        return self._begin(chat_id, group=True, sender=sender, sender_selected=sender_selected)

    def _begin(self, chat_id, *, group, sender, sender_selected):
        with self.session.authority_lock:
            account = self.auth.snapshot()
            if (self.pending is not None or self.auth.status != 'ready' or account.auth_state != 'ready' or account.network_state != 'online'
                or account.revocation_pending or not self.client.transport_ready):
                raise TelegramBoundaryError('Destination setup unavailable')
            try:
                number = int(chat_id)
                if _telegram_id(number) != chat_id: raise ValueError
            except (TypeError, ValueError, TelegramBoundaryError):
                raise TelegramBoundaryError('Invalid destination') from None
            if group:
                if not isinstance(sender, dict) or set(sender) != {'kind','id'} or sender['kind'] not in {'user','chat'}:
                    raise TelegramBoundaryError('Invalid sender')
                try:
                    if _telegram_id(int(sender['id'])) != sender['id']: raise ValueError
                except (ValueError, TypeError, TelegramBoundaryError):
                    raise TelegramBoundaryError('Invalid sender') from None
                if sender['kind'] == 'user' and sender['id'] != account.account_id:
                    raise TelegramBoundaryError('Invalid sender')
                if sender['kind'] == 'chat' and sender_selected is not True:
                    raise TelegramBoundaryError('Explicit sender selection required')
            self.evidence = None
            extra = 'setup:chat:' + uuid4().hex
            self.pending = {'extra':extra, 'account_id':account.account_id, 'epoch':account.epoch, 'chat_id':chat_id, 'stage':'chat', 'group':group, 'sender':deepcopy(sender), 'sender_selected':sender_selected}
            self._setup_send({'@type':'getChat','chat_id':number,'@extra':extra}, self.pending)

    def consume(self, raw):
        typ = raw.get('@type', '')
        # Any target/account metadata change invalidates previously verified
        # evidence. Conservative global invalidation avoids guessing ownership.
        if typ.startswith('updateChat') or typ in {'updateConnectionState','updateAuthorizationState','updateUser','updateUserFullInfo','updateBasicGroup','updateSupergroup','updateSupergroupFullInfo'}:
            self.evidence = None
            p = self.pending
            expected_update = (p is not None and p['stage'] == 'select_set' and typ == 'updateChatMessageSender'
                and raw.get('chat_id') == int(p['chat_id']) and raw.get('message_sender_id') == self._native_sender(p['sender']))
            if not expected_update:
                self.pending = None
            return False
        p = self.pending
        if p is None or raw.get('@extra') != p['extra']:
            return False
        if typ == 'error':
            self.pending = None
            return True
        if p['stage'] == 'select_available':
            options = raw.get('senders')
            wanted = self._native_sender(p['sender'])
            valid = (typ == 'chatMessageSenders' and isinstance(options, list) and len(options) <= 100
                and any(isinstance(item, dict) and item.get('needs_premium') is False and item.get('sender') == wanted for item in options))
            if not valid:
                self.pending = None
                return True
            p['stage'], p['extra'] = 'select_set', 'setup:select:set:' + uuid4().hex
            self._setup_send({'@type':'setChatMessageSender','chat_id':int(p['chat_id']), 'message_sender_id':wanted, '@extra':p['extra']}, p)
            return True
        if p['stage'] == 'select_set':
            if typ != 'ok':
                self.pending = None
                return True
            p['stage'], p['extra'] = 'select_read', 'setup:select:read:' + uuid4().hex
            self._setup_send({'@type':'getChat','chat_id':int(p['chat_id']),'@extra':p['extra']}, p)
            return True
        if p['stage'] == 'select_read':
            if typ != 'chat' or raw.get('id') != int(p['chat_id']) or raw.get('message_sender_id') != self._native_sender(p['sender']):
                self.pending = None
            else:
                p['stage'] = 'select_complete'
            return True
        if p['stage'] == 'chat':
            kind, sender = raw.get('type'), raw.get('message_sender_id')
            try:
                saved = p.get('saved_messages') is True
                valid = (typ == 'chat' and _telegram_id(raw.get('id')) == p['chat_id'] and isinstance(kind, dict)
                    and isinstance(raw.get('title'), str) and len(raw['title']) <= 500)
                selected = p['sender'] if p['group'] else {'kind':'user','id':p['account_id']}
                native_kind = 'messageSenderUser' if selected['kind'] == 'user' else 'messageSenderChat'
                field = 'user_id' if selected['kind'] == 'user' else 'chat_id'
                # TDLib omits null custom-object fields from JSON. An absent
                # or null sender means the sender cannot be changed.
                # Private/basic-group chats use the personal identity; a null
                # supergroup sender additionally needs non-anonymous current
                # membership below. Never infer a channel identity here.
                implicit_personal = (sender is None and selected['kind']=='user'
                    and isinstance(kind,dict) and (kind.get('@type') in {'chatTypePrivate','chatTypeBasicGroup'}
                        or (kind.get('@type')=='chatTypeSupergroup' and kind.get('is_channel') is False)))
                explicit_sender = (isinstance(sender, dict) and sender.get('@type') == native_kind
                    and _telegram_id(sender.get(field)) == selected['id'])
                valid = valid and (implicit_personal or explicit_sender)
                p['implicit_personal'] = implicit_personal
                if p['group']:
                    native_type = kind.get('@type')
                    valid = valid and native_type in {'chatTypeBasicGroup','chatTypeSupergroup'}
                    field = 'basic_group_id' if native_type == 'chatTypeBasicGroup' else 'supergroup_id'
                    group_id = _telegram_id(kind.get(field)) if valid else None
                    if native_type == 'chatTypeSupergroup' and type(kind.get('is_channel')) is not bool: valid = False
                else:
                    valid = valid and kind.get('@type') == 'chatTypePrivate'
                    user_id = _telegram_id(kind.get('user_id')) if valid else None
                    if saved:
                        valid = valid and user_id == p['account_id'] and p['chat_id'] == p['account_id']
            except (TelegramBoundaryError, AttributeError):
                valid = False
            if not valid:
                self.pending = None
                return True
            p['title'] = raw['title']
            if p['group']:
                p['native_type'], p['group_id'] = native_type, group_id
                p['is_channel'] = kind.get('is_channel', False)
                permissions = raw.get('permissions')
                p['default_can_send'] = isinstance(permissions, dict) and permissions.get('can_send_basic_messages') is True
                p['stage'], p['extra'] = 'group', 'setup:group:' + uuid4().hex
                method, field = ('getBasicGroup','basic_group_id') if native_type == 'chatTypeBasicGroup' else ('getSupergroup','supergroup_id')
                self._setup_send({'@type':method, field:int(group_id), '@extra':p['extra']}, p)
            else:
                p['stage'], p['extra'] = 'paid', 'setup:paid:' + uuid4().hex
                self._setup_send({'@type':'canSendMessageToUser', 'user_id':int(user_id), 'only_local':False, '@extra':p['extra']}, p)
            return True
        if p['stage'] == 'group':
            try:
                valid = _telegram_id(raw.get('id')) == p['group_id']
                if p['native_type'] == 'chatTypeBasicGroup':
                    valid = valid and typ == 'basicGroup' and raw.get('is_active') is True
                    p['audience'] = 'private'
                else:
                    valid = (valid and typ == 'supergroup' and raw.get('is_channel') is p['is_channel']
                        and type(raw.get('paid_message_star_count')) is int and raw['paid_message_star_count'] == 0
                        and raw.get('restriction_info') is None)
                    names = raw.get('usernames')
                    if names is None: p['audience'] = 'private'
                    elif isinstance(names, dict) and isinstance(names.get('active_usernames'), list):
                        p['audience'] = 'public' if names['active_usernames'] else 'private'
                    else: valid = False
            except TelegramBoundaryError:
                valid = False
            if not valid:
                self.pending = None
                return True
            p['stage'], p['extra'] = 'member', 'setup:member:' + uuid4().hex
            self._setup_send({'@type':'getChatMember', 'chat_id':int(p['chat_id']),
                'member_id':{'@type':'messageSenderUser','user_id':int(p['account_id'])}, '@extra':p['extra']}, p)
            return True
        if p['stage'] == 'member':
            member, status = raw.get('member_id'), raw.get('status')
            try:
                valid = (typ == 'chatMember' and isinstance(member, dict) and member.get('@type') == 'messageSenderUser'
                    and _telegram_id(member.get('user_id')) == p['account_id'] and isinstance(status, dict))
                kind = status.get('@type') if valid else None
                if kind == 'chatMemberStatusCreator': valid = status.get('is_member') is True
                elif kind == 'chatMemberStatusAdministrator':
                    rights = status.get('rights')
                    valid = not p['is_channel'] or (isinstance(rights, dict) and rights.get('can_post_messages') is True)
                elif kind == 'chatMemberStatusMember': valid = not p['is_channel'] and p['default_can_send']
                elif kind == 'chatMemberStatusRestricted':
                    permissions = status.get('permissions')
                    valid = not p['is_channel'] and status.get('is_member') is True and isinstance(permissions, dict) and permissions.get('can_send_basic_messages') is True
                else: valid = False
            except TelegramBoundaryError:
                valid = False
            if not valid:
                self.pending = None
                return True
            if p.get('implicit_personal') and p['native_type']=='chatTypeSupergroup':
                if kind=='chatMemberStatusCreator':
                    valid = status.get('is_anonymous') is False
                elif kind=='chatMemberStatusAdministrator':
                    valid = isinstance(status.get('rights'),dict) and status['rights'].get('is_anonymous') is False
                if not valid:
                    self.pending = None
                    return True
            if p['sender']['kind'] == 'chat':
                p['stage'], p['extra'] = 'senders', 'setup:senders:' + uuid4().hex
                self._setup_send({'@type':'getChatAvailableMessageSenders','chat_id':int(p['chat_id']),'@extra':p['extra']}, p)
            else: p['stage'] = 'complete'
            return True
        if p['stage'] == 'senders':
            senders = raw.get('senders')
            valid = False
            if typ == 'chatMessageSenders' and isinstance(senders, list) and len(senders) <= 100:
                for item in senders:
                    try:
                        native = item['sender']
                        if (item.get('needs_premium') is False and native.get('@type') == 'messageSenderChat'
                            and _telegram_id(native.get('chat_id')) == p['sender']['id']): valid = True
                    except (KeyError, TypeError, AttributeError, TelegramBoundaryError): pass
            if not valid: self.pending = None
            else: p['stage'] = 'complete'
            return True
        if p['stage'] == 'paid':
            if typ != 'canSendMessageToUserResultOk':
                self.pending = None
                return True
            p['stage'] = 'complete'
            return True
        return True

    def finish(self):
        with self.session.authority_lock:
            p, account = self.pending, self.auth.snapshot()
            if (p is None or p['stage'] != 'complete' or account.account_id != p['account_id'] or account.epoch != p['epoch']
                or self.auth.status != 'ready' or account.auth_state != 'ready' or account.network_state != 'online' or account.revocation_pending or not self.client.transport_ready):
                self.pending = None
                raise TelegramBoundaryError('Destination verification unavailable')
            selected = p['sender'] if p['group'] else {'kind':'user','id':p['account_id']}
            can_select = p['group'] and selected['kind'] == 'chat'
            snapshot = {'account_id':p['account_id'], 'chat_id':p['chat_id'], 'title':p['title'], 'audience':p.get('audience','private'),
                'permissions':{'can_send':True, 'can_select_sender':can_select}, 'sender':selected.copy(), 'paid_required':False}
            version = self.config.save_destination(snapshot, account_epoch=p['epoch'], sender_selected=bool(can_select and p['sender_selected']))
            self.evidence = (p['epoch'], version, deepcopy(snapshot))
            self.pending = None
            return snapshot, version

    def is_current(self, delivery, proposal):
        evidence = self.evidence
        return bool(evidence and evidence[0] == delivery['account_epoch'] and evidence[1] == proposal['destination_version']
            and evidence[2] == proposal['destination'] and evidence[2]['account_id'] == delivery['account_id']
            and evidence[2]['chat_id'] == delivery['destination_id'] and evidence[2]['sender'] == delivery['sender'])
