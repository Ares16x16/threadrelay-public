"""Pinned Windows link-input source fixtures; no Windows/CMake/native execution.

Library closure reviewed against TDLib bc9c263e2bfee06aaab41e82db51a103376030bc:
https://github.com/tdlib/td/blob/bc9c263e2bfee06aaab41e82db51a103376030bc/CMakeLists.txt
and tdutils, tdnet, tdactor, tde2e, tddb, sqlite and tdtl CMakeLists.txt.
CMake file-API artifacts are paths relative to the top-level build directory:
https://cmake.org/cmake/help/v3.31/manual/cmake-file-api.7.html
"""

import json

import pytest

from test_packaging import script

# Pinned non-LTO/JNI-off/DotNet-off graph: tdjson closure, source-generator
# libraries, and additional archives present in the audited configured graph.
# These are fixture outputs, NOT an allowlist of names in the production auditor.
GENERATED = (
    "tdjson_private.lib",
    "tdclient.lib",
    "tdcore.lib",
    "tdapi.lib",
    "tdmtproto.lib",
    "tdnet/tdnet.lib",
    "tddb/tddb.lib",
    "tdactor/tdactor.lib",
    "tde2e/tde2e.lib",
    "sqlite/tdsqlite.lib",
    "tdutils/tdutils.lib",
    "tdtl/tdtl.lib",
    "td/generate/tl_writer_cpp.lib",
    "tdjson_static.lib",
    "tdc.lib",
    "memprof.lib",
    "memprof_stat.lib",
)
# Explicit pinned TDLib Windows dependencies + CMake's standard MSVC SDK set.
SDK = (
    "ws2_32.lib Mswsock.lib Crypt32.lib Normaliz.lib psapi.lib shell32.lib "
    "kernel32.lib user32.lib gdi32.lib winspool.lib ole32.lib oleaut32.lib "
    "uuid.lib comdlg32.lib advapi32.lib"
)


@pytest.fixture
def windows_graph(tmp_path):
    audit = script("tdlib_build_audit")
    source, inputs, build = (
        tmp_path / name for name in ("source", "reviewed inputs", "output/build")
    )
    for path in (source, inputs, build):
        path.mkdir(parents=True)
    reply = build / ".cmake/api/v1/reply"
    reply.mkdir(parents=True)
    (build / "CMakeCache.txt").write_text(
        "\n".join(f"{key}:STRING={value}" for key, value in audit.POLICY.items())
    )

    def write(name, value):
        (reply / name).write_text(json.dumps(value))

    targets = [{"jsonFile": "tdjson.json"}]
    for i, path in enumerate(GENERATED):
        name = f"library-{i}.json"
        targets.append({"jsonFile": name})
        write(name, {"type": "STATIC_LIBRARY", "artifacts": [{"path": path}]})
    code = {"configurations": [{"name": "Release", "targets": targets}]}
    write("code.json", code)
    write(
        "index-one.json",
        {
            "reply": {
                "codemodel-v2": {"jsonFile": "code.json"},
                "toolchains-v1": {"jsonFile": "tools.json"},
            }
        },
    )
    write(
        "tools.json",
        {
            "toolchains": [
                {
                    "compiler": {
                        "path": str(inputs / "cl.exe"),
                        "implicit": {
                            "linkLibraries": ["libcmt", "oldnames"],
                            "linkDirectories": [str(inputs / "sdk/lib")],
                        },
                    }
                }
            ]
        },
    )
    target = {
        "type": "SHARED_LIBRARY",
        "artifacts": [{"path": "tdjson.dll"}, {"path": "tdjson.lib"}],
        "link": {"language": "CXX", "commandFragments": []},
    }

    def check(*fragments):
        target["link"]["commandFragments"] = [
            {"role": "libraries", "fragment": fragment} for fragment in fragments
        ]
        write("tdjson.json", target)
        return audit.configured(build, source, inputs, "windows-x64")

    return check, write, code, inputs, build, source


def test_windows_pinned_sdk_dependency_set_is_accepted_without_graph():
    audit = script("tdlib_build_audit")
    audit.fragment(SDK, ["/reviewed", "/source", "/build"], "windows-x64", "/build")


@pytest.mark.parametrize("spelling", ["relative", "backslash", "absolute"])
def test_windows_pinned_link_closure_is_bound_to_configured_artifacts(
    windows_graph,
    spelling,
):
    check, _, _, inputs, build, _ = windows_graph
    libraries = {
        "relative": list(GENERATED),
        "backslash": [path.replace("/", "\\") for path in GENERATED],
        "absolute": [str(build / path) for path in GENERATED],
    }[spelling]
    result = check(
        SDK,
        " ".join(f'"{path}"' for path in libraries),
        f'"{inputs}/openssl/lib/libcrypto.lib" "{inputs}/openssl/lib/libssl.lib" '
        f'"{inputs}/zlib/lib/zlibstatic.lib"',
    )
    assert result["configured_targets"] == 18


@pytest.mark.parametrize(
    "library",
    [
        "unknown.lib",
        "tdutils.lib",
        "libcrypto.lib",
        "tdutils/unlisted.lib",
        "tdutils/ws2_32.lib",
        r"tdutils\Mswsock.lib",
        "crc32c.lib",
        "abseil.lib",
        "./unlisted.lib",
        "tdutils/../tdutils/tdutils.lib",
        r"tdutils\..\tdutils\tdutils.lib",
        "../install/tdutils.lib",
        "../../ambient/unknown.lib",
        r"C:\ambient\unknown.lib",
    ],
)
def test_windows_link_rejects_unknown_bare_unlisted_or_traversing_input(
    windows_graph,
    library,
):
    check, *_ = windows_graph
    with pytest.raises(ValueError, match="Unreviewed"):
        check(library)


@pytest.mark.parametrize("location", ["output", "build", "source", "external"])
def test_windows_absolute_library_needs_inventory_or_graph_membership(
    windows_graph,
    location,
):
    check, _, _, _, build, source = windows_graph
    path = {
        "output": build.parent / "unlisted.lib",
        "build": build / "unlisted.lib",
        "source": source / "unlisted.lib",
        "external": build.parent.parent / "ambient/unlisted.lib",
    }[location]
    with pytest.raises(ValueError, match="Unreviewed"):
        check(f'"{path}"')


@pytest.mark.parametrize("path", ["../outside.lib", "tdutils/../tdutils.lib"])
def test_windows_configured_artifact_cannot_authorize_traversal(windows_graph, path):
    check, write, *_ = windows_graph
    write("library-0.json", {"type": "STATIC_LIBRARY", "artifacts": [{"path": path}]})
    with pytest.raises(ValueError, match="Unreviewed"):
        check(f'"{path}"')


def test_windows_artifact_from_another_configuration_is_not_authority(windows_graph):
    check, write, code, *_ = windows_graph
    write(
        "debug-only.json",
        {
            "type": "STATIC_LIBRARY",
            "artifacts": [{"path": "debug-only.lib"}],
        },
    )
    code["configurations"].append(
        {"name": "Debug", "targets": [{"jsonFile": "debug-only.json"}]}
    )
    write("code.json", code)
    with pytest.raises(ValueError, match="Unreviewed"):
        check("debug-only.lib")


@pytest.mark.parametrize(
    "document,path",
    [
        ("library-0.json", "tdjson_private.lib"),
        ("library-10.json", "tdutils/tdutils.lib"),
    ],
)
def test_windows_removed_graph_output_is_not_authorized_by_its_name(
    windows_graph,
    document,
    path,
):
    check, write, _, _, build, _ = windows_graph
    write(document, {"type": "STATIC_LIBRARY", "artifacts": []})
    for spelling in (path, str(build / path)):
        with pytest.raises(ValueError, match="Unreviewed"):
            check(f'"{spelling}"')


@pytest.mark.parametrize("location", ["inputs", "output", "source", "external"])
def test_windows_artifact_record_cannot_authorize_an_outside_build_library(
    windows_graph,
    location,
):
    check, write, _, inputs, build, source = windows_graph
    path = {
        "inputs": inputs / "fake.lib",
        "output": build.parent / "fake.lib",
        "source": source / "fake.lib",
        "external": build.parent.parent / "ambient/fake.lib",
    }[location]
    write(
        "library-0.json", {"type": "STATIC_LIBRARY", "artifacts": [{"path": str(path)}]}
    )
    with pytest.raises(ValueError, match="Unreviewed"):
        check(f'"{path}"')
