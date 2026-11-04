"""Exact automatic authority for durable, route-bound message snapshots.

Legacy policy-backed intents remain readable for compatibility. Production uses
saved route configuration, an explicitly selected Auto route and exact native
account/destination/correlation checks.
"""
from copy import deepcopy
import json

from .message import format_outgoing
from threadrelay.evaluation.dataset import digest
from threadrelay.storage.database import encode
from threadrelay.storage.outbox import Outbox


class AutomaticQueue:
    def __init__(self, router, automatic, sender, clock):
        self.router,self.automatic,self.sender,self.clock=router,automatic,sender,clock
        self.db,self.auth=router.db,sender.auth
        self.outbox=Outbox(self.db)
        self._uncertain=set()
        self._active_connection=None
        self._scan_after=None
        from .route_schedule import RouteSchedule
        self.schedule = RouteSchedule(self.db)

    def _score_policy(self, con=None):
        if self.automatic.route_driven:
            return digest({'authority':'route-driven-v1'})
        policy=self.automatic.policy(con)
        if policy is None or policy.get('method')!='topic_score':raise ValueError('score_policy_required')
        return digest(self.automatic._basis(con)[0]) if con is not None else digest(self.automatic.binding())

    def profile_ready(self, profile_id):
        with self.db.connection() as con:
            return con.execute("SELECT 1 FROM local_policy WHERE name IN (?,?)",
                ('automatic-incomplete:'+profile_id,'manual-history-incomplete:'+profile_id)).fetchone() is None

    def recheck_active(self, plan, *, in_flight=False):
        if plan['job_id'] in self._uncertain:raise ValueError('automatic_outcome_unknown')
        con=self._active_connection
        if con is not None:
            # Router.begin_send already checked limits before reserving this
            # start. Refreshing inside its writer could invoke cancellation
            # callbacks while that writer remains held.
            snapshot=self.sender.session.snapshot
            if snapshot['state']!='running' or snapshot.get('config',{}).get('mode')!='auto':
                raise ValueError('session_not_running')
        elif in_flight and self.automatic.route_driven:
            pass
        elif in_flight:
            from .sender import COLLECTION_ONLY_REASONS
            snapshot=self.sender.session.snapshot
            if snapshot['state']!='running' and snapshot['attention_reason'] not in COLLECTION_ONLY_REASONS:
                raise ValueError('session_not_running')
        elif not in_flight and not self.sender.session.can_transmit(self.clock.now(),automatic=True):
            raise ValueError('session_not_running')
        if con is None:
            with self.db.connection() as con:self._recheck(con,plan,in_flight=in_flight)
        else:self._recheck(con,plan,in_flight=in_flight)

    def _recheck(self, con, plan, *, in_flight=False):
        if self._score_policy(con)!=plan['policy_hash']:raise ValueError('automatic_policy_changed')
        if self.automatic.route_driven and not in_flight:
            row = self.schedule.require_ready(con, plan['job_id'])
            if self.router.selected_route_ids() != [row['route_id']] or row['text'] != plan['text']:
                raise ValueError('selected_route_changed')
        receipt,current,destination=self.router._proposal(con,plan['proposal'])
        topics = {topic for route in receipt['routes'] for topic in route['topic_ids']}
        matched = any(result['topic_id'] in topics and result['match'] and not result['abstained'] for result in receipt['classification']['topic_results'])
        uncertain = not matched if self.automatic.route_driven else receipt['classification']['abstained']
        if (receipt['mode']!='auto' or receipt['approval_required']
                or not self.automatic.source_authorized(current,receipt['routes']) or uncertain
                or not all(self.automatic.route_authorized(route) for route in receipt['routes'])):
            raise ValueError('automatic_authority_changed')
        target=plan['target']
        if (destination!=plan['destination'] or target['sender']!={'kind':'user','id':target['account_id']}
                or destination['audience']=='unknown' or int(target['destination_id'])>=0):
            raise ValueError('automatic_target_changed')

    def plan(self, delivery_id, version=1):
        if version!=1:raise ValueError('automatic_intent_changed')
        delivery=self.outbox.get(delivery_id)
        if delivery is None or delivery['state']!='pending' or any(ref['approval_id'] is not None for ref in delivery['route_refs']):
            raise ValueError('pristine_automatic_intent_required')
        proposal=self.router.proposal_for_delivery(delivery)
        target={key:deepcopy(delivery[key]) for key in ('account_id','account_epoch','destination_id','sender')}
        target['destination_version']=proposal['destination_version']
        scheduled = self.schedule.get(delivery_id) if self.automatic.route_driven else None
        if self.automatic.route_driven and scheduled is None:
            raise ValueError('route_queue_snapshot_required')
        plan={'job_id':delivery_id,'job_version':1,'revision_id':None,'delivery':delivery,
            'text':scheduled['text'] if scheduled else format_outgoing(proposal['post']),'destination':proposal['destination'],'target':target,
            'proposal':proposal,'policy_hash':self._score_policy()}
        self.recheck_active(plan)
        if not self.router.authorize(delivery,self.sender.session)['allowed']:raise ValueError('automatic_intent_blocked')
        return plan

    def due(self):
        try:
            if not self.sender.session.can_transmit(self.clock.now(),automatic=True):return None
            self._score_policy()
            with self.db.connection() as con:
                if self._scan_after is None:
                    rows=con.execute("SELECT id,created_at FROM deliveries WHERE state='pending' ORDER BY created_at,id LIMIT 100").fetchall()
                else:
                    stamp,identifier=self._scan_after
                    rows=con.execute("SELECT id,created_at FROM deliveries WHERE state='pending' AND (created_at>? OR (created_at=? AND id>?)) ORDER BY created_at,id LIMIT 100",
                        (stamp,stamp,identifier)).fetchall()
            if not rows:
                self._scan_after=None
                return None
            for row in rows:
                self._scan_after=(row['created_at'],row['id'])
                try:return self.plan(row['id'])
                except ValueError as error:
                    if self.automatic.route_driven and str(error) in {
                            'classification_not_current', 'post_unavailable', 'post_not_canonical',
                            'content_snapshot_mismatch', 'routing_configuration_changed', 'route_revoked',
                            'destination_changed', 'destination_unavailable', 'destination_not_zero_paid_sendable',
                            'sender_identity_mismatch', 'sender_selection_required', 'delivery_binding_mismatch',
                            'route_scope_mismatch', 'proposal_not_current', 'proposal_receipt_integrity',
                            'proposal_binding_mismatch', 'automatic_authority_changed', 'automatic_target_changed'}:
                        self.schedule.block_unstarted(row['id'], str(error))
                    continue
        except Exception:return None
        return None

    def preflight_failed(self, plan, reason):
        """Preserve queued work across collection/queue pause; expose real failures."""
        delivery_id = plan['job_id']
        self._scan_after = None
        if not self.automatic.route_driven:
            self.cancel(delivery_id)
            return
        if delivery_id in self._uncertain or not self.sender.session.can_transmit(self.clock.now(), automatic=True):
            return
        safe_reason = reason if reason in {'verification_failed', 'preflight_unavailable', 'timed_out', 'authority_changed',
            'destination_title_changed','destination_chat_mismatch','destination_member_mismatch','destination_sender_unverified',
            'destination_verification_interrupted','destination_verification_incomplete','destination_snapshot_changed',
            'preflight_retry_exhausted'} else 'preflight_unavailable'
        self.schedule.block_unstarted(delivery_id, safe_reason, require_unpaused=True)

    def claim(self, delivery_id, version, expected_plan, profile_id):
        if not self.profile_ready(profile_id) or self.plan(delivery_id,version)!=expected_plan:raise ValueError('automatic_intent_changed')
        if not self.sender.destinations.is_current(expected_plan['delivery'],expected_plan['proposal']):
            raise ValueError('fresh_destination_required')
        claim=self.outbox.claim_next('score-automatic-owner',only_id=delivery_id)
        if claim is None:raise ValueError('automatic_claim_unavailable')
        try:
            with self.db.transaction() as con:
                con.execute('INSERT INTO local_policy(name,payload) VALUES (?,?)',('automatic-incomplete:'+profile_id,
                    encode({'delivery_id':delivery_id,'attempt_id':claim['attempt_id']})))
        except Exception:
            self._uncertain.add(delivery_id)
            raise
        return {'job_id':delivery_id,'job_version':1,'delivery':claim,'plan':deepcopy(expected_plan),'profile_id':profile_id}

    def initiate(self, claim, initiate):
        def start(con):
            self._active_connection=con
            try:
                self.recheck_active(claim['plan'])
                account=self.auth.snapshot();delivery=claim['delivery']
                if (self.auth.status!='ready' or account.auth_state!='ready' or account.network_state!='online'
                        or account.revocation_pending or (account.account_id,account.epoch)!=(delivery['account_id'],delivery['account_epoch'])
                        or not self.sender.client.transport_ready
                        or not self.sender.destinations.is_current(delivery,claim['plan']['proposal'])):
                    raise ValueError('automatic_account_changed')
                try:
                    initiate()
                finally:
                    if self.automatic.route_driven:
                        self.schedule.started(con, claim['job_id'])
                return True
            finally:self._active_connection=None
        result=self.router.start_with_connection(claim['delivery'],self.sender.session,now=self.clock.now(),start=start)
        if result is not True:raise ValueError('automatic_dispatch_denied')

    def complete(self, claim, temporary, final):
        d=claim['delivery']
        if not self.outbox.transition(d['id'],'sending','sent',{'delivery_id':d['id'],'attempt_id':d['attempt_id'],
                'request_extra':d['request_extra'],'temporary_message_id':temporary,'final_message_id':final,'state':'sent'}):
            raise ValueError('automatic_completion_unavailable')
        self.sender.session.record_counter('sent')

    def complete_failed(self, claim, event):
        """Commit an exact native rejection, without retrying the failed message."""
        d=claim['delivery']
        if not self.outbox.transition(d['id'],'sending','failed',{'delivery_id':d['id'],'attempt_id':d['attempt_id'],
                'request_extra':d['request_extra'],'temporary_message_id':event.temporary_message_id,
                'final_message_id':None,'state':'failed'}):
            raise ValueError('automatic_failure_unavailable')
        label={'FLOOD_WAIT':'telegram_flood_wait','FLOOD_PREMIUM_WAIT':'telegram_flood_wait',
            'SLOWMODE_WAIT':'telegram_slowmode_wait','TELEGRAM_ERROR':'telegram_rejected'}[event.error_reason]
        reason=label+(':'+str(int(event.retry_after)) if event.retry_after>0 else ':'+str(event.error_code))
        result={'delivery_id':d['id'],'attempt_id':d['attempt_id'],'temporary_message_id':event.temporary_message_id,
            'failed_message_id':event.failed_message_id,'error_code':event.error_code,
            'error_reason':event.error_reason,'retry_after':event.retry_after,
            'failure_kind':'request_rejected' if event.request_rejected else 'message_failed'}
        with self.db.transaction() as con:
            con.execute('INSERT INTO local_policy(name,payload) VALUES (?,?)',('automatic-failure:'+d['id'],encode(result)))
            con.execute('UPDATE route_queue SET reason=? WHERE delivery_id=?',(reason,d['id']))
            if event.retry_after<=0:
                con.execute('INSERT INTO route_queue_control(route_id,paused) SELECT route_id,1 FROM route_queue WHERE delivery_id=? ON CONFLICT(route_id) DO UPDATE SET paused=1',(d['id'],))
            if event.retry_after>0:
                until=self.db.now()+event.retry_after
                name='telegram-wait:'+d['account_id']+(':'+d['destination_id'] if event.error_reason=='SLOWMODE_WAIT' else '')
                old=con.execute('SELECT payload FROM local_policy WHERE name=?',(name,)).fetchone()
                if old is not None:until=max(until,json.loads(old[0])['until'])
                con.execute('INSERT INTO local_policy(name,payload) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload',
                    (name,encode({'until':until,'reason':label,'error_code':event.error_code})))

    def note_unknown_provider_error(self, claim, event):
        if event.error_code is None:
            return
        d=claim['delivery']
        label={'FLOOD_WAIT':'telegram_flood_wait','FLOOD_PREMIUM_WAIT':'telegram_flood_wait',
            'SLOWMODE_WAIT':'telegram_slowmode_wait','TELEGRAM_ERROR':'telegram_rejected'}[event.error_reason]
        reason='outcome_unknown;'+label+':'+str(int(event.retry_after) if event.retry_after>0 else event.error_code)
        with self.db.transaction() as con:
            con.execute('UPDATE route_queue SET reason=? WHERE delivery_id=?',(reason,d['id']))
            con.execute('INSERT INTO local_policy(name,payload) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload',
                ('automatic-provider-error:'+d['id'],encode({'attempt_id':d['attempt_id'],'error_code':event.error_code,
                    'error_reason':event.error_reason,'retry_after':event.retry_after})))
            if event.retry_after>0:
                name='telegram-wait:'+d['account_id']+(':'+d['destination_id'] if event.error_reason=='SLOWMODE_WAIT' else '')
                old=con.execute('SELECT payload FROM local_policy WHERE name=?',(name,)).fetchone()
                until=max(self.db.now()+event.retry_after,json.loads(old[0])['until'] if old else 0)
                con.execute('INSERT INTO local_policy(name,payload) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload',
                    (name,encode({'until':until,'reason':label,'error_code':event.error_code})))

    def finish(self, claim):
        with self.db.transaction() as con:
            row=con.execute('SELECT payload,state FROM deliveries WHERE id=?',(claim['job_id'],)).fetchone()
            if row is None or row['state'] not in {'sent','failed'} or json.loads(row['payload'])['attempt_id']!=claim['delivery']['attempt_id']:
                raise ValueError('automatic_completion_unavailable')
            if row['state']=='failed':
                receipt=con.execute('SELECT payload FROM local_policy WHERE name=?',('automatic-failure:'+claim['job_id'],)).fetchone()
                if receipt is None or json.loads(receipt[0])['attempt_id']!=claim['delivery']['attempt_id']:
                    raise ValueError('automatic_failure_unavailable')
            con.execute('DELETE FROM local_policy WHERE name=?',('automatic-incomplete:'+claim['profile_id'],))

    def unknown(self, claim):
        self._uncertain.add(claim['job_id'])
        # A finish commit can succeed before its return is observed. Restore
        # the deny latch on every uncertain completion, including that case.
        with self.db.transaction() as con:
            con.execute('INSERT INTO local_policy(name,payload) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload',
                ('automatic-incomplete:'+claim['profile_id'],encode({'delivery_id':claim['job_id'],'attempt_id':claim['delivery']['attempt_id']})))
        self.unknown_delivery(claim['job_id'])

    def unknown_delivery(self, delivery_id):
        self._uncertain.add(delivery_id)
        with self.db.transaction() as con:
            con.execute("UPDATE route_queue SET reason=COALESCE(reason,'outcome_unknown') WHERE delivery_id=?", (delivery_id,))
        current=self.outbox.get(delivery_id)
        if current is not None and current['state']=='sending':
            self.outbox.transition(delivery_id,'sending','unknown',{'delivery_id':delivery_id,'attempt_id':current['attempt_id'],
                'request_extra':current['request_extra'],'temporary_message_id':current['temporary_message_id'],
                'final_message_id':None,'state':'unknown'})

    def cancel(self, delivery_id):
        if self.automatic.route_driven:self.schedule.cancel(delivery_id)
        else:self.outbox.cancel_pending(delivery_id)
