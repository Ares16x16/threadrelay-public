"""Explicit owner-saved chat identities, separate from native verification."""
from datetime import datetime, timezone
import json

from threadrelay.contracts import validate_contract
from .database import encode
from .outbox import _current_account

_REQUIRED='saved-telegram-destinations-required'


def _key(account, chat):
    return f'saved-telegram-destination:{account}:{chat}'


class SavedDestinations:
    def __init__(self, database):
        self.db=database

    def require_saved_selection(self):
        # Product startup enables this policy; it has no renderer disable path.
        with self.db.transaction() as con:
            con.execute('INSERT INTO local_policy(name,payload) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload',(_REQUIRED,'true'))

    def query(self, account_id, epoch):
        if account_id is None:return {'items':[]}
        with self.db.connection() as con:
            rows=con.execute('SELECT payload FROM local_policy WHERE name LIKE ? ORDER BY name LIMIT 201',(f'saved-telegram-destination:{account_id}:%',)).fetchall()
        items=[]
        for row in rows:
            value=json.loads(row['payload'])
            validate_contract('SavedTelegramDestination',value)
            if value['account_id']==account_id and value['account_epoch']==epoch:items.append(value)
        result={'items':items[:200]}
        validate_contract('SavedTelegramDestinations',result)
        return result

    def save_verified(self, destination, epoch, kind):
        validate_contract('DestinationSnapshot',destination)
        value={key:destination[key] for key in ('chat_id','title','sender','account_id')}
        value.update(account_epoch=epoch,kind=kind,saved_at=datetime.fromtimestamp(self.db.now(),timezone.utc).isoformat())
        validate_contract('SavedTelegramDestination',value)
        with self.db.transaction() as con:
            _current_account(con,{'account_id':value['account_id'],'account_epoch':epoch})
            name=_key(value['account_id'],value['chat_id'])
            old=con.execute('SELECT 1 FROM local_policy WHERE name=?',(name,)).fetchone()
            count=con.execute('SELECT COUNT(*) FROM local_policy WHERE name LIKE ?',(f"saved-telegram-destination:{value['account_id']}:%",)).fetchone()[0]
            if old is None and count>=200:raise ValueError('Saved chat limit reached')
            con.execute('INSERT INTO local_policy(name,payload) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload',(name,encode(value)))
        return value

    @staticmethod
    def require(con, destination, epoch):
        policy=con.execute('SELECT payload FROM local_policy WHERE name=?',(_REQUIRED,)).fetchone()
        if policy is None or policy['payload']!='true':return
        row=con.execute('SELECT payload FROM local_policy WHERE name=?',(_key(destination['account_id'],destination['chat_id']),)).fetchone()
        if row is None:raise ValueError('destination_not_saved')
        value=json.loads(row['payload'])
        validate_contract('SavedTelegramDestination',value)
        if (value['account_epoch']!=epoch or any(value[key]!=destination[key] for key in ('account_id','chat_id','title','sender'))):
            raise ValueError('destination_not_saved')
