"""User-selected continuous intake preserves explicit pause and hard limits."""
from test_session import Clock, configured, config


def test_continuous_collection_ignores_optional_post_and_duration_caps_until_pause():
    clock = Clock()
    session = configured(clock)
    session.start(config(mode='review',continuous=True,max_posts=1,duration_seconds=1))
    for _ in range(3):
        assert session.observe_post()
    clock.advance(3600)
    assert session.can_intake() and session.can_transmit(clock.now())
    assert session.snapshot['counters']['observed'] == 3
    session.pause()
    clock.advance(3600)
    assert not session.can_intake() and not session.can_transmit(clock.now())


def test_bounded_collection_and_auto_delivery_budget_remain_explicit():
    session = configured()
    session.start(config(continuous=False,max_posts=1))
    assert session.observe_post()
    assert session.snapshot['attention_reason'] == 'post_limit'
    session = configured()
    session.start(config(mode='auto',continuous=True,max_auto_deliveries=0))
    assert session.snapshot['attention_reason'] == 'automatic_delivery_limit'
