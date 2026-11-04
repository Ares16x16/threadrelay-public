"""Offline held-out evaluation. No runtime download, process launch or send path.

The native integration calls main(..., runtime_factory=trusted_factory). That
factory is an internal context-manager capability, NOT a CLI module/path/URL.
It must prepare a fresh, cold, approved and owned DecisionRuntime, yield its
Classifier, and stop/confirm owned shutdown on exit. Missing factory is Not run.
"""
from __future__ import annotations

import argparse
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any, TypeAlias

from threadrelay.contracts import validate_contract
from threadrelay.inference.decision import Classifier
from threadrelay.inference.provision import load_catalog
from threadrelay.inference.runtime import RuntimeLimits
from .dataset import Dataset, _pairs, digest, load_dataset, validate_dataset
from .metrics import summarize
from .evidence import REPORT_FORMAT, validate_evidence
from .hardware import capture_hardware, performance_comparability
from .memory import ScopedMemorySampler, unavailable_memory

EvaluationReport: TypeAlias = dict[str, Any]
RuntimeFactory: TypeAlias = Callable[[dict[str, Any]], AbstractContextManager[Classifier]]
EVALUATION_VERSION = REPORT_FORMAT
PROVENANCE_KEYS = frozenset(("model_id", "model_revision", "model_sha256", "quantization", "runtime_version",
    "runtime_commit", "context_tokens", "max_input_bytes", "reading_mode", "scores_calibrated",
    "prompt_version", "cache_version", "positive_threshold", "negative_threshold",
    "cpu_threads", "max_response_bytes", "timeout_seconds"))


def _hardware() -> dict[str, str]:
    return capture_hardware()


def _versions(dataset, metadata=None) -> dict[str, str]:
    versions = {"evaluation_version": EVALUATION_VERSION, "dataset_sha256": digest(dataset),
                "topics_sha256": digest(dataset["topics"]), "dataset_split": dataset["split"],
                "synthetic_dataset": json.dumps(dataset["synthetic"]),
                "evaluated_topic_ids": json.dumps([t["id"] for t in dataset["topics"] if t["enabled"]]),
                "observed_languages": json.dumps(sorted({r["language"] for r in dataset["records"]}))}
    for key, value in (metadata or {}).items():
        if key not in PROVENANCE_KEYS:
            continue  # Never export arbitrary runtime metadata or overwrite dataset identity.
        if not isinstance(key, str) or type(value) not in (str, int, float, bool) or (isinstance(value, float) and not math.isfinite(value)):
            raise ValueError("invalid_evaluation_provenance")
        versions[key] = value if isinstance(value, str) else json.dumps(value, allow_nan=False)
    if metadata and metadata.get("reading_mode") == "one-way-llama.cpp":
        manifest = next(m["manifest"] for m in load_catalog()["models"]
                        if m["manifest"]["sha256"] == metadata["model_sha256"])
        versions.update(model_license=manifest["license"], model_url=manifest["url"])
    return versions


def _performance(latencies=None, *, synthetic=False):
    ordered = sorted(latencies or [])
    # Nearest rank: no interpolated observations, p95 of three calls is max.
    def percentile(p):
        return ordered[max(0, math.ceil(p * len(ordered)) - 1)] if ordered else None
    return {"latency_sample_count": len(ordered), "p50_latency_ms": percentile(.5), "p95_latency_ms": percentile(.95),
            "latency_scope": ("unavailable_no_classifier_calls" if not ordered else
                "synthetic_classifier_calls_only_not_model_performance" if synthetic else
                "sequential_classifier_calls_including_validation_and_skipped_inference_no_warmup_removed"),
            "latency_method": "monotonic_wall_clock_nearest_rank_milliseconds",
            "cold_start_ms": None, "cold_start_unavailable_reason": "evaluate_receives_an_already_prepared_classifier",
            **unavailable_memory()}


def _acceptance():
    return {"status": "review_only_no_release_authority", "proposed_precision_target": .95,
            "thresholds": "User must choose actual precision thresholds, recall tolerance and adequate per-topic/language counts; a point estimate is not a release guarantee.",
            "synthetic_warning": "Synthetic results never unlock auto-send. Reports do not change configuration, routes or approvals."}


def _verify_native_metadata(metadata, manifest=None):
    catalog = load_catalog()
    candidates = [r["manifest"] for r in catalog["models"]] if manifest is None else [manifest]
    found = any(all(metadata.get(key) == m[field] for key, field in (
        ("model_id", "id"), ("model_revision", "version"), ("model_sha256", "sha256"),
        ("quantization", "quantization"), ("runtime_version", "runtime_version"))) for m in candidates)
    if (not found or metadata.get("runtime_commit") != catalog["runtime"]["commit"]
            or metadata.get("reading_mode") != "one-way-llama.cpp"):
        raise ValueError("unverified_evaluation_runtime_identity")


def evaluate(dataset: Dataset, classifier: Classifier, *, clock: Callable[[], float] = time.perf_counter,
             memory_sampler: ScopedMemorySampler | None = None, hardware: dict[str, str] | None = None) -> EvaluationReport:
    """Measure explicit test split; no training, threshold fitting or routing.

    TopicResult abstentions/missing outputs reduce end-to-end recall for gold
    positives, but do not become negative decisions. Global abstention does not
    erase another topic's explicit positive. Confidence intervals are descriptive.
    """
    dataset = deepcopy(dataset)
    validate_dataset(dataset)
    if memory_sampler is not None and memory_sampler.window != "classification_calls":
        raise ValueError("invalid_memory_window")
    metadata = deepcopy(classifier.metadata)
    synthetic_runtime = metadata.get("reading_mode", "").startswith("synthetic")
    if synthetic_runtime:
        if not dataset["synthetic"]:
            raise ValueError("synthetic_runtime_requires_synthetic_dataset")
    else:
        _verify_native_metadata(metadata)
    predictions, latencies, outcomes = [], [], {}
    confirmed_inferences = 0
    expected_version = f'{metadata["model_id"]}@{metadata["model_sha256"][:16]}+{metadata["runtime_version"]}'
    with memory_sampler if memory_sampler is not None else nullcontext():
        for record in dataset["records"]:
            start = clock()
            prediction = classifier.classify(deepcopy(record["post"]), deepcopy(dataset["topics"]))
            duration = (clock() - start) * 1000
            if not math.isfinite(duration) or duration < 0:
                raise ValueError("invalid_evaluation_clock")
            if classifier.metadata != metadata:
                raise ValueError("evaluation_provenance_changed")
            if prediction.get("model_version") != expected_version:
                raise ValueError("classification_model_version_mismatch")
            reason = classifier.last_reason or "decided"
            outcomes[reason] = outcomes.get(reason, 0) + 1
            if not synthetic_runtime and reason in ("decided", "uncertain_decision"):
                confirmed_inferences += 1
            predictions.append(prediction)
            latencies.append(duration)
    topics = [t["id"] for t in dataset["topics"] if t["enabled"]]
    metrics = summarize(dataset["records"], predictions, topics)
    metrics.update(report_format=REPORT_FORMAT, status="synthetic_smoke_only" if synthetic_runtime else (
        "no_model_inference_confirmed" if not confirmed_inferences else
        "synthetic_dataset_model_evaluation" if dataset["synthetic"] else "local_labeled_evaluation"),
        classifier_outcome_counts=outcomes, confirmed_model_inference_count=confirmed_inferences,
        dataset_sample_count=len(dataset["records"]), dataset_counts=_dataset_counts(dataset),
        performance=_performance(latencies, synthetic=synthetic_runtime),
        acceptance=_acceptance(), held_out_warning="Test split and exact topic-example overlap checked locally; independent labeling and external training/validation overlap require human audit.")
    versions = _versions(dataset, metadata)
    versions["classification_model_versions"] = json.dumps(sorted({p["model_version"] for p in predictions}))
    if memory_sampler is not None:
        metrics["performance"].update(memory_sampler.result())
    report = {"metrics": metrics, "versions": versions, "hardware": capture_hardware(hardware)}
    validate_contract("EvaluationReport", report)
    return report


def _dataset_counts(dataset):
    topic_ids = [t["id"] for t in dataset["topics"] if t["enabled"]]
    def counts(records):
        return {"sample_count": len(records), "per_topic": {topic_id: {
            "sample_count": len(records),
            "positive_count": sum(topic_id in r["expected_topic_ids"] for r in records)}
            for topic_id in topic_ids}}
    dataset_counts = counts(dataset["records"])
    dataset_counts["language_slices"] = {language: counts([
        r for r in dataset["records"] if r["language"] == language])
        for language in sorted({r["language"] for r in dataset["records"]})}
    return dataset_counts


def _not_run(dataset, reason):
    catalog = load_catalog()
    report = {"metrics": {"report_format": REPORT_FORMAT, "status": "not_run", "reason": reason, "sample_count": 0,
        "dataset_counts": _dataset_counts(dataset),
        "dataset_sample_count": len(dataset["records"]), "per_topic": {}, "language_slices": {},
        "abstention_count": None, "performance": _performance(), "acceptance": _acceptance(),
        "quantization_comparison": {m["manifest"]["quantization"]: {"status": "not_run", "reason": reason,
            "pinned_manifest": m["manifest"]} for m in catalog["models"]}},
        "versions": _versions(dataset), "hardware": _hardware()}
    report["metrics"]["performance"]["cold_start_unavailable_reason"] = "trusted_runtime_not_prepared"
    validate_contract("EvaluationReport", report)
    return report


def compare_reports(q4: EvaluationReport, q8: EvaluationReport) -> EvaluationReport:
    """Compare already-run Q4/Q8 evidence, never launch or substitute a model."""
    for report in (q4, q8):
        validate_contract("EvaluationReport", report)
        if report["metrics"].get("status") != "local_labeled_evaluation" or report["versions"].get("synthetic_dataset") != "false":
            raise ValueError("real_model_reports_required")
        _verify_native_metadata(report["versions"])
        validate_evidence(report)
    keys = ("dataset_sha256", "topics_sha256", "dataset_split", "evaluation_version", "model_revision",
            "runtime_version", "runtime_commit", "prompt_version", "cache_version", "positive_threshold",
            "negative_threshold", "context_tokens", "max_input_bytes", "reading_mode",
            "evaluated_topic_ids", "observed_languages")
    if (q4["versions"].get("quantization") != "Q4_K_M" or q8["versions"].get("quantization") != "Q8_0"
            or any(key not in q4["versions"] or q4["versions"][key] != q8["versions"].get(key) for key in keys)
            or q4["metrics"]["dataset_counts"] != q8["metrics"]["dataset_counts"]
            or type(q4["metrics"].get("sample_count")) is not int or q4["metrics"]["sample_count"] <= 0
            or any(r["metrics"].get("sample_count") != q4["metrics"]["sample_count"]
                   or r["metrics"].get("dataset_sample_count") != q4["metrics"]["sample_count"] for r in (q4, q8))):
        raise ValueError("incomparable_evaluation_reports")
    report = {"metrics": {"report_format": REPORT_FORMAT, "status": "local_quantization_comparison", "acceptance": _acceptance(),
        "sample_count": q4["metrics"]["sample_count"],
        "comparison_versions": {"Q4_K_M": deepcopy(q4["versions"]), "Q8_0": deepcopy(q8["versions"])},
        "comparison_hardware": {"Q4_K_M": deepcopy(q4["hardware"]), "Q8_0": deepcopy(q8["hardware"])},
        "performance_comparability": performance_comparability(q4, q8),
        "quantization_comparison": {"Q4_K_M": deepcopy(q4["metrics"]), "Q8_0": deepcopy(q8["metrics"])}},
        "versions": {key: q4["versions"][key] for key in keys}, "hardware": {}}
    report["versions"]["compared_model_sha256"] = json.dumps([q4["versions"]["model_sha256"], q8["versions"]["model_sha256"]])
    validate_contract("EvaluationReport", report)
    return report


class _SyntheticRuntime:
    """Fixed uncertain decisions exercise plumbing, not model accuracy/speed."""
    limits = RuntimeLimits()
    metadata = {"model_id": "synthetic-uncertain", "model_revision": "synthetic-v1", "model_sha256": "0" * 64,
        "quantization": "not_applicable", "runtime_version": "synthetic-v1", "runtime_commit": "0" * 40,
        "reading_mode": "synthetic-smoke", "context_tokens": 4096, "max_input_bytes": 3072, "scores_calibrated": False}

    def decide(self, state, questions):
        return {key: .5 for key in questions}


def _write_report(path: Path, report: EvaluationReport):
    # Private, atomic, no overwrite (including symlinks and the input dataset).
    encoded = (json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=".threadrelay-evaluation-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main(argv=None, *, runtime_factory: RuntimeFactory | None = None,
         memory_factory: Callable[[], ScopedMemorySampler] | None = None,
         hardware_provider: Callable[[], dict[str, str]] | None = None,
         trusted_report: Callable[[bytes], None] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Held-out offline classifier evaluation; no downloads or automatic runtime launcher.")
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--dataset", type=Path, help="Local JSONL header + DatasetRecord lines; data stays local.")
    inputs.add_argument("--compare", nargs=2, type=Path, metavar=("Q4_REPORT", "Q8_REPORT"), help="Compare complete same-data real reports; performance conditions are flagged separately.")
    parser.add_argument("--output", required=True, type=Path, help="New private JSON report; existing files are never overwritten.")
    parser.add_argument("--quantization", choices=("Q4_K_M", "Q8_0"), default="Q4_K_M")
    parser.add_argument("--synthetic-smoke", action="store_true", help="Explicit synthetic-only fixed-abstention plumbing test; NOT a model benchmark.")
    args = parser.parse_args(argv)
    try:
        if args.output.exists() or args.output.is_symlink():
            raise ValueError("report_output_already_exists")
        if args.compare:
            if args.synthetic_smoke:
                raise ValueError("incompatible_evaluation_options")
            report = compare_reports(*(json.loads(p.read_text(encoding="utf-8"), object_pairs_hook=_pairs,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite"))) for p in args.compare))
            code = 0
        else:
            dataset = load_dataset(args.dataset)
            if args.synthetic_smoke:
                if not dataset["synthetic"]:
                    raise ValueError("synthetic_smoke_requires_synthetic_dataset")
                report, code = evaluate(dataset, Classifier(_SyntheticRuntime())), 0
            elif runtime_factory is None:
                report, code = _not_run(dataset, "trusted_owned_native_runtime_factory_unavailable"), 2
            else:
                manifest = next(m["manifest"] for m in load_catalog()["models"] if m["manifest"]["quantization"] == args.quantization)
                try:
                    memory = memory_factory() if memory_factory is not None else None
                    if memory is not None and memory.window != "cold_preparation_classification_cleanup":
                        raise ValueError("invalid_memory_window")
                    with memory if memory is not None else nullcontext():
                        start = time.perf_counter()
                        with runtime_factory(deepcopy(manifest)) as classifier:
                            prepared_ms = (time.perf_counter() - start) * 1000
                            _verify_native_metadata(classifier.metadata, manifest)
                            report = evaluate(dataset, classifier,
                                hardware=hardware_provider() if hardware_provider is not None else None)
                            performance = report["metrics"]["performance"]
                            performance.update(cold_start_ms=prepared_ms, cold_start_unavailable_reason=None,
                                cold_start_scope="trusted_cold_factory_verification_load_and_canary")
                    if memory is not None:
                        report["metrics"]["performance"].update(memory.result())
                    code = 2 if report["metrics"]["status"] == "no_model_inference_confirmed" else 0
                except Exception:
                    # No traceback, dataset prose, endpoint/token or native path export.
                    report, code = _not_run(dataset, "trusted_runtime_preparation_evaluation_or_cleanup_failed"), 2
        _write_report(args.output, report)
        if (trusted_report is not None and code == 0 and runtime_factory is not None
                and not args.compare and not args.synthetic_smoke and not dataset['synthetic']):
            validate_evidence(report)
            trusted_report((json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode('utf-8'))
        if code:
            print("Evaluation not run: approved owned runtime integration is unavailable or failed. Review mode remains required.", file=sys.stderr)
        return code
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        print("Evaluation failed: invalid input, incompatible evidence, or report output unavailable. No release permission granted.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
