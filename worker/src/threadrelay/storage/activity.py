"""Durable, bounded activity metadata. Observing it grants no work authority."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import re
import sqlite3


_KINDS = {
    **dict.fromkeys(('session_starting', 'session_running', 'session_paused',
                    'session_attention', 'session_stopped'), 'session'),
    **dict.fromkeys(('reading', 'skipped', 'history_saved', 'collection_recovering', 'collection_recovered'), 'collection'),
    **dict.fromkeys(('classification_started', 'classification_succeeded',
                    'classification_failed', 'classification_cancelled'), 'model'),
    **dict.fromkeys(('delivery_pending', 'delivery_preparing', 'delivery_sending', 'delivery_sent',
                    'delivery_failed', 'delivery_unknown', 'delivery_cancelled', 'delivery_retryable',
                    'job_draft', 'job_waiting', 'job_blocked', 'job_paused', 'job_cancelled',
                    'telegram_test_verifying', 'telegram_test_sending', 'telegram_test_sent',
                    'telegram_test_failed', 'telegram_test_unknown'), 'delivery'),
}


def _stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _sequence(value):
    if (not isinstance(value, str) or re.fullmatch(r'0|[1-9][0-9]{0,18}', value) is None
            or int(value) > 9223372036854775807):
        raise ValueError('Invalid activity sequence')
    return int(value)


def _text(value, limit):
    return value[:limit] if isinstance(value, str) else None


class ActivityStore:
    def __init__(self, database):
        self.db = database

    @contextmanager
    def _writer(self, connection, nonblocking):
        if connection is not None:
            yield connection
        elif not nonblocking:
            with self.db.transaction() as con:
                yield con
        else:
            with self.db.connection() as con:
                con.execute('PRAGMA busy_timeout=0')
                con.execute('BEGIN IMMEDIATE')
                try:
                    yield con
                    con.commit()
                except BaseException:
                    con.rollback()
                    raise

    def append(self, kind, *, detail=None, revision_id=None, run_id=None,
               delivery_id=None, job_id=None, connection=None, nonblocking=False):
        if not isinstance(kind, str) or kind not in _KINDS:
            raise ValueError('Invalid activity kind')
        if type(nonblocking) is not bool:
            raise ValueError('Invalid activity writer mode')
        if detail is not None and (not isinstance(detail, str) or len(detail) > 200):
            raise ValueError('Invalid activity detail')
        for reference in (revision_id, run_id, delivery_id, job_id):
            if reference is not None and (not isinstance(reference, str) or not 1 <= len(reference) <= 128):
                raise ValueError('Invalid activity reference')
        if connection is not None and (not isinstance(connection, sqlite3.Connection) or not connection.in_transaction):
            raise ValueError('Activity append requires the existing transaction')
        with self._writer(connection, nonblocking) as con:
            job = con.execute('SELECT due_at,context FROM workflow_jobs WHERE id=?', (job_id,)).fetchone() if job_id else None
            destination = None
            if job is not None:
                destination = json.loads(job['context']).get('spec', {}).get('target', {})
                destination = destination.get('destination_id') if isinstance(destination, dict) else None
            cursor = con.execute('''INSERT INTO activity_entries(at,kind,revision_id,run_id,delivery_id,job_id,detail,due_at,destination)
                VALUES (?,?,?,?,?,?,?,?,?)''', (self.db.now(), kind, revision_id, run_id, delivery_id,
                    job_id, detail, None if job is None else job['due_at'], _text(destination, 500)))
            return str(cursor.lastrowid)

    @staticmethod
    def _entry(row):
        result = None
        if row['kind'] == 'classification_succeeded' and row['classification'] is not None:
            classification = json.loads(row['classification'])
            result = ('abstained' if classification['abstained'] else
                      'match' if any(item['match'] for item in classification['topic_results']) else 'no_match')
        return {'sequence': str(row['seq']), 'at': _stamp(row['at']),
                'category': _KINDS[row['kind']], 'kind': row['kind'],
                'revision_id': row['source_revision'], 'run_id': row['run_id'],
                'delivery_id': row['delivery_id'], 'job_id': row['job_id'],
                'author': _text(row['author'], 500), 'text_preview': _text(row['text_preview'], 500),
                'permalink': _text(row['permalink'], 4096), 'destination': _text(row['destination'], 500),
                'detail': _text(row['detail'], 200), 'result': result,
                'due_at': None if row['due_at'] is None else _stamp(row['due_at'])}

    def query(self, after=None, before=None, limit=50):
        if after is not None and before is not None:
            raise ValueError('Choose one activity direction')
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Invalid activity page size')
        position = _sequence(after) if after is not None else _sequence(before) if before is not None else None
        forward = after is not None
        where = 'WHERE e.seq>?' if forward else 'WHERE e.seq<?' if before is not None else ''
        order = 'ASC' if forward else 'DESC'
        parameters = (() if position is None else (position,)) + (limit + 1,)
        with self.db.connection() as con:
            con.execute('BEGIN')
            rows = con.execute(f'''SELECT e.*,COALESCE(e.revision_id,run.revision_id,job.revision_id) AS source_revision,
                substr(json_extract(revision.payload,'$.author'),1,500) AS author,
                COALESCE(substr(json_extract(revision.payload,'$.text'),1,500),
                    CASE WHEN run.kind='sample' THEN substr(json_extract(run.input_snapshot,'$.text'),1,500) END) AS text_preview,
                CASE WHEN length(json_extract(revision.payload,'$.permalink'))<=4096
                    THEN json_extract(revision.payload,'$.permalink') END AS permalink,run.classification
                FROM activity_entries e LEFT JOIN archive_runs run ON run.id=e.run_id
                LEFT JOIN workflow_jobs job ON job.id=e.job_id
                LEFT JOIN archive_revisions revision ON revision.id=COALESCE(e.revision_id,run.revision_id,job.revision_id)
                {where} ORDER BY e.seq {order} LIMIT ?''', parameters).fetchall()
            entries, size = [], 0
            for row in rows[:limit]:
                entry = self._entry(row)
                count = len(json.dumps(entry, ensure_ascii=True).encode())
                if entries and size + count > 800000:
                    break
                entries.append(entry)
                size += count
            if not forward:
                entries.reverse()
            oldest = entries[0]['sequence'] if entries else None
            older = oldest is not None and con.execute('SELECT 1 FROM activity_entries WHERE seq<? LIMIT 1', (int(oldest),)).fetchone() is not None
            return {'entries': entries, 'cursor': entries[-1]['sequence'] if entries else after or '0',
                    'has_more': forward and len(rows) > len(entries),
                    'older_cursor': oldest if older else None, 'as_of': _stamp(self.db.now())}
