"""Persistent connection/manual-send provenance authenticated by the database key.

Only a brand-new empty private profile can acquire this purpose. A copied,
unmarked or tampered database is never adopted. A consumed owner-test profile
can resume only after its sole message succeeded with exact native correlation.
History jobs can reuse a live authenticated client only after exact native
success and durable database completion; neither purpose grants general sends.
"""

import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import math
from copy import deepcopy
from uuid import UUID

from .private_directory import private_directory

MARKER = "connection-purpose.json"
_DOMAIN = b"ThreadRelay/connection-only-profile/v1\0"


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate profile field")
        value[key] = item
    return value


class ConnectionProfile:
    def __init__(self, path, descriptor, contents, key_digest, signer):
        self._descriptor = self._directory_fd = self._files_fd = None
        self.path = path
        self._contents = contents
        self._manual_finalized = False
        self._automatic_finalized = False
        self.key_digest = key_digest
        self._signer = signer
        self._identity = self._file_identity(os.fstat(descriptor))
        self._directory = os.stat(path, follow_symlinks=False)
        self._directory_fd = os.open(
            path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        private_directory(path / "files")
        self._files_fd = os.open(
            path / "files", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        self._files = os.fstat(self._files_fd)
        self._descriptor = descriptor

    @property
    def purpose(self):
        return json.loads(self._contents)['body']['purpose']

    @property
    def profile_id(self):
        return json.loads(self._contents)['body']['profile_id']

    def owner_test_record(self):
        return deepcopy(json.loads(self._contents)['body'].get('owner_test'))

    def manual_history_record(self):
        return deepcopy(json.loads(self._contents)['body'].get('manual_history'))

    def automatic_record(self):
        return deepcopy(json.loads(self._contents)['body'].get('automatic'))

    def _replace_marker(self, body):
        """Publish a durable authenticated transition before returning authority."""
        self.recheck()
        if self._signer is None:
            raise ValueError('Closed profile purpose')
        digest = self._signer.copy(); digest.update(_canonical(body))
        contents = _canonical({'body':body,'mac':digest.hexdigest()})
        if len(contents)>4096:
            raise ValueError('Profile purpose too large')
        name = '.connection-purpose-' + secrets.token_hex(16)
        descriptor = None
        replacement = None
        try:
            descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                                 0o600, dir_fd=self._directory_fd)
            remaining = memoryview(contents)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:raise ValueError('Profile purpose write failed')
                remaining = remaining[written:]
            os.fsync(descriptor)
            self.recheck()
            os.replace(name, MARKER, src_dir_fd=self._directory_fd, dst_dir_fd=self._directory_fd)
            os.fsync(self._directory_fd)
            replacement = os.open(MARKER, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                                  dir_fd=self._directory_fd)
            identity = self._file_identity(os.fstat(replacement))
            os.close(self._descriptor)
            self._descriptor, self._identity, self._contents = replacement, identity, contents
            replacement = None
            self.recheck()
        except BaseException:
            # A replacement may already be durable. Never use the old in-memory
            # assertion after any uncertain transition; pending stays quarantined.
            self.close()
            raise
        finally:
            if descriptor is not None:os.close(descriptor)
            if replacement is not None:os.close(replacement)
            if self._directory_fd is not None:
                try:os.unlink(name, dir_fd=self._directory_fd)
                except OSError:pass

    def begin_owner_test(self, binding):
        body = json.loads(self._contents)['body']
        if body['purpose'] != 'connection_only' or body['ever_sent'] is not False:
            raise ValueError('Owner test already consumed')
        _validate_test_binding(binding)
        if binding['profile_id'] != body['profile_id']:
            raise ValueError('Owner test profile mismatch')
        body.update(schema_version=2, purpose='owner_test_pending', ever_sent=True, owner_test=deepcopy(binding))
        self._replace_marker(body)

    def complete_owner_test_sent(self, binding, temporary_message_id, final_message_id, *, closed_client_id=None, active_client_id=None):
        body = json.loads(self._contents)['body']
        client_id=active_client_id if active_client_id is not None else closed_client_id
        if (body['purpose'] != 'owner_test_pending' or body.get('owner_test') != binding
                or (active_client_id is not None and closed_client_id is not None)
                or type(client_id) is not int or client_id != binding['client_id']
                or not _identifier(temporary_message_id) or not _identifier(final_message_id)):
            raise ValueError('Owner test completion mismatch')
        body.update(schema_version=5 if active_client_id is not None else 2,purpose='owner_test_sent', owner_test={**binding,
                    'temporary_message_id':temporary_message_id,'final_message_id':final_message_id,'closed':active_client_id is None})
        self._replace_marker(body)
        if active_client_id is None:self._signer = None

    def begin_manual_history(self, binding):
        body=json.loads(self._contents)['body']
        if self._manual_finalized or self._automatic_finalized or body['purpose'] not in {'connection_only','owner_test_sent','manual_history_sent','automatic_sent','automatic_failed'}:
            raise ValueError('Manual History profile unavailable')
        _validate_history_binding(binding)
        if binding['profile_id']!=body['profile_id']:
            raise ValueError('Manual History profile mismatch')
        previous=body.get('manual_history')
        if previous is not None and (previous['job_id']==binding['job_id']
                or previous['delivery_id']==binding['delivery_id'] or previous['attempt_id']==binding['attempt_id']):
            raise ValueError('Manual History already consumed')
        body.update(schema_version=5 if body['schema_version']==5 else 3,purpose='manual_history_pending',ever_sent=True,manual_history=deepcopy(binding))
        self._replace_marker(body)

    def complete_manual_history_sent(self, binding, temporary_message_id, final_message_id, *, closed_client_id=None, active_client_id=None):
        body=json.loads(self._contents)['body']
        client_id=active_client_id if active_client_id is not None else closed_client_id
        if (body['purpose']!='manual_history_pending' or body.get('manual_history')!=binding
                or (active_client_id is not None and closed_client_id is not None)
                or type(client_id) is not int or client_id!=binding['client_id']
                or not _identifier(temporary_message_id) or not _identifier(final_message_id)):
            raise ValueError('Manual History completion mismatch')
        body.update(schema_version=5 if body['schema_version']==5 else 4 if active_client_id is not None else 3,
            purpose='manual_history_sent',manual_history={**binding,
            'temporary_message_id':temporary_message_id,'final_message_id':final_message_id,'closed':active_client_id is None})
        self._replace_marker(body)
        # Block further permits until the owning controller has durably finalized
        # SQLite too. The signer remains available to quarantine uncertain work.
        self._manual_finalized=True

    def finalize_manual_history_sent(self, binding):
        """Release live-session reuse only after the controller commits SQLite."""
        self.recheck()
        body=json.loads(self._contents)['body']
        record=body.get('manual_history')
        completion={'temporary_message_id','final_message_id','closed'}
        if (not self._manual_finalized or body['schema_version'] not in {4,5}
                or body['purpose']!='manual_history_sent' or not isinstance(record,dict)
                or record.get('closed') is not False
                or {key:value for key,value in record.items() if key not in completion}!=binding):
            raise ValueError('Manual History finalization mismatch')
        self._manual_finalized=False

    def quarantine_manual_history(self, binding):
        body=json.loads(self._contents)['body']
        _validate_history_binding(binding)
        record=body.get('manual_history')
        completion={'temporary_message_id','final_message_id','closed'}
        if (body['purpose'] not in {'manual_history_pending','manual_history_sent'}
                or not isinstance(record,dict)
                or {key:value for key,value in record.items() if key not in completion}!=binding):
            raise ValueError('Manual History quarantine mismatch')
        body.update(schema_version=5 if body['schema_version']==5 else 3,purpose='manual_history_unknown',ever_sent=True)
        self._replace_marker(body)
        self._signer=None

    def begin_automatic(self, binding):
        body=json.loads(self._contents)['body']
        if (self._manual_finalized or self._automatic_finalized
                or body['purpose'] not in {'connection_only','owner_test_sent','manual_history_sent','automatic_sent','automatic_failed'}):
            raise ValueError('Automatic profile unavailable')
        _validate_automatic_binding(binding)
        if binding['profile_id']!=body['profile_id']:
            raise ValueError('Automatic profile mismatch')
        previous=body.get('automatic')
        if previous is not None and any(previous[key]==binding[key] for key in ('delivery_id','attempt_id','request_extra')):
            raise ValueError('Automatic attempt already consumed')
        body.update(schema_version=5,purpose='automatic_pending',ever_sent=True,automatic=deepcopy(binding))
        self._replace_marker(body)

    def complete_automatic_sent(self, binding, temporary_message_id, final_message_id, *, active_client_id):
        body=json.loads(self._contents)['body']
        if (body['purpose']!='automatic_pending' or body.get('automatic')!=binding
                or type(active_client_id) is not int or active_client_id!=binding['client_id']
                or not _identifier(temporary_message_id) or not _identifier(final_message_id)):
            raise ValueError('Automatic completion mismatch')
        body.update(purpose='automatic_sent',automatic={**binding,
            'temporary_message_id':temporary_message_id,'final_message_id':final_message_id,'closed':False})
        self._replace_marker(body)
        self._automatic_finalized=True

    def finalize_automatic_sent(self, binding):
        """Release live-session reuse only after the Auto owner commits SQLite."""
        self.recheck()
        body=json.loads(self._contents)['body'];record=body.get('automatic')
        completion={'temporary_message_id','final_message_id','closed'}
        if (not self._automatic_finalized or body['purpose']!='automatic_sent'
                or not isinstance(record,dict) or record.get('closed') is not False
                or {key:value for key,value in record.items() if key not in completion}!=binding):
            raise ValueError('Automatic finalization mismatch')
        self._automatic_finalized=False

    def complete_automatic_failed(self, binding, temporary_message_id, failed_message_id, error_code, error_reason, retry_after, *, active_client_id):
        body=json.loads(self._contents)['body']
        record={**binding,'temporary_message_id':temporary_message_id,'failed_message_id':failed_message_id,
            'error_code':error_code,'error_reason':error_reason,'retry_after':retry_after,'closed':False,
            'failure_kind':'request_rejected' if temporary_message_id is None and failed_message_id is None else 'message_failed'}
        _validate_automatic_failure(record,body['profile_id'])
        if (body['purpose']!='automatic_pending' or body.get('automatic')!=binding
                or type(active_client_id) is not int or active_client_id!=binding['client_id']):
            raise ValueError('Automatic failure mismatch')
        body.update(purpose='automatic_failed',automatic=record)
        self._replace_marker(body)
        self._automatic_finalized=True

    def finalize_automatic_failed(self, binding):
        self.recheck()
        body=json.loads(self._contents)['body'];record=body.get('automatic')
        completion={'temporary_message_id','failed_message_id','error_code','error_reason','retry_after','closed','failure_kind'}
        if (not self._automatic_finalized or body['purpose']!='automatic_failed' or not isinstance(record,dict)
                or {key:value for key,value in record.items() if key not in completion}!=binding):
            raise ValueError('Automatic failure finalization mismatch')
        self._automatic_finalized=False

    def quarantine_automatic(self, binding):
        body=json.loads(self._contents)['body'];record=body.get('automatic')
        _validate_automatic_binding(binding)
        completion={'temporary_message_id','final_message_id','failed_message_id','error_code','error_reason','retry_after','closed','failure_kind'}
        if (body['purpose'] not in {'automatic_pending','automatic_sent','automatic_failed'} or not isinstance(record,dict)
                or {key:value for key,value in record.items() if key not in completion}!=binding):
            raise ValueError('Automatic quarantine mismatch')
        body.update(purpose='automatic_unknown',ever_sent=True)
        self._replace_marker(body)
        self._signer=None

    @staticmethod
    def _file_identity(info):
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise ValueError("Private profile purpose unavailable")
        return info.st_dev, info.st_ino

    def recheck(self):
        if self._descriptor is None:
            raise ValueError("Closed profile purpose")
        private_directory(self.path)
        current = os.stat(self.path, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (
            self._directory.st_dev,
            self._directory.st_ino,
        ):
            raise ValueError("Replaced profile directory")
        private_directory(self.path / "files")
        files = (self.path / "files").lstat()
        if (files.st_dev, files.st_ino) != (self._files.st_dev, self._files.st_ino):
            raise ValueError("Replaced profile files directory")
        marker = self.path / MARKER
        if (
            self._file_identity(marker.lstat()) != self._identity
            or self._file_identity(os.fstat(self._descriptor)) != self._identity
            or os.pread(self._descriptor, 4097, 0) != self._contents
        ):
            raise ValueError("Replaced profile purpose")

    def close(self):
        self._signer = None
        for name in ("_descriptor", "_directory_fd", "_files_fd"):
            descriptor = getattr(self, name, None)
            if descriptor is not None:
                os.close(descriptor)
                setattr(self, name, None)

    def __del__(self):
        self.close()


def _identifier(value):
    try:return isinstance(value,str) and str(int(value)) == value and 0 < abs(int(value)) < 2**53
    except (ValueError, TypeError):return False


def _validate_test_binding(value):
    fields = {'attempt_id','request_extra','account_id','account_epoch','chat_id','sender_id',
              'destination_version','content_hash','generation','client_id','profile_id'}
    if not isinstance(value,dict) or set(value) != fields:
        raise ValueError('Invalid owner test binding')
    if (any(not isinstance(value[key],str) or len(value[key]) != size
            or any(c not in '0123456789abcdef' for c in value[key])
            for key,size in (('attempt_id',32),('profile_id',64),('content_hash',64)))
            or value['request_extra'] != 'owner-test:' + value['attempt_id']
            or any(not _identifier(value[key]) for key in ('account_id','chat_id','sender_id'))
            or int(value['account_id']) <= 0 or value['sender_id'] != value['account_id']
            or type(value['account_epoch']) is not int or value['account_epoch'] < 0
            or any(type(value[key]) is not int or value[key] < 1 for key in ('generation','client_id','destination_version'))):
        raise ValueError('Invalid owner test binding')


def _validate_history_binding(value):
    extra={'job_id','job_version','delivery_id'}
    if (not isinstance(value,dict) or not extra<=set(value)
            or not isinstance(value['job_id'],str) or not 1<=len(value['job_id'])<=128
            or type(value['job_version']) is not int or value['job_version']<1
            or not isinstance(value['delivery_id'],str) or len(value['delivery_id'])!=32
            or any(c not in '0123456789abcdef' for c in value['delivery_id'])
            or value.get('request_extra')!='owner-history:'+str(value.get('attempt_id'))):
        raise ValueError('Invalid manual History binding')
    base={key:item for key,item in value.items() if key not in extra}
    base['request_extra']='owner-test:'+str(value.get('attempt_id'))
    _validate_test_binding(base)


def _validate_automatic_binding(value):
    extra={'delivery_id','plan_id','policy_hash'}
    if not isinstance(value,dict) or not extra<=set(value):
        raise ValueError('Invalid automatic binding')
    for key in ('delivery_id','attempt_id','request_extra'):
        candidate=value.get(key)
        try:valid=isinstance(candidate,str) and str(UUID(candidate))==candidate
        except (ValueError,TypeError,AttributeError):valid=False
        if not valid:raise ValueError('Invalid automatic binding')
    if any(not isinstance(value[key],str) or len(value[key])!=64
           or any(c not in '0123456789abcdef' for c in value[key]) for key in ('plan_id','policy_hash')):
        raise ValueError('Invalid automatic binding')
    base={key:item for key,item in value.items() if key not in extra}
    # Reuse the shared account/profile/text/route identity checks, while keeping
    # automatic UUID authority distinct from manual's one-use request namespace.
    base['attempt_id']='a'*32;base['request_extra']='owner-test:'+'a'*32
    _validate_test_binding(base)
    if int(value['chat_id'])>=0:raise ValueError('Automatic saved-group route required')


def _validate_completion(record, profile_id, *, history=False, automatic=False, active=False):
    completion={'temporary_message_id','final_message_id','closed'}
    if not isinstance(record,dict) or not completion<=set(record):
        raise ValueError('Invalid manual completion')
    validator=_validate_automatic_binding if automatic else _validate_history_binding if history else _validate_test_binding
    validator({key:value for key,value in record.items() if key not in completion})
    if (record['closed'] is not (False if active else True) or record['profile_id']!=profile_id
            or not _identifier(record['temporary_message_id']) or not _identifier(record['final_message_id'])):
        raise ValueError('Invalid manual completion')



def _validate_automatic_failure(record, profile_id):
    completion={'temporary_message_id','failed_message_id','error_code','error_reason','retry_after','closed','failure_kind'}
    if not isinstance(record,dict) or not completion<=set(record):
        raise ValueError('Invalid automatic failure')
    _validate_automatic_binding({key:value for key,value in record.items() if key not in completion})
    if (record['profile_id']!=profile_id or record['closed'] is not False
            or not ((record['failure_kind']=='message_failed' and _identifier(record['temporary_message_id']) and _identifier(record['failed_message_id']) and record['temporary_message_id']!=record['failed_message_id'])
                or (record['failure_kind']=='request_rejected' and record['temporary_message_id'] is None and record['failed_message_id'] is None and record['error_code'] in {400,403,420,429}))
            or type(record['error_code']) is not int or not 0<=record['error_code']<=999
            or record['error_reason'] not in {'FLOOD_WAIT','FLOOD_PREMIUM_WAIT','SLOWMODE_WAIT','TELEGRAM_ERROR'}
            or type(record['retry_after']) not in (int,float) or not math.isfinite(record['retry_after'])
            or not 0<=record['retry_after']<=100000000):
        raise ValueError('Invalid automatic failure')

def connection_only_profile(directory, account_ref, database_key):
    directory = Path(directory)
    if (
        os.name != "posix"
        or not isinstance(database_key, bytes)
        or len(database_key) != 32
    ):
        raise ValueError("Connection-only profile unavailable")
    private_directory(directory)
    marker = directory / MARKER
    account = hashlib.sha256(account_ref.encode("ascii")).hexdigest()
    if not marker.exists():
        if any(directory.iterdir()):
            raise ValueError("Unknown or send-capable profile requires reconciliation")
        body = {
            "schema_version": 1,
            "purpose": "connection_only",
            "ever_sent": False,
            "policy_version": 1,
            "account_reference": account,
            "profile_id": secrets.token_hex(32),
        }
        value = {
            "body": body,
            "mac": hmac.digest(
                database_key, _DOMAIN + _canonical(body), "sha256"
            ).hex(),
        }
        contents = _canonical(value)
        fd = os.open(
            marker,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        try:
            with os.fdopen(fd, "wb", closefd=False) as output:
                output.write(contents)
                output.flush()
                os.fsync(fd)
        finally:
            os.close(fd)
    descriptor = os.open(
        marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    )
    try:
        contents = os.pread(descriptor, 4097, 0)
        if len(contents) > 4096:
            raise ValueError("Profile purpose too large")
        value = json.loads(contents, object_pairs_hook=_unique)
        body = value["body"]
        purpose=body.get('purpose')
        extra=({'owner_test'} if purpose=='owner_test_sent' else
               {'manual_history'} | ({'owner_test'} if 'owner_test' in body else set()) |
                    ({'automatic'} if 'automatic' in body else set()) if purpose=='manual_history_sent' else
               {'automatic'} | ({'owner_test'} if 'owner_test' in body else set()) |
                    ({'manual_history'} if 'manual_history' in body else set()) if purpose in {'automatic_sent','automatic_failed'} else set())
        if (
            set(value) != {"body", "mac"}
            or set(body)
            != ({
                "schema_version",
                "purpose",
                "ever_sent",
                "policy_version",
                "account_reference",
                "profile_id",
            } | extra)
            or type(body['schema_version']) is not int
            or body["schema_version"] not in ({3,4,5} if purpose=='manual_history_sent' else {5} if purpose in {'automatic_sent','automatic_failed'} else {2,5} if purpose=='owner_test_sent' else {1})
            or body["policy_version"] != 1
            or purpose not in {'connection_only','owner_test_sent','manual_history_sent','automatic_sent','automatic_failed'}
            or body["ever_sent"] is not (purpose!='connection_only')
            or body["account_reference"] != account
            or not isinstance(body["profile_id"], str)
            or len(body["profile_id"]) != 64
            or any(c not in "0123456789abcdef" for c in body["profile_id"])
            or not isinstance(value["mac"], str)
            or not hmac.compare_digest(
                value["mac"],
                hmac.digest(database_key, _DOMAIN + _canonical(body), "sha256").hex(),
            )
        ):
            raise ValueError("Unrecognized profile purpose")
        if 'owner_test' in body:
            if type(body['owner_test'].get('closed')) is not bool:raise ValueError('Invalid owner test completion')
            _validate_completion(body['owner_test'],body['profile_id'],active=body['schema_version']==5 and body['owner_test']['closed'] is False)
        if 'manual_history' in body:
            if body['schema_version']<5:
                active=body['schema_version']==4
            else:
                if type(body['manual_history'].get('closed')) is not bool:raise ValueError('Invalid manual completion')
                active=body['manual_history']['closed'] is False
            _validate_completion(body['manual_history'],body['profile_id'],history=True,active=active)
        if 'automatic' in body:
            if body['schema_version']!=5:raise ValueError('Invalid automatic schema')
            if 'failed_message_id' in body['automatic']:
                _validate_automatic_failure(body['automatic'],body['profile_id'])
            else:
                _validate_completion(body['automatic'],body['profile_id'],automatic=True,active=True)
        guard = ConnectionProfile(
            directory, descriptor, contents, hashlib.sha256(database_key).digest(),
            hmac.new(database_key, _DOMAIN, digestmod='sha256')
        )
        descriptor = None
        guard.recheck()
        return guard
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise
