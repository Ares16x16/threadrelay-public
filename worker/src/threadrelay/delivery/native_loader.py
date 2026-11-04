"""Private capability from the generation-owned host lane; not a JSON receipt.

The host has already pinned the entire dependency tree and target no-flush
acceptance and retains its stable execution guard through worker termination.
This is not protection against arbitrary code inside the trusted worker.
"""

import ctypes
import os
from pathlib import Path
from threading import Lock

_KEY = object()


class _VerifiedLibrary:
    def __init__(self, key, path, generation, is_live, purpose):
        if key is not _KEY:
            raise ValueError("Private loader capability required")
        self._path, self._generation, self._is_live = path, generation, is_live
        self._purpose = purpose
        self._used = False
        self._lock = Lock()


def _from_host(path: Path, *, generation: int, is_live, purpose="delivery"):
    """Only HostControl calls this after its exact private response validation."""
    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or ".." in path.parts
        or generation < 1
        or purpose not in {"connection_only", "delivery"}
    ):
        raise ValueError("Private loader capability invalid")
    return _VerifiedLibrary(_KEY, path, generation, is_live, purpose)


def _load(capability):
    from .telegram import NativeUnavailable

    if type(capability) is not _VerifiedLibrary:
        raise NativeUnavailable("Native TDLib verification unavailable")
    with capability._lock:
        if capability._used or not capability._is_live():
            raise NativeUnavailable("Native TDLib capability revoked")
        capability._used = True
        try:
            # Absolute fixed path, C ABI. Never find_library, WinDLL, PATH, or
            # inherited current-directory DLL search. Non-OS dependencies are
            # included in the host's retained complete verified bundle.
            kwargs = {"winmode": 0x100 | 0x800} if os.name == "nt" else {}
            return ctypes.CDLL(str(capability._path), **kwargs)
        except (OSError, ValueError):
            raise NativeUnavailable("Native TDLib load unavailable") from None
