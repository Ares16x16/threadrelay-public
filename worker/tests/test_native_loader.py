"""Loader unit tests use an inert library; never native safety evidence."""

from pathlib import Path
import pytest
from threadrelay.delivery.telegram import NativeJsonBinding, NativeUnavailable


def test_caller_receipts_and_environment_cannot_load_library(monkeypatch):
    monkeypatch.setenv("THREADRELAY_NATIVE_VERIFIED", "1")
    monkeypatch.setattr("ctypes.CDLL", lambda *a, **k: pytest.fail("must not load"))
    for receipt in [
        None,
        {},
        {"path": "/tmp/tdjson", "verified": True},
        Path("/tmp/tdjson"),
    ]:
        with pytest.raises(NativeUnavailable):
            NativeJsonBinding.load_verified(receipt)


def test_private_host_load_capability_uses_exact_absolute_path_and_c_abi(
    monkeypatch, tmp_path
):
    from threadrelay.delivery.native_loader import _from_host

    calls = []

    class Export:
        argtypes = None
        restype = None

    class Library:
        td_create_client_id = Export()
        td_send = Export()
        td_receive = Export()
        td_execute = Export()

    library = Library()
    monkeypatch.setattr(
        "ctypes.CDLL", lambda path, **kwargs: calls.append((path, kwargs)) or library
    )
    path = tmp_path / "libtdjson.dylib"
    capability = _from_host(path, generation=1, is_live=lambda: True)
    binding = NativeJsonBinding.load_verified(capability)
    assert binding._library is library
    assert calls[0][0] == str(path)
    assert library.td_create_client_id.argtypes == []
    assert len(library.td_send.argtypes) == 2
    with pytest.raises(NativeUnavailable):
        NativeJsonBinding.load_verified(capability)  # one-shot
    with pytest.raises(NativeUnavailable):
        NativeJsonBinding.load_verified(
            _from_host(path, generation=2, is_live=lambda: False)
        )


@pytest.mark.skipif(
    __import__("os").name == "nt",
    reason="POSIX mode test; Windows ACL native gate separate",
)
def test_existing_permissive_session_root_and_symlink_are_rejected_before_native_creation(
    tmp_path,
):
    from threadrelay.delivery.telegram import TelegramClient, TelegramBoundaryError

    root = tmp_path / "telegram"
    root.mkdir(mode=0o755)
    client = TelegramClient(
        lambda: pytest.fail("must not create native client"), None, root
    )
    with pytest.raises(TelegramBoundaryError):
        client.initialize_offline("vault:fixture")
    root.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    root.symlink_to(outside, target_is_directory=True)
    client = TelegramClient(
        lambda: pytest.fail("must not create native client"), None, root
    )
    with pytest.raises(TelegramBoundaryError):
        client.initialize_offline("vault:fixture")
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("tags", [[1], OSError("synthetic ACL read failure")])
def test_darwin_session_acl_grants_or_read_failure_prevent_native_creation(
    monkeypatch, tmp_path, tags
):
    import threadrelay.delivery.private_directory as private
    from threadrelay.delivery.telegram import TelegramClient, TelegramBoundaryError

    root = tmp_path / "telegram"
    root.mkdir(mode=0o700)

    def inspect(fd):
        import os

        assert os.fstat(fd).st_ino == root.stat().st_ino
        if isinstance(tags, Exception):
            raise tags
        return tags

    monkeypatch.setattr(private, "_darwin_acl_tags", inspect, raising=False)
    monkeypatch.setattr(private, "_is_darwin", lambda: True, raising=False)
    client = TelegramClient(
        lambda: pytest.fail("native construction forbidden"), None, root
    )
    with pytest.raises(TelegramBoundaryError):
        client.initialize_offline("vault:fixture")
    assert root.stat().st_mode & 0o777 == 0o700
    assert list(root.iterdir()) == []


def test_darwin_acl_adapter_uses_sdk_iteration_and_frees_on_failure(monkeypatch):
    import ctypes
    import errno
    from threadrelay.delivery.private_directory import _darwin_acl_tags

    calls = []

    class Fn:
        def __init__(self, function):
            self.function = function

        def __call__(self, *args):
            return self.function(*args)

    def entry(acl, selector, output):
        calls.append(selector)
        if selector == 0:
            output._obj.value = 2
            return 0
        assert selector == -1  # Darwin ACL_NEXT_ENTRY, not a POSIX guess
        ctypes.set_errno(errno.EINVAL)
        return -1

    class Lib:
        acl_get_fd_np = Fn(lambda fd, kind: 1 if kind == 0x100 else 0)
        acl_get_entry = Fn(entry)
        acl_get_tag_type = Fn(lambda entry, tag: setattr(tag._obj, "value", 1) or 0)
        acl_free = Fn(lambda acl: calls.append("freed") or 0)

    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: Lib())
    assert _darwin_acl_tags(7) == [1]
    assert calls == [0, -1, "freed"]


def test_darwin_acl_adapter_treats_enoent_as_absent_acl(monkeypatch):
    import ctypes
    import errno
    from threadrelay.delivery.private_directory import _darwin_acl_tags

    class Fn:
        def __call__(self, fd, kind):
            assert kind == 0x100
            ctypes.set_errno(errno.ENOENT)
            return None

    class Lib:
        acl_get_fd_np = Fn()
        acl_get_entry = Fn()
        acl_get_tag_type = Fn()
        acl_free = Fn()

    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: Lib())
    assert _darwin_acl_tags(7) == []


def test_new_darwin_session_directory_acl_is_checked_before_native_creation(
    monkeypatch, tmp_path
):
    import os
    import threadrelay.delivery.private_directory as private
    from threadrelay.delivery.telegram import TelegramClient, TelegramBoundaryError

    root = tmp_path / "telegram"
    seen = []

    def inspect(fd):
        info = os.fstat(fd)
        seen.append((info.st_uid, info.st_mode & 0o777))
        return [1]

    monkeypatch.setattr(private, "_darwin_acl_tags", inspect)
    monkeypatch.setattr(private, "_is_darwin", lambda: True)
    client = TelegramClient(
        lambda: pytest.fail("native construction forbidden"), None, root
    )
    with pytest.raises(TelegramBoundaryError):
        client.initialize_offline("vault:fixture")
    assert seen == [(os.geteuid(), 0o700)]
    assert root.is_dir() and list(root.iterdir()) == []
