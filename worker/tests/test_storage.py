"""Real SQLite regressions; no transport, real identities, credentials or sleeps."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from threading import Barrier

import pytest

from threadrelay.contracts import ProtocolError
from threadrelay.storage.database import Database, DatabaseInUse
from threadrelay.storage.posts import PostStore
from threadrelay.storage.outbox import AccountStore, Outbox
from threadrelay.storage.retention import Retention

NOW = datetime(2026, 10, 28, tzinfo=timezone.utc)
FIXTURES = json.loads((Path(__file__).parents[2] / "contracts/fixtures.json").read_text())


def fixture(name):
    return deepcopy(next(c["value"] for c in FIXTURES["valid"] if c["name"] == name))


def snapshot(account="9007199254740993", epoch=1):
    return {**fixture("account-snapshot"), "account_id": account, "epoch": epoch}


def delivery(**changes):
    return {**fixture("Delivery"), **changes}


def post(**changes):
    return {**fixture("ObservedPost"), **changes}


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path / "app.db", clock=lambda: NOW) as database:
        AccountStore(database).activate(snapshot(), vault_ref="vault:synthetic", session_ref="session:synthetic")
        PostStore(database).upsert(post())
        Outbox(database).save_approval(fixture("Approval"))
        yield database


def test_post_upsert_edits_snapshot_without_changing_identity(db):
    store = PostStore(db)
    assert store.upsert(post()) is False
    edited = post(text="Edited outer text", content_hash="b" * 64, quote={"author": "Quoted", "text": "Separate quote", "permalink": None})
    assert store.upsert(edited) is False
    assert store.get("synthetic-1") == edited
    assert store.upsert(post(post_id="another")) is True


def test_account_scoped_dedup_does_not_change_on_sender_or_edit(db):
    outbox = Outbox(db)
    first = outbox.enqueue(delivery())
    changed = delivery(id="changed", sender={"kind": "chat", "id": "-9007199254740994"}, content_hash="b" * 64)
    assert outbox.enqueue(changed) == first
    assert outbox.get(first) == delivery()
    AccountStore(db).activate(snapshot("9007199254740994"))
    second = delivery(id="second", account_id="9007199254740994", sender={"kind": "user", "id": "9007199254740994"}, route_refs=[{"route_id": "route-1", "route_version": 1, "approval_id": None}])
    assert outbox.enqueue(second) == "second"
    assert outbox.get(first)["state"] == "cancelled"


def test_account_epoch_revocation_cancels_pending_and_never_inherits_authority(db):
    outbox = Outbox(db)
    outbox.enqueue(delivery())
    assert AccountStore(db).invalidate_epoch("9007199254740993") == 2
    assert outbox.get("delivery-1")["state"] == "cancelled"
    assert outbox.claim_next("worker") is None
    with pytest.raises(ValueError, match="account"):
        outbox.enqueue(delivery(id="stale", post_id="other"))
    with pytest.raises(ValueError, match="epoch"):
        AccountStore(db).activate(snapshot(epoch=1))
    AccountStore(db).activate(snapshot(epoch=2))
    assert outbox.enqueue(delivery(account_epoch=2, id="new")) == "delivery-1"
    with pytest.raises(ValueError, match="account"):
        outbox.save_approval(fixture("Approval"))


def test_no_implicit_account_and_no_non_pending_enqueue(tmp_path):
    with Database(tmp_path / "empty.db", clock=lambda: NOW) as db:
        with pytest.raises(ValueError, match="account"):
            Outbox(db).enqueue(delivery())
        AccountStore(db).activate(snapshot())
        with pytest.raises(ProtocolError):
            Outbox(db).enqueue(delivery(state="sent"))


def test_approval_binding_and_content_must_match_before_enqueue(db):
    outbox = Outbox(db)
    for bad in [delivery(content_hash="b" * 64), delivery(sender={"kind": "chat", "id": "-42"}), delivery(route_refs=[{"route_id":"other", "route_version":1, "approval_id":"approval-1"}])]:
        with pytest.raises(ValueError):
            outbox.enqueue(bad)
    assert outbox.get("delivery-1") is None


def test_two_independent_sqlite_connections_compete_for_exactly_one_claim(db):
    outbox = Outbox(db)
    outbox.enqueue(delivery())
    barrier = Barrier(8)
    def claim(index):
        barrier.wait()
        return Outbox(db).claim_next(f"worker-{index}")
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(claim, range(8)))
    assert sum(item is not None for item in claims) == 1
    claimed = next(item for item in claims if item)
    assert claimed["state"] == "sending"
    assert claimed["attempt_id"] and claimed["request_extra"]
    assert outbox.attempts(claimed["id"]) == [{"delivery_id": claimed["id"], "attempt_id": claimed["attempt_id"], "request_extra": claimed["request_extra"], "temporary_message_id": None, "final_message_id": None, "state": "sending"}]


def attempt(claimed, state, **changes):
    return {"delivery_id": claimed["id"], "attempt_id": claimed["attempt_id"], "request_extra": claimed["request_extra"], "temporary_message_id": None, "final_message_id": None, "state": state, **changes}


def test_ordered_attempt_correlation_and_final_ids_do_not_replay(db):
    outbox = Outbox(db)
    outbox.enqueue(delivery())
    claimed = outbox.claim_next("worker")
    temporary = attempt(claimed, "sending", temporary_message_id="-9007199254740993")
    assert outbox.transition(claimed["id"], "sending", "sending", temporary)
    assert outbox.get(claimed["id"])["message_id"] is None
    wrong = attempt(claimed, "sent", request_extra="other", final_message_id="9007199254740994")
    assert outbox.transition(claimed["id"], "sending", "sent", wrong) is False
    final = {**temporary, "state": "sent", "final_message_id": "9007199254740994"}
    assert outbox.transition(claimed["id"], "sending", "sent", final)
    assert outbox.transition(claimed["id"], "sending", "sent", final) is False
    assert outbox.transition(claimed["id"], "sent", "sending", temporary) is False
    stored = outbox.get(claimed["id"])
    assert stored["final_message_id"] == stored["message_id"] == "9007199254740994"
    assert stored["temporary_message_id"] == "-9007199254740993"
    assert outbox.claim_next("another") is None


def test_cannot_confirm_sent_from_an_initial_message_without_final_id(db):
    outbox = Outbox(db); outbox.enqueue(delivery()); claimed = outbox.claim_next("worker")
    with pytest.raises(ProtocolError):
        outbox.transition(claimed["id"], "sending", "sent", attempt(claimed, "sent", temporary_message_id="-42"))
    assert outbox.get(claimed["id"])["state"] == "sending"


def test_claim_transaction_rolls_back_both_state_and_attempt_on_database_failure(db):
    Outbox(db).enqueue(delivery())
    with db.transaction() as con:
        con.execute("CREATE TRIGGER fail_attempt BEFORE INSERT ON telegram_attempts BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        Outbox(db).claim_next("worker")
    assert Outbox(db).get("delivery-1")["state"] == "pending"
    assert Outbox(db).attempts("delivery-1") == []


def test_unclean_claim_reopens_unknown_and_never_automatically_retries(tmp_path):
    path=tmp_path / "crash.db"
    with Database(path, clock=lambda: NOW) as db:
        AccountStore(db).activate(snapshot()); PostStore(db).upsert(post())
        outbox=Outbox(db); outbox.save_approval(fixture("Approval")); outbox.enqueue(delivery())
        claimed=outbox.claim_next("worker")
    # Close isn't delivery shutdown: any unconfirmed Sending must remain ambiguous.
    with Database(path, clock=lambda: NOW) as reopened:
        outbox=Outbox(reopened)
        assert reopened.recovery["counts"]["unknown"] == 1
        assert outbox.get(claimed["id"])["state"] == "unknown"
        assert outbox.attempts(claimed["id"])[0]["state"] == "unknown"
        assert outbox.claim_next("worker") is None
        assert outbox.transition(claimed["id"], "unknown", "pending", attempt(claimed,"pending")) is False


def test_revoking_inflight_retains_uncertainty_and_correlation(db):
    outbox=Outbox(db);outbox.enqueue(delivery());claimed=outbox.claim_next("worker")
    AccountStore(db).invalidate_epoch("9007199254740993")
    assert outbox.get(claimed["id"])["state"] == "unknown"
    assert outbox.attempts(claimed["id"])[0]["attempt_id"] == claimed["attempt_id"]
    assert outbox.claim_next("worker") is None


def test_stale_content_blocks_claim_even_after_valid_enqueue(db):
    outbox=Outbox(db);outbox.enqueue(delivery())
    PostStore(db).upsert(post(text="Edited",content_hash="b"*64))
    assert outbox.claim_next("worker") is None
    assert outbox.get("delivery-1")["state"] == "cancelled"


def test_single_owner_rejects_second_database_instance(db):
    with pytest.raises(DatabaseInUse):
        Database(db.path)


def test_failed_migration_rolls_back_ddl_data_and_version(tmp_path):
    path=tmp_path / "migration.db"
    with Database(path) as db:
        version=db.schema_version
    migration=tmp_path / "002_bad.sql"
    migration.write_text("CREATE TABLE must_rollback (id TEXT); INSERT INTO must_rollback VALUES ('x'); INSERT INTO missing_table VALUES ('bad');")
    with pytest.raises(sqlite3.OperationalError):
        Database(path, migrations=[(version+1,migration)])
    with Database(path) as db:
        assert db.schema_version == version
        with db.transaction() as con:
            assert con.execute("SELECT name FROM sqlite_master WHERE name='must_rollback'").fetchone() is None


def test_backup_restores_complete_wal_snapshot_and_recovery(db, tmp_path):
    outbox=Outbox(db);outbox.enqueue(delivery());outbox.claim_next("worker")
    backup=tmp_path / "backup.db";db.backup(backup)
    assert sqlite3.connect(backup).execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with Database(backup,clock=lambda:NOW) as restored:
        assert PostStore(restored).get("synthetic-1")["text"] == "A synthetic example"
        assert Outbox(restored).get("delivery-1")["state"] == "unknown"
        assert Outbox(restored).claim_next("worker") is None
    with pytest.raises(ValueError):db.backup(db.path)


def test_retention_separates_text_dedup_and_diagnostic_boundaries(db):
    outbox=Outbox(db);outbox.enqueue(delivery())
    db.record_diagnostic("synthetic_event")
    retention=Retention(db)
    assert retention.prune(NOW+timedelta(days=7,microseconds=-1))["deleted_counts"]["post_text"] == 0
    report=retention.prune(NOW+timedelta(days=7))
    assert report["deleted_counts"]["post_text"] == 1
    assert report["deleted_counts"]["diagnostics"] == 1
    assert PostStore(db).get("synthetic-1") is None
    assert outbox.get("delivery-1")["state"] == "cancelled"
    assert outbox.enqueue(delivery(id="duplicate")) == "delivery-1"
    assert retention.prune(NOW+timedelta(days=30,microseconds=-1))["deleted_counts"]["deliveries"] == 0
    assert retention.prune(NOW+timedelta(days=30))["deleted_counts"]["deliveries"] == 1
    assert outbox.get("delivery-1") is None
    assert retention.prune(NOW+timedelta(days=31))["deleted_counts"]["deliveries"] == 0


def test_retention_defaults_are_configurable_and_now_must_be_utc(db):
    with pytest.raises(ValueError):Retention(db, text_days=0)
    with pytest.raises(ValueError):Retention(db).prune(NOW.replace(tzinfo=None))
    assert Retention(db,text_days=10).prune(NOW+timedelta(days=7))["deleted_counts"]["post_text"] == 0


def test_app_database_and_redacted_export_have_no_credentials_or_telegram_payload_columns(db):
    Outbox(db).enqueue(delivery())
    exported=json.dumps(db.redacted_export())
    for disallowed in ["A synthetic example","Synthetic account","vault:synthetic","session:synthetic","9007199254740993","approval-1"]:
        assert disallowed not in exported
    with db.transaction() as con:
        tables=con.execute("SELECT sql FROM sqlite_master WHERE type='table'").fetchall()
        ddl=" ".join(row[0] for row in tables).lower()
    for disallowed in ["password","api_hash","auth_code","session_bytes","phone","telegram_content"]:
        assert disallowed not in ddl
    with pytest.raises(ProtocolError):
        Outbox(db).enqueue({**delivery(),"api_hash":"not-a-real-secret"})
    with pytest.raises(ValueError):
        AccountStore(db).activate(snapshot(),vault_ref="raw credential")


def test_newer_database_version_is_rejected_without_downgrade(tmp_path):
    path = tmp_path / "future.db"
    with Database(path):
        pass
    with sqlite3.connect(path) as con:
        con.execute("PRAGMA user_version=99")
    with pytest.raises(ValueError, match="newer"):
        Database(path)
    with sqlite3.connect(path) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 99


def test_private_database_files_are_not_world_readable(db):
    import os
    if os.name == "nt":
        pytest.skip("Windows ACL behavior requires native platform verification")
    assert db.path.stat().st_mode & 0o077 == 0
    assert Path(str(db.path) + ".lock").stat().st_mode & 0o077 == 0


def test_actual_process_crash_after_claim_recovers_one_unknown_without_replay(tmp_path):
    import subprocess
    import sys
    path = tmp_path / "process-crash.db"
    with Database(path, clock=lambda: NOW) as database:
        AccountStore(database).activate(snapshot())
        PostStore(database).upsert(post())
        Outbox(database).save_approval(fixture("Approval"))
        Outbox(database).enqueue(delivery())
    script = """
import os, sys
from threadrelay.storage.database import Database
from threadrelay.storage.outbox import Outbox
with Database(sys.argv[1]) as db:
    assert Outbox(db).claim_next('crashing-worker')['state'] == 'sending'
    os._exit(0)
"""
    result = subprocess.run([sys.executable, "-c", script, str(path)], cwd=Path(__file__).parents[1], timeout=10)
    assert result.returncode == 0
    with Database(path, clock=lambda: NOW) as reopened:
        assert reopened.recovery["counts"] == {"unknown": 1}
        assert len(Outbox(reopened).attempts("delivery-1")) == 1
        assert Outbox(reopened).claim_next("next-worker") is None


def test_late_observation_does_not_overwrite_a_newer_content_snapshot(db):
    store = PostStore(db)
    newer = post(text="New snapshot",content_hash="b"*64,observed_at="2026-10-28T01:00:00Z")
    store.upsert(newer)
    store.upsert(post())
    assert store.get("synthetic-1")["text"] == "New snapshot"


def test_expired_approval_cannot_be_used_by_a_late_claim(db):
    outbox = Outbox(db)
    outbox.enqueue(delivery())
    with db.transaction() as con:
        con.execute("UPDATE approvals SET valid=0 WHERE id='approval-1'")
    assert outbox.claim_next("worker") is None
    assert outbox.get("delivery-1")["state"] == "cancelled"


def test_retention_does_not_erase_a_still_inflight_attempt(db):
    outbox = Outbox(db)
    outbox.enqueue(delivery())
    outbox.claim_next("worker")
    # A fresh observation does not mean an in-flight send has completed.
    PostStore(db).upsert(post(observed_at="2026-11-27T00:00:00Z"))
    report = Retention(db).prune(NOW + timedelta(days=30))
    assert report["deleted_counts"]["deliveries"] == 0
    assert outbox.get("delivery-1")["state"] == "sending"
    assert len(outbox.attempts("delivery-1")) == 1


def test_actual_crash_inside_claim_transaction_never_persists_half_a_claim(tmp_path):
    import subprocess
    import sys
    path = tmp_path / "mid-claim-crash.db"
    with Database(path, clock=lambda: NOW) as database:
        AccountStore(database).activate(snapshot())
        PostStore(database).upsert(post())
        Outbox(database).save_approval(fixture("Approval"))
        Outbox(database).enqueue(delivery())
    # Test-only trigger kills the subprocess after the delivery UPDATE, before
    # attempt INSERT/COMMIT. No production crash hook or transport is involved.
    script = """
from contextlib import contextmanager
import os, sys
from threadrelay.storage.database import Database
from threadrelay.storage.outbox import Outbox
with Database(sys.argv[1]) as db:
    original = db.connection
    @contextmanager
    def connection():
        with original() as con:
            con.create_function('crash_now', 0, lambda: os._exit(17))
            yield con
    db.connection = connection
    with db.transaction() as con:
        con.execute('CREATE TRIGGER crash_claim BEFORE INSERT ON telegram_attempts BEGIN SELECT crash_now(); END')
    Outbox(db).claim_next('crashing-worker')
"""
    result = subprocess.run([sys.executable, "-c", script, str(path)], cwd=Path(__file__).parents[1], timeout=10)
    assert result.returncode == 17
    with Database(path, clock=lambda: NOW) as reopened:
        outbox = Outbox(reopened)
        assert outbox.get("delivery-1")["state"] == "pending"
        assert outbox.attempts("delivery-1") == []
        with reopened.transaction() as con:
            con.execute("DROP TRIGGER crash_claim")
        assert outbox.claim_next("next-worker") is not None
        assert outbox.claim_next("second-worker") is None


def test_account_switch_failure_is_atomic_and_export_is_static_allowlist(db):
    accounts = AccountStore(db)
    with pytest.raises(ValueError):
        accounts.activate(snapshot("9007199254740994"), session_ref="untrusted bytes")
    assert accounts.current()["account_id"] == "9007199254740993"
    assert accounts.current()["epoch"] == 1
    with pytest.raises(ValueError):
        db.record_diagnostic("raw TDLib payload or exception")
    assert db.redacted_export() == {"format_version":1,"delivery_counts":{},"history":[]}


def test_retention_of_expired_body_preserves_live_send_and_delayed_result(db):
    outbox = Outbox(db)
    outbox.enqueue(delivery())
    claimed = outbox.claim_next("worker")
    temporary = attempt(claimed, "sending", temporary_message_id="-9007199254740993")
    assert outbox.transition(claimed["id"], "sending", "sending", temporary)

    # Both body and delivery are expired; TTL is not a transport outcome.
    expired_at = NOW + timedelta(days=30)
    report = Retention(db).prune(expired_at)
    assert report["deleted_counts"]["post_text"] == 1
    assert PostStore(db).get("synthetic-1") is None
    assert report["deleted_counts"]["deliveries"] == 0
    assert outbox.get(claimed["id"])["state"] == "sending"
    assert outbox.attempts(claimed["id"]) == [temporary]

    # Rediscovery cannot create a second operation while the first is in flight.
    PostStore(db).upsert(post(observed_at="2026-11-27T00:00:00Z"))
    assert outbox.enqueue(delivery(id="rediscovered")) == claimed["id"]
    assert outbox.claim_next("second-worker") is None

    # A genuinely delayed final update still resolves the original correlation.
    final = {**temporary, "state": "sent", "final_message_id": "9007199254740994"}
    assert outbox.transition(claimed["id"], "sending", "sent", final)
    assert outbox.get(claimed["id"])["message_id"] == "9007199254740994"
    assert outbox.attempts(claimed["id"]) == [final]
    assert outbox.claim_next("third-worker") is None
    # Only now is existing resolved-history expiry allowed to remove the record.
    assert Retention(db).prune(expired_at)["deleted_counts"]["deliveries"] == 1
