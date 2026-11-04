"""Deterministic current-state routing over synthetic SQLite and typed decisions."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

import hashlib
from threadrelay.contracts import ProtocolError, validate_contract
from threadrelay.inference.decision import Classifier, StaleClassification
from threadrelay.routing.policy import Router, authorize_send
from threadrelay.routing.approvals import ApprovalStore
from threadrelay.storage.database import Database
from threadrelay.storage.outbox import AccountStore, Outbox
from threadrelay.storage.posts import PostStore
from threadrelay.storage.routing import ConfigStore
from threadrelay.session import SessionController

NOW = datetime(2026, 10, 28, tzinfo=timezone.utc)
ACCOUNT = "9007199254740993"
DEST = "-9007199254740994"
SENDER = {"kind": "user", "id": ACCOUNT}


class ScoresRuntime:
    limits = SimpleNamespace(max_input_bytes=10000)
    metadata = {"model_id": "synthetic-decision", "model_sha256": "a" * 64,
        "runtime_commit": "b" * 40, "runtime_version": "synthetic", "context_tokens": 4096}

    def decide(self, state, questions):
        return {key: 0.95 for key in questions}


def topic(id="science", **changes):
    return {"id": id, "version": 1, "description": "Synthetic research", "positive_examples": [],
        "negative_examples": [], "exclusions": [], "enabled": True, **changes}


def route(id="route-a", **changes):
    return {"id": id, "version": 1, "topic_ids": ["science"], "destination_id": DEST,
        "mode": "review", "enabled": True, "account_id": ACCOUNT, "account_epoch": 1,
        "sender": deepcopy(SENDER), **changes}


def destination(**changes):
    return {"account_id": ACCOUNT, "chat_id": DEST, "title": "Synthetic audience", "audience": "private",
        "permissions": {"can_send": True, "can_select_sender": False}, "sender": deepcopy(SENDER),
        "paid_required": False, **changes}


def post(**changes):
    value = {"post_id": "threads:Abc123", "permalink": "https://www.threads.net/@synthetic/post/Abc123",
        "author": "synthetic", "text": "Synthetic research", "quote": None, "visibility": "public",
        "quality_flags": [], "source_session_id": "synthetic-session", "observed_at": NOW.isoformat(), **changes}
    snapshot = {k: value[k] for k in ("author", "text", "quote", "visibility", "quality_flags")}
    value["content_hash"] = hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False,
        separators=(",", ":")).encode()).hexdigest()
    return value


@pytest.fixture
def env(tmp_path):
    with Database(tmp_path / "app.db", clock=lambda: NOW) as db:
        AccountStore(db).activate({"account_id": ACCOUNT, "epoch": 1, "auth_state": "ready",
            "display_name": "Synthetic", "network_state": "offline", "revocation_pending": False})
        config = ConfigStore(db)
        config.save_topic(topic())
        config.save_route(route())
        config.save_destination(destination(), account_epoch=1)
        p = post()
        assert "post_id" in p
        PostStore(db).upsert(p)
        classifier = Classifier(ScoresRuntime())
        router = Router(db, classifier=classifier, auto_send_authorized=lambda: True, auto_route_authorized=lambda route: True)
        yield SimpleNamespace(db=db, config=config, post=p, classifier=classifier, router=router,
            approvals=ApprovalStore(router))


def plan(env, **post_changes):
    p = post(**post_changes) if post_changes else env.post
    PostStore(env.db).upsert(p)
    topics = env.config.topics()
    return env.router.plan(p, env.classifier.classify_bound(p, topics), env.config.routes())


def running(mode="review", **changes):
    session = SessionController(setup_check=lambda _: {"allowed": True, "reason": None})
    session.start({"feed_url": "https://www.threads.net/", "mode": mode, **changes})
    return session


def approved(env, proposal):
    return env.approvals.approve(proposal, disclosure=env.approvals.disclosure(proposal))


def staged(env, proposal=None):
    proposal = proposal or plan(env)[0]
    approval = approved(env, proposal)
    id = env.router.stage(proposal, approval=approval)
    return Outbox(env.db).get(id), approval


def test_bare_dto_is_not_routing_provenance(env):
    bare = env.classifier.classify(env.post, env.config.topics())
    with pytest.raises(TypeError, match="ClassifiedDecision"):
        env.router.plan(env.post, bare, env.config.routes())


@pytest.mark.parametrize("change", ["text", "topics", "model"])
def test_stale_full_current_context_is_rejected(env, change):
    bundle = env.classifier.classify_bound(env.post, env.config.topics())
    if change == "text":
        env.post = {**env.post, "text": "Edited without changing claimed hash"}
        PostStore(env.db).upsert(env.post)
    elif change == "topics":
        env.config.save_topic(topic(version=2, description="New current definition"))
    else:
        env.classifier._runtime.metadata = {**ScoresRuntime.metadata, "model_sha256": "c" * 64}
    with pytest.raises(StaleClassification):
        env.router.plan(env.post, bundle, env.config.routes())


@pytest.mark.parametrize("visibility", ["restricted", "unknown"])
def test_nonpublic_never_auto_and_disclosure_is_explicit(env, visibility):
    env.config.save_route(route(version=2, mode="auto"))
    proposal = plan(env, visibility=visibility)[0]
    assert proposal["approval_required"] is True
    assert proposal["destination"]["audience"] == "private"
    with pytest.raises(ValueError, match="disclosure"):
        env.approvals.approve(proposal)
    delivery, approval = staged(env, proposal)
    assert env.router.authorize(delivery, running("auto"))["allowed"] is True
    stripped = {**delivery, "route_refs": [{**r, "approval_id": None} for r in delivery["route_refs"]]}
    assert env.router.authorize(stripped, running("auto"))["allowed"] is False


def test_multiple_topics_and_routes_merge_one_destination_and_keep_all_bindings(env):
    env.config.save_topic(topic("math"))
    env.config.save_route(route("route-b", topic_ids=["math"]))
    proposals = plan(env)
    assert len(proposals) == 1
    proposal = proposals[0]
    validate_contract("DeliveryProposal", proposal)
    assert proposal["labels"] == ["math", "science"]
    assert [r["route_id"] for r in proposal["route_refs"]] == ["route-a", "route-b"]
    delivery, approval = staged(env, proposal)
    assert len({r["approval_id"] for r in delivery["route_refs"]}) == 2
    assert env.router.authorize(delivery, running())["allowed"] is True
    env.config.delete_route("route-b")
    assert env.router.authorize(delivery, running())["allowed"] is False


def test_route_input_order_does_not_change_proposals(env):
    env.config.save_route(route("route-b"))
    bundle = env.classifier.classify_bound(env.post, env.config.topics())
    routes = env.config.routes()
    assert env.router.plan(env.post, bundle, routes) == env.router.plan(env.post, bundle, routes[::-1])


@pytest.mark.parametrize("change", ["account", "epoch", "sender", "content", "destination", "destination_audience", "route", "disabled", "deleted", "topic", "paid", "permissions"])
def test_current_changes_revoke_staged_authority(env, change):
    delivery, approval = staged(env)
    if change == "account":
        AccountStore(env.db).activate({"account_id": "42", "epoch": 1, "auth_state": "ready",
            "display_name": None, "network_state": "offline", "revocation_pending": False})
    elif change == "epoch":
        AccountStore(env.db).invalidate_epoch(ACCOUNT)
    elif change == "sender":
        env.config.save_destination(destination(sender={"kind": "chat", "id": "-42"}), account_epoch=1)
    elif change == "content":
        PostStore(env.db).upsert(post(text="Edited content"))
    elif change == "destination":
        env.config.save_route(route(version=2, destination_id="-42"))
    elif change == "destination_audience":
        env.config.save_destination(destination(audience="public"), account_epoch=1)
    elif change == "route":
        env.config.save_route(route(version=2))
    elif change == "disabled":
        env.config.save_route(route(version=2, enabled=False))
    elif change == "deleted":
        env.config.delete_route("route-a")
    elif change == "topic":
        env.config.save_topic(topic(version=2))
    elif change == "paid":
        env.config.save_destination(destination(paid_required=True), account_epoch=1)
    else:
        env.config.save_destination(destination(permissions={"can_send": False, "can_select_sender": False}), account_epoch=1)
    assert env.router.authorize(delivery, running())["allowed"] is False
    assert authorize_send(delivery, env.post, route(), approval, running(), router=env.router)["allowed"] is False


def test_approval_while_paused_never_transmits(env):
    session = running(); session.pause()
    delivery, approval = staged(env)
    calls = []
    assert env.router.begin_send(delivery, session, now=NOW, start=lambda: calls.append("send")) is None
    assert calls == []
    assert env.router.authorize(delivery, session)["allowed"] is False


def test_dry_run_persists_only_diagnostic_not_delivery_intent(env):
    env.config.save_route(route(version=2, mode="dry_run"))
    proposal = plan(env)[0]
    assert env.router.stage(proposal) is None
    with env.db.connection() as con:
        assert con.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0
        assert con.execute("SELECT code FROM diagnostics WHERE code='routing_dry_run'").fetchone()


@pytest.mark.parametrize("reason", ["quality", "abstain", "not_enabled"])
def test_uncertain_or_unapproved_auto_is_review_only(env, reason):
    env.config.save_route(route(version=2, mode="auto"))
    if reason == "abstain":
        env.classifier._runtime.decide = lambda state, questions: {k: 0.5 for k in questions}
    if reason == "not_enabled":
        env.router = Router(env.db, classifier=env.classifier)
    proposal = plan(env, quality_flags=[{"code": "synthetic_warning", "detail": "Synthetic warning"}]) [0] if reason == "quality" else plan(env)[0]
    assert proposal["approval_required"] is True
    with pytest.raises(ValueError, match="approval"):
        env.router.stage(proposal)


def test_only_current_configured_labels_and_observed_canonical_link_reach_proposal(env):
    proposal = plan(env)[0]
    assert proposal["post"]["permalink"] == env.post["permalink"]
    assert proposal["labels"] == ["science"]
    assert proposal["route"]["destination_id"] == DEST
    forged = deepcopy(proposal); forged["route"]["destination_id"] = "-42"
    with pytest.raises(ValueError, match="proposal"):
        env.router.stage(forged)
    forged = deepcopy(proposal); forged["post"]["permalink"] = "https://example.com/forged"
    with pytest.raises(ValueError, match="proposal"):
        approved(env, forged)


def test_auto_rechecks_setup_and_session_cap_inside_synchronized_begin_send(env):
    env.config.save_route(route(version=2, mode="auto"))
    proposal = plan(env)[0]
    assert proposal["approval_required"] is False
    env.router.stage(proposal)
    delivery = Outbox(env.db).claim_next("synthetic-worker")
    session = running("auto", max_auto_deliveries=1)
    calls = []
    assert env.router.begin_send(delivery, session, now=NOW, start=lambda: calls.append("initiated") or "handle") == "handle"
    assert session.snapshot["state"] == "paused"
    assert env.router.begin_send(delivery, session, now=NOW, start=lambda: calls.append("second")) is None
    assert calls == ["initiated"]
    env.router.auto_send_authorized = lambda: False
    assert env.router.authorize(delivery, running("auto"))["allowed"] is False


def test_versions_cannot_be_reused_after_change_or_delete(env):
    with pytest.raises(ValueError, match="version"):
        env.config.save_route(route(mode="auto"))
    env.config.delete_route("route-a")
    with pytest.raises(ValueError, match="version"):
        env.config.save_route(route())
    env.config.save_route(route(version=2))
    assert env.config.routes() == [route(version=2)]


def test_missing_current_store_metadata_never_grants_permission(env):
    delivery, _ = staged(env)
    with env.db.transaction() as con:
        con.execute("DELETE FROM routing_delivery_context WHERE delivery_id=?", (delivery["id"],))
    assert env.router.authorize(delivery, running())["allowed"] is False


def test_auto_authorization_also_requires_current_auto_session_mode(env):
    env.config.save_route(route(version=2, mode="auto"))
    proposal = plan(env)[0]
    delivery = Outbox(env.db).get(env.router.stage(proposal))
    assert env.router.authorize(delivery, running("review"))["allowed"] is False


def test_observational_check_never_takes_session_lock_under_database_lock(env):
    delivery, _ = staged(env)
    session = running()
    original = session.can_transmit
    def inspect_order(*args, **kwargs):
        assert env.db._operations == 0, "DB -> lifecycle lock order is unsafe"
        return original(*args, **kwargs)
    session.can_transmit = inspect_order
    assert env.router.authorize(delivery, session)["allowed"] is True


def test_pending_or_already_initiated_attempt_cannot_start_twice(env):
    delivery, _ = staged(env)
    calls = []
    session = running()
    assert env.router.begin_send(delivery, session, now=NOW, start=lambda: calls.append("unclaimed")) is None
    assert calls == []
    claimed = Outbox(env.db).claim_next("synthetic-worker")
    assert env.router.begin_send(claimed, session, now=NOW, start=lambda: calls.append("first") or "handle") == "handle"
    assert env.router.begin_send(claimed, session, now=NOW, start=lambda: calls.append("duplicate")) is None
    assert calls == ["first"]


class ManualClock:
    def __init__(self):
        self.elapsed = 10.0
    def monotonic(self):
        return self.elapsed
    def now(self):
        return NOW


def test_destination_rolling_rate_window_counts_uncertain_initiations(env):
    clock = ManualClock()
    env.router = Router(env.db, classifier=env.classifier, clock=clock, max_per_destination=1)
    env.approvals = ApprovalStore(env.router)
    delivery, _ = staged(env)
    claimed = Outbox(env.db).claim_next("synthetic-worker")
    with pytest.raises(RuntimeError, match="uncertain"):
        env.router.begin_send(claimed, running(), now=NOW, start=lambda: (_ for _ in ()).throw(RuntimeError("uncertain")))
    # Another post/delivery, same destination; no retry of the unknown request.
    p = {**post(), "post_id": "threads:Next", "permalink": "https://www.threads.net/@synthetic/post/Next"}
    env.post = p; PostStore(env.db).upsert(p)
    other, _ = staged(env)
    other = Outbox(env.db).claim_next("synthetic-worker")
    assert env.router.begin_send(other, running(), now=NOW, start=lambda: "too soon") is None
    clock.elapsed += 59.999
    assert env.router.begin_send(other, running(), now=NOW, start=lambda: "still too soon") is None
    clock.elapsed += 0.001
    assert env.router.begin_send(other, running(), now=NOW, start=lambda: "after window") == "after window"


def test_malformed_model_destination_and_url_cannot_reach_proposal(env):
    from dataclasses import replace
    bundle = env.classifier.classify_bound(env.post, env.config.topics())
    value = json.loads(bundle._encoded)
    value.update(destination_id="-42", url="https://example.com/forged")
    forged = replace(bundle, _encoded=json.dumps(value).encode())
    with pytest.raises(ProtocolError):
        env.router.plan(env.post, forged, env.config.routes())


def test_legacy_proposal_and_unbound_approval_cannot_stage_authority(env):
    proposal = plan(env)[0]
    legacy = {k: proposal[k] for k in ("post", "route", "labels", "approval_required")}
    with pytest.raises(ValueError, match="provenance"):
        approved(env, legacy)
    approval = approved(env, proposal)
    env.approvals.revoke(approval["id"])
    with pytest.raises(ValueError, match="approval"):
        env.router.stage(proposal, approval=approval)


def test_channel_sender_requires_explicit_selection_and_exact_verified_identity(env):
    sender = {"kind": "chat", "id": "-42"}
    env.config.save_route(route(version=2, sender=sender))
    dest = destination(sender=sender, permissions={"can_send": True, "can_select_sender": True})
    env.config.save_destination(dest, account_epoch=1)
    assert plan(env) == []
    env.config.save_destination(dest, account_epoch=1, sender_selected=True)
    proposal = plan(env)[0]
    delivery, _ = staged(env, proposal)
    assert env.router.authorize(delivery, running())["allowed"] is True


def test_database_upgrade_preserves_existing_storage_and_revokes_on_post_expiry(tmp_path):
    from importlib.resources import files
    from datetime import timedelta
    from threadrelay.storage.retention import Retention
    path = tmp_path / "upgrade.db"
    with Database(path, clock=lambda: NOW, migrations=[(1, files("threadrelay.storage").joinpath("migrations/001_initial.sql"))]) as db:
        PostStore(db).upsert(post())
        assert db.schema_version == 1
    with Database(path, clock=lambda: NOW) as db:
        assert db.schema_version == 8
        assert PostStore(db).get(post()["post_id"]) == post()
        Retention(db).prune(NOW + timedelta(days=8))
        assert PostStore(db).get(post()["post_id"]) is None


def test_config_revocation_serializes_with_initiation_and_same_connection_updates(env):
    from contextlib import contextmanager
    from threading import Event, Thread, current_thread
    delivery, _ = staged(env)
    claimed = Outbox(env.db).claim_next("synthetic-worker")
    attempted, changed = Event(), Event()
    owner = current_thread()
    original = env.db.connection
    @contextmanager
    def traced_connection():
        with original() as con:
            if current_thread() is not owner:
                con.set_trace_callback(lambda sql: attempted.set() if sql == "BEGIN IMMEDIATE" else None)
            yield con
    env.db.connection = traced_connection
    errors = []
    def revoke():
        try:
            env.config.save_route(route(version=2, enabled=False))
            changed.set()
        except BaseException as error:
            errors.append(error)
    thread = Thread(target=revoke)
    def initiate(con):
        assert con.in_transaction
        assert con.execute("SELECT state FROM deliveries WHERE id=?", (claimed["id"],)).fetchone()[0] == "sending"
        thread.start()
        assert attempted.wait(5), "writer did not reach SQLite begin"
        assert not changed.is_set(), "revocation raced through active initiation"
        # Task7 correlation can use this transaction, not another BEGIN.
        assert Outbox(env.db).enqueue(delivery, connection=con) == delivery["id"]
        return "in-flight"
    assert env.router.start_with_connection(claimed, running(), now=NOW, start=initiate) == "in-flight"
    thread.join(5)
    assert not thread.is_alive()
    assert errors == [] and changed.is_set()
    assert env.router.authorize(claimed, running())["allowed"] is False


def test_attempt_reservation_survives_exception_and_no_callback_is_retried(env):
    delivery, _ = staged(env)
    claimed = Outbox(env.db).claim_next("synthetic-worker")
    with pytest.raises(RuntimeError, match="uncertain"):
        env.router.begin_send(claimed, running(), now=NOW, start=lambda: (_ for _ in ()).throw(RuntimeError("uncertain")))
    with env.db.connection() as con:
        row = con.execute("SELECT initiated_attempt_id FROM routing_delivery_context WHERE delivery_id=?", (claimed["id"],)).fetchone()
        assert row[0] == claimed["attempt_id"]
    assert env.router.begin_send(claimed, running(), now=NOW, start=lambda: pytest.fail("duplicate initiation")) is None


def test_restart_rate_cooldown_includes_ambiguous_durable_attempts(env):
    delivery, _ = staged(env)
    _claimed = Outbox(env.db).claim_next("synthetic-worker")
    # A crash can roll back the initiation marker even after bytes escaped;
    # recovery's Unknown must therefore also force a conservative cooldown.
    env.db.recover()
    clock = ManualClock()
    env.router = Router(env.db, classifier=env.classifier, clock=clock, max_per_destination=1)
    env.approvals = ApprovalStore(env.router)
    env.post = {**post(), "post_id": "threads:Next", "permalink": "https://www.threads.net/@synthetic/post/Next"}
    PostStore(env.db).upsert(env.post)
    other, _ = staged(env)
    other = Outbox(env.db).claim_next("synthetic-worker")
    assert env.router.begin_send(other, running(), now=NOW, start=lambda: "too soon") is None
    clock.elapsed += 60
    assert env.router.begin_send(other, running(), now=NOW, start=lambda: "cooled down") == "cooled down"


def test_retention_removes_plan_and_approval_context_with_expired_text(env):
    from datetime import timedelta
    from threadrelay.storage.retention import Retention
    delivery, approval = staged(env)
    Retention(env.db).prune(NOW + timedelta(days=8))
    with env.db.connection() as con:
        assert con.execute("SELECT COUNT(*) FROM routing_plans").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM routing_approval_context").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM routing_delivery_context").fetchone()[0] == 0
    assert env.router.authorize(delivery, running())["allowed"] is False


def test_distinct_destinations_get_independent_proposals_and_conflicting_senders_fail_closed(env):
    env.config.save_destination(destination(chat_id="-42"), account_epoch=1)
    env.config.save_route(route("route-b", destination_id="-42"))
    assert {p["route"]["destination_id"] for p in plan(env)} == {DEST, "-42"}
    env.config.save_route(route("route-c", sender={"kind": "chat", "id": "-43"}))
    assert [p["route"]["destination_id"] for p in plan(env)] == ["-42"]


@pytest.mark.parametrize("state", ["stopped", "paused", "needs_attention", "failed"])
def test_nonrunning_states_deny_final_current_authorization(env, state):
    delivery, approval = staged(env)
    session = running()
    if state == "stopped":
        session.stop()
    elif state == "paused":
        session.pause()
    elif state == "needs_attention":
        session.require_attention("synthetic")
    else:
        session.fail("synthetic")
    assert authorize_send(delivery, env.post, route(), approval, session, router=env.router)["allowed"] is False


def test_rolling_clock_regression_denies_without_an_initiation(env):
    clock = ManualClock()
    env.router = Router(env.db, classifier=env.classifier, clock=clock)
    env.approvals = ApprovalStore(env.router)
    delivery, _ = staged(env)
    claimed = Outbox(env.db).claim_next("synthetic-worker")
    clock.elapsed -= 1
    assert env.router.begin_send(claimed, running(), now=NOW, start=lambda: pytest.fail("bad clock")) is None


def test_new_review_route_cannot_be_hidden_by_old_auto_plan(env):
    env.config.save_route(route(version=2, mode="auto"))
    proposal = plan(env)[0]
    delivery = Outbox(env.db).get(env.router.stage(proposal))
    assert env.router.authorize(delivery, running("auto"))["allowed"] is True
    env.config.save_route(route("new-review-route", mode="review"))
    assert env.router.authorize(delivery, running("auto"))["allowed"] is False


def test_supplied_route_and_approval_must_belong_to_delivery_scope(env):
    env.config.save_route(route("unrelated", destination_id="-42"))
    delivery, approval = staged(env)
    assert authorize_send(delivery, env.post, route(), approval, running(), router=env.router)["allowed"] is True
    assert authorize_send(delivery, env.post, route("unrelated", destination_id="-42"), approval, running(), router=env.router)["allowed"] is False


def test_initiated_attempt_is_not_observationally_authorized_again(env):
    delivery, _ = staged(env)
    claimed = Outbox(env.db).claim_next("synthetic-worker")
    assert env.router.begin_send(claimed, running(), now=NOW, start=lambda: "handle") == "handle"
    assert env.router.authorize(claimed, running())["allowed"] is False


def test_plan_receipt_integrity_is_checked_before_authorization(env):
    delivery, _ = staged(env)
    with env.db.transaction() as con:
        row = con.execute("SELECT p.id,p.payload FROM routing_plans p JOIN routing_delivery_context c ON c.plan_id=p.id WHERE c.delivery_id=?", (delivery["id"],)).fetchone()
        receipt = json.loads(row["payload"]); receipt["labels"] = ["forged"]
        con.execute("UPDATE routing_plans SET payload=? WHERE id=?", (json.dumps(receipt), row["id"]))
    assert env.router.authorize(delivery, running())["allowed"] is False


def test_delivery_proposal_is_reconstructed_from_current_receipt_on_guard_connection(env):
    proposal = plan(env)[0]
    delivery, _ = staged(env, proposal)
    claimed = Outbox(env.db).claim_next("synthetic-worker")
    observed = []
    def initiate(con):
        current = env.router.proposal_for_delivery(claimed, connection=con)
        assert current == proposal
        observed.append((current["post"]["permalink"], current["labels"], current["route"]["destination_id"]))
        return "in-flight"
    assert env.router.start_with_connection(claimed, running(), now=NOW, start=initiate) == "in-flight"
    assert observed == [(env.post["permalink"], ["science"], DEST)]


@pytest.fixture
def short_busy_timeout(env):
    """Real SQLite contention fails promptly; no sleep or fake cancellation."""
    from contextlib import contextmanager
    original = env.db.connection
    @contextmanager
    def connection():
        with original() as con:
            con.execute("PRAGMA busy_timeout=1")
            yield con
    env.db.connection = connection
    return env


@pytest.mark.parametrize("raises", [False, True])
def test_auto_cap_releases_guard_before_db_writing_intake_cancel(short_busy_timeout, raises):
    env = short_busy_timeout
    clock = ManualClock()
    env.config.save_route(route(version=2, mode="auto"))
    env.router = Router(env.db, classifier=env.classifier, clock=clock, auto_send_authorized=lambda: True, auto_route_authorized=lambda route: True)
    env.router.stage(plan(env)[0])
    claimed = Outbox(env.db).claim_next("synthetic-worker")
    session = SessionController(clock=clock, setup_check=lambda _: {"allowed": True, "reason": None},
        cancel_intake=lambda: env.db.record_diagnostic("intake_cancelled"))
    session.start({"feed_url": "https://www.threads.net/", "mode": "auto", "max_auto_deliveries": 1})
    calls = []
    def initiate():
        calls.append("initiated")
        if raises:
            raise RuntimeError("uncertain initiation")
        return "handle"
    if raises:
        with pytest.raises(RuntimeError, match="uncertain initiation"):
            env.router.begin_send(claimed, session, now=NOW, start=initiate)
    else:
        assert env.router.begin_send(claimed, session, now=NOW, start=initiate) == "handle"
    assert calls == ["initiated"]
    assert session.snapshot["state"] == "paused"
    assert session.snapshot["attention_reason"] == "automatic_delivery_limit"
    with env.db.connection() as con:
        assert con.execute("SELECT code FROM diagnostics WHERE code='intake_cancelled'").fetchone()
        assert con.execute("SELECT initiated_attempt_id FROM routing_delivery_context WHERE delivery_id=?", (claimed["id"],)).fetchone()[0] == claimed["attempt_id"]
    assert env.db._operations == 0
    assert env.router.begin_send(claimed, running("auto"), now=NOW, start=lambda: pytest.fail("duplicate initiation")) is None


@pytest.mark.parametrize("expiry_phase", ["authorize", "start", "denied", "authorize_raises"])
def test_deadline_releases_guard_before_db_writing_intake_cancel(short_busy_timeout, expiry_phase):
    env = short_busy_timeout
    clock = ManualClock()
    env.config.save_route(route(version=2, mode="auto"))
    env.router = Router(env.db, classifier=env.classifier, clock=clock, auto_send_authorized=lambda: True, auto_route_authorized=lambda route: True)
    env.router.stage(plan(env)[0])
    claimed = Outbox(env.db).claim_next("synthetic-worker")
    session = SessionController(clock=clock, setup_check=lambda _: {"allowed": True, "reason": None},
        cancel_intake=lambda: env.db.record_diagnostic("intake_cancelled"))
    session.start({"feed_url": "https://www.threads.net/", "mode": "auto", "duration_seconds": 1})
    original = env.router._authorize
    if expiry_phase == "denied":
        env.router.auto_send_authorized = lambda: False
    def authorize(*args, **kwargs):
        try:
            if expiry_phase == "authorize_raises":
                raise RuntimeError("authorization unavailable")
            return original(*args, **kwargs)
        finally:
            if expiry_phase != "start":
                clock.elapsed += 2
    env.router._authorize = authorize
    calls = []
    def initiate():
        calls.append("initiated")
        clock.elapsed += 2
        return "handle"
    if expiry_phase == "authorize_raises":
        with pytest.raises(RuntimeError, match="authorization unavailable"):
            env.router.begin_send(claimed, session, now=NOW, start=initiate)
    else:
        result = env.router.begin_send(claimed, session, now=NOW, start=initiate)
        assert result == ("handle" if expiry_phase == "start" else None)
    assert calls == (["initiated"] if expiry_phase == "start" else [])
    assert session.snapshot["state"] == "paused"
    assert session.snapshot["attention_reason"] == "duration_limit"
    with env.db.connection() as con:
        assert con.execute("SELECT code FROM diagnostics WHERE code='intake_cancelled'").fetchone()
        marker = con.execute("SELECT initiated_attempt_id FROM routing_delivery_context WHERE delivery_id=?", (claimed["id"],)).fetchone()[0]
        assert marker == (claimed["attempt_id"] if expiry_phase == "start" else None)
    assert env.db._operations == 0


@pytest.mark.parametrize("persist_unknown", [True, False])
def test_guard_commit_failure_closes_gate_and_retains_ambiguous_attempt(env, monkeypatch, persist_unknown):
    import sqlite3
    original = sqlite3.connect
    failures, commits, closed = [True], [], []
    class CommitFailure(sqlite3.Connection):
        marker_written = False
        def execute(self, sql, *args, **kwargs):
            if sql.startswith("UPDATE routing_delivery_context SET initiated_attempt_id="):
                self.marker_written = True
            return super().execute(sql, *args, **kwargs)
        def commit(self):
            if self.marker_written:
                commits.append("attempted")
                if failures[0]:
                    failures[0] = False
                    raise sqlite3.OperationalError("synthetic commit failure")
            return super().commit()
        def close(self):
            if self.marker_written:
                closed.append("closed")
            return super().close()
    def connect(*args, **kwargs):
        return original(*args, **kwargs, factory=CommitFailure)
    monkeypatch.setattr(sqlite3, "connect", connect)
    original_transition = Outbox.transition
    if not persist_unknown:
        def unavailable(*args, **kwargs):
            raise sqlite3.OperationalError("synthetic ongoing storage failure")
        monkeypatch.setattr(Outbox, "transition", unavailable)
    delivery, _ = staged(env)
    claimed = Outbox(env.db).claim_next("synthetic-worker")
    session = running()
    calls = []
    with pytest.raises(sqlite3.OperationalError, match="synthetic commit failure"):
        env.router.begin_send(claimed, session, now=NOW, start=lambda: calls.append("initiated") or "not-a-success")
    assert calls == ["initiated"]
    assert commits == ["attempted"] and closed == ["closed"]
    assert env.db._operations == 0
    assert session.snapshot["state"] == "needs_attention"
    assert session.snapshot["attention_reason"] == "authorization_release_failed"
    current = Outbox(env.db).get(claimed["id"])
    assert current["state"] == ("unknown" if persist_unknown else "sending")
    assert current["attempt_id"] == claimed["attempt_id"]
    assert Outbox(env.db).attempts(current["id"])[0]["state"] == current["state"]
    assert env.router.begin_send(claimed, running(), now=NOW, start=lambda: pytest.fail("ambiguous fresh retry")) is None
    if not persist_unknown:
        monkeypatch.setattr(Outbox, "transition", original_transition)
        env.db.recover()
        assert Outbox(env.db).get(claimed["id"])["state"] == "unknown"


def test_overfull_destination_is_suppressed_without_losing_other_destinations(env):
    for index in range(100):
        env.config.save_route(route(id=f'overflow-{index}'))
    env.config.save_destination(destination(chat_id='-123'), account_epoch=1)
    env.config.save_route(route(id='independent', destination_id='-123'))
    proposals = plan(env)
    assert [p['route']['destination_id'] for p in proposals] == ['-123']
    with env.db.connection() as con:
        assert con.execute("SELECT count(*) FROM diagnostics WHERE code='routing_route_limit'").fetchone()[0] == 1
