"""App-owned immutable-history plans; local approval never supplies native proof.

Only the existing ordered Sender may claim/dispatch jobs. Claims are committed
before initiation and uncertain outcomes have no automatic outgoing transition.
"""
from copy import deepcopy
from datetime import datetime
import hashlib
import hmac
import json
import math
import re
import secrets
from uuid import uuid4

from threadrelay.contracts import validate_contract
from threadrelay.routing.policy import _canonical, _destination
from threadrelay.session import SystemClock
from threadrelay.storage.database import encode
from threadrelay.storage.outbox import Outbox, _current_account
from threadrelay.storage.routing import ConfigStore
from .workflow_schedule import plan_due, spacing, stamp, validate_timing
from .workflow_templates import render_template, validate_template
from .sender import COLLECTION_ONLY_REASONS


_ACTIVE = ('blocked', 'waiting')
_FINAL = ('sent', 'failed', 'unknown', 'cancelled')
_DEFAULT = {'id': 'url', 'version': 1, 'name': 'Post URL', 'body': '{{url}}',
    'readonly': True, 'created_at': '2026-11-01T00:00:00+00:00', 'updated_at': '2026-11-01T00:00:00+00:00'}
_FATAL = frozenset(('destination_required', 'destination_changed', 'destination_not_sendable',
    'source_unavailable', 'source_ineligible', 'template_changed', 'missing_template_values',
    'message_too_long', 'empty_message', 'invalid_message_text', 'preview_too_large',
    'topic_configuration_changed'))


def _digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def _bounded(value):
    if len(json.dumps(value, ensure_ascii=True, separators=(',', ':')).encode()) > 850_000:
        raise ValueError('workflow_projection_too_large')
    return value


class DeliveryWorkflow:
    def __init__(self, db, archive, *, auth, destinations, sender, native_gate=lambda: False,
                 automatic=None, clock=None, randint=None):
        if (archive.db is not db or sender.router.db is not db or sender.auth is not auth
                or sender.destinations is not destinations):
            raise ValueError('Workflow must share the owned delivery lifecycle')
        self.db, self.archive, self.auth = db, archive, auth
        self.destinations, self.sender, self.native_gate = destinations, sender, native_gate
        self.automatic, self.clock = automatic, clock or SystemClock()
        self.randint = randint or secrets.SystemRandom().randint
        self._lock = sender.session.authority_lock
        self._closed = False
        self._uncertain = set()
        self._destination_work = None
        self._scan_after = 0
        self._candidate = self._refresh_candidate = None
        self._last_wall, self._last_mono = self._time(), self.clock.monotonic()
        if getattr(sender,'_workflow',None) is not None and not sender._workflow._closed:
            raise ValueError('Workflow already owned')
        with self.db.transaction() as con:
            # Future schedules retain their dates. Overdue work needs an explicit
            # resume; opening the app can never flush an overdue startup backlog.
            rows = con.execute("SELECT * FROM workflow_jobs WHERE kind='job' AND status IN ('waiting','blocked') AND due_at<=?", (self._last_wall,)).fetchall()
            for row in rows:
                self._change(con, row, status='paused', reasons=['restart_requires_resume'])
            self._sync(con)
        sender.session.add_gate_close_hook(self._session_gate_closed)
        sender._workflow=self

    def _time(self):
        now = self.clock.now()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset().total_seconds() != 0:
            raise ValueError('workflow_clock_unavailable')
        result = now.timestamp()
        if not math.isfinite(result):
            raise ValueError('workflow_clock_unavailable')
        return result

    def _native(self):
        try:
            return self.native_gate() is True
        except Exception:
            return False

    def _automatic_basis(self, con):
        if self.automatic is None:
            return None
        try:
            basis = self.automatic._basis(con)[0]
            revision = con.execute("SELECT payload FROM local_policy WHERE name='automatic-revision'").fetchone()
            return {'basis': basis, 'policy_revision': revision[0] if revision else None}
        except Exception:
            return None

    @staticmethod
    def _draft(row):
        spec = json.loads(row['spec'])
        value = {key: deepcopy(spec[key]) for key in ('revision_ids', 'target', 'template_id', 'template_version', 'timing')}
        value['source_policy'] = spec.get('source_policy','public_only')
        return {'id': row['id'], 'version': row['version'], **value,
            'created_at': stamp(row['created_at']), 'updated_at': stamp(row['updated_at'])}

    @staticmethod
    def _template(row):
        return {'id': row['id'], 'version': row['version'], 'name': row['name'], 'body': row['body'],
            'readonly': False, 'created_at': stamp(row['created_at']), 'updated_at': stamp(row['updated_at'])}

    def _get_template(self, con, identifier):
        if identifier == 'url':
            return deepcopy(_DEFAULT)
        row = con.execute('SELECT * FROM workflow_templates WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise ValueError('template_unavailable')
        return self._template(row)

    def _get_draft(self, con, identifier, version=None):
        row = con.execute('SELECT * FROM workflow_drafts WHERE id=?', (identifier,)).fetchone()
        if row is None or (version is not None and row['version'] != version):
            raise ValueError('draft_changed')
        return row

    def get_draft(self, draft_id):
        with self.db.connection() as con:
            return self._draft(self._get_draft(con, draft_id))

    def workspace(self):
        with self._lock, self.db.connection() as con:
            templates = [deepcopy(_DEFAULT)] + [self._template(row) for row in con.execute('SELECT * FROM workflow_templates ORDER BY name,id LIMIT 20')]
            drafts = []
            targets = []
            for row in con.execute('SELECT * FROM routing_destinations WHERE payload IS NOT NULL ORDER BY account_id,chat_id LIMIT 100'):
                destination = json.loads(row['payload'])
                target = {'account_id': row['account_id'], 'account_epoch': row['account_epoch'],
                    'destination_id': row['chat_id'], 'destination_version': row['version'], 'sender': destination['sender']}
                try:
                    _current_account(con, target)
                    _destination(con, target['account_id'], target['destination_id'], target['account_epoch'])
                except ValueError:
                    continue
                # Offer exact saved snapshots for local preparation even after
                # reconnect/restart invalidates the ephemeral verifier receipt.
                # Preview, approval and dispatch retain all live target checks;
                # reading this workspace never starts verification or a send.
                targets.append({'target': target, 'destination': destination})
            reasons = self._setup_reasons()
            result = {'templates': templates, 'targets': targets, 'drafts': drafts, 'setup_reasons': reasons}
            for row in con.execute('SELECT * FROM workflow_drafts ORDER BY updated_at DESC,id LIMIT 20'):
                item=self._draft(row)
                if len(json.dumps({**result,'drafts':drafts+[item]},ensure_ascii=True).encode())>800_000:
                    break
                drafts.append(item)
            validate_contract('DeliveryWorkspace', result)
            return _bounded(result)

    def _setup_reasons(self):
        reasons = []
        if not self._native():
            reasons.append('native_delivery_unavailable')
        snapshot = self.auth.snapshot()
        if self.auth.status != 'ready' or snapshot.auth_state != 'ready' or snapshot.revocation_pending:
            reasons.append('account_not_ready')
        try:
            online = snapshot.network_state == 'online' and self.sender.client.transport_ready
        except Exception:
            online = False
        if not online:
            reasons.append('transport_offline')
        return reasons

    def save_template(self, request):
        body, name = request['body'], request['name']
        validate_template(body)
        if not isinstance(name, str) or not 1 <= len(name) <= 120:
            raise ValueError('invalid_template_name')
        with self._lock, self.db.transaction() as con:
            identifier, expected = request['id'], request['expected_version']
            row = None if identifier is None else con.execute('SELECT * FROM workflow_templates WHERE id=?', (identifier,)).fetchone()
            if identifier == 'url' or (row is None) != (expected is None) or (row is not None and row['version'] != expected):
                raise ValueError('template_changed')
            if row is None and con.execute('SELECT COUNT(*) FROM workflow_templates').fetchone()[0] >= 20:
                raise ValueError('template_limit')
            identifier = identifier or uuid4().hex
            now = self._time();version = 1 if row is None else row['version'] + 1
            con.execute('INSERT INTO workflow_templates VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version,name=excluded.name,body=excluded.body,updated_at=excluded.updated_at',
                (identifier, version, name, body, now if row is None else row['created_at'], now))
            if row is not None:
                for job in con.execute("SELECT j.* FROM workflow_jobs j JOIN workflow_drafts d ON d.id=j.draft_id WHERE json_extract(d.spec,'$.template_id')=? AND j.status NOT IN ('sending','sent','failed','unknown','cancelled')", (identifier,)).fetchall():
                    self._change(con, job, status='paused', reasons=['template_changed'], approval_hash=None, requires_approval=1)
                con.execute("UPDATE workflow_drafts SET preview=NULL,preview_hash=NULL WHERE json_extract(spec,'$.template_id')=?", (identifier,))
            return self._template(con.execute('SELECT * FROM workflow_templates WHERE id=?', (identifier,)).fetchone())

    def configure(self, request):
        spec = {key: deepcopy(request[key]) for key in ('revision_ids', 'target', 'template_id', 'template_version', 'timing')}
        spec['source_policy'] = request.get('source_policy','public_only')
        if spec['source_policy'] not in ('public_only','manual_audience_reviewed','captured'):
            raise ValueError('invalid_source_policy')
        if (not isinstance(spec['revision_ids'], list) or len(spec['revision_ids']) > 50
                or len(set(spec['revision_ids'])) != len(spec['revision_ids'])):
            raise ValueError('invalid_source_selection')
        validate_timing(spec['timing'])
        if spec['timing']['mode']=='new_matching' and spec['source_policy']=='manual_audience_reviewed':
            raise ValueError('manual_source_policy_not_applicable')
        if spec['timing']['mode'] != 'new_matching' and not spec['revision_ids']:
            raise ValueError('source_required')
        if spec['target'] is not None:
            validate_contract('WorkflowTarget', spec['target'])
        with self._lock, self.db.transaction() as con:
            template = self._get_template(con, spec['template_id'])
            if template['version'] != spec['template_version']:
                raise ValueError('template_changed')
            identifier, expected = request['id'], request['expected_version']
            row = None if identifier is None else con.execute('SELECT * FROM workflow_drafts WHERE id=?', (identifier,)).fetchone()
            if (row is None) != (expected is None) or (row is not None and row['version'] != expected):
                raise ValueError('draft_changed')
            now = self._time()
            spec['due_times'] = plan_due(spec['timing'], len(spec['revision_ids']), now, self.randint)
            spec['render_runs'] = {}
            for revision in spec['revision_ids']:
                if not isinstance(revision, str) or not 1 <= len(revision) <= 160:
                    raise ValueError('invalid_source_selection')
                self._source(con, revision)
                run = con.execute("SELECT id FROM archive_runs WHERE revision_id=? AND kind='collection' AND status='succeeded' ORDER BY seq DESC LIMIT 1", (revision,)).fetchone()
                spec['render_runs'][revision] = run['id'] if run else None
            spec['topics'] = []
            if spec['timing']['mode'] == 'new_matching':
                topics = {topic['id']: topic for topic in ConfigStore.read_all(con, 'routing_topics')}
                for identifier in spec['timing']['topic_ids']:
                    if identifier not in topics or not topics[identifier]['enabled']:
                        raise ValueError('topic_configuration_changed')
                    spec['topics'].append(topics[identifier])
            identifier = request['id'] or uuid4().hex
            version = 1 if row is None else row['version'] + 1
            con.execute('INSERT INTO workflow_drafts VALUES (?,?,?,NULL,NULL,?,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version,spec=excluded.spec,preview=NULL,preview_hash=NULL,updated_at=excluded.updated_at',
                (identifier, version, encode(spec), now if row is None else row['created_at'], now))
            if row is not None:
                for job in con.execute("SELECT * FROM workflow_jobs WHERE draft_id=? AND status NOT IN ('sending','sent','failed','unknown','cancelled')", (identifier,)).fetchall():
                    self._change(con, job, status='cancelled', reasons=['draft_changed'], approval_hash=None, requires_approval=1)
                    if job['kind'] == 'rule':
                        self._revoke_children(con, job['id'], 'rule_changed')
            result = self._draft(self._get_draft(con, identifier))
            validate_contract('DeliveryDraft', result)
            return result

    @staticmethod
    def _source(con, revision, source_policy='public_only'):
        row = con.execute('SELECT * FROM archive_revisions WHERE id=?', (revision,)).fetchone()
        if row is None:
            raise ValueError('source_unavailable')
        post = json.loads(row['payload'])
        validate_contract('ObservedPost', post)
        _canonical(post)
        public = post['visibility']=='public' and not post['quality_flags']
        reviewed = (source_policy=='manual_audience_reviewed' and post['visibility']=='unknown'
            and len(post['quality_flags'])==1 and post['quality_flags'][0]['code']=='visibility_unverified')
        eligible = row['provenance']=='captured' and _digest(post)==row['id'] and (source_policy=='captured' or public or reviewed)
        return post, eligible

    @staticmethod
    def _topic_text(con, run_id):
        if run_id is None:
            return None
        row = con.execute("SELECT topics,classification FROM archive_runs WHERE id=? AND kind='collection' AND status='succeeded'", (run_id,)).fetchone()
        if row is None:
            return None
        classification = json.loads(row['classification'])
        matched = {item['topic_id'] for item in classification['topic_results'] if item['match'] and not item['abstained']}
        names = [topic['description'] for topic in json.loads(row['topics']) if topic['id'] in matched]
        return ', '.join(names) if names else None

    def _target_reasons(self, con, target, *, include_native=True, native=None):
        if target is None:
            return ['destination_required']
        reasons = []
        if include_native and not (self._native() if native is None else native):
            reasons.append('native_delivery_unavailable')
        try:
            row = con.execute('SELECT * FROM routing_destinations WHERE account_id=? AND chat_id=?', (target['account_id'], target['destination_id'])).fetchone()
            destination, version = _destination(con, target['account_id'], target['destination_id'], target['account_epoch'])
            if row is None or version != target['destination_version'] or destination['sender'] != target['sender']:
                raise ValueError('destination_changed')
        except ValueError:
            return reasons + ['destination_changed']
        try:
            _current_account(con, target)
        except ValueError:
            reasons.append('account_not_ready')
        snapshot = self.auth.snapshot()
        if (snapshot.account_id != target['account_id'] or snapshot.epoch != target['account_epoch']):
            reasons.append('account_changed')
        if self.auth.status != 'ready' or snapshot.auth_state != 'ready' or snapshot.revocation_pending:
            reasons.append('account_not_ready')
        try:
            transport = snapshot.network_state == 'online' and self.sender.client.transport_ready
        except Exception:
            transport = False
        if not transport:
            reasons.append('transport_offline')
        delivery = {**target}
        proposal = {'destination': destination, 'destination_version': version}
        if not self.destinations.is_current(delivery, proposal):
            reasons.append('destination_unverified')
        return sorted(set(reasons))

    def _preview(self, con, row, native, basis=None):
        spec = json.loads(row['spec'])
        template = self._get_template(con, spec['template_id'])
        reasons = self._target_reasons(con, spec['target'], native=native)
        if template['version'] != spec['template_version']:
            reasons.append('template_changed')
        items = []
        mode = spec['timing']['mode']
        for index, revision in enumerate(spec['revision_ids']):
            post, eligible = self._source(con, revision, spec.get('source_policy','public_only'))
            rendered = render_template(template['body'], post, topic=self._topic_text(con, spec['render_runs'].get(revision)))
            item_reasons = [] if eligible else ['source_ineligible']
            if rendered['missing_fields']:
                item_reasons.append('missing_template_values')
            if rendered['error']:
                item_reasons.append(rendered['error'])
            due = spec['due_times'][index] if mode != 'new_matching' else row['created_at']
            items.append({'revision_id': revision, 'post_id': post['post_id'], 'url': post['permalink'],
                'text': '' if rendered['error'] else rendered['text'], 'due_at': stamp(due),
                'missing_fields': rendered['missing_fields'], 'source_eligible': eligible, 'blocked_reasons': item_reasons})
            reasons.extend(item_reasons)
        if mode == 'new_matching':
            if not self._topics_current(con, spec['topics']):
                reasons.append('topic_configuration_changed')
            if spec['timing']['delivery_mode'] == 'automatic' and basis is None:
                reasons.append('automatic_evidence_unavailable')
        destination = None
        if spec['target'] is not None:
            target = spec['target']
            saved = con.execute('SELECT payload FROM routing_destinations WHERE account_id=? AND chat_id=?',
                (target['account_id'], target['destination_id'])).fetchone()
            if saved is not None and saved['payload'] is not None:
                destination = json.loads(saved['payload'])
        value = {'draft_id': row['id'], 'version': row['version'], 'items': items, 'target': spec['target'],
            'timing': spec['timing'], 'blocked_reasons': sorted(set(reasons)),
            'source_policy': spec.get('source_policy','public_only'),
            'destination': destination, 'topic_bindings': [{key: topic[key] for key in ('id','version','description')} for topic in spec['topics']],
            'approvable': not bool(_FATAL.intersection(reasons))}
        if len(json.dumps(value, ensure_ascii=True).encode()) > 800_000:
            value.update(items=[], topic_bindings=[], destination=None, approvable=False, blocked_reasons=['preview_too_large'])
        value['preview_hash'] = _digest({'preview': value, 'spec': spec, 'automatic_basis': basis})
        validate_contract('DeliveryPreview', value)
        return value

    def preview(self, draft_id, version):
        native = self._native()
        with self._lock, self.db.transaction() as con:
            row = self._get_draft(con, draft_id, version)
            spec = json.loads(row['spec'])
            basis = self._automatic_basis(con) if spec['timing']['mode'] == 'new_matching' and spec['timing']['delivery_mode'] == 'automatic' else None
            value = self._preview(con, row, native, basis)
            con.execute('UPDATE workflow_drafts SET preview=?,preview_hash=? WHERE id=?', (encode(value), value['preview_hash'], draft_id))
            return value

    def approve(self, draft_id, version, preview_hash):
        native = self._native()
        with self._lock, self.db.transaction() as con:
            row = self._get_draft(con, draft_id, version)
            spec = json.loads(row['spec'])
            basis = self._automatic_basis(con) if spec['timing']['mode'] == 'new_matching' and spec['timing']['delivery_mode'] == 'automatic' else None
            preview = self._preview(con, row, native, basis)
            if (row['preview_hash'] is None or not isinstance(preview_hash, str)
                    or not hmac.compare_digest(preview_hash, row['preview_hash'])
                    or not hmac.compare_digest(preview_hash, preview['preview_hash']) or not preview['approvable']):
                raise ValueError('preview_changed_or_unavailable')
            previous = con.execute('SELECT * FROM workflow_jobs WHERE draft_id=? AND draft_version=? AND approval_hash IS NOT NULL ORDER BY seq', (draft_id, version)).fetchall()
            if previous and spec['timing']['mode'] == 'new_matching' and json.loads(previous[0]['context']).get('automatic_basis') != basis:
                for old in previous:
                    self._change(con, old, status='cancelled', reasons=['rule_changed'], approval_hash=None)
                    self._revoke_children(con, old['id'], 'rule_changed')
                previous = []
            if previous:
                return {'items': [self._job(con, item) for item in previous], 'next_cursor': None}
            spec = json.loads(row['spec']);created = []
            if spec['timing']['mode'] == 'new_matching':
                context = {'spec': spec, 'cutoff': con.execute('SELECT COALESCE(MAX(seq),0) FROM archive_revisions').fetchone()[0],
                    'automatic_basis': basis, 'last_due': None}
                status = 'blocked' if preview['blocked_reasons'] else 'waiting'
                created.append(self._insert_job(con, row, kind='rule', status=status, reasons=preview['blocked_reasons'], approval_hash=preview_hash, context=context))
            else:
                for item in preview['items']:
                    placeholder = con.execute("SELECT * FROM workflow_jobs WHERE draft_id=? AND draft_version=? AND kind='job' AND requires_approval=1 AND status IN ('draft','paused') ORDER BY seq LIMIT 1", (draft_id,version)).fetchone()
                    existing = self._duplicate(con, item['post_id'], spec['target'], None if placeholder is None else placeholder['id'])
                    reasons = list(item['blocked_reasons']) + self._target_reasons(con, spec['target'], native=native)
                    if existing:
                        reasons.append(existing)
                    context = {'spec': spec, 'run_id': spec['render_runs'].get(item['revision_id']), 'automatic_basis': None}
                    if placeholder is not None:
                        parent = con.execute('SELECT * FROM workflow_jobs WHERE id=?',(placeholder['rule_id'],)).fetchone()
                        if parent is None or parent['status'] not in _ACTIVE or parent['approval_hash'] is None:
                            raise ValueError('rule_changed')
                        created.append(self._change(con,placeholder,status='blocked' if reasons else 'waiting',reasons=reasons,
                            approval_hash=preview_hash,requires_approval=0,text=item['text'],context=encode(context)))
                    else:
                        created.append(self._insert_job(con, row, revision_id=item['revision_id'], post_id=item['post_id'], text=item['text'],
                            due_at=datetime.fromisoformat(item['due_at']).timestamp(), status='blocked' if reasons else 'waiting', reasons=reasons,
                            approval_hash=preview_hash, context=context))
            return {'items': [self._job(con, item) for item in created], 'next_cursor': None}

    def _insert_job(self, con, draft, *, kind='job', revision_id=None, post_id=None, text=None, due_at=None,
                    status='draft', reasons=(), approval_hash=None, requires_approval=False, rule_id=None, context=None):
        identifier = uuid4().hex;now = self._time()
        con.execute('INSERT INTO workflow_jobs(id,version,draft_id,draft_version,kind,revision_id,post_id,text,due_at,status,reasons,approval_hash,requires_approval,rule_id,context,created_at,updated_at) VALUES (?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (identifier, draft['id'], draft['version'], kind, revision_id, post_id, text, due_at, status,
                encode(sorted(set(reasons))), approval_hash, int(requires_approval), rule_id, encode(context or {}), now, now))
        return con.execute('SELECT * FROM workflow_jobs WHERE id=?', (identifier,)).fetchone()

    @staticmethod
    def _duplicate(con, post_id, target, own_job=None):
        if target is None:
            return None
        old = con.execute("SELECT payload FROM archive_deliveries WHERE json_extract(payload,'$.account_id')=? AND json_extract(payload,'$.post_id')=? AND json_extract(payload,'$.destination_id')=?", (target['account_id'], post_id, target['destination_id'])).fetchall()
        for row in old:
            value = json.loads(row['payload'])
            if own_job is not None and value.get('workflow_job_id') == own_job:
                continue
            return 'already_sent' if value['state'] == 'sent' else 'unknown_outcome' if value['state'] in ('unknown', 'sending', 'retryable') else 'already_scheduled'
        for row in con.execute("SELECT id,context,reasons FROM workflow_jobs WHERE post_id=? AND status NOT IN ('draft','cancelled')", (post_id,)):
            if set(json.loads(row['reasons'])) & {'already_scheduled','already_sent','unknown_outcome'}:
                continue  # A shadow duplicate must not revoke the original job.
            other=json.loads(row['context'])['spec']['target']
            if row['id'] != own_job and other is not None and all(other[key]==target[key] for key in ('account_id','destination_id')):
                return 'already_scheduled'
        return None

    def _job(self, con, row):
        context = json.loads(row['context']);spec = context['spec']
        value = {'id': row['id'], 'version': row['version'], 'kind': row['kind'], 'draft_id': row['draft_id'],
            'draft_version': row['draft_version'], 'revision_id': row['revision_id'], 'post_id': row['post_id'], 'text': row['text'],
            'target': spec['target'], 'due_at': None if row['due_at'] is None else stamp(row['due_at']), 'timing': spec['timing'],
            'source_policy': spec.get('source_policy','public_only'),
            'status': row['status'], 'blocked_reasons': json.loads(row['reasons']), 'requires_approval': bool(row['requires_approval']),
            'rule_id': row['rule_id'], 'delivery_id': row['delivery_id'], 'attempt_id': row['attempt_id'],
            'created_at': stamp(row['created_at']), 'updated_at': stamp(row['updated_at'])}
        validate_contract('DeliveryJob', value)
        return value

    def query_jobs(self, cursor=None, limit=30, status=None):
        if type(limit) is not int or not 1 <= limit <= 50 or (cursor is not None and (not isinstance(cursor, str) or re.fullmatch(r'[1-9][0-9]{0,18}', cursor) is None)):
            raise ValueError('invalid_job_page')
        if status is not None and status not in ('draft', 'blocked', 'waiting', 'paused', 'sending', *_FINAL):
            raise ValueError('invalid_job_status')
        with self.db.connection() as con:
            rows = con.execute('SELECT * FROM workflow_jobs WHERE seq<? AND (? IS NULL OR status=?) ORDER BY seq DESC LIMIT ?',
                (int(cursor) if cursor else 9223372036854775807, status, status, limit+1)).fetchall()
            items=[];size=0
            for row in rows[:limit]:
                item=self._job(con, row);count=len(json.dumps(item, ensure_ascii=True).encode())
                if items and size+count>800_000:
                    break
                items.append(item);size+=count
            return {'items': items, 'next_cursor': str(rows[len(items)-1]['seq']) if len(rows)>len(items) else None}

    def _change(self, con, row, **changes):
        if 'reasons' in changes:
            changes['reasons'] = encode(sorted(set(changes['reasons'])))
        if all(row[key] == value for key,value in changes.items()):
            return row
        fields = ','.join(key+'=?' for key in changes)
        con.execute('UPDATE workflow_jobs SET '+fields+',version=version+1,updated_at=? WHERE id=? AND version=?',
            (*changes.values(), self._time(), row['id'], row['version']))
        return con.execute('SELECT * FROM workflow_jobs WHERE id=?', (row['id'],)).fetchone()

    def _action(self, job_id, version, action):
        with self._lock, self.db.transaction() as con:
            row = con.execute('SELECT * FROM workflow_jobs WHERE id=?', (job_id,)).fetchone()
            if row is None or row['version'] != version or row['status'] in ('sending', *_FINAL):
                raise ValueError('job_changed_or_terminal')
            if action == 'resume':
                if row['status'] not in ('paused','blocked') or row['requires_approval'] or row['approval_hash'] is None:
                    raise ValueError('approval_required')
                context = json.loads(row['context']);timing=context['spec']['timing'];due=row['due_at']
                context.pop('destination_refresh_failed',None)
                if row['kind']=='rule':
                    context['cutoff']=con.execute('SELECT COALESCE(MAX(seq),0) FROM archive_revisions').fetchone()[0]
                    context['last_due']=None
                if due is not None and due <= self._time():
                    due=self._time()+(spacing(timing,self.randint) if timing['mode'] in ('fixed','random') else 0)
                changed=self._change(con,row,status='waiting',reasons=[],due_at=due,context=encode(context))
            else:
                changed=self._change(con,row,status='paused' if action=='pause' else 'cancelled',reasons=['paused' if action=='pause' else 'cancelled'])
            if row['kind']=='rule' and action in ('pause','cancel'):
                self._revoke_children(con,row['id'],'paused' if action=='pause' else 'cancelled')
            return self._job(con,changed)

    def pause(self, job_id, version):return self._action(job_id,version,'pause')
    def resume(self, job_id, version):return self._action(job_id,version,'resume')
    def cancel(self, job_id, version):return self._action(job_id,version,'cancel')

    def _revoke_children(self, con, rule_id, reason):
        for child in con.execute("SELECT * FROM workflow_jobs WHERE rule_id=? AND status NOT IN ('sending','sent','failed','unknown','cancelled')", (rule_id,)).fetchall():
            self._change(con,child,status='paused',reasons=[reason],approval_hash=None,requires_approval=1)

    @staticmethod
    def _topics_current(con, selected):
        topics={topic['id']:topic for topic in ConfigStore.read_all(con,'routing_topics')}
        return all(topics.get(topic['id'])==topic and topic['enabled'] for topic in selected)

    def _sync(self, con):
        for row in con.execute("SELECT * FROM workflow_jobs WHERE status='sending'").fetchall():
            record=con.execute('SELECT payload FROM archive_deliveries WHERE id=?',(row['delivery_id'],)).fetchone()
            if record is None:
                self._change(con,row,status='unknown',reasons=['unknown_outcome'])
                continue
            state=json.loads(record['payload'])['state']
            if state in _FINAL or state=='retryable':
                final='unknown' if state=='retryable' else state
                self._change(con,row,status=final,reasons=['unknown_outcome'] if final=='unknown' else [])

    def _clock_check(self):
        wall,mono=self._time(),self.clock.monotonic()
        invalid=(not math.isfinite(mono) or mono<self._last_mono or wall<self._last_wall
            or abs((wall-self._last_wall)-(mono-self._last_mono))>5)
        self._last_wall,self._last_mono=wall,mono
        if invalid:
            self._cancel_destination_refresh()
            with self.db.transaction() as con:
                for row in con.execute("SELECT * FROM workflow_jobs WHERE status IN ('blocked','waiting')").fetchall():
                    self._change(con,row,status='paused',reasons=['clock_changed'])
            self.sender.session.require_attention('workflow_clock_changed')
        return not invalid

    def tick(self):
        with self._lock:
            if self._closed:
                return
            if not self._clock_check():
                return
            with self.db.transaction() as con:
                self._sync(con)
            self.sender.start_workflow(self)

    def close(self):
        with self._lock:
            self._closed=True
            self._cancel_destination_refresh()

    def _session_gate_closed(self):
        # Global Stop is a durable queue stop. Collection-only pause/hide does
        # not grant or restore any queue/network authority.
        snapshot=self.sender.session.snapshot
        if snapshot['attention_reason'] in COLLECTION_ONLY_REASONS or snapshot['state'] not in ('pausing','stopping','needs_attention','failed'):
            return
        self._cancel_destination_refresh()
        with self.db.transaction() as con:
            for row in con.execute("SELECT * FROM workflow_jobs WHERE status IN ('blocked','waiting')").fetchall():
                self._change(con,row,status='paused',reasons=['global_stop' if snapshot['state']=='stopping' else 'global_pause'])

    # The Sender adapters below are internal trusted ownership boundaries.
    def preflight_due(self):
        self._candidate=self._refresh_candidate=None
        with self.db.transaction() as con:
            rows=self._due_rows(con,after=self._scan_after)
            if not rows and self._scan_after:
                rows=self._due_rows(con,after=0)
            if not rows:
                return None
            native=self._native()
            for row in rows:
                # Advance only past inspected records: returning one eligible
                # job must not skip the rest of this bounded batch.
                self._scan_after=row['seq']
                reasons=self._job_reasons(con,row,native)
                if reasons:
                    self._change(con,row,status='blocked',reasons=reasons)
                    if set(reasons)=={'destination_unverified'} and self._refresh_candidate is None:
                        self._refresh_candidate=row['id']
                else:
                    self._candidate=row['id']
                    return True
        return False

    def _due_rows(self, con, *, after=0, identifier=None):
        clause=' AND id=?' if identifier is not None else ' AND seq>?'
        owner=getattr(self.sender.client,'_owner_history',None)
        active=getattr(owner,'active_job_id',None)
        parameters=[self._time(),identifier if identifier is not None else after]
        if active is not None:
            # The explicit actor owns this exact job's transient verification.
            # Other jobs and every direct claim/mutation recheck remain active.
            clause+=' AND id<>?';parameters.append(active)
        return con.execute("SELECT * FROM workflow_jobs WHERE kind='job' AND status IN ('blocked','waiting') AND requires_approval=0 AND approval_hash IS NOT NULL AND due_at<=?"+clause+' ORDER BY seq LIMIT 16',
            parameters).fetchall()

    def _job_reasons(self, con, row, native, *, check_rate=True):
        context=json.loads(row['context']);spec=context['spec'];reasons=self._target_reasons(con,spec['target'],native=native)
        if context.get('destination_refresh_failed'):
            reasons.append('destination_verification_unavailable')
        if row['id'] in self._uncertain:
            reasons.append('unknown_outcome')
        draft=self._get_draft(con,row['draft_id'])
        if draft['version']!=row['draft_version'] or json.loads(draft['spec'])!=spec:
            reasons.append('draft_changed')
        template=self._get_template(con,spec['template_id'])
        if template['version']!=spec['template_version']:
            reasons.append('template_changed')
        post,eligible=self._source(con,row['revision_id'],spec.get('source_policy','public_only'))
        rendered=render_template(template['body'],post,topic=self._topic_text(con,context.get('run_id')))
        if not eligible:
            reasons.append('source_ineligible')
        if rendered['error'] or rendered['missing_fields'] or rendered['text']!=row['text']:
            reasons.append('draft_changed')
        duplicate=self._duplicate(con,row['post_id'],spec['target'],row['id'])
        if duplicate:
            reasons.append(duplicate)
        if row['rule_id'] is not None:
            rule=con.execute('SELECT * FROM workflow_jobs WHERE id=?',(row['rule_id'],)).fetchone()
            if rule is None or rule['status'] not in _ACTIVE or rule['approval_hash'] is None:
                reasons.append('rule_changed')
            else:
                rule_context=json.loads(rule['context']);rule_spec=rule_context['spec']
                if not self._topics_current(con,rule_spec['topics']):
                    reasons.append('topic_configuration_changed')
                if rule_spec['timing']['delivery_mode']=='automatic':
                    current=self._automatic_basis(con)
                    if context.get('automatic_basis') is None or current is None:
                        reasons.append('automatic_evidence_unavailable')
                    elif current!=context['automatic_basis']:
                        reasons.append('automatic_binding_changed')
                    run=con.execute('SELECT * FROM archive_runs WHERE id=?',(context.get('run_id'),)).fetchone()
                    if (run is None or run['status']!='succeeded' or run['kind']!='collection' or run['revision_id']!=row['revision_id']
                            or json.loads(run['input_snapshot'])!={'post':post}
                            or json.loads(run['binding'])!=self.sender.router.classifier.metadata
                            or not self._run_matches(rule_spec,json.loads(run['topics']),json.loads(run['classification']),automatic=True)):
                        reasons.append('automatic_binding_changed')
        if check_rate:
            try:
                self.sender.router._rate_check({'account_id':spec['target']['account_id'],'destination_id':spec['target']['destination_id']})
            except ValueError:
                reasons.append('destination_rate_limit')
        return sorted(set(reasons))

    def transport_claim_current(self, con, delivery):
        if self._closed or not self._native():
            return False
        row=con.execute('SELECT j.*,c.initiated_attempt_id FROM workflow_jobs j JOIN workflow_delivery_context c ON c.job_id=j.id WHERE c.delivery_id=?', (delivery['id'],)).fetchone()
        if row is None:
            return False
        target=json.loads(row['context'])['spec']['target']
        post,_=self._source(con,row['revision_id'])
        attempt=con.execute("SELECT 1 FROM telegram_attempts WHERE delivery_id=? AND attempt_id=? AND request_extra=? AND state='sending'",
            (delivery['id'],delivery['attempt_id'],delivery['request_extra'])).fetchone()
        return bool(row and row['kind']=='job' and row['status']=='sending' and row['approval_hash'] is not None
            and not row['requires_approval'] and delivery.get('workflow_job_id')==row['id']
            and row['delivery_id']==delivery['id'] and row['attempt_id']==delivery['attempt_id']
            and row['initiated_attempt_id']==delivery['attempt_id']
            and attempt is not None and delivery['post_id']==post['post_id'] and delivery['content_hash']==post['content_hash']
            and all(delivery[key]==target[key] for key in ('account_id','account_epoch','destination_id','sender'))
            and not self._job_reasons(con,row,True,check_rate=False))

    @staticmethod
    def claim_destination_version(con, delivery):
        row=con.execute('SELECT context FROM workflow_jobs WHERE id=?',(delivery['workflow_job_id'],)).fetchone()
        return json.loads(row['context'])['spec']['target']['destination_version'] if row else None

    def _cancel_destination_refresh(self):
        work,self._destination_work=self._destination_work,None
        if work is not None and self.destinations.pending is work[1]:
            self.destinations.invalidate()

    def _destination_refresh_failed(self, job_id):
        self._cancel_destination_refresh()
        with self.db.transaction() as con:
            row=con.execute('SELECT * FROM workflow_jobs WHERE id=?',(job_id,)).fetchone()
            if row is not None and row['status'] in _ACTIVE:
                context=json.loads(row['context']);context['destination_refresh_failed']=True
                self._change(con,row,status='blocked',reasons=['destination_verification_unavailable'],context=encode(context))

    def refresh_destination(self):
        """Only the ordered Sender calls this for an explicitly approved due job.

        Refresh one exact saved target; a failed lookup waits for an explicit
        Resume. This never starts a client, enumerates chats or enables network.
        """
        if self._closed:
            return
        if self._destination_work is not None:
            identifier,pending,deadline=self._destination_work
            if self.destinations.pending is not pending or self.clock.monotonic()>=deadline:
                self._destination_refresh_failed(identifier);return
            if pending['stage']=='complete':
                try:
                    self.destinations.finish()
                    self._destination_work=None
                except Exception:
                    self._destination_refresh_failed(identifier)
            return
        if self.destinations.pending is not None:
            return  # A deliberate UI verification owns its own result.
        if self._refresh_candidate is None:
            return
        native=self._native()
        with self.db.transaction() as con:
            selected=None
            for row in self._due_rows(con,identifier=self._refresh_candidate):
                if set(self._job_reasons(con,row,native))=={'destination_unverified'}:
                    selected=row;break
            if selected is None:
                return
            target=json.loads(selected['context'])['spec']['target']
            saved=con.execute('SELECT * FROM routing_destinations WHERE account_id=? AND chat_id=?',(target['account_id'],target['destination_id'])).fetchone()
            identifier=selected['id']
        try:
            if int(target['destination_id'])>0:
                self.destinations.begin_private(target['destination_id'])
            else:
                self.destinations.begin_group(target['destination_id'],sender=target['sender'],sender_selected=bool(saved['sender_selected']))
            if self.destinations.pending is None:
                raise ValueError('destination_verification_unavailable')
            self._destination_work=(identifier,self.destinations.pending,self.clock.monotonic()+30)
        except Exception:
            self._destination_refresh_failed(identifier)

    def claim_due(self):
        native=self._native()
        identifier,self._candidate=self._candidate,None
        with self.db.transaction() as con:
            for row in self._due_rows(con,identifier=identifier):
                reasons=self._job_reasons(con,row,native)
                if reasons:
                    self._change(con,row,status='blocked',reasons=reasons);continue
                claim=self._claim_job(con,row)
                if claim is not None:return claim
        return None

    def _claim_job(self, con, row, *, manual=False):
        spec=json.loads(row['context'])['spec'];target=spec['target'];post,_=self._source(con,row['revision_id'])
        con.execute('INSERT OR IGNORE INTO posts VALUES (?,?,?,?)',(post['post_id'],post['content_hash'],self._time(),self._time()))
        old=con.execute('SELECT * FROM deliveries WHERE account_id=? AND post_id=? AND destination_id=?',
            (target['account_id'],post['post_id'],target['destination_id'])).fetchone()
        identifier=uuid4().hex if old is None else old['id']
        if old is not None:
            context=con.execute('SELECT * FROM workflow_delivery_context WHERE delivery_id=?',(identifier,)).fetchone()
            if (context is None or context['job_id']!=row['id'] or context['initiated_attempt_id'] is not None
                    or old['state']!='cancelled' or json.loads(old['payload'])['attempt_id'] is not None):
                self._change(con,row,status='blocked',reasons=['already_scheduled']);return None
        delivery={'id':identifier,'workflow_job_id':row['id'],'post_id':post['post_id'],'content_hash':post['content_hash'],
            'destination_id':target['destination_id'],'account_id':target['account_id'],'account_epoch':target['account_epoch'],
            'sender':target['sender'],'route_refs':[],'state':'sending','attempt_id':uuid4().hex,'request_extra':uuid4().hex,
            'message_id':None,'temporary_message_id':None,'final_message_id':None}
        if manual:delivery['request_extra']='owner-history:'+delivery['attempt_id']
        validate_contract('Delivery',delivery);now=self._time()
        if old is None:
            con.execute("INSERT INTO deliveries VALUES (?,?,?,?,?,?,?,'sending','workflow',?,?)",(identifier,target['account_id'],target['account_epoch'],post['post_id'],target['destination_id'],post['content_hash'],encode(delivery),now,now))
            con.execute('INSERT INTO workflow_delivery_context VALUES (?,?,NULL)',(identifier,row['id']))
        else:
            con.execute("UPDATE deliveries SET payload=?,state='sending',updated_at=? WHERE id=?",(encode(delivery),now,identifier))
        con.execute("INSERT INTO telegram_attempts(delivery_id,attempt_id,request_extra,state) VALUES (?,?,?,'sending')",(identifier,delivery['attempt_id'],delivery['request_extra']))
        changed=self._change(con,row,status='sending',reasons=[],delivery_id=identifier,attempt_id=delivery['attempt_id'])
        return {'job_id':row['id'],'job_version':changed['version'],'delivery':delivery}

    def _manual_history_plan(self, con, job_id, version, *, allow_unverified):
        """Exact local intent; never production/background native authority."""
        row=con.execute('SELECT * FROM workflow_jobs WHERE id=?',(job_id,)).fetchone()
        if (self._closed or row is None or row['version']!=version or row['kind']!='job'
                or row['rule_id'] is not None or row['status'] not in ('blocked','waiting')
                or row['requires_approval'] or row['approval_hash'] is None or row['due_at'] is None
                or row['due_at']>self._time()):
            raise ValueError('manual_history_unavailable')
        spec=json.loads(row['context'])['spec'];target=spec['target']
        if spec['timing']['mode']!='now' or target is None or target['sender']!={'kind':'user','id':target['account_id']}:
            raise ValueError('manual_history_unavailable')
        destination,_=_destination(con,target['account_id'],target['destination_id'],target['account_epoch'])
        saved=con.execute('SELECT payload FROM local_policy WHERE name=?',
            (f"saved-telegram-destination:{target['account_id']}:{target['destination_id']}",)).fetchone()
        selected=json.loads(saved['payload']) if saved else None
        if (selected is None or selected.get('kind')!='group' or selected.get('account_epoch')!=target['account_epoch']
                or any(selected.get(key)!=destination[key] for key in ('account_id','chat_id','title','sender'))):
            raise ValueError('manual_history_unavailable')
        reasons=set(self._job_reasons(con,row,True))
        if reasons-({'destination_unverified'} if allow_unverified else set()):
            raise ValueError('manual_history_unavailable')
        if con.execute("SELECT 1 FROM deliveries WHERE state IN ('pending','sending','retryable','unknown') LIMIT 1").fetchone():
            raise ValueError('manual_history_unavailable')
        return {'job_id':row['id'],'job_version':row['version'],'revision_id':row['revision_id'],
            'text':row['text'],'destination':destination,'target':target,
            'approval_hash':row['approval_hash'],'draft_id':row['draft_id'],'draft_version':row['draft_version'],'spec':spec}

    def manual_history_plan(self, job_id, version):
        with self._lock,self.db.connection() as con:
            return self._manual_history_plan(con,job_id,version,allow_unverified=True)

    def manual_history_profile_ready(self, profile_id):
        with self.db.connection() as con:
            return con.execute('SELECT 1 FROM local_policy WHERE name IN (?,?)',
                ('manual-history-incomplete:'+profile_id,'automatic-incomplete:'+profile_id)).fetchone() is None

    def claim_manual_history(self, job_id, version, expected_plan, profile_id):
        with self._lock,self.db.transaction() as con:
            plan=self._manual_history_plan(con,job_id,version,allow_unverified=False)
            if plan!=expected_plan:raise ValueError('manual_history_changed')
            row=con.execute('SELECT * FROM workflow_jobs WHERE id=?',(job_id,)).fetchone()
            claim=self._claim_job(con,row,manual=True)
            if claim is None:raise ValueError('manual_history_unavailable')
            con.execute('UPDATE workflow_delivery_context SET initiated_attempt_id=? WHERE delivery_id=?',
                (claim['delivery']['attempt_id'],claim['delivery']['id']))
            claim['profile_id']=profile_id
            con.execute('INSERT INTO local_policy(name,payload) VALUES (?,?)',('manual-history-incomplete:'+profile_id,
                encode({'job_id':job_id,'attempt_id':claim['delivery']['attempt_id']})))
            return claim

    def initiate_manual_history(self, claim, initiate):
        # Claim is committed before profile fsync/native entry. Keep the final
        # authority recheck serialized with account revocation and other writers.
        with self._lock,self.db.transaction() as con:
            row=con.execute('SELECT * FROM workflow_jobs WHERE id=?',(claim['job_id'],)).fetchone()
            context=con.execute('SELECT * FROM workflow_delivery_context WHERE delivery_id=?',(claim['delivery']['id'],)).fetchone()
            saved=con.execute('SELECT payload FROM deliveries WHERE id=?',(claim['delivery']['id'],)).fetchone()
            if (self._closed or row is None or row['status']!='sending' or row['version']!=claim['job_version']
                    or context is None or context['job_id']!=row['id'] or context['initiated_attempt_id']!=claim['delivery']['attempt_id']
                    or saved is None or json.loads(saved['payload'])!=claim['delivery'] or self._job_reasons(con,row,True)):
                raise ValueError('manual_history_changed')
            key,now=self.sender.router._rate_check(claim['delivery'])
            self.sender.router._started[key].append(now)
            return initiate()

    def complete_manual_history(self, claim, temporary, final):
        delivery=claim['delivery']
        with self.db.transaction() as con:
            row=con.execute('SELECT * FROM workflow_jobs WHERE id=?',(claim['job_id'],)).fetchone()
            saved=con.execute('SELECT payload FROM deliveries WHERE id=?',(delivery['id'],)).fetchone()
            current=json.loads(saved['payload']) if saved else None
            if (row is None or row['status']!='sending' or row['attempt_id']!=delivery['attempt_id']
                    or current is None or current['state']!='sending'
                    or any(current[key]!=delivery[key] for key in ('attempt_id','request_extra','workflow_job_id'))
                    or current['temporary_message_id'] not in (None,temporary)):
                raise ValueError('manual_history_completion_unavailable')
            current.update(state='sent',temporary_message_id=temporary,final_message_id=final,message_id=final)
            validate_contract('Delivery',current)
            con.execute("UPDATE deliveries SET state='sent',payload=?,updated_at=? WHERE id=?",(encode(current),self._time(),delivery['id']))
            con.execute("UPDATE telegram_attempts SET state='sent',temporary_message_id=?,final_message_id=? WHERE attempt_id=? AND request_extra=?",
                (temporary,final,delivery['attempt_id'],delivery['request_extra']))
            self._change(con,row,status='sent',reasons=[])

    def finish_manual_history(self, claim):
        # Clear the durable deny latch only after DB completion AND authenticated
        # native-success profile publication. Any uncertain finish stays denied.
        with self.db.transaction() as con:
            row=con.execute('SELECT status,attempt_id FROM workflow_jobs WHERE id=?',(claim['job_id'],)).fetchone()
            if row is None or row['status']!='sent' or row['attempt_id']!=claim['delivery']['attempt_id']:
                raise ValueError('manual_history_completion_unavailable')
            con.execute('DELETE FROM local_policy WHERE name=?',('manual-history-incomplete:'+claim['profile_id'],))

    def unknown_manual_history(self, job_id):
        # A commit may succeed before its caller sees an exception. Recover
        # only this job's existing manual claim to deny it, never to dispatch.
        self._uncertain.add(job_id)
        with self.db.connection() as con:
            row=con.execute('SELECT * FROM workflow_jobs WHERE id=?',(job_id,)).fetchone()
            saved=con.execute('SELECT payload FROM deliveries WHERE id=?',(row['delivery_id'],)).fetchone() if row is not None else None
            if row is None or row['status']!='sending' or saved is None:return
            delivery=json.loads(saved['payload'])
            if (delivery.get('workflow_job_id')!=job_id or delivery.get('attempt_id')!=row['attempt_id']
                    or delivery.get('request_extra')!='owner-history:'+str(row['attempt_id'])):return
            claim={'job_id':job_id,'job_version':row['version'],'delivery':delivery}
        self.unknown_claim(claim)

    def start_claim(self, claim, initiate):
        native=self._native()
        with self.db.transaction() as con:
            row=con.execute('SELECT * FROM workflow_jobs WHERE id=?',(claim['job_id'],)).fetchone()
            context=con.execute('SELECT * FROM workflow_delivery_context WHERE delivery_id=?',(claim['delivery']['id'],)).fetchone()
            saved=con.execute('SELECT payload FROM deliveries WHERE id=?',(claim['delivery']['id'],)).fetchone()
            if (self._closed or row is None or row['status']!='sending' or row['version']!=claim['job_version']
                    or context is None or context['initiated_attempt_id'] is not None or saved is None
                    or json.loads(saved['payload'])!=claim['delivery'] or self._job_reasons(con,row,native)):
                raise ValueError('workflow_authority_changed')
            con.execute('UPDATE workflow_delivery_context SET initiated_attempt_id=? WHERE delivery_id=?',
                (claim['delivery']['attempt_id'],claim['delivery']['id']))
            return initiate(con,row,json.loads(row['context'])['spec']['target'])

    def release_unstarted(self, claim):
        with self.db.transaction() as con:
            row=con.execute('SELECT * FROM workflow_jobs WHERE id=?',(claim['job_id'],)).fetchone()
            saved=con.execute('SELECT * FROM deliveries WHERE id=?',(claim['delivery']['id'],)).fetchone()
            context=con.execute('SELECT * FROM workflow_delivery_context WHERE delivery_id=?',(claim['delivery']['id'],)).fetchone()
            if (saved is None or saved['state']!='sending' or json.loads(saved['payload'])!=claim['delivery']
                    or context is None or context['job_id']!=claim['job_id']
                    or context['initiated_attempt_id'] not in (None,claim['delivery']['attempt_id'])):
                self._uncertain.add(claim['job_id'])
                self._sync(con)
                latest=con.execute('SELECT * FROM workflow_jobs WHERE id=?',(claim['job_id'],)).fetchone()
                if latest['status']=='sending':
                    self._change(con,latest,status='unknown',reasons=['unknown_outcome'])
                return
            delivery=deepcopy(claim['delivery']);delivery.update(state='cancelled',attempt_id=None,request_extra=None)
            con.execute("UPDATE deliveries SET state='cancelled',payload=?,updated_at=? WHERE id=? AND state='sending'",(encode(delivery),self._time(),delivery['id']))
            con.execute("UPDATE telegram_attempts SET state='cancelled' WHERE attempt_id=?",(claim['delivery']['attempt_id'],))
            con.execute('UPDATE workflow_delivery_context SET initiated_attempt_id=NULL WHERE delivery_id=?',(delivery['id'],))
            self._change(con,row,status='blocked',reasons=['workflow_authority_changed'],attempt_id=None)

    def unknown_claim(self, claim):
        self._uncertain.add(claim['job_id'])
        delivery=claim['delivery']
        if claim.get('profile_id') is not None:
            # Restore the durable deny latch even if terminal commit succeeded
            # before its return was lost and the profile signer became unusable.
            with self.db.transaction() as con:
                con.execute('INSERT OR IGNORE INTO local_policy(name,payload) VALUES (?,?)',
                    ('manual-history-incomplete:'+claim['profile_id'],
                     encode({'job_id':claim['job_id'],'attempt_id':delivery['attempt_id']})))
        Outbox(self.db).transition(delivery['id'],'sending','unknown',{'delivery_id':delivery['id'],'attempt_id':delivery['attempt_id'],
            'request_extra':delivery['request_extra'],'temporary_message_id':None,'final_message_id':None,'state':'unknown'})
        with self.db.transaction() as con:
            row=con.execute('SELECT * FROM workflow_jobs WHERE id=?',(claim['job_id'],)).fetchone()
            self._change(con,row,status='unknown',reasons=['unknown_outcome'])

    def on_history(self, revision_id, run_id):
        with self._lock:
            if self._closed:
                return []
            run=self.archive.get_run(run_id)
            if (run['kind']!='collection' or run['status']!='succeeded' or run['revision_id']!=revision_id
                    or run['provenance']!='captured'):
                return []
            with self.db.transaction() as con:
                rules=con.execute("SELECT * FROM workflow_jobs WHERE kind='rule' AND status IN ('blocked','waiting') AND approval_hash IS NOT NULL ORDER BY seq LIMIT 100").fetchall()
                if not rules:
                    return []
                source,eligible=self._source(con,revision_id)
                _,manual_eligible=self._source(con,revision_id,'manual_audience_reviewed')
                _,captured_eligible=self._source(con,revision_id,'captured')
                if not captured_eligible or run['input_snapshot']!={'post':source}:
                    return []
                first=con.execute('SELECT MIN(seq) FROM archive_revisions WHERE post_id=?',(source['post_id'],)).fetchone()[0]
                created=[];native=self._native()
                for rule in rules:
                    context=json.loads(rule['context']);spec=context['spec'];automatic=spec['timing']['delivery_mode']=='automatic'
                    if spec.get('source_policy')!='captured' and not (eligible if automatic else eligible or manual_eligible):
                        continue
                    if (first<=context['cutoff'] or con.execute('SELECT 1 FROM workflow_enrollments WHERE rule_id=? AND revision_id=?',(rule['id'],revision_id)).fetchone()
                            or not self._run_matches(spec,run['topics'],run['classification'],automatic=automatic)):
                        continue
                    if not self._topics_current(con,spec['topics']):
                        self._change(con,rule,status='blocked',reasons=['topic_configuration_changed']);continue
                    template=self._get_template(con,spec['template_id'])
                    if template['version']!=spec['template_version']:
                        self._change(con,rule,status='paused',reasons=['template_changed'],approval_hash=None,requires_approval=1);continue
                    rendered=render_template(template['body'],source,topic=self._topic_text(con,run_id))
                    now=self._time();due=max(now,context['last_due']+spec['timing']['interval_seconds'] if context['last_due'] is not None else now)
                    generated={'revision_ids':[revision_id],'target':spec['target'],'template_id':spec['template_id'],'template_version':spec['template_version'],
                        'source_policy':spec.get('source_policy','public_only'),
                        'timing':{'mode':'at_time','timezone':spec['timing']['timezone'],'at':stamp(due)},'due_times':[due],
                        'render_runs':{revision_id:run_id},'topics':[]}
                    identifier=uuid4().hex
                    con.execute('INSERT INTO workflow_drafts VALUES (?,1,?,NULL,NULL,?,?)',(identifier,encode(generated),now,now))
                    draft=self._get_draft(con,identifier)
                    reasons=self._target_reasons(con,spec['target'],native=native)
                    if rendered['missing_fields']:
                        reasons.append('missing_template_values')
                    if rendered['error']:
                        reasons.append(rendered['error'])
                    duplicate=self._duplicate(con,source['post_id'],spec['target'])
                    if duplicate:
                        reasons.append(duplicate)
                    if automatic and context.get('automatic_basis') is None:
                        reasons.append('automatic_evidence_unavailable')
                    child_context={'spec':generated,'run_id':run_id,'automatic_basis':context.get('automatic_basis') if automatic else None}
                    needs_review=not automatic or bool(rendered['missing_fields']) or rendered['error'] is not None
                    child=self._insert_job(con,draft,revision_id=revision_id,post_id=source['post_id'],text='' if rendered['error'] else rendered['text'],due_at=due,
                        status='draft' if needs_review else 'blocked' if reasons else 'waiting',reasons=['approval_required'] if needs_review else reasons,
                        approval_hash=None if needs_review else _digest({'rule_consent':rule['approval_hash'],'revision':revision_id,'run':run_id,'text':rendered['text'],'due':due}),
                        requires_approval=needs_review,rule_id=rule['id'],context=child_context)
                    con.execute('INSERT INTO workflow_enrollments VALUES (?,?,?)',(rule['id'],revision_id,child['id']))
                    if not duplicate:
                        context['last_due']=due
                        rule=self._change(con,rule,context=encode(context))
                    created.append(self._job(con,child))
                return created

    @staticmethod
    def _run_matches(spec, topics, classification, *, automatic):
        if classification is None or (automatic and classification['abstained']):
            return False
        captured={topic['id']:topic for topic in topics}
        if not all(captured.get(topic['id'])==topic for topic in spec['topics']):
            return False
        return any(item['topic_id'] in spec['timing']['topic_ids'] and item['match'] and not item['abstained'] for item in classification['topic_results'])
