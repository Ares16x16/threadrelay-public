"""Removing DTO closure, provenance, UTC, caps, or defaults must break these fixtures."""
import json
from importlib.resources import files
from pathlib import Path

import pytest
from threadrelay.contracts import ProtocolError, validate_contract

FIXTURES = json.loads((Path(__file__).parents[2] / "contracts/fixtures.json").read_text())


@pytest.mark.parametrize("case", FIXTURES["valid"], ids=lambda case: case["name"])
def test_accepts_shared_valid_contracts(case):
    validate_contract(case["contract"], case["value"])


@pytest.mark.parametrize("case", FIXTURES["invalid"], ids=lambda case: case["name"])
def test_rejects_shared_invalid_contracts(case):
    with pytest.raises(ProtocolError):
        validate_contract(case["contract"], case["value"])


def test_packaged_schema_includes_every_shared_dto():
    schema = json.loads(files("threadrelay").joinpath("protocol.schema.json").read_text())
    expected = {
        "ObservedPost", "Topic", "Classification", "Route", "Approval", "Delivery",
        "SessionConfig", "SessionSnapshot", "PostIdentity", "ExtractionIssue", "DeliveryProposal",
        "AuthorizationResult", "ModelManifest", "VerificationResult", "ShutdownReport",
        "PruneReport", "RecoverySummary", "DeliveryEvent", "Dataset", "EvaluationReport",
        "Command", "Event",
    }
    assert expected <= schema["definitions"].keys()


def test_defaults_are_explicit_not_permission_to_transmit():
    schema = json.loads(files("threadrelay").joinpath("protocol.schema.json").read_text())
    config = schema["definitions"]["SessionConfig"]["properties"]
    assert {key: value.get("default") for key, value in config.items() if key not in {"feed_url", "route_ids"}} == {
        "mode": "review", "duration_seconds": 1800, "max_posts": 100, "max_auto_deliveries": 10, "continuous": False, "unlimited_auto_deliveries": False,
    }
    assert "default" not in config["route_ids"]
    assert config["route_ids"]["maxItems"] == 1
    for value in schema["definitions"]["SessionCounters"]["properties"].values():
        assert value["default"] == 0


def test_destination_failure_diagnostics_are_bounded_and_optional():
    page = {'state':'failed','account_id':None,'account_epoch':None,'items':[],
            'next_cursor':None,'can_load_more':False,'exhausted':False,'capped':False,
            'blocked_reason':None}
    validate_contract('TelegramDestinations', page)
    validate_contract('TelegramDestinations', {**page,'failure_stage':'load','failure_reason':'timeout'})
    validate_contract('TelegramDestinations', {**page,'failure_stage':None,'failure_reason':None})
    for field in ('failure_stage','failure_reason'):
        with pytest.raises(ProtocolError):
            validate_contract('TelegramDestinations', {**page,field:'raw native error or private content'})
