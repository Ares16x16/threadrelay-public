"""Recovery/failure matrix entrypoint. Synthetic source checks, not native proof.

Re-export focused existing real-implementation tests instead of inventing a
parallel recovery implementation. Additional cases below exercise composition.
"""

from test_storage import (
    test_actual_process_crash_after_claim_recovers_one_unknown_without_replay,
    test_failed_migration_rolls_back_ddl_data_and_version,
)
from test_session import test_stop_sleep_wake_and_recovery_never_resume
from test_telegram_auth import (
    test_restart_retains_pending_server_revocation_in_inert_account_snapshot,
    test_ready_requires_regular_user_and_logout_revokes_epoch,
    test_queries_are_inert_and_start_requires_no_pending,
)
from test_delivery import (
    test_correlated_response_is_not_success_and_success_update_wins_out_of_order,
    test_stopped_session_does_not_claim_pending_delivery,
)
import pytest
from threadrelay.application import Application
from threadrelay.main import dispatch

# Explicit fixture/test exports are pytest collection, not unused dead imports.
__all__ = [
    "test_actual_process_crash_after_claim_recovers_one_unknown_without_replay",
    "test_failed_migration_rolls_back_ddl_data_and_version",
    "test_stop_sleep_wake_and_recovery_never_resume",
    "test_restart_retains_pending_server_revocation_in_inert_account_snapshot",
    "test_ready_requires_regular_user_and_logout_revokes_epoch",
    "test_queries_are_inert_and_start_requires_no_pending",
    "test_correlated_response_is_not_success_and_success_update_wins_out_of_order",
    "test_stopped_session_does_not_claim_pending_delivery",
]


@pytest.mark.parametrize(
    "kind",
    [
        "QueryState",
        "QueryAccount",
        "QueryTopics",
        "QueryRoutes",
        "QueryHistory",
        "QueryReview",
    ],
)
def test_reopen_with_missing_all_native_dependencies_never_constructs_them(
    tmp_path, kind
):
    for _ in range(2):
        with Application(tmp_path) as app:
            event = dispatch(
                {"version": 1, "id": "recovery", "type": kind, "payload": {}},
                controller=app.session,
                features=app.features,
                coordinator=app.coordinator,
            )
            assert event["type"] != "Error"
            assert app.session.snapshot["state"] == "stopped"
            assert app.telegram._binding is None
            assert app.runtime._lease is None


def test_partial_model_download_is_not_an_installed_model_on_reopen(tmp_path):
    from threadrelay.inference.provision import ModelProvisioner

    provision = ModelProvisioner()
    model = provision.primary_manifest
    path = tmp_path / "interrupted.gguf.part"
    path.write_bytes(b"GGUF\x03\0\0\0synthetic incomplete")
    assert provision.verify(model, path)["valid"] is False
    with Application(tmp_path / "app") as app:
        assert app.session.snapshot["state"] == "stopped"
        assert app.runtime._lease is None


def test_failed_native_login_keeps_local_database_available(tmp_path):
    with Application(tmp_path) as app:
        event = dispatch(
            {"version": 1, "id": "login", "type": "BeginTelegramLogin", "payload": {}},
            controller=app.session,
            features=app.features,
            coordinator=app.coordinator,
        )
        assert event["type"] == "Error"
        assert app.telegram._binding is None
        assert app.features.topics() == []
        assert app.session.snapshot["state"] == "stopped"
