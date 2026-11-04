from datetime import datetime, timezone
import pytest
from threadrelay.storage.database import Database
from threadrelay.delivery.route_schedule import RouteSchedule


def test_schedule_random_pause_restart_and_spacing(tmp_path):
    now = [datetime(2026, 11, 2, tzinfo=timezone.utc)]
    path = tmp_path / 'queue.sqlite'
    db = Database(path, clock=lambda: now[0])
    draws = []
    queue = RouteSchedule(db, draw=lambda low, high: draws.append((low, high)) or 12)
    route = {'id': 'r', 'version': 1, 'queue': {'mode': 'random', 'minimum_seconds': 10, 'maximum_seconds': 20}}
    for identifier in ('a', 'b'):
        with db.transaction() as con:
            queue.enroll(con, identifier, route, 'immutable ' + identifier, 'target', 'Group')
    assert draws == [(10, 20), (10, 20)]
    assert queue.ready('a') and not queue.ready('b')
    queue.pause('r', True)
    assert not queue.ready('a')
    db.close()
    db = Database(path, clock=lambda: now[0])
    queue = RouteSchedule(db, draw=lambda *_: pytest.fail('random gap was redrawn'))
    assert not queue.ready('a')
    queue.pause('r', False)
    now[0] = datetime(2026, 11, 2, 1, tzinfo=timezone.utc)
    assert queue.ready('a') and not queue.ready('b')
    with db.transaction() as con:
        queue.started(con, 'a')
    assert not queue.ready('b')
    now[0] = datetime(2026, 11, 2, 1, 0, 12, tzinfo=timezone.utc)
    assert queue.ready('b')
    db.close()


def test_immediate_and_start_at_and_cancel(tmp_path):
    db = Database(tmp_path / 'queue.sqlite', clock=lambda: datetime(2026, 11, 2, tzinfo=timezone.utc))
    queue = RouteSchedule(db)
    with db.transaction() as con:
        queue.enroll(con, 'a', {'id': 'r', 'version': 1, 'queue': {'mode': 'interval', 'interval_seconds': 60, 'start_at':'2026-11-02T01:00:00Z'}}, 'url', '-1', 'Group')
    assert not queue.ready('a')
    queue.cancel('a')
    assert not queue.ready('a')
    assert queue.query()['items'][0]['state'] == 'cancelled'
    db.close()
