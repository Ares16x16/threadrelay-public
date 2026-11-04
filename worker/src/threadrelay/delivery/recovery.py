"""Explicit profile isolation. Unknown outcomes are retained and never retried."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re

from threadrelay.storage.database import encode
from threadrelay.storage.isolation import commit_isolation,is_isolated_delivery,records,valid_receipt,account_matches

INTENT='telegram-recovery-intent'


class TelegramRecovery:
    def __init__(self,root,auth,client,session,*,quarantine=None,verify=None,stop_collection=None,reset_owners=(),clock):
        self.root=Path(root);self.auth,self.client,self.session=auth,client,session
        self.quarantine,self.verify=quarantine,verify
        self.stop_collection=stop_collection or session.stop
        self.reset_owners=reset_owners;self.clock=clock
        self.db=auth._accounts.db
        self._state='blocked';self._reason='not_started';self._plan=None;self._deadline=None
        with self.db.connection() as con:
            row=con.execute('SELECT payload FROM local_policy WHERE name=?',(INTENT,)).fetchone()
            if row:
                value=json.loads(row[0]);self._plan=value['plan']
                self._state='ready_to_login' if value['state']=='ready_to_login' else 'blocked'
                self._reason=None if self._state=='ready_to_login' else 'recovery_interrupted'

    def query(self):
        with self.session.authority_lock:
            result={'state':self._state}
            if self._reason is not None:result['reason']=self._reason
            return result

    def _persist(self):
        with self.db.transaction() as con:
            con.execute('INSERT INTO local_policy(name,payload) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload',
                (INTENT,encode({'state':self._state,'plan':self._plan})))

    def _marker(self,reference):
        directory=self.root/hashlib.sha256(reference.encode('ascii')).hexdigest()
        if directory.is_symlink() or not directory.is_dir():raise ValueError('profile_unavailable')
        descriptor=os.open(directory/'connection-purpose.json',os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
        try:data=os.read(descriptor,4097)
        finally:os.close(descriptor)
        if len(data)>4096:raise ValueError('profile_unavailable')
        value=json.loads(data);body=value['body']
        return body,hashlib.sha256(data).hexdigest(),data.decode('utf-8')

    def _make_plan(self,delivery_id):
        with self.db.connection() as con:
            row=con.execute('SELECT * FROM deliveries WHERE id=?',(delivery_id,)).fetchone()
            if row is None or row['state']!='unknown':raise ValueError('delivery_not_unknown')
            if is_isolated_delivery(con,delivery_id):return None
            saved=json.loads(row['payload'])
            account=con.execute('SELECT * FROM accounts WHERE account_id=? AND epoch=? AND active=1',
                (row['account_id'],row['account_epoch'])).fetchone()
            if account is None or not account['vault_ref'] or json.loads(account['snapshot'])['revocation_pending']:
                raise ValueError('account_changed')
            attempt=con.execute('SELECT * FROM telegram_attempts WHERE attempt_id=?',(saved['attempt_id'],)).fetchone()
            if attempt is None or attempt['state']!='unknown':raise ValueError('attempt_changed')
            # A retry after interrupted native rename uses the exact durable
            # intent; the native journal independently verifies its old source.
            old=self._plan
            if (old and old['delivery_id']==delivery_id and old['delivery_payload']==row['payload']
                    and old['attempt']==dict(attempt) and old.get('native_requested') is True):return deepcopy(old)
            body,digest,contents=self._marker(account['vault_ref'])
            if body.get('account_reference')!=hashlib.sha256(account['vault_ref'].encode('ascii')).hexdigest():
                raise ValueError('profile_binding_changed')
            binding=body.get('automatic') if body.get('purpose','').startswith('automatic_') else body.get('manual_history')
            text=con.execute('SELECT text FROM route_queue WHERE delivery_id=?',(delivery_id,)).fetchone()
            if text is None:
                text=con.execute('SELECT j.text FROM workflow_jobs j JOIN workflow_delivery_context c ON c.job_id=j.id WHERE c.delivery_id=?',(delivery_id,)).fetchone()
            outgoing_hash=hashlib.sha256(text[0].encode('utf-8')).hexdigest() if text and isinstance(text[0],str) else None
            if (not isinstance(binding,dict) or not re.fullmatch(r'[a-f0-9]{64}',body.get('profile_id',''))
                    or any(binding.get(a)!=saved[b] for a,b in (('delivery_id','id'),('attempt_id','attempt_id'),
                        ('request_extra','request_extra'),('account_id','account_id'),('account_epoch','account_epoch'),
                        ('chat_id','destination_id')))
                    or outgoing_hash is None or binding.get('content_hash')!=outgoing_hash
                    or binding.get('sender_id')!=saved['sender']['id'] or binding.get('profile_id')!=body['profile_id']):
                raise ValueError('profile_binding_changed')
            latches=[dict(item) for item in con.execute('SELECT name,payload FROM local_policy WHERE name IN (?,?)',
                ('automatic-incomplete:'+body['profile_id'],'manual-history-incomplete:'+body['profile_id']))]
            return {'delivery_id':delivery_id,'delivery_payload':row['payload'],'attempt':dict(attempt),'latches':latches,
                'account_id':row['account_id'],'account_epoch':row['account_epoch'],'vault_ref':account['vault_ref'],
                'old_profile_id':body['profile_id'],'old_marker_sha256':digest,'marker_contents':contents,
                'outgoing_text_hash':outgoing_hash,'native_requested':False}

    def begin(self,delivery_id):
        with self.session.authority_lock:
            if self._state=='closing':return self.query()
            try:
                if self.quarantine is None or self.verify is None:raise ValueError('quarantine_unavailable')
                plan=self._make_plan(delivery_id)
                if plan is None:
                    self.verify_for_login()
                    for owner in self.reset_owners:owner.reset_after_isolation(delivery_id)
                    self._state='ready_to_login';self._reason=None
                    if self._plan is not None:self._persist()
                    return self.query()
            except Exception as error:
                self._state='blocked';self._reason=str(error) if str(error) in {'delivery_not_unknown','account_changed','attempt_changed','profile_binding_changed','profile_unavailable','quarantine_unavailable'} else 'profile_unavailable'
                return self.query()
        self.stop_collection()
        with self.session.authority_lock:
            self._plan=plan;self._state='closing';self._reason=None;self._deadline=self.clock()+30
            self._persist()
            underlying=self.client._client if hasattr(self.client,'_client') else self.client
            if underlying is not None and underlying._binding is not None and not underlying._closed:
                self.auth.cancel()
            else:self.auth._latch_terminal(revoke=False)
            return self.query()

    def tick(self):
        with self.session.authority_lock:
            if self._state!='closing':return
            underlying=self.client._client if hasattr(self.client,'_client') else self.client
            if underlying is not None and underlying._binding is not None and not underlying._closed:
                if self.clock()>=self._deadline:
                    self._state='failed';self._reason='native_client_not_closed';self._persist()
                return
            try:
                if underlying is not None and underlying._profile_guard is not None:underlying._profile_guard.close()
                if not self._plan['native_requested']:
                    body,digest,contents=self._marker(self._plan['vault_ref'])
                    if body['profile_id']!=self._plan['old_profile_id']:raise ValueError('profile_binding_changed')
                    self._plan['old_marker_sha256']=digest
                    self._plan['marker_contents']=contents
                    self._plan['native_requested']=True
                    self._persist()
                receipt=self.quarantine(self._plan['old_profile_id'],self._plan['old_marker_sha256'])
                verified=self.verify(receipt['recovery_id'])
                if not valid_receipt(receipt) or verified!=receipt:raise ValueError('quarantine_verification_failed')
                with self.db.transaction() as con:commit_isolation(con,self._plan,receipt)
                # Reset only ephemeral owners tied to the physically isolated
                # attempt. Durable Unknown rows and deduplication remain intact.
                for owner in self.reset_owners:owner.reset_after_isolation(self._plan['delivery_id'])
                if underlying is not None and underlying._binding is None and hasattr(self.client,'_client'):
                    self.client._client=None
                self.auth._snapshot=replace(self.auth.snapshot(),auth_state='closed',network_state='offline')
                self.auth.status='closed'
                self._state='ready_to_login';self._reason=None;self._persist()
            except Exception:
                self._state='failed';self._reason='quarantine_verification_failed';self._persist()

    def verify_for_login(self):
        with self.db.connection() as con:
            values=records(con)
            if any(not account_matches(con,value) for value in values):raise ValueError('account_changed')
        if not values:return
        for value in values:
            receipt=value['receipt']
            if self.verify is None or self.verify(receipt['recovery_id'])!=receipt:
                raise ValueError('quarantine_verification_failed')

    def profile_allowed(self,profile,directory):
        with self.db.connection() as con:values=records(con)
        if not values:return True
        if profile.profile_id in {value['receipt']['old_profile_id'] for value in values}:return False
        expected=self.root/hashlib.sha256(values[-1]['vault_ref'].encode('ascii')).hexdigest()
        if directory!=expected:return False
        self.verify_for_login()
        # Native verification binds the current canonical inode through signed
        # recovery/reseed edges. The guard independently pins this open profile.
        profile.recheck()
        return True
