#!/usr/bin/env python3
"""Fixed synthetic source worker for the test-only native inference harness."""

from __future__ import annotations
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import sys
import time
import traceback


def send(value):
    print(json.dumps(value, separators=(",", ":")), flush=True)


def command():
    line = sys.stdin.buffer.readline(65537)
    if not line or len(line) > 65536:
        raise EOFError("Bounded fixture command unavailable")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError("Fixture command unavailable")
    return value


def main():
    if len(sys.argv) != 5 or sys.version_info[:3] != (3, 12, 16):
        raise ValueError("Pinned fixture required")
    source, deps, project, root = (Path(value) for value in sys.argv[1:])
    if source != project / "worker/src" or not root.is_relative_to(
        project / ".cache/llama-native/runs"
    ):
        raise ValueError("Fixed local fixture paths required")
    sys.path[:0] = [str(source), str(deps)]
    from threadrelay.broker_channel import HostControl
    from threadrelay.inference.native_launcher import NativeInferenceLauncher
    from threadrelay.inference.provision import ModelProvisioner
    from threadrelay.inference.runtime import DecisionRuntime
    from threadrelay.inference.decision import Classifier

    runtime = None
    control = HostControl(socket.socket(fileno=4), generation=1, root=root)
    original_exchange = control._exchange

    def diagnostic_exchange(kind, *args, **kwargs):
        value = original_exchange(kind, *args, **kwargs)
        if kind == "StartPinnedInference":
            print(json.dumps({"start_response": value}), file=sys.stderr, flush=True)
        return value

    control._exchange = diagnostic_exchange
    send(
        {"event": "worker_ready", "pid": os.getpid(), "python": sys.version.split()[0]}
    )
    try:
        if command() != {"command": "start"}:
            raise ValueError("Fixture start required")
        p = ModelProvisioner()
        model = root / "models/h2o-lightning-4b-Q4_K_M.gguf"

        runtime = DecisionRuntime(
            model_file=model,
            launcher=NativeInferenceLauncher(root, control),
            setup_authorized=lambda: True,
            can_infer=lambda: True,
        )
        mark = time.monotonic()
        runtime.start(p.primary_manifest)
        startup_seconds = time.monotonic() - mark
        unauthenticated = http.client.HTTPConnection(
            "127.0.0.1", runtime._lease.port, timeout=5
        )
        try:
            unauthenticated.connect()
            if not runtime._lease.verify_connection(unauthenticated.sock):
                raise ValueError("Unauthenticated probe peer unverified")
            unauthenticated.request("GET", "/props", headers={"Connection": "close"})
            response = unauthenticated.getresponse()
            if response.status != 401 or len(response.read(65537)) > 65536:
                raise ValueError("Unauthenticated request accepted")
        finally:
            unauthenticated.close()
        send(
            {
                "event": "runtime_ready",
                "startup_seconds": startup_seconds,
                "owned_port": runtime._lease.port,
                "missing_auth_status": 401,
            }
        )
        if command() != {"command": "classify"}:
            raise ValueError("Fixture classification required")
        classifier = Classifier(runtime)
        topics = [
            {
                "id": "synthetic-ai-software",
                "version": 1,
                "enabled": True,
                "description": "Posts about artificial intelligence models or software development. 人工智慧模型或軟體開發。",
                "positive_examples": [
                    "An open source language model",
                    "Software debugging tools",
                ],
                "negative_examples": ["Breakfast recipes", "Walking in a park"],
                "exclusions": ["Food and personal leisure"],
            }
        ]
        template = next(
            c["value"]
            for c in json.loads((project / "contracts/fixtures.json").read_text())[
                "valid"
            ]
            if c["name"] == "ObservedPost"
        )
        observations = []
        for name, text in [
            (
                "en-ai",
                "A new open source AI language model improves code generation and reasoning.",
            ),
            (
                "en-neutral",
                "I baked banana bread for breakfast and watered the garden.",
            ),
            ("zh-ai", "今天發布新的開源人工智慧語言模型，可以協助程式開發與除錯。"),
            ("zh-neutral", "今天早餐吃香蕉麵包，下午到公園散步。"),
        ]:
            post = {
                **template,
                "post_id": "synthetic-" + name,
                "text": text,
                "quote": None,
                "quality_flags": [],
                "content_hash": hashlib.sha256(text.encode()).hexdigest(),
            }
            mark = time.monotonic()
            bundle = classifier.classify_bound(post, topics)
            value = bundle.for_snapshot(post, topics, classifier)
            if classifier.last_reason not in {None, "uncertain_decision"}:
                raise ValueError(
                    "Synthetic inference abstained: "
                    + str(classifier.last_reason)
                    + "; case="
                    + name
                )
            observations.append(
                {
                    "name": name,
                    "classification": value,
                    "seconds": time.monotonic() - mark,
                    "snapshot_key": bundle.context.snapshot_key,
                    "reason": classifier.last_reason,
                }
            )
        send(
            {
                "event": "classified",
                "synthetic_only": True,
                "observations": observations,
                "metadata": classifier.metadata,
            }
        )
        while True:
            value = command()
            if value == {"command": "close"}:
                result = runtime.stop()
                send({"event": "runtime_closed", "report": result})
            elif value == {"command": "probe_loss"}:
                result = classifier.classify(
                    {**template, "quote": None, "quality_flags": []}, topics
                )
                send(
                    {
                        "event": "loss_observed",
                        "abstained": result["abstained"],
                        "reason": classifier.last_reason,
                    }
                )
            elif value == {"command": "exit"}:
                send({"event": "worker_exiting"})
                return
            else:
                raise ValueError("Unknown fixture command")
    finally:
        if runtime is not None:
            runtime.stop()
        control.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc(file=sys.stderr)
        send({"event": "fixture_error"})
        sys.exit(2)
