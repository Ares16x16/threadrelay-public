"""Pinned TDLib JSON boundary. Native loading requires a private host capability.

Only named backend methods issue requests. Raw updates never leave this module;
fixtures can inject a binding, but cannot establish a production safety receipt.
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from uuid import uuid4
from threading import Lock
from typing import Any, Callable, Protocol
from .events import AttemptEvents

MAX_JSON = 1_048_576
_NATIVE_RECEIVE_LOCK = Lock()
_OWNER_LOCK = Lock()
_OWNED_ROOTS: set[Path] = set()


class NativeUnavailable(RuntimeError):
    pass


class TelegramBoundaryError(RuntimeError):
    pass


def _group_metadata_identity(chat_id):
    """Pinned TDLib DialogId ranges; never guess a secret-chat/channel identity."""
    try:
        if type(chat_id) is int:
            number=chat_id
        elif isinstance(chat_id,str) and str(int(chat_id))==chat_id:
            number=int(chat_id)
        else:raise ValueError
        if -999999999999<=number<0:
            return 'chatTypeBasicGroup',-number
        channel=-1000000000000-number
        if 0<channel<1000000000000-(1<<31):
            return 'chatTypeSupergroup',channel
    except (TypeError,ValueError):
        pass
    raise TelegramBoundaryError('Saved group identity unavailable')


class JsonBinding(Protocol):
    def create(self) -> int: ...
    def send(self, client_id: int, request: dict[str, Any]) -> None: ...
    def receive(self, timeout: float) -> dict[str, Any] | None: ...


class Vault(Protocol):
    def resolve(self, ref: str) -> dict[str, Any]: ...


def _copy_native_json(pointer: int) -> bytes:
    raw = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ubyte))
    length = 0
    while length <= MAX_JSON and raw[length] != 0:
        length += 1
    if length > MAX_JSON:
        raise TelegramBoundaryError("Invalid native response")
    return ctypes.string_at(pointer, length)


class NativeJsonBinding:
    """Bindings for the new TDLib JSON C interface, not a library verifier."""
    def __init__(self, library: Any, *, purpose="delivery", generation=0):
        if purpose not in {"delivery", "connection_only"}:
            raise NativeUnavailable("Native TDLib purpose unavailable")
        self._purpose = purpose
        self.generation = generation
        self._profile = None
        self._owned_client = None
        self._connection_account_id = None
        self._connection_get_me_pending = False
        self._connection_list_pending = {}
        self._connection_chat_pending = {}
        self._connection_listed_chats = set()
        self._connection_private_users = set()
        self._connection_groups = {}
        self._automatic_send_consumed = None
        self._owner_history_metadata_lane = None
        self._owner_history_metadata_chat = None
        self._owner_history_metadata_identity = None
        self._owner_history_metadata_phase = None
        self._owner_history_metadata_extra = None
        self._owner_history_metadata_list_index = None
        self._library = library
        library.td_create_client_id.argtypes = []
        library.td_create_client_id.restype = ctypes.c_int
        library.td_send.argtypes = [ctypes.c_int, ctypes.c_char_p]
        library.td_send.restype = None
        library.td_receive.argtypes = [ctypes.c_double]
        library.td_receive.restype = ctypes.c_void_p
        library.td_execute.argtypes = [ctypes.c_char_p]
        library.td_execute.restype = ctypes.c_void_p

    @classmethod
    def load_verified(cls, receipt: object) -> NativeJsonBinding:
        from .native_loader import _load
        try:
            return cls(_load(receipt), purpose=receipt._purpose, generation=receipt._generation)
        except (AttributeError, TypeError):
            raise NativeUnavailable("Native TDLib ABI unavailable") from None

    def create(self) -> int:
        if self.connection_only:
            if self._profile is None or self._owned_client is not None:
                raise TelegramBoundaryError("Private connection profile required")
            self._profile.recheck()
        # Suppress native logs synchronously before client creation.
        with _NATIVE_RECEIVE_LOCK:
            pointer = self._library.td_execute(b'{"@type":"setLogStream","log_stream":{"@type":"logStreamEmpty"}}')
            try:
                if not pointer or json.loads(_copy_native_json(pointer).decode("utf-8")).get("@type") != "ok":
                    raise ValueError
            except (ValueError, UnicodeError, AttributeError, TelegramBoundaryError):
                raise TelegramBoundaryError("Native logging could not be disabled") from None
            client = int(self._library.td_create_client_id())
            self._owned_client = client
            return client

    @property
    def connection_only(self):
        return self._purpose == "connection_only"

    def bind_connection_profile(self, profile):
        from .connection_profile import ConnectionProfile
        if not self.connection_only or type(profile) is not ConnectionProfile or self._profile is not None:
            raise TelegramBoundaryError("Private connection profile required")
        profile.recheck()
        self._profile = profile

    def _connection_request(self, client_id, request):
        if self._profile is None or client_id != self._owned_client:
            raise TelegramBoundaryError("Private connection profile required")
        self._profile.recheck()
        methods = {"setNetworkType", "setTdlibParameters", "setOption", "setAuthenticationPhoneNumber",
            "checkAuthenticationCode", "checkAuthenticationPassword", "requestQrCodeAuthentication", "resendAuthenticationCode",
            "getMe", "getAuthorizationState", "close", "logOut", "createPrivateChat", "getChat", "canSendMessageToUser",
            "loadChats", "getChats", "getBasicGroup", "getSupergroup", "getChatMember", "getChatAvailableMessageSenders",
            "createBasicGroupChat"}
        method = request.get("@type")
        if method not in methods:
            raise TelegramBoundaryError("Connection-only profile cannot send messages")
        if method=='resendAuthenticationCode' and (set(request)!={'@type','reason','@extra'} or request['reason'] is not None
                or not isinstance(request['@extra'],str) or not re.fullmatch(r'auth:resend:[A-Za-z0-9_-]{1,128}',request['@extra'])):
            raise TelegramBoundaryError('Explicit correlated code resend required')
        if method == "getMe":
            if set(request) != {"@type", "@extra"} or request["@extra"] != "auth:get-me":
                raise TelegramBoundaryError("Correlated own identity required")
            self._connection_get_me_pending = True
        if method in {"loadChats", "getChats"}:
            limit = request.get('limit')
            stage = 'load' if method == 'loadChats' else 'chats'
            manual=self._connection_extra(request,self._owner_metadata_prefix('prepare-load:'),self._owner_metadata_prefix('prepare-ids:'))
            if manual:
                index=self._owner_history_metadata_list_index
                expected={'@type':'chatListMain' if index==0 else 'chatListArchive'}
                phase='super_load' if method=='loadChats' else 'super_ids'
                prefix=self._owner_metadata_prefix('prepare-load:') if method=='loadChats' else self._owner_metadata_prefix('prepare-ids:')
                if (self._connection_account_id is None or self._owner_history_metadata_identity is None
                        or self._owner_history_metadata_identity[0]!='chatTypeSupergroup' or index not in (0,1)
                        or self._owner_history_metadata_phase!=phase
                        or set(request)!={'@type','chat_list','limit','@extra'} or request.get('chat_list')!=expected
                        or type(limit) is not int or limit!=100 or not self._connection_extra(request,prefix)):
                    raise TelegramBoundaryError('Exact saved supergroup preparation required')
                self._owner_history_metadata_phase=phase+'_pending'
                self._owner_history_metadata_extra=request['@extra']
                return  # Dedicated bounded scope; never ordinary discovery inventory.
            if (self._connection_account_id is None or set(request) != {'@type','chat_list','limit','@extra'}
                    or request.get('chat_list') not in ({'@type':'chatListMain'}, {'@type':'chatListArchive'})
                    or type(limit) is not int or not 1 <= limit <= (100 if method == 'loadChats' else 10000)
                    or not self._connection_extra(request, 'listing:' + stage + ':')):
                raise TelegramBoundaryError('Explicit bounded destination discovery required')
            if method == 'getChats':
                if len(self._connection_list_pending) >= 128:
                    raise TelegramBoundaryError('Destination discovery busy')
                self._connection_list_pending[request['@extra']] = limit
        if method in {"createPrivateChat", "getChat", "canSendMessageToUser"}:
            fields = {"@type", "chat_id"} if method == "getChat" else {"@type", "user_id", "force" if method == "createPrivateChat" else "only_local"}
            if set(request) not in (fields, fields | {"@extra"}):
                raise TelegramBoundaryError("Only own or explicitly listed chat metadata is available")
            field = "chat_id" if method == "getChat" else "user_id"
            own = self._connection_account_id is not None and request.get(field) == self._connection_account_id
            listed = (method == 'getChat' and request.get(field) in self._connection_listed_chats
                      and self._connection_extra(request, 'listing:chat:', 'setup:chat:', 'setup:select:read:'))
            paid = (method == 'canSendMessageToUser' and request.get(field) in self._connection_private_users
                    and self._connection_extra(request, 'setup:paid:'))
            manual = (method=='getChat' and type(request.get(field)) is int
                      and request.get(field)==self._owner_history_metadata_chat
                      and self._owner_history_metadata_phase=='get_chat'
                      and self._connection_extra(request,'setup:chat:'))
            if (self._connection_account_id is None or type(request.get(field)) is not int
                    or not (own or listed or paid or manual)
                    or (method == "createPrivateChat" and request.get("force") is not False)
                    or (method == "canSendMessageToUser" and request.get("only_local") is not False)):
                raise TelegramBoundaryError("Only own or explicitly listed chat metadata is available")
            if method == 'getChat' and (listed or manual):
                if len(self._connection_chat_pending) >= 128:
                    raise TelegramBoundaryError('Destination metadata busy')
                if manual:self.clear_owner_history_metadata()
                self._connection_chat_pending[request['@extra']] = request['chat_id']
        if method=='createBasicGroupChat':
            if (self._connection_account_id is None or self._owner_history_metadata_phase!='create'
                    or self._owner_history_metadata_identity is None
                    or self._owner_history_metadata_identity[0]!='chatTypeBasicGroup'
                    or set(request)!={'@type','basic_group_id','force','@extra'}
                    or type(request.get('basic_group_id')) is not int
                    or request['basic_group_id']!=self._owner_history_metadata_identity[1]
                    or request.get('force') is not False
                    or not self._connection_extra(request,self._owner_metadata_prefix('prepare-chat:'))
                    or len(self._connection_chat_pending)>=128):
                raise TelegramBoundaryError('Exact saved basic-group preparation required')
            self._owner_history_metadata_phase='create_pending'
            self._owner_history_metadata_extra=request['@extra']
            self._connection_chat_pending[request['@extra']]=self._owner_history_metadata_chat
        if method in {'getBasicGroup','getSupergroup'}:
            field = 'basic_group_id' if method == 'getBasicGroup' else 'supergroup_id'
            native_type = 'chatTypeBasicGroup' if method == 'getBasicGroup' else 'chatTypeSupergroup'
            manual_basic=(method=='getBasicGroup' and self._owner_history_metadata_phase=='basic_info'
                and self._owner_history_metadata_identity is not None
                and self._owner_history_metadata_identity[0]=='chatTypeBasicGroup'
                and type(request.get(field)) is int and request[field]==self._owner_history_metadata_identity[1]
                and self._connection_extra(request,self._owner_metadata_prefix('prepare-basic:')))
            if (self._connection_account_id is None or set(request) != {'@type',field,'@extra'}
                    or type(request.get(field)) is not int
                    or not (manual_basic or (native_type,request[field]) in self._connection_groups.values()
                            and self._connection_extra(request,'setup:group:'))):
                raise TelegramBoundaryError('Listed group metadata required')
            if manual_basic:
                self._owner_history_metadata_phase='basic_info_pending'
                self._owner_history_metadata_extra=request['@extra']
        if method in {'getChatMember','getChatAvailableMessageSenders'}:
            fields = {'@type','chat_id','@extra'} | ({'member_id'} if method == 'getChatMember' else set())
            if (self._connection_account_id is None or set(request) != fields
                    or type(request.get('chat_id')) is not int or request['chat_id'] not in self._connection_groups
                    or not self._connection_extra(request, *(['setup:member:','listing:member:'] if method == 'getChatMember' else ['setup:senders:','setup:select:available:']))
                    or (method == 'getChatMember' and request.get('member_id') != {'@type':'messageSenderUser','user_id':self._connection_account_id})):
                raise TelegramBoundaryError('Listed group self-metadata required')
        if method == "logOut":
            self._connection_account_id = None
            self._connection_get_me_pending = False
            self._clear_destination_scope()
        if method == "setTdlibParameters":
            try:
                key = base64.b64decode(request["database_encryption_key"], validate=True)
                if (request["database_directory"] != str(self._profile.path)
                        or request["files_directory"] != str(self._profile.path / "files")
                        or hashlib.sha256(key).digest() != self._profile.key_digest
                        or any(request.get(name) is not False for name in ("use_file_database", "use_chat_info_database", "use_message_database", "use_secret_chats"))):
                    raise ValueError
            except (KeyError, ValueError, TypeError):
                raise TelegramBoundaryError("Connection profile parameters rejected") from None
        if method == "setNetworkType" and request.get("type", {}).get("@type") not in {"networkTypeNone", "networkTypeOther"}:
            raise TelegramBoundaryError("Connection network request rejected")
        if method == "setOption":
            allowed = {"ignore_background_updates", "ignore_inline_thumbnails", "disable_top_chats",
                "disable_network_statistics", "disable_persistent_network_statistics", "message_unload_delay", "notification_group_count_max"}
            name = request.get("name")
            expected = ({"@type": "optionValueInteger", "value": "60" if name == "message_unload_delay" else "0"}
                        if name in {"message_unload_delay", "notification_group_count_max"}
                        else {"@type": "optionValueBoolean", "value": True})
            if name not in allowed or request.get("value") != expected:
                raise TelegramBoundaryError("Connection option rejected")

    def _owner_metadata_prefix(self, suffix):
        lane=self._owner_history_metadata_lane
        return (lane if lane in {'owner-history','owner-automatic'} else 'owner-history')+':'+suffix

    @staticmethod
    def _connection_extra(request, *prefixes):
        value = request.get('@extra')
        return isinstance(value,str) and any(value.startswith(prefix) and re.fullmatch(r'[a-f0-9]{32}',value[len(prefix):]) for prefix in prefixes)

    def _clear_destination_scope(self):
        self._connection_list_pending.clear()
        self._connection_chat_pending.clear()
        self._connection_listed_chats.clear()
        self._connection_private_users.clear()
        self._connection_groups.clear()
        self.clear_owner_history_metadata()

    def _observe_destination_scope(self, result):
        extra = result.get('@extra')
        if extra is not None and extra==self._owner_history_metadata_extra:
            identity=self._owner_history_metadata_identity
            phase=self._owner_history_metadata_phase
            if phase=='super_load_pending':
                if (result.get('@type')=='ok' or result.get('@type')=='error'
                        and type(result.get('code')) is int and result['code']==404):
                    self._owner_history_metadata_phase='super_ids';self._owner_history_metadata_extra=None
                else:self.clear_owner_history_metadata()
                return
            if phase=='super_ids_pending':
                values=result.get('chat_ids')
                valid=(result.get('@type')=='chats' and type(result.get('total_count')) is int
                    and result['total_count']>=0 and isinstance(values,list) and len(values)<=100
                    and all(type(value) is int and value!=0 and abs(value)<2**53 for value in values)
                    and len(set(values))==len(values))
                if not valid:self.clear_owner_history_metadata()
                elif self._owner_history_metadata_chat in values:
                    self._owner_history_metadata_phase='get_chat';self._owner_history_metadata_extra=None
                elif self._owner_history_metadata_list_index==0:
                    self._owner_history_metadata_list_index=1
                    self._owner_history_metadata_phase='super_load';self._owner_history_metadata_extra=None
                else:self.clear_owner_history_metadata()
                return  # No unrelated returned ID acquires metadata permission.
            if phase=='basic_info_pending':
                if (result.get('@type')=='basicGroup' and type(result.get('id')) is int
                        and identity is not None and result['id']==identity[1]):
                    self._owner_history_metadata_phase='create';self._owner_history_metadata_extra=None
                else:self.clear_owner_history_metadata()
                return
            if phase=='create_pending':
                kind=result.get('type')
                if (result.get('@type')=='chat' and type(result.get('id')) is int
                        and result['id']==self._owner_history_metadata_chat
                        and isinstance(kind,dict) and kind.get('@type')=='chatTypeBasicGroup'
                        and type(kind.get('basic_group_id')) is int and identity is not None
                        and kind['basic_group_id']==identity[1]):
                    self._owner_history_metadata_phase='get_chat';self._owner_history_metadata_extra=None
                else:
                    self._connection_chat_pending.pop(extra,None)
                    self.clear_owner_history_metadata()
                    return
        limit = self._connection_list_pending.pop(extra, None) if isinstance(extra,str) else None
        if limit is not None and result.get('@type') == 'chats':
            values = result.get('chat_ids')
            if (type(result.get('total_count')) is int and result['total_count']>=0
                    and isinstance(values,list) and len(values)<=limit and all(type(value) is int and value != 0 and abs(value)<2**53 for value in values)
                    and len(set(values))==len(values) and len(self._connection_listed_chats | set(values))<=20000):
                self._connection_listed_chats.update(values)
        chat_id = self._connection_chat_pending.pop(extra,None) if isinstance(extra,str) else None
        if chat_id is not None and result.get('@type')=='chat' and type(result.get('id')) is int and result['id']==chat_id:
            kind=result.get('type')
            if not isinstance(kind,dict):return
            typ=kind.get('@type')
            if typ=='chatTypePrivate':
                identifier=kind.get('user_id')
                if type(identifier) is int and 0<identifier<2**53:self._connection_private_users.add(identifier)
            elif typ in {'chatTypeBasicGroup','chatTypeSupergroup'}:
                identifier=kind.get('basic_group_id' if typ=='chatTypeBasicGroup' else 'supergroup_id')
                if type(identifier) is int and 0<identifier<2**53:self._connection_groups[chat_id]=(typ,identifier)

    def send(self, client_id: int, request: dict[str, Any]) -> None:
        if self.connection_only:
            self._connection_request(client_id, request)
        try:
            data = json.dumps(request, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
            if not data or len(data) > MAX_JSON:
                raise ValueError
            self._library.td_send(client_id, data)
        except (ValueError, TypeError, UnicodeError):
            raise TelegramBoundaryError("Invalid native request") from None

    def close_owned_connection(self, client_id: int) -> None:
        """Terminal cleanup only, including after its profile guard is corrupt.

        No caller-supplied request can use this route. Ownership was fixed at
        client creation; a broken profile cannot prevent closing that client.
        """
        if (not self.connection_only or self._profile is None or type(client_id) is not int
                or client_id < 1 or client_id != self._owned_client):
            raise TelegramBoundaryError('Owned connection close unavailable')
        self._library.td_send(client_id, b'{"@type":"close","@extra":"auth:close"}')

    def send_owner_test(self, permit):
        """One exact connection-test permit; never an ordinary send API."""
        from .owner_test import OwnerTestPermit
        if not self.connection_only or type(permit) is not OwnerTestPermit:
            raise TelegramBoundaryError('Owner test authority unavailable')
        request = permit.take_for_native(self._owned_client, self._profile, self.generation,
                                         self._connection_account_id)
        try:
            data = json.dumps(request, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')
            if not data or len(data) > MAX_JSON:raise ValueError
            self._library.td_send(self._owned_client, data)
        except (ValueError, TypeError, UnicodeError):
            raise TelegramBoundaryError('Owner test dispatch unavailable') from None

    def send_owner_history(self, permit):
        """Dispatch only an opaque owner-approved exact History request."""
        from .owner_history import OwnerHistoryPermit
        if not self.connection_only or type(permit) is not OwnerHistoryPermit:
            raise TelegramBoundaryError('Manual History authority unavailable')
        request=permit.take_for_native(self._owned_client,self._profile,self.generation,
                                      self._connection_account_id)
        try:
            data=json.dumps(request,separators=(',', ':'),ensure_ascii=False,allow_nan=False).encode('utf-8')
            if not data or len(data)>MAX_JSON:raise ValueError
            self._library.td_send(self._owned_client,data)
        except (ValueError,TypeError,UnicodeError):
            raise TelegramBoundaryError('Manual History dispatch unavailable') from None

    def send_owner_automatic(self, permit):
        """Dispatch only a concrete policy-bound automatic one-use permit."""
        from .owner_automatic import OwnerAutomaticPermit
        if not self.connection_only or type(permit) is not OwnerAutomaticPermit:
            raise TelegramBoundaryError('Automatic delivery authority unavailable')
        request=permit.take_for_native(self._owned_client,self._profile,self.generation,
                                      self._connection_account_id)
        record=self._profile.automatic_record()
        identity=(record['profile_id'],record['attempt_id'],record['request_extra'])
        if self._automatic_send_consumed==identity:
            raise TelegramBoundaryError('Automatic delivery already consumed')
        try:
            data=json.dumps(request,separators=(',', ':'),ensure_ascii=False,allow_nan=False).encode('utf-8')
            if not data or len(data)>MAX_JSON:raise ValueError
            self._automatic_send_consumed=identity
            self._library.td_send(self._owned_client,data)
        except (ValueError,TypeError,UnicodeError):
            raise TelegramBoundaryError('Automatic delivery dispatch unavailable') from None

    def scope_owner_history_metadata(self, permit):
        """One backend-owned saved-group lookup; no general metadata or send scope."""
        from .owner_history import OwnerHistoryMetadataPermit
        if (not self.connection_only or type(permit) is not OwnerHistoryMetadataPermit
                or self._owner_history_metadata_chat is not None):
            raise TelegramBoundaryError('Manual History metadata unavailable')
        chat=permit.take_for_native(self._owned_client,self._profile,self.generation,self._connection_account_id)
        if type(chat) is not int or not -(2**53-1)<=chat<0:
            raise TelegramBoundaryError('Manual History metadata unavailable')
        identity=_group_metadata_identity(chat)
        self._owner_history_metadata_lane='owner-history'
        self._owner_history_metadata_chat=chat
        self._owner_history_metadata_identity=identity
        self._owner_history_metadata_phase='basic_info' if identity[0]=='chatTypeBasicGroup' else 'super_load'
        self._owner_history_metadata_extra=None
        self._owner_history_metadata_list_index=None if identity[0]=='chatTypeBasicGroup' else 0

    def scope_owner_automatic_metadata(self, permit):
        """One policy-bound saved-group lookup, with a separate request lane."""
        from .owner_automatic import OwnerAutomaticMetadataPermit
        if (not self.connection_only or type(permit) is not OwnerAutomaticMetadataPermit
                or self._owner_history_metadata_chat is not None):
            raise TelegramBoundaryError('Automatic delivery metadata unavailable')
        chat=permit.take_for_native(self._owned_client,self._profile,self.generation,self._connection_account_id)
        if type(chat) is not int or not -(2**53-1)<=chat<0:
            raise TelegramBoundaryError('Automatic saved-group metadata unavailable')
        identity=_group_metadata_identity(chat)
        self._owner_history_metadata_lane='owner-automatic'
        self._owner_history_metadata_chat=chat
        self._owner_history_metadata_identity=identity
        self._owner_history_metadata_phase='basic_info' if identity[0]=='chatTypeBasicGroup' else 'super_load'
        self._owner_history_metadata_extra=None
        self._owner_history_metadata_list_index=None if identity[0]=='chatTypeBasicGroup' else 0

    def clear_owner_automatic_metadata(self):
        if self._owner_history_metadata_lane=='owner-automatic':
            self.clear_owner_history_metadata()

    def clear_owner_history_metadata(self):
        """Revoke an unused exact metadata scope without issuing any request."""
        self._owner_history_metadata_lane=None
        self._owner_history_metadata_chat=None
        self._owner_history_metadata_identity=None
        self._owner_history_metadata_phase=None
        self._owner_history_metadata_extra=None
        self._owner_history_metadata_list_index=None

    def receive(self, timeout: float) -> dict[str, Any] | None:
        if not 0 <= timeout <= 1:
            raise TelegramBoundaryError("Invalid receive timeout")
        # td_receive's pointer is copied before the next native call.
        with _NATIVE_RECEIVE_LOCK:
            pointer = self._library.td_receive(timeout)
            if not pointer:
                return None
            data = _copy_native_json(pointer)
        try:
            result = json.loads(data.decode("utf-8"))
            if not isinstance(result, dict):
                raise ValueError
            if self.connection_only and result.get("@client_id") == self._owned_client:
                if (result.get('@type')=='updateAuthorizationState' and isinstance(result.get('authorization_state'),dict)
                        and result['authorization_state'].get('@type')!='authorizationStateReady'):
                    self._connection_account_id=None
                    self._connection_get_me_pending=False
                    self._clear_destination_scope()
                self._observe_destination_scope(result)
                if (self._connection_get_me_pending and result.get("@type") == "user" and result.get("@extra") == "auth:get-me"
                        and result.get("type", {}).get("@type") == "userTypeRegular"
                        and type(result.get("id")) is int and 0 < result["id"] < 2**53):
                    self._clear_destination_scope()
                    self._connection_account_id = result["id"]
                    self._connection_get_me_pending = False
                if (result.get("@type") == "updateAuthorizationState" and result.get("authorization_state", {}).get("@type")
                        in {"authorizationStateLoggingOut", "authorizationStateClosing", "authorizationStateClosed"}):
                    self._connection_account_id = None
                    self._clear_destination_scope()
                    if (result["authorization_state"]["@type"] == "authorizationStateClosed" and self._profile is not None
                            and self._profile.purpose not in {'owner_test_pending','manual_history_pending','automatic_pending'}):
                        self._profile.close()
            return result
        except (ValueError, UnicodeError):
            raise TelegramBoundaryError("Invalid native response") from None


@dataclass(frozen=True)
class AuthEvent:
    kind: str
    # Private QR is consumed only by the dedicated secure presentation path.
    private_qr: str | None = field(default=None, repr=False)
    account_id: str | None = None
    code_delivery: dict | None = None
    retry_after: int | None = None


@dataclass(frozen=True)
class NetworkPermit:
    """Trusted backend authority, not an IPC/renderer DTO.

    A future owner must synchronize issuance with session state and revocation.
    A mocked permit does not prove the native no-flush condition.
    """
    purpose: str  # clean_auth or running_reconciliation
    account_epoch: int
    pending: bool


def _telegram_id(value: Any) -> str:
    # TDLib int53 is numeric. No bool, lossy float, or unsafe conversion.
    if type(value) is not int or not -(2**53 - 1) <= value <= 2**53 - 1:
        raise TelegramBoundaryError("Invalid Telegram identifier")
    return str(value)


class TelegramClient:
    def __init__(self, binding_factory: Callable[[], JsonBinding], vault: Vault, session_root: Path, *, connection_only=None):
        self._factory = binding_factory
        self._vault = vault
        self._root = Path(session_root).absolute()
        self._binding: JsonBinding | None = None
        self._client_id: int | None = None
        self._vault_ref: str | None = None
        self._directory: Path | None = None
        self._receive_lock = Lock()
        self._closed = False
        self._logout_requested = self._logout_ok = self._logout_seen = False
        self._closing = False
        self._offline_pending = False
        self._offline_failed = False
        self._auth_extra: str | None = None
        self._config_pending: set[str] = set()
        self._config_started = False
        self._config_failed = False
        self._attempt_events = AttemptEvents()
        self._network_enabled = False
        self._network_enable_extra = None
        self._network_disable_extra = None
        self._network_failed = False
        self._connection_ready = False
        self._destination_verifier = None
        self._destination_listing = None
        self._owner_test = None
        self._owner_history = None
        self._owner_automatic = None
        self._last_receive_had_event = False
        self._connection_only = connection_only
        self._profile_guard = None
        self._initial_credentials = None
        self.profile_allowed=None

    def has_existing_session(self,account_ref: str) -> bool:
        """Existence only; HMAC, key and native identity are rechecked on restore."""
        if not re.fullmatch(r'vault:[A-Za-z0-9_-]{1,100}',account_ref):return False
        directory=self._root/hashlib.sha256(account_ref.encode('ascii')).hexdigest()
        return bool(not self._root.is_symlink() and directory.is_dir() and not directory.is_symlink()
            and all((directory/name).is_file() and not (directory/name).is_symlink()
                for name in ('connection-purpose.json','td.binlog')))

    def initialize_offline(self, account_ref: str, *, require_existing=False) -> None:
        if self._binding is not None or not re.fullmatch(r"vault:[A-Za-z0-9_-]{1,100}", account_ref):
            raise TelegramBoundaryError("Invalid account initialization")
        if require_existing and not self.has_existing_session(account_ref):
            raise TelegramBoundaryError('Saved Telegram profile unavailable')
        # Opaque account directory is backend generated, never renderer supplied.
        with _OWNER_LOCK:
            if self._root in _OWNED_ROOTS:
                raise TelegramBoundaryError("Telegram session already owned")
            _OWNED_ROOTS.add(self._root)
        try:
            from .private_directory import private_directory
            private_directory(self._root)
            directory = self._root / hashlib.sha256(account_ref.encode("ascii")).hexdigest()
            private_directory(directory)
            if directory.is_symlink() or not directory.is_dir():
                raise ValueError
            files = directory / "files"
            if files.exists() or files.is_symlink():
                private_directory(files)
            binding = self._factory()  # loads verified code, never creates a client/database
            restricted = getattr(binding, "connection_only", False) is True
            if self._connection_only is not None and self._connection_only is not restricted:
                raise ValueError("Native connection scope mismatch")
            self._connection_only = restricted
            if restricted:
                from .connection_profile import connection_only_profile
                self._initial_credentials = self._vault.resolve(account_ref)
                self._profile_guard = connection_only_profile(directory, account_ref, self._initial_credentials["database_key"])
                if self.profile_allowed is not None and not self.profile_allowed(self._profile_guard,directory):
                    raise ValueError('Isolated profile cannot be reopened')
                binding.bind_connection_profile(self._profile_guard)
            elif (directory / "connection-purpose.json").exists():
                raise ValueError("Connection-only profiles cannot be adopted for delivery")
            private_directory(files)
            if files.is_symlink() or not files.is_dir():
                raise ValueError
            client_id = binding.create()
            if type(client_id) is not int or client_id <= 0:
                raise ValueError
            self._binding, self._client_id = binding, client_id
            self._directory, self._vault_ref = directory, account_ref
            self._offline_pending = True
            self._send({"@type": "setNetworkType", "type": {"@type": "networkTypeNone"}, "@extra": "init:offline"})
        except Exception:
            # If create succeeded, native ownership is uncertain. Retain the
            # process-local lock until an observed Closed or process restart.
            if self._binding is None:
                self._initial_credentials = None
                if self._profile_guard is not None:
                    self._profile_guard.close()
                with _OWNER_LOCK:
                    _OWNED_ROOTS.discard(self._root)
            raise TelegramBoundaryError("Telegram initialization unavailable") from None

    def _send(self, request: dict[str, Any], *, allow_closing: bool = False) -> None:
        if self._binding is None or self._client_id is None or self._closed or (self._closing and not allow_closing):
            raise TelegramBoundaryError("Telegram client unavailable")
        try:
            self._binding.send(self._client_id, request)
        except Exception:
            raise TelegramBoundaryError("Telegram request unavailable") from None

    def _parameters(self) -> None:
        try:
            values = self._initial_credentials if self._initial_credentials is not None else self._vault.resolve(self._vault_ref)
            self._initial_credentials = None
            api_id, api_hash, key = values["api_id"], values["api_hash"], values["database_key"]
            if type(api_id) is not int or api_id <= 0 or not isinstance(api_hash, str) or not api_hash or not isinstance(key, bytes) or len(key) < 32:
                raise ValueError
            assert self._directory is not None
            self._config_started = True
            self._config_pending = {"init:parameters"} | {"init:option:" + name for name in (
                "ignore_background_updates", "ignore_inline_thumbnails", "disable_top_chats",
                "disable_network_statistics", "disable_persistent_network_statistics",
                "message_unload_delay", "notification_group_count_max")}
            self._send({"@type": "setTdlibParameters", "use_test_dc": False,
                "database_directory": str(self._directory), "files_directory": str(self._directory / "files"),
                "database_encryption_key": base64.b64encode(key).decode("ascii"),
                "use_file_database": False, "use_chat_info_database": False,
                "use_message_database": False, "use_secret_chats": False,
                "api_id": api_id, "api_hash": api_hash, "system_language_code": "en",
                "device_model": "ThreadRelay", "system_version": "desktop",
                "application_version": "0.1", "@extra": "init:parameters"})
            for name in ("ignore_background_updates", "ignore_inline_thumbnails", "disable_top_chats",
                         "disable_network_statistics", "disable_persistent_network_statistics"):
                self._send({"@type": "setOption", "name": name,
                    "value": {"@type": "optionValueBoolean", "value": True}, "@extra": "init:option:" + name})
            for name, value in (("message_unload_delay", "60"), ("notification_group_count_max", "0")):
                self._send({"@type": "setOption", "name": name,
                    "value": {"@type": "optionValueInteger", "value": value}, "@extra": "init:option:" + name})
        except Exception:
            raise TelegramBoundaryError("Secure credentials unavailable") from None
        finally:
            # Python cannot guarantee zeroization of immutable strings/bytes.
            values = api_hash = key = None

    def local_attempts_quiescent(self) -> bool:
        """Current process-local correlation check, not native no-flush proof."""
        attempts = self._attempt_events
        return not (attempts.attempts or attempts.reservations or attempts.local_queries
                    or attempts.reconcile_queries or attempts.events)

    def reserve_attempt(self):
        return self._attempt_events.reserve()

    def cancel_attempt_reservation(self, reservation) -> None:
        self._attempt_events.cancel_reservation(reservation)

    def retire_attempt(self, request_extra, *, abandon_retry=False) -> bool:
        return self._attempt_events.retire(request_extra, abandon_retry=abandon_retry)

    def register_attempt(self, *, reservation=None, **binding) -> None:
        self._attempt_events.register(reservation=reservation, **binding)

    def authorization_event(self) -> AuthEvent | None:
        # Sole receive owner; AuthController consumes all authorization state.
        return self._receive_once()

    def verify_failed_attempt(self, request_extra: str, failed_id: str) -> None:
        query = 'retry:local:' + uuid4().hex
        try:
            attempt = self._attempt_events.verify_failed(request_extra, failed_id, query)
            self._send({'@type':'getMessageLocally', 'chat_id':int(attempt['chat_id']),
                        'message_id':int(failed_id), '@extra':query})
        except (ValueError, TypeError):
            raise TelegramBoundaryError('Failed message unavailable') from None

    def expire_attempt(self, request_extra: str) -> None:
        self._attempt_events.expire(request_extra)

    def take_delivery_event(self):
        return self._attempt_events.events.popleft() if self._attempt_events.events else None

    def delivery_event(self):
        return self.take_delivery_event()

    def _receive_once(self) -> AuthEvent | None:
        self._last_receive_had_event = False
        if self._binding is None or self._closed:
            return None
        with self._receive_lock:
            try:
                event = self._binding.receive(0)
                self._last_receive_had_event = event is not None
            except Exception:
                raise TelegramBoundaryError("Telegram receive unavailable") from None
            if not isinstance(event, dict) or event.get("@client_id") != self._client_id:
                return None
            typ = event.get("@type")
            # Observe only this client's bounded owner-test correlations. The
            # normal authorization/verifier handlers retain sole receive ownership.
            if self._owner_test is not None:
                self._owner_test.consume(event)
            if self._owner_history is not None:
                self._owner_history.consume(event)
            if self._owner_automatic is not None:
                self._owner_automatic.consume(event)
            if not self._closing and self._destination_listing is not None and self._destination_listing.consume(event):
                return None
            if not self._closing and self._destination_verifier is not None and self._destination_verifier.consume(event):
                return None
            if not self._closing and self._attempt_events.consume(event):
                return None
            if typ == "updateAuthorizationState":
                state = event.get("authorization_state")
                if not isinstance(state, dict):
                    return None
                name = state.get("@type")
                self._auth_extra = None
                terminal = {"authorizationStateLoggingOut", "authorizationStateClosing", "authorizationStateClosed"}
                if self._closing and name not in terminal:
                    return None
                if name in terminal:
                    self._closing = True
                if name == "authorizationStateLoggingOut" and self._logout_requested:
                    self._logout_seen = True
                if name == "authorizationStateWaitTdlibParameters":
                    if self._offline_failed:
                        return AuthEvent("error")
                    self._parameters()
                    return AuthEvent("parameters")
                mapping = {"authorizationStateWaitPhoneNumber": "phone", "authorizationStateWaitCode": "code",
                    "authorizationStateWaitPassword": "password", "authorizationStateReady": "ready_pending_identity",
                    "authorizationStateLoggingOut": "logging_out", "authorizationStateClosing": "closing",
                    "authorizationStateClosed": "closed"}
                if name == "authorizationStateWaitOtherDeviceConfirmation":
                    link = state.get("link")
                    return AuthEvent("qr", private_qr=link) if isinstance(link, str) and len(link) <= 4096 else AuthEvent("unsupported")
                if name=='authorizationStateWaitCode':
                    info=state.get('code_info');info=info if isinstance(info,dict) else {}
                    def channel(value):
                        typ=value.get('@type') if isinstance(value,dict) else None
                        return {'authenticationCodeTypeTelegramMessage':'telegram','authenticationCodeTypeSms':'sms',
                            'authenticationCodeTypeSmsWord':'sms','authenticationCodeTypeSmsPhrase':'sms',
                            'authenticationCodeTypeCall':'call','authenticationCodeTypeFlashCall':'call',
                            'authenticationCodeTypeMissedCall':'call','authenticationCodeTypeFragment':'fragment'}.get(typ,'unknown')
                    timeout=info.get('timeout');next_type=info.get('next_type')
                    available=isinstance(next_type,dict) and type(timeout) is int and 0<=timeout<=86400
                    return AuthEvent('code',code_delivery={'method':channel(info.get('type')),
                        'next_method':channel(next_type) if available else None,'resend_after_seconds':timeout if available else None})
                if name == "authorizationStateClosed":
                    self._closed = True
                    with _OWNER_LOCK:
                        _OWNED_ROOTS.discard(self._root)
                    if self._logout_ok and self._logout_seen:
                        return AuthEvent("logout_confirmed")
                return AuthEvent(mapping.get(name, "unsupported"))
            if self._closing:
                if event.get('@extra') == 'auth:logout' and self._logout_requested and typ == 'ok':
                    self._logout_ok = True
                return None
            if typ == "updateConnectionState":
                state = event.get("state")
                if not isinstance(state, dict):
                    return None
                connection = state.get("@type")
                if connection == "connectionStateReady":
                    self._connection_ready = True
                    return AuthEvent("network_online")
                if connection == "connectionStateWaitingForNetwork":
                    self._connection_ready = False
                    return AuthEvent("network_offline")
                if connection in {"connectionStateConnecting", "connectionStateConnectingToProxy", "connectionStateUpdating"}:
                    self._connection_ready = False
                    return AuthEvent("network_connecting")
                return None
            if typ == "user" and event.get("@extra") == "auth:get-me":
                user_type = event.get("type")
                if not isinstance(user_type, dict) or user_type.get("@type") != "userTypeRegular":
                    return AuthEvent("unsupported")
                account_id = event.get("id")
                if type(account_id) is not int or account_id <= 0:
                    raise TelegramBoundaryError("Invalid Telegram identifier")
                return AuthEvent("identity", account_id=_telegram_id(account_id))
            if event.get('@extra') in {self._network_enable_extra, self._network_disable_extra} and event.get('@extra') is not None:
                extra = event['@extra']
                if typ == 'ok':
                    if extra == self._network_enable_extra: self._network_enable_extra = None
                    if extra == self._network_disable_extra: self._network_disable_extra = None
                    return None
                if typ == 'error':
                    self._network_failed = True
                    self._network_enabled = False
                    self._connection_ready = False
                    try:
                        self.close()
                    except TelegramBoundaryError:
                        pass  # closure remains unproved, authority stays latched
                    return AuthEvent('error')
            if typ == "ok" and event.get("@extra") == "init:offline":
                self._offline_pending = False
                return None
            if typ == "ok" and event.get("@extra") in self._config_pending:
                self._config_pending.remove(event["@extra"])
                return None
            if typ == "error":
                extra = event.get("@extra")
                if extra == "init:offline":
                    self._offline_pending = False
                    self._offline_failed = True
                    return AuthEvent("error")
                if extra in self._config_pending:
                    self._config_pending.remove(extra)
                    self._config_failed = True
                    return AuthEvent("error")
                if extra == self._auth_extra and extra is not None:
                    self._auth_extra = None
                    wait=re.fullmatch(r'FLOOD_WAIT_(\d{1,6})',event.get('message','')) if isinstance(event.get('message'),str) else None
                    return AuthEvent("submit_error",retry_after=int(wait[1]) if wait and 0<int(wait[1])<=86400 else None)
                if extra in {"init:parameters", "auth:get-me"}:
                    return AuthEvent("error")
                return None  # unrelated errors are dropped
            return None  # unsolicited bodies/contacts/media never escape

    def request_qr(self, challenge_id: str) -> None:
        self._auth_extra = "auth:qr:" + challenge_id
        try:
            self._send({"@type": "requestQrCodeAuthentication", "other_user_ids": [], "@extra": self._auth_extra})
        except TelegramBoundaryError:
            self._auth_extra = None
            raise

    def resend_code(self, challenge_id: str) -> None:
        self._auth_extra='auth:resend:'+challenge_id
        try:self._send({'@type':'resendAuthenticationCode','reason':None,'@extra':self._auth_extra})
        except TelegramBoundaryError:
            self._auth_extra=None
            raise

    def submit_secret(self, kind: str, value: str, challenge_id: str) -> None:
        if not isinstance(value, str) or not 1 <= len(value) <= 4096:
            raise TelegramBoundaryError("Invalid secure input")
        requests = {"phone": ("setAuthenticationPhoneNumber", "phone_number"),
                    "code": ("checkAuthenticationCode", "code"),
                    "password": ("checkAuthenticationPassword", "password")}
        if kind not in requests:
            raise TelegramBoundaryError("Invalid secure input")
        method, field = requests[kind]
        self._auth_extra = "auth:submit:" + challenge_id
        request = {"@type": method, field: value, "@extra": self._auth_extra}
        if kind == "phone":
            request["settings"] = None
        self._send(request)

    def get_me(self) -> None:
        self._send({"@type": "getMe", "@extra": "auth:get-me"})

    @property
    def network_config_ready(self) -> bool:
        return (not self._closed and not self._closing and not self._offline_pending and not self._offline_failed
            and self._config_started and not self._config_pending and not self._config_failed)

    def enable_network(self, permit: NetworkPermit, *, connection=None, running_account=None) -> None:
        if permit.purpose in {'running_reconciliation','running_bootstrap','saved_account_restore'}:
            # A purpose label/boolean is not a permit. The trusted Sender must
            # supply its currently held SQLite writer; recheck the same account
            # and uncertain outbox on that connection before native dispatch.
            try:
                from threadrelay.storage.outbox import _current_account,_has_native_uncertainty
                from threadrelay.storage.isolation import records
                if connection is None or not connection.in_transaction or running_account is None:
                    raise ValueError
                # A deferred transaction alone is not a writer guard. The
                # identity-preserving write acquires SQLite's writer before the
                # current account/outbox recheck and synchronous native call.
                guarded = connection.execute('UPDATE accounts SET active=active WHERE account_id=? AND epoch=? AND active=1',
                    (running_account, permit.account_epoch))
                if guarded.rowcount != 1:
                    raise ValueError
                if permit.purpose == 'running_reconciliation':
                    _current_account(connection, {'account_id':running_account, 'account_epoch':permit.account_epoch})
                else:
                    saved = connection.execute('SELECT snapshot FROM accounts WHERE account_id=? AND epoch=? AND active=1', (running_account, permit.account_epoch)).fetchone()
                    if saved is None or json.loads(saved[0])['revocation_pending']:
                        raise ValueError
                if _has_native_uncertainty(connection):
                    raise ValueError
                if records(connection) and (self.profile_allowed is None or self._profile_guard is None
                        or not self.profile_allowed(self._profile_guard,self._directory)):
                    raise ValueError
            except Exception:
                raise TelegramBoundaryError('Network authority unavailable') from None
        if (self._offline_pending or self._offline_failed or not self._config_started or self._config_pending or self._config_failed
            or self._network_failed or self._network_disable_extra is not None or self._network_enable_extra is not None
            or permit.purpose not in {"clean_auth", "running_reconciliation", "running_bootstrap", "saved_account_restore"}
            or permit.pending or permit.account_epoch < 0 or not self.local_attempts_quiescent()):
            raise TelegramBoundaryError("Network authority unavailable")
        # Revalidation above remains mandatory even for an already live client.
        # Another setNetworkType changes TDLib's network generation and can
        # interrupt the connection; Start alone must not erase acknowledged Ready.
        if self.transport_ready:
            return
        extra = 'network:enable:' + uuid4().hex
        self._network_enable_extra = extra
        self._connection_ready = False
        self._send({"@type": "setNetworkType", "type": {"@type": "networkTypeOther"}, "@extra": extra})
        self._network_enabled = True

    @property
    def transport_ready(self) -> bool:
        return (not self._closed and not self._closing and self._network_enabled and self._connection_ready
                and not self._offline_pending and not self._offline_failed and self._config_started
                and not self._config_pending and not self._config_failed and not self._network_failed
                and self._network_enable_extra is None and self._network_disable_extra is None)

    def initiate_message(self, *, chat_id: str, text: str, request_extra: str) -> None:
        if not self.transport_ready:
            raise TelegramBoundaryError("Telegram transport unavailable")
        try:
            number = int(chat_id)
            if str(number) != chat_id or not -(2**53-1) <= number <= 2**53-1 or not text:
                raise ValueError
        except (ValueError, TypeError):
            raise TelegramBoundaryError("Invalid Telegram message") from None
        self._send({"@type":"sendMessage", "chat_id":number, "topic_id":None, "reply_to":None,
            "options":{"@type":"messageSendOptions", "suggested_post_info":None,
                "disable_notification":False, "from_background":False, "protect_content":False,
                "allow_paid_broadcast":False, "paid_message_star_count":0,
                "update_order_of_installed_sticker_sets":False, "scheduling_state":None,
                "effect_id":"0", "sending_id":0, "only_preview":False},
            "reply_markup":None, "input_message_content":{"@type":"inputMessageText",
                "text":{"@type":"formattedText", "text":text, "entities":[]},
                "link_preview_options":{"@type":"linkPreviewOptions", "is_disabled":True,
                    "url":"", "force_small_media":False, "force_large_media":False, "show_above_text":False},
                "clear_draft":False}, "@extra":request_extra})

    def initiate_retry(self, *, chat_id: str, failed_id: str, request_extra: str) -> None:
        if not self.transport_ready:
            raise TelegramBoundaryError("Telegram transport unavailable")
        try:
            chat, message = int(chat_id), int(failed_id)
            if _telegram_id(chat) != chat_id or _telegram_id(message) != failed_id:
                raise ValueError
        except (ValueError, TypeError):
            raise TelegramBoundaryError("Invalid failed message") from None
        self._send({'@type':'resendMessages', 'chat_id':chat, 'message_ids':[message],
                    'quote':None, 'paid_message_star_count':0, '@extra':request_extra})

    def reconcile_pending(self, *, request_extras: list[str] | None = None) -> None:
        if not request_extras or len(request_extras) > 128 or len(set(request_extras)) != len(request_extras):
            raise NativeUnavailable("Exact own attempts are required")
        # Validate the complete batch before sending any lookup.
        attempts = []
        for extra in request_extras:
            attempt = self._attempt_events.attempts.get(extra)
            if (attempt is None or attempt['terminal'] or attempt['temporary'] is None
                or extra in self._attempt_events.reconcile_queries.values()):
                raise TelegramBoundaryError("Own attempt unavailable")
            attempts.append((extra, attempt))
        if len(attempts) + len(self._attempt_events.reconcile_queries) > 128:
            raise TelegramBoundaryError("Own attempt unavailable")
        for extra, attempt in attempts:
            query = 'reconcile:local:' + uuid4().hex
            self._attempt_events.reconcile(extra, query)
            self._send({'@type':'getMessageLocally', 'chat_id':int(attempt['chat_id']),
                        'message_id':int(attempt['temporary']), '@extra':query})

    def disable_network(self) -> None:
        # Local authority closes before the asynchronous native request. Actual
        # no-flush behavior is still a Task 10 runtime gate, not this flag.
        enabled = self._network_enabled
        self._network_enabled = False
        self._connection_ready = False
        if self._destination_verifier is not None:
            self._destination_verifier.invalidate()
        if enabled and not self._closing and not self._closed:
            extra = 'network:disable:' + uuid4().hex
            self._network_disable_extra = extra
            self._network_enable_extra = None
            self._send({'@type':'setNetworkType', 'type':{'@type':'networkTypeNone'}, '@extra':extra})

    def begin_shutdown(self) -> None:
        """Latch before any terminal request; keep consuming only terminal states."""
        self._closing = True
        self._network_enabled = False
        self._connection_ready = False
        self._auth_extra = None

    def close(self) -> None:
        self.begin_shutdown()
        if type(self._binding) is NativeJsonBinding and self._binding.connection_only:
            if self._closed:raise TelegramBoundaryError('Native client unavailable')
            self._binding.close_owned_connection(self._client_id)
        else:
            self._send({"@type": "close", "@extra": "auth:close"}, allow_closing=True)

    def logout(self, permit: NetworkPermit) -> None:
        self.begin_shutdown()
        if (permit.pending or permit.purpose != "clean_auth" or self._closed
                or self._offline_pending or self._offline_failed or not self._config_started
                or self._config_pending or self._config_failed or self._network_failed
                or not self.local_attempts_quiescent()):
            raise TelegramBoundaryError("Logout authority unavailable")
        self._logout_requested = True
        self._send({'@type':'setNetworkType','type':{'@type':'networkTypeOther'},'@extra':'auth:logout-network'}, allow_closing=True)
        self._send({"@type": "logOut", "@extra": "auth:logout"}, allow_closing=True)
