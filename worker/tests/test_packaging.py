"""Source packaging validation; synthetic files do not prove native acceptance."""

import importlib.util
from pathlib import Path
import sys
import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_fixed_spec_collects_checkout_data_and_modules_despite_stale_installed_worker(tmp_path):
    """Exercise the real freezer collectors without performing another freeze."""
    import json
    import subprocess

    python = ROOT / '.cache/venvs/packaging-3.12.16/bin/python'
    if not python.exists():
        pytest.skip('Prepared pinned target-native packaging environment required')
    stale = tmp_path / 'stale-site-packages/threadrelay'
    stale.mkdir(parents=True)
    (stale / '__init__.py').write_text('')
    (stale / 'protocol.schema.json').write_text('{"stale":true}')
    (stale / 'only_in_stale_install.py').write_text('')
    child = r'''
import json, runpy, sys
from pathlib import Path
from types import SimpleNamespace
root, stale = Path(sys.argv[1]), sys.argv[2]
sys.path.insert(0, stale)
import threadrelay  # A previously imported stale installed package must not win.
captured = {}
def analysis(*args, **kwargs):
    captured.update(kwargs)
    return SimpleNamespace(pure=[], scripts=[], binaries=[], datas=[])
runpy.run_path(str(root/'packaging/worker.spec'), init_globals={
    'SPECPATH':str(root/'packaging'), 'Analysis':analysis,
    'PYZ':lambda *a, **k:None, 'EXE':lambda *a, **k:None,
    'COLLECT':lambda *a, **k:None})
print(json.dumps({'datas':captured['datas'], 'modules':captured['hiddenimports']}))
'''
    result = subprocess.run([str(python), '-B', '-c', child, str(ROOT), str(stale.parent)],
                            check=True, capture_output=True, text=True)
    collected = json.loads(result.stdout)
    data = {Path(source).relative_to(ROOT / 'worker/src').as_posix(): Path(source).read_bytes()
            for source, _ in collected['datas'] if Path(source).is_relative_to(ROOT / 'worker/src')}
    assert data.get('threadrelay/protocol.schema.json') == (ROOT / 'worker/src/threadrelay/protocol.schema.json').read_bytes()
    resources = [* (ROOT / 'worker/src/threadrelay').rglob('*.json'),
                 * (ROOT / 'worker/src/threadrelay').rglob('*.sql')]
    for path in resources:
        assert data[path.relative_to(ROOT / 'worker/src').as_posix()] == path.read_bytes()
    assert 'threadrelay.collector.embedded' in collected['modules']
    assert 'threadrelay.only_in_stale_install' not in collected['modules']


def script(name):
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), ROOT / "scripts" / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tdlib_plan_is_pinned_but_unresolved_builds_cannot_execute(tmp_path):
    builder = script("build-tdlib")
    plan = builder.build_plan("windows-x64", ROOT / "runtimes/tdlib-manifest.json")
    assert plan["commit"] == "bc9c263e2bfee06aaab41e82db51a103376030bc"
    assert plan["native_acceptance"] == "Not run"
    assert plan["blockers"]
    assert "-DTD_ENABLE_JNI=OFF" in plan["configure"]
    with pytest.raises(ValueError, match="unresolved"):
        builder.require_build_inputs(plan, tmp_path)


def test_trusted_bundle_links_preserve_framework_graph_and_reject_escape(tmp_path):
    common = script("packaging_common")
    source = tmp_path / "source"
    source.mkdir()
    (source / "Versions/A").mkdir(parents=True)
    (source / "Versions/A/binary").write_bytes(b"synthetic")
    (source / "Versions/Current").symlink_to("A")
    (source / "binary").symlink_to("Versions/Current/binary")
    inventory = common.inventory_tree(source)
    output = tmp_path / "output"
    common.stage_tree(source, output, inventory)
    assert (output / "binary").is_symlink()
    assert (output / "binary").read_bytes() == b"synthetic"
    (source / "escape").symlink_to("../outside")
    with pytest.raises(ValueError):
        common.inventory_tree(source)


def test_inventory_measurement_ignores_atime_changed_by_hashing(tmp_path, monkeypatch):
    common = script("packaging_common")
    source = tmp_path / "source"
    source.mkdir()
    target = source / "payload"
    target.write_bytes(b"measured bytes")
    original_lstat = Path.lstat
    original_digest = common.digest
    digested = set()

    class AtimeChanged:
        def __init__(self, snapshot):
            self.snapshot = snapshot

        def __getattr__(self, name):
            return getattr(self.snapshot, name)

        @property
        def st_atime_ns(self):
            return self.snapshot.st_atime_ns + 1_000_000_000

        @property
        def st_atime(self):
            return self.snapshot.st_atime + 1

    def lstat(path):
        snapshot = original_lstat(path)
        return AtimeChanged(snapshot) if path == target and path in digested else snapshot

    def digest(path):
        result = original_digest(path)
        digested.add(path)
        return result

    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(common, "digest", digest)
    entries = common.inventory_tree(source)
    assert next(entry for entry in entries if entry["path"] == "payload")["sha256"]


def test_inventory_measurement_still_rejects_mtime_change(tmp_path, monkeypatch):
    common = script("packaging_common")
    source = tmp_path / "source"
    source.mkdir()
    target = source / "payload"
    target.write_bytes(b"measured bytes")
    original_lstat = Path.lstat
    original_digest = common.digest
    digested = set()

    class MtimeChanged:
        def __init__(self, snapshot):
            self.snapshot = snapshot

        def __getattr__(self, name):
            return getattr(self.snapshot, name)

        @property
        def st_mtime_ns(self):
            return self.snapshot.st_mtime_ns + 1_000_000_000

    def lstat(path):
        snapshot = original_lstat(path)
        return MtimeChanged(snapshot) if path == target and path in digested else snapshot

    def digest(path):
        result = original_digest(path)
        digested.add(path)
        return result

    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(common, "digest", digest)
    with pytest.raises(ValueError, match="changed during measurement"):
        common.inventory_tree(source)


def test_package_plan_is_console_onedir_fixed_layout_without_browser_download():
    packaging = script("package-sidecars")
    plan = packaging.package_plan("macos-arm64")
    assert plan["freezer"] == "PyInstaller==6.22.3"
    assert plan["console"] is True and plan["onefile"] is False
    assert plan["worker"] == "sidecars/threadrelay-worker"
    assert plan["evaluator"] == "Contents/MacOS/threadrelay-evaluate"
    assert plan["browser"] == "omitted_pending_terms_and_exact_artifact_review"
    assert plan["native_acceptance"] == "Not run"


def test_freezer_cache_is_fixed_under_new_output_not_the_callers_cache(tmp_path, monkeypatch):
    packaging = script("package-sidecars")
    monkeypatch.setattr(packaging, "target_host", lambda target: None)
    monkeypatch.setattr(packaging, "check_interpreter_identity", lambda *args: None)
    versions = {"pyinstaller": "6.22.3", "pyinstaller-hooks-contrib": "2026.9"}
    monkeypatch.setattr(packaging.importlib.metadata, "version", versions.__getitem__)
    caller_cache = tmp_path / "caller-cache"
    caller_cache.mkdir()
    sentinel = caller_cache / "preserve"
    sentinel.write_text("unrelated cache")
    monkeypatch.setenv("PYINSTALLER_CONFIG_DIR", str(caller_cache))
    invocations = []
    monkeypatch.setattr(packaging.subprocess, "run", lambda *args, **kwargs: invocations.append((args, kwargs)))
    output = tmp_path / "new-freeze"
    packaging.freeze("macos-arm64", output)
    assert len(invocations) == 1
    environment = invocations[0][1]["env"]
    assert environment["PYINSTALLER_CONFIG_DIR"] == str(output / "pyinstaller-cache")
    assert (output / "pyinstaller-cache").is_dir()
    assert environment["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] == "1"
    assert sentinel.read_text() == "unrelated cache"


def test_identity_inventory_cannot_mint_native_acceptance(tmp_path):
    packaging = script("package-sidecars")
    (tmp_path / "program").write_bytes(b"synthetic")
    result = packaging.identity_receipt(tmp_path, "windows-x64")
    assert result["result"] == "identity_only"
    assert result["native_acceptance"] == "Not run"
    assert "checks" not in result
    assert "targets" not in result


def test_qr_notice_closure_is_measured_from_installed_lock():
    import json
    import hashlib

    root = ROOT / "packaging/notices/frontend"
    sbom = json.loads((root / "sbom.json").read_text())
    assert (
        sbom["lock_sha256"]
        == hashlib.sha256(
            (ROOT / "apps/desktop/pnpm-lock.yaml").read_bytes()
        ).hexdigest()
    )
    packages = {p["name"]: p for p in sbom["packages"]}
    assert packages["qrcode"]["version"] == "1.5.4"
    assert {"dijkstrajs", "pngjs", "yargs", "emoji-regex"} <= packages.keys()
    for package in packages.values():
        assert package["notices"]
        for notice in package["notices"]:
            assert (
                hashlib.sha256((root / notice["path"]).read_bytes()).hexdigest()
                == notice["sha256"]
            )


def test_packaged_selfcheck_includes_schema_migrations_catalog_and_private_driver_sources():
    from threadrelay.package_resources import verify_resources

    assert verify_resources() == {
        "resources": "verified",
        "native_acceptance": "Not run",
    }


def test_tauri_overlay_preserves_fixed_target_layout_without_signing_service(tmp_path):
    packaging = script("package-sidecars")
    mac = packaging.tauri_overlay("macos-arm64", tmp_path)
    assert mac["bundle"]["macOS"]["files"]["MacOS/threadrelay-evaluate"] == str(
        tmp_path / "Contents/MacOS/threadrelay-evaluate"
    )
    assert mac["bundle"]["macOS"]["signingIdentity"] is None
    win = packaging.tauri_overlay("windows-x64", tmp_path)
    assert (
        win["bundle"]["resources"][str(tmp_path / "threadrelay-evaluate.exe")]
        == "threadrelay-evaluate.exe"
    )
    assert win["build"]["features"] == ["desktop"]


def test_trusted_staging_retains_root_and_directory_metadata(tmp_path):
    import os

    if not hasattr(os, "setxattr"):
        pytest.skip("xattr API unavailable on this source host")
    common = script("packaging_common")
    source = tmp_path / "source"
    (source / "directory").mkdir(parents=True)
    (source / "directory/file").write_bytes(b"synthetic")
    os.setxattr(source, "user.threadrelay-test", b"root-marker")
    os.setxattr(source / "directory", "user.threadrelay-test", b"directory-marker")
    output = tmp_path / "output"
    common.stage_tree(source, output, common.inventory_tree(source))
    assert os.getxattr(output, "user.threadrelay-test") == b"root-marker"
    assert (
        os.getxattr(output / "directory", "user.threadrelay-test")
        == b"directory-marker"
    )


def test_target_python_notice_identity_is_not_a_caller_label(tmp_path):
    common = script("packaging_common")
    executable = tmp_path / "python"
    license_file = tmp_path / "LICENSE"
    executable.write_bytes(b"synthetic-interpreter")
    license_file.write_bytes(b"synthetic-license")
    pin = {
        "python_version": "3.12.0",
        "python_executable_sha256": common.digest(executable),
        "python_license_sha256": common.digest(license_file),
    }
    common.check_interpreter_identity(pin, executable, "3.12.0", license_file)
    license_file.write_bytes(b"different license")
    with pytest.raises(ValueError, match="identity"):
        common.check_interpreter_identity(pin, executable, "3.12.0", license_file)


def test_tdlib_build_plan_never_discovers_ambient_visual_studio():
    builder = script("build-tdlib")
    for target in ("windows-x64", "macos-arm64"):
        plan = builder.build_plan(target)
        assert plan["configure"][plan["configure"].index("-G") + 1] == "Ninja"
        tools = {
            name: Path("/reviewed") / name for name in builder.required_tools(target)
        }
        flags = builder.configure_arguments(plan, tools, Path("/new/install"))
        for name in (
            "CMAKE_C_COMPILER",
            "CMAKE_CXX_COMPILER",
            "CMAKE_LINKER",
            "CMAKE_MAKE_PROGRAM",
            "OPENSSL_CRYPTO_LIBRARY",
            "OPENSSL_SSL_LIBRARY",
            "ZLIB_LIBRARY",
        ):
            assert any(flag.startswith(f"-D{name}:") for flag in flags)


def test_tdlib_discovery_and_actual_input_audit_reject_outside_optional_dependency(
    tmp_path,
):
    builder = script("build-tdlib")
    plan = builder.build_plan("macos-arm64")
    tools = {
        name: tmp_path / "reviewed" / name
        for name in builder.required_tools(plan["target"])
    }
    flags = builder.configure_arguments(plan, tools, tmp_path / "output/install")
    for flag in (
        "-DCMAKE_DISABLE_FIND_PACKAGE_Crc32c=ON",
        "-DCMAKE_DISABLE_FIND_PACKAGE_ABSL=ON",
        "-DCCACHE_FOUND:BOOL=OFF",
        "-DCMAKE_FIND_USE_CMAKE_SYSTEM_PATH=OFF",
        "-DCMAKE_CXX_COMPILER_LAUNCHER:STRING=",
    ):
        assert flag in flags
    audit = script("tdlib_build_audit")
    roots = [tmp_path / name for name in ("reviewed", "source", "output")]
    for root in roots:
        root.mkdir()
    policy = "\n".join(f"{key}:STRING={value}" for key, value in audit.POLICY.items())
    audit.cache(policy, roots)
    configured_values = {
        flag[2:].split("=", 1)[0].split(":", 1)[0]: flag.split("=", 1)[1]
        for flag in flags
        if flag.startswith("-D")
    }
    assert all(configured_values[key] == value for key, value in audit.POLICY.items())
    for cache in (
        f"CRC32C_LIBRARY:FILEPATH={tmp_path}/ambient/libcrc32c.a",
        "CRC32C_FOUND:BOOL=ON",
        f"CMAKE_CXX_COMPILER_LAUNCHER:STRING={tmp_path}/ambient/ccache",
    ):
        with pytest.raises(ValueError):
            audit.cache(cache, roots)
    for fragment in (
        f"-I{tmp_path}/ambient/include",
        f"{tmp_path}/ambient/libcrc32c.a",
        "-lcrc32c",
        "ccache",
        "../../ambient/libunknown.a",
        "-include ../../ambient/header.h",
        "-I../../ambient/include",
        "-B/usr/local/bin",
        "-fuse-ld=unreviewed",
    ):
        with pytest.raises(ValueError):
            audit.fragment(fragment, roots, "macos-arm64")
    with pytest.raises(ValueError):
        audit.dependencies(
            f"object: #deps 1\n    {tmp_path}/ambient/header.h\n", roots, roots[-1]
        )


def test_tdlib_codemodel_rejects_an_outside_include_and_actual_headers(tmp_path):
    import json

    audit = script("tdlib_build_audit")
    source, inputs, build = (
        tmp_path / name for name in ("source", "reviewed", "output/build")
    )
    source.mkdir()
    inputs.mkdir()
    build.mkdir(parents=True)
    reply = build / ".cmake/api/v1/reply"
    reply.mkdir(parents=True)
    (build / "CMakeCache.txt").write_text(
        "\n".join(f"{key}:STRING={value}" for key, value in audit.POLICY.items())
    )

    def write(name, value):
        (reply / name).write_text(json.dumps(value))

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
                        "path": str(inputs / "clang++"),
                        "implicit": {
                            "includeDirectories": [str(inputs / "sdk")],
                            "linkLibraries": ["System"],
                        },
                    }
                }
            ]
        },
    )
    write("code.json", {"configurations": [{"targets": [{"jsonFile": "target.json"}]}]})
    target = {
        "compileGroups": [{"includes": [{"path": str(source)}]}],
        "link": {"commandFragments": [{"fragment": "-lSystem"}]},
    }
    write("target.json", target)
    assert (
        audit.configured(build, source, inputs, "macos-arm64")["configured_targets"]
        == 1
    )
    target["compileGroups"][0]["includes"][0]["path"] = str(
        tmp_path / "ambient/include"
    )
    write("target.json", target)
    with pytest.raises(ValueError, match="Unreviewed"):
        audit.configured(build, source, inputs, "macos-arm64")
    assert (
        audit.dependencies(
            f"object: #deps 1\n    {source}/header.h\n",
            [inputs, source, build.parent],
            build,
        )
        == 1
    )


def test_tdlib_configured_receipt_requires_discovery_policy_in_cache(tmp_path):
    audit = script("tdlib_build_audit")
    with pytest.raises(ValueError, match="policy"):
        audit.cache("CMAKE_CXX_COMPILER_LAUNCHER:STRING=\n", [tmp_path])
