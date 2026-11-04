"""Synthetic byte streams only; no multi-GB weights or runtime are acquired."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path

import pytest

from threadrelay.inference.provision import (
    DownloadConsent, ModelProvisioner, ProvisioningError, load_catalog,
)

ROOT = Path(__file__).parents[2]
SYNTHETIC_GGUF = b"GGUF\x03\x00\x00\x00" + b"synthetic-not-real-weights" * 1024


def synthetic_catalog():
    catalog = deepcopy(load_catalog())
    record = catalog["models"][0]
    record["manifest"]["sha256"] = hashlib.sha256(SYNTHETIC_GGUF).hexdigest()
    record["size_bytes"] = len(SYNTHETIC_GGUF)
    return catalog


def model(catalog):
    return deepcopy(catalog["models"][0]["manifest"])


def test_official_catalog_selects_decision_file_and_exact_immutable_revision():
    catalog = load_catalog()
    provisioner = ModelProvisioner()
    primary = provisioner.primary_manifest
    assert primary["id"] == "h2o-lightning-4b-q4-k-m"
    assert primary["sha256"] == "03b90d043897e0de78983e36702f182d19fb6b2321586707841533ea7d8c407d"
    assert primary["url"] == "https://huggingface.co/h2oai/h2o-lightning-4b-gguf/resolve/ff38e4448f584d30c140799e6e3f150dace64e23/h2o-lightning-4b-Q4_K_M.gguf"
    assert primary["license"] == "Apache-2.0"
    assert primary["runtime_version"] == "b11946"
    assert catalog["models"][0]["size_bytes"] == 2708808736
    assert catalog["models"][1]["manifest"]["sha256"] == "c5bf7cb8485f60c3a3347a9975424f258c6017e1bb8663aaf0447b822fde1b59"
    assert catalog["models"][1]["size_bytes"] == 4482407456
    assert catalog["runtime"]["commit"] == "8ca4060b7e30e76baaa5670ee1ec1b760b7f8552"
    assert {v["platform"] for v in catalog["runtime"]["artifacts"]} == {"windows-x64", "macos-arm64"}
    assert provisioner.primary_manifest is not primary  # no mutable pin reference escapes


def test_streaming_verification_reads_bounded_chunks(tmp_path, monkeypatch):
    catalog = synthetic_catalog()
    path = tmp_path / "model.gguf"
    path.write_bytes(SYNTHETIC_GGUF)
    # An accidental whole-file Path.read_bytes shortcut must not return valid.
    monkeypatch.setattr(Path, "read_bytes", lambda _: pytest.fail("unbounded read"))
    result = ModelProvisioner(catalog=catalog).verify(model(catalog), path)
    assert result == {"valid": True, "reason": None}


@pytest.mark.parametrize("bad_bytes,reason", [
    (b"GGUF\x03\x00\x00\x00" + b"changed", "size_mismatch"),
    (b"CHAT\x03\x00\x00\x00" + SYNTHETIC_GGUF[8:], "invalid_gguf"),
    (b"GGUF\x01\x00\x00\x00" + SYNTHETIC_GGUF[8:], "invalid_gguf"),
    (SYNTHETIC_GGUF[:-1] + b"!", "checksum_mismatch"),
])
def test_invalid_model_bytes_are_rejected(tmp_path, bad_bytes, reason):
    catalog = synthetic_catalog()
    path = tmp_path / "bad.gguf"
    path.write_bytes(bad_bytes)
    assert ModelProvisioner(catalog=catalog).verify(model(catalog), path) == {"valid": False, "reason": reason}


def test_missing_directory_and_symlink_are_not_model_files(tmp_path):
    catalog = synthetic_catalog()
    provisioner = ModelProvisioner(catalog=catalog)
    assert not provisioner.verify(model(catalog), tmp_path / "missing")["valid"]
    assert not provisioner.verify(model(catalog), tmp_path)["valid"]
    path = tmp_path / "real.gguf"
    path.write_bytes(SYNTHETIC_GGUF)
    alias = tmp_path / "alias.gguf"
    alias.symlink_to(path)
    assert not provisioner.verify(model(catalog), alias)["valid"]


@pytest.mark.parametrize("field,value", [("sha256", "a" * 64), ("url", "https://evil.invalid/a"), ("quantization", "stock"), ("runtime_version", "b11772"), ("license", "unknown")])
def test_unregistered_manifest_cannot_bypass_pins(tmp_path, field, value):
    catalog = synthetic_catalog()
    path = tmp_path / "model.gguf"
    path.write_bytes(SYNTHETIC_GGUF)
    changed = {**model(catalog), field: value}
    assert ModelProvisioner(catalog=catalog).verify(changed, path) == {"valid": False, "reason": "unrecognized_model"}


class BoundedSource:
    def __init__(self, body):
        self.body = body
        self.opened = []
        self.read_sizes = []
        self.closed = False

    def open(self, url):
        self.opened.append(url)
        owner = self
        class Stream(io.BytesIO):
            def read(self, n=-1):
                assert 0 < n <= 1024 * 1024
                owner.read_sizes.append(n)
                return super().read(n)
            def close(self):
                owner.closed = True
                super().close()
        return Stream(self.body)


def test_download_denied_without_artifact_bound_explicit_consent(tmp_path):
    catalog = synthetic_catalog()
    source = BoundedSource(SYNTHETIC_GGUF)
    provisioner = ModelProvisioner(catalog=catalog, download_source=source)
    for consent in (None, True, DownloadConsent("wrong", "a" * 64)):
        with pytest.raises(ProvisioningError, match="download_consent_required"):
            provisioner.download(model(catalog), tmp_path, consent=consent)
    assert source.opened == []
    assert list(tmp_path.iterdir()) == []


def test_download_only_pinned_url_and_publish_after_hash_verification(tmp_path):
    catalog = synthetic_catalog()
    source = BoundedSource(SYNTHETIC_GGUF)
    manifest = model(catalog)
    path = ModelProvisioner(catalog=catalog, download_source=source).download(
        manifest, tmp_path, consent=DownloadConsent(manifest["id"], manifest["sha256"]))
    assert path.name == "h2o-lightning-4b-Q4_K_M.gguf"
    assert path.read_bytes() == SYNTHETIC_GGUF
    assert source.opened == [manifest["url"]]
    assert source.closed
    assert len(list(tmp_path.iterdir())) == 1


@pytest.mark.parametrize("body", [SYNTHETIC_GGUF[:-1] + b"!", SYNTHETIC_GGUF + b"overflow"])
def test_download_corruption_or_oversize_never_publishes_file(tmp_path, body):
    catalog = synthetic_catalog()
    source = BoundedSource(body)
    manifest = model(catalog)
    with pytest.raises(ProvisioningError):
        ModelProvisioner(catalog=catalog, download_source=source).download(
            manifest, tmp_path, consent=DownloadConsent(manifest["id"], manifest["sha256"]))
    assert source.closed
    assert list(tmp_path.iterdir()) == []


def test_runtime_archive_checksum_uses_pinned_platform_artifact(tmp_path):
    catalog = synthetic_catalog()
    body = b"synthetic archive; not executable"
    catalog["runtime"]["artifacts"][0]["sha256"] = hashlib.sha256(body).hexdigest()
    path = tmp_path / "runtime.zip"
    path.write_bytes(body)
    provisioner = ModelProvisioner(catalog=catalog)
    assert provisioner.verify_runtime_archive("windows-x64", path)["valid"] is True
    path.write_bytes(b"untrusted")
    assert provisioner.verify_runtime_archive("windows-x64", path)["valid"] is False
    assert provisioner.verify_runtime_archive("linux-x64", path)["valid"] is False


def test_verification_cancels_between_chunks_without_valid_receipt(tmp_path):
    catalog = synthetic_catalog()
    path = tmp_path / "synthetic.gguf"
    path.write_bytes(SYNTHETIC_GGUF)
    checks = [False, True]
    result = ModelProvisioner(catalog=catalog).verify(model(catalog), path, cancelled=lambda: checks.pop(0))
    assert result == {"valid": False, "reason": "cancelled"}


def test_catalog_and_download_are_not_implicitly_network_enabled(tmp_path):
    catalog = synthetic_catalog()
    manifest = model(catalog)
    with pytest.raises(ProvisioningError, match="approved_download_adapter_unavailable"):
        ModelProvisioner(catalog=catalog).download(manifest, tmp_path, consent=DownloadConsent(manifest["id"], manifest["sha256"]))
    assert list(tmp_path.iterdir()) == []


def test_packaged_catalog_matches_root_source_of_truth():
    # A stale bundled copy would silently provision different model pins.
    from importlib.resources import files
    packaged = files("threadrelay.inference").joinpath("manifest.json")
    assert packaged.is_file()
    assert json.loads(packaged.read_text()) == json.loads((ROOT / "models/manifest.json").read_text())
