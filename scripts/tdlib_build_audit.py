"""Fail-closed audit of CMake codemodel/toolchains and Ninja's actual header inputs.

Identity-only build receipts require this audit; it is not native loader proof.
All compiler/SDK inputs must live in the fully inventoried reviewed input root.
No ambient /usr/local, package manager, plugin, launcher or optional-library input.
"""

import json
from pathlib import Path, PureWindowsPath
import re
import shlex

SYSTEM_LIBRARIES = {
    "macos-arm64": {"c", "c++", "c++abi", "m", "System", "pthread", "dl", "resolv"},
    "windows-x64": {
        "kernel32",
        "user32",
        "gdi32",
        "winspool",
        "shell32",
        "ole32",
        "oleaut32",
        "uuid",
        "comdlg32",
        "advapi32",
        "ws2_32",
        # Pinned tdutils, tdnet, tdmtproto and tdsqlite require this SDK library.
        "mswsock",
        "crypt32",
        "bcrypt",
        "normaliz",
        "iphlpapi",
        "secur32",
        "userenv",
        "psapi",
        "msvcrt",
        "libcmt",
        "vcruntime",
        "ucrt",
        "oldnames",
    },
}
FRAMEWORKS = {
    "Security",
    "CoreFoundation",
    "Foundation",
    "SystemConfiguration",
    "Network",
    "CoreServices",
    "CFNetwork",
}
FALSE = {"", "0", "OFF", "FALSE", "NO", "IGNORE"}
POLICY = {
    "CMAKE_DISABLE_FIND_PACKAGE_Crc32c": "ON",
    "CMAKE_DISABLE_FIND_PACKAGE_ABSL": "ON",
    "CRC32C_FOUND": "OFF",
    "ABSL_FOUND": "OFF",
    "TD_WITH_ABSEIL": "OFF",
    "CCACHE_FOUND": "OFF",
    "CMAKE_FIND_USE_PACKAGE_REGISTRY": "OFF",
    "CMAKE_FIND_USE_SYSTEM_PACKAGE_REGISTRY": "OFF",
    "CMAKE_FIND_USE_CMAKE_SYSTEM_PATH": "OFF",
    "CMAKE_FIND_USE_CMAKE_ENVIRONMENT_PATH": "OFF",
    "CMAKE_FIND_USE_SYSTEM_ENVIRONMENT_PATH": "OFF",
    "CMAKE_FIND_USE_INSTALL_PREFIX": "OFF",
    "CMAKE_FIND_USE_PACKAGE_ROOT_PATH": "OFF",
    "CMAKE_C_COMPILER_LAUNCHER": "",
    "CMAKE_CXX_COMPILER_LAUNCHER": "",
    "CMAKE_C_LINKER_LAUNCHER": "",
    "CMAKE_CXX_LINKER_LAUNCHER": "",
    "CMAKE_PROJECT_INCLUDE": "",
    "CMAKE_PROJECT_INCLUDE_BEFORE": "",
    "CMAKE_PROJECT_TOP_LEVEL_INCLUDES": "",
    "CMAKE_TOOLCHAIN_FILE": "",
    "CMAKE_FIND_ROOT_PATH_MODE_PROGRAM": "ONLY",
    "CMAKE_FIND_ROOT_PATH_MODE_LIBRARY": "ONLY",
    "CMAKE_FIND_ROOT_PATH_MODE_INCLUDE": "ONLY",
    "CMAKE_FIND_ROOT_PATH_MODE_PACKAGE": "ONLY",
}


def contained(value, roots, base=None):
    value = str(value).strip('"')
    path = Path(value)
    if not path.is_absolute():
        if base is None:
            raise ValueError("Relative build input unavailable")
        path = Path(base) / path
    path = path.resolve()
    if not any(path.is_relative_to(Path(root).resolve()) for root in roots):
        raise ValueError("Unreviewed build input")
    return path


def cache(text, roots):
    values = {}
    for line in text.splitlines():
        if not line or line.startswith(("#", "//")):
            continue
        match = re.fullmatch(r"([^:=]+):([^=]+)=(.*)", line)
        if not match:
            raise ValueError("Unrecognized CMake cache")
        name, kind, value = match.groups()
        if name in values:
            raise ValueError("Duplicate CMake cache key")
        values[name] = value
        upper = name.upper()
        if (
            "LAUNCHER" in upper
            or upper
            in {
                "CCACHE_FOUND",
                "CRC32C_FOUND",
                "ABSL_FOUND",
                "TD_WITH_ABSEIL",
                "RULE_LAUNCH_COMPILE",
                "RULE_LAUNCH_LINK",
            }
        ) and value.upper() not in FALSE:
            raise ValueError("Optional package/launcher is not allowed")
        if kind in {"PATH", "FILEPATH"} or any(
            key in upper
            for key in (
                "INCLUDE_DIR",
                "LIBRARIES",
                "LIBRARY",
                "PREFIX_PATH",
                "ROOT_PATH",
                "PROGRAM_PATH",
                "MODULE_PATH",
                "IMPLICIT_LINK",
                "IMPLICIT_INCLUDE",
            )
        ):
            for item in value.split(";"):
                if item.upper() in FALSE or item.endswith("-NOTFOUND"):
                    continue
                if Path(item).is_absolute():
                    contained(item, roots)
                elif kind in {"PATH", "FILEPATH"}:
                    raise ValueError("Unresolved CMake input path")

    if any(values.get(name) != expected for name, expected in POLICY.items()):
        raise ValueError("Closed CMake discovery policy missing or changed")


def windows_input_path(value, roots, base):
    # CMake artifacts use '/', but native-shell link fragments may use '\\'.
    # Do not normalize away traversal or treat a foreign drive as a relative name
    # when running source fixtures on a non-Windows host.
    value = value.replace("\\", "/")
    if ".." in value.split("/") or (
        PureWindowsPath(value).drive and not Path(value).is_absolute()
    ):
        raise ValueError("Unreviewed Windows input path")
    return contained(value, roots, base)


def windows_library(value, roots, base, artifacts):
    bare = not any(character in value for character in "/\\:")
    if bare and value[:-4].lower() in SYSTEM_LIBRARIES["windows-x64"]:
        return
    path = windows_input_path(value, roots, base)
    # A root-level archive such as tdjson_private.lib can be spelled without a
    # slash by CMake. It is an exact graph output, never a new bare SDK allowance.
    if path in artifacts:
        return
    if bare:
        raise ValueError("Unreviewed bare Windows library")
    # Outside generated outputs, only the fully inventoried build inputs may
    # supply a library (e.g. pinned OpenSSL/zlib). Source/output containment alone
    # does not authorize an arbitrary archive.
    contained(path, [roots[0]])


def fragment(value, roots, target, base=None, *, artifacts=()):
    # The codemodel has expanded paths, not shell instructions supplied by a user.
    # Reject hidden inputs/launchers even if the source unexpectedly adds them.
    if re.search(
        r"(?i)(ccache|sccache|distcc|crc32c|abseil|fplugin|fpass-plugin|(^|\s)-load|(^|\s)@)",
        value,
    ):
        raise ValueError("Unreviewed optional/plugin/launcher input")
    if re.search(
        r"(^|\s)(-B|--gcc-toolchain|--ld-path|-fuse-ld|-wrapper|-specs|-resource-dir|-fmodule-file|/B[12])",
        value,
    ):
        raise ValueError("Unreviewed compiler/linker driver override")
    tokens = shlex.split(value, posix=target != "windows-x64")
    framework = False
    input_path = False
    for token in tokens:
        token = token.strip('"')
        if input_path:
            contained(token, roots, base or roots[-1])
            input_path = False
            continue
        if token in {
            "-I",
            "-L",
            "-F",
            "-isystem",
            "-iquote",
            "-isysroot",
            "-include",
            "-imacros",
            "-ivfsoverlay",
            "/I",
            "/FI",
            "/LIBPATH",
        }:
            input_path = True
            continue
        if framework:
            if token not in FRAMEWORKS:
                raise ValueError("Unreviewed framework")
            framework = False
            continue
        if token == "-framework":
            framework = True
            continue
        if token.startswith("-l") and len(token) > 2:
            if token[2:] not in SYSTEM_LIBRARIES[target]:
                raise ValueError("Unreviewed bare link library")
        if target == "windows-x64" and token.lower().endswith(".lib"):
            windows_library(token, roots, base or roots[-1], artifacts)
            continue
        # Expand common compiler/linker path prefixes and comma-separated -Wl.
        for part in token.split(","):
            for prefix in (
                "-I",
                "-L",
                "-F",
                "-isystem",
                "-iquote",
                "-isysroot",
                "--sysroot=",
                "-include",
                "-imacros",
                "-fmodule-map-file=",
                "/external:I",
                "/FI",
                "/I",
                "/LIBPATH:",
            ):
                if part.startswith(prefix) and len(part) > len(prefix):
                    part = part[len(prefix) :].strip('"')
                    contained(part, roots, base or roots[-1])
                    break
            if (
                not Path(part).is_absolute()
                and not part.startswith(("-", "/", "@rpath/", "@loader_path/"))
                and part.lower().endswith((".a", ".so", ".dylib", ".o", ".obj", ".lib"))
            ):
                if (
                    target == "windows-x64"
                    and part.lower().endswith(".lib")
                    and part[:-4].lower() in SYSTEM_LIBRARIES[target]
                ):
                    continue
                contained(part, roots, base or roots[-1])
            if Path(part).is_absolute():
                # MSVC switches begin '/', but are not Unix file inputs.
                if target == "windows-x64" and re.match(
                    r"^/[A-Za-z][A-Za-z0-9:-]*$", part
                ):
                    continue
                contained(part, roots)
    if framework or input_path:
        raise ValueError("Unresolved compiler/framework input")


def dependencies(text, roots, build):
    count = 0
    for line in text.splitlines():
        if line.startswith("    "):
            contained(line.strip(), roots, build)
            count += 1
        elif line and ": #deps " not in line:
            raise ValueError("Unrecognized actual header dependency record")
    if count == 0:
        raise ValueError("Actual header dependency evidence missing")
    return count


def configured(build, source, inputs, target):
    build, source, inputs = map(Path, (build, source, inputs))
    roots = [inputs, source, build.parent]
    cache((build / "CMakeCache.txt").read_text(), roots)
    reply = build / ".cmake/api/v1/reply"
    indexes = sorted(reply.glob("index-*.json"))
    if len(indexes) != 1:
        raise ValueError("Exact CMake file API reply required")
    index = json.loads(indexes[0].read_text())

    def document(name):
        path = contained(reply / name, [reply])
        return json.loads(path.read_text())

    responses = index.get("reply", {})
    code = document(responses["codemodel-v2"]["jsonFile"])
    toolchains = document(responses["toolchains-v1"]["jsonFile"])
    for toolchain in toolchains["toolchains"]:
        compiler = toolchain["compiler"]
        contained(compiler["path"], [inputs])
        implicit = compiler.get("implicit", {})
        for key in (
            "includeDirectories",
            "linkDirectories",
            "linkFrameworkDirectories",
        ):
            for path in implicit.get(key, []):
                contained(path, [inputs])
        for library in implicit.get("linkLibraries", []):
            if Path(library).is_absolute():
                contained(library, [inputs])
            elif library not in SYSTEM_LIBRARIES[target]:
                raise ValueError("Unreviewed implicit library")
    count = 0
    for config in code["configurations"]:
        targets = [document(item["jsonFile"]) for item in config["targets"]]
        # File-API artifact paths are relative to the top-level build directory.
        # Collect before examining links (dependency documents can occur later),
        # and never borrow outputs from a different configuration.
        artifacts = (
            {
                windows_input_path(artifact["path"], [build], build)
                for target_doc in targets
                for artifact in target_doc.get("artifacts", [])
            }
            if target == "windows-x64"
            else set()
        )
        for target_doc in targets:
            for source_file in target_doc.get("sources", []):
                contained(
                    source_file["path"],
                    roots,
                    build if source_file.get("isGenerated") else source,
                )
            for group in target_doc.get("compileGroups", []):
                if group.get("sysroot"):
                    contained(group["sysroot"]["path"], [inputs])
                for include in group.get("includes", []):
                    contained(include["path"], roots)
                for command in group.get("compileCommandFragments", []):
                    fragment(
                        command["fragment"], roots, target, build, artifacts=artifacts
                    )
            for command in target_doc.get("link", {}).get("commandFragments", []):
                fragment(command["fragment"], roots, target, build, artifacts=artifacts)
            count += 1
    if count == 0:
        raise ValueError("Empty configured build graph")
    return {
        "configured_targets": count,
        "cache_sha256": __import__("packaging_common").digest(build / "CMakeCache.txt"),
    }
