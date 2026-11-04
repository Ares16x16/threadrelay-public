"""Observed material conditions, not matching unknown sentinels or host names."""
import os
from pathlib import Path
import platform

FIELDS = ("os", "os_release", "architecture", "python_version", "logical_cpu_count", "cpu_model",
    "physical_memory_bytes", "acceleration_mode", "accelerator_model", "accelerator_memory_bytes",
    "acceleration_backend", "gpu_offload_layers")
UNAVAILABLE = "unavailable_not_observed"


def observe_hardware():
    cpu_model = platform.processor().strip()
    if platform.system() == "Linux":
        try:
            cpu_model = next(line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text().splitlines()
                if line.lower().startswith("model name"))
        except (OSError, StopIteration):
            pass
    if cpu_model.lower() in ("", "arm", "arm64", "aarch64", "x86_64", "amd64"):
        cpu_model = UNAVAILABLE
    try:
        physical_memory = str(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"))
        if int(physical_memory) <= 0:
            physical_memory = UNAVAILABLE
    except (AttributeError, OSError, ValueError):
        physical_memory = UNAVAILABLE
    return {"os": platform.system(), "os_release": platform.release(), "architecture": platform.machine(),
        "python_version": platform.python_version(), "logical_cpu_count": str(os.cpu_count() or UNAVAILABLE),
        "cpu_model": cpu_model, "physical_memory_bytes": physical_memory,
        "acceleration_mode": UNAVAILABLE, "accelerator_model": UNAVAILABLE, "accelerator_memory_bytes": UNAVAILABLE,
        "acceleration_backend": UNAVAILABLE, "gpu_offload_layers": UNAVAILABLE}


def capture_hardware(observed=None):
    if observed is None:
        return observe_hardware()
    if not isinstance(observed, dict) or any(key not in FIELDS or not isinstance(value, str) for key, value in observed.items()):
        raise ValueError("invalid_observed_hardware")
    return {key: observed.get(key, UNAVAILABLE) for key in FIELDS}


def _known(value):
    return isinstance(value, str) and bool(value.strip()) and not any(word in value.lower() for word in ("unavailable", "unknown", "not_observed", "not_measured"))


def performance_comparability(first, second):
    reasons, differences = [], []
    material = FIELDS
    for key in material:
        left, right = first["hardware"].get(key), second["hardware"].get(key)
        if not _known(left) or not _known(right):
            reasons.append(f"unverified_hardware:{key}")
        elif left != right:
            differences.append(f"different_hardware:{key}")
    for report in (first, second):
        hw = report["hardware"]
        if hw.get("acceleration_mode") not in ("cpu_only", "gpu", "mixed"):
            reasons.append("unverified_acceleration_mode")
        elif hw["acceleration_mode"] == "cpu_only":
            if (any(hw.get(key) != "not_applicable_cpu_only" for key in ("accelerator_model", "accelerator_memory_bytes"))
                    or hw.get("acceleration_backend") != "none_cpu_only" or hw.get("gpu_offload_layers") != "0"):
                reasons.append("inconsistent_cpu_only_accelerator_evidence")
        else:
            if any(not _known(hw.get(key)) or hw.get(key) == "not_applicable_cpu_only" for key in ("accelerator_model", "accelerator_memory_bytes")):
                reasons.append("incomplete_accelerator_evidence")
            if (not _known(hw.get("acceleration_backend")) or hw.get("acceleration_backend") == "none_cpu_only"
                    or not hw.get("gpu_offload_layers", "").isdecimal() or int(hw["gpu_offload_layers"]) <= 0):
                reasons.append("incomplete_accelerator_configuration")
        for key in ("logical_cpu_count", "physical_memory_bytes"):
            value = hw.get(key, "")
            if not value.isdecimal() or int(value) <= 0:
                reasons.append(f"unverified_hardware:{key}")
    for key in ("cpu_threads", "context_tokens", "max_input_bytes", "max_response_bytes", "timeout_seconds"):
        left, right = first["versions"].get(key), second["versions"].get(key)
        try:
            valid = all(float(v) > 0 and float(v) < float("inf") for v in (left, right))
            if key != "timeout_seconds":
                valid = valid and all(isinstance(v, str) and v.isdecimal() for v in (left, right))
        except (ValueError, TypeError):
            valid = False
        if not valid:
            reasons.append(f"unverified_runtime_limit:{key}")
        elif float(left) != float(right):
            differences.append(f"different_runtime_limit:{key}")
    status = "different_recorded_conditions" if differences else "unverified" if reasons else "same_recorded_conditions"
    first_perf, second_perf = first["metrics"]["performance"], second["metrics"]["performance"]
    memory_status = "unavailable"
    if first_perf["peak_memory_bytes"] is not None and second_perf["peak_memory_bytes"] is not None:
        memory_keys = ("method", "scope", "window", "interval_seconds", "accelerator_coverage")
        memory_status = status if all(first_perf["memory_measurement"][key] == second_perf["memory_measurement"][key]
            for key in memory_keys) else "different_measurement_conditions"
    coverage, signatures, latency_reasons = {}, [], []
    has_failed_inference = False
    for quantization, report in (("Q4_K_M", first), ("Q8_0", second)):
        metrics = report["metrics"]
        outcomes = dict(metrics["classifier_outcome_counts"])
        coverage[quantization] = {"sample_count": metrics["sample_count"],
            "confirmed_model_inference_count": metrics["confirmed_model_inference_count"],
            "classifier_outcome_counts": outcomes}
        failed = outcomes.get("inference_unavailable", 0)
        has_failed_inference = has_failed_inference or failed > 0
        # Decided and uncertain_decision both confirm a native decision call.
        # Extraction/context refusals and failed calls are not equivalent work.
        skipped = {key: value for key, value in outcomes.items()
            if key not in ("decided", "uncertain_decision", "inference_unavailable") and value > 0}
        signatures.append((metrics["sample_count"], metrics["confirmed_model_inference_count"], failed, skipped))
    if has_failed_inference:
        latency_reasons.append("failed_inference_included_in_end_to_end_latency")
    if signatures[0][1] != signatures[1][1]:
        latency_reasons.append("different_confirmed_native_inference_coverage")
    if signatures[0] != signatures[1]:
        latency_reasons.append("different_skipped_or_failed_inference_coverage")
        latency_status = "different_inference_coverage"
    else:
        latency_status = "unverified" if has_failed_inference else status
    return {"status": status,
        "reasons": sorted(set(differences + reasons)),
        "latency_coverage": coverage, "latency_comparison_reasons": latency_reasons,
        "measurement_comparability": {"classifier_call_latency": latency_status,
            "cold_preparation": status if all(p["cold_start_ms"] is not None for p in (first_perf, second_perf)) else "unavailable",
            "peak_process_RSS": memory_status},
        "limitations": "Equal recorded material conditions do not prove the same host, background load, thermal state or repeatability. Classifier-call latency is descriptive end-to-end work, not pure model-forward speed; failed or unequal inference coverage cannot establish comparable model speed. Unverified/different conditions forbid comparative speed/memory conclusions; quality metrics remain separate."}
