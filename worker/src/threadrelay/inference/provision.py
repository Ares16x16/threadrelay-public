"""Pinned local artifact verification; acquisition requires a separate consent.

The optional source is a trusted setup adapter, not an arbitrary URL bridge.
There is no default network downloader, extractor, installer or model execution.
"""
from collections.abc import Callable
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, BinaryIO, Protocol

from threadrelay.contracts import ProtocolError, validate_contract

ModelManifest = dict[str, Any]
VerificationResult = dict[str, Any]
CHUNK_BYTES = 1024 * 1024


class ProvisioningError(RuntimeError):
    """Static, redacted failure codes only."""


@dataclass(frozen=True)
class DownloadConsent:
    """Trusted setup receipt bound to the exact selected artifact, not a bool.

    No IPC accepts this object. A future setup UI/native bridge must obtain the
    user's explicit choice before constructing it; this is not that UI.
    """
    model_id: str
    sha256: str


class ApprovedModelSource(Protocol):
    """Future setup-only HTTPS adapter: bounded time, no unapproved redirects.

    Must request only the supplied pinned publisher URL; any CDN redirect must
    remain HTTPS and be authenticated to the publisher's artifact origin. The
    stream must be cancellable/bounded by that adapter. No such network adapter
    is configured by default or reachable through current worker IPC.
    """
    def open(self, url: str) -> BinaryIO: ...


def load_catalog() -> dict[str, Any]:
    """Load the catalog bundled unchanged by generate-model-manifest.py."""
    bundled = files("threadrelay.inference").joinpath("manifest.json")
    return json.loads(bundled.read_text(encoding="utf-8"))


class ModelProvisioner:
    def __init__(self, *, catalog: dict[str, Any] | None = None,
                 download_source: ApprovedModelSource | None = None):
        # catalog injection is internal application/test DI, never renderer JSON.
        self._catalog = deepcopy(load_catalog() if catalog is None else catalog)
        self._source = download_source
        for record in self._catalog["models"]:
            validate_contract("ModelManifest", record["manifest"])
            if (type(record["size_bytes"]) is not int or record["size_bytes"] < 8
                    or Path(record["filename"]).name != record["filename"]
                    or record["format"] != "GGUF-v3" or record["decision_type"] != "h2o-lightning"):
                raise ValueError("invalid_trusted_catalog")

    @property
    def primary_manifest(self) -> ModelManifest:
        return deepcopy(next(r["manifest"] for r in self._catalog["models"]
                             if r["manifest"]["id"] == self._catalog["primary_model_id"]))

    @property
    def runtime_pin(self) -> dict[str, Any]:
        return deepcopy(self._catalog["runtime"])

    def _record(self, manifest: ModelManifest) -> dict[str, Any] | None:
        try:
            validate_contract("ModelManifest", manifest)
        except ProtocolError:
            return None
        return next((r for r in self._catalog["models"] if r["manifest"] == manifest), None)

    @staticmethod
    def _verify(file: Path, digest: str, size: int | None, *, gguf: bool,
                cancelled: Callable[[], bool]) -> VerificationResult:
        def fail(reason):
            return {"valid": False, "reason": reason}
        try:
            if file.is_symlink():
                return fail("invalid_file")
            # O_NONBLOCK avoids waiting on FIFOs before fstat rejects them.
            fd = os.open(file, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode):
                    return fail("invalid_file")
                if size is not None and before.st_size != size:
                    return fail("size_mismatch")
                sha = hashlib.sha256()
                first = True
                while True:
                    if cancelled():
                        return fail("cancelled")
                    chunk = stream.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    if first and gguf and (len(chunk) < 8 or chunk[:8] != b"GGUF\x03\0\0\0"):
                        return fail("invalid_gguf")
                    first = False
                    sha.update(chunk)
                after = os.fstat(stream.fileno())
                if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                    return fail("file_changed")
                if gguf and first:
                    return fail("invalid_gguf")
                if sha.hexdigest() != digest:
                    return fail("checksum_mismatch")
            return {"valid": True, "reason": None}
        except (OSError, ValueError):
            return fail("invalid_file")

    def verify(self, manifest: ModelManifest, file: Path, *,
               cancelled: Callable[[], bool] = lambda: False) -> VerificationResult:
        record = self._record(manifest)
        if record is None:
            return {"valid": False, "reason": "unrecognized_model"}
        # Exact trusted checksum, not a guess from GGUF's self-reported metadata,
        # distinguishes the decision-enabled artifact from stock chat weights.
        return self._verify(file, manifest["sha256"], record["size_bytes"], gguf=True, cancelled=cancelled)

    def verify_runtime_archive(self, platform: str, file: Path) -> VerificationResult:
        artifact = next((a for a in self._catalog["runtime"]["artifacts"] if a["platform"] == platform), None)
        if artifact is None:
            return {"valid": False, "reason": "unsupported_platform"}
        # Official exact archive byte counts were not established; do not invent
        # them from rounded display sizes. SHA256 still binds all file bytes.
        return self._verify(file, artifact["sha256"], None, gguf=False, cancelled=lambda: False)

    def download(self, manifest: ModelManifest, directory: Path, *, consent: DownloadConsent | None = None) -> Path:
        record = self._record(manifest)
        if record is None:
            raise ProvisioningError("unrecognized_model")
        if type(consent) is not DownloadConsent or consent != DownloadConsent(manifest["id"], manifest["sha256"]):
            raise ProvisioningError("download_consent_required")
        if self._source is None:
            raise ProvisioningError("approved_download_adapter_unavailable")
        # Directory is a trusted installation path; never an IPC-supplied path.
        if not directory.is_absolute() or not directory.is_dir() or any(p.is_symlink() for p in (directory, *directory.parents)):
            raise ProvisioningError("invalid_install_directory")
        target = directory / record["filename"]
        if target.exists() or target.is_symlink():
            raise ProvisioningError("artifact_already_exists")
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=directory, prefix=".threadrelay-model-", delete=False) as output:
                temporary = Path(output.name)
                with closing(self._source.open(manifest["url"])) as stream:
                    total = 0
                    while chunk := stream.read(CHUNK_BYTES):
                        total += len(chunk)
                        if total > record["size_bytes"]:
                            raise ProvisioningError("download_size_limit")
                        output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            result = self.verify(manifest, temporary)
            if not result["valid"]:
                raise ProvisioningError(result["reason"])
            # Atomic no-replace publication; never overwrite a concurrently
            # installed artifact. Same-directory hardlink is supported on the
            # proposed local NTFS/APFS installs; unsupported filesystems fail.
            os.link(temporary, target)
            return target
        except ProvisioningError:
            raise
        except Exception:
            raise ProvisioningError("download_failed") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
