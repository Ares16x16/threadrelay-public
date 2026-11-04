"""Narrow local feature operations. No browser, TDLib, model or process startup.

Task 10 must own a long-lived instance and wire it to the private IPC worker;
constructing this object alone does not open a database or establish authority.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

from .contracts import validate_contract
from .storage.database import encode
from .storage.routing import ConfigStore
from .routing.policy import _destination
from .storage.outbox import _current_account, AccountStore
from .routing.approvals import ApprovalStore, ReviewDisclosure
from .delivery.message import format_outgoing


class LocalFeatures:
    def __init__(self, database, *, router=None, account=None, destination=None, threads=None, session=None, wake_delivery=lambda: None, automatic=None, sample=None, workflow=None, listing=None, app_status=None, activity=None, owner_test=None, owner_history=None, owner_automatic=None):
        self.db = database
        self.workflow = workflow
        self.listing, self.app_status = listing, app_status
        self.activity = activity
        self.owner_test = owner_test
        self.owner_history = owner_history
        self.owner_automatic = owner_automatic
        self.session, self.wake_delivery = session, wake_delivery
        self.automatic, self.sample = automatic, sample
        self.config = ConfigStore(database)
        self.router = router
        self.account_features = account
        self.destination_features = destination
        self.threads_features = threads

    def delivery_workflow(self, kind, payload):
        workflow = self.workflow
        if workflow is None:
            raise NotImplementedError
        if kind == 'QueryDeliveryWorkspace':
            return 'DeliveryWorkspace', workflow.workspace()
        if kind == 'SaveMessageTemplate':
            return 'MessageTemplate', {'template': workflow.save_template(payload)}
        if kind == 'ConfigureDeliveryDraft':
            return 'DeliveryDraft', {'draft': workflow.configure(payload)}
        if kind == 'GetDeliveryDraft':
            return 'DeliveryDraft', {'draft': workflow.get_draft(payload['draft_id'])}
        if kind == 'PreviewDeliveryDraft':
            return 'DeliveryPreview', workflow.preview(payload['draft_id'], payload['version'])
        if kind == 'ApproveDeliveryDraft':
            return 'DeliveryJobs', workflow.approve(payload['draft_id'], payload['version'], payload['preview_hash'])
        if kind == 'QueryDeliveryJobs':
            return 'DeliveryJobs', workflow.query_jobs(**payload)
        action = {'PauseDeliveryJob': 'pause', 'ResumeDeliveryJob': 'resume', 'CancelDeliveryJob': 'cancel'}.get(kind)
        if action is None:
            raise ValueError('Unsupported delivery operation')
        return 'DeliveryJob', {'job': getattr(workflow, action)(payload['job_id'], payload['version'])}

    def retention(self):
        with self.db.connection() as con:
            row = con.execute("SELECT payload FROM local_policy WHERE name='retention'").fetchone()
        return json.loads(row[0]) if row else {'text_days':7, 'dedup_days':30, 'diagnostic_days':7, 'confirmed':False}

    def save_retention(self, policy):
        validate_contract('RetentionPolicy', policy)
        if not policy['confirmed'] or policy['dedup_days'] < policy['text_days']:
            raise ValueError('Explicit retention confirmation required')
        with self.db.transaction() as con:
            con.execute("INSERT INTO local_policy VALUES ('retention',?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload", (encode(policy),))

    def topics(self):
        return self.config.topics()

    def routes(self):
        return self.config.routes()

    def save_topic(self, topic):
        self.config.save_topic(topic)

    def _validate_route(self, route, con):
        if route['enabled']:
            _current_account(con, route)
            destination, _ = _destination(con, route['account_id'], route['destination_id'], route['account_epoch'])
            if destination['sender'] != route['sender']:
                raise ValueError('Sender mismatch')
            available = {t['id'] for t in ConfigStore.read_all(con, 'routing_topics') if t['enabled']}
            if not set(route['topic_ids']) <= available:
                raise ValueError('Topic unavailable')
        old = con.execute('SELECT version,payload FROM routing_routes WHERE id=?', (route['id'],)).fetchone()
        if old and (route['version'] < old['version'] or (route['version'] == old['version'] and old['payload'] != encode(route))):
            raise ValueError('Configuration version must increase')

    def preview_auto_route(self, route):
        validate_contract('Route', route)
        if self.automatic is None:
            raise ValueError('Auto-send gate unavailable')
        with self.db.transaction() as con:
            self._validate_route(route, con)
            return self.automatic.preview(route, connection=con)

    def save_route(self, route, *, expected_auto_receipt=None):
        validate_contract('Route', route)
        from .delivery.workflow_templates import validate_template
        validate_template(route.get('message_template', '{{url}}'))
        queue = route.get('queue') or {}
        if queue.get('mode') == 'random' and queue['minimum_seconds'] > queue['maximum_seconds']:
            raise ValueError('invalid_queue_interval')
        # Route configuration and activation share the writer with revocation/send-start.
        # This never replaces Sender/Router live authority checks.
        with self.db.transaction() as con:
            self._validate_route(route, con)
            if route['enabled'] and route['mode'] == 'auto' and not getattr(self.automatic, 'route_driven', False):
                if self.automatic is None or expected_auto_receipt is None:
                    raise ValueError('Auto-send gate unavailable')
                self.automatic.opt_in(route, expected_auto_receipt, connection=con)
            old = con.execute('SELECT payload FROM routing_routes WHERE id=?', (route['id'],)).fetchone()
            if old and old[0] != encode(route):
                ConfigStore.invalidate_route_queue(con, route['id'])
            con.execute('INSERT INTO routing_routes(id,version,payload) VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version,payload=excluded.payload', (route['id'], route['version'], encode(route)))

    def route_queue(self, payload):
        from .delivery.route_schedule import RouteSchedule
        return RouteSchedule(self.db).query(**payload)

    def set_route_queue_paused(self, route_id, paused):
        from .delivery.route_schedule import RouteSchedule
        RouteSchedule(self.db).pause(route_id, paused)
        self.wake_delivery()

    def cancel_route_queue_item(self, delivery_id):
        from .delivery.route_schedule import RouteSchedule
        RouteSchedule(self.db).cancel(delivery_id)

    def clear_route_queue(self, route_id):
        from .delivery.route_schedule import RouteSchedule
        with self.session.authority_lock if self.session is not None else nullcontext():
            return RouteSchedule(self.db).clear(route_id)

    def history(self):
        with self.db.connection() as con:
            rows = con.execute('SELECT id,payload FROM archive_deliveries ORDER BY created_at DESC,id LIMIT 10000').fetchall()
            result = []
            for row in rows:
                attempts = [dict(a) for a in con.execute('SELECT delivery_id,attempt_id,request_extra,temporary_message_id,final_message_id,state FROM archive_attempts WHERE delivery_id=? ORDER BY rowid', (row['id'],))]
                result.append({'delivery': json.loads(row['payload']), 'attempts': attempts})
            return result

    def archive_posts(self, request):
        from .storage.archive import ArchiveStore
        return ArchiveStore(self.db).list_posts(**request)

    def archive_deliveries(self, request):
        from .storage.archive import ArchiveStore
        return ArchiveStore(self.db).list_deliveries(**request)

    def archive_runs(self, request):
        from .storage.archive import ArchiveStore
        return ArchiveStore(self.db).list_runs(**request)

    def archive_detail(self, request):
        from .storage.archive import ArchiveStore
        return ArchiveStore(self.db).read_detail(**request)

    def collection(self):
        from .storage.collection import CollectionStore
        return CollectionStore(self.db).query()

    def account(self):
        if self.account_features is not None:
            return self.account_features.query()
        # Persisted state is not live verification. No client is constructed.
        with self.db.connection() as con:
            row = con.execute('SELECT snapshot FROM accounts ORDER BY active DESC, rowid DESC LIMIT 1').fetchone()
        if row:
            snapshot = json.loads(row[0])
            if snapshot['auth_state'] == 'ready':
                snapshot.update(auth_state='unknown', network_state='offline', display_name=None)
        else:
            snapshot = {'account_id': None, 'epoch': 0, 'auth_state': 'logged_out', 'display_name': None, 'network_state': 'offline', 'revocation_pending': False}
        return {'snapshot': snapshot, 'challenge': None, 'status': 'revocation_pending' if snapshot['revocation_pending'] else 'idle', 'pending_revocations': [s['account_id'] for s in AccountStore(self.db).pending_revocations()]}

    def review(self):
        if self.router is None:
            raise NotImplementedError
        result = []
        with self.db.transaction() as con:
            ids = [row[0] for row in con.execute("SELECT id FROM routing_plans WHERE id NOT IN (SELECT plan_id FROM routing_delivery_context) ORDER BY id LIMIT 10000")]
            for plan_id in ids:
                try:
                    receipt, post, destination = self.router._receipt(con, plan_id)
                    if not receipt['approval_required'] or receipt['mode'] == 'dry_run':
                        continue
                    proposal = {'plan_id': plan_id, 'post': post, 'route': receipt['routes'][0],
                        'labels': receipt['labels'], 'approval_required': True, 'destination': destination,
                        'destination_version': receipt['destination_version'],
                        'route_refs': [{'route_id': r['id'], 'route_version': r['version'], 'approval_id': None} for r in receipt['routes']]}
                    self.router._proposal(con, proposal)
                    classification = receipt.get('classification')
                    ids = {r['topic_id'] for r in classification['topic_results']} if classification else set(receipt['labels'])
                    topics = [{k: topic[k] for k in ('id', 'version', 'description')} for topic in ConfigStore.read_all(con, 'routing_topics') if topic['id'] in ids]
                    result.append({"proposal": proposal, "outgoing_text": format_outgoing(post), "classification": classification, "topics": topics})
                except ValueError:
                    # Stale or revoked plans are not reviewable authority.
                    continue
        return result

    def approve_review(self, proposal, disclosure):
        if self.router is None:
            raise NotImplementedError
        with (self.session.authority_lock if self.session is not None else nullcontext()):
            approval = ApprovalStore(self.router).approve(proposal, disclosure=ReviewDisclosure(**disclosure))
            _, created = self.router.stage(proposal, approval=approval, report_created=True)
            if created:
                # This session's newly staged deliveries; stopped staging remains in History.
                if self.session is not None and self.session.snapshot['state'] == 'running':
                    self.session.record_counter('queued')
                self.wake_delivery()

    def bulk_review(self, request, *, dismiss=False):
        validate_contract('BulkDismissRequest' if dismiss else 'BulkReviewRequest', request)
        ids = [item['plan_id'] if dismiss else item['proposal'].get('plan_id') for item in request['items']]
        if len(set(ids)) != len(ids):
            raise ValueError('Distinct review items required')
        results = []
        for item, plan_id in zip(request['items'], ids):
            try:
                if dismiss:
                    self.dismiss_review(plan_id)
                else:
                    self.approve_review(item['proposal'], item['disclosure'])
                status = 'dismissed' if dismiss else 'staged'
            except ValueError:
                status = 'rejected'
            results.append({'plan_id':plan_id, 'status':status})
        return {'results':results}

    def dismiss_review(self, plan_id):
        # Delete only the exact unstaged receipt, not post/history or deliveries.
        with self.db.transaction() as con:
            con.execute('DELETE FROM routing_plans WHERE id=? AND NOT EXISTS (SELECT 1 FROM routing_delivery_context WHERE plan_id=?)', (plan_id, plan_id))


class AccountFeatures:
    """Trusted adapter; no client construction or receive on query.

    This is not a production native gate. The assembly must provide a verified
    gate and a one-shot secure reference disposer. AuthController.poll remains
    the only native receive owner; none of these methods starts another loop.
    """
    def __init__(self, auth, *, vault_reference, native_gate, discard_secret_ref, wall_clock, lifecycle=None):
        self.auth, self.vault_reference, self.native_gate = auth, vault_reference, native_gate
        self.lifecycle = lifecycle
        self.discard_secret_ref = discard_secret_ref
        self.wall_clock = wall_clock
        self._handoff_failed = False

    def query(self):
        with self.auth._lifecycle_lock:
            challenge = self.auth.challenge()
            projected = None
            if challenge is not None:
                # Sample presentation wall time before the final monotonic TTL
                # so time spent in the handoff cannot extend the UI deadline.
                wall = self.wall_clock()
                if not isinstance(wall, datetime) or wall.tzinfo is None or wall.utcoffset() is None:
                    raise ValueError('UTC presentation clock unavailable')
                challenge, remaining = self.auth.challenge_with_remaining()
                if challenge is not None:
                    projected = asdict(challenge)
                    if projected.get('code_delivery') is None:projected.pop('code_delivery',None)
                    projected['expires_at'] = (wall.astimezone(timezone.utc) + timedelta(seconds=remaining)).isoformat()
            return {'snapshot': self.auth.snapshot().as_contract(), 'challenge': projected, 'status': self.auth.status, 'pending_revocations': [s['account_id'] for s in self.auth._accounts.pending_revocations()]}

    def begin(self):
        with self.auth._lifecycle_lock:
            if self._handoff_failed or not self.native_gate():
                raise NotImplementedError
            self.auth.begin(self.vault_reference(), pending=self.auth._accounts.has_unresolved())

    def restore(self):
        with self.auth._lifecycle_lock:
            if self._handoff_failed or not self.native_gate():raise NotImplementedError
            reference=self.auth.saved_restore_reference()
            if reference is None or not self.auth._client.has_existing_session(reference):return
            actual=self.vault_reference()
            if actual!=reference:
                from .delivery.auth import AuthFailure
                raise AuthFailure('Saved account reference changed')
            self.auth.begin(actual,pending=self.auth._accounts.has_unresolved(),restoring=True)

    def cancel(self):
        self.auth.cancel()

    def submit_secure(self, challenge_id, kind, epoch, secret_ref):
        # Reference disposal is part of this one-shot boundary even on stale or
        # failed submissions. It is never accepted in an ordinary Command DTO.
        with self.auth._lifecycle_lock:
            try:
                if self._handoff_failed or not self.native_gate(): raise NotImplementedError
                if self.auth.snapshot().epoch != epoch:
                    from .delivery.auth import AuthFailure
                    raise AuthFailure('Invalid or expired challenge')
                self.auth.submit(challenge_id, kind, secret_ref)
            finally:
                try:
                    self.discard_secret_ref(secret_ref)
                except Exception:
                    self._handoff_failed = True
                    try: self.auth.cancel()
                    except Exception: pass  # Authority remains latched; do not echo native errors.
                    from .delivery.auth import AuthFailure
                    raise AuthFailure('Secure input unavailable') from None

    def request_qr(self, challenge_id, epoch):
        with self.auth._lifecycle_lock:
            if self._handoff_failed or not self.native_gate(): raise NotImplementedError
            if self.auth.snapshot().epoch != epoch:
                from .delivery.auth import AuthFailure
                raise AuthFailure('Invalid or expired challenge')
            self.auth.request_qr(challenge_id)

    def resend_code(self, challenge_id, epoch):
        with self.auth._lifecycle_lock:
            if self._handoff_failed or not self.native_gate():raise NotImplementedError
            if self.auth.snapshot().epoch!=epoch:
                from .delivery.auth import AuthFailure
                raise AuthFailure('Invalid or expired challenge')
            self.auth.resend_code(challenge_id)

    def logout(self, mode):
        if mode == 'close_local':
            self.auth.cancel()
        else:
            if self.lifecycle is None:
                raise NotImplementedError
            self.lifecycle.account_action(mode)


class DestinationFeatures:
    """Explicit known-chat setup. AuthController.poll owns the ordered receive.

    Querying only observes/finalizes already-correlated local evidence; it does
    not poll, reconnect, enumerate chats or start any network request.
    """
    def __init__(self, verifier, *, native_gate, self_gate=None, metadata_gate=None, listing=None):
        self.verifier, self.native_gate = verifier, native_gate
        self.self_gate = native_gate if self_gate is None else self_gate
        self.metadata_gate = native_gate if metadata_gate is None else metadata_gate
        self.listing = listing
        self.phase = 'idle'
        self.snapshot = None
        self.selection = None
        self.operation = None
        self._verified_kind = None

    def query_saved(self):
        from .storage.saved_destinations import SavedDestinations
        account=self.verifier.auth.snapshot()
        return SavedDestinations(self.verifier.config.db).query(account.account_id,account.epoch)

    def save_saved(self, request):
        from .storage.saved_destinations import SavedDestinations
        with self.verifier.session.authority_lock:
            account=self.verifier.auth.snapshot()
            snapshot=self.snapshot
            evidence=self.verifier.evidence
            if (self._owner_test_busy() or self.phase!='verified' or snapshot is None or evidence is None
                    or not self.metadata_gate() or account.auth_state!='ready' or account.network_state!='online'
                    or account.revocation_pending or not self.verifier.client.transport_ready
                    or request!={'chat_id':snapshot['chat_id'],'account_id':account.account_id,'account_epoch':account.epoch}
                    or snapshot['account_id']!=account.account_id or self._verified_kind is None
                    or snapshot['paid_required'] or not snapshot['permissions']['can_send']
                    or not self.verifier.is_current({'account_id':account.account_id,'account_epoch':account.epoch,
                        'destination_id':snapshot['chat_id'],'sender':snapshot['sender']},
                        {'destination':snapshot,'destination_version':evidence[1]})):
                raise ValueError('Verify this exact chat before saving')
            SavedDestinations(self.verifier.config.db).save_verified(snapshot,account.epoch,self._verified_kind)

    def _owner_test_busy(self):
        client=getattr(self.verifier,'client',None)
        return any(owner is not None and owner.busy for owner in
            (getattr(client,'_owner_test',None),getattr(client,'_owner_history',None),getattr(client,'_owner_automatic',None)))

    def begin_verify(self, request):
        with self.verifier.session.authority_lock:
            if self._owner_test_busy(): raise ValueError('Telegram test owns verification')
            if not self.metadata_gate(): raise NotImplementedError
            self.snapshot = None
            self._verified_kind = None
            chat, kind, sender = request['chat_id'], request['kind'], request['sender']
            account = self.verifier.auth.snapshot()
            if kind == 'private':
                if sender is not None: raise ValueError('Unexpected sender')
                self.verifier.begin_private(chat)
            else:
                if sender is None: raise ValueError('Sender required')
                selected = self.selection == (account.account_id, account.epoch, chat, sender['kind'], sender['id'])
                selected = selected or self.listing is not None and self.listing.sender_matches(chat, sender)
                self.verifier.begin_group(chat, sender=sender, sender_selected=selected)
            self.phase, self.operation = 'pending', 'verify'

    def begin_saved_messages(self):
        with self.verifier.session.authority_lock:
            if self._owner_test_busy(): raise ValueError('Telegram test owns verification')
            if not self.self_gate(): raise NotImplementedError
            self.snapshot, self.selection = None, None
            self._verified_kind = None
            self.verifier.begin_saved_messages()
            self.phase, self.operation = 'pending', 'verify'

    def begin_select(self, request):
        with self.verifier.session.authority_lock:
            if self._owner_test_busy(): raise ValueError('Telegram test owns verification')
            if not self.native_gate(): raise NotImplementedError
            self.selection = None
            self.snapshot = None
            self.verifier.select_sender(request['chat_id'], request['sender'])
            self.phase, self.operation = 'pending', 'select'

    def query(self):
        with self.verifier.session.authority_lock:
            if self._owner_test_busy(): return {'state':'pending','snapshot':None}
            metadata_allowed = self.operation == 'verify' and self.metadata_gate()
            self_allowed = self.self_gate() and ((self.verifier.pending or {}).get('saved_messages') is True or (self.snapshot or {}).get('chat_id') == self.verifier.auth.snapshot().account_id)
            if not self.native_gate() and not metadata_allowed and not self_allowed:
                self.phase, self.snapshot, self.selection = 'failed', None, None
                return {'state': 'failed', 'snapshot': None}
            if self.phase == 'pending':
                pending = self.verifier.pending
                if pending is None:
                    self.phase = 'failed'
                elif self.operation == 'verify' and pending['stage'] == 'complete':
                    self._verified_kind='saved_messages' if pending.get('saved_messages') else ('channel' if pending.get('is_channel') else 'group') if pending.get('group') else 'private'
                    self.snapshot, _ = self.verifier.finish()
                    self.phase = 'verified'
                elif self.operation == 'select' and pending['stage'] == 'select_complete':
                    account = self.verifier.auth.snapshot()
                    chat = pending['chat_id']
                    sender = self.verifier.finish_selection()
                    self.selection = (account.account_id, account.epoch, chat, sender['kind'], sender['id'])
                    self.phase = 'sender_selected'
            if self.phase == 'verified' and self.verifier.evidence is None:
                self.snapshot = None
                self.phase = 'failed'
            return {'state': self.phase, 'snapshot': self.snapshot if self.phase == 'verified' else None}


class ThreadsFeatures:
    """Only the trusted app-owned profile, never a renderer path or URL."""
    def __init__(self, browser, *, app_data_dir, open_owned=None):
        from pathlib import Path
        root = Path(app_data_dir)
        if not root.is_absolute(): raise ValueError('Trusted app data root required')
        self.browser = browser
        self.open_owned = open_owned
        self.profile = root.resolve() / 'browser-profiles' / 'threads'

    def open_login(self):
        # BrowserSession enforces the dedicated profile and owned launcher.
        if self.open_owned is not None:
            self.open_owned()
        else:
            self.browser.open(self.profile)
