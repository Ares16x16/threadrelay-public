"""Clear only unstarted work; retain every permanent delivery record."""
from test_owner_history import manual as manual_fixture
from test_owner_automatic import auto as auto_fixture,configure_route_queue,enqueue
from threadrelay.storage.outbox import Outbox,_set_state

manual=manual_fixture
auto=auto_fixture


def test_clear_queue_covers_all_pages_pauses_and_preserves_started_and_history(auto):
    env=auto;configure_route_queue(env,queue={'mode':'immediate'})
    ids=[enqueue(env,'Clear'+str(i)) for i in range(125)]
    with env.db.transaction() as con:
        for item,state in zip(ids[:5],['unknown','sending','sent','failed','retryable']):
            row=con.execute('SELECT * FROM deliveries WHERE id=?',(item,)).fetchone()
            _set_state(con,row,state,env.db.now())
        con.execute('UPDATE route_queue SET started_at=1 WHERE delivery_id=?',(ids[5],))
        env.auto_queue.schedule.enroll(con,'other-route',{'id':'other','version':1},'other','-99','Other group')
    before={item:Outbox(env.db).get(item) for item in ids[:6]}
    with env.db.connection() as con:archived=con.execute('SELECT COUNT(*) FROM archive_deliveries').fetchone()[0]
    assert env.auto_queue.schedule.clear('score')==119
    result=env.auto_queue.schedule.query(route_id='score',limit=200)
    assert result['paused'] and sum(i['state']=='cancelled' for i in result['items'])==119
    assert all(Outbox(env.db).get(item)==before[item] for item in ids[:6])
    assert env.auto_queue.schedule.query(route_id='other')['items'][0]['state']=='queued'
    assert env.auto_queue.schedule.clear('score')==0
    with env.db.connection() as con:assert con.execute('SELECT COUNT(*) FROM archive_deliveries').fetchone()[0]==archived


def test_clear_queue_command_reaches_the_real_scoped_queue(auto):
    from threadrelay.features import LocalFeatures
    from threadrelay.main import dispatch
    env=auto;configure_route_queue(env,queue={'mode':'immediate'});item=enqueue(env,'ClearCommand')
    event=dispatch({'version':1,'id':'clear-command','type':'ClearRouteQueue','payload':{'route_id':'score'}},
        features=LocalFeatures(env.db,session=env.session))
    assert event=={'version':1,'id':'clear-command','type':'Acknowledged','payload':{'operation':'ClearRouteQueue'}}
    assert Outbox(env.db).get(item)['state']=='cancelled'
    assert env.auto_queue.schedule.query(route_id='score')['paused']
