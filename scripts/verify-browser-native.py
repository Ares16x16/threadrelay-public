#!/usr/bin/env python3
"""Prepare/verify the pinned local native browser integration fixture.

This script creates development identity inputs and raw observations. It never
writes production acceptance documents, inventories, or capabilities. The GUI
run must be explicitly coordinated by the operator.
"""
from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import plistlib
import shutil
import secrets
import stat
import subprocess
import sys
import urllib.request
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
CACHE = PROJECT / ".cache/native-browser"
ARCHIVE = CACHE / "downloads/chrome-mac-arm64-156.0.8078.4.zip"
URL = "https://cdn.playwright.dev/builds/cft/156.0.8078.4/mac-arm64/chrome-mac-arm64.zip"
SHA256 = "987d2f8499c87d21676f78178550cfcbfb1bb53786821e2cff46c5a7e99b58be"
SIZE = 194831853
BROWSER_VERSION = "156.0.8078.4"
BROWSER_REVISION = "1248"
DEPS = PROJECT / ".cache/venvs/worker-3.12.16/lib/python3.12/site-packages"
PYTHON = PROJECT / ".cache/build-inputs/install/python-3.12.16/bin/python3.12"
RESOURCES = CACHE / "resources"
APP_NAME = "Google Chrome for Testing.app"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def private_directory(path: Path) -> None:
    if not path.is_relative_to(PROJECT):
        raise ValueError("Path escapes project")
    for parent in reversed(path.parents):
        if parent == PROJECT or parent.is_relative_to(PROJECT):
            if parent.is_symlink():
                raise ValueError("Symlink project cache ancestor")
    if path.is_symlink():
        raise ValueError("Symlink project cache")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        raise ValueError("Invalid project directory")


def write_json(path: Path, value: object) -> None:
    if path.is_symlink():
        raise ValueError("Symlink output")
    private_directory(path.parent)
    temporary = path.with_suffix(path.suffix + ".new")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
    temporary.replace(path)


def clean_env() -> dict[str, str]:
    return {"TMPDIR": str(PROJECT / ".cache/tmp"), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}


def extended_attributes(path: Path) -> dict[str, str]:
    # This project-built Python may omit os.listxattr on Darwin. Use the native
    # no-follow API and record value hashes, preserving warning metadata.
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    library.listxattr.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    library.listxattr.restype = ctypes.c_ssize_t
    library.getxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
    library.getxattr.restype = ctypes.c_ssize_t
    encoded = os.fsencode(path)
    size = library.listxattr(encoded, None, 0, 1)
    if size < 0:
        raise OSError(ctypes.get_errno(), "Native attribute readback unavailable")
    names = ctypes.create_string_buffer(size)
    if library.listxattr(encoded, names, size, 1) != size:
        raise ValueError("Native attributes changed")
    output = {}
    for name in names.raw.split(b"\0"):
        if not name:
            continue
        length = library.getxattr(encoded, name, None, 0, 0, 1)
        if length < 0 or length > 1024 * 1024:
            raise ValueError("Native attribute value unavailable")
        value = ctypes.create_string_buffer(length)
        if library.getxattr(encoded, name, value, length, 0, 1) != length:
            raise ValueError("Native attribute value changed")
        output[os.fsdecode(name)] = hashlib.sha256(value.raw).hexdigest()
    return output


def verify_archive(path: Path) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size != SIZE or digest(path) != SHA256:
        raise ValueError("Pinned official archive identity mismatch")


def archive_manifest(path: Path) -> dict:
    sys.path.insert(0, str(PROJECT / "scripts"))
    from packaging_common import validate_inventory

    entries = []
    with zipfile.ZipFile(path) as archive:
        seen = set()
        total = 0
        for item in archive.infolist():
            name = PurePosixPath(item.filename.rstrip("/"))
            if not name.parts or name.parts[0] != "chrome-mac-arm64" or name.is_absolute():
                raise ValueError("Unexpected archive root")
            if ".." in name.parts or "\\" in item.filename or str(name).lower() in seen:
                raise ValueError("Archive path/alias rejected")
            seen.add(str(name).lower())
            mode = item.external_attr >> 16
            total += item.file_size
            if len(seen) > 100_000 or total > 4 * 1024**3:
                raise ValueError("Archive bounds exceeded")
            if item.is_dir():
                entry = {"kind": "directory", "path": str(name)}
            elif stat.S_ISLNK(mode):
                entry = {"kind": "symlink", "path": str(name), "target": archive.read(item).decode("ascii")}
            elif stat.S_ISREG(mode) or stat.S_IFMT(mode) == 0:
                entry = {"kind": "file", "path": str(name), "size": item.file_size,
                         "sha256": "0" * 64, "executable": bool(mode & 0o111)}
            else:
                raise ValueError("Special archive object rejected")
            entries.append(entry)
    explicit = {value["path"] for value in entries}
    implicit = {str(parent) for value in entries for parent in PurePosixPath(value["path"]).parents
                if str(parent) != "." and str(parent) not in explicit}
    entries.extend({"kind": "directory", "path": name} for name in sorted(implicit))
    # Hash placeholders are used only for archive path/link validation. The
    # extracted complete inventory below hashes every real regular file.
    validate_inventory(entries)
    return {"entries": len(entries), "unpacked_bytes": total,
            "symlinks": [value for value in entries if value["kind"] == "symlink"]}


def download() -> None:
    private_directory(CACHE / "downloads")
    if ARCHIVE.exists():
        verify_archive(ARCHIVE)
        return
    partial = ARCHIVE.with_suffix(".zip.partial")
    if not partial.exists():
        class OfficialRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                from urllib.parse import urlsplit
                parsed = urlsplit(newurl)
                if parsed.scheme != "https" or parsed.hostname not in {"cdn.playwright.dev", "storage.googleapis.com"}:
                    raise ValueError("Unofficial browser redirect")
                return super().redirect_request(req, fp, code, msg, headers, newurl)

        opener = urllib.request.build_opener(OfficialRedirect())
        with opener.open(URL, timeout=60) as response, partial.open("xb") as output:
            count = 0
            while chunk := response.read(1024 * 1024):
                count += len(chunk)
                if count > SIZE:
                    raise ValueError("Browser download size exceeded")
                output.write(chunk)
    verify_archive(partial)
    partial.rename(ARCHIVE)


def prepare() -> None:
    if sys.version_info[:3] != (3, 12, 16) or platform.system() != "Darwin" or platform.machine() != "arm64":
        raise ValueError("Requires project Python 3.12.16 on macOS arm64")
    os.umask(0o077)
    for directory in [CACHE, CACHE / "downloads", CACHE / "evidence", RESOURCES / "native", PROJECT / ".cache/tmp"]:
        private_directory(directory)
    download()
    archive_info = archive_manifest(ARCHIVE)
    browser = RESOURCES / "native/browser"
    if not (browser / APP_NAME).exists():
        if browser.exists():
            browser.rmdir()  # only an empty directory made for this fixture
        unpacked = CACHE / "downloads/unpacked"
        if unpacked.exists():
            raise ValueError("Prior extraction is retained; inspect it before reuse")
        private_directory(unpacked)
        subprocess.run(["/usr/bin/ditto", "-x", "-k", str(ARCHIVE), str(unpacked)], check=True, env=clean_env())
        (unpacked / "chrome-mac-arm64").rename(browser)
    sys.path.insert(0, str(PROJECT / "scripts"))
    from packaging_common import inventory_tree

    original_driver = DEPS / "playwright/driver"
    local_driver = RESOURCES / "native/playwright"
    if not local_driver.exists():
        subprocess.run(["/usr/bin/ditto", str(original_driver), str(local_driver)], check=True, env=clean_env())
    driver_inventory = inventory_tree(original_driver)
    if inventory_tree(local_driver) != driver_inventory:
        raise ValueError("Complete Node driver tree identity drift")
    for item in driver_inventory:
        original_attrs = extended_attributes(original_driver / item["path"])
        copied_attrs = extended_attributes(local_driver / item["path"])
        if any(copied_attrs.get(name) != value for name, value in original_attrs.items()):
            raise ValueError("Node driver metadata was not preserved")
    entry_root = RESOURCES / "native/threadrelay"
    private_directory(entry_root)
    entry = entry_root / "owned-driver.cjs"
    source_entry = PROJECT / "apps/desktop/src-tauri/native/owned-driver.cjs"
    if not entry.exists():
        shutil.copy2(source_entry, entry, follow_symlinks=False)
    if digest(entry) != digest(source_entry):
        raise ValueError("Owned driver source drift")
    app = browser / APP_NAME
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    if info["CFBundleShortVersionString"] != BROWSER_VERSION:
        raise ValueError("Browser version drift")
    package = json.loads((local_driver / "package/package.json").read_text())
    if package["version"] != "1.64.0-beta-1791568591000":
        raise ValueError("Node driver package drift")
    node = subprocess.check_output([str(local_driver / "node"), "--version"], env=clean_env(), text=True).strip()
    if node != "v24.22.1":
        raise ValueError("Node version drift")
    verify = subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
                            capture_output=True, text=True, env=clean_env())
    details = subprocess.run(["/usr/bin/codesign", "--display", "--verbose=4", str(app)],
                             capture_output=True, text=True, env=clean_env())
    (CACHE / "evidence/codesign-verification.txt").write_text(verify.stdout + verify.stderr)
    (CACHE / "evidence/codesign-details.txt").write_text(details.stdout + details.stderr)
    inventory = inventory_tree(RESOURCES)
    write_json(CACHE / "evidence/resource-inventory.json", inventory)
    attributes = []
    for entry in inventory:
        path = RESOURCES / entry["path"]
        names = extended_attributes(path)
        if names:
            attributes.append({"path": entry["path"], "attributes": names})
    write_json(CACHE / "evidence/resource-attributes.json", attributes)
    receipt = {
        "schema_version": 1, "purpose": "local-development-browser-identity", "result": "identity_only",
        "native_acceptance": "not_run", "independent_review": "not_run",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_commit": subprocess.check_output(["/usr/bin/git", "rev-parse", "HEAD"], cwd=PROJECT, text=True).strip(),
        "official_download_url": URL,
        "archive_sha256": SHA256, "archive_size": SIZE,
        "browser_version": BROWSER_VERSION, "browser_revision": BROWSER_REVISION,
        "browser_scm_revision": info["SCMRevision"],
        "node_version": node, "playwright_core_version": package["version"],
        "python_version": platform.python_version(), "python_sha256": digest(PYTHON),
        "resource_inventory_sha256": digest(CACHE / "evidence/resource-inventory.json"),
        "resource_inventory_entries": len(inventory),
        "resource_file_bytes": sum(value.get("size", 0) for value in inventory),
        "archive_manifest": archive_info, "codesign_verify_exit": verify.returncode,
        "security_metadata": "Retained. No quarantine removal, re-signing, sandbox bypass, or warning suppression.",
        "scope": "Project-local source integration fixture. No accounts, credentials, live feed, model, or send.",
        "production_catalogs_modified": False,
    }
    write_json(CACHE / "evidence/identity-receipt.json", receipt)
    print(json.dumps({"result": "identity_only", "inventory_entries": len(inventory),
                      "inventory_bytes": receipt["resource_file_bytes"], "signature_verified": verify.returncode == 0,
                      "signature_warning_retained": verify.returncode != 0,
                      "evidence": str(CACHE / "evidence/identity-receipt.json")}))


SCENARIOS = ["identity_drift", "partial_spawn", "normal_close", "browser_loss", "driver_loss", "worker_loss", "malformed_frame"]


def source_identity() -> dict:
    files = [*sorted((PROJECT / "apps/desktop/src-tauri/src").glob("*.rs")),
             *sorted((PROJECT / "apps/desktop/src-tauri/native").glob("*.c")),
             *sorted((PROJECT / "apps/desktop/src-tauri/native").glob("*.json")),
             *sorted((PROJECT / "apps/desktop/src-tauri/native").glob("*.cjs")),
             *sorted((PROJECT / "worker/src/threadrelay").rglob("*.py")),
             *sorted((PROJECT / "worker/src/threadrelay").rglob("*.json")),
             PROJECT / "scripts/verify-browser-native.py", PROJECT / "scripts/verify-browser-worker.py",
             PROJECT / "apps/desktop/src-tauri/Cargo.toml", PROJECT / "apps/desktop/src-tauri/Cargo.lock",
             PROJECT / "apps/desktop/src-tauri/build.rs", PROJECT / "runtimes/package-manifest.json",
             PROJECT / "runtimes/tdlib-manifest.json"]
    return {"commit": subprocess.check_output(["/usr/bin/git", "rev-parse", "HEAD"], cwd=PROJECT, text=True).strip(),
            "source_files": {str(path.relative_to(PROJECT)): digest(path) for path in files},
            "python_sha256": digest(PYTHON), "python_version": platform.python_version()}


def make_fixture(scenario: str) -> tuple[Path, dict]:
    os.umask(0o077)
    receipt = json.loads((CACHE / "evidence/identity-receipt.json").read_text())
    inventory = CACHE / "evidence/resource-inventory.json"
    if receipt["archive_sha256"] != SHA256 or digest(inventory) != receipt["resource_inventory_sha256"]:
        raise ValueError("Prepared browser identity drift")
    private_directory(CACHE / "runs")
    run_id = scenario + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(4)
    run_root = CACHE / "runs" / run_id
    # Exact pinned macOS Chromium SCM f842f52 permits 253 sun_path bytes.
    # Refuse before spawn instead of allowing any external temp fallback.
    socket_template = run_root / "native-runtime/browser/tmp/com.google.chrome.for.testing.XXXXXX/SingletonSocket"
    if len(os.fsencode(socket_template)) > 253:
        raise ValueError("Pinned macOS Chrome singleton socket path exceeds 253 bytes")
    run_root.mkdir(mode=0o700)
    fixture = {"schema_version": 1, "project": str(PROJECT), "resources": str(RESOURCES),
               "inventory": str(inventory), "inventory_sha256": digest(inventory), "archive_sha256": SHA256,
               "python": str(PYTHON), "dependencies": str(DEPS),
               "worker_script": str(PROJECT / "scripts/verify-browser-worker.py"),
               "output": str(CACHE / "evidence" / (run_id + ".json")),
               "run_root": str(run_root), "scenario": scenario}
    path = CACHE / "evidence" / (run_id + ".fixture.json")
    write_json(path, fixture)
    return path, fixture


def run_native(binary: Path, scenario: str, visible: bool) -> None:
    if not visible:
        raise ValueError("Coordinated sole visible browser session requires --visible")
    binary = binary.absolute()
    binary_roots = [PROJECT / ".cache/cargo-target" / profile / "deps" for profile in ["debug", "release"]]
    if binary.is_symlink() or not binary.is_file() or not any(binary.is_relative_to(root) for root in binary_roots):
        raise ValueError("Use the coordinated project-local compiled Rust test binary")
    selected = SCENARIOS if scenario == "all" else [scenario]
    for selected_scenario in selected:
        fixture_path, fixture = make_fixture(selected_scenario)
        output = Path(fixture["output"])
        log_path = output.with_suffix(".test-log.txt")
        inputs = source_identity()
        binary_sha256 = digest(binary)
        environment = clean_env() | {"THREADRELAY_BROWSER_NATIVE_FIXTURE": str(fixture_path)}
        args = [str(binary), "browser_acceptance::visible_owned_browser_pipeline", "--exact", "--ignored", "--nocapture", "--test-threads=1"]
        timed_out = False
        with log_path.open("x") as log:
            child = subprocess.Popen(args, cwd=PROJECT, env=environment, stdout=log, stderr=subprocess.STDOUT,
                                     start_new_session=True)
            try:
                result = child.wait(timeout=240)
            except subprocess.TimeoutExpired:
                timed_out = True
                child.kill()  # this exact runner-owned process only; native death guards own its children
                result = child.wait(timeout=10)
        after = source_identity()
        binary_unchanged = binary_sha256 == digest(binary)
        record = {"schema_version": 1, "purpose": "browser-development-integration-runner", "result": "observed" if result == 0 and inputs == after and binary_unchanged else "failed",
                  "native_acceptance": "not_minted", "scenario": selected_scenario, "test_binary_sha256": binary_sha256,
                  "test_binary_unchanged_during_run": binary_unchanged,
                  "command": args, "fixture": str(fixture_path), "exit_code": result, "timed_out": timed_out,
                  "source_unchanged_during_run": inputs == after, "inputs": inputs,
                  "raw_observation": str(output), "test_log": str(log_path),
                  "timeout_policy": "Only runner-owned host is killed; uncertain snapshots/cleanup are retained for review."}
        write_json(output.with_suffix(".runner.json"), record)
        print(json.dumps({"scenario": selected_scenario, "result": record["result"], "exit_code": result,
                          "evidence": str(output), "log": str(log_path)}), flush=True)
        if record["result"] != "observed":
            raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "fixture", "run"])
    parser.add_argument("--scenario", choices=[*SCENARIOS, "all"], default="normal_close")
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--visible", action="store_true")
    args = parser.parse_args()
    if args.action == "prepare":
        prepare()
    elif args.action == "fixture":
        if args.scenario == "all":
            raise ValueError("A single scenario is required for fixture creation")
        path, _ = make_fixture(args.scenario)
        print(path)
    else:
        if args.binary is None:
            raise ValueError("Explicit coordinated Rust test binary required")
        run_native(args.binary, args.scenario, args.visible)


if __name__ == "__main__":
    main()
