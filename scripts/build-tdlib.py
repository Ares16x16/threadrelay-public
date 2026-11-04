#!/usr/bin/env python3
"""Official pinned TDLib source builder. No fetch/install or acceptance minting."""

import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
from packaging_common import digest, inventory_tree, normal_name

ROOT = Path(__file__).resolve().parents[1]
PIN = "bc9c263e2bfee06aaab41e82db51a103376030bc"
SCHEMA = "98b21543066e0124623b7d36f6496ef38cf86e9f9825aedcdd8e2527633fee4c"


def build_plan(target, manifest=ROOT / "runtimes/tdlib-manifest.json"):
    value = json.loads(Path(manifest).read_text())
    if (
        value["schema_version"] != 1
        or value["repository"] != "https://github.com/tdlib/td"
        or value["commit"] != PIN
        or value["schema"]["sha256"] != SCHEMA
        or value["version"] != "1.8.67"
        or target not in ("windows-x64", "macos-arm64")
    ):
        raise ValueError("Invalid official TDLib pin")
    record = value["targets"][target]
    blockers = [name for name in ("toolchain", "inputs") if not record[name]]
    for name in ("gperf", "ninja"):
        if value["source_inputs"][name]["sha256"] is None:
            blockers.append(name + " source hash")
    return {
        "target": target,
        "commit": PIN,
        "schema": value["schema"],
        "configure": value["configure"] + record["cmake_flags"],
        "toolchain": record["toolchain"],
        "inputs": record["inputs"],
        "blockers": blockers,
        "native_acceptance": "Not run",
        "licenses": value["licenses"],
        "library": record["library"],
    }


def required_tools(target):
    result = {
        "cmake",
        "gperf",
        "git",
        "compiler",
        "cxx_compiler",
        "linker",
        "make",
        "perl",
        "openssl_root",
        "zlib_root",
        "openssl_include",
        "openssl_crypto_library",
        "openssl_ssl_library",
        "zlib_include",
        "zlib_library",
        "sdk",
        "openssl_license",
        "zlib_license",
        "inspector",
        "dependency_inspector",
    }
    if target == "windows-x64":
        result |= {
            "compiler_include",
            "compiler_lib",
            "sdk_ucrt_include",
            "sdk_shared_include",
            "sdk_um_include",
            "sdk_ucrt_lib",
            "sdk_um_lib",
            "resource_compiler",
            "manifest_tool",
        }
    return result


def require_build_inputs(plan, root):
    if plan["blockers"]:
        raise ValueError("Build inputs unresolved: " + ", ".join(plan["blockers"]))
    root = Path(root).resolve()
    toolchain = plan["toolchain"]
    required = required_tools(plan["target"])
    if not required <= set(toolchain):
        raise ValueError("Build toolchain unresolved")
    # Full reviewed build-input tree, including headers/libraries and compiler
    # helpers. A claimed version string alone never authorizes execution.
    if inventory_tree(root) != plan["inputs"]:
        raise ValueError("Build inputs mismatch")
    for path in toolchain.values():
        if not isinstance(path, str) or not normal_name(path):
            raise ValueError("Invalid toolchain path")
    return {name: root / path for name, path in toolchain.items()}


def configure_arguments(plan, tools, stage):
    flags = [*plan["configure"], f"-DCMAKE_INSTALL_PREFIX:PATH={stage}"]
    mapping = {
        "CMAKE_C_COMPILER": "compiler",
        "CMAKE_CXX_COMPILER": "cxx_compiler",
        "CMAKE_LINKER": "linker",
        "CMAKE_MAKE_PROGRAM": "make",
        "GPERF_EXECUTABLE": "gperf",
        "OPENSSL_CRYPTO_LIBRARY": "openssl_crypto_library",
        "OPENSSL_SSL_LIBRARY": "openssl_ssl_library",
        "ZLIB_LIBRARY": "zlib_library",
    }
    for name, tool in mapping.items():
        flags.append(f"-D{name}:FILEPATH={tools[tool]}")
    for name, tool in {
        "OPENSSL_ROOT_DIR": "openssl_root",
        "OPENSSL_INCLUDE_DIR": "openssl_include",
        "ZLIB_ROOT": "zlib_root",
        "ZLIB_INCLUDE_DIR": "zlib_include",
    }.items():
        flags.append(f"-D{name}:PATH={tools[tool]}")
    flags += [
        "-DCMAKE_FIND_USE_PACKAGE_REGISTRY=OFF",
        "-DCMAKE_FIND_USE_SYSTEM_PACKAGE_REGISTRY=OFF",
    ]
    flags += [
        "-DCMAKE_DISABLE_FIND_PACKAGE_Crc32c=ON",
        "-DCMAKE_DISABLE_FIND_PACKAGE_ABSL=ON",
        "-DCRC32C_FOUND:BOOL=OFF",
        "-DABSL_FOUND:BOOL=OFF",
        "-DTD_WITH_ABSEIL=OFF",
        "-DCCACHE_FOUND:BOOL=OFF",
        "-DCMAKE_FIND_USE_CMAKE_SYSTEM_PATH=OFF",
        "-DCMAKE_FIND_USE_CMAKE_ENVIRONMENT_PATH=OFF",
        "-DCMAKE_FIND_USE_SYSTEM_ENVIRONMENT_PATH=OFF",
        "-DCMAKE_FIND_USE_INSTALL_PREFIX=OFF",
        "-DCMAKE_FIND_USE_PACKAGE_ROOT_PATH=OFF",
        "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
        "-DCMAKE_C_COMPILER_LAUNCHER:STRING=",
        "-DCMAKE_CXX_COMPILER_LAUNCHER:STRING=",
        "-DCMAKE_C_LINKER_LAUNCHER:STRING=",
        "-DCMAKE_CXX_LINKER_LAUNCHER:STRING=",
        "-DCMAKE_PROJECT_INCLUDE:FILEPATH=",
        "-DCMAKE_PROJECT_INCLUDE_BEFORE:FILEPATH=",
        "-DCMAKE_PROJECT_TOP_LEVEL_INCLUDES:STRING=",
        "-DCMAKE_TOOLCHAIN_FILE:FILEPATH=",
        "-DCMAKE_FIND_ROOT_PATH_MODE_PROGRAM=ONLY",
        "-DCMAKE_FIND_ROOT_PATH_MODE_LIBRARY=ONLY",
        "-DCMAKE_FIND_ROOT_PATH_MODE_INCLUDE=ONLY",
        "-DCMAKE_FIND_ROOT_PATH_MODE_PACKAGE=ONLY",
        "-DCMAKE_FIND_ROOT_PATH:STRING="
        + os.path.commonpath([str(p) for p in tools.values()]),
        "-DCMAKE_PREFIX_PATH:STRING="
        + ";".join(str(tools[k]) for k in ("openssl_root", "zlib_root", "sdk")),
        "-DCMAKE_PROGRAM_PATH:STRING="
        + ";".join(sorted({str(p.parent) for p in tools.values()})),
    ]
    if plan["target"] == "macos-arm64":
        flags.append(f"-DCMAKE_OSX_SYSROOT:PATH={tools['sdk']}")
    else:
        flags += [
            f"-DCMAKE_RC_COMPILER:FILEPATH={tools['resource_compiler']}",
            f"-DCMAKE_MT:FILEPATH={tools['manifest_tool']}",
        ]
    return flags


def execute(plan, source, inputs, output, jobs):
    expected = (
        ("Windows", "AMD64") if plan["target"] == "windows-x64" else ("Darwin", "arm64")
    )
    if (platform.system(), platform.machine()) != expected:
        raise ValueError("Build requires exact native target")
    tools = require_build_inputs(plan, inputs)
    source, output = Path(source).resolve(), Path(output).resolve()

    git_env = {
        "PATH": str(tools["git"].parent),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_COUNT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    if os.name == "nt":
        git_env["SystemRoot"] = os.environ["SystemRoot"]

    def git(*args):
        return subprocess.check_output(
            [str(tools["git"]), "-c", "core.fsmonitor=false", "-C", str(source), *args],
            text=True,
            env=git_env,
        ).strip()

    if git("rev-parse", "HEAD") != PIN or git(
        "status", "--porcelain", "--untracked-files=all"
    ):
        raise ValueError("Exact clean official Git checkout required")
    if (
        git("remote", "get-url", "origin").removesuffix(".git")
        != "https://github.com/tdlib/td"
    ):
        raise ValueError("Official source required")
    schema = source / plan["schema"]["path"]
    if schema.stat().st_size != plan["schema"]["size"] or digest(schema) != SCHEMA:
        raise ValueError("TDLib schema mismatch")
    for name in plan["licenses"]:
        if not (source / name).is_file():
            raise ValueError("TDLib notices missing")
    output.mkdir(parents=False)  # no overwrite of an older build
    stage = output / "install"
    build = output / "build"
    env = {
        "PATH": os.pathsep.join(sorted({str(p.parent) for p in tools.values()})),
        "TMP": str(output),
        "TEMP": str(output),
        "TMPDIR": str(output),
        "LC_ALL": "C",
        "CC": str(tools["compiler"]),
        "CXX": str(tools["cxx_compiler"]),
    }
    if os.name == "nt":
        env["SystemRoot"] = os.environ["SystemRoot"]
        env["INCLUDE"] = os.pathsep.join(
            str(tools[name])
            for name in (
                "compiler_include",
                "sdk_ucrt_include",
                "sdk_shared_include",
                "sdk_um_include",
            )
        )
        env["LIB"] = os.pathsep.join(
            str(tools[name]) for name in ("compiler_lib", "sdk_ucrt_lib", "sdk_um_lib")
        )
    flags = configure_arguments(plan, tools, stage)
    commands = [
        [str(tools["cmake"]), "-S", str(source), "-B", str(build), *flags],
        [
            str(tools["cmake"]),
            "--build",
            str(build),
            "--config",
            "Release",
            "--target",
            "tdjson",
            "--parallel",
            str(jobs),
        ],
        [str(tools["cmake"]), "--install", str(build), "--config", "Release"],
    ]
    from tdlib_build_audit import configured, dependencies

    query = build / ".cmake/api/v1/query"
    query.mkdir(parents=True)
    for kind in ("codemodel-v2", "toolchains-v1"):
        (query / kind).touch()
    subprocess.run(commands[0], env=env, cwd=output, check=True)
    input_audit = configured(build, source, inputs, plan["target"])
    subprocess.run(commands[1], env=env, cwd=output, check=True)
    # Audit actual compiler-recorded headers as well as configured include/link
    # inputs; a dependency listing of the final DLL cannot reveal static input.
    actual = subprocess.check_output(
        [str(tools["make"]), "-C", str(build), "-t", "deps"],
        env=env,
        cwd=output,
        text=True,
    )
    input_audit["actual_headers"] = dependencies(
        actual, [Path(inputs), source, output], build
    )
    header_record = output / "actual-header-inputs.txt"
    header_record.write_text(actual)
    input_audit["headers_sha256"] = digest(header_record)
    input_audit.update(configured(build, source, inputs, plan["target"]))
    subprocess.run(commands[2], env=env, cwd=output, check=True)
    # Keep bundled encrypted-SQLite's Zetetic BSD conditions, not just the
    # upstream SQLite public-domain description. Source notices are exact pins.
    import shutil

    notices = stage / "share/licenses/tdlib"
    notices.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source / "LICENSE_1_0.txt", notices / "BOOST-LICENSE_1_0.txt")
    shutil.copyfile(
        source / "sqlite/sqlite/LICENSE", notices / "ZETETIC-SQLITE-LICENSE"
    )
    shutil.copyfile(tools["openssl_license"], notices / "OPENSSL-LICENSE")
    shutil.copyfile(tools["zlib_license"], notices / "ZLIB-LICENSE")
    libraries = list(stage.rglob(plan["library"]))
    if len(libraries) != 1:
        raise ValueError("Expected one native TDLib library")
    library = libraries[0]
    exports_command = (
        [str(tools["inspector"]), "/EXPORTS", str(library)]
        if plan["target"] == "windows-x64"
        else [str(tools["inspector"]), "-gU", str(library)]
    )
    exports = subprocess.check_output(exports_command, env=env, cwd=output, text=True)
    import re

    for symbol in ("td_create_client_id", "td_send", "td_receive", "td_execute"):
        expected = symbol if plan["target"] == "windows-x64" else "_" + symbol
        if not re.search(
            r"(?<![A-Za-z0-9_])" + expected + r"(?![A-Za-z0-9_])", exports
        ):
            raise ValueError("TDLib C ABI export missing")
    deps_command = (
        [str(tools["dependency_inspector"]), "/DEPENDENTS", str(library)]
        if plan["target"] == "windows-x64"
        else [str(tools["dependency_inspector"]), "-L", str(library)]
    )
    dependency_report = subprocess.check_output(
        deps_command, env=env, cwd=output, text=True
    )
    (output / "native-loader-audit.txt").write_text(exports + "\n" + dependency_report)
    receipt = {
        "schema_version": 1,
        "result": "identity_only",
        "native_acceptance": "Not run",
        "library": library.relative_to(stage).as_posix(),
        "library_sha256": digest(library),
        "dependency_audit": "review required; report is not automatic loader safety",
        "target": plan["target"],
        "commit": PIN,
        "schema_sha256": SCHEMA,
        "commands": commands,
        "inputs": plan["inputs"],
        "input_audit": input_audit,
        "files": inventory_tree(stage),
    }
    (output / "build-identity.json").write_text(json.dumps(receipt, indent=2) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target", choices=["windows-x64", "macos-arm64"], required=True
    )
    parser.add_argument("--validate-plan", action="store_true")
    parser.add_argument("--execute", action="store_true")
    for arg in ("source", "inputs", "output"):
        parser.add_argument("--" + arg, type=Path)
    parser.add_argument("--jobs", type=int, default=2)
    args = parser.parse_args(argv)
    plan = build_plan(args.target)
    if args.execute:
        if not all((args.source, args.inputs, args.output)) or not 1 <= args.jobs <= 16:
            parser.error("bounded complete build arguments required")
        execute(plan, args.source, args.inputs, args.output, args.jobs)
    else:
        print(json.dumps(plan, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"TDLib build unavailable: {error}") from None
