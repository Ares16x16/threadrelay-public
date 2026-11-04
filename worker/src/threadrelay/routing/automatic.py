"""Deliberate auto policy + host-attested real evidence + exact per-route opt-in.

No proposed threshold is activated by default; synthetic/exported reports and
renderer flags cannot provide the trusted evidence callback. Point estimates and
uncertainty are measured evaluation statistics, not calibrated model scores.
"""
import json
import hmac
import secrets
from copy import deepcopy
from contextlib import nullcontext
from threadrelay.contracts import validate_contract
from threadrelay.evaluation.dataset import digest
from threadrelay.evaluation.evidence import validate_evidence
from threadrelay.evaluation.metrics import LANGUAGES
from threadrelay.storage.database import encode
from threadrelay.storage.routing import ConfigStore


class AutomaticPolicy:
    def __init__(self, db, classifier, evidence=lambda: None, *, route_driven=False):
        self.db, self.classifier, self.evidence = db, classifier, evidence
        self.route_driven = route_driven
        # Only validated previews can mint receipts; outstanding previews expire on restart.
        self._preview_key = secrets.token_bytes(32)

    def policy(self, connection=None):
        with (nullcontext(connection) if connection is not None else self.db.connection()) as con:
            row = con.execute("SELECT payload FROM local_policy WHERE name='automatic'").fetchone()
        return json.loads(row[0]) if row else None

    def save(self, policy):
        validate_contract('AutoPolicy', policy)
        with self.db.transaction() as con:
            con.execute("INSERT INTO local_policy VALUES ('automatic',?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload", (encode(policy),))
            con.execute("INSERT INTO local_policy VALUES ('automatic-revision',?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload", (encode(secrets.token_hex(32)),))
            # Even changing back to an old policy requires renewed route consent.
            con.execute("DELETE FROM local_policy WHERE name LIKE 'auto-route:%'")

    def _basis(self, connection):
        # Read each trusted source once; disclosure and authorization use the same snapshot.
        policy = self.policy(connection)
        metadata = deepcopy(self.classifier.metadata)
        if policy is None:
            raise ValueError('confirmed_policy_and_trusted_real_evidence_required')
        validate_contract('AutoPolicy', policy)
        topics = ConfigStore.read_all(connection, 'routing_topics')
        if policy.get('method') == 'topic_score':
            # Explicit owner-selected relevance policy. No evaluation statistic
            # is invented or treated as a calibrated per-post probability.
            if not any(topic['enabled'] for topic in topics):
                raise ValueError('enabled_topic_required')
            binding = {'policy':digest(policy), 'evidence':None, 'topics':digest(topics), 'classifier':digest(metadata)}
            return binding, policy, None, topics
        report = deepcopy(self.evidence())
        if report is None:
            raise ValueError('confirmed_policy_and_trusted_real_evidence_required')
        validate_evidence(report)
        # The classifier has no trusted per-item language restriction. Partial
        # language evidence must not authorize arbitrary multilingual posts.
        if set(policy['languages']) != set(LANGUAGES):
            raise ValueError('all_supported_language_criteria_required')
        versions = report['versions']
        if versions['synthetic_dataset'] != 'false' or versions['dataset_split'] != 'test':
            raise ValueError('real_held_out_evidence_required')
        if versions['topics_sha256'] != digest(topics):
            raise ValueError('topics_changed')
        for name, value in metadata.items():
            expected = value if isinstance(value, str) else json.dumps(value, allow_nan=False)
            if versions.get(name) != expected:
                raise ValueError('classifier_configuration_changed')
        metrics = report['metrics']
        for language in policy['languages']:
            selected = metrics['language_slices'].get(language)
            if selected is None:
                raise ValueError('language_evidence_missing')
            for topic in (t for t in topics if t['enabled']):
                counts = selected['per_topic'][topic['id']]
                if (counts['precision'] is None or counts['recall'] is None
                    or counts['precision'] < policy['min_precision'] or counts['recall'] < policy['min_recall']
                    or counts['positive_count'] < policy['min_positive'] or counts['predicted_positive_count'] < policy['min_predicted']):
                    raise ValueError('confirmed_criteria_not_met')
        binding = {'policy':digest(policy),'evidence':digest(report),'topics':digest(topics),'classifier':digest(metadata)}
        return binding, policy, report, topics

    def binding(self):
        with self.db.connection() as con:
            return self._basis(con)[0]

    def authorized(self):
        if self.route_driven:
            return True
        try:
            self.binding()
            return True
        except Exception:
            return False

    def source_authorized(self, post, routes):
        """Only exact score-route consent may share an audience-unverified link.

        The source snapshot remains Unknown. No extracted body is sent, and
        restricted sources or any other collection quality issue stay blocked.
        """
        if self.route_driven:
            return True
        if post['visibility']=='public' and not post['quality_flags']:
            return True
        policy=self.policy()
        return bool(policy and policy.get('method')=='topic_score' and routes
            and all(route.get('allow_unverified_source') is True for route in routes)
            and post['visibility']=='unknown'
            and all(flag['code']=='visibility_unverified' for flag in post['quality_flags']))

    def status(self):
        if self.route_driven:
            return {'policy':None, 'evidence_available':False, 'eligible':True, 'reason':'route_selection_required'}
        try:
            evidence = self.evidence() is not None
        except Exception:
            evidence = False
        try:
            self.binding()
            eligible, reason = True, ('topic_score_requires_per_route_opt_in' if self.policy().get('method') == 'topic_score' else 'eligible_requires_per_route_opt_in')
        except Exception as error:
            eligible, reason = False, str(error) if isinstance(error, ValueError) else 'trusted_evidence_unavailable'
        return {'policy':self.policy(),'evidence_available':evidence,'eligible':eligible,'reason':reason[:128]}

    def _route_binding(self, route, connection, basis=None):
        row = connection.execute('SELECT payload,version FROM routing_destinations WHERE account_id=? AND chat_id=? AND account_epoch=?',
            (route['account_id'],route['destination_id'],route['account_epoch'])).fetchone()
        if row is None or row['payload'] is None:
            raise ValueError('destination_changed')
        if self.policy(connection).get('method') == 'topic_score':
            destination = json.loads(row['payload'])
            if (route['sender'] != {'kind':'user','id':route['account_id']}
                    or int(route['destination_id']) >= 0 or destination['audience'] == 'unknown'):
                raise ValueError('saved_group_personal_sender_required')
            saved = connection.execute('SELECT payload FROM local_policy WHERE name=?',
                (f"saved-telegram-destination:{route['account_id']}:{route['destination_id']}",)).fetchone()
            selected = json.loads(saved[0]) if saved else None
            if (selected is None or selected.get('kind') != 'group' or selected.get('account_epoch') != route['account_epoch']
                    or any(selected.get(key) != destination[key] for key in ('account_id','chat_id','title','sender'))):
                raise ValueError('saved_group_personal_sender_required')
        return {**(basis if basis is not None else self._basis(connection)[0]), 'route':digest(route), 'destination':digest(json.loads(row['payload'])), 'destination_version':row['version']}

    def _preview(self, route, connection):
        if route['mode'] != 'auto' or not route['enabled']:
            raise ValueError('Enabled auto route required')
        basis, policy, report, topics = self._basis(connection)
        binding = self._route_binding(route, connection, basis)
        row = connection.execute('SELECT payload FROM routing_destinations WHERE account_id=? AND chat_id=?',
            (route['account_id'], route['destination_id'])).fetchone()
        disclosure = {'route':route, 'destination':json.loads(row['payload']),
            'destination_version':binding['destination_version'], 'policy':policy,
            'evidence_sha256':binding['evidence'], 'topics':topics,
            'versions':report['versions'] if report is not None else {
                'decision_method':'topic_score', **{key:str(value) for key,value in self.classifier.metadata.items()}}}
        previous = connection.execute('SELECT version,payload FROM routing_routes WHERE id=?', (route['id'],)).fetchone()
        revision = connection.execute("SELECT payload FROM local_policy WHERE name='automatic-revision'").fetchone()
        signed = {'disclosure':disclosure, 'binding':binding,
            'previous_route':dict(previous) if previous else None, 'policy_revision':revision[0] if revision else None}
        receipt = hmac.digest(self._preview_key, encode(signed).encode(), 'sha256').hex()
        return {**disclosure, 'receipt':receipt}, binding

    def preview(self, route, *, connection):
        # LocalFeatures validates current account, sender, topics and route version first.
        return self._preview(route, connection)[0]

    def opt_in(self, route, expected_receipt, *, connection):
        validate_contract('AutoPreviewReceipt', expected_receipt)
        preview, value = self._preview(route, connection)
        if not hmac.compare_digest(expected_receipt, preview['receipt']):
            raise ValueError('Auto preview changed; review and confirm again')
        connection.execute('INSERT INTO local_policy VALUES (?,?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload', ('auto-route:'+route['id'],encode(value)))

    def route_authorized(self, route):
        if self.route_driven:
            return route['enabled'] and route['mode'] == 'auto'
        try:
            with self.db.connection() as con:
                row = con.execute('SELECT payload FROM local_policy WHERE name=?', ('auto-route:'+route['id'],)).fetchone()
                return bool(row and json.loads(row[0]) == self._route_binding(route, con))
        except Exception:
            return False
