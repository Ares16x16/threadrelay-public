"""Permanent observed records. Archive reads grant no collection/send authority."""
from datetime import datetime, timezone
import hashlib
import json
import re
from uuid import uuid4

from threadrelay.contracts import validate_contract
from .database import encode


_BINDINGS = frozenset(('model_id', 'model_revision', 'model_sha256', 'quantization',
    'runtime_version', 'runtime_commit', 'context_tokens', 'max_input_bytes', 'cpu_threads',
    'max_response_bytes', 'timeout_seconds', 'reading_mode', 'scores_calibrated',
    'prompt_version', 'cache_version', 'positive_threshold', 'negative_threshold', 'cache_hit'))
_ERRORS = frozenset(('session_cancelled', 'application_interrupted', 'model_unavailable',
    'classification_failed', 'cleanup_unconfirmed', 'classification_context_mismatch'))


def _stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _page(cursor, limit):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('Invalid archive page size')
    if cursor is not None and (not isinstance(cursor, str) or re.fullmatch(r'[1-9][0-9]{0,18}', cursor) is None):
        raise ValueError('Invalid archive cursor')
    return int(cursor) if cursor is not None else 9223372036854775807


class ArchiveStore:
    def __init__(self, database):
        self.db = database

    def observe(self, post):
        validate_contract('ObservedPost', post)
        payload = encode(post)
        revision = hashlib.sha256(payload.encode()).hexdigest()
        with self.db.transaction() as con:
            con.execute('INSERT OR IGNORE INTO archive_revisions(id,post_id,payload,provenance,created_at) VALUES (?,?,?,?,?)',
                        (revision, post['post_id'], payload, 'captured', self.db.now()))
            con.execute('INSERT INTO archive_observations(revision_id,observed_at,source_session_id,recorded_at) VALUES (?,?,?,?)',
                        (revision, post['observed_at'], post['source_session_id'], self.db.now()))
        return revision

    def get_revision(self, revision_id):
        with self.db.connection() as con:
            row = con.execute('SELECT payload FROM archive_revisions WHERE id=?', (revision_id,)).fetchone()
        if row is None:
            raise ValueError('Archived revision unavailable')
        return json.loads(row['payload'])

    def high_water(self):
        with self.db.connection() as con:
            return con.execute('SELECT COALESCE(MAX(seq),0) FROM archive_revisions').fetchone()[0]

    def revision_sequence(self, revision_id):
        with self.db.connection() as con:
            row = con.execute('SELECT r.seq, (SELECT MIN(seq) FROM archive_revisions WHERE post_id=r.post_id) AS first_seq FROM archive_revisions r WHERE id=?', (revision_id,)).fetchone()
        if row is None:
            raise ValueError('Archived revision unavailable')
        return {'sequence': row['seq'], 'first_post_sequence': row['first_seq']}

    def list_posts(self, cursor=None, limit=30, search=''):
        before = _page(cursor, limit)
        if not isinstance(search, str) or len(search) > 200:
            raise ValueError('Invalid archive search')
        with self.db.connection() as con:
            # Pick the current revision for each canonical post before applying the
            # cursor or search. Filtering first can resurrect an older revision on
            # a later page or when only its stale content matches the search.
            rows = con.execute("""SELECT r.* FROM archive_revisions r
                WHERE r.seq<? AND NOT EXISTS (
                    SELECT 1 FROM archive_revisions newer
                    WHERE newer.post_id=r.post_id AND newer.seq>r.seq
                ) AND (?='' OR instr(lower(r.payload),lower(?))>0)
                ORDER BY r.seq DESC LIMIT ?""", (before, search, search, limit + 1)).fetchall()
        items, size = [], 0
        for row in rows[:limit]:
            post = json.loads(row['payload'])
            item = {'revision_id': row['id'], 'post_id': post['post_id'], 'permalink': post['permalink'],
                'author': post['author'], 'text_preview': post['text'][:500], 'observed_at': post['observed_at'],
                'visibility': post['visibility'], 'quality_flags': post['quality_flags'], 'provenance': row['provenance']}
            item_size = len(json.dumps(item, ensure_ascii=True).encode())
            if items and size + item_size > 800000:
                break
            items.append(item); size += item_size
        return {'items': items, 'next_cursor': str(rows[len(items)-1]['seq']) if len(rows) > len(items) else None}

    def list_deliveries(self, cursor=None, limit=30, search=''):
        before = _page(cursor, limit)
        if not isinstance(search, str) or len(search) > 200:
            raise ValueError('Invalid archive search')
        with self.db.connection() as con:
            rows = con.execute("SELECT rowid AS seq,* FROM archive_deliveries WHERE rowid<? AND (?='' OR instr(lower(payload),lower(?))>0) ORDER BY rowid DESC LIMIT ?", (before, search, search, limit + 1)).fetchall()
            items, size = [], 0
            for row in rows[:limit]:
                count = con.execute('SELECT COUNT(*) FROM archive_attempts WHERE delivery_id=?', (row['id'],)).fetchone()[0]
                item = {'delivery': json.loads(row['payload']), 'attempt_count': count,
                        'created_at': _stamp(row['created_at']), 'updated_at': _stamp(row['updated_at'])}
                item_size = len(json.dumps(item, ensure_ascii=True).encode())
                if items and size + item_size > 800000:
                    break
                items.append(item); size += item_size
        return {'items': items, 'next_cursor': str(rows[len(items)-1]['seq']) if len(rows) > len(items) else None}

    def get_delivery(self, delivery_id):
        with self.db.connection() as con:
            con.execute('BEGIN')
            row = con.execute('SELECT payload FROM archive_deliveries WHERE id=?', (delivery_id,)).fetchone()
            if row is None:
                raise ValueError('Archived delivery unavailable')
            attempts = [dict(item) for item in con.execute('SELECT delivery_id,attempt_id,request_extra,temporary_message_id,final_message_id,state FROM archive_attempts WHERE delivery_id=? ORDER BY rowid', (delivery_id,))]
            return {'delivery': json.loads(row['payload']), 'attempts': attempts}

    def start_run(self, kind, post_revision_id, input_snapshot, topics, binding):
        if kind not in ('collection', 'sample') or not isinstance(input_snapshot, dict):
            raise ValueError('Invalid model run')
        if kind == 'collection':
            if set(input_snapshot) != {'post'} or self.get_revision(post_revision_id) != input_snapshot['post']:
                raise ValueError('Model input revision mismatch')
        elif post_revision_id is not None or set(input_snapshot) != {'text'} or not isinstance(input_snapshot['text'], str) or len(input_snapshot['text']) > 100000:
            raise ValueError('Invalid sample input')
        if not isinstance(topics, list) or len(topics) > 100:
            raise ValueError('Invalid model topics')
        for topic in topics:
            validate_contract('Topic', topic)
        if not isinstance(binding, dict) or set(binding) - _BINDINGS or any(type(v) not in (str, bool, int, float) for v in binding.values()):
            raise ValueError('Invalid model binding')
        run_id = uuid4().hex
        with self.db.transaction() as con:
            con.execute('INSERT INTO archive_runs(id,kind,revision_id,input_snapshot,topics,binding,started_at,status,provenance) VALUES (?,?,?,?,?,?,?,?,?)',
                (run_id, kind, post_revision_id, encode(input_snapshot), encode(topics), encode(binding), self.db.now(), 'running', 'captured'))
        return run_id

    def finish_run(self, run_id, status, classification=None, error_code=None):
        if status not in ('succeeded', 'failed', 'cancelled') or error_code not in _ERRORS | {None}:
            raise ValueError('Invalid model completion')
        if status == 'succeeded':
            validate_contract('Classification', classification)
            if error_code is not None:
                raise ValueError('Successful run cannot contain an error')
        elif classification is not None or error_code is None:
            raise ValueError('Unsuccessful run cannot publish a result')
        with self.db.transaction() as con:
            row = con.execute('SELECT * FROM archive_runs WHERE id=? AND status=?', (run_id, 'running')).fetchone()
            if row is None:
                raise ValueError('Run already completed or unavailable')
            if classification is not None and row['kind'] == 'collection':
                source = json.loads(row['input_snapshot'])['post']
                if classification['post_id'] != source['post_id']:
                    raise ValueError('Result post mismatch')
            con.execute('UPDATE archive_runs SET status=?,classification=?,error_code=?,finished_at=? WHERE id=?',
                (status, None if classification is None else encode(classification), error_code, self.db.now(), run_id))

    @staticmethod
    def _run(row, *, detail=False):
        inputs = json.loads(row['input_snapshot']) if detail else {}
        topics = json.loads(row['topics']) if detail else []
        value = {'run_id': row['id'], 'kind': row['kind'], 'revision_id': row['revision_id'],
            'started_at': None if row['started_at'] is None else _stamp(row['started_at']), 'finished_at': None if row['finished_at'] is None else _stamp(row['finished_at']),
            'status': row['status'], 'classification': None if row['classification'] is None else json.loads(row['classification']),
            'error_code': row['error_code'], 'input_preview': inputs.get('text', inputs.get('post', {}).get('text', ''))[:500] if detail else row['input_preview'],
            'topic_count': len(topics) if detail else row['topic_count'], 'binding': json.loads(row['binding']), 'provenance': row['provenance']}
        if detail:
            value.update(input_snapshot=inputs, topics=topics)
        return value

    def get_run(self, run_id):
        with self.db.connection() as con:
            row = con.execute('SELECT * FROM archive_runs WHERE id=?', (run_id,)).fetchone()
        if row is None:
            raise ValueError('Model run unavailable')
        return self._run(row, detail=True)

    def list_runs(self, cursor=None, limit=30, revision_id=None):
        before = _page(cursor, limit)
        with self.db.connection() as con:
            rows = con.execute("""SELECT seq,id,kind,revision_id,started_at,finished_at,status,
                classification,error_code,binding,provenance,
                substr(COALESCE(json_extract(input_snapshot,'$.text'),json_extract(input_snapshot,'$.post.text'),''),1,500) AS input_preview,
                json_array_length(topics) AS topic_count
                FROM archive_runs WHERE seq<? AND (? IS NULL OR revision_id=?) ORDER BY seq DESC LIMIT ?""",
                (before, revision_id, revision_id, limit+1)).fetchall()
        # Many maximum-size per-topic outputs can fill a frame. Stop on a complete
        # record and return its cursor; never lose a result at a byte boundary.
        items, size = [], 0
        for row in rows[:limit]:
            item = self._run(row)
            item_size = len(json.dumps(item, ensure_ascii=True).encode())
            if items and size + item_size > 800000:
                break
            items.append(item); size += item_size
        return {'items': items, 'next_cursor': str(rows[len(items)-1]['seq']) if len(rows) > len(items) else None}

    def read_detail(self, kind, record_id, offset=0, expected_digest=None):
        if type(offset) is not int or offset < 0 or kind not in ('revision', 'run', 'delivery'):
            raise ValueError('Invalid archive detail request')
        value = {'revision': self.get_revision, 'run': self.get_run, 'delivery': self.get_delivery}[kind](record_id)
        raw = encode(value)
        if offset > len(raw):
            raise ValueError('Invalid archive detail offset')
        digest = hashlib.sha256(raw.encode()).hexdigest()
        if expected_digest is not None and expected_digest != digest:
            raise ValueError('Archive detail changed; refresh it')
        if offset and expected_digest is None:
            raise ValueError('Archive continuation requires a snapshot digest')
        end = min(len(raw), offset + 100000)
        return {'kind': kind, 'record_id': record_id, 'offset': offset, 'data': raw[offset:end],
                'next_offset': end if end < len(raw) else None, 'digest': digest}
