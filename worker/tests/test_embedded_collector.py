"""Embedded collector source regressions over synthetic host leases, no WebKit proof."""

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest

from threadrelay.collector.browser import BrowserShutdownUnconfirmed, BrowserUnavailable
from threadrelay.collector.embedded import (
    EmbeddedBrowserSession,
    validate_embedded_snapshot,
)
from test_collector import raw_snapshot

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def snapshot(**changes):
    return {
        "schema_version": 1,
        "state": "ready",
        "cards": [raw_snapshot()],
        "scroll_y": 0,
        "viewport_height": 900,
        **changes,
    }


class Lease:
    webview_generation = 4
    source_session_id = "a" * 64
    status = "active"
    closed = 0

    def __init__(self):
        self.frames = [snapshot()]
        self.close_report = {"stopped": True, "remaining": []}
        self.calls = []
        self.on_snapshot = lambda: None
        self.on_scroll = lambda: None

    def poll(self):
        return self.status

    def snapshot(self):
        self.calls.append("snapshot")
        self.on_snapshot()
        return deepcopy(self.frames.pop(0) if len(self.frames) > 1 else self.frames[0])

    def scroll(self):
        self.calls.append("scroll")
        self.on_scroll()

    def close(self):
        self.closed += 1
        return deepcopy(self.close_report)


class Broker:
    def __init__(self):
        self.lease = Lease()
        self.attaches = 0

    def attach_embedded_threads(self):
        self.attaches += 1
        return self.lease


def opened(tmp_path, gate=lambda: True):
    broker = Broker()
    reasons = []
    browser = EmbeddedBrowserSession(
        app_data_dir=tmp_path,
        broker=broker,
        can_intake=gate,
        on_attention=reasons.append,
        now=lambda: NOW,
    )
    browser.open(tmp_path / "browser-profiles/threads")
    return browser, broker, reasons


def test_embedded_session_captures_before_scroll_and_binds_source(tmp_path):
    browser, broker, _ = opened(tmp_path)
    browser.scroll_next()
    broker.lease.frames = [
        snapshot(
            cards=[raw_snapshot(permalink="https://www.threads.com/@alice/post/Second")]
        )
    ]
    posts = browser.observe()
    assert broker.lease.calls[:2] == ["snapshot", "scroll"]
    assert [p["post_id"] for p in posts] == ["threads:Code_1", "threads:Second"]
    assert all(p["source_session_id"] == "a" * 64 for p in posts)
    assert browser.observe() == []
    browser.close()
    assert broker.attaches == 1 and broker.lease.closed == 1


def test_embedded_dedup_reconciles_buffered_edit_reverting_to_last_emission(tmp_path):
    browser, broker, _ = opened(tmp_path)
    assert browser.observe()[0]["text"] == "A bounded visible post."
    broker.lease.frames = [snapshot(cards=[raw_snapshot(text="Buffered edit")])]
    browser.scroll_next()
    broker.lease.frames = [snapshot()]
    assert browser.observe() == []
    browser.close()


@pytest.mark.parametrize(
    "state",
    [
        "manual_login_or_challenge",
        "unexpected_navigation",
        "snapshot_limit",
    ],
)
def test_embedded_native_attention_state_pauses_without_scroll_or_reattach(
    tmp_path, state
):
    browser, broker, reasons = opened(tmp_path)
    broker.lease.frames = [snapshot(state=state, cards=[])]
    assert browser.observe() == []
    browser.scroll_next()
    assert browser.observe() == []
    assert (
        reasons == [state]
        and broker.attaches == 1
        and broker.lease.calls == ["snapshot"]
    )
    browser.close()


def test_embedded_gate_closure_during_snapshot_discards_late_posts_and_scroll(tmp_path):
    gate = [True]
    browser, broker, _ = opened(tmp_path, lambda: gate[0])
    broker.lease.on_snapshot = lambda: gate.__setitem__(0, False)
    assert browser.observe() == []
    browser.scroll_next()
    assert broker.lease.calls == ["snapshot"]
    browser.close()


def test_embedded_default_gate_does_not_snapshot_or_scroll(tmp_path):
    browser, broker, _ = opened(tmp_path, lambda: False)
    browser.scroll_next()
    assert browser.observe() == [] and broker.lease.calls == []
    browser.close()


@pytest.mark.parametrize(
    "state,reason",
    [("disconnected", "browser_lost"), ("ownership_lost", "browser_ownership_lost")],
)
def test_embedded_ownership_loss_pauses_once(tmp_path, state, reason):
    browser, broker, reasons = opened(tmp_path)
    broker.lease.status = state
    assert browser.observe() == []
    browser.scroll_next()
    assert reasons == [reason] and broker.lease.calls == []
    browser.close()


def test_embedded_close_retains_failed_lease_for_later_cleanup(tmp_path):
    browser, broker, reasons = opened(tmp_path)
    broker.lease.close_report = {"stopped": False, "remaining": ["embedded_threads"]}
    with pytest.raises(BrowserShutdownUnconfirmed):
        browser.close()
    with pytest.raises(BrowserUnavailable):
        browser.open(tmp_path / "browser-profiles/threads")
    assert browser.observe() == [] and reasons == ["browser_shutdown_unconfirmed"]
    broker.lease.close_report = {"stopped": True, "remaining": []}
    browser.close()
    assert broker.lease.closed == 2


@pytest.mark.parametrize(
    "change",
    [
        {"extra": True},
        {"schema_version": True},
        {"state": "script"},
        {"cards": [raw_snapshot()] * 101},
        {"scroll_y": float("nan")},
        {"viewport_height": False},
        {"viewport_height": 0},
        {"cards": [{"issue": "arbitrary_code"}]},
        {"cards": [raw_snapshot(text=7)]},
        {"cards": [raw_snapshot(extra="unapproved")]},
        {"cards": [raw_snapshot(text="x" * 100000)] * 11},
    ],
)
def test_embedded_snapshot_rejects_unbounded_or_unrecognized_wire_shape(change):
    with pytest.raises(ValueError):
        validate_embedded_snapshot(snapshot(**change))


def test_native_embedded_script_contains_exact_existing_rendered_card_extractor():
    from threadrelay.collector.extractor import RENDERED_SNAPSHOT_JS

    path = (
        Path(__file__).parents[2] / "apps/desktop/src-tauri/native/embedded-snapshot.js"
    )
    source = path.read_text()
    assert "const snapshotCard = " + RENDERED_SNAPSHOT_JS + ";" in source
    assert source.index("manual_login_or_challenge") < source.index(
        "const snapshotCard"
    )
    assert "snapshot_limit" in source and "1000000" in source


def test_embedded_mixed_unsupported_cards_retain_issues_and_admit_valid_posts(tmp_path):
    browser, broker, reasons = opened(tmp_path)
    broker.lease.frames = [snapshot(cards=[{"issue": "unknown_layout"}, raw_snapshot(), {"issue": "not_visible"}])]
    assert [post["post_id"] for post in browser.observe()] == ["threads:Code_1"]
    assert browser.issues == [{"code": "unknown_layout", "detail": "unknown layout"}]
    assert reasons == []
    browser.scroll_next()
    assert broker.lease.calls[-1] == "scroll"
    browser.close()


def test_embedded_any_card_limit_still_pauses_without_scroll(tmp_path):
    browser, broker, reasons = opened(tmp_path)
    broker.lease.frames = [snapshot(cards=[raw_snapshot(), {"issue": "snapshot_limit"}])]
    assert browser.observe() == [] and reasons == ["snapshot_limit"]
    browser.scroll_next()
    assert broker.lease.calls == ["snapshot"]
    browser.close()


def test_embedded_skips_unsupported_cards_and_captures_a_later_valid_viewport(tmp_path):
    browser, broker, reasons = opened(tmp_path)
    broker.lease.frames = [snapshot(cards=[{"issue":"unknown_layout"},{"issue":"not_visible"}])]
    assert browser.observe() == []
    assert browser.issues == [{"code":"unknown_layout","detail":"unknown layout"}]
    assert reasons == []
    browser.scroll_next()
    assert broker.lease.calls[-1] == "scroll"
    broker.lease.frames = [snapshot(scroll_y=585)]
    assert [post['post_id'] for post in browser.observe()] == ['threads:Code_1']
    assert reasons == [] and broker.attaches == 1
    browser.close()


def test_embedded_unsupported_recovery_uses_bounded_scroll_cycles_and_cooldowns(tmp_path):
    from test_session import Clock
    clock = Clock()
    browser, broker, reasons = opened(tmp_path)
    browser._monotonic = clock.monotonic
    broker.lease.frames = [snapshot(cards=[{"issue":"unknown_layout"}])]
    for _ in range(5):
        assert browser.observe() == []
    assert reasons == []
    for attempt, cooldown in enumerate([1, 2, 3, 3, 3, 3], 1):
        for _ in range(3):
            browser.scroll_next()
        assert broker.lease.calls.count("scroll") == attempt * 3
        assert browser.observe() == [] and reasons == []
        assert browser.recovery_status() == {'reason':'unsupported_cards','retry_in_seconds':cooldown,'attempt':attempt}
        before = list(broker.lease.calls)
        for _ in range(5):
            assert browser.observe() == []
            browser.scroll_next()
        assert broker.lease.calls == before and broker.attaches == 1
        clock.advance(cooldown)
    browser.close()


def test_embedded_recovery_does_not_scroll_past_a_later_global_login_block(tmp_path):
    browser, broker, reasons = opened(tmp_path)
    broker.lease.frames = [snapshot(cards=[{"issue":"unknown_layout"}])]
    assert browser.observe() == []
    broker.lease.frames = [snapshot(state='manual_login_or_challenge',cards=[])]
    browser.scroll_next()
    assert reasons == ['manual_login_or_challenge']
    assert 'scroll' not in broker.lease.calls
    browser.close()


def test_embedded_validated_card_resets_unsupported_viewport_recovery_budget(tmp_path):
    from test_session import Clock
    clock = Clock()
    browser, broker, reasons = opened(tmp_path)
    browser._monotonic = clock.monotonic
    unsupported = snapshot(cards=[{"issue":"unsupported_text_layout"}])
    broker.lease.frames = [unsupported]
    for _ in range(3):
        browser.scroll_next()
    broker.lease.frames = [snapshot()]
    assert len(browser.observe()) == 1
    broker.lease.frames = [unsupported]
    for _ in range(3):
        browser.scroll_next()
    assert broker.lease.calls.count('scroll') == 6
    assert reasons == []
    assert browser.observe() == [] and reasons == []
    assert browser.recovery_status()['attempt'] == 1
    clock.advance(1)
    broker.lease.frames = [snapshot(cards=[raw_snapshot(permalink='https://www.threads.com/@alice/post/Later')])]
    assert [post['post_id'] for post in browser.observe()] == ['threads:Later']
    assert browser.recovery_status() is None and browser.recovery_delay() == 0
    browser.close()


@pytest.mark.parametrize('frame,reason', [
    (snapshot(state='unknown_layout',cards=[]),'unknown_layout'),
    (snapshot(cards=[]),'empty_feed'),
])
def test_embedded_unknown_feed_advances_once_after_cooldown_in_same_lease(tmp_path, frame, reason):
    from test_session import Clock
    clock = Clock()
    browser, broker, reasons = opened(tmp_path)
    browser._monotonic = clock.monotonic
    assert len(browser.observe()) == 1
    broker.lease.frames = [frame]
    assert browser.observe() == [] and reasons == []
    assert browser.recovery_status() == {'reason':reason,'retry_in_seconds':1,'attempt':1}
    for _ in range(4):
        browser.scroll_next()
        assert browser.observe() == []
    assert 'scroll' not in broker.lease.calls
    clock.advance(1)
    assert browser.observe() == []
    browser.scroll_next()
    assert broker.lease.calls.count('scroll') == 1
    assert browser.recovery_status() == {'reason':reason,'retry_in_seconds':2,'attempt':2}
    clock.advance(2)
    broker.lease.frames = [snapshot(cards=[raw_snapshot(permalink='https://www.threads.com/@alice/post/Recovered')])]
    assert [post['post_id'] for post in browser.observe()] == ['threads:Recovered']
    assert browser.recovery_status() is None and reasons == []
    assert broker.attaches == 1 and broker.lease.closed == 0
    browser.close()


@pytest.mark.parametrize('frame', [
    snapshot(state='unknown_layout', cards=[]),
    snapshot(cards=[]),
])
def test_embedded_recovery_reaches_new_viewport_without_user_navigation(tmp_path, frame):
    from test_session import Clock
    clock = Clock()
    browser, broker, reasons = opened(tmp_path)
    browser._monotonic = clock.monotonic
    broker.lease.frames = [frame]
    broker.lease.on_scroll = lambda: setattr(broker.lease, 'frames', [snapshot(scroll_y=585)])
    assert browser.observe() == []
    clock.advance(1)
    # The run loop observes before scrolling. Re-reading the same unknown DOM
    # must not keep postponing its sole permitted recovery scroll.
    assert browser.recovery_delay() == 0
    assert browser.observe() == []
    browser.scroll_next()
    assert broker.lease.calls.count('scroll') == 1
    before = list(broker.lease.calls)
    for _ in range(5):
        assert browser.observe() == []
        browser.scroll_next()
    assert broker.lease.calls == before  # bounded, no busy recovery loop
    clock.advance(2)
    assert [p['post_id'] for p in browser.observe()] == ['threads:Code_1']
    assert browser.recovery_status() is None and reasons == []
    assert broker.attaches == 1 and broker.lease.closed == 0
    browser.close()


@pytest.mark.parametrize('stop', ['gate', 'ownership', 'login', 'navigation', 'limit'])
def test_embedded_due_recovery_scroll_rechecks_current_authority(tmp_path, stop):
    from test_session import Clock
    clock = Clock()
    gate = [True]
    browser, broker, reasons = opened(tmp_path, lambda: gate[0])
    browser._monotonic = clock.monotonic
    broker.lease.frames = [snapshot(state='unknown_layout', cards=[])]
    assert browser.observe() == []
    clock.advance(1)
    assert browser.recovery_delay() == 0
    if stop == 'gate': gate[0] = False
    elif stop == 'ownership': broker.lease.status = 'ownership_lost'
    else:
        state = {'login': 'manual_login_or_challenge', 'navigation': 'unexpected_navigation', 'limit': 'snapshot_limit'}[stop]
        broker.lease.frames = [snapshot(state=state, cards=[])]
    browser.scroll_next()
    assert 'scroll' not in broker.lease.calls
    assert broker.attaches == 1
    if stop != 'gate': assert len(reasons) == 1
    browser.close()


@pytest.mark.parametrize('stop', ['gate','ownership','login','navigation','limit','invalid'])
def test_embedded_recovery_cannot_rearm_after_a_hard_stop(tmp_path, stop):
    from test_session import Clock
    clock = Clock()
    gate = [True]
    browser, broker, reasons = opened(tmp_path,lambda:gate[0])
    browser._monotonic = clock.monotonic
    broker.lease.frames = [snapshot(state='unknown_layout',cards=[])]
    assert browser.observe() == [] and reasons == []
    clock.advance(1)
    if stop == 'gate': gate[0] = False
    elif stop == 'ownership': broker.lease.status = 'ownership_lost'
    elif stop == 'login': broker.lease.frames = [snapshot(state='manual_login_or_challenge',cards=[])]
    elif stop == 'navigation': broker.lease.frames = [snapshot(state='unexpected_navigation',cards=[])]
    elif stop == 'limit': broker.lease.frames = [snapshot(state='snapshot_limit',cards=[])]
    else: broker.lease.frames = [{'not':'a snapshot'}]
    assert browser.observe() == []
    before = list(broker.lease.calls)
    clock.advance(60)
    assert browser.observe() == []
    browser.scroll_next()
    assert broker.lease.calls == before and 'scroll' not in broker.lease.calls
    assert broker.attaches == 1
    if stop != 'gate': assert len(reasons) == 1
    browser.close()


@pytest.mark.parametrize('bad_time', [float('nan'), float('inf'), 99])
def test_embedded_invalid_recovery_clock_fails_closed(tmp_path, bad_time):
    clock = [100.0]
    browser, broker, reasons = opened(tmp_path)
    browser._monotonic = lambda:clock[0]
    broker.lease.frames = [snapshot(state='unknown_layout',cards=[])]
    assert browser.observe() == [] and reasons == []
    clock[0] = bad_time
    assert browser.recovery_delay() == 0
    assert reasons == ['clock_invalid']
    browser.scroll_next()
    assert broker.lease.calls == ['snapshot']
    browser.close()


def test_embedded_layout_recovery_preserves_already_emitted_post_dedup(tmp_path):
    from test_session import Clock
    clock = Clock()
    browser, broker, reasons = opened(tmp_path)
    browser._monotonic = clock.monotonic
    assert len(browser.observe()) == 1
    broker.lease.frames = [snapshot(state='unknown_layout',cards=[])]
    assert browser.observe() == []
    clock.advance(1)
    broker.lease.frames = [snapshot()]
    assert browser.observe() == []
    assert browser.recovery_status() is None and reasons == []
    assert broker.attaches == 1
    browser.close()


def test_embedded_recovery_status_is_pure_even_after_the_retry_deadline(tmp_path):
    from test_session import Clock
    clock = Clock()
    browser, broker, reasons = opened(tmp_path)
    browser._monotonic = clock.monotonic
    assert len(browser.observe()) == 1
    seen = dict(browser._seen)
    broker.lease.frames = [snapshot(cards=[{'issue':'unknown_layout'}])]
    for _ in range(3):
        browser.scroll_next()
    assert browser.observe() == []
    before = (browser._retry_at,browser._unsupported_scrolls,browser._last_recovery_clock,list(broker.lease.calls))
    clock.advance(10)
    for _ in range(5):
        assert browser.recovery_status() == {'reason':'unsupported_cards','attempt':1,'retry_in_seconds':0}
    assert (browser._retry_at,browser._unsupported_scrolls,browser._last_recovery_clock,broker.lease.calls) == before
    assert browser._seen == seen and reasons == []
    assert browser.recovery_delay() == 0
    assert browser._retry_at is None and browser._unsupported_scrolls == 0
    browser.close()


def test_embedded_recovery_status_remains_coherent_during_owner_reset(tmp_path):
    from threading import Event, Thread, current_thread
    from threadrelay.contracts import validate_contract
    entered, release = Event(), Event()
    stamp = [100.0]
    def monotonic():
        if current_thread().name == 'recovery-reader-fixture':
            entered.set()
            assert release.wait(2)
        return stamp[0]
    browser, broker, reasons = opened(tmp_path)
    browser._monotonic = monotonic
    broker.lease.frames = [snapshot(state='unknown_layout',cards=[])]
    assert browser.observe() == []
    stamp[0] = 101.0
    results, errors = [], []
    def read():
        try: results.append(browser.recovery_status())
        except Exception as error: errors.append(type(error).__name__)
    thread = Thread(target=read,name='recovery-reader-fixture')
    thread.start()
    try:
        assert entered.wait(2)
        broker.lease.frames = [snapshot()]
        assert len(browser.observe()) == 1
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive() and errors == []
    assert results == [{'reason':'unknown_layout','attempt':1,'retry_in_seconds':0}]
    validate_contract('RecoverySnapshot',results[0])
    assert browser.recovery_status() is None and reasons == []
    browser.close()


@pytest.mark.parametrize('read_time,delay', [(99,2),(-1000,3),(float('nan'),0)])
def test_embedded_backward_status_clock_does_not_latch_attention_but_owner_still_stops(tmp_path, read_time, delay):
    clock = [100.0]
    browser, broker, reasons = opened(tmp_path)
    browser._monotonic = lambda:clock[0]
    broker.lease.frames = [snapshot(state='unknown_layout',cards=[])]
    assert browser.observe() == []
    clock[0] = read_time
    assert browser.recovery_status() == {'reason':'unknown_layout','attempt':1,'retry_in_seconds':delay}
    assert reasons == [] and browser._last_recovery_clock == 100.0
    assert browser.recovery_delay() == 0
    assert reasons == ['clock_invalid'] and browser.recovery_status() is None
    assert broker.lease.calls == ['snapshot']
    browser.close()
