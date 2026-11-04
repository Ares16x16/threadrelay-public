"""Versioned exported evidence validation; hashes are not signed attestations."""
import json
import math
import re

from .metrics import LANGUAGES, _finish

REPORT_FORMAT = "threadrelay-evaluation-report-v2"
COUNT_KEYS = ("sample_count", "positive_count", "predicted_positive_count", "true_positive_count",
    "false_positive_count", "true_negative_count", "false_negative_count", "abstention_count", "missing_prediction_count")
OUTCOMES = frozenset(("decided", "uncertain_decision", "extraction_uncertainty", "invalid_post_encoding",
    "post_context_budget", "inference_unavailable"))


def _require(condition):
    if not condition:
        raise ValueError("invalid_evaluation_evidence")


def _integer(value):
    return type(value) is int and value >= 0


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _equal(actual, expected):
    if expected is None:
        return actual is None
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(_equal(a, e) for a, e in zip(actual, expected))
    return type(actual) in (int, float) and math.isfinite(actual) and math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-12)


def _stats(stats, n, positive=None):
    _require(isinstance(stats, dict) and all(k in stats and _integer(stats[k]) for k in COUNT_KEYS))
    _require(stats["sample_count"] == n and (positive is None or stats["positive_count"] == positive))
    tp, fp, tn, fn, abstained = (stats[k] for k in ("true_positive_count", "false_positive_count", "true_negative_count", "false_negative_count", "abstention_count"))
    _require(tp + fn == stats["positive_count"] <= n and tp + fp == stats["predicted_positive_count"] <= n)
    _require(stats["missing_prediction_count"] <= abstained <= n)
    missing_negative = n - (tp + fp + tn + fn)
    _require(0 <= missing_negative <= abstained and abstained - missing_negative <= fn)
    expected = _finish({k: stats[k] for k in COUNT_KEYS})
    _require(all(k in stats and _equal(stats[k], expected[k]) for k in expected if k not in COUNT_KEYS))


def _slice(metrics, expected, topics):
    _require(isinstance(metrics, dict) and isinstance(expected, dict))
    n = expected["sample_count"]
    _require(_integer(n) and n > 0 and _integer(metrics["sample_count"]) and metrics["sample_count"] == n)
    _require(set(metrics["per_topic"]) == set(expected["per_topic"]) == set(topics))
    for topic in topics:
        counts = expected["per_topic"][topic]
        _require(_integer(counts["sample_count"]) and counts["sample_count"] == n and _integer(counts["positive_count"]) and counts["positive_count"] <= n)
        _stats(metrics["per_topic"][topic], n, counts["positive_count"])
    micro_counts = {key: sum(metrics["per_topic"][topic][key] for topic in topics) for key in COUNT_KEYS}
    _require(all(metrics["micro"][key] == value for key, value in micro_counts.items()))
    _stats(metrics["micro"], n * len(topics))
    abstention = metrics["abstention_count"]
    topic_abstentions = [metrics["per_topic"][topic]["abstention_count"] for topic in topics]
    _require(_integer(abstention) and max(topic_abstentions) <= abstention <= min(n, sum(topic_abstentions)))
    _require(_equal(metrics["abstention_rate"], abstention / n))


def _memory(performance):
    peak, reason, memory = (performance[k] for k in ("peak_memory_bytes", "peak_memory_unavailable_reason", "memory_measurement"))
    _require(memory["method"] == "sampled_simultaneous_process_RSS_peak_lower_bound")
    _require(memory["scope"] in ("unavailable", "partial_owned_process_scope", "worker_and_all_owned_runtime_processes"))
    _require(memory["window"] in ("unavailable", "classification_calls", "cold_preparation_classification_cleanup"))
    if memory["scope"] == "unavailable":
        _require(memory["window"] == "unavailable" and memory["interval_seconds"] is None
            and memory["sample_count"] == 0 and memory["successful_sample_count"] == 0
            and memory["worker_coverage"] is False and memory["owned_runtime_coverage"] is False)
    else:
        _require(_finite(memory["interval_seconds"]) and .01 <= memory["interval_seconds"] <= 1
            and memory["window"] != "unavailable")
    _require(_integer(memory["sample_count"]) and _integer(memory["successful_sample_count"]) and memory["successful_sample_count"] <= memory["sample_count"])
    if memory["sample_count"] == 0:
        _require(all(memory[key] is None for key in ("peak_worker_rss_bytes", "peak_owned_runtime_rss_bytes", "peak_accelerator_memory_bytes")))
    _require(type(memory["worker_coverage"]) is bool and type(memory["owned_runtime_coverage"]) is bool)
    if memory["scope"] == "worker_and_all_owned_runtime_processes":
        _require(memory["worker_coverage"] and memory["owned_runtime_coverage"])
    elif memory["scope"] == "partial_owned_process_scope":
        _require(not (memory["worker_coverage"] and memory["owned_runtime_coverage"]))
    _require(memory["accelerator_coverage"] in ("unavailable", "not_applicable_cpu_only", "separately_observed_not_added_to_RSS"))
    for key in ("peak_worker_rss_bytes", "peak_owned_runtime_rss_bytes", "peak_accelerator_memory_bytes"):
        _require(memory[key] is None or _integer(memory[key]))
    if memory["accelerator_coverage"] == "separately_observed_not_added_to_RSS":
        _require(_integer(memory["peak_accelerator_memory_bytes"]))
    else:
        _require(memory["peak_accelerator_memory_bytes"] is None)
    if peak is None:
        _require(isinstance(reason, str) and bool(reason))
    else:
        _require(_integer(peak) and reason is None and memory["sample_count"] > 0
            and memory["successful_sample_count"] == memory["sample_count"]
            and memory["worker_coverage"] and memory["owned_runtime_coverage"]
            and memory["scope"] == "worker_and_all_owned_runtime_processes")
        _require(_finite(memory["interval_seconds"]) and .01 <= memory["interval_seconds"] <= 1
            and memory["window"] in ("classification_calls", "cold_preparation_classification_cleanup"))
        worker, runtime = memory["peak_worker_rss_bytes"], memory["peak_owned_runtime_rss_bytes"]
        _require(_integer(worker) and _integer(runtime) and max(worker, runtime) <= peak <= worker + runtime)
    _require(isinstance(memory["limitations"], str) and bool(memory["limitations"]))


def validate_evidence(report):
    """Reject partial/inconsistent real-run exports before any comparison.

    Does not prove native execution, genuine labels or an untampered external
    file; those require independent provenance and ownership checks.
    """
    try:
        metrics, versions = report["metrics"], report["versions"]
        _require(metrics["report_format"] == REPORT_FORMAT)
        _require(versions["evaluation_version"] == REPORT_FORMAT)
        _require(metrics["uncertainty"]["method"] == "two_sided_95_percent_wilson_no_continuity_correction"
            and isinstance(metrics["uncertainty"]["limitations"], str) and bool(metrics["uncertainty"]["limitations"]))
        _require(metrics["acceptance"]["status"] == "review_only_no_release_authority"
            and all(isinstance(metrics["acceptance"][key], str) and bool(metrics["acceptance"][key])
                    for key in ("thresholds", "synthetic_warning")))
        _require(isinstance(metrics["held_out_warning"], str) and bool(metrics["held_out_warning"]))
        _require(all(isinstance(versions[key], str) and bool(versions[key])
            for key in ("prompt_version", "cache_version", "positive_threshold", "negative_threshold", "context_tokens", "max_input_bytes")))
        _require(0 <= float(versions["negative_threshold"]) < float(versions["positive_threshold"]) <= 1)
        _require(all(re.fullmatch(r"[0-9a-f]{64}", versions[key]) is not None for key in ("dataset_sha256", "topics_sha256")))
        _require(versions["dataset_split"] == "test")
        expected_model = f'{versions["model_id"]}@{versions["model_sha256"][:16]}+{versions["runtime_version"]}'
        _require(json.loads(versions["classification_model_versions"]) == [expected_model])
        topics, languages = json.loads(versions["evaluated_topic_ids"]), json.loads(versions["observed_languages"])
        _require(isinstance(topics, list) and 1 <= len(topics) <= 100
            and all(isinstance(t, str) and 1 <= len(t) <= 128 for t in topics) and len(set(topics)) == len(topics))
        _require(isinstance(languages, list) and bool(languages) and len(set(languages)) == len(languages) and set(languages) <= set(LANGUAGES))
        n = metrics["sample_count"]
        _require(_integer(n) and 0 < n <= 100_000 and _integer(metrics["dataset_sample_count"]) and metrics["dataset_sample_count"] == n)
        counts = metrics["dataset_counts"]
        _slice(metrics, counts, topics)
        _require(set(metrics["language_slices"]) == set(counts["language_slices"]) == set(languages))
        for language in languages:
            _slice(metrics["language_slices"][language], counts["language_slices"][language], topics)
        _require(sum(counts["language_slices"][lang]["sample_count"] for lang in languages) == n)
        _require(sum(metrics["language_slices"][lang]["abstention_count"] for lang in languages) == metrics["abstention_count"])
        for topic in topics:
            for key in COUNT_KEYS:
                _require(sum(metrics["language_slices"][lang]["per_topic"][topic][key] for lang in languages) == metrics["per_topic"][topic][key])
        outcomes = metrics["classifier_outcome_counts"]
        _require(isinstance(outcomes, dict) and set(outcomes) <= OUTCOMES and all(_integer(v) for v in outcomes.values()) and sum(outcomes.values()) == n)
        confirmed = metrics["confirmed_model_inference_count"]
        _require(_integer(confirmed) and 0 < confirmed <= n and confirmed == outcomes.get("decided", 0) + outcomes.get("uncertain_decision", 0))
        performance = metrics["performance"]
        _require(performance["latency_sample_count"] == n and _integer(performance["latency_sample_count"]))
        _require(_finite(performance["p50_latency_ms"]) and _finite(performance["p95_latency_ms"]) and performance["p50_latency_ms"] <= performance["p95_latency_ms"])
        _require(performance["latency_method"] == "monotonic_wall_clock_nearest_rank_milliseconds"
            and performance["latency_scope"] == "sequential_classifier_calls_including_validation_and_skipped_inference_no_warmup_removed")
        if performance["cold_start_ms"] is None:
            _require(isinstance(performance["cold_start_unavailable_reason"], str) and bool(performance["cold_start_unavailable_reason"]))
        else:
            _require(_finite(performance["cold_start_ms"]) and performance["cold_start_unavailable_reason"] is None
                and performance["cold_start_scope"] == "trusted_cold_factory_verification_load_and_canary")
        _memory(performance)
    except (KeyError, TypeError, ValueError, OverflowError, RecursionError):
        raise ValueError("invalid_evaluation_evidence") from None
