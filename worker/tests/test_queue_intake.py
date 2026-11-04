"""Independent route intake while delivery waits; synthetic native boundary only."""
from threading import Event

import pytest
from test_owner_automatic import auto as auto_fixture, manual as manual_fixture, configure_route_queue, enqueue, progress, send_requests
from test_owner_history import drain
from test_routing import post
from threadrelay.pipeline import Pipeline
from threadrelay.storage.posts import PostStore


auto=auto_fixture
manual=manual_fixture


def collect(env, suffix):
    source=post(post_id='threads:'+suffix,permalink='https://www.threads.net/@synthetic/post/'+suffix)
    router=env.sender.router
    pipeline=Pipeline(env.session,Event(),PostStore(env.db),env.config,router.classifier,router,env.sender)
    pipeline.process(source)


@pytest.mark.parametrize('network',['connectionStateConnecting','connectionStateWaitingForNetwork'])
def test_authenticated_network_wait_keeps_later_matches_queued_without_sending(auto,network):
    configure_route_queue(auto,queue={'mode':'immediate'})
    first=enqueue(auto,'BlockedHead')
    auto.auto_queue.schedule.block_unstarted(first,'verification_failed')
    auto.library.events.append({'@client_id':auto.library.client_id,'@type':'updateConnectionState','state':{'@type':network}})
    drain(auto)
    assert auto.session.snapshot['state']=='running'
    collect(auto,'MatchDuringReconnect')
    items=auto.auto_queue.schedule.query()['items']
    assert len(items)==2
    assert items[0]['state']=='blocked' and items[1]['state']=='queued'
    auto.auto_controller.tick()
    assert send_requests(auto)==[]
    auto.library.events.append({'@client_id':auto.library.client_id,'@type':'updateConnectionState','state':{'@type':'connectionStateReady'}})
    for _ in range(30):
        drain(auto);auto.auto_controller.tick()
        if send_requests(auto):break
    assert len(send_requests(auto))==1
    assert auto.auto_queue.schedule.get(first)['reason']=='verification_failed'


def test_unknown_send_blocks_further_sends_without_blocking_new_intake(auto):
    configure_route_queue(auto,queue={'mode':'immediate'})
    first=enqueue(auto,'UnconfirmedHead');progress(auto)
    auto.auto_controller._fail('dispatch_unconfirmed')
    assert auto.auto_controller._state=='unknown'
    collect(auto,'MatchAfterUnknown')
    items=auto.auto_queue.schedule.query()['items']
    assert len(items)==2 and items[0]['state']=='unknown' and items[1]['state']=='queued'
    for _ in range(3):auto.auto_controller.tick()
    assert len(send_requests(auto))==1
    assert auto.auto_queue.schedule.get(first)['started_at'] is not None


@pytest.mark.parametrize('explicit_pause',[False,True])
def test_network_recovery_never_reopens_paused_or_revoked_authority(auto,explicit_pause):
    from test_owner_history import auth_state
    configure_route_queue(auto,queue={'mode':'immediate'})
    enqueue(auto,'MustNotSend')
    if explicit_pause:auto.session.pause()
    else:
        auto.library.events.append(auth_state(auto,'LoggingOut'))
        drain(auto)
    assert auto.session.snapshot['state']!='running'
    auto.library.events.append({'@client_id':auto.library.client_id,'@type':'updateConnectionState','state':{'@type':'connectionStateReady'}})
    drain(auto)
    collect(auto,'MustNotQueue')
    assert auto.session.snapshot['state']!='running'
    assert len(auto.auto_queue.schedule.query()['items'])==1
    assert not send_requests(auto)


@pytest.mark.parametrize('first,second,expected',[(.95,.01,True),(.01,.95,True),(.95,.5,True),(.5,.95,True),(.95,.95,True),(.01,.01,False),(.5,.5,False)])
def test_route_matches_any_selected_topic_and_queues_once(auto,first,second,expected):
    from test_routing import topic
    auto.config.save_topic(topic('second'))
    auto.config.save_topic(topic('unselected'))
    auto.sender.router.classifier._runtime.decide=lambda state,questions:{key:{'science':first,'second':second,'unselected':.99}[key] for key in questions}
    configure_route_queue(auto,topic_ids=['science','second'],queue={'mode':'immediate'})
    collect(auto,'AnySelectedTopic')
    items=auto.auto_queue.schedule.query()['items']
    assert len(items)==int(expected)
    if expected:
        progress(auto)
        assert len(send_requests(auto))==1
    else:
        auto.auto_controller.tick()
        assert not send_requests(auto)
