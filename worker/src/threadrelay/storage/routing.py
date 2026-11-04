"""Minimal durable current configuration. No setup, auth or transport authority.

All mutations serialize with Router.begin_send using SQLite BEGIN IMMEDIATE.
Versions cannot be reused, including after deletion; account epochs additionally
bind destination verification. Caller must be trusted local configuration/setup,
not arbitrary model output or a generic renderer database bridge.
"""
import json
from threadrelay.contracts import validate_contract
from .database import Database, encode
from .outbox import _current_account


class ConfigStore:
    def __init__(self, database: Database):
        self.db = database

    def _save(self, table, contract, value):
        validate_contract(contract, value)
        with self.db.transaction() as con:
            old = con.execute(f"SELECT * FROM {table} WHERE id=?", (value["id"],)).fetchone()
            if old and (value["version"] < old["version"] or (value["version"] == old["version"] and old["payload"] != encode(value))):
                raise ValueError("Configuration version must increase")
            if table == 'routing_routes' and old and old['payload'] != encode(value):
                self.invalidate_route_queue(con, value['id'])
            con.execute(f"INSERT INTO {table}(id,version,payload) VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version,payload=excluded.payload",
                (value["id"], value["version"], encode(value)))

    @staticmethod
    def invalidate_route_queue(con, route_id):
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='route_queue'").fetchone():
            con.execute("UPDATE route_queue SET reason='route_configuration_changed' WHERE route_id=? AND started_at IS NULL AND cancelled=0 AND reason IS NULL", (route_id,))

    def save_topic(self, topic):
        self._save("routing_topics", "Topic", topic)

    def save_route(self, route):
        self._save("routing_routes", "Route", route)

    def _delete(self, table, id):
        with self.db.transaction() as con:
            if table == 'routing_routes':
                self.invalidate_route_queue(con, id)
            con.execute(f"UPDATE {table} SET payload=NULL WHERE id=?", (id,))

    def delete_route(self, id):
        self._delete("routing_routes", id)

    def delete_topic(self, id):
        self._delete("routing_topics", id)

    def _list(self, table):
        with self.db.connection() as con:
            return self.read_all(con, table)

    @staticmethod
    def read_all(con, table):
        if table not in {"routing_topics", "routing_routes"}:
            raise ValueError("Unknown configuration table")
        return [json.loads(row[0]) for row in con.execute(f"SELECT payload FROM {table} WHERE payload IS NOT NULL ORDER BY id")]

    def topics(self):
        return self._list("routing_topics")

    def routes(self):
        return self._list("routing_routes")

    def save_destination(self, destination, *, account_epoch, sender_selected=False):
        validate_contract("DestinationSnapshot", destination)
        validate_contract("AccountEpoch", account_epoch)
        if type(sender_selected) is not bool:
            raise ValueError("Explicit sender selection must be boolean")
        with self.db.transaction() as con:
            _current_account(con, {"account_id": destination["account_id"], "account_epoch": account_epoch})
            old = con.execute("SELECT * FROM routing_destinations WHERE account_id=? AND chat_id=?",
                (destination["account_id"], destination["chat_id"])).fetchone()
            payload = encode(destination)
            version = 1 if old is None else old["version"] + int(old["payload"] != payload or old["account_epoch"] != account_epoch or old["sender_selected"] != int(sender_selected))
            con.execute("""INSERT INTO routing_destinations VALUES (?,?,?,?,?,?) ON CONFLICT(account_id,chat_id)
                DO UPDATE SET account_epoch=excluded.account_epoch,version=excluded.version,payload=excluded.payload,sender_selected=excluded.sender_selected""",
                (destination["account_id"], destination["chat_id"], account_epoch, version, payload, int(sender_selected)))
            return version

    def delete_destination(self, account_id, chat_id):
        with self.db.transaction() as con:
            con.execute("UPDATE routing_destinations SET payload=NULL,version=version+1 WHERE account_id=? AND chat_id=?", (account_id, chat_id))
