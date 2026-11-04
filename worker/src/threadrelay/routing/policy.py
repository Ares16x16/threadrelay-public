"""Current-state policy, not transport or model authority.

Router.begin_send is the integration boundary: lifecycle lock -> SQLite write
transaction -> synchronous initiation, then release both before awaiting any
response. All ConfigStore/AccountStore/Outbox writes use that same SQLite writer
serialization. Inside start, Task 7 must reuse the supplied connection via
Outbox's connection= argument; opening another write transaction would deadlock.
Standalone authorize/authorize_send is observational, not a transferable permit.
"""
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from uuid import uuid4

from threadrelay.collector.canonicalize import canonicalize_observed_link
from threadrelay.contracts import validate_contract
from threadrelay.inference.decision import ClassifiedDecision, StaleClassification
from threadrelay.storage.database import encode
from threadrelay.storage.outbox import Outbox, _current_account
from threadrelay.storage.routing import ConfigStore
from threadrelay.session import SystemClock


class RoutingDenied(ValueError):
    pass


def _digest(value):
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def _proposal_hash(proposal):
    value = deepcopy(proposal)
    value.pop("plan_id", None)
    # Observation timing/session are not reviewed content or route authority.
    value["post"].pop("observed_at", None)
    value["post"].pop("source_session_id", None)
    return _digest(value)


def _post(con, post_id):
    row = con.execute("SELECT payload FROM post_text WHERE post_id=?", (post_id,)).fetchone()
    if row is None:
        raise RoutingDenied("post_unavailable")
    return json.loads(row[0])


def _canonical(post):
    identity = canonicalize_observed_link(post["permalink"])
    if identity is None or identity != {key: post[key] for key in ("post_id", "permalink")}:
        raise RoutingDenied("post_not_canonical")
    # Do not trust a supplied hash while actual attribution/text has changed.
    snapshot = {key: post[key] for key in ("author", "text", "quote", "visibility", "quality_flags")}
    expected = hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    if expected != post["content_hash"]:
        raise StaleClassification("content_snapshot_mismatch")


def _destination(con, account_id, destination_id, epoch):
    row = con.execute("SELECT * FROM routing_destinations WHERE account_id=? AND chat_id=?", (account_id, destination_id)).fetchone()
    if row is None or row["payload"] is None or row["account_epoch"] != epoch:
        raise RoutingDenied("destination_unavailable")
    value = json.loads(row["payload"])
    from threadrelay.storage.saved_destinations import SavedDestinations
    SavedDestinations.require(con, value, epoch)
    if value["paid_required"] or not value["permissions"]["can_send"]:
        raise RoutingDenied("destination_not_zero_paid_sendable")
    if value["sender"]["kind"] == "user" and value["sender"]["id"] != account_id:
        raise RoutingDenied("sender_identity_mismatch")
    if value["sender"]["kind"] == "chat" and (not row["sender_selected"] or not value["permissions"]["can_select_sender"]):
        raise RoutingDenied("sender_selection_required")
    return value, row["version"]


class Router:
    def __init__(self, database, *, classifier, auto_send_authorized=lambda: False,
            clock=None, auto_route_authorized=lambda route: False, max_per_destination=2, rate_window_seconds=60,
            auto_source_authorized=lambda post,routes:post['visibility']=='public' and not post['quality_flags'],
            selected_route_ids=None):
        if type(max_per_destination) is not int or max_per_destination < 1 or type(rate_window_seconds) not in (int, float) or not math.isfinite(rate_window_seconds) or rate_window_seconds < 60:
            raise ValueError("Invalid conservative rate limits")
        self.db, self.classifier = database, classifier
        self.selected_route_ids = selected_route_ids
        self.auto_send_authorized = auto_send_authorized
        self.auto_route_authorized = auto_route_authorized
        self.auto_source_authorized = auto_source_authorized
        self.clock = clock or SystemClock()
        self.max_per_destination, self.rate_window_seconds = max_per_destination, rate_window_seconds
        self._started = {}
        self._uncertain_attempts = set()  # deny-only circuit if storage fails
        self._last_monotonic = self.clock.monotonic()
        if not math.isfinite(self._last_monotonic):
            raise ValueError("Invalid monotonic clock")
        # Monotonic timestamps cannot be carried across process/boot boundaries.
        # Conservatively cool down every retained initiated/ambiguous destination
        # for a full window after construction; never infer safe elapsed time from
        # wall-clock timestamps. Keep one long-lived Router per worker.
        with self.db.connection() as con:
            keys = con.execute("""SELECT DISTINCT d.account_id,d.destination_id FROM deliveries d
                LEFT JOIN routing_delivery_context c ON c.delivery_id=d.id
                WHERE c.initiated_attempt_id IS NOT NULL OR d.state IN ('sending','unknown')""").fetchall()
        for row in keys:
            self._started[(row[0], row[1])] = [self._last_monotonic] * self.max_per_destination

    def _rate_check(self, delivery, *, scoped=False):
        now = self.clock.monotonic()
        if not math.isfinite(now) or now < self._last_monotonic:
            raise RoutingDenied("rate_clock_invalid")
        self._last_monotonic = now
        key = (delivery["account_id"], delivery["destination_id"])
        self._started[key] = [stamp for stamp in self._started.get(key, []) if stamp > now - self.rate_window_seconds]
        if not scoped and len(self._started[key]) >= self.max_per_destination:
            raise RoutingDenied("destination_rate_limit")
        return key, now

    def rate_delay(self, delivery):
        now = self.clock.monotonic()
        stamps = self._started.get((delivery['account_id'], delivery['destination_id']), [])
        return max(0.0, min(stamps) + self.rate_window_seconds - now) if len(stamps) >= self.max_per_destination else 0.0

    def plan(self, post, classification, routes=None, *, session_mode=None):
        """One deterministic proposal per account+destination, from current data.

        Any contributing review/dry-run/uncertain route wins over auto. Conflicting
        effective senders are suppressed, never chosen by model score/order.
        """
        if not isinstance(classification, ClassifiedDecision):
            raise TypeError("ClassifiedDecision is required")
        validate_contract("ObservedPost", post)
        with self.db.transaction() as con:
            current = _post(con, post["post_id"])
            topics = ConfigStore.read_all(con, "routing_topics")
            # Validate the bundle against actual current content/definitions/model,
            # including data changed without increasing a claimed hash/version.
            decision = classification.for_snapshot(current, topics, self.classifier)
            if self.classifier.cache_key(post, topics) != self.classifier.cache_key(current, topics):
                raise StaleClassification("post_context_mismatch")
            _canonical(current)
            validate_contract("Classification", decision)
            current_routes = ConfigStore.read_all(con, "routing_routes")
            if routes is not None:
                for route in routes:
                    validate_contract("Route", route)
                if sorted(routes, key=lambda r: r["id"]) != current_routes:
                    raise RoutingDenied("routes_not_current")
            scoped = self.selected_route_ids is not None and session_mode == 'auto'
            if scoped:
                selected_ids = self.selected_route_ids()
                if not isinstance(selected_ids, list) or len(selected_ids) != 1:
                    raise RoutingDenied('select_one_auto_route')
                current_routes = [r for r in current_routes if r['id'] in selected_ids and r['enabled'] and r['mode'] == 'auto']
                if len(current_routes) != 1:
                    raise RoutingDenied('selected_auto_route_unavailable')
            results = {r["topic_id"]: r for r in decision["topic_results"]}
            enabled = {t["id"] for t in topics if t["enabled"]}
            matched_ids = {id for id, result in results.items() if result['match'] and not result['abstained']}
            candidate_ids = matched_ids if scoped else {id for id, result in results.items() if result['match'] or result['abstained']}
            groups = {}
            for route in current_routes:
                labels = sorted(set(route["topic_ids"]) & enabled & candidate_ids)
                if route["enabled"] and labels:
                    groups.setdefault((route["account_id"], route["destination_id"]), []).append((route, labels))
            proposals = []
            for _, members in sorted(groups.items()):
                # Never truncate contributing authority or split one destination
                # into duplicate sends. Other independently bounded groups survive.
                if len(members) > 100:
                    con.execute("INSERT INTO diagnostics(code,created_at) VALUES ('routing_route_limit',?)", (self.db.now(),))
                    continue
                selected = members[0][0]
                try:
                    _current_account(con, selected)
                    destination, version = _destination(con, selected["account_id"], selected["destination_id"], selected["account_epoch"])
                    if any(r["account_epoch"] != selected["account_epoch"] or r["sender"] != destination["sender"] for r, _ in members):
                        raise RoutingDenied("sender_conflict")
                except ValueError:
                    con.execute("INSERT INTO diagnostics(code,created_at) VALUES ('routing_binding_unavailable',?)", (self.db.now(),))
                    continue
                modes = {r["mode"] for r, _ in members}
                mode = "dry_run" if "dry_run" in modes or session_mode == "dry_run" else "review" if "review" in modes or session_mode == "review" else "auto"
                primary = deepcopy(selected)
                # Primary route stays exact current config; effective mode lives in
                # the receipt, not in a fabricated merged route/version.
                required = (mode != "auto" or self.auto_source_authorized(current,[r for r,_ in members]) is not True
                    or (decision["abstained"] and not scoped) or destination["audience"] == "unknown" or not self.auto_send_authorized() or not all(self.auto_route_authorized(r) for r, _ in members))
                refs = [{"route_id": r["id"], "route_version": r["version"], "approval_id": None} for r, _ in members]
                proposal = {"post": deepcopy(current), "route": primary, "labels": sorted({label for _, labels in members for label in labels}),
                    "approval_required": required, "route_refs": refs, "destination": destination, "destination_version": version}
                receipt = {"proposal_hash": _proposal_hash(proposal), "snapshot_key": classification.context.snapshot_key,
                    "routes": [r for r, _ in members], "route_set_hash": _digest(current_routes), "destination_version": version, "mode": mode,
                    "approval_required": required, "labels": proposal["labels"], "post_id": current["post_id"], "classification": decision}
                if scoped:
                    receipt['selected_route_ids'] = selected_ids
                proposal["plan_id"] = _digest(receipt)
                validate_contract("DeliveryProposal", proposal)
                con.execute("INSERT OR IGNORE INTO routing_plans(id,post_id,payload) VALUES (?,?,?)", (proposal["plan_id"], current["post_id"], encode(receipt)))
                if mode == "dry_run":
                    con.execute("INSERT INTO diagnostics(code,created_at) VALUES ('routing_dry_run',?)", (self.db.now(),))
                proposals.append(proposal)
            return proposals

    def _receipt(self, con, plan_id):
        row = con.execute("SELECT payload FROM routing_plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise RoutingDenied("proposal_not_current")
        receipt = json.loads(row[0])
        if _digest(receipt) != plan_id:
            raise RoutingDenied("proposal_receipt_integrity")
        current = _post(con, receipt["post_id"])
        _canonical(current)
        topics = ConfigStore.read_all(con, "routing_topics")
        if self.classifier.cache_key(current, topics) != receipt["snapshot_key"]:
            raise RoutingDenied("classification_not_current")
        current_routes = ConfigStore.read_all(con, "routing_routes")
        if 'selected_route_ids' in receipt:
            current_routes = [r for r in current_routes if r['id'] in receipt['selected_route_ids']]
        if _digest(current_routes) != receipt["route_set_hash"]:
            raise RoutingDenied("routing_configuration_changed")
        routes = {r["id"]: r for r in current_routes}
        for route in receipt["routes"]:
            if not route["enabled"] or routes.get(route["id"]) != route:
                raise RoutingDenied("route_revoked")
            _current_account(con, route)
        primary = receipt["routes"][0]
        destination, version = _destination(con, primary["account_id"], primary["destination_id"], primary["account_epoch"])
        if version != receipt["destination_version"] or any(r["sender"] != destination["sender"] for r in receipt["routes"]):
            raise RoutingDenied("destination_changed")
        return receipt, current, destination

    def _proposal(self, con, proposal):
        validate_contract("DeliveryProposal", proposal)
        if "plan_id" not in proposal or "route_refs" not in proposal:
            raise RoutingDenied("proposal_provenance_required")
        receipt, current, destination = self._receipt(con, proposal["plan_id"])
        if _proposal_hash(proposal) != receipt["proposal_hash"]:
            raise RoutingDenied("proposal_binding_mismatch")
        return receipt, current, destination

    def stage(self, proposal, *, approval=None, report_created=False):
        """Persist one pristine intent; optionally report dedup creation atomically."""
        with self.db.transaction() as con:
            receipt, current, _ = self._proposal(con, proposal)
            if receipt["mode"] == "dry_run":
                return (None, False) if report_created else None
            if 'selected_route_ids' in receipt and (self.selected_route_ids is None or self.selected_route_ids() != receipt['selected_route_ids']):
                raise RoutingDenied('selected_route_changed')
            refs = deepcopy(proposal["route_refs"])
            if receipt["approval_required"]:
                if approval is None:
                    raise RoutingDenied("approval_required")
                row = con.execute("SELECT a.payload FROM approvals a JOIN routing_approval_context c ON c.approval_id=a.id WHERE a.id=? AND c.plan_id=? AND a.valid=1",
                    (approval.get("id"), proposal["plan_id"])).fetchone()
                if row is None or json.loads(row[0]) != approval:
                    raise RoutingDenied("approval_not_current")
                values = [json.loads(row[0]) for row in con.execute("SELECT a.payload FROM approvals a JOIN routing_approval_context c ON c.approval_id=a.id WHERE c.plan_id=? AND a.valid=1 ORDER BY a.created_at,a.id", (proposal["plan_id"],))]
                for ref in refs:
                    matches = [a for a in values if a["route_id"] == ref["route_id"] and a["route_version"] == ref["route_version"]]
                    if not matches:
                        raise RoutingDenied("approval_scope_incomplete")
                    ref["approval_id"] = matches[0]["id"]
            elif approval is not None:
                raise RoutingDenied("unexpected_approval")
            primary = proposal["route"]
            delivery = {"id": str(uuid4()), "post_id": current["post_id"], "content_hash": current["content_hash"],
                "destination_id": primary["destination_id"], "account_id": primary["account_id"], "account_epoch": primary["account_epoch"],
                "sender": deepcopy(primary["sender"]), "route_refs": refs, "state": "pending", "attempt_id": None,
                "request_extra": None, "message_id": None, "temporary_message_id": None, "final_message_id": None}
            id = Outbox(self.db).enqueue(delivery, connection=con)
            if id == delivery["id"]:
                con.execute("INSERT INTO routing_delivery_context(delivery_id,plan_id) VALUES (?,?)", (id, proposal["plan_id"]))
                if 'selected_route_ids' in receipt:
                    from threadrelay.delivery.workflow_templates import render_template
                    from threadrelay.delivery.route_schedule import RouteSchedule
                    topics = {t['id']:t['description'] for t in ConfigStore.read_all(con, 'routing_topics')}
                    rendered = render_template(primary.get('message_template', '{{url}}'), current,
                        topic=', '.join(topics[label] for label in proposal['labels'] if label in topics) or None)
                    reason = rendered['error'] or ('missing_template_fields:' + ','.join(rendered['missing_fields']) if rendered['missing_fields'] else None)
                    RouteSchedule(self.db).enroll(con, id, primary, '' if rendered['error'] else rendered['text'], primary['destination_id'], proposal['destination']['title'], snapshot=proposal, reason=reason)
            return (id, id == delivery["id"]) if report_created else id

    def proposal_for_delivery(self, delivery, *, connection=None):
        """Reconstruct current message inputs; this lookup grants NO send permit.

        Inside initiation, reuse its connection. Outside it, this is only a
        display/current-data lookup and must never be used for check-then-send.
        """
        validate_contract("Delivery", delivery)
        with (nullcontext(connection) if connection is not None else self.db.transaction()) as con:
            row = con.execute("SELECT d.payload,c.plan_id FROM deliveries d JOIN routing_delivery_context c ON c.delivery_id=d.id WHERE d.id=?", (delivery["id"],)).fetchone()
            if row is None or json.loads(row["payload"]) != delivery:
                raise RoutingDenied("delivery_not_current")
            receipt, current, destination = self._receipt(con, row["plan_id"])
            proposal = {"plan_id": row["plan_id"], "post": current, "route": receipt["routes"][0],
                "labels": receipt["labels"], "approval_required": receipt["approval_required"],
                "destination": destination, "destination_version": receipt["destination_version"],
                "route_refs": [{"route_id": r["id"], "route_version": r["version"], "approval_id": None} for r in receipt["routes"]]}
            self._proposal(con, proposal)
            return proposal

    def _authorize(self, con, delivery, *, for_start=False):
        validate_contract("Delivery", delivery)
        if (delivery["id"], delivery["attempt_id"]) in self._uncertain_attempts:
            raise RoutingDenied("attempt_outcome_uncertain")
        row = con.execute("SELECT payload FROM deliveries WHERE id=?", (delivery["id"],)).fetchone()
        if row is None or json.loads(row[0]) != delivery or delivery["state"] not in {"pending", "sending"}:
            raise RoutingDenied("delivery_not_current")
        row = con.execute("SELECT plan_id,initiated_attempt_id FROM routing_delivery_context WHERE delivery_id=?", (delivery["id"],)).fetchone()
        if row is None:
            raise RoutingDenied("delivery_provenance_required")
        if row["initiated_attempt_id"] is not None:
            raise RoutingDenied("attempt_already_initiated")
        if for_start:
            attempt = con.execute("SELECT * FROM telegram_attempts WHERE attempt_id=? AND delivery_id=? AND request_extra=? AND state='sending'",
                (delivery["attempt_id"], delivery["id"], delivery["request_extra"])).fetchone()
            if delivery["state"] != "sending" or attempt is None or row["initiated_attempt_id"] is not None:
                raise RoutingDenied("durable_unstarted_claim_required")
        plan_id = row[0]
        receipt, current, _ = self._receipt(con, plan_id)
        primary = receipt["routes"][0]
        if any(delivery[key] != primary[key] for key in ("account_id", "account_epoch", "destination_id", "sender")) or delivery["content_hash"] != current["content_hash"] or delivery["post_id"] != current["post_id"]:
            raise RoutingDenied("delivery_binding_mismatch")
        from threadrelay.delivery.route_schedule import RouteSchedule
        if RouteSchedule(self.db).provider_wait(con,delivery['account_id'],delivery['destination_id']):
            raise RoutingDenied('telegram_provider_wait')
        if receipt["mode"] == "dry_run":
            raise RoutingDenied("dry_run")
        if 'selected_route_ids' in receipt:
            if self.selected_route_ids is None or self.selected_route_ids() != receipt['selected_route_ids']:
                raise RoutingDenied('selected_route_changed')
            from threadrelay.delivery.route_schedule import RouteSchedule
            RouteSchedule(self.db).require_ready(con, delivery['id'])
        if [(r["route_id"], r["route_version"]) for r in delivery["route_refs"]] != [(r["id"], r["version"]) for r in receipt["routes"]]:
            raise RoutingDenied("route_scope_mismatch")
        if receipt["approval_required"]:
            for ref in delivery["route_refs"]:
                row = con.execute("SELECT a.payload FROM approvals a JOIN routing_approval_context c ON c.approval_id=a.id WHERE a.id=? AND a.valid=1 AND c.plan_id=?", (ref["approval_id"], plan_id)).fetchone()
                if row is None:
                    raise RoutingDenied("approval_revoked")
                approval = json.loads(row[0])
                if any(approval[key] != delivery[key] for key in ("account_id", "account_epoch", "sender", "post_id", "destination_id", "content_hash")) or any(approval[key] != ref[key] for key in ("route_id", "route_version")):
                    raise RoutingDenied("approval_binding_mismatch")
        elif (self.auto_source_authorized(current,receipt['routes']) is not True or not self.auto_send_authorized()
                or any(ref["approval_id"] is not None for ref in delivery["route_refs"])
                or not all(self.auto_route_authorized(r) for r in receipt["routes"])):
            raise RoutingDenied("automatic_send_not_authorized")
        self._rate_check(delivery, scoped="selected_route_ids" in receipt)
        return {"allowed": True, "reason": None}

    def authorize(self, delivery, session):
        """Observational current check. Only begin_send holds through initiation."""
        try:
            automatic = all(r["approval_id"] is None for r in delivery["route_refs"])
            if not session.can_transmit(datetime.fromtimestamp(self.db.now(), timezone.utc), automatic=automatic):
                raise RoutingDenied("session_not_running")
            with self.db.transaction() as con:
                return self._authorize(con, delivery)
        except (ValueError, TypeError, KeyError):
            return {"allowed": False, "reason": "routing_authorization_denied"}

    def begin_send(self, delivery, session, *, now, start):
        """Task 7 coordinator; start() must initiate synchronously.

        The delivery must already have a durably committed Outbox claim. The
        no-argument callback is for transport-only initiation. Use
        start_with_connection for correlation updates on the SAME connection;
        never create a new uncommitted send identity immediately before network.
        """
        return self.start_with_connection(delivery, session, now=now, start=lambda con: start())

    def start_with_connection(self, delivery, session, *, now, start, on_denied=lambda reason: None):
        transaction = None
        connection = None
        initiated = False
        denial = "session_not_running"
        def check():
            nonlocal transaction, connection, denial
            transaction = self.db.transaction()
            connection = transaction.__enter__()
            try:
                return self._authorize(connection, delivery, for_start=True)["allowed"]
            except (ValueError, TypeError, KeyError) as error:
                denial = str(error) if isinstance(error, RoutingDenied) else "routing_authorization_denied"
                return False
        def initiate():
            nonlocal initiated
            # Selected-route entries already have durable, user-chosen spacing.
            # The legacy 2/minute budget must not override that exact schedule.
            scoped = self.selected_route_ids is not None and connection.execute(
                'SELECT 1 FROM route_queue WHERE delivery_id=?', (delivery['id'],)).fetchone() is not None
            key, stamp = self._rate_check(delivery, scoped=scoped)
            self._started[key].append(stamp)  # Unknown/exception consumes rate budget.
            connection.execute("UPDATE routing_delivery_context SET initiated_attempt_id=? WHERE delivery_id=?",
                (delivery["attempt_id"], delivery["id"]))
            initiated = True
            return start(connection)
        def release():
            nonlocal transaction, connection
            current = transaction
            transaction, connection = None, None
            if current is not None:
                # Commit even if initiation raises: a transmitted-but-uncertain
                # attempt must survive. Close before session refresh/cancellation,
                # which may legitimately write to this same Database.
                try:
                    current.__exit__(None, None, None)
                except BaseException:
                    if initiated:
                        self._uncertain_attempts.add((delivery["id"], delivery["attempt_id"]))
                        # The failed transaction has already rolled back/closed.
                        # Conservatively terminalize the original durable claim
                        # using the existing Outbox authority, never a new send.
                        try:
                            outbox = Outbox(self.db)
                            saved = outbox.get(delivery["id"])
                            if saved is not None and saved["state"] == "sending":
                                outbox.transition(saved["id"], "sending", "unknown", {
                                    "delivery_id": saved["id"], "attempt_id": saved["attempt_id"],
                                    "request_extra": saved["request_extra"], "temporary_message_id": saved["temporary_message_id"],
                                    "final_message_id": None, "state": "unknown"})
                        except Exception:
                            # If storage stays unavailable, the session hook
                            # closes the gate and this local circuit denies reuse.
                            # On process recovery durable Sending becomes Unknown.
                            pass
                    raise
        try:
            validate_contract("Delivery", delivery)
            automatic = all(r["approval_id"] is None for r in delivery["route_refs"])
            result = session.begin_send(now, automatic=automatic, authorize=check,
                start=initiate, release_authorization=release)
            if result is None and not initiated:
                on_denied(denial)
            return result
        finally:
            release()  # Idempotent fallback for failures before the session scope.


def authorize_send(delivery, current_post, current_route, approval, session, *, router):
    """Compatibility-facing check; supplied snapshots alone never grant authority."""
    try:
        automatic = all(r["approval_id"] is None for r in delivery["route_refs"])
        if not session.can_transmit(datetime.fromtimestamp(router.db.now(), timezone.utc), automatic=automatic):
            raise RoutingDenied("session_not_running")
        with router.db.transaction() as con:
            if _post(con, delivery["post_id"]) != current_post:
                raise RoutingDenied("post_not_current")
            if not any(ref["route_id"] == current_route["id"] and ref["route_version"] == current_route["version"] for ref in delivery["route_refs"]):
                raise RoutingDenied("route_not_in_delivery_scope")
            row = con.execute("SELECT payload FROM routing_routes WHERE id=?", (current_route["id"],)).fetchone()
            if row is None or row[0] is None or json.loads(row[0]) != current_route:
                raise RoutingDenied("route_not_current")
            if approval is not None:
                if not any(ref["approval_id"] == approval["id"] for ref in delivery["route_refs"]):
                    raise RoutingDenied("approval_not_in_delivery_scope")
                row = con.execute("SELECT payload FROM approvals WHERE id=? AND valid=1", (approval["id"],)).fetchone()
                if row is None or json.loads(row[0]) != approval:
                    raise RoutingDenied("approval_not_current")
            return router._authorize(con, delivery)
    except (ValueError, TypeError, KeyError):
        return {"allowed": False, "reason": "routing_authorization_denied"}
