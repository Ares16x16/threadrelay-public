"""Real local activity observations never activate services or block revocation."""
from threading import Event, Thread
from types import SimpleNamespace

from threadrelay.application import Application
from threadrelay.main import dispatch
from threadrelay.pipeline import PreparedSession
from threadrelay.session import SessionController


def test_activity_query_is_read_only_and_has_current_observed_status(tmp_path):
    with Application(tmp_path) as app:
        result = dispatch({'version':1,'id':'activity','type':'QueryActivity','payload':{'limit':50}}, features=app.features)
        assert result['type'] == 'Activity'
        assert result['payload']['recording_ok'] is True
        assert result['payload']['session']['state'] == 'stopped'
        assert result['payload']['model']['state'] == 'unavailable'
        assert app.telegram._client is None and app.runtime._lease is None
        assert not (tmp_path/'telegram').exists()


def test_owner_test_queries_and_rejected_unauthenticated_send_never_activate_accounts(tmp_path):
    with Application(tmp_path) as app:
        def request(kind, payload):
            return dispatch({'version':1,'id':'owner-test','type':kind,'payload':payload},features=app.features)
        result = request('QueryTelegramTest', {})
        assert result['type'] == 'TelegramTestState' and result['payload']['state'] == 'idle'
        assert request('SendTelegramTest', {'chat_id':'-7','account_id':'7','account_epoch':0})['type'] == 'Error'
        assert app.telegram._client is None and not (tmp_path/'telegram').exists()


def test_session_observation_failure_cannot_prevent_stop_or_mutate_state():
    observed = []
    def observer(snapshot):
        observed.append(snapshot['state'])
        snapshot['state'] = 'running'
        raise RuntimeError('observation failed')
    session = SessionController(on_transition=observer)
    assert session.stop()['state'] == 'stopped'
    assert observed == ['stopping','stopped']


def test_activity_writer_contention_never_delays_revocation(tmp_path):
    with Application(tmp_path) as app:
        revoked = Event()
        # Observe revocation before the existing durable workflow pause hook
        # (which itself legitimately waits for its writer after closing gates).
        app.session._gate_close_hooks.insert(0, revoked.set)
        with app.db.connection() as writer:
            writer.execute('BEGIN IMMEDIATE')
            transition = Thread(target=lambda:app.session.require_attention('browser_lost'))
            transition.start()
            try:
                gate_closed = revoked.wait(.25)
            finally:
                writer.rollback()
                transition.join(2)
        assert gate_closed
        assert app.query_activity()['recording_ok'] is False
        assert app.session.stop()['state'] == 'stopped'


def test_prepared_session_reports_bounded_reading_and_skipped_cards():
    events = []
    cancelled = Event()
    clock = SimpleNamespace(monotonic=lambda:0)
    class Browser:
        issues = [{'code':'unknown_layout'}]
        scans = 0
        def observe(self):
            self.scans += 1
            return []
        def scroll_next(self):
            if self.scans == 2: cancelled.set()
    browser = Browser()
    pipeline = SimpleNamespace(on_activity=lambda kind, **values:events.append((kind,values)))
    session = SimpleNamespace(can_intake=lambda:not cancelled.is_set(), _clock=clock)
    PreparedSession(browser, None, pipeline, session).run(cancelled)
    assert [event[0] for event in events] == ['reading','skipped']
    assert events[1][1]['detail'] == 'unknown_layout'
