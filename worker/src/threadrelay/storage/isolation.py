"""Exact inert-attempt snapshots, accepted only after native profile quarantine."""
import json
import re
import hashlib

from .database import encode

PREFIX='telegram-isolation:'


def valid_receipt(value):
    keys={'recovery_id','old_profile_id','old_marker_sha256','archive_id','new_directory_device','new_directory_inode'}
    return (isinstance(value,dict) and set(value)==keys
        and all(isinstance(value[k],str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}',value[k]) for k in ('recovery_id','archive_id'))
        and all(isinstance(value[k],str) and re.fullmatch(r'[a-f0-9]{64}',value[k]) for k in ('old_profile_id','old_marker_sha256'))
        and all(type(value[k]) is int and 0<=value[k]<2**64 for k in ('new_directory_device','new_directory_inode')))


def records(con):
    values=[]
    for row in con.execute('SELECT payload FROM local_policy WHERE name GLOB ?',(PREFIX+'*',)):
        value=json.loads(row[0])
        if (not isinstance(value,dict) or not valid_receipt(value.get('receipt'))
                or type(value.get('sequence')) is not int or value['sequence']<1
                or not isinstance(value.get('delivery_payload'),str) or not isinstance(value.get('attempt'),dict)
                or not isinstance(value.get('latches'),list) or not bound_snapshot(value)):
            raise ValueError('Invalid isolated attempt proof')
        values.append(value)
    return sorted(values,key=lambda value:value['sequence'])


def bound_snapshot(value):
    try:
        receipt=value['receipt'];contents=value['marker_contents'];saved=json.loads(value['delivery_payload'])
        body=json.loads(contents)['body'];binding=body.get('automatic') if body['purpose'].startswith('automatic_') else body.get('manual_history')
        return (isinstance(contents,str) and len(contents)<=4096
            and hashlib.sha256(contents.encode('utf-8')).hexdigest()==receipt['old_marker_sha256']
            and body['profile_id']==receipt['old_profile_id']
            and body['account_reference']==hashlib.sha256(value['vault_ref'].encode('ascii')).hexdigest()
            and saved['account_id']==value['account_id'] and saved['account_epoch']==value['account_epoch']
            and isinstance(binding,dict) and binding.get('profile_id')==body['profile_id']
            and binding.get('sender_id')==saved['sender']['id']
            and binding.get('content_hash')==value['outgoing_text_hash']
            and all(binding.get(a)==saved[b] for a,b in (('delivery_id','id'),('attempt_id','attempt_id'),
                ('request_extra','request_extra'),('account_id','account_id'),('account_epoch','account_epoch'),
                ('chat_id','destination_id'))))
    except (KeyError,TypeError,ValueError,AttributeError):return False


def isolated_work(con):
    deliveries={};attempts={};latches={}
    for value in records(con):
        if not account_matches(con,value):continue
        saved=json.loads(value['delivery_payload']);attempt=value['attempt']
        if (saved.get('state')!='unknown' or saved.get('attempt_id')!=attempt.get('attempt_id')
                or saved.get('request_extra')!=attempt.get('request_extra') or attempt.get('state')!='unknown'
                or saved.get('id')!=attempt.get('delivery_id')):continue
        row=con.execute('SELECT payload,state FROM deliveries WHERE id=?',(saved['id'],)).fetchone()
        current=con.execute('SELECT * FROM telegram_attempts WHERE attempt_id=?',(attempt['attempt_id'],)).fetchone()
        if (row is None or row['state']!='unknown' or row['payload']!=value['delivery_payload']
                or current is None or dict(current)!=attempt):continue
        deliveries[saved['id']]=value['delivery_payload'];attempts[attempt['attempt_id']]=attempt
        for latch in value['latches']:
            name=latch.get('name');payload=latch.get('payload')
            if (name in {'automatic-incomplete:'+value['receipt']['old_profile_id'],
                    'manual-history-incomplete:'+value['receipt']['old_profile_id']} and isinstance(payload,str)):
                binding=json.loads(payload)
                marker=json.loads(value['marker_contents'])['body']
                same_job=(binding.get('delivery_id')==saved['id'] if name.startswith('automatic-') else
                    binding.get('job_id')==marker.get('manual_history',{}).get('job_id'))
                if binding.get('attempt_id')!=saved['attempt_id'] or not same_job:continue
                actual=con.execute('SELECT payload FROM local_policy WHERE name=?',(name,)).fetchone()
                if actual is not None and actual[0]==payload:latches[name]=payload
    return deliveries,attempts,latches


def account_matches(con,value):
    row=con.execute('SELECT epoch,vault_ref,snapshot,active,local_removed FROM accounts WHERE account_id=?',
        (value['account_id'],)).fetchone()
    if row is None or row['epoch']<value['account_epoch']:return False
    # Isolation proves an old transport inert, not permission for a new login
    # or send. Deliberate local removal must not revive that archived attempt.
    if row['local_removed'] and row['epoch']>value['account_epoch']:return True
    return bool(row['active'] and row['vault_ref']==value['vault_ref']
        and not json.loads(row['snapshot'])['revocation_pending'])


def is_isolated_delivery(con,delivery_id):
    return delivery_id in isolated_work(con)[0]


def commit_isolation(con,plan,receipt):
    if (not valid_receipt(receipt) or receipt['old_profile_id']!=plan['old_profile_id']
            or receipt['old_marker_sha256']!=plan['old_marker_sha256']):
        raise ValueError('Native isolation binding changed')
    row=con.execute('SELECT payload,state FROM deliveries WHERE id=?',(plan['delivery_id'],)).fetchone()
    attempt=con.execute('SELECT * FROM telegram_attempts WHERE attempt_id=?',(plan['attempt']['attempt_id'],)).fetchone()
    account=con.execute('SELECT epoch,vault_ref,active FROM accounts WHERE account_id=?',(plan['account_id'],)).fetchone()
    if (row is None or row['state']!='unknown' or row['payload']!=plan['delivery_payload']
            or attempt is None or dict(attempt)!=plan['attempt'] or account is None or not account['active']
            or account['epoch']!=plan['account_epoch'] or account['vault_ref']!=plan['vault_ref']):
        raise ValueError('Isolated attempt changed')
    existing=records(con)
    value={**plan,'receipt':receipt,'sequence':max((item['sequence'] for item in existing),default=0)+1}
    if not bound_snapshot(value):raise ValueError('Isolation marker binding changed')
    name=PREFIX+receipt['recovery_id']
    previous=con.execute('SELECT payload FROM local_policy WHERE name=?',(name,)).fetchone()
    if previous is not None:
        old=json.loads(previous[0])
        if old['receipt']!=receipt or old['delivery_payload']!=plan['delivery_payload'] or old['attempt']!=plan['attempt']:
            raise ValueError('Isolation receipt changed')
        return
    con.execute('INSERT INTO local_policy(name,payload) VALUES (?,?)',(name,encode(value)))
