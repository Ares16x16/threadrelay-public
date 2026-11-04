#!/usr/bin/env python3
"""Prepare/run opt-in real native inference fixture; never changes trust catalogs."""

from __future__ import annotations
import argparse
import ctypes
import errno
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / ".cache/llama-native"
MODEL = BASE / "models/h2o-lightning-4b-Q4_K_M.gguf"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def prepare(scenario):
    os.umask(0o077)
    spec = importlib.util.spec_from_file_location(
        "preflight", BASE / "probe_no_weights.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    verified = module.verified_archive_install()
    if (
        MODEL.is_symlink()
        or MODEL.stat().st_size != 2708808736
        or digest(MODEL)
        != "03b90d043897e0de78983e36702f182d19fb6b2321586707841533ea7d8c407d"
    ):
        raise ValueError("Exact pinned Q4 model required")
    resources = BASE / "resources"
    runtime = resources / "native/llama"
    if not runtime.exists():
        runtime.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copytree(BASE / "runtime/llama-b11946", runtime, symlinks=True)
    entries = [
        {"kind": "directory", "path": "native"},
        {"kind": "directory", "path": "native/llama"},
    ]
    for name, value in sorted(verified.items()):
        path = runtime / name
        if value["type"] == "symlink":
            if not path.is_symlink() or os.readlink(path) != value["target"]:
                raise ValueError("Runtime link changed")
            entries.append(
                {
                    "kind": "symlink",
                    "path": "native/llama/" + name,
                    "target": value["target"],
                }
            )
        else:
            if (
                path.is_symlink()
                or path.stat().st_size != value["size_bytes"]
                or digest(path) != value["sha256"]
            ):
                raise ValueError("Runtime bytes changed")
            entries.append(
                {
                    "kind": "file",
                    "path": "native/llama/" + name,
                    "size": value["size_bytes"],
                    "sha256": value["sha256"],
                    "executable": bool(path.stat().st_mode & stat.S_IXUSR),
                }
            )
    inventory = BASE / "evidence/native-resource-inventory.json"
    write(inventory, entries)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    root = BASE / "runs" / stamp
    (root / "models").mkdir(parents=True, mode=0o700)
    subprocess.run(
        ["/bin/cp", "-c", str(MODEL), str(root / "models" / MODEL.name)], check=True
    )
    if (root / "models" / MODEL.name).stat().st_nlink != 1:
        raise ValueError("Exclusive model snapshot source required")
    output = BASE / "evidence" / ("owned-" + scenario + "-" + stamp + ".json")
    fixture = {
        "project": str(ROOT),
        "resources": str(resources),
        "inventory": str(inventory),
        "inventory_sha256": digest(inventory),
        "python": str(
            ROOT / ".cache/build-inputs/install/python-3.12.16/bin/python3.12"
        ),
        "dependencies": str(
            ROOT / ".cache/venvs/worker-3.12.16/lib/python3.12/site-packages"
        ),
        "worker_script": str(ROOT / "scripts/verify-inference-worker.py"),
        "run_root": str(root),
        "output": str(output),
        "scenario": scenario,
    }
    path = output.with_suffix(".fixture.json")
    write(path, fixture)
    return path, fixture


class NativeObserver:
    """Read-only libproc observations of identities already recorded by the host."""

    class BsdInfo(ctypes.Structure):
        _fields_ = (
            [
                (name, ctypes.c_uint32)
                for name in (
                    "flags",
                    "status",
                    "xstatus",
                    "pid",
                    "ppid",
                    "uid",
                    "gid",
                    "ruid",
                    "rgid",
                    "svuid",
                    "svgid",
                    "reserved",
                )
            ]
            + [("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)]
            + [
                (name, ctypes.c_uint32)
                for name in ("nfiles", "pgid", "jobc", "tdev", "tpgid")
            ]
            + [
                ("nice", ctypes.c_int32),
                ("start_sec", ctypes.c_uint64),
                ("start_usec", ctypes.c_uint64),
            ]
        )

    def __init__(self):
        self.library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        self.library.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        self.library.proc_pidinfo.restype = ctypes.c_int
        self.library.proc_listpids.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        self.library.proc_listpids.restype = ctypes.c_int
        observed = self.identity(os.getpid())
        if (
            ctypes.sizeof(self.BsdInfo) != 136
            or observed.get("pid") != os.getpid()
            or observed.get("uid") != os.geteuid()
        ):
            raise RuntimeError("Native observer ABI self-readback unavailable")

    def identity(self, pid):
        info = self.BsdInfo()
        ctypes.set_errno(0)
        size = self.library.proc_pidinfo(
            pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)
        )
        error = ctypes.get_errno()
        if size != ctypes.sizeof(info):
            return {
                "query": "absent"
                if size <= 0 and error in {errno.ESRCH, errno.ENOENT}
                else "unavailable",
                "pid": pid,
                "bytes": size,
                "errno": error,
            }
        return {
            "query": "present",
            **{
                name: int(getattr(info, name))
                for name in (
                    "pid",
                    "ppid",
                    "pgid",
                    "uid",
                    "start_sec",
                    "start_usec",
                    "status",
                )
            },
        }

    def group(self, leader):
        pids = (ctypes.c_int * 1024)()
        ctypes.set_errno(0)
        size = self.library.proc_listpids(2, leader, pids, ctypes.sizeof(pids))
        error = ctypes.get_errno()
        if (
            size < 0
            or size >= ctypes.sizeof(pids)
            or size % ctypes.sizeof(ctypes.c_int)
            or (size == 0 and error not in {0, errno.ESRCH, errno.ENOENT})
        ):
            return {
                "query": "unavailable",
                "leader": leader,
                "bytes": size,
                "errno": error,
            }
        return {
            "query": "observed",
            "leader": leader,
            "members": [
                self.identity(pid) for pid in sorted(pids[: size // 4]) if pid > 0
            ],
        }


def same_identity(observed, recorded):
    return observed.get("query") == "present" and all(
        observed.get(key) == recorded[key]
        for key in ("pid", "pgid", "start_sec", "start_usec")
    )


def listener_state(port):
    connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    connection.settimeout(0.2)
    try:
        code = connection.connect_ex(("127.0.0.1", port))
        return {
            "state": "accepting"
            if code == 0
            else "refused"
            if code == errno.ECONNREFUSED
            else "unavailable",
            "errno": code,
        }
    except OSError as error:
        return {"state": "unavailable", "errno": error.errno}
    finally:
        connection.close()


def run_host_loss(binary, fixture, value, stream):
    ready_path = Path(value["output"]).with_suffix(".host-loss-ready.json")
    command = [
        str(binary),
        "--exact",
        "inference_acceptance::owned_local_decisions",
        "--ignored",
        "--nocapture",
        "--test-threads=1",
    ]
    environment = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "TMPDIR": str(ROOT / ".cache/tmp"),
        "THREADRELAY_INFERENCE_NATIVE_FIXTURE": str(fixture),
    }
    host = subprocess.Popen(
        command, cwd=ROOT, env=environment, stdout=stream, stderr=subprocess.STDOUT
    )
    ready = None
    before_host, before_groups, before_listener, observations = None, [], None, []
    try:
        observer = NativeObserver()
        deadline = time.monotonic() + 90
        while not ready_path.exists():
            if host.poll() is not None:
                raise RuntimeError(
                    "Owned host exited before its classified identity record"
                )
            if time.monotonic() >= deadline:
                raise RuntimeError("Bounded parent-loss fixture readiness unavailable")
            time.sleep(0.05)
        if ready_path.is_symlink() or ready_path.stat().st_size > 1048576:
            raise RuntimeError("Bounded host identity record required")
        ready = json.loads(ready_path.read_text())
        if (
            ready.get("phase") != "ready_for_owned_parent_sigkill"
            or ready["host"]["pid"] != host.pid
        ):
            raise RuntimeError("Retained Popen host identity mismatch")
        before_host = observer.identity(host.pid)
        before_groups = [observer.group(group["leader"]) for group in ready["groups"]]
        recorded = {
            member["pid"]: member
            for group in ready["groups"]
            for member in group["members"]
        }
        if not same_identity(before_host, ready["host"]) or len(before_groups) != 2:
            raise RuntimeError("Exact live Popen identity unavailable before kill")
        for current, expected in zip(before_groups, ready["groups"]):
            if (
                current["query"] != "observed"
                or {v["pid"] for v in current["members"]}
                != {v["pid"] for v in expected["members"]}
                or not all(
                    same_identity(member, recorded[member["pid"]])
                    for member in current["members"]
                )
            ):
                raise RuntimeError("Recorded same-manager group changed before kill")
        before_listener = listener_state(ready["runtime"]["owned_port"])
        if before_listener["state"] != "accepting" or host.poll() is not None:
            raise RuntimeError("Live owned host/listener precondition unavailable")
        killed = time.monotonic()
        host.kill()  # Only this still-retained live Popen child, never a discovered PID/group.
        returncode = host.wait(timeout=5)
        observations = []
        settled = False
        consecutive = 0
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            groups = [observer.group(group["leader"]) for group in ready["groups"]]
            children = [observer.identity(pid) for pid in recorded]
            listener = listener_state(ready["runtime"]["owned_port"])
            gone = (
                all(
                    group["query"] == "observed" and not group["members"]
                    for group in groups
                )
                and all(child["query"] == "absent" for child in children)
                and listener["state"] == "refused"
            )
            observations.append(
                {
                    "seconds_after_sigkill": round(time.monotonic() - killed, 6),
                    "groups": groups,
                    "recorded_children": children,
                    "listener": listener,
                    "all_gone": gone,
                }
            )
            consecutive = consecutive + 1 if gone else 0
            if consecutive >= 2:
                settled = True
                break
            time.sleep(0.1)
        snapshots = []
        for path in [
            Path(ready["guarded_resources"]),
            Path(ready["model_snapshot"]["loaded_model_path"]).parent,
        ]:
            if (
                not path.is_relative_to(Path(value["run_root"]) / "execution-staging")
                or path.is_symlink()
            ):
                raise RuntimeError("Recorded immutable snapshot namespace mismatch")
            meta = path.stat()
            snapshots.append(
                {
                    "path": str(path),
                    "exists": True,
                    "inode": meta.st_ino,
                    "mode": oct(stat.S_IMODE(meta.st_mode)),
                    "flags": getattr(meta, "st_flags", None),
                    "action": "read_only_metadata; retained, not unsealed or adopted",
                }
            )
        record = {
            "result": "observed" if settled and returncode == -9 else "inconclusive",
            "native_acceptance": "not_minted",
            "scenario": "host_loss",
            "kill_target": "only live retained Popen host",
            "host_returncode": returncode,
            "before": {
                "host": before_host,
                "groups": before_groups,
                "listener": before_listener,
                "host_record": ready,
            },
            "after": observations,
            "watchdog_process_cleanup_observed": settled,
            "artifact_cleanup": "pending; immutable snapshots intentionally retained after owner death",
            "retained_snapshots": snapshots,
            "capabilities_minted": [],
        }
        write(Path(value["output"]), record)
        return 0 if record["result"] == "observed" else 2
    except Exception as error:
        write(
            Path(value["output"]),
            {
                "result": "inconclusive",
                "native_acceptance": "not_minted",
                "scenario": "host_loss",
                "error": type(error).__name__ + ": " + str(error),
                "before": {
                    "host": before_host,
                    "groups": before_groups,
                    "listener": before_listener,
                    "host_record": ready,
                },
                "after": observations,
                "artifact_cleanup": "not claimed; do not unseal or adopt unknown snapshots",
                "capabilities_minted": [],
            },
        )
        return 2
    finally:
        if host.poll() is None:
            host.kill()
            host.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scenario",
        choices=["normal_close", "runtime_loss", "worker_loss", "host_loss"],
        default="normal_close",
    )
    parser.add_argument("--test-binary", type=Path)
    args = parser.parse_args()
    fixture, value = prepare(args.scenario)
    if args.test_binary is None:
        print(json.dumps({"fixture": str(fixture), "output": value["output"]}))
        return
    binary = args.test_binary.resolve()
    if not any(
        binary.is_relative_to(ROOT / ".cache/cargo-target" / profile / "deps")
        for profile in ("debug", "release")
    ) or not binary.name.startswith("threadrelay_desktop-"):
        raise ValueError("Fixed project test binary required")
    paths = sorted(
        {
            *[
                path
                for path in (ROOT / "worker/src").rglob("*")
                if path.is_file() and path.suffix in {".py", ".json", ".sql"}
            ],
            *list((ROOT / "apps/desktop/src-tauri/src").rglob("*.rs")),
            *[
                path
                for path in (ROOT / "apps/desktop/src-tauri/native").rglob("*")
                if path.is_file() and path.suffix in {".c", ".h", ".m", ".js", ".json"}
            ],
            ROOT / "apps/desktop/src-tauri/Cargo.toml",
            ROOT / "apps/desktop/src-tauri/Cargo.lock",
            ROOT / "models/manifest.json",
            ROOT / "scripts/verify-inference-native.py",
            ROOT / "scripts/verify-inference-worker.py",
        }
    )
    before = {str(p.relative_to(ROOT)): digest(p) for p in paths}
    binary_before = digest(binary)
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "TMPDIR": str(ROOT / ".cache/tmp"),
        "THREADRELAY_INFERENCE_NATIVE_FIXTURE": str(fixture),
    }
    log = Path(value["output"]).with_suffix(".test.log")
    with log.open("xb") as stream:
        if args.scenario == "host_loss":
            result_code = run_host_loss(binary, fixture, value, stream)
        else:
            result_code = subprocess.run(
                [
                    str(binary),
                    "--exact",
                    "inference_acceptance::owned_local_decisions",
                    "--ignored",
                    "--nocapture",
                    "--test-threads=1",
                ],
                cwd=ROOT,
                env=env,
                stdout=stream,
                stderr=subprocess.STDOUT,
                timeout=180,
            ).returncode
    after = {str(p.relative_to(ROOT)): digest(p) for p in paths}
    binary_after = digest(binary)
    report_path = Path(value["output"])
    if not report_path.is_file():
        raise RuntimeError(
            "Native test did not produce its required observation; wrong or filtered test binary"
        )
    write(
        Path(value["output"]).with_suffix(".run.json"),
        {
            "command": [
                str(binary),
                "--exact",
                "inference_acceptance::owned_local_decisions",
                "--ignored",
                "--nocapture",
                "--test-threads=1",
            ],
            "fixture": str(fixture),
            "returncode": result_code,
            "source_before": before,
            "source_after": after,
            "source_frozen": before == after,
            "binary_sha256": binary_after,
            "binary_before_sha256": binary_before,
            "binary_unchanged": binary_before == binary_after,
            "log_sha256": digest(log),
        },
    )
    print(
        json.dumps(
            {
                "fixture": str(fixture),
                "output": value["output"],
                "returncode": result_code,
                "source_frozen": before == after,
            }
        )
    )
    raise SystemExit(
        result_code if before == after and binary_before == binary_after else 2
    )


if __name__ == "__main__":
    main()
