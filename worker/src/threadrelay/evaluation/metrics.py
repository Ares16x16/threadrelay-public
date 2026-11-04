"""Deterministic multi-label metrics; undefined denominators remain null."""
import math
from typing import Any

from threadrelay.contracts import validate_contract


LANGUAGES = ("zh", "en", "mixed", "other")


def wilson(successes: int, total: int) -> list[float] | None:
    """Two-sided 95% Wilson score interval, no continuity correction."""
    if total == 0:
        return None
    z = 1.959963984540054
    p, z2 = successes / total, z * z
    center = (p + z2 / (2 * total)) / (1 + z2 / total)
    half = z * math.sqrt(p * (1 - p) / total + z2 / (4 * total * total)) / (1 + z2 / total)
    return [max(0.0, center - half), min(1.0, center + half)]


def _finish(counts):
    predicted, positive = counts["predicted_positive_count"], counts["positive_count"]
    tp, n = counts["true_positive_count"], counts["sample_count"]
    return {**counts, "precision": tp / predicted if predicted else None,
            "recall": tp / positive if positive else None,
            "precision_95ci": wilson(tp, predicted), "recall_95ci": wilson(tp, positive),
            "abstention_rate": counts["abstention_count"] / n if n else None,
            "coverage": (n - counts["abstention_count"]) / n if n else None}


def _slice(records, predictions, topic_ids):
    per_topic = {}
    abstained_records = set()
    for topic_id in topic_ids:
        counts = {key: 0 for key in ("sample_count", "positive_count", "predicted_positive_count",
            "true_positive_count", "false_positive_count", "true_negative_count", "false_negative_count",
            "abstention_count", "missing_prediction_count")}
        for record, prediction in zip(records, predictions, strict=True):
            counts["sample_count"] += 1
            positive = topic_id in record["expected_topic_ids"]
            counts["positive_count"] += positive
            result = next((r for r in prediction["topic_results"] if r["topic_id"] == topic_id), None)
            missing = result is None
            abstained = missing or result["abstained"]
            if prediction["abstained"] or abstained:
                abstained_records.add(record["post"]["post_id"])
            counts["missing_prediction_count"] += missing
            counts["abstention_count"] += abstained
            if abstained:
                # End-to-end recall counts a missed positive even on abstention.
                # Abstained negatives are NOT true negatives.
                counts["false_negative_count"] += positive
                continue
            match = result["match"]
            counts["predicted_positive_count"] += match
            counts["true_positive_count"] += match and positive
            counts["false_positive_count"] += match and not positive
            counts["false_negative_count"] += not match and positive
            counts["true_negative_count"] += not match and not positive
        per_topic[topic_id] = _finish(counts)
    micro_counts = {key: sum(t[key] for t in per_topic.values()) for key in
                    ("sample_count", "positive_count", "predicted_positive_count", "true_positive_count",
                     "false_positive_count", "true_negative_count", "false_negative_count",
                     "abstention_count", "missing_prediction_count")}
    n = len(records)
    return {"sample_count": n, "abstention_count": len(abstained_records),
            "abstention_rate": len(abstained_records) / n if n else None,
            "per_topic": per_topic, "micro": _finish(micro_counts)}


def summarize(records: list[dict[str, Any]], predictions: list[dict[str, Any]], topic_ids: list[str]) -> dict[str, Any]:
    if len(records) != len(predictions) or len(topic_ids) != len(set(topic_ids)):
        raise ValueError("invalid_evaluation_prediction")
    for record, prediction in zip(records, predictions, strict=True):
        validate_contract("Classification", prediction)
        ids = [r["topic_id"] for r in prediction["topic_results"]]
        if (prediction["post_id"] != record["post"]["post_id"] or len(ids) != len(set(ids))
                or not set(ids) <= set(topic_ids)):
            raise ValueError("invalid_evaluation_prediction")
    result = _slice(records, predictions, topic_ids)
    result["language_slices"] = {}
    for language in LANGUAGES:
        indices = [i for i, r in enumerate(records) if r["language"] == language]
        if indices:
            result["language_slices"][language] = _slice([records[i] for i in indices],
                [predictions[i] for i in indices], topic_ids)
    result["uncertainty"] = {"method": "two_sided_95_percent_wilson_no_continuity_correction",
        "limitations": "Assumes independent samples; correlated posts or micro topic-label pairs may understate uncertainty. Does not cover label error, sampling bias, topic selection or distribution shift."}
    return result
