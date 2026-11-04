"""Synthetic lifecycle tests; no browser, model, account or network transport."""
from datetime import datetime, timedelta, timezone
from threading import Event, Thread

import pytest

from threadrelay.contracts import ProtocolError, validate_contract
from threadrelay.session import ParentWatchdog, SessionController


class Clock:
    def __init__(self):
        self.elapsed = 100.0
        self.utc = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def monotonic(self):
        return self.elapsed

    def now(self):
        return self.utc

    def advance(self, seconds):
        self.elapsed += seconds
        self.utc += timedelta(seconds=seconds)


def configured(clock=None, **kwargs):
    return SessionController(clock=clock or Clock(), setup_check=lambda config: {"allowed": True, "reason": None}, **kwargs)


def config(**kwargs):
    return {"feed_url": "https://www.threads.com/", **kwargs}


def test_open_and_default_setup_are_fail_closed():
    clock = Clock()
    controller = SessionController(clock=clock)
    assert controller.snapshot == {"state": "stopped", "counters": {"observed": 0, "matched": 0, "queued": 0, "sent": 0}, "attention_reason": None}
    assert not controller.can_transmit(clock.now())
    assert controller.start(config())["state"] == "needs_attention"
    assert not controller.can_transmit(clock.now())


def test_every_nonrunning_state_denies_intake_and_transmission():
    clock = Clock()
    seen = []
    controller = None

    def observe():
        seen.append(controller.snapshot["state"])
        assert not controller.can_transmit(clock.now())
        assert not controller.can_intake()

    def setup(_):
        observe()
        return {"allowed": True, "reason": None}

    controller = SessionController(clock=clock, setup_check=setup, cancel_intake=observe)
    observe()  # stopped
    assert controller.start(config())["state"] == "running"
    assert controller.can_transmit(clock.now())
    controller.pause()
    observe()  # paused
    controller.stop()
    controller.require_attention("synthetic_issue")
    observe()
    controller.fail("synthetic_failure")
    observe()
    assert set(seen) == {"stopped", "starting", "pausing", "paused", "stopping", "needs_attention", "failed"}


def test_defaults_and_exact_30_minute_monotonic_deadline():
    clock = Clock()
    controller = configured(clock)
    controller.start(config())
    clock.utc -= timedelta(days=10)  # NTP/wall-clock rollback cannot extend the session.
    clock.elapsed += 1799.999
    assert controller.can_intake()
    clock.elapsed += .001
    assert not controller.can_transmit(clock.now())
    assert not controller.can_intake()
    assert controller.snapshot["state"] == "paused"
    assert controller.snapshot["attention_reason"] == "duration_limit"


def test_hundredth_post_closes_intake_and_send_gate_without_discarding_queue_count():
    clock = Clock()
    controller = configured(clock)
    controller.start(config())
    controller.record_counter("queued", 3)
    for _ in range(99):
        assert controller.observe_post()
    assert controller.can_transmit(clock.now())
    assert controller.observe_post()
    assert not controller.observe_post()
    assert controller.snapshot["counters"] == {"observed": 100, "matched": 0, "queued": 3, "sent": 0}
    assert not controller.can_transmit(clock.now())
    assert controller.snapshot["attention_reason"] == "post_limit"


def test_tightened_limits_and_snapshot_mutation_cannot_change_authority():
    clock = Clock()
    controller = configured(clock)
    controller.start(config(duration_seconds=5, max_posts=2))
    snapshot = controller.snapshot
    snapshot["state"] = "stopped"
    snapshot["counters"]["observed"] = 999
    assert controller.observe_post()
    assert controller.observe_post()
    assert not controller.can_transmit(clock.now())
    for bad in [config(max_posts=101), config(duration_seconds=0), config(mode="invented")]:
        with pytest.raises(ProtocolError):
            controller.start(bad)
    assert controller.snapshot["counters"]["observed"] == 2


def test_dry_run_never_transmits_even_while_running():
    clock = Clock()
    controller = configured(clock)
    assert controller.start(config(mode="dry_run"))["state"] == "running"
    assert controller.can_intake()
    assert not controller.can_transmit(clock.now())
    assert controller.begin_send(clock.now(), automatic=False, authorize=lambda: True, start=lambda: pytest.fail("transmitted")) is None


def test_automatic_budget_reserves_at_transmission_including_unknown_outcomes():
    clock = Clock()
    controller = configured(clock)
    controller.start(config(mode="auto", max_auto_deliveries=2))
    starts = []
    for _ in range(2):
        assert controller.begin_send(clock.now(), automatic=True, authorize=lambda: True, start=lambda: starts.append("unknown") or "future") == "future"
    assert controller.begin_send(clock.now(), automatic=True, authorize=lambda: True, start=lambda: starts.append("unexpected")) is None
    assert starts == ["unknown", "unknown"]
    assert controller.snapshot["attention_reason"] == "automatic_delivery_limit"
    assert controller.snapshot["counters"]["sent"] == 0  # reservation is not successful delivery.


def test_auto_cannot_be_requested_in_review_and_zero_auto_budget_is_closed():
    clock = Clock()
    controller = configured(clock)
    controller.start(config())
    assert controller.begin_send(clock.now(), automatic=True, authorize=lambda: True, start=lambda: pytest.fail("transmitted")) is None
    controller.stop()
    controller.start(config(mode="auto", max_auto_deliveries=0))
    assert not controller.can_transmit(clock.now())


def test_pause_closes_gate_before_cancel_and_does_not_relaunch_browser():
    clock = Clock()
    cancellations = []
    controller = configured(clock, cancel_intake=lambda: cancellations.append("cancel"))
    controller.start(config())
    controller.browser_lost()
    assert controller.snapshot["state"] == "paused"
    assert controller.snapshot["attention_reason"] == "browser_lost"
    assert not controller.can_intake()
    assert not controller.can_transmit(clock.now())
    controller.tick()
    assert cancellations == ["cancel"]
    assert controller.snapshot["state"] == "paused"


def test_pause_and_send_start_are_serialized_and_inflight_result_can_finish():
    clock = Clock()
    controller = configured(clock)
    controller.start(config())
    in_start, release, pause_attempted, pause_done = Event(), Event(), Event(), Event()
    results = []

    def start():
        in_start.set()
        assert release.wait(2)
        return "inflight"

    sending = Thread(target=lambda: results.append(controller.begin_send(clock.now(), automatic=False, authorize=lambda: True, start=start)))
    sending.start()
    assert in_start.wait(2)

    def pause():
        pause_attempted.set()
        controller.pause()
        pause_done.set()

    pausing = Thread(target=pause)
    pausing.start()
    assert pause_attempted.wait(2)
    assert not pause_done.is_set()
    release.set()
    sending.join(2)
    pausing.join(2)
    assert not sending.is_alive() and not pausing.is_alive()
    assert results == ["inflight"]
    assert pause_done.is_set()
    assert controller.begin_send(clock.now(), automatic=False, authorize=lambda: True, start=lambda: pytest.fail("late send")) is None


def test_final_authorization_runs_under_gate_and_reentrant_revocation_cannot_send():
    clock = Clock()
    controller = configured(clock)
    controller.start(config())
    assert controller.begin_send(clock.now(), automatic=False, authorize=lambda: False, start=lambda: pytest.fail("unauthorized")) is None

    def revoke():
        controller.pause()
        return True

    assert controller.begin_send(clock.now(), automatic=False, authorize=revoke, start=lambda: pytest.fail("revoked")) is None


def test_stop_sleep_wake_and_recovery_never_resume():
    clock = Clock()
    controller = configured(clock)
    controller.start(config())
    assert controller.stop()["state"] == "stopped"
    assert not controller.can_transmit(clock.now())
    controller.start(config())
    controller.system_sleep()
    assert not controller.can_intake()
    controller.system_wake()
    assert controller.snapshot["state"] == "needs_attention"
    controller.tick()
    assert not controller.can_transmit(clock.now())
    recovered = configured(clock, recovered=True)
    assert recovered.snapshot["state"] == "needs_attention"
    assert not recovered.can_transmit(clock.now())


def test_parent_loss_stops_once_and_never_restarts():
    clock = Clock()
    controller = configured(clock)
    controller.start(config())
    alive = [True]
    cleanup = []
    watchdog = ParentWatchdog(controller, parent_alive=lambda: alive[0], shutdown=lambda: cleanup.append("owned cleanup"))
    assert watchdog.poll()
    alive[0] = False
    assert not watchdog.poll()
    assert controller.snapshot["state"] == "stopped"
    assert not controller.can_transmit(clock.now())
    alive[0] = True
    assert not watchdog.poll()
    assert cleanup == ["owned cleanup"]


def test_bad_clocks_and_counters_fail_closed_and_snapshots_match_contract():
    clock = Clock()
    controller = configured(clock)
    controller.start(config())
    with pytest.raises(ValueError):
        controller.can_transmit(datetime(2026, 1, 1))
    for name, count in [("observed", 1), ("sent", -1), ("queued", True), ("unknown", 1)]:
        with pytest.raises(ValueError):
            controller.record_counter(name, count)
    validate_contract("SessionSnapshot", controller.snapshot)
    clock.elapsed = 1  # A broken monotonic source must not extend authority.
    assert not controller.can_transmit(clock.now())
    assert controller.snapshot["state"] == "needs_attention"


def test_parent_probe_failure_is_terminal_and_shutdown_is_once_even_if_it_raises():
    clock = Clock()
    controller = configured(clock)
    controller.start(config())
    cleanup = []

    def probe():
        raise OSError("synthetic inaccessible parent")

    def shutdown():
        cleanup.append(True)
        raise OSError("synthetic cleanup error")

    watchdog = ParentWatchdog(controller, parent_alive=probe, shutdown=shutdown)
    with pytest.raises(OSError):
        watchdog.poll()
    assert controller.snapshot["state"] == "stopped"
    assert not watchdog.poll()
    assert cleanup == [True]


def test_private_parent_probe_tracks_parent_identity_not_wall_time():
    from threadrelay.session import ParentProcess

    with ParentProcess() as parent:
        assert parent.alive()
    # A closed probe cannot authorize continued operation.
    assert not parent.alive()


def test_setup_and_cancellation_failures_close_all_gates():
    clock = Clock()

    def broken(_):
        raise RuntimeError("synthetic setup failure")

    controller = SessionController(clock=clock, setup_check=broken)
    assert controller.start(config())["state"] == "failed"
    assert not controller.can_transmit(clock.now())
    controller = SessionController(clock=clock, setup_check=lambda _: {"allowed": "yes"})
    assert controller.start(config())["state"] == "failed"

    def cancellation_failure():
        raise RuntimeError("synthetic cancellation failure")

    controller = configured(clock, cancel_intake=cancellation_failure)
    controller.start(config())
    assert controller.stop()["state"] == "failed"
    assert not controller.can_transmit(clock.now())


def test_ipc_pause_stop_query_use_session_authority_but_start_stays_setup_gated():
    from threadrelay.main import dispatch
    clock = Clock()
    controller = configured(clock)
    controller.start(config())

    def command(kind, payload=None):
        return {"version": 1, "id": "lifecycle", "type": kind, "payload": payload or {}}

    assert dispatch(command("QueryState"), controller=controller)["payload"]["state"] == "running"
    assert dispatch(command("Pause"), controller=controller)["payload"]["state"] == "paused"
    assert dispatch(command("Stop"), controller=controller)["payload"]["state"] == "stopped"
    assert dispatch(command("Start", config()), controller=controller)["type"] == "Error"
    assert controller.snapshot["state"] == "stopped"


@pytest.mark.skipif(not hasattr(__import__("os"), "pidfd_open"), reason="Linux stable process-handle integration evidence only")
def test_worker_parent_watchdog_exits_even_when_private_ipc_pipe_stays_open():
    import os
    import select
    import signal
    import subprocess
    import sys

    read_fd, write_fd = os.pipe()
    helper = None
    child_fd = None
    try:
        helper = subprocess.Popen(
            [sys.executable, "-c", '''import os, subprocess, sys
worker = subprocess.Popen([sys.executable, "-m", "threadrelay.main"], stdin=int(sys.argv[1]), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(worker.pid, flush=True)
sys.stdin.buffer.read(1)
os._exit(0)
''', str(read_fd)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            pass_fds=(read_fd,), text=True,
        )
        pid = int(helper.stdout.readline())
        child_fd = os.pidfd_open(pid)
        helper.stdin.write("x")
        helper.stdin.flush()
        assert helper.wait(timeout=2) == 0
        # write_fd is deliberately still open, so existing EOF handling alone
        # cannot terminate the worker. Kernel readiness replaces test sleeps.
        assert select.select([child_fd], [], [], 2)[0] == [child_fd]
    finally:
        if child_fd is not None:
            if not select.select([child_fd], [], [], 0)[0]:
                signal.pidfd_send_signal(child_fd, signal.SIGKILL)
            os.close(child_fd)
        if helper is not None and helper.poll() is None:
            helper.kill()
            helper.wait(timeout=2)
        os.close(read_fd)
        os.close(write_fd)


def test_transmission_exception_still_consumes_automatic_budget():
    clock = Clock()
    controller = configured(clock)
    controller.start(config(mode="auto", max_auto_deliveries=1))

    def unknown_after_initiation():
        raise OSError("synthetic response lost")

    with pytest.raises(OSError):
        controller.begin_send(clock.now(), automatic=True, authorize=lambda: True, start=unknown_after_initiation)
    assert not controller.can_transmit(clock.now())
    assert controller.snapshot["attention_reason"] == "automatic_delivery_limit"
    assert controller.snapshot["counters"]["sent"] == 0


def test_invalid_clock_seen_under_authorization_guard_stays_revoked_after_release():
    clock = Clock()
    controller = configured(clock)
    controller.start(config())
    previous = clock.elapsed
    releases = []
    def authorize():
        clock.elapsed -= 1
        return True
    def release():
        releases.append("closed")
        clock.elapsed = previous
    assert controller.begin_send(clock.now(), automatic=False, authorize=authorize,
        start=lambda: pytest.fail("invalid clock started transmission"), release_authorization=release) is None
    assert releases == ["closed"]
    assert controller.snapshot["state"] == "needs_attention"
    assert controller.snapshot["attention_reason"] == "clock_invalid"


def test_projection_reports_only_config_of_promoted_generation():
    controller = configured()
    selected = config(mode='dry_run', duration_seconds=30, max_posts=2)
    snapshot = controller.start(selected)
    assert snapshot['config'] == {**selected, 'max_auto_deliveries': 10}
    controller.stop()
    assert controller.snapshot['config']['mode'] == 'dry_run'
    token = controller.begin_start(config(mode='review'))
    assert token is not None and 'config' not in controller.snapshot
    controller.pause()
    assert 'config' not in controller.complete_start(token)


def test_reentrant_pause_and_new_reservation_cannot_promote_old_start():
    controller = None
    nested = []
    first = [True]
    def setup(_):
        if first[0]:
            first[0] = False
            controller.pause()
            nested.append(controller.begin_start(config(mode='dry_run')))
        return {'allowed': True, 'reason': None}
    controller = SessionController(setup_check=setup)
    old = controller.begin_start(config(mode='review'))
    assert old is None
    assert nested[0] is not None
    assert controller.complete_start(nested[0])['config']['mode'] == 'dry_run'


def test_explicit_unlimited_auto_deliveries_does_not_stop_at_legacy_ten():
    clock=Clock();controller=configured(clock)
    controller.start(config(mode='auto',continuous=True,unlimited_auto_deliveries=True,max_auto_deliveries=0))
    for _ in range(40):
        assert controller.begin_send(clock.now(),automatic=True,authorize=lambda:True,start=lambda:'started')=='started'
    assert controller.snapshot['state']=='running' and controller.snapshot['attention_reason'] is None
    controller.pause()
    assert controller.begin_send(clock.now(),automatic=True,authorize=lambda:True,start=lambda:'unexpected') is None


def test_higher_finite_auto_budget_is_supported_and_still_enforced():
    clock=Clock();controller=configured(clock)
    controller.start(config(mode='auto',continuous=True,max_auto_deliveries=25))
    for _ in range(25):
        assert controller.begin_send(clock.now(),automatic=True,authorize=lambda:True,start=lambda:True)
    assert not controller.can_transmit(clock.now(),automatic=True)
    assert controller.snapshot['attention_reason']=='automatic_delivery_limit'
