"""Bounded, explicit held-out JSONL datasets; real user data stays outside Git."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any, TypeAlias

from threadrelay.contracts import validate_contract

Dataset: TypeAlias = dict[str, Any]
FORMAT = "threadrelay-evaluation-v1"
MAX_LINE_BYTES = 1_048_576
MAX_FILE_BYTES = 64 * 1024 * 1024


def digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                         separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def validate_dataset(dataset: Dataset) -> None:
    validate_contract("Dataset", dataset)
    if "topics" not in dataset or "synthetic" not in dataset:
        raise ValueError("evaluation_topics_and_provenance_required")
    if dataset["split"] != "test":
        raise ValueError("held_out_test_split_required")
    if not dataset["records"]:
        raise ValueError("empty_evaluation_dataset")
    topics = dataset["topics"]
    ids = [t["id"] for t in topics]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate_topic_id")
    enabled = {t["id"] for t in topics if t["enabled"]}
    if not enabled:
        raise ValueError("enabled_evaluation_topic_required")
    examples = {" ".join(text.split()) for t in topics
                for key in ("positive_examples", "negative_examples") for text in t[key]}
    seen_ids, seen_hashes, seen_content = set(), set(), set()
    for record in dataset["records"]:
        post = record["post"]
        # Exact duplicate text/quote evidence cannot inflate the sample count,
        # even if a caller supplied a different ID or content_hash.
        quote = post["quote"]
        normalized_text = " ".join(post["text"].split())
        normalized_quote = None if quote is None else {
            "author": quote["author"], "text": " ".join(quote["text"].split())}
        content = digest({"text": normalized_text, "quote": normalized_quote})
        if (post["post_id"] in seen_ids or post["content_hash"] in seen_hashes or content in seen_content):
            raise ValueError("duplicate_evaluation_record")
        seen_ids.add(post["post_id"])
        seen_hashes.add(post["content_hash"])
        seen_content.add(content)
        if not set(record["expected_topic_ids"]) <= enabled:
            raise ValueError("unknown_expected_topic")
        if normalized_text in examples or (quote is not None and normalized_quote["text"] in examples):
            raise ValueError("topic_example_leaks_test_record")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def load_dataset(path: str | Path) -> Dataset:
    """First JSONL line is format/split/topics/synthetic; others are DatasetRecord.

    No random splitting, missing-label imputation, model access or text export.
    The caller must separately audit provenance and cross-dataset/train overlap.
    """
    try:
        items, size = [], 0
        with Path(path).open("rb") as stream:
            while line := stream.readline(MAX_LINE_BYTES + 1):
                size += len(line)
                if len(line) > MAX_LINE_BYTES or size > MAX_FILE_BYTES or len(items) > 100_000:
                    raise ValueError("dataset_limit")
                items.append(json.loads(line.decode("utf-8"), object_pairs_hook=_pairs,
                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite"))))
        if not items or not isinstance(items[0], dict):
            raise ValueError("missing_header")
        header = items[0]
        if (set(header) != {"format", "split", "topics", "synthetic"} or header["format"] != FORMAT):
            raise ValueError("invalid_header")
        dataset = {key: deepcopy(header[key]) for key in ("split", "topics", "synthetic")}
        dataset["records"] = items[1:]
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise ValueError("invalid_evaluation_file") from None
    validate_dataset(dataset)
    return dataset
