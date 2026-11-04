#!/usr/bin/env python3
"""Fixed source/offline bundle assembly; no downloads, native execution or proof promotion."""

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
from packaging_common import (
    check_interpreter_identity,
    digest,
    inventory_tree,
    stage_tree,
)

ROOT = Path(__file__).resolve().parents[1]


def package_plan(target):
    if target not in ("windows-x64", "macos-arm64"):
        raise ValueError("Unsupported package target")
    windows = target == "windows-x64"
    return {
        "target": target,
        "freezer": "PyInstaller==6.22.3",
        "hooks": "2026.9",
        "builder_lock": "packaging/uv.lock",
        "worker_lock": "worker/uv.lock",
        "console": True,
        "onefile": False,
        "upx": False,
        "worker": "sidecars/threadrelay-worker" + (".exe" if windows else ""),
        "evaluator": "threadrelay-evaluate.exe"
        if windows
        else "Contents/MacOS/threadrelay-evaluate",
        "driver": "native/playwright/node" + (".exe" if windows else ""),
        "driver_entry": "native/threadrelay/owned-driver.cjs",
        "browser": "omitted_pending_terms_and_exact_artifact_review",
        "model_weights": "not bundled",
        "native_acceptance": "Not run",
    }


def identity_receipt(root, target):
    files = inventory_tree(root)
    return {
        "schema_version": 1,
        "platform": target,
        "result": "identity_only",
        "native_acceptance": "Not run",
        "entries": files,
        "measured_files": sum(e["kind"] == "file" for e in files),
        "measured_bytes": sum(e.get("size", 0) for e in files),
    }


def target_host(target):
    expected = ("Windows", "AMD64") if target == "windows-x64" else ("Darwin", "arm64")
    if (platform.system(), platform.machine()) != expected:
        raise ValueError("Build requires exact native target")
    if sys.version_info[:2] != (3, 12):
        raise ValueError("Pinned Python 3.12 required")


def freeze(target, output):
    target_host(target)
    pin = json.loads((ROOT / "runtimes/package-manifest.json").read_text())["targets"][
        target
    ]
    check_interpreter_identity(pin, Path(sys.executable), platform.python_version())
    if (
        importlib.metadata.version("pyinstaller") != "6.22.3"
        or importlib.metadata.version("pyinstaller-hooks-contrib") != "2026.9"
    ):
        raise ValueError("Run through hash-locked packaging environment")
    # PyInstaller specs are fixed project code; no arbitrary spec or hook CLI.
    output = Path(output).resolve()
    output.mkdir(parents=False)
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        in {
            "PATH",
            "SystemRoot",
            "WINDIR",
            "HOME",
            "USERPROFILE",
            "TEMP",
            "TMP",
            "TMPDIR",
        }
    }
    env["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] = "1"
    env["PYTHONHASHSEED"] = "0"
    # --clean must only touch this new build's cache, never the user's global
    # PyInstaller cache or a caller-selected directory.
    cache = output / "pyinstaller-cache"
    cache.mkdir(mode=0o700)
    env["PYINSTALLER_CONFIG_DIR"] = str(cache)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--clean",
            "--noconfirm",
            "--distpath",
            str(output / "dist"),
            "--workpath",
            str(output / "work"),
            str(ROOT / "packaging/worker.spec"),
        ],
        check=True,
        cwd=ROOT,
        env=env,
    )


def assemble(target, worker, evaluator, notices, output):
    target_host(target)
    plan = package_plan(target)
    worker, evaluator, notices, output = map(Path, (worker, evaluator, notices, output))
    if output.exists():
        raise ValueError("Output must be new; interrupted packages never activated")
    suffix = ".exe" if target == "windows-x64" else ""
    if not (worker / ("threadrelay-worker" + suffix)).is_file():
        raise ValueError("Console worker unavailable")
    internal = worker / "_internal"
    required = [
        "threadrelay/protocol.schema.json",
        "threadrelay/inference/manifest.json",
        "threadrelay/storage/migrations",
        "jsonschema_specifications/schemas",
        "playwright/driver/package/cli.js",
        "playwright/driver/LICENSE",
    ]
    if any(not (internal / p).exists() for p in required):
        raise ValueError("Frozen resources incomplete")
    # The existing private-API compatibility module verifies every Python
    # fingerprint at use; these fixed driver sources are verified here too.
    package = internal / "playwright/driver/package"
    if (
        json.loads((package / "package.json").read_text())["version"]
        != "1.64.0-beta-1791568591000"
    ):
        raise ValueError("Driver version drift")
    if (
        digest(package / "lib/coreBundle.js")
        != "829521b67ed7be90c7d6d6098ff39f2241562d47e4ad7ccab13710924a56001d"
    ):
        raise ValueError("Driver source drift")
    if (
        not (notices / "THIRD_PARTY_NOTICES.md").is_file()
        or not (notices / "sbom.json").is_file()
    ):
        raise ValueError("Measured license/SBOM package required")
    sbom = json.loads((notices / "sbom.json").read_text())
    if (
        sbom.get("scope") != "complete-target-offline-build-and-runtime"
        or sbom.get("target") != target
    ):
        raise ValueError("Complete target notice collection required")
    if {e["path"]: e for e in inventory_tree(notices) if e["path"] != "sbom.json"} != {
        e["path"]: e for e in sbom.get("notice_inventory", [])
    }:
        raise ValueError("Notice inventory mismatch")
    names = {c["name"].lower() for c in sbom.get("components", [])}
    if (
        not {
            "python-interpreter",
            "playwright-driver",
            "playwright",
            "pyinstaller",
            "pyinstaller-hooks-contrib",
            "frontend",
            "jsonschema",
        }
        <= names
    ):
        raise ValueError("Required target notices missing")
    for entry in inventory_tree(worker):
        path = Path(entry["path"])
        if path.suffix.lower() in {".db", ".sqlite", ".gguf", ".key", ".log"} or any(
            part.lower()
            in {"profiles", "browser-profiles", "secrets", ".env", "pytest"}
            for part in path.parts
        ):
            raise ValueError("Private or test material in worker bundle")
    if not evaluator.is_file() or evaluator.is_symlink():
        raise ValueError("Fixed console evaluator unavailable")
    output.mkdir(mode=0o700)
    resources = output if target == "windows-x64" else output / "Contents/Resources"
    resources.mkdir(parents=True, exist_ok=True)
    stage_tree(worker, resources / "sidecars", inventory_tree(worker))
    (resources / "native").mkdir()
    stage_tree(
        internal / "playwright/driver",
        resources / "native/playwright",
        inventory_tree(internal / "playwright/driver"),
    )
    (resources / "native/threadrelay").mkdir()
    (resources / "native/threadrelay/owned-driver.cjs").write_bytes(
        (ROOT / "apps/desktop/src-tauri/native/owned-driver.cjs").read_bytes()
    )
    stage_tree(notices, resources / "licenses", inventory_tree(notices))
    # Evaluate lives beside the trusted host, outside the resource inventory
    # to avoid a recursive hash of a host embedding its own compiled catalog.
    entry = output / plan["evaluator"]
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_bytes(evaluator.read_bytes())
    entry.chmod(0o500)
    receipt = identity_receipt(resources, target)
    if target == "windows-x64":
        receipt["entries"] = [
            e for e in receipt["entries"] if e["path"] != "threadrelay-evaluate.exe"
        ]
    # Candidate is intentionally beside, not inside, the measured resource tree.
    receipt["evaluator_sha256"] = digest(entry)
    receipt["plan"] = plan
    output.with_name(output.name + "-identity-candidate.json").write_text(
        json.dumps(receipt, indent=2) + "\n"
    )
    return receipt


def tauri_overlay(target, staged):
    package_plan(target)
    staged = Path(staged).absolute()
    resources = staged if target == "windows-x64" else staged / "Contents/Resources"
    mapping = {
        str(resources / name): name for name in ("sidecars", "native", "licenses")
    }
    bundle = {"active": True, "resources": mapping}
    if target == "windows-x64":
        mapping[str(staged / "threadrelay-evaluate.exe")] = "threadrelay-evaluate.exe"
    else:
        bundle["macOS"] = {
            "files": {
                "MacOS/threadrelay-evaluate": str(
                    staged / "Contents/MacOS/threadrelay-evaluate"
                )
            },
            "signingIdentity": None,
            "minimumSystemVersion": "14.0",
        }
    return {"build": {"features": ["desktop"]}, "bundle": bundle}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target", choices=["windows-x64", "macos-arm64"], required=True
    )
    parser.add_argument("--validate-plan", action="store_true")
    parser.add_argument("--freeze", action="store_true")
    parser.add_argument("--assemble", action="store_true")
    parser.add_argument(
        "--tauri-config",
        type=Path,
        help="Write fixed build overlay for --output staged tree; does not run bundling",
    )
    parser.add_argument(
        "--measure",
        type=Path,
        help="Measure FINAL target resource tree after rewriting/signing; no proof promotion",
    )
    for name in ("worker", "evaluator", "notices", "output"):
        parser.add_argument("--" + name, type=Path)
    args = parser.parse_args(argv)
    if args.tauri_config:
        if not args.output:
            parser.error("Staged --output root required")
        with args.tauri_config.open("x") as stream:
            json.dump(tauri_overlay(args.target, args.output), stream, indent=2)
    elif args.measure:
        if not args.output:
            parser.error("New identity --output JSON required")
        target_host(args.target)
        with args.output.open("x") as stream:
            json.dump(identity_receipt(args.measure, args.target), stream, indent=2)
    elif args.freeze:
        if args.assemble or not args.output:
            parser.error("Select freeze and a new output directory")
        freeze(args.target, args.output)
    elif args.assemble:
        if not all((args.worker, args.evaluator, args.notices, args.output)):
            parser.error("Complete fixed inputs required")
        assemble(args.target, args.worker, args.evaluator, args.notices, args.output)
    else:
        print(json.dumps(package_plan(args.target), indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"Package unavailable: {error}") from None
