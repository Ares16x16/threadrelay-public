"""Synchronous initiation coordinated by the existing durable routing gate."""
from .message import format_outgoing
from dataclasses import dataclass
import json
import math
import time

from threadrelay.storage.outbox import Outbox, _current_account
from .telegram import NetworkPermit

COLLECTION_ONLY_REASONS = frozenset(('view_hidden','duration_limit','post_limit','automatic_delivery_limit'))


@dataclass(frozen=True)
class ReconciliationStatus:
    """Application/local correlation evidence, not a native runtime proof."""
    unresolved_app_attempts: int
    local_correlations_quiescent: bool

    @property
    def quiescent(self):
        return self.unresolved_app_attempts == 0 and self.local_correlations_quiescent


@dataclass(frozen=True)
class SendInitiated:
    attempt_id: str
    temporary_message_id: str | None = None


class Sender:
    def __init__(self, router, session, auth, client, *, destination_authority, clock, monotonic=time.monotonic, attempt_timeout=30.0):
        if auth._lifecycle_lock is not session.authority_lock:
            raise ValueError('Account and session must share the lifecycle lock')
        if auth._client is not client:
            raise ValueError('Account and transport must share the client')
        self.router, self.session, self.auth, self.client = router, session, auth, client
        self.destinations, self.clock = destination_authority, clock
        self.outbox = Outbox(router.db)
        self.monotonic = monotonic
        if not math.isfinite(attempt_timeout) or attempt_timeout <= 0:
            raise ValueError("Invalid attempt timeout")
        self.attempt_timeout = attempt_timeout
        self._deadlines = {}
        self._deferred = {}
        self._prepared_delivery_id = None
        self._destination_work = None
        self._originals = set()
        self._retry_evidence = {}
        self._workflow = None
        session.add_gate_close_hook(self._close_transport)

    def _close_transport(self):
        destination_work,self._destination_work = self._destination_work,None
        if destination_work is not None and self.destinations is not None and self.destinations.pending is destination_work[0]:
            self.destinations.invalidate()
        evidence, self._retry_evidence = self._retry_evidence, {}
        # Collection authority gates new starts; it does not own account lifetime.
        # Keep TDLib's existing connection and exact in-flight receipts alive.
        # Explicit logout/removal/shutdown still close through AuthController.
        for delivery_id, item in evidence.items():
            self.outbox.unknown_retry(delivery_id, item['attempt_id'])
            self._retire_durable_terminal(delivery_id, item['attempt_id'], item['request_extra'])

    def _retire_durable_terminal(self, delivery_id, attempt_id, request_extra):
        saved = self.outbox.get(delivery_id)
        if (saved is None or saved['state'] not in {'sent','failed','unknown','cancelled'}
            or saved['attempt_id'] != attempt_id or saved['request_extra'] != request_extra):
            return False
        if saved['state'] in {'unknown','cancelled'}:
            self.client.expire_attempt(request_extra)
        self.client.retire_attempt(request_extra, abandon_retry=True)
        self._originals.discard((delivery_id, attempt_id))
        return True

    def enable_running_network(self):
        """Issue reconnect only under current lifecycle + account writer guard.

        This mechanism is still gated by the absent native no-flush proof.
        """
        with self.session.authority_lock:
            if not self._drain() or not self.session.can_transmit(self.clock()):
                raise ValueError('Running authority unavailable')
            account = self.auth.snapshot()
            if self.auth.status != 'ready' or account.auth_state != 'ready' or account.account_id is None or account.revocation_pending:
                raise ValueError('Account authority unavailable')
            try:
                with self.router.db.transaction() as con:
                    _current_account(con, {'account_id':account.account_id, 'account_epoch':account.epoch})
                    # Start/Resume owns collection, not TDLib's existing network.
                    # A current connection may still be resolving an exact
                    # attempt or reconnecting itself. Grant no new permit here.
                    if self.client._network_enabled and not self.client._closing and not self.client._closed:
                        return
                    unresolved = con.execute("SELECT COUNT(*) FROM deliveries WHERE state IN ('sending','retryable','unknown')").fetchone()[0]
                    status = ReconciliationStatus(unresolved, self.client.local_attempts_quiescent())
                    # Pristine app Pending has never reached TDLib. Every
                    # uncertain own attempt remains blocked until a separately
                    # authorized resolution can establish quiescence; cache
                    # absence in the all-false profile is not such evidence.
                    if not status.quiescent:
                        raise ValueError('Unresolved Telegram attempts')
                    self.client.enable_network(NetworkPermit('running_reconciliation', account.epoch, False),
                        connection=con, running_account=account.account_id)
            except Exception:
                try: self.client.disable_network()
                finally: self.session.require_attention('telegram_network_authority_failed')
                raise

    def prepare_destination(self):
        """Refresh only an already-approved pending destination; never select/change a sender."""
        if self.destinations is None:
            return False
        pending = self.destinations.pending
        if self._destination_work is not None:
            expected, _ = self._destination_work
            if pending is not expected:
                self._destination_work = None
                self.session.require_attention('destination_verification_unavailable')
                return False  # Do not repeat a failed/interrupted native lookup.
            if pending['stage'] == 'complete':
                self._destination_work = None
                self.destinations.finish()
            else:
                return False
        elif pending is not None:
            return False  # A deliberate UI setup owns its result; never steal it.
        now = self.monotonic()
        self._deferred = {key: stamp for key, stamp in self._deferred.items() if stamp > now}
        excluded = list(self._deferred)
        clause = ' AND id NOT IN (' + ','.join('?' for _ in excluded) + ')' if excluded else ''
        with self.router.db.connection() as con:
            rows = con.execute("SELECT payload FROM deliveries WHERE state IN ('pending','retryable') AND id NOT IN (SELECT delivery_id FROM route_queue)" + clause + " ORDER BY created_at,id LIMIT 100", excluded).fetchall()
        self._prepared_delivery_id = None
        for row in rows:
            delivery = json.loads(row[0])
            if self._deferred.get(delivery['id'], 0) > now:
                continue
            try:
                proposal = self.router.proposal_for_delivery(delivery)
            except ValueError:
                self.outbox.cancel_pending(delivery['id'])
                continue
            if self.destinations.is_current(delivery, proposal):
                if delivery['state'] == 'retryable':
                    continue  # Retry delay must not head-of-line block other pending work.
                self._prepared_delivery_id = delivery['id']
                return True
            # Destination type is durable verified setup metadata. A private
            # user is positive-ID; group/channel IDs are negative in TDLib.
            if int(delivery['destination_id']) > 0:
                self.destinations.begin_private(delivery['destination_id'])
            else:
                with self.router.db.connection() as con:
                    saved = con.execute('SELECT sender_selected FROM routing_destinations WHERE account_id=? AND chat_id=? AND account_epoch=?',
                        (delivery['account_id'], delivery['destination_id'], delivery['account_epoch'])).fetchone()
                self.destinations.begin_group(delivery['destination_id'], sender=delivery['sender'],
                    sender_selected=bool(saved and saved[0]))
            self._destination_work = (self.destinations.pending, now + self.attempt_timeout)
            return False
        return False

    def start_next(self, worker_id, *, delivery_id=None):
        with self.session.authority_lock:
            if not self._drain() or not self.session.can_transmit(self.clock()): return None
            reservation = self.client.reserve_attempt()
            if reservation is None: return None
            try:
                return self._start_next_locked(worker_id, reservation, delivery_id=delivery_id)
            finally:
                self.client.cancel_attempt_reservation(reservation)

    def start_workflow(self, workflow):
        """One explicit app-owned queue claim; collector Running is independent.

        Workflow source/preview/rule consent is checked under the same account
        lifecycle and SQLite writer. Native proof and current zero-paid target
        authority remain mandatory; no client creation or reconnect occurs here.
        """
        if workflow.sender is not self or workflow.auth is not self.auth:
            raise ValueError('Workflow owner mismatch')
        with self.session.authority_lock:
            if workflow._closed:
                return None
            ready=workflow.preflight_due()
            if ready is None:
                return None
            if not ready:
                snapshot=self.auth.snapshot()
                if self.auth.status=='ready' and snapshot.network_state=='online' and self.client.transport_ready and self._drain():
                    workflow.refresh_destination()
                return None
            if not self._drain():
                return None
            reservation=self.client.reserve_attempt()
            if reservation is None:
                return None
            claim=None;native_entered=False
            try:
                claim=workflow.claim_due()
                if claim is None:
                    return None
                delivery=claim['delivery']
                def initiate(con,job,target):
                    nonlocal native_entered
                    if not workflow._native():
                        raise ValueError('Native delivery unavailable')
                    key,stamp=self.router._rate_check(delivery)
                    self.client.register_attempt(reservation=reservation,delivery_id=delivery['id'],attempt_id=delivery['attempt_id'],
                        request_extra=delivery['request_extra'],account_id=delivery['account_id'],epoch=delivery['account_epoch'],
                        chat_id=delivery['destination_id'],sender=delivery['sender'],destination_version=target['destination_version'],
                        content_hash=delivery['content_hash'])
                    self.router._started[key].append(stamp)
                    native_entered=True
                    self.client.initiate_message(chat_id=delivery['destination_id'],text=job['text'],request_extra=delivery['request_extra'])
                    self._originals.add((delivery['id'],delivery['attempt_id']))
                    self._deadlines[delivery['id']]=(delivery['attempt_id'],self.monotonic()+self.attempt_timeout)
                    return SendInitiated(delivery['attempt_id'])
                return workflow.start_claim(claim,initiate)
            except Exception:
                if claim is not None:
                    if native_entered:
                        workflow.unknown_claim(claim)
                    else:
                        workflow.release_unstarted(claim)
                    self.client.expire_attempt(claim['delivery']['request_extra'])
                    self.client.retire_attempt(claim['delivery']['request_extra'],abandon_retry=True)
                return None
            finally:
                self.client.cancel_attempt_reservation(reservation)

    def _start_next_locked(self, worker_id, reservation, *, delivery_id=None):
        now = self.monotonic()
        self._deferred = {key: stamp for key, stamp in self._deferred.items() if stamp > now}
        claim = self.outbox.claim_next(worker_id, exclude_ids=self._deferred, only_id=delivery_id, exclude_scheduled=True)
        if claim is None:
            return None
        native_entered = False
        def initiate(con):
            nonlocal native_entered
            snapshot = self.auth.snapshot()
            proposal = self.router.proposal_for_delivery(claim, connection=con)
            if (self.auth.status != 'ready' or snapshot.auth_state != 'ready' or snapshot.network_state != 'online' or snapshot.revocation_pending
                or snapshot.account_id != claim['account_id'] or snapshot.epoch != claim['account_epoch']
                or not self.client.transport_ready or not self.destinations.is_current(claim, proposal)):
                raise ValueError('Current Telegram authority unavailable')
            post = proposal['post']
            text = format_outgoing(post)
            self.client.register_attempt(reservation=reservation, delivery_id=claim['id'], attempt_id=claim['attempt_id'],
                request_extra=claim['request_extra'], account_id=claim['account_id'], epoch=claim['account_epoch'],
                chat_id=claim['destination_id'], sender=claim['sender'], destination_version=proposal['destination_version'],
                content_hash=claim['content_hash'])
            native_entered = True
            self.client.initiate_message(chat_id=claim['destination_id'], text=text, request_extra=claim['request_extra'])
            self._originals.add((claim['id'], claim['attempt_id']))
            self._deadlines[claim['id']] = (claim['attempt_id'], self.monotonic() + self.attempt_timeout)
            return SendInitiated(claim['attempt_id'])
        try:
            denial = []
            result = self.router.start_with_connection(claim, self.session, now=self.clock(), start=initiate, on_denied=denial.append)
            if result is None:
                reason = denial[0] if denial else None
                if reason in {'destination_rate_limit', 'session_not_running'}:
                    if not self.outbox.release_unstarted(claim, defer=True):
                        self._unknown(claim)
                    elif reason == 'destination_rate_limit':
                        self._deferred[claim['id']] = self.monotonic() + self.router.rate_delay(claim)
                elif not self.outbox.release_unstarted(claim, defer=False):
                    self._unknown(claim)
            return result
        except Exception:
            # Local preflight/registration failure is not ambiguous transmission.
            if native_entered or not self.outbox.release_unstarted(claim, defer=False, before_native=True):
                self._unknown(claim)
            else:
                self.client.expire_attempt(claim['request_extra'])
                self.client.retire_attempt(claim['request_extra'], abandon_retry=True)
            raise

    def _unknown(self, claim):
        self.outbox.transition(claim['id'], 'sending', 'unknown', {
            'delivery_id':claim['id'], 'attempt_id':claim['attempt_id'], 'request_extra':claim['request_extra'],
            'temporary_message_id':None, 'final_message_id':None, 'state':'unknown'})
        self._retire_durable_terminal(claim['id'], claim['attempt_id'], claim['request_extra'])

    def timeout(self, delivery_id):
        with self.session.authority_lock:
            saved = self.outbox.get(delivery_id)
            if saved is None: return
            if saved['state'] == 'retryable':
                self._retry_evidence.pop(delivery_id, None)
                if self.outbox.unknown_retry(delivery_id, saved['attempt_id']):
                    self.client.retire_attempt(saved['request_extra'], abandon_retry=True)
                    self._originals.discard((saved['id'], saved['attempt_id']))
                return
            if saved['state'] != 'sending': return
            self.client.expire_attempt(saved['request_extra'])
            if self.outbox.transition(saved['id'], 'sending', 'unknown', {
                'delivery_id':saved['id'], 'attempt_id':saved['attempt_id'], 'request_extra':saved['request_extra'],
                'temporary_message_id':saved['temporary_message_id'], 'final_message_id':None, 'state':'unknown'}):
                self.client.retire_attempt(saved['request_extra'], abandon_retry=True)
                self._originals.discard((saved['id'], saved['attempt_id']))

    def prepare_retry(self, delivery_id):
        with self.session.authority_lock:
            saved = self.outbox.get(delivery_id)
            evidence = self._retry_evidence.get(delivery_id)
            if (saved is None or saved['state'] != 'retryable' or evidence is None
                or saved['attempt_id'] != evidence['attempt_id'] or not self.client.transport_ready):
                raise ValueError('Retry evidence unavailable')
            self.client.verify_failed_attempt(saved['request_extra'], evidence['failed_id'])

    def retry(self, delivery_id, worker_id):
        with self.session.authority_lock:
            evidence = self._retry_evidence.get(delivery_id)
            if evidence is None or not evidence.get('verified'):
                return None
            if not self._drain(): return None
            saved = self.outbox.get(delivery_id)
            if saved is None or self.router.rate_delay(saved) > 0:
                return None
            evidence = self._retry_evidence.get(delivery_id)
            now = self.monotonic()
            if (evidence is None or not evidence.get('verified') or not math.isfinite(now) or now < evidence['deadline']
                or not self.session.can_transmit(self.clock())):
                return None
            reservation = self.client.reserve_attempt()
            if reservation is None: return None
            try:
                return self._retry_reserved(delivery_id, worker_id, evidence, reservation)
            finally:
                self.client.cancel_attempt_reservation(reservation)

    def _retry_reserved(self, delivery_id, worker_id, evidence, reservation):
        claim = self.outbox.claim_retry(delivery_id, evidence['attempt_id'], worker_id)
        if claim is None:
            if self._retire_durable_terminal(delivery_id, evidence['attempt_id'], evidence['request_extra']):
                self._retry_evidence.pop(delivery_id, None)
            return None
        self._retry_evidence.pop(delivery_id, None)
        self.client.retire_attempt(evidence['request_extra'], abandon_retry=True)
        self._originals.discard((delivery_id, evidence['attempt_id']))
        native_entered = False
        def initiate(con):
            nonlocal native_entered
            snapshot = self.auth.snapshot()
            proposal = self.router.proposal_for_delivery(claim, connection=con)
            if (self.auth.status != 'ready' or snapshot.auth_state != 'ready' or snapshot.network_state != 'online' or snapshot.revocation_pending
                or snapshot.account_id != claim['account_id'] or snapshot.epoch != claim['account_epoch']
                or not self.client.transport_ready or not self.destinations.is_current(claim, proposal)):
                raise ValueError('Current Telegram authority unavailable')
            self.client.register_attempt(reservation=reservation, delivery_id=claim['id'], attempt_id=claim['attempt_id'],
                request_extra=claim['request_extra'], account_id=claim['account_id'], epoch=claim['account_epoch'],
                chat_id=claim['destination_id'], sender=claim['sender'], destination_version=proposal['destination_version'],
                content_hash=claim['content_hash'])
            native_entered = True
            self.client.initiate_retry(chat_id=claim['destination_id'], failed_id=evidence['failed_id'], request_extra=claim['request_extra'])
            self._originals.add((claim['id'], claim['attempt_id']))
            self._deadlines[claim['id']] = (claim['attempt_id'], self.monotonic() + self.attempt_timeout)
            return SendInitiated(claim['attempt_id'])
        try:
            result = self.router.start_with_connection(claim, self.session, now=self.clock(), start=initiate)
            if result is None and not self.outbox.release_unstarted(claim, defer=False): self._unknown(claim)
            return result
        except Exception:
            if native_entered or not self.outbox.release_unstarted(claim, defer=False, before_native=True):
                self._unknown(claim)
            else:
                self.client.expire_attempt(claim['request_extra'])
                self.client.retire_attempt(claim['request_extra'], abandon_retry=True)
            raise

    def expire_deadlines(self):
        """Expire before receiving a late result; timeout never creates retry authority."""
        with self.session.authority_lock:
            now = self.monotonic()
            if not math.isfinite(now):
                self.session.require_attention('telegram_clock_invalid')
                return
            if self._destination_work is not None and now >= self._destination_work[1]:
                self._destination_work = None
                self.destinations.invalidate()
                self.session.require_attention('destination_verification_timeout')
            for delivery_id, evidence in list(self._retry_evidence.items())[:128]:
                if (evidence.get('verification_requested') and not evidence.get('verified')
                        and now >= evidence['verification_deadline']):
                    self.timeout(delivery_id)
            for delivery_id, (attempt_id, deadline) in list(self._deadlines.items())[:128]:
                saved = self.outbox.get(delivery_id)
                if saved is None or saved['attempt_id'] != attempt_id or saved['state'] != 'sending':
                    self._deadlines.pop(delivery_id, None)
                elif now >= deadline:
                    self.timeout(delivery_id)
                    self._deadlines.pop(delivery_id, None)
    def maintain(self):
        """Called only by the ordered owner; one monotonic clock, bounded work per tick."""
        with self.session.authority_lock:
            self.expire_deadlines()
            now = self.monotonic()
            if not self.session.can_transmit(self.clock()):
                return
            for delivery_id, evidence in list(self._retry_evidence.items())[:16]:
                if self._retire_durable_terminal(delivery_id, evidence['attempt_id'], evidence['request_extra']):
                    self._retry_evidence.pop(delivery_id, None)
                    continue
                if now < evidence['deadline']:
                    continue
                saved = self.outbox.get(delivery_id)
                try:
                    proposal = self.router.proposal_for_delivery(saved)
                except ValueError:
                    self.timeout(delivery_id)
                    continue
                if not self.destinations.is_current(saved, proposal):
                    continue
                if not evidence.get('verification_requested'):
                    self.prepare_retry(delivery_id)
                    evidence['verification_requested'] = True
                    evidence['verification_deadline'] = now + self.attempt_timeout
                elif evidence.get('verified'):
                    self.retry(delivery_id, 'owner-retry')
                elif now >= evidence['verification_deadline']:
                    self.timeout(delivery_id)

    def _drain(self):
        # A bounded nonblocking drain ensures already-queued revocations,
        # network changes and failed-message deletion precede authorization.
        for _ in range(128):
            self.poll()
            if not self.client._last_receive_had_event:
                return True
        return False

    def poll(self):
        """Pump one ordered native event via AuthController, then persist only own metadata."""
        with self.session.authority_lock:
            try:
                previous_network = self.auth.snapshot().network_state
                previous_ready = self.auth.snapshot().auth_state == 'ready'
                kind = self.auth.poll()
            except Exception:
                self.session.require_attention('telegram_receive_failed')
                raise
            lost_auth = kind in {'phone','code','password','qr','ready_pending_identity','unsupported','error','submit_error','parameters','logging_out','closing','closed'}
            lost_network = ((kind == 'network_offline' and not self.auth.awaiting_bootstrap_connection())
                or (kind == 'network_connecting' and previous_network == 'online'))
            # A route-driven Auto session collects independently of network
            # delivery. Keep enrolling while its authenticated transport waits;
            # the owner still requires online, fresh verification before claim.
            config=self.session.snapshot.get('config') or {}
            collect_through_network_wait=(getattr(self.router,'selected_route_ids',None) is not None
                and config.get('mode')=='auto' and bool(config.get('route_ids'))
                and self.auth.status=='ready' and self.auth.snapshot().auth_state=='ready'
                and not self.auth.snapshot().revocation_pending)
            if self.session.snapshot['state'] == 'running' and ((previous_ready and lost_auth)
                    or lost_network and not collect_through_network_wait):
                self.session.require_attention('telegram_authority_changed')
            while (event := self.client.take_delivery_event()) is not None:
                saved = self.outbox.get(event.delivery_id)
                if saved is None:
                    continue
                if (saved['attempt_id'] != event.attempt_id or saved['request_extra'] != event.request_extra
                    or saved['account_id'] != event.account_id or saved['account_epoch'] != event.epoch
                    or saved['destination_id'] != event.chat_id or saved['sender'] != {'kind':event.sender_kind,'id':event.sender_id}
                    or saved['content_hash'] != event.content_hash):
                    continue
                if saved['state'] not in {'sending','retryable'}:
                    if event.kind in {'succeeded','failed','unknown','retry_invalidated'}:
                        self.client.retire_attempt(event.request_extra, abandon_retry=True)
                        self._originals.discard((event.delivery_id, event.attempt_id))
                    continue
                with self.router.db.connection() as con:
                    row = con.execute("""SELECT p.payload FROM routing_plans p JOIN routing_delivery_context c ON c.plan_id=p.id
                        WHERE c.delivery_id=? AND c.initiated_attempt_id=?""", (event.delivery_id, event.attempt_id)).fetchone()
                if row is None and (event.delivery_id, event.attempt_id) not in self._originals:
                    continue  # Body retention may remove the plan, never own-attempt provenance.
                if row is not None and json.loads(row[0])['destination_version'] != event.destination_version:
                    continue
                if event.kind in {'retry_verified', 'retry_invalidated'}:
                    evidence = self._retry_evidence.get(event.delivery_id)
                    if saved['state'] != 'retryable' or evidence is None or evidence['attempt_id'] != event.attempt_id:
                        continue
                    if event.kind == 'retry_invalidated':
                        self._retry_evidence.pop(event.delivery_id, None)
                        self.outbox.unknown_retry(event.delivery_id, event.attempt_id)
                        self._retire_durable_terminal(event.delivery_id, event.attempt_id, event.request_extra)
                    else:
                        stamp = self.monotonic()
                        if math.isfinite(stamp):
                            evidence['verified'] = True
                            evidence['deadline'] = max(evidence['deadline'], stamp + event.retry_after)
                    continue
                if saved['state'] != 'sending':
                    continue
                stamp = self.monotonic() if event.kind == 'failed' and event.can_retry else None
                retry_proven = (stamp is not None and math.isfinite(stamp)
                    and (event.delivery_id, event.attempt_id) in self._originals
                    and self.session.can_transmit(self.clock()))
                failure_state = 'retryable' if retry_proven else ('unknown' if event.can_retry else 'failed')
                state = {'initiated':'sending','succeeded':'sent','failed':failure_state,'unknown':'unknown'}[event.kind]
                metadata = {'delivery_id':event.delivery_id, 'attempt_id':event.attempt_id,
                    'request_extra':event.request_extra, 'temporary_message_id':event.temporary_message_id,
                    'final_message_id':event.final_message_id if state == 'sent' else None, 'state':state}
                if self.outbox.transition(event.delivery_id, 'sending', state, metadata):
                    if state == 'sent': self.session.record_counter('sent')
                    if state in {'sent','failed','unknown'}:
                        self.client.retire_attempt(event.request_extra, abandon_retry=True)
                        self._originals.discard((event.delivery_id, event.attempt_id))
                    if state == 'retryable':
                        self._retry_evidence[event.delivery_id] = {'attempt_id':event.attempt_id,
                            'request_extra':event.request_extra, 'failed_id':event.failed_message_id, 'deadline':stamp + event.retry_after, 'verified':False}
                elif event.kind in {'succeeded','failed','unknown'}:
                    # Revocation may have terminalized this exact attempt
                    # between the observational read and the SQLite transition.
                    self._retire_durable_terminal(event.delivery_id, event.attempt_id, event.request_extra)
