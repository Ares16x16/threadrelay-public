"""Review regressions: scoped observations and complete/comparable evidence."""
from contextlib import contextmanager
import json

import pytest

from threadrelay.evaluation.memory import MemoryObservation, MemoryScope, ScopedMemorySampler
from threadrelay.evaluation.runner import compare_reports, evaluate, main
from threadrelay.inference.decision import Classifier
from test_evaluation import dataset, native_runtime, paired_reports, record, write_jsonl


class ManualScheduler:
    """Injected sampling cadence; no wall-clock sleeps or native processes."""
    def start(self, callback, interval_seconds):
        self.callback = callback
        self.stopped = False
        callback()
        return self

    def tick(self):
        self.callback()

    def stop(self):
        self.stopped = True
        return True


def sampler(observations, *, scope=None, scheduler=None, window="classification_calls"):
    values = iter(observations)
    return ScopedMemorySampler(lambda: next(values), scope=scope or MemoryScope(True, True, "cpu_only"),
        scheduler=scheduler or ManualScheduler(), window=window)


def test_scoped_memory_uses_simultaneous_sum_not_sum_of_component_peaks():
    schedule = ManualScheduler()
    memory = sampler([MemoryObservation(100, 200), MemoryObservation(250, 25)], scheduler=schedule)
    with memory:
        schedule.tick()
    result = memory.result()
    assert result["peak_memory_bytes"] == 300
    assert result["peak_memory_unavailable_reason"] is None
    details = result["memory_measurement"]
    assert details["method"] == "sampled_simultaneous_process_RSS_peak_lower_bound"
    assert details["sample_count"] == 2
    assert details["peak_worker_rss_bytes"] == 250
    assert details["peak_owned_runtime_rss_bytes"] == 200
    assert details["accelerator_coverage"] == "not_applicable_cpu_only"
    assert schedule.stopped


def test_memory_accelerator_is_separate_never_added_to_process_rss():
    memory = sampler([MemoryObservation(100, 200, 400)], scope=MemoryScope(True, True, "separate"))
    with memory:
        pass
    result = memory.result()
    assert result["peak_memory_bytes"] == 300
    assert result["memory_measurement"]["peak_accelerator_memory_bytes"] == 400
    assert result["memory_measurement"]["accelerator_coverage"] == "separately_observed_not_added_to_RSS"


@pytest.mark.parametrize("observation,scope,reason", [
    (MemoryObservation(100, None), MemoryScope(True, True, "unavailable"), "incomplete_process_RSS_observation"),
    (MemoryObservation(100, 200), MemoryScope(True, False, "cpu_only"), "incomplete_owned_process_scope"),
    (MemoryObservation(-1, 200), MemoryScope(True, True, "cpu_only"), "invalid_memory_observation"),
])
def test_partial_or_invalid_memory_never_claims_worker_plus_runtime_peak(observation, scope, reason):
    memory = sampler([observation], scope=scope)
    with memory:
        pass
    result = memory.result()
    assert result["peak_memory_bytes"] is None
    assert result["peak_memory_unavailable_reason"] == reason


def test_intermittent_missing_observation_invalidates_aggregate_peak():
    schedule = ManualScheduler()
    memory = sampler([MemoryObservation(10, 20), MemoryObservation(20, None)], scheduler=schedule)
    with memory:
        schedule.tick()
    assert memory.result()["peak_memory_bytes"] is None
    assert memory.result()["memory_measurement"]["sample_count"] == 2


def test_evaluate_consumes_scoped_sampler_and_keeps_synthetic_scope_clear():
    schedule = ManualScheduler()
    memory = sampler([MemoryObservation(10, 20), MemoryObservation(100, 200)], scheduler=schedule)
    runtime = native_runtime([{"research": 1, "art": 0}])
    decide = runtime.decide
    def observed(state, questions):
        schedule.tick()
        return decide(state, questions)
    runtime.decide = observed
    report = evaluate(dataset(), Classifier(runtime), memory_sampler=memory)
    assert report["metrics"]["performance"]["peak_memory_bytes"] == 300
    assert report["metrics"]["performance"]["memory_measurement"]["window"] == "classification_calls"
    assert report["metrics"]["status"] == "synthetic_dataset_model_evaluation"


def test_main_scopes_memory_around_cold_factory_and_cleanup(tmp_path):
    schedule = ManualScheduler()
    memory = sampler([MemoryObservation(10, 0), MemoryObservation(20, 300), MemoryObservation(30, 0)],
        scheduler=schedule, window="cold_preparation_classification_cleanup")
    @contextmanager
    def factory(manifest):
        runtime = native_runtime([{"research": 1, "art": 0}])
        schedule.tick()
        try:
            yield Classifier(runtime)
        finally:
            schedule.tick()
    source, output = tmp_path / "data.jsonl", tmp_path / "report.json"
    write_jsonl(source, dataset())
    assert main(["--dataset", str(source), "--output", str(output)], runtime_factory=factory,
                memory_factory=lambda: memory) == 0
    performance = json.loads(output.read_text())["metrics"]["performance"]
    assert performance["peak_memory_bytes"] == 320
    assert performance["memory_measurement"]["window"] == "cold_preparation_classification_cleanup"
    assert performance["memory_measurement"]["sample_count"] == 3


@pytest.mark.parametrize("missing", ["per_topic", "language_slices", "confirmed_model_inference_count", "performance", "dataset_counts", "report_format"])
def test_comparison_rejects_missing_required_report_evidence(missing):
    reports = paired_reports()
    for report in reports:
        report["metrics"].pop(missing, None)
    with pytest.raises(ValueError, match="invalid_evaluation_evidence"):
        compare_reports(*reports)


@pytest.mark.parametrize("mutation", [
    lambda m: m["per_topic"]["research"].update(positive_count=0),
    lambda m: m["per_topic"]["research"].update(precision=.1),
    lambda m: m["language_slices"]["en"]["per_topic"].pop("art"),
    lambda m: m["language_slices"]["en"].update(sample_count=2),
    lambda m: m.update(confirmed_model_inference_count=0),
    lambda m: m.update(classifier_outcome_counts={"decided": 9}),
    lambda m: m["performance"].pop("peak_memory_unavailable_reason"),
    lambda m: m["performance"].update(p50_latency_ms=float("nan")),
    lambda m: m["performance"].update(p95_latency_ms=-1),
])
def test_comparison_rejects_inconsistent_or_invalid_evidence(mutation):
    reports = paired_reports()
    for report in reports:
        mutation(report["metrics"])
    with pytest.raises(ValueError, match="invalid_evaluation_evidence"):
        compare_reports(*reports)


def test_unknown_hardware_cannot_establish_performance_comparability():
    reports = paired_reports()
    comparison = compare_reports(*reports)
    assert comparison["metrics"]["performance_comparability"]["status"] == "unverified"
    assert comparison["metrics"]["performance_comparability"]["reasons"]


def known_hardware():
    return {"os": "synthetic-os", "os_release": "synthetic-v1", "architecture": "synthetic-arch",
        "python_version": "3.12.0", "logical_cpu_count": "8", "cpu_model": "synthetic-CPU-A",
        "physical_memory_bytes": "16000000000", "acceleration_mode": "cpu_only",
        "accelerator_model": "not_applicable_cpu_only", "accelerator_memory_bytes": "not_applicable_cpu_only",
        "acceleration_backend": "none_cpu_only", "gpu_offload_layers": "0"}


def complete_conditions(reports):
    for report in reports:
        report["hardware"] = known_hardware()
        report["versions"].update(cpu_threads="4", max_response_bytes="65536", timeout_seconds="15.0")
    return reports


def test_recorded_complete_equal_conditions_can_be_identified_without_claiming_same_host():
    comparison = compare_reports(*complete_conditions(paired_reports()))
    assert comparison["metrics"]["performance_comparability"]["status"] == "same_recorded_conditions"
    assert comparison["metrics"]["performance_comparability"]["limitations"]


@pytest.mark.parametrize("field,value", [("cpu_model", "synthetic-CPU-B"), ("physical_memory_bytes", "8000000000"),
    ("acceleration_mode", "gpu"), ("accelerator_model", "synthetic-GPU-B")])
def test_different_observed_hardware_is_not_performance_comparable(field, value):
    reports = complete_conditions(paired_reports())
    reports[1]["hardware"][field] = value
    result = compare_reports(*reports)
    assert result["metrics"]["performance_comparability"]["status"] == "different_recorded_conditions"


def test_runtime_thread_count_and_limits_are_captured_and_differences_are_not_comparable():
    runtime = native_runtime([{"research": 1, "art": 0}])
    runtime._metadata.update(cpu_threads=7, max_response_bytes=2048, timeout_seconds=3.0)
    report = evaluate(dataset(), Classifier(runtime), hardware=known_hardware())
    assert report["versions"]["cpu_threads"] == "7"
    assert report["versions"]["max_response_bytes"] == "2048"
    assert report["hardware"]["cpu_model"] == "synthetic-CPU-A"
    reports = complete_conditions(paired_reports())
    reports[1]["versions"]["cpu_threads"] = "8"
    result = compare_reports(*reports)
    assert result["metrics"]["performance_comparability"]["status"] == "different_recorded_conditions"


@pytest.mark.parametrize("mutation", [
    lambda m: m["dataset_counts"]["per_topic"]["research"].update(sample_count=True),
    lambda m: m["language_slices"]["en"].update(sample_count=True),
    lambda m: m["performance"]["memory_measurement"].pop("scope"),
    lambda m: m["performance"]["memory_measurement"].pop("window"),
    lambda m: m["performance"]["memory_measurement"].update(scope="invented_scope"),
])
def test_evidence_rejects_boolean_counts_and_missing_memory_scope_even_when_unavailable(mutation):
    reports = paired_reports()
    for report in reports:
        mutation(report["metrics"])
    with pytest.raises(ValueError, match="invalid_evaluation_evidence"):
        compare_reports(*reports)


def test_wrong_memory_window_cannot_mislabel_the_observed_scope(tmp_path):
    memory = sampler([MemoryObservation(10, 20)], window="cold_preparation_classification_cleanup")
    with pytest.raises(ValueError, match="invalid_memory_window"):
        evaluate(dataset(), Classifier(native_runtime([{"research": 1, "art": 0}])), memory_sampler=memory)


def test_unavailable_memory_is_not_marked_comparable_even_with_known_hardware():
    result = compare_reports(*complete_conditions(paired_reports()))
    assert result["metrics"]["performance_comparability"]["measurement_comparability"]["peak_process_RSS"] == "unavailable"


def test_nonintegral_cpu_threads_cannot_verify_execution_configuration():
    reports = complete_conditions(paired_reports())
    for report in reports:
        report["versions"]["cpu_threads"] = "4.5"
    assert compare_reports(*reports)["metrics"]["performance_comparability"]["status"] == "unverified"


def test_missing_separate_accelerator_observation_is_explicitly_unavailable():
    memory = sampler([MemoryObservation(10, 20, None)], scope=MemoryScope(True, True, "separate"))
    with memory:
        pass
    assert memory.result()["peak_memory_bytes"] == 30
    assert memory.result()["memory_measurement"]["accelerator_coverage"] == "unavailable"
    def unavailable():
        raise OSError("private-native-error")
    memory = ScopedMemorySampler(unavailable, scope=MemoryScope(True, True, "separate"), scheduler=ManualScheduler())
    with memory:
        pass
    result = memory.result()
    assert result["memory_measurement"]["accelerator_coverage"] == "unavailable"
    assert "private-native-error" not in json.dumps(result)


@pytest.mark.parametrize("key,value", [("acceleration_backend", "vulkan"), ("gpu_offload_layers", "12")])
def test_backend_and_offload_differences_are_not_ignored(key, value):
    reports = complete_conditions(paired_reports())
    reports[1]["hardware"][key] = value
    assert compare_reports(*reports)["metrics"]["performance_comparability"]["status"] == "different_recorded_conditions"


@pytest.mark.parametrize("field,value", [("dataset_sha256", "not-a-digest"), ("classification_model_versions", '["other"]')])
def test_comparison_rejects_invalid_dataset_or_classification_provenance(field, value):
    reports = paired_reports()
    for report in reports:
        report["versions"][field] = value
    with pytest.raises(ValueError, match="invalid_evaluation_evidence"):
        compare_reports(*reports)


def test_missing_whole_language_slice_is_not_hidden_by_other_slices():
    reports = paired_reports()
    for report in reports:
        report["metrics"]["language_slices"] = {}
        report["metrics"]["dataset_counts"]["language_slices"] = {}
    with pytest.raises(ValueError, match="invalid_evaluation_evidence"):
        compare_reports(*reports)


def test_unconfirmed_sampler_shutdown_invalidates_peak_and_late_ticks_do_not_observe():
    class Unconfirmed(ManualScheduler):
        def stop(self):
            return False
    schedule, observations = Unconfirmed(), []
    def observe():
        observations.append("bounded-read")
        return MemoryObservation(10, 20)
    memory = ScopedMemorySampler(observe, scope=MemoryScope(True, True, "cpu_only"), scheduler=schedule)
    with memory:
        pass
    assert memory.result()["peak_memory_bytes"] is None
    assert memory.result()["peak_memory_unavailable_reason"] == "memory_sampler_shutdown_unconfirmed"
    schedule.tick()
    assert len(observations) == 1
    assert memory.result()["memory_measurement"]["sample_count"] == 1


@pytest.mark.parametrize("field", ["cpu_model", "physical_memory_bytes", "acceleration_mode", "acceleration_backend", "gpu_offload_layers"])
def test_equal_unknown_material_fields_still_leave_comparability_unverified(field):
    reports = complete_conditions(paired_reports())
    for report in reports:
        report["hardware"][field] = "unavailable_not_observed"
    assert compare_reports(*reports)["metrics"]["performance_comparability"]["status"] == "unverified"


def test_periodic_default_scheduler_can_collect_and_stop_without_sleep():
    from threading import Event
    observed = Event()
    def observe():
        observed.set()
        return MemoryObservation(10, 20)
    memory = ScopedMemorySampler(observe, scope=MemoryScope(True, True, "cpu_only"))
    with memory:
        assert observed.wait(timeout=1)
    assert memory.result()["peak_memory_bytes"] == 30
    assert memory.result()["memory_measurement"]["sample_count"] >= 1


def test_complete_multilanguage_partial_abstention_evidence_is_accepted():
    data = dataset([record("a", "en"), record("b", "zh", ()), record("c", "mixed", ("art",))])
    data["synthetic"] = False
    runtime = native_runtime([{"research": 1, "art": .5}, {"research": .5, "art": 0}, {"research": 0, "art": 1}])
    first = evaluate(data, Classifier(runtime))
    from threadrelay.inference.provision import load_catalog
    manifest = load_catalog()["models"][1]["manifest"]
    runtime = native_runtime([{"research": 1, "art": .5}, {"research": .5, "art": 0}, {"research": 0, "art": 1}])
    runtime._metadata.update(model_id=manifest["id"], model_sha256=manifest["sha256"], quantization=manifest["quantization"])
    second = evaluate(data, Classifier(runtime))
    result = compare_reports(first, second)
    assert set(result["metrics"]["quantization_comparison"]["Q4_K_M"]["language_slices"]) == {"en", "zh", "mixed"}
    assert result["metrics"]["quantization_comparison"]["Q8_0"]["abstention_count"] == 2


def test_boolean_dataset_total_is_not_a_valid_measured_count():
    reports = paired_reports()
    for report in reports:
        report["metrics"]["dataset_sample_count"] = True
    with pytest.raises(ValueError, match="invalid_evaluation_evidence"):
        compare_reports(*reports)


def test_cli_comparison_rejects_duplicate_json_keys_without_publishing_evidence(tmp_path):
    first, second = complete_conditions(paired_reports())
    source_a, source_b, output = tmp_path / "a.json", tmp_path / "b.json", tmp_path / "compared.json"
    encoded = json.dumps(first)
    encoded = encoded.replace('"sample_count": 1', '"sample_count": 99, "sample_count": 1', 1)
    source_a.write_text(encoded)
    source_b.write_text(json.dumps(second))
    assert main(["--compare", str(source_a), str(source_b), "--output", str(output)]) == 2
    assert not output.exists()


def test_pair_rejects_different_gold_slice_counts_even_if_fingerprint_was_copied():
    first, _ = paired_reports()
    from threadrelay.inference.provision import load_catalog
    manifest = load_catalog()["models"][1]["manifest"]
    runtime = native_runtime([{"research": 1, "art": 0}])
    runtime._metadata.update(model_id=manifest["id"], model_sha256=manifest["sha256"], quantization=manifest["quantization"])
    data = dataset([record("a", expected=("art",))])
    data["synthetic"] = False
    second = evaluate(data, Classifier(runtime))
    second["versions"]["dataset_sha256"] = first["versions"]["dataset_sha256"]
    with pytest.raises(ValueError, match="incomparable_evaluation_reports"):
        compare_reports(first, second)


@pytest.mark.parametrize("missing", ["uncertainty", "acceptance", "held_out_warning"])
def test_versioned_report_requires_uncertainty_and_no_release_caveats(missing):
    reports = paired_reports()
    for report in reports:
        report["metrics"].pop(missing)
    with pytest.raises(ValueError, match="invalid_evaluation_evidence"):
        compare_reports(*reports)


def test_invalid_classifier_threshold_provenance_is_not_accepted():
    reports = paired_reports()
    for report in reports:
        report["versions"]["positive_threshold"] = "NaN"
    with pytest.raises(ValueError, match="invalid_evaluation_evidence"):
        compare_reports(*reports)


def coverage_reports(*, first_failures=(), second_failures=(), first_skips=(), second_skips=()):
    """Actual Classifier outcomes over a synthetic decision boundary, not speed evidence."""
    from threadrelay.inference.provision import load_catalog
    from threadrelay.inference.runtime import RuntimeUnavailable
    data = dataset([record("a"), record("b")])
    data["synthetic"] = False
    reports = []
    for index, (failures, skips) in enumerate(((first_failures, first_skips), (second_failures, second_skips))):
        runtime = native_runtime([{"research": 1, "art": 0}] * 2)
        manifest = load_catalog()["models"][index]["manifest"]
        runtime._metadata.update(model_id=manifest["id"], model_sha256=manifest["sha256"], quantization=manifest["quantization"])
        decide = runtime.decide
        failed_texts = {r["post"]["text"] for r in data["records"] if r["post"]["post_id"] in failures}
        def bounded_decide(state, questions, *, failed_texts=failed_texts, decide=decide):
            if state["post_text"] in failed_texts:
                raise RuntimeUnavailable("synthetic-inference-failure")
            return decide(state, questions)
        runtime.decide = bounded_decide
        class ExtractionSkipped(Classifier):
            def classify(self, post, topics):
                # Simulate a local extraction-quality refusal at the adapter boundary.
                if post["post_id"] in skips:
                    post["quality_flags"] = [{"code": "missing", "detail": "Synthetic extraction failure"}]
                return super().classify(post, topics)
        reports.append(evaluate(data, ExtractionSkipped(runtime)))
    return complete_conditions(reports)


def test_one_sided_inference_failure_is_not_comparable_speed_and_retains_end_to_end_latency():
    first, second = coverage_reports(second_failures=("a",))
    comparison = compare_reports(first, second)
    conditions = comparison["metrics"]["performance_comparability"]
    assert conditions["measurement_comparability"]["classifier_call_latency"] != "same_recorded_conditions"
    assert "failed_inference_included_in_end_to_end_latency" in conditions["latency_comparison_reasons"]
    assert conditions["latency_coverage"]["Q4_K_M"]["confirmed_model_inference_count"] == 2
    assert conditions["latency_coverage"]["Q8_0"]["confirmed_model_inference_count"] == 1
    assert conditions["latency_coverage"]["Q8_0"]["classifier_outcome_counts"] == {"inference_unavailable": 1, "decided": 1}
    assert comparison["metrics"]["quantization_comparison"]["Q8_0"]["performance"] == second["metrics"]["performance"]


def test_equal_failed_coverage_is_still_not_a_successful_model_speed_comparison():
    first, second = coverage_reports(first_failures=("a",), second_failures=("a",))
    conditions = compare_reports(first, second)["metrics"]["performance_comparability"]
    assert conditions["measurement_comparability"]["classifier_call_latency"] == "unverified"
    assert "failed_inference_included_in_end_to_end_latency" in conditions["latency_comparison_reasons"]


def test_unequal_nonfailure_skipped_coverage_is_not_comparable_model_speed():
    first, second = coverage_reports(second_skips=("a",))
    conditions = compare_reports(first, second)["metrics"]["performance_comparability"]
    assert conditions["measurement_comparability"]["classifier_call_latency"] == "different_inference_coverage"
    assert "different_confirmed_native_inference_coverage" in conditions["latency_comparison_reasons"]
    assert conditions["latency_coverage"]["Q8_0"]["classifier_outcome_counts"] == {"extraction_uncertainty": 1, "decided": 1}


def test_equal_nonfailure_skip_coverage_is_disclosed_with_descriptive_call_latency():
    first, second = coverage_reports(first_skips=("a",), second_skips=("a",))
    conditions = compare_reports(first, second)["metrics"]["performance_comparability"]
    assert conditions["measurement_comparability"]["classifier_call_latency"] == "same_recorded_conditions"
    assert conditions["latency_coverage"]["Q4_K_M"]["sample_count"] == 2
    assert conditions["latency_coverage"]["Q4_K_M"]["confirmed_model_inference_count"] == 1
    assert "not pure model-forward speed" in conditions["limitations"]


def test_sampling_gate_rechecks_closure_before_admitting_an_observer():
    from threading import Lock
    class Deferred(ManualScheduler):
        def start(self, callback, interval_seconds):
            self.callback = callback
            return self
        def stop(self):
            return False
    schedule, observations = Deferred(), []
    def observe():
        observations.append("must-not-run-after-close")
        return MemoryObservation(10, 20)
    memory = ScopedMemorySampler(observe, scope=MemoryScope(True, True, "cpu_only"), scheduler=schedule)
    class CloseBeforeAcquireReturns:
        def __init__(self):
            self.lock = Lock()
        def acquire(self, blocking=False):
            # Force exactly the former race: initial check passed, but closure
            # wins before the sampling gate's acquisition returns.
            memory.__exit__(None, None, None)
            return self.lock.acquire(blocking=blocking)
        def release(self):
            self.lock.release()
    memory._in_sample = CloseBeforeAcquireReturns()
    memory.__enter__()
    schedule.tick()
    assert observations == []
    assert memory.result()["memory_measurement"]["sample_count"] == 0
    assert memory.result()["peak_memory_unavailable_reason"] == "memory_sampler_shutdown_unconfirmed"


def test_already_admitted_observer_does_not_block_close_or_publish_a_late_result():
    from threading import Event, Thread
    class Deferred(ManualScheduler):
        def start(self, callback, interval_seconds):
            self.callback = callback
            return self
        def stop(self):
            return False
    started, release, closed = Event(), Event(), Event()
    schedule = Deferred()
    def observe():
        started.set()
        assert release.wait(timeout=2)
        return MemoryObservation(10, 20)
    memory = ScopedMemorySampler(observe, scope=MemoryScope(True, True, "cpu_only"), scheduler=schedule)
    memory.__enter__()
    sampling = Thread(target=schedule.tick, daemon=True)
    sampling.start()
    assert started.wait(timeout=1)
    def close():
        memory.__exit__(None, None, None)
        closed.set()
    closing = Thread(target=close, daemon=True)
    closing.start()
    try:
        assert closed.wait(timeout=1), "close must not wait for a bounded observer still in flight"
        assert memory.result()["memory_measurement"]["sample_count"] == 0
    finally:
        release.set()
        sampling.join(timeout=1)
        closing.join(timeout=1)
    assert not sampling.is_alive() and not closing.is_alive()
    assert memory.result()["memory_measurement"]["sample_count"] == 0
