"""Stopped worker shell. stdin/stdout are reserved exclusively for framed IPC.

The default process has no feature assembly, HTTP server, browser, model or
transport. Injected LocalFeatures exposes narrow local operations for Task 10's
trusted owner; Start and all native adapters remain unavailable by default.
"""
import json
import struct
import sys
from contextlib import nullcontext
from collections.abc import Callable
from typing import Any, BinaryIO

from .contracts import Command, Event, ProtocolError, validate_contract
from .session import ParentProcess, ParentWatchdog, SessionController
from .delivery.owner_history import ManualHistoryPreviewError

MAX_FRAME_BYTES = 1_048_576
_ROUTE_SAVE_REASONS = {
    'Inactive or stale account binding':'account_changed',
    'Unavailable account binding':'account_unavailable',
    'destination_unavailable':'destination_unavailable',
    'destination_not_saved':'destination_not_saved',
    'destination_not_zero_paid_sendable':'destination_not_sendable',
    'Sender mismatch':'sender_changed',
    'sender_identity_mismatch':'sender_changed',
    'sender_selection_required':'sender_changed',
    'Topic unavailable':'topic_unavailable',
    'Configuration version must increase':'route_changed',
}


class _TelegramAuthOutcomeUnknown(ValueError):
    """Internal closed reason; never project native or exception text."""


def _read_exact(stream: BinaryIO, size: int, *, clean_eof: bool = False) -> bytes | None:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.read(size - len(chunks))
        if not chunk:
            if clean_eof and not chunks:
                return None
            raise ProtocolError("Truncated frame")
        chunks.extend(chunk)
    return bytes(chunks)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("Duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ProtocolError("Non-JSON numeric constant")


def read_frame(stream: BinaryIO) -> Any | None:
    """Read one frame incrementally; None means clean EOF before a new prefix."""
    header = _read_exact(stream, 4, clean_eof=True)
    if header is None:
        return None
    size = struct.unpack(">I", header)[0]
    if not 1 <= size <= MAX_FRAME_BYTES:
        raise ProtocolError("Invalid frame size")
    payload = _read_exact(stream, size)
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        if value is None:
            raise ProtocolError("Null is not a protocol message")
        return value
    except ProtocolError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise ProtocolError("Invalid frame JSON") from error


def encode_frame(value: Any) -> bytes:
    try:
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise ProtocolError("Invalid JSON value") from error
    if not 1 <= len(payload) <= MAX_FRAME_BYTES:
        raise ProtocolError("Invalid frame size")
    return struct.pack(">I", len(payload)) + payload


def dispatch(command: Command, *, controller: SessionController | None = None, features=None, coordinator=None, readiness=None) -> Event:
    """Validate and dispatch only named operations; native effects require trusted assembly."""
    validate_contract("Command", command)
    encode_frame(command)  # Direct callers must meet the same byte ceiling.
    controller = controller or SessionController()
    kind = command["type"]
    if kind in {'BeginTelegramRecovery','QueryTelegramRecovery'} and features is not None:
        try:
            recovery=getattr(features,'telegram_recovery',None)
            if recovery is None:raise NotImplementedError
            result=recovery.begin(command['payload']['delivery_id']) if kind=='BeginTelegramRecovery' else recovery.query()
            event={'version':1,'id':command['id'],'type':'TelegramRecoveryState','payload':result}
        except NotImplementedError:
            event={'version':1,'id':command['id'],'type':'TelegramRecoveryState','payload':{'state':'blocked','reason':'quarantine_unavailable'}}
        except Exception:
            event={'version':1,'id':command['id'],'type':'TelegramRecoveryState','payload':{'state':'failed','reason':'quarantine_verification_failed'}}
    elif kind == 'QueryActivity' and features is not None:
        try:
            if features.activity is None: raise NotImplementedError
            event = {'version':1,'id':command['id'],'type':'Activity','payload':features.activity(**command['payload'])}
        except NotImplementedError:
            event = {'version':1,'id':command['id'],'type':'Error','payload':{'code':'unsupported','message':'Live activity unavailable in this build.'}}
        except Exception:
            event = {'version':1,'id':command['id'],'type':'Error','payload':{'code':'invalid_request','message':'Activity could not be read. Refresh to check the current state.'}}
    elif kind in {'PreviewManualHistorySend','SendManualHistoryOnce','QueryManualHistorySend'} and features is not None:
        try:
            owner=getattr(features,'owner_history',None)
            if owner is None:raise NotImplementedError
            if kind=='PreviewManualHistorySend':
                event={'version':1,'id':command['id'],'type':'ManualHistoryPreview','payload':owner.preview(command['payload'])}
            elif kind=='QueryManualHistorySend':
                event={'version':1,'id':command['id'],'type':'ManualHistorySendState','payload':owner.query()}
            else:
                owner.begin(command['payload'])
                event={'version':1,'id':command['id'],'type':'Acknowledged','payload':{'operation':kind}}
        except NotImplementedError:
            event={'version':1,'id':command['id'],'type':'Error','payload':{'code':'unsupported','message':'Manual sending is unavailable in this build.'}}
        except ManualHistoryPreviewError as error:
            event={'version':1,'id':command['id'],'type':'Error','payload':{'code':'invalid_request','message':'manual_history:'+error.reason}}
        except Exception:
            event={'version':1,'id':command['id'],'type':'Error','payload':{'code':'invalid_request','message':'Manual send could not start. Check the saved chat, connection and job status.'}}
    elif kind in {'QueryTelegramTest','SendTelegramTest'} and features is not None:
        try:
            if features.owner_test is None: raise NotImplementedError
            if kind == 'QueryTelegramTest':
                event = {'version':1,'id':command['id'],'type':'TelegramTestState','payload':features.owner_test.query()}
            else:
                features.owner_test.begin(**command['payload'])
                event = {'version':1,'id':command['id'],'type':'Acknowledged','payload':{'operation':kind}}
        except NotImplementedError:
            event = {'version':1,'id':command['id'],'type':'Error','payload':{'code':'unsupported','message':'Telegram connection test unavailable.'}}
        except Exception:
            event = {'version':1,'id':command['id'],'type':'Error','payload':{'code':'invalid_request','message':'Choose your verified Test group after completing Telegram login.'}}
    elif kind in {'QuerySavedTelegramDestinations','SaveTelegramDestination'} and features is not None:
        try:
            adapter=features.destination_features
            if adapter is None:raise NotImplementedError
            if kind=='QuerySavedTelegramDestinations':
                event={'version':1,'id':command['id'],'type':'SavedTelegramDestinations','payload':adapter.query_saved()}
            else:
                adapter.save_saved(command['payload'])
                event={'version':1,'id':command['id'],'type':'Acknowledged','payload':{'operation':kind}}
        except NotImplementedError:
            event={'version':1,'id':command['id'],'type':'Error','payload':{'code':'unsupported','message':'Saved chats unavailable.'}}
        except Exception:
            event={'version':1,'id':command['id'],'type':'Error','payload':{'code':'invalid_request','message':'Verify the selected chat and current account before saving.'}}
    elif kind == 'QueryAppStatus' and features is not None and features.app_status is not None:
        event = {'version':1,'id':command['id'],'type':'AppStatus','payload':features.app_status()}
    elif kind in {'QueryTelegramDestinations','RefreshTelegramDestinations'} and features is not None:
        try:
            if features.listing is None:
                raise NotImplementedError
            if kind == 'QueryTelegramDestinations':
                event = {'version':1,'id':command['id'],'type':'TelegramDestinations','payload':features.listing.query(**command['payload'])}
            else:
                session=getattr(features,'session',None)
                with (session.authority_lock if session is not None else nullcontext()):
                    if any(getattr(owner,'busy',False) for owner in
                            (getattr(features,'owner_history',None),getattr(features,'owner_test',None),getattr(features,'owner_automatic',None))):
                        raise ValueError('Telegram send owns metadata verification')
                    features.listing.refresh(**command['payload'])
                event = {'version':1,'id':command['id'],'type':'Acknowledged','payload':{'operation':kind}}
        except NotImplementedError:
            event = {'version':1,'id':command['id'],'type':'Error','payload':{'code':'unsupported','message':'Destination browsing unavailable.'}}
        except Exception:
            event = {'version':1,'id':command['id'],'type':'Error','payload':{'code':'invalid_request','message':'Connect Telegram before refreshing destinations.'}}
    elif kind == "QueryReadiness" and readiness is not None:
        try:
            event = {"version": 1, "id": command["id"], "type": "Readiness", "payload": readiness(command["payload"])}
        except Exception:
            event = {"version": 1, "id": command["id"], "type": "Error", "payload": {"code": "invalid_request", "message": "Readiness could not be checked."}}
    elif kind == "Start" and features is not None:
        try:
            snapshot = (coordinator or controller).start(command["payload"])
            event = {"version": 1, "id": command["id"], "type": "State", "payload": snapshot}
        except Exception:
            event = {"version": 1, "id": command["id"], "type": "Error", "payload": {"code": "invalid_request", "message": "The session could not be started."}}
    elif kind in {"QueryState", "Pause", "Stop", "StopCollection", "SuspendCollection"}:
        owner = coordinator or controller
        snapshot = owner.tick() if kind == "QueryState" else owner.pause() if kind == "Pause" else owner.suspend_collection() if kind == "SuspendCollection" else owner.stop()
        event = {"version": 1, "id": command["id"], "type": "State", "payload": snapshot}
    elif features is not None and kind in {"QueryRouteQueue", "SetRouteQueuePaused", "ClearRouteQueue", "CancelRouteQueueItem", "QueryTopics", "QueryRoutes", "QueryHistory", "QueryReview", "QueryCollection", "QueryArchivePosts", "QueryArchiveDeliveries", "QueryArchiveRuns", "ReadArchiveDetail", "QueryDeliveryWorkspace", "SaveMessageTemplate", "ConfigureDeliveryDraft", "GetDeliveryDraft", "PreviewDeliveryDraft", "ApproveDeliveryDraft", "QueryDeliveryJobs", "PauseDeliveryJob", "ResumeDeliveryJob", "CancelDeliveryJob", "QueryAccount", "SaveTopic", "SaveRoute", "ApproveReview", "DismissReview", "BeginTelegramLogin", "CheckTelegramLogin", "RestoreTelegramSession", "CancelTelegramLogin", "LogoutTelegram", "RequestTelegramQr", "ResendTelegramCode", "VerifyDestination", "VerifySavedMessages", "SelectSender", "QueryDestination", "OpenThreadsLogin", "QueryRetention", "SaveRetention", "QueryAutoStatus", "SaveAutoPolicy", "OptInAutoRoute", "PreviewAutoRoute", "TestSample", "BulkApproveReview", "BulkDismissReview"}:
        try:
            queries = {"QueryTopics": ("Topics", features.topics), "QueryRoutes": ("Routes", features.routes),
                       "QueryHistory": ("History", features.history), "QueryReview": ("Review", features.review),
                       "QueryCollection": ("Collection", features.collection),
                       "QueryAccount": ("Account", features.account), "QueryRetention": ("Retention", features.retention)}
            archive_queries = {'QueryArchiveDeliveries': ('ArchiveDeliveries', 'archive_deliveries'), 'QueryArchivePosts': ('ArchivePosts', 'archive_posts'),
                               'QueryArchiveRuns': ('ArchiveRuns', 'archive_runs'),
                               'ReadArchiveDetail': ('ArchiveDetailChunk', 'archive_detail')}
            if kind in {'QueryDeliveryWorkspace', 'SaveMessageTemplate', 'ConfigureDeliveryDraft', 'GetDeliveryDraft', 'PreviewDeliveryDraft', 'ApproveDeliveryDraft', 'QueryDeliveryJobs', 'PauseDeliveryJob', 'ResumeDeliveryJob', 'CancelDeliveryJob'}:
                event_type, payload = features.delivery_workflow(kind, command['payload'])
                event = {'version':1,'id':command['id'],'type':event_type,'payload':payload}
            elif kind in archive_queries:
                event_type, method = archive_queries[kind]
                event = {'version':1,'id':command['id'],'type':event_type,'payload':getattr(features, method)(command['payload'])}
            elif kind in {'BulkApproveReview','BulkDismissReview'}:
                event = {'version':1,'id':command['id'],'type':'BulkReview','payload':features.bulk_review(command['payload'],dismiss=kind=='BulkDismissReview')}
            elif kind == 'QueryRouteQueue':
                event = {'version':1,'id':command['id'],'type':'RouteQueue','payload':features.route_queue(command['payload'])}
            elif kind == 'TestSample':
                if features.sample is None: raise NotImplementedError
                event = {'version':1,'id':command['id'],'type':'Sample','payload':features.sample(command['payload'])}
            elif kind == 'PreviewAutoRoute':
                event = {'version':1,'id':command['id'],'type':'AutoRoutePreview','payload':features.preview_auto_route(command['payload'])}
            elif kind == 'QueryAutoStatus':
                if features.automatic is None: raise NotImplementedError
                event = {'version':1,'id':command['id'],'type':'AutoStatus','payload':features.automatic.status()}
            elif kind == "QueryDestination":
                if features.destination_features is None: raise NotImplementedError
                event = {"version": 1, "id": command["id"], "type": "DestinationStatus", "payload": features.destination_features.query()}
            elif kind in queries:
                event_type, operation = queries[kind]
                event = {"version": 1, "id": command["id"], "type": event_type, "payload": operation()}
            else:
                if kind == 'SetRouteQueuePaused': features.set_route_queue_paused(**command['payload'])
                elif kind == 'ClearRouteQueue': features.clear_route_queue(**command['payload'])
                elif kind == 'CancelRouteQueueItem': features.cancel_route_queue_item(**command['payload'])
                elif kind == 'SaveRetention': features.save_retention(command['payload'])
                elif kind == 'SaveAutoPolicy':
                    if features.automatic is None: raise NotImplementedError
                    features.automatic.save(command['payload'])
                elif kind == 'OptInAutoRoute': features.save_route(command['payload']['route'], expected_auto_receipt=command['payload']['expected_receipt'])
                elif kind == "SaveTopic": features.save_topic(command["payload"])
                elif kind == "SaveRoute": features.save_route(command["payload"])
                elif kind == "ApproveReview": features.approve_review(command["payload"]["proposal"], command["payload"]["disclosure"])
                elif kind == "DismissReview": features.dismiss_review(command["payload"]["plan_id"])
                elif kind == "OpenThreadsLogin":
                    if features.threads_features is None: raise NotImplementedError
                    features.threads_features.open_login()
                elif kind in {"VerifyDestination", "VerifySavedMessages", "SelectSender"}:
                    adapter = features.destination_features
                    if adapter is None: raise NotImplementedError
                    if kind == "VerifyDestination": adapter.begin_verify(command["payload"])
                    elif kind == "VerifySavedMessages": adapter.begin_saved_messages()
                    else: adapter.begin_select(command["payload"])
                else:
                    adapter = features.account_features
                    if adapter is None: raise NotImplementedError
                    if kind in {"BeginTelegramLogin","CheckTelegramLogin"}:
                        if kind=='CheckTelegramLogin' or not adapter._handoff_failed and adapter.native_gate():
                            from .storage.outbox import _has_native_uncertainty
                            with adapter.auth._accounts.db.connection() as con:
                                if _has_native_uncertainty(con):raise _TelegramAuthOutcomeUnknown
                        if kind=='BeginTelegramLogin':adapter.begin()
                    elif kind == "RestoreTelegramSession":adapter.restore()
                    elif kind == "CancelTelegramLogin": adapter.cancel()
                    elif kind == "RequestTelegramQr": adapter.request_qr(command["payload"]["challenge_id"], command["payload"]["account_epoch"])
                    elif kind == "ResendTelegramCode": adapter.resend_code(command["payload"]["challenge_id"], command["payload"]["account_epoch"])
                    else: adapter.logout(command["payload"]["mode"])
                event = {"version": 1, "id": command["id"], "type": "Acknowledged", "payload": {"operation": kind}}
        except NotImplementedError:
            event = {"version": 1, "id": command["id"], "type": "Error", "payload": {"code": "unsupported", "message": "Unavailable in this build."}}
        except Exception as error:
            # No raw storage/model/TDLib exception or submitted content is exposed.
            reason=_ROUTE_SAVE_REASONS.get(str(error)) if kind in {'SaveRoute','OptInAutoRoute'} else None
            message='route_save:'+reason if reason else 'The operation could not be completed. Review current details and try again.'
            if isinstance(error,_TelegramAuthOutcomeUnknown):message='telegram_auth:outcome_unknown'
            elif kind=='BeginTelegramLogin':
                reason={'Authentication already started':'already_started','Authentication unavailable':'profile_unavailable',
                    'Saved account isolation proof unavailable':'isolation_unavailable'}.get(str(error))
                if reason:message='telegram_auth:'+reason
            event = {"version": 1, "id": command["id"], "type": "Error", "payload": {"code": "invalid_request", "message": message}}
    else:
        event = {"version": 1, "id": command["id"], "type": "Error", "payload": {
            "code": "unsupported", "message": "Setup and session start are not available in this build.",
        }}
    try:
        validate_contract("Event", event)
        encode_frame(event)  # Oversized or malformed local projections fail closed.
    except ProtocolError:
        event = {"version": 1, "id": command["id"], "type": "Error", "payload": {
            "code": "internal_error", "message": "Local data could not be returned within the protocol boundary."}}
        validate_contract("Event", event)
    return event


def serve_one(
    source: BinaryIO, destination: BinaryIO,
    handler: Callable[[Command], Event] = dispatch,
) -> bool:
    command = read_frame(source)
    if command is None:
        return False
    validate_contract("Command", command)
    event = handler(command)
    validate_contract("Event", event)
    destination.write(encode_frame(event))
    destination.flush()
    return True


def main() -> int:
    # The private parent is probed independently of blocking IPC reads: a dead
    # desktop cannot leave an active collector in this worker running forever.
    import os
    from threading import Event as ThreadEvent, Thread

    controller = SessionController()
    done = ThreadEvent()
    try:
        with ParentProcess() as parent:
            watchdog = ParentWatchdog(controller, parent_alive=parent.alive, shutdown=lambda: os._exit(2))

            def monitor() -> None:
                while not done.is_set():
                    if not watchdog.poll():
                        return
                    done.wait(0.25)  # production polling only; injected poll tests never sleep.

            thread = Thread(target=monitor, name="private-parent-watchdog", daemon=True)
            thread.start()
            try:
                while serve_one(sys.stdin.buffer, sys.stdout.buffer, lambda command: dispatch(command, controller=controller)):
                    pass
            finally:
                controller.stop()
                done.set()
                thread.join(timeout=1)
    except (ProtocolError, OSError):
        # No raw command, frame, post, path or secrets in diagnostics.
        print("Private IPC rejected; worker stopped.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
