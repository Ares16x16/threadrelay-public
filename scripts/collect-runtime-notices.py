#!/usr/bin/env python3
"""Target-only conservative build/runtime notice collector, not legal clearance."""

import argparse
import importlib.metadata as metadata
import json
from pathlib import Path
import platform
import re
import shutil
import sys
from packaging_common import (
    check_interpreter_identity,
    digest,
    inventory_tree,
    stage_tree,
)

ROOT = Path(__file__).resolve().parents[1]


def collect(target, python_license, output):
    if (platform.system(), platform.machine()) != (
        {"windows-x64": ("Windows", "AMD64"), "macos-arm64": ("Darwin", "arm64")}[
            target
        ]
    ):
        raise ValueError(
            "Collect actual target interpreter notices, not cloud substitutes"
        )
    python_license, output = Path(python_license), Path(output)
    if not python_license.is_file() or python_license.is_symlink():
        raise ValueError("Exact target Python LICENSE required")
    pin = json.loads((ROOT / "runtimes/package-manifest.json").read_text())["targets"][
        target
    ]
    check_interpreter_identity(
        pin, Path(sys.executable), platform.python_version(), python_license
    )
    output.mkdir(mode=0o700)
    components = []
    for dist in sorted(
        metadata.distributions(), key=lambda d: d.metadata["Name"].lower()
    ):
        name = dist.metadata["Name"]
        version = dist.version
        if name.lower().startswith("threadrelay"):
            continue
        destination = output / "python-packages" / f"{name}-{version}"
        copied = []
        for relative in dist.files or []:
            if not re.search(
                r"(^|/)(licen[sc]e|notice|copying)([-._/]|$)", str(relative), re.I
            ):
                continue
            source = Path(dist.locate_file(relative))
            if not source.is_file() or source.is_symlink():
                continue
            if source.stat().st_size > 4 * 1024**2:
                raise ValueError("Unexpected license file size")
            destination.mkdir(parents=True, exist_ok=True)
            filename = f"{len(copied):04d}-{source.name}"
            shutil.copyfile(source, destination / filename)
            copied.append((destination / filename).relative_to(output).as_posix())
        if not copied:
            raise ValueError(f"License text missing: {name}")
        components.append(
            {
                "name": name,
                "version": version,
                "notice_files": copied,
                "scope": "conservative builder and worker environment; inspect actual frozen inventory",
            }
        )
    (output / "python-interpreter").mkdir()
    shutil.copyfile(python_license, output / "python-interpreter/LICENSE")
    components.append(
        {
            "name": "python-interpreter",
            "version": platform.python_version(),
            "interpreter_input_sha256": digest(Path(sys.executable)),
            "notice_files": ["python-interpreter/LICENSE"],
        }
    )
    import playwright

    driver = Path(playwright.__file__).parent / "driver"
    # Preserve Node's full incorporated notices and core's exact attributions.
    destination = output / "playwright-driver"
    destination.mkdir()
    driver_notices = []
    for source in driver.rglob("*"):
        if (
            source.is_file()
            and not source.is_symlink()
            and re.search(r"(LICENSE|NOTICE|COPYING)", source.name, re.I)
        ):
            target_path = destination / source.relative_to(driver)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target_path)
            driver_notices.append(target_path.relative_to(output).as_posix())
    if not driver_notices or not (destination / "LICENSE").is_file():
        raise ValueError("Complete driver notices missing")
    components.append(
        {
            "name": "playwright-driver",
            "version": "1.64.0-beta-1791568591000 / Node 24.22.1",
            "notice_files": driver_notices,
        }
    )
    frontend = ROOT / "packaging/notices/frontend"
    stage_tree(frontend, output / "frontend", inventory_tree(frontend))
    components.append(
        {
            "name": "frontend",
            "lock_sha256": digest(ROOT / "apps/desktop/pnpm-lock.yaml"),
            "notice_files": ["frontend/THIRD_PARTY_NOTICES.txt", "frontend/sbom.json"],
        }
    )
    (output / "THIRD_PARTY_NOTICES.md").write_text(
        "# ThreadRelay source/offline package notices\n\nFull license texts and incorporated notices are retained in the component directories. This conservative collection includes builder-only dependencies; see sbom.json and the final measured frozen inventory. Browser, TDLib, llama runtime and model weights are omitted from this offline package. Adding them requires their exact licenses/notices and target review. Not a legal redistribution clearance.\n"
    )
    entries = inventory_tree(output)
    sbom = {
        "schema_version": 1,
        "scope": "complete-target-offline-build-and-runtime",
        "target": target,
        "native_components": "omitted",
        "components": components,
        "notice_inventory": entries,
    }
    (output / "sbom.json").write_text(json.dumps(sbom, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target", choices=["windows-x64", "macos-arm64"], required=True
    )
    parser.add_argument("--python-license", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    collect(args.target, args.python_license, args.output)


if __name__ == "__main__":
    main()
