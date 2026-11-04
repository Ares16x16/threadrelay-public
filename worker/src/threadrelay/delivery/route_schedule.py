"""Durable exact message snapshots and route-local dispatch spacing."""
from datetime import datetime, timezone
import json
import math
import random

from threadrelay.storage.database import encode


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace('+00:00', 'Z')


class RouteSchedule:
    def __init__(self, db, *, draw=random.SystemRandom().uniform):
        self.db, self.draw = db, draw

    def enroll(self, con, delivery_id, route, text, destination_id, destination_title, *, snapshot=None, reason=None):
        config = route.get('queue') or {'mode': 'immediate'}
        mode = config['mode']
        gap = (config['interval_seconds'] if mode == 'interval' else
               self.draw(config['minimum_seconds'], config['maximum_seconds']) if mode == 'random' else 0)
        due = self.db.now()
        if config.get('start_at'):
            due = max(due, datetime.fromisoformat(config['start_at'].replace('Z', '+00:00')).timestamp())
        previous = con.execute('SELECT due_at,started_at FROM route_queue WHERE route_id=? AND cancelled=0 AND reason IS NULL ORDER BY sequence DESC LIMIT 1', (route['id'],)).fetchone()
        if previous:
            due = max(due, (previous['started_at'] if previous['started_at'] is not None else previous['due_at']) + gap)
        con.execute('INSERT OR IGNORE INTO route_queue(delivery_id,route_id,route_version,text,destination_id,destination_title,due_at,gap_seconds,reason,snapshot,sequence) VALUES (?,?,?,?,?,?,?,?,?,?,(SELECT COALESCE(MAX(sequence),0)+1 FROM route_queue))',
                    (delivery_id, route['id'], route['version'], text, destination_id, destination_title, due, gap, reason, encode(snapshot or {})))

    def get(self, delivery_id, con=None):
        if con is None:
            with self.db.connection() as connection:
                return self.get(delivery_id, connection)
        row = con.execute('SELECT * FROM route_queue WHERE delivery_id=?', (delivery_id,)).fetchone()
        return dict(row) if row else None

    def provider_wait(self, con, account_id, destination_id=None):
        if account_id is None:
            return None
        names=['telegram-wait:'+account_id]
        if destination_id is not None:names.append('telegram-wait:'+account_id+':'+destination_id)
        active=[]
        for name in names:
            row=con.execute('SELECT payload FROM local_policy WHERE name=?',(name,)).fetchone()
            if row is not None:
                value=json.loads(row[0])
                if value['until']>self.db.now():active.append(value)
        return max(active,key=lambda value:value['until']) if active else None

    def require_ready(self, con, delivery_id):
        row = self.get(delivery_id, con)
        if row is None or row['cancelled'] or row['reason'] or row['started_at'] is not None:
            raise ValueError('queue_item_unavailable')
        delivery=con.execute('SELECT account_id FROM deliveries WHERE id=?',(delivery_id,)).fetchone()
        if delivery and self.provider_wait(con,delivery[0],row['destination_id']):
            raise ValueError('telegram_provider_wait')
        paused = con.execute('SELECT paused FROM route_queue_control WHERE route_id=?', (row['route_id'],)).fetchone()
        if paused and paused[0]:
            raise ValueError('route_queue_paused')
        earlier = con.execute("SELECT 1 FROM route_queue q LEFT JOIN deliveries d ON d.id=q.delivery_id WHERE q.route_id=? AND q.sequence<? AND q.cancelled=0 AND q.reason IS NULL AND q.started_at IS NULL AND (d.state IS NULL OR d.state IN ('pending','sending')) LIMIT 1", (row['route_id'], row['sequence'])).fetchone()
        if row['due_at'] > self.db.now() or earlier:
            raise ValueError('queue_item_waiting')
        return row

    def ready(self, delivery_id):
        try:
            with self.db.connection() as con:
                self.require_ready(con, delivery_id)
            return True
        except ValueError:
            return False

    def started(self, con, delivery_id):
        row = self.require_ready(con, delivery_id)
        now = self.db.now()
        con.execute('UPDATE route_queue SET started_at=? WHERE delivery_id=?', (now, delivery_id))
        # A delayed first dispatch moves the remaining schedule forward. Each
        # random gap is sampled only at enrollment and never redrawn on restart.
        due = now
        for item in con.execute('SELECT * FROM route_queue WHERE route_id=? AND sequence>? AND started_at IS NULL AND cancelled=0 ORDER BY sequence', (row['route_id'], row['sequence'])).fetchall():
            due = max(item['due_at'], due + item['gap_seconds'])
            con.execute('UPDATE route_queue SET due_at=? WHERE delivery_id=?', (due, item['delivery_id']))

    def pause(self, route_id, paused):
        with self.db.transaction() as con:
            con.execute('INSERT INTO route_queue_control VALUES (?,?) ON CONFLICT(route_id) DO UPDATE SET paused=excluded.paused', (route_id, int(paused)))
            if not paused:
                # Explicit resume may repeat metadata checks, never a send that
                # was claimed or initiated. All route/account checks run again.
                con.execute("""UPDATE route_queue SET reason=NULL
                    WHERE route_id=? AND cancelled=0 AND started_at IS NULL
                    AND reason IN ('verification_failed','preflight_unavailable','timed_out','preflight_retry_exhausted','destination_verification_interrupted','destination_verification_incomplete')
                    AND EXISTS (SELECT 1 FROM deliveries d WHERE d.id=route_queue.delivery_id
                        AND d.state='pending'
                        AND json_extract(d.payload,'$.attempt_id') IS NULL
                        AND json_extract(d.payload,'$.request_extra') IS NULL
                        AND json_extract(d.payload,'$.message_id') IS NULL
                        AND json_extract(d.payload,'$.temporary_message_id') IS NULL
                        AND json_extract(d.payload,'$.final_message_id') IS NULL)
                    AND NOT EXISTS (SELECT 1 FROM telegram_attempts t WHERE t.delivery_id=route_queue.delivery_id)
                    AND NOT EXISTS (SELECT 1 FROM routing_delivery_context r WHERE r.delivery_id=route_queue.delivery_id AND r.initiated_attempt_id IS NOT NULL)
                    AND NOT EXISTS (SELECT 1 FROM workflow_delivery_context w WHERE w.delivery_id=route_queue.delivery_id AND w.initiated_attempt_id IS NOT NULL)""", (route_id,))

    def cancel(self, delivery_id):
        with self.db.transaction() as con:
            row = self.get(delivery_id, con)
            if row is None or row['started_at'] is not None:
                raise ValueError('only_unstarted_queue_items_can_be_cancelled')
            delivery = con.execute('SELECT state FROM deliveries WHERE id=?', (delivery_id,)).fetchone()
            if delivery and delivery[0] != 'pending':
                raise ValueError('only_unstarted_queue_items_can_be_cancelled')
            con.execute('UPDATE route_queue SET cancelled=1 WHERE delivery_id=?', (delivery_id,))
            if delivery:
                from threadrelay.storage.outbox import _set_state
                record = con.execute('SELECT * FROM deliveries WHERE id=?', (delivery_id,)).fetchone()
                _set_state(con, record, 'cancelled', self.db.now())

    def clear(self, route_id):
        """Pause this route and cancel pristine entries across every page."""
        with self.db.transaction() as con:
            con.execute('INSERT INTO route_queue_control VALUES (?,1) ON CONFLICT(route_id) DO UPDATE SET paused=1',(route_id,))
            rows=con.execute("""SELECT q.delivery_id,d.payload,d.state FROM route_queue q
                LEFT JOIN deliveries d ON d.id=q.delivery_id
                WHERE q.route_id=? AND q.started_at IS NULL AND q.cancelled=0
                AND (d.state IS NULL OR (d.state='pending'
                    AND json_extract(d.payload,'$.attempt_id') IS NULL
                    AND json_extract(d.payload,'$.request_extra') IS NULL
                    AND json_extract(d.payload,'$.message_id') IS NULL
                    AND json_extract(d.payload,'$.temporary_message_id') IS NULL
                    AND json_extract(d.payload,'$.final_message_id') IS NULL
                    AND NOT EXISTS(SELECT 1 FROM telegram_attempts t WHERE t.delivery_id=q.delivery_id)
                    AND NOT EXISTS(SELECT 1 FROM routing_delivery_context r WHERE r.delivery_id=q.delivery_id AND r.initiated_attempt_id IS NOT NULL)
                    AND NOT EXISTS(SELECT 1 FROM workflow_delivery_context w WHERE w.delivery_id=q.delivery_id AND w.initiated_attempt_id IS NOT NULL)))""",(route_id,)).fetchall()
            for item in rows:
                con.execute('UPDATE route_queue SET cancelled=1 WHERE delivery_id=?',(item['delivery_id'],))
                if item['state'] is not None:
                    from threadrelay.storage.outbox import _set_state
                    row=con.execute('SELECT * FROM deliveries WHERE id=?',(item['delivery_id'],)).fetchone()
                    _set_state(con,row,'cancelled',self.db.now())
            return len(rows)

    def block_unstarted(self, delivery_id, reason, *, require_unpaused=False):
        """Record a static failure without changing the message or attempt state."""
        with self.db.transaction() as con:
            if require_unpaused:
                row = con.execute('SELECT 1 FROM route_queue q JOIN route_queue_control c ON c.route_id=q.route_id WHERE q.delivery_id=? AND c.paused=1', (delivery_id,)).fetchone()
                if row:
                    return False
            changed = con.execute("UPDATE route_queue SET reason=? WHERE delivery_id=? AND started_at IS NULL AND cancelled=0 AND reason IS NULL AND EXISTS (SELECT 1 FROM deliveries d WHERE d.id=route_queue.delivery_id AND d.state='pending')", (reason, delivery_id))
            return changed.rowcount == 1

    def query(self, route_id=None, limit=100, cursor=None):
        after = int(cursor) if cursor else 0
        if after < 0:
            raise ValueError('invalid_queue_cursor')
        with self.db.connection() as con:
            rows = con.execute('SELECT q.*, d.state AS delivery_state, d.account_id AS delivery_account_id FROM route_queue q LEFT JOIN deliveries d ON d.id=q.delivery_id WHERE q.sequence>? AND (? IS NULL OR q.route_id=?) ORDER BY q.sequence LIMIT ?', (after, route_id, route_id, limit + 1)).fetchall()
            controls = con.execute('SELECT route_id,paused FROM route_queue_control').fetchall()
            paused = {r['route_id'] for r in controls if r['paused']}
            items = []
            for row in rows[:limit]:
                state = row['delivery_state'] or 'pending'
                wait=self.provider_wait(con,row['delivery_account_id'],row['destination_id']) if state=='pending' else None
                reason=row['reason']
                due=row['due_at']
                if row['cancelled']:
                    state = 'cancelled'
                elif row['reason'] and state == 'pending':
                    state = 'blocked'
                elif state == 'pending' and wait:
                    state='waiting'
                    reason=wait['reason']+':'+str(math.ceil(wait['until']-self.db.now()))
                    due=max(due,wait['until'])
                elif state == 'pending':
                    state = 'queued' if row['due_at'] <= self.db.now() and row['route_id'] not in paused else 'waiting'
                elif state == 'retryable':
                    state = 'unknown'
                if state=='unknown':
                    from threadrelay.storage.isolation import is_isolated_delivery
                    if is_isolated_delivery(con,row['delivery_id']):reason='archived_not_retried'
                items.append({'id':row['delivery_id'], 'route_id':row['route_id'], 'route_version':row['route_version'], 'text':row['text'], 'destination_id':row['destination_id'], 'destination_title':row['destination_title'], 'due_at':stamp(due), 'state':state, 'reason':reason})
            return {'items':items, 'paused':route_id in paused if route_id else bool(paused), 'next_cursor':str(rows[limit-1]['sequence']) if len(rows)>limit else None}
