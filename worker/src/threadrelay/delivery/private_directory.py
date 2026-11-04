"""Private TDLib session directories; no chmod/ACL relaxation of existing data."""

import os
from pathlib import Path
import stat


def private_directory(path: Path) -> None:
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Private directory unavailable")
    # Reject every existing link/reparse component, including the root itself.
    for part in [*reversed(path.parents), path]:
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Private directory unavailable")
    if os.name == "nt":
        _windows_private(path)
        return
    # Keep traversal and final validation anchored to no-follow descriptors.
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(path.anchor, flags)
    try:
        for index, part in enumerate(path.parts[1:]):
            if index == len(path.parts) - 2:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or os.getuid() != os.geteuid()
        ):
            raise ValueError("Private directory permissions unavailable")
        if _is_darwin() and _darwin_acl_tags(fd):
            # Conservative: no extended ACE, including unknown tags, grants
            # access in a session directory. Never edit an existing ACL.
            raise ValueError("Private directory ACL unavailable")
        after = os.fstat(fd)
        if (after.st_dev, after.st_ino, after.st_mode, after.st_uid) != (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_uid,
        ):
            raise ValueError("Private directory identity changed")
    finally:
        os.close(fd)


def _is_darwin():
    import sys

    return sys.platform == "darwin"


def _darwin_acl_tags(fd):
    """Read native ACL from the already opened directory, fail closed on error."""
    import ctypes as c
    import errno

    libc = c.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    libc.acl_get_fd_np.argtypes = [c.c_int, c.c_int]
    libc.acl_get_fd_np.restype = c.c_void_p
    libc.acl_get_entry.argtypes = [c.c_void_p, c.c_int, c.POINTER(c.c_void_p)]
    libc.acl_get_entry.restype = c.c_int
    libc.acl_get_tag_type.argtypes = [c.c_void_p, c.POINTER(c.c_int)]
    libc.acl_get_tag_type.restype = c.c_int
    libc.acl_free.argtypes = [c.c_void_p]
    libc.acl_free.restype = c.c_int
    c.set_errno(0)
    acl = libc.acl_get_fd_np(fd, 0x100)  # ACL_TYPE_EXTENDED (Darwin SDK)
    if not acl:
        # On macOS, a descriptor with no extended ACL reports ENOENT. The
        # caller holds and revalidates this same open descriptor around the
        # query, so this case means there are no entries; all other failures
        # remain closed.
        if c.get_errno() == errno.ENOENT:
            return []
        raise OSError("Private directory ACL unavailable")
    tags = []
    try:
        selector = 0  # ACL_FIRST_ENTRY
        while True:
            entry = c.c_void_p()
            c.set_errno(0)
            result = libc.acl_get_entry(acl, selector, c.byref(entry))
            if result != 0:
                if result == -1 and c.get_errno() == errno.EINVAL:
                    return tags  # Darwin exhausts entries with EINVAL
                raise OSError("Private directory ACL unavailable")
            tag = c.c_int()
            if libc.acl_get_tag_type(entry, c.byref(tag)) != 0:
                raise OSError("Private directory ACL unavailable")
            tags.append(tag.value)
            if len(tags) > 128:
                raise OSError("Private directory ACL unavailable")
            selector = -1  # ACL_NEXT_ENTRY
    finally:
        libc.acl_free(acl)


def _windows_private(path):
    """Create atomically with protected owner-only DACL; verify existing ACL.

    Windows native acceptance must exercise SID/ACL behavior; POSIX tests do
    not prove it. No shell, global policy, registry or inherited permissive ACL.
    """
    import ctypes as c
    from ctypes import wintypes as w

    adv = c.WinDLL("advapi32", use_last_error=True)
    kernel = c.WinDLL("kernel32", use_last_error=True)
    ptr = c.c_void_p

    class SecurityAttributes(c.Structure):
        _fields_ = [("length", w.DWORD), ("descriptor", ptr), ("inherit", w.BOOL)]

    class Acl(c.Structure):
        _fields_ = [
            ("revision", w.BYTE),
            ("reserved", w.BYTE),
            ("size", w.WORD),
            ("count", w.WORD),
            ("reserved2", w.WORD),
        ]

    def bind(dll, name, args, result):
        fn = getattr(dll, name)
        fn.argtypes = args
        fn.restype = result
        return fn

    convert = bind(
        adv,
        "ConvertStringSecurityDescriptorToSecurityDescriptorW",
        [w.LPCWSTR, w.DWORD, c.POINTER(ptr), c.POINTER(w.DWORD)],
        w.BOOL,
    )
    create = bind(
        kernel, "CreateDirectoryW", [w.LPCWSTR, c.POINTER(SecurityAttributes)], w.BOOL
    )
    free = bind(kernel, "LocalFree", [ptr], ptr)
    info = bind(
        adv,
        "GetNamedSecurityInfoW",
        [
            w.LPWSTR,
            w.DWORD,
            w.DWORD,
            c.POINTER(ptr),
            c.POINTER(ptr),
            c.POINTER(ptr),
            c.POINTER(ptr),
            c.POINTER(ptr),
        ],
        w.DWORD,
    )
    control = bind(
        adv,
        "GetSecurityDescriptorControl",
        [ptr, c.POINTER(w.WORD), c.POINTER(w.DWORD)],
        w.BOOL,
    )
    ace = bind(adv, "GetAce", [ptr, w.DWORD, c.POINTER(ptr)], w.BOOL)
    equal = bind(adv, "EqualSid", [ptr, ptr], w.BOOL)
    token_open = bind(
        adv, "OpenProcessToken", [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)], w.BOOL
    )
    token_info = bind(
        adv,
        "GetTokenInformation",
        [w.HANDLE, c.c_int, ptr, w.DWORD, c.POINTER(w.DWORD)],
        w.BOOL,
    )
    current = bind(kernel, "GetCurrentProcess", [], w.HANDLE)
    close = bind(kernel, "CloseHandle", [w.HANDLE], w.BOOL)
    sid_convert = bind(
        adv, "ConvertStringSidToSidW", [w.LPCWSTR, c.POINTER(ptr)], w.BOOL
    )
    descriptor = ptr()
    actual = ptr()
    owner = ptr()
    dacl = ptr()
    owner_rights = ptr()
    token = w.HANDLE()
    try:
        if not convert("D:P(A;;FA;;;OW)", 1, c.byref(descriptor), None):
            raise ValueError
        attributes = SecurityAttributes(c.sizeof(SecurityAttributes), descriptor, False)
        if not create(str(path), c.byref(attributes)) and c.get_last_error() != 183:
            raise ValueError
        if (
            info(
                str(path),
                1,
                1 | 4,
                c.byref(owner),
                None,
                c.byref(dacl),
                None,
                c.byref(actual),
            )
            != 0
        ):
            raise ValueError
        flags = w.WORD()
        revision = w.DWORD()
        if (
            not control(actual, c.byref(flags), c.byref(revision))
            or not flags.value & 0x1000
            or not dacl
        ):
            raise ValueError
        if not token_open(current(), 8, c.byref(token)):
            raise ValueError
        size = w.DWORD()
        token_info(token, 1, None, 0, c.byref(size))
        if not 0 < size.value <= 65536:
            raise ValueError
        buffer = c.create_string_buffer(size.value)
        if not token_info(token, 1, buffer, size, c.byref(size)):
            raise ValueError
        user_sid = c.cast(buffer, c.POINTER(ptr))[0]
        if (
            not equal(owner, user_sid)
            or c.cast(dacl, c.POINTER(Acl)).contents.count != 1
        ):
            raise ValueError
        entry = ptr()
        if not ace(dacl, 0, c.byref(entry)) or not sid_convert(
            "S-1-3-4", c.byref(owner_rights)
        ):
            raise ValueError
        header = c.string_at(entry, 8)
        if (
            header[0] != 0
            or header[1] != 0
            or int.from_bytes(header[4:8], "little") != 0x001F01FF
        ):
            raise ValueError
        sid = ptr(entry.value + 8)
        if not (equal(sid, owner_rights) or equal(sid, user_sid)):
            raise ValueError
    except (OSError, ValueError):
        raise ValueError("Private directory ACL unavailable") from None
    finally:
        if token:
            close(token)
        for value in (descriptor, actual, owner_rights):
            if value:
                free(value)
