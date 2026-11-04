"""Held-out metric behavior over the real Classifier and synthetic decision I/O."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from threadrelay.contracts import ProtocolError, validate_contract
from threadrelay.evaluation.dataset import load_dataset, validate_dataset
from threadrelay.evaluation.metrics import summarize
from threadrelay.evaluation.runner import compare_reports, evaluate, main
from threadrelay.inference.decision import Classifier
from threadrelay.inference.runtime import RuntimeLimits

ROOT = Path(__file__).parents[2]


def topic(id="research"):
    return {"id": id, "version": 1, "description": id, "positive_examples": [],
            "negative_examples": [], "exclusions": [], "enabled": True}


def record(id, language="en", expected=("research",), text=None):
    text = text or f"Synthetic held-out statement {id}"
    return {"post": {"post_id": id, "permalink": f"https://www.threads.net/@synthetic/post/{id}",
            "author": "synthetic", "text": text, "content_hash": hashlib.sha256(text.encode()).hexdigest(),
            "visibility": "public", "quality_flags": [], "observed_at": "2026-01-01T00:00:00Z",
            "source_session_id": "synthetic-evaluation", "quote": None},
            "expected_topic_ids": list(expected), "language": language}


def dataset(records=None):
    return {"records": records or [record("a")], "split": "test", "synthetic": True,
            "topics": [topic(), topic("art")]}


class Runtime:
    """Only the slow owned inference boundary is replaced; no oracle labels."""
    limits = RuntimeLimits()

    def __init__(self, scores):
        self.scores = iter(scores)
        self._metadata = {"model_id": "synthetic-decisions", "model_revision": "synthetic-v1",
            "model_sha256": "0" * 64, "quantization": "not_applicable", "runtime_version": "synthetic-v1",
            "runtime_commit": "0" * 40, "context_tokens": 4096, "max_input_bytes": 3072,
            "reading_mode": "synthetic-test-double", "scores_calibrated": False}

    @property
    def metadata(self):
        return deepcopy(self._metadata)

    def decide(self, state, questions):
        assert set(state) == {"post_text", "quote"}
        assert set(questions) == {"research", "art"}
        return next(self.scores)


def test_zero_positives_are_undefined_not_perfect_accuracy():
    report = evaluate(dataset([record("a", expected=())]), Classifier(Runtime([{"research": 0, "art": 0}])))
    metrics = report["metrics"]["per_topic"]["research"]
    assert metrics["sample_count"] == 1
    assert metrics["positive_count"] == metrics["predicted_positive_count"] == 0
    assert metrics["precision"] is metrics["recall"] is None
    assert metrics["precision_95ci"] is metrics["recall_95ci"] is None


def test_partial_abstention_is_not_a_negative_or_an_erased_positive():
    data = dataset([record("a", "zh"), record("b", "en", ()), record("c", "mixed")])
    runtime = Runtime([{"research": 1, "art": .5}, {"research": 1, "art": 0}, {"research": .5, "art": 0}])
    metrics = evaluate(data, Classifier(runtime))["metrics"]
    research = metrics["per_topic"]["research"]
    assert (research["true_positive_count"], research["false_positive_count"], research["false_negative_count"]) == (1, 1, 1)
    assert research["precision"] == research["recall"] == .5
    assert research["abstention_count"] == 1
    assert research["precision_95ci"] == pytest.approx([.0945312057, .9054687943])
    assert metrics["abstention_count"] == 2
    assert set(metrics["language_slices"]) == {"zh", "en", "mixed"}
    assert metrics["language_slices"]["zh"]["per_topic"]["research"]["precision"] == 1
    assert metrics["language_slices"]["mixed"]["per_topic"]["research"]["recall"] == 0
    assert metrics["language_slices"]["en"]["per_topic"]["art"]["sample_count"] == 1


def test_missing_prediction_is_an_abstention_and_missed_gold_label():
    predictions = [{"post_id": "a", "model_version": "synthetic", "topic_results": [], "abstained": False}]
    metrics = summarize(dataset()["records"], predictions, ["research", "art"])
    assert metrics["abstention_count"] == 1
    assert metrics["per_topic"]["research"]["missing_prediction_count"] == 1
    assert metrics["per_topic"]["research"]["false_negative_count"] == 1
    assert metrics["per_topic"]["art"]["true_negative_count"] == 0
    assert metrics["per_topic"]["research"]["precision"] is None
    assert metrics["per_topic"]["research"]["recall"] == 0


def test_latency_is_measured_with_scope_and_other_measurements_are_unavailable():
    ticks = iter([1, 1.01, 2, 2.03, 3, 3.02])
    report = evaluate(dataset([record("a"), record("b"), record("c")]),
        Classifier(Runtime([{"research": 1, "art": 0}] * 3)), clock=lambda: next(ticks))
    validate_contract("EvaluationReport", report)
    performance = report["metrics"]["performance"]
    assert performance["p50_latency_ms"] == pytest.approx(20)
    assert performance["p95_latency_ms"] == pytest.approx(30)
    assert performance["latency_scope"] == "synthetic_classifier_calls_only_not_model_performance"
    assert performance["cold_start_ms"] is performance["peak_memory_bytes"] is None
    assert performance["cold_start_unavailable_reason"]
    assert performance["peak_memory_unavailable_reason"]
    assert report["versions"]["model_sha256"] == "0" * 64
    assert report["versions"]["positive_threshold"] == "0.9"
    assert report["metrics"]["acceptance"]["status"] == "review_only_no_release_authority"
    serialized = json.dumps(report)
    assert "Synthetic held-out statement" not in serialized
    assert report["hardware"]["python_version"]


@pytest.mark.parametrize("mutation,reason", [
    (lambda d: d.update(split="train"), "held_out_test_split_required"),
    (lambda d: d.pop("topics"), "evaluation_topics_and_provenance_required"),
    (lambda d: d.pop("synthetic"), "evaluation_topics_and_provenance_required"),
    (lambda d: d["records"].append(deepcopy(d["records"][0])), "duplicate_evaluation_record"),
    (lambda d: d["records"][0].update(expected_topic_ids=["absent"]), "unknown_expected_topic"),
    (lambda d: d["topics"].append(deepcopy(d["topics"][0])), "duplicate_topic_id"),
    (lambda d: d["topics"][0]["positive_examples"].append(d["records"][0]["post"]["text"]), "topic_example_leaks_test_record"),
    (lambda d: d.update(records=[]), "empty_evaluation_dataset"),
])
def test_invalid_or_leaking_dataset_fails_before_inference(mutation, reason):
    data = dataset()
    mutation(data)
    with pytest.raises(ValueError, match=reason):
        validate_dataset(data)


def test_malformed_truth_is_not_silently_assumed_negative():
    data = dataset()
    del data["records"][0]["expected_topic_ids"]
    with pytest.raises(ProtocolError):
        validate_dataset(data)


def write_jsonl(path, data):
    header = {"format": "threadrelay-evaluation-v1", **{k: data[k] for k in ("split", "topics", "synthetic")}}
    path.write_text("\n".join(json.dumps(item) for item in [header, *data["records"]]) + "\n", encoding="utf-8")


def test_jsonl_loader_preserves_topic_definitions_and_provenance(tmp_path):
    path = tmp_path / "local.jsonl"
    data = dataset()
    write_jsonl(path, data)
    assert load_dataset(path) == data
    validate_contract("Dataset", data)


@pytest.mark.parametrize("content", ['{"format":"threadrelay-evaluation-v1","split":"test","split":"train"}\n', '{"x":NaN}\n', '\xff'])
def test_jsonl_loader_rejects_ambiguous_or_invalid_json_without_echoing_data(tmp_path, content):
    path = tmp_path / "bad.jsonl"
    path.write_bytes(content.encode("latin1"))
    with pytest.raises(ValueError, match="invalid_evaluation_file") as error:
        load_dataset(path)
    assert "NaN" not in str(error.value)


def test_synthetic_fixture_has_languages_and_adversarial_examples():
    data = load_dataset(ROOT / "fixtures/evaluation/synthetic.jsonl")
    assert data["synthetic"] is True
    assert {r["language"] for r in data["records"]} >= {"zh", "en", "mixed"}
    assert any(r["post"]["quote"] for r in data["records"])
    assert len(data["records"]) >= 10


def test_cli_default_writes_honest_not_run_report_without_creating_model(tmp_path, capsys):
    output = tmp_path / "report.json"
    assert main(["--dataset", str(ROOT / "fixtures/evaluation/synthetic.jsonl"), "--output", str(output)]) == 2
    report = json.loads(output.read_text())
    assert report["metrics"]["status"] == "not_run"
    assert report["metrics"]["sample_count"] == 0
    assert report["metrics"]["dataset_sample_count"] >= 10
    assert report["metrics"]["performance"]["p50_latency_ms"] is None
    assert set(report["metrics"]["quantization_comparison"]) == {"Q4_K_M", "Q8_0"}
    assert "not run" in capsys.readouterr().err.lower()


def test_cli_smoke_is_explicit_synthetic_and_cannot_overwrite_inputs(tmp_path):
    path, output = tmp_path / "data.jsonl", tmp_path / "report.json"
    write_jsonl(path, dataset())
    assert main(["--dataset", str(path), "--output", str(output), "--synthetic-smoke"]) == 0
    report = json.loads(output.read_text())
    assert report["metrics"]["status"] == "synthetic_smoke_only"
    assert report["metrics"]["abstention_count"] == 1
    assert report["versions"]["quantization"] == "not_applicable"
    before = path.read_bytes()
    assert main(["--dataset", str(path), "--output", str(path), "--synthetic-smoke"]) == 2
    assert path.read_bytes() == before
    data = dataset()
    data["synthetic"] = False
    write_jsonl(path, data)
    assert main(["--dataset", str(path), "--output", str(tmp_path / "private.json"), "--synthetic-smoke"]) == 2
    assert not (tmp_path / "private.json").exists()


def test_comparison_requires_same_data_topics_policy_and_real_quantizations():
    one = evaluate(dataset(), Classifier(Runtime([{"research": 1, "art": 0}])))
    two = deepcopy(one)
    one["versions"]["quantization"], two["versions"]["quantization"] = "Q4_K_M", "Q8_0"
    with pytest.raises(ValueError, match="real_model_reports_required"):
        compare_reports(one, two)


@pytest.mark.parametrize("change", ["duplicate", "unknown", "wrong_post"])
def test_invalid_prediction_identity_is_rejected_instead_of_scored(change):
    predictions = [{"post_id": "a", "model_version": "synthetic", "topic_results": [
        {"topic_id": "research", "match": True, "abstained": False}], "abstained": False}]
    if change == "duplicate":
        predictions[0]["topic_results"] *= 2
    elif change == "unknown":
        predictions[0]["topic_results"][0]["topic_id"] = "intruder"
    else:
        predictions[0]["post_id"] = "different"
    with pytest.raises(ValueError, match="invalid_evaluation_prediction"):
        summarize(dataset()["records"], predictions, ["research", "art"])


def test_trusted_factory_is_explicit_context_managed_and_captures_cold_preparation(tmp_path):
    from contextlib import contextmanager
    from threadrelay.inference.provision import load_catalog
    pin = load_catalog()
    events = []
    @contextmanager
    def factory(manifest):
        assert manifest["quantization"] == "Q8_0"
        runtime = Runtime([{"research": 1, "art": 0}])
        runtime._metadata.update(model_id=manifest["id"], model_revision=manifest["version"],
            model_sha256=manifest["sha256"], quantization=manifest["quantization"],
            runtime_version=manifest["runtime_version"], runtime_commit=pin["runtime"]["commit"],
            reading_mode="one-way-llama.cpp")
        events.append("opened")
        try:
            yield Classifier(runtime)
        finally:
            events.append("closed")
    path, output = tmp_path / "data.jsonl", tmp_path / "report.json"
    write_jsonl(path, dataset())
    assert main(["--dataset", str(path), "--output", str(output), "--quantization", "Q8_0"], runtime_factory=factory) == 0
    report = json.loads(output.read_text())
    assert events == ["opened", "closed"]
    assert report["metrics"]["status"] == "synthetic_dataset_model_evaluation"
    assert report["metrics"]["performance"]["cold_start_ms"] >= 0
    assert report["metrics"]["performance"]["cold_start_scope"] == "trusted_cold_factory_verification_load_and_canary"
    assert report["versions"]["model_sha256"] == pin["models"][1]["manifest"]["sha256"]


def test_factory_identity_mismatch_is_not_recorded_as_real_inference(tmp_path):
    from contextlib import contextmanager
    @contextmanager
    def factory(manifest):
        yield Classifier(Runtime([{"research": 1, "art": 0}]))
    path, output = tmp_path / "data.jsonl", tmp_path / "report.json"
    write_jsonl(path, dataset())
    assert main(["--dataset", str(path), "--output", str(output)], runtime_factory=factory) == 2
    assert json.loads(output.read_text())["metrics"]["status"] == "not_run"


def test_comparison_keeps_each_topic_language_and_refuses_mismatched_policy():
    from threadrelay.inference.provision import load_catalog
    data = dataset()
    data["synthetic"] = False
    reports = []
    for entry in load_catalog()["models"]:
        runtime = Runtime([{"research": 1, "art": 0}])
        manifest = entry["manifest"]
        runtime._metadata.update(model_id=manifest["id"], model_revision=manifest["version"],
            model_sha256=manifest["sha256"], quantization=manifest["quantization"],
            runtime_version=manifest["runtime_version"], runtime_commit=load_catalog()["runtime"]["commit"],
            reading_mode="one-way-llama.cpp")
        reports.append(evaluate(data, Classifier(runtime)))
    compared = compare_reports(*reports)
    assert set(compared["metrics"]["quantization_comparison"]) == {"Q4_K_M", "Q8_0"}
    assert compared["metrics"]["quantization_comparison"]["Q8_0"]["language_slices"]["en"]["per_topic"]["art"]["sample_count"] == 1
    reports[1]["versions"]["positive_threshold"] = "0.99"
    with pytest.raises(ValueError, match="incomparable_evaluation_reports"):
        compare_reports(*reports)


def test_metadata_change_during_run_invalidates_provenance():
    class Changed(Runtime):
        def decide(self, state, questions):
            self._metadata["model_revision"] = "changed"
            return super().decide(state, questions)
    with pytest.raises(ValueError, match="evaluation_provenance_changed"):
        evaluate(dataset(), Classifier(Changed([{"research": 1, "art": 0}])))


def native_runtime(scores):
    from threadrelay.inference.provision import load_catalog
    catalog = load_catalog()
    manifest = catalog["models"][0]["manifest"]
    runtime = Runtime(scores)
    runtime._metadata.update(model_id=manifest["id"], model_revision=manifest["version"],
        model_sha256=manifest["sha256"], quantization=manifest["quantization"],
        runtime_version=manifest["runtime_version"], runtime_commit=catalog["runtime"]["commit"],
        reading_mode="one-way-llama.cpp")
    return runtime


def test_no_successful_model_call_is_not_a_real_benchmark():
    from threadrelay.inference.runtime import RuntimeUnavailable
    runtime = native_runtime([])
    def unavailable(state, questions):
        raise RuntimeUnavailable("synthetic-outage")
    runtime.decide = unavailable
    report = evaluate(dataset(), Classifier(runtime))
    assert report["metrics"]["status"] == "no_model_inference_confirmed"
    assert report["metrics"]["confirmed_model_inference_count"] == 0
    assert report["metrics"]["classifier_outcome_counts"] == {"inference_unavailable": 1}
    assert report["metrics"]["sample_count"] == 1
    assert report["metrics"]["abstention_count"] == 1
    assert report["metrics"]["performance"]["latency_sample_count"] == 1


def test_skipped_extraction_is_counted_separately_from_successful_inference():
    data = dataset([record("a"), record("b")])
    data["records"][1]["post"]["quality_flags"] = [{"code": "missing", "detail": "Synthetic incomplete extraction"}]
    report = evaluate(data, Classifier(native_runtime([{"research": 1, "art": 0}])))
    assert report["metrics"]["confirmed_model_inference_count"] == 1
    assert report["metrics"]["classifier_outcome_counts"] == {"decided": 1, "extraction_uncertainty": 1}


def test_unexpected_classification_model_version_cannot_corrupt_report_provenance():
    class Misidentified(Classifier):
        def classify(self, post, topics):
            result = super().classify(post, topics)
            result["model_version"] = "different-model"
            return result
    with pytest.raises(ValueError, match="classification_model_version_mismatch"):
        evaluate(dataset(), Misidentified(Runtime([{"research": 1, "art": 0}])))


def test_unrecognized_metadata_is_neither_exported_nor_allowed_to_replace_dataset_digest():
    runtime = Runtime([{"research": 1, "art": 0}])
    runtime._metadata.update(dataset_sha256="wrong", endpoint_token="must-not-be-exported")
    report = evaluate(dataset(), Classifier(runtime))
    assert len(report["versions"]["dataset_sha256"]) == 64
    assert "must-not-be-exported" not in json.dumps(report)


def test_quote_example_leakage_and_whitespace_duplicates_are_rejected():
    data = dataset()
    data["records"][0]["post"]["quote"] = {"author": "synthetic", "text": "copied example", "permalink": None}
    data["topics"][0]["negative_examples"] = ["copied example"]
    with pytest.raises(ValueError, match="topic_example_leaks_test_record"):
        validate_dataset(data)
    data = dataset([record("a", text="same statement"), record("b", text="same   statement")])
    with pytest.raises(ValueError, match="duplicate_evaluation_record"):
        validate_dataset(data)


def test_not_run_report_still_preserves_known_dataset_slice_counts(tmp_path):
    path, output = tmp_path / "data.jsonl", tmp_path / "report.json"
    write_jsonl(path, dataset([record("a", "zh"), record("b", "mixed", ())]))
    assert main(["--dataset", str(path), "--output", str(output)]) == 2
    counts = json.loads(output.read_text())["metrics"]["dataset_counts"]
    assert counts["per_topic"]["research"] == {"sample_count": 2, "positive_count": 1}
    assert counts["language_slices"]["zh"]["per_topic"]["art"] == {"sample_count": 1, "positive_count": 0}
    assert counts["language_slices"]["mixed"]["sample_count"] == 1


def paired_reports():
    from threadrelay.inference.provision import load_catalog
    data = dataset()
    data["synthetic"] = False
    reports = []
    for entry in load_catalog()["models"]:
        runtime = native_runtime([{"research": 1, "art": 0}])
        manifest = entry["manifest"]
        runtime._metadata.update(model_id=manifest["id"], model_revision=manifest["version"],
            model_sha256=manifest["sha256"], quantization=manifest["quantization"])
        reports.append(evaluate(data, Classifier(runtime)))
    return reports


def test_comparison_retains_each_models_complete_provenance():
    q4, q8 = paired_reports()
    comparison = compare_reports(q4, q8)
    assert comparison["metrics"]["comparison_versions"]["Q4_K_M"] == q4["versions"]
    assert comparison["metrics"]["comparison_versions"]["Q8_0"] == q8["versions"]


def test_comparison_rejects_incomplete_or_different_sample_counts():
    q4, q8 = paired_reports()
    q8["metrics"]["sample_count"] = 0
    # Malformed individual evidence now fails before pairwise comparability.
    with pytest.raises(ValueError, match="invalid_evaluation_evidence"):
        compare_reports(q4, q8)
