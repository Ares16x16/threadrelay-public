"""Trusted local build-tree inventory. Generic downloaded archives stay separate.

This creates identity candidates, NEVER native acceptance. Stage regular files
before links; no destination writes can traverse a link supplied by a bundle.
"""

from collections import deque
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat

MAX_ENTRIES = 100_000
MAX_BYTES = 64 * 1024**3


def digest(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def same_file_snapshot(before, after):
    # Reading a file may update st_atime on macOS and other filesystems. Check
    # identity and mutation metadata while excluding that measurement side effect.
    fields = (
        "st_mode",
        "st_ino",
        "st_dev",
        "st_nlink",
        "st_uid",
        "st_gid",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
        "st_birthtime_ns",
        "st_flags",
        "st_file_attributes",
    )
    return all(getattr(before, field, None) == getattr(after, field, None) for field in fields)


def check_interpreter_identity(pin, executable, version, license_file=None):
    required = ("python_version", "python_executable_sha256", "python_license_sha256")
    if any(not pin.get(key) for key in required):
        raise ValueError("Exact reviewed target Python input identity unresolved")
    if (
        version != pin["python_version"]
        or digest(executable) != pin["python_executable_sha256"]
    ):
        raise ValueError("Target interpreter input identity mismatch")
    if (
        license_file is not None
        and digest(license_file) != pin["python_license_sha256"]
    ):
        raise ValueError("Target interpreter license identity mismatch")


def normal_name(name):
    reserved = {"CON", "PRN", "AUX", "NUL"} | {
        f"{p}{n}" for p in ("COM", "LPT") for n in range(1, 10)
    }
    return (
        isinstance(name, str)
        and 0 < len(name) <= 1024
        and name.isascii()
        and all(
            p not in ("", ".", "..")
            and not p.endswith((".", " "))
            and p.split(".")[0].upper() not in reserved
            and all(ord(c) >= 32 and ord(c) != 127 and c not in '\\:*?"<>|' for c in p)
            for p in name.split("/")
        )
    )


def validate_inventory(entries):
    if not entries or len(entries) > MAX_ENTRIES:
        raise ValueError("Invalid bundle inventory")
    graph = {}
    aliases = set()
    total = 0
    for entry in entries:
        name = entry.get("path")
        if not normal_name(name) or name.lower() in aliases:
            raise ValueError("Bundle path alias")
        aliases.add(name.lower())
        graph[name] = entry
        kind = entry.get("kind")
        fields = {"path", "kind"} | (
            {"sha256", "size", "executable"}
            if kind == "file"
            else {"target"}
            if kind == "symlink"
            else set()
        )
        if kind not in ("file", "directory", "symlink") or set(entry) != fields:
            raise ValueError("Invalid bundle entry")
        if kind == "file":
            if (
                type(entry["size"]) is not int
                or entry["size"] < 0
                or type(entry["executable"]) is not bool
                or len(entry["sha256"]) != 64
                or any(c not in "0123456789abcdef" for c in entry["sha256"])
            ):
                raise ValueError("Invalid file identity")
            total += entry["size"]
            if total > MAX_BYTES:
                raise ValueError("Bundle size exceeded")
        if kind == "symlink":
            target = entry["target"]
            if (
                not isinstance(target, str)
                or not 0 < len(target) <= 1024
                or not target.isascii()
                or target.startswith("/")
                or any(ord(c) < 32 or ord(c) == 127 or c in "\\:" for c in target)
            ):
                raise ValueError("Invalid bundle link")
    for name in graph:
        for parent in PurePosixPath(name).parents:
            if str(parent) == ".":
                continue
            if graph.get(str(parent), {}).get("kind") != "directory":
                raise ValueError("Unlisted parent")
    for name, entry in graph.items():
        if entry["kind"] != "symlink":
            continue
        todo = deque(name.split("/"))
        done = []
        links = 0
        while todo:
            part = todo.popleft()
            if part in ("", "."):
                continue
            if part == "..":
                if not done:
                    raise ValueError("Escaping bundle link")
                done.pop()
                continue
            current = "/".join([*done, part])
            item = graph.get(current)
            if item is None:
                raise ValueError("Dangling bundle link")
            if item["kind"] == "symlink":
                links += 1
                if links > 64:
                    raise ValueError("Cyclic bundle link")
                todo.extendleft(reversed(item["target"].split("/")))
            else:
                if item["kind"] == "file" and todo:
                    raise ValueError("Link through regular file")
                done.append(part)
    return entries


def inventory_tree(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Invalid bundle root")
    entries = []
    pending = [root]
    while pending:
        for path in sorted(pending.pop().iterdir()):
            meta = path.lstat()
            name = path.relative_to(root).as_posix()
            if getattr(meta, "st_file_attributes", 0) & 0x400 and not path.is_symlink():
                raise ValueError("Reparse point rejected")
            if stat.S_ISLNK(meta.st_mode):
                entry = {"kind": "symlink", "path": name, "target": os.readlink(path)}
            elif stat.S_ISDIR(meta.st_mode):
                entry = {"kind": "directory", "path": name}
                pending.append(path)
            elif (
                stat.S_ISREG(meta.st_mode)
                and meta.st_nlink == 1
                and not meta.st_mode & 0o6000
            ):
                entry = {
                    "kind": "file",
                    "path": name,
                    "sha256": digest(path),
                    "size": meta.st_size,
                    "executable": bool(meta.st_mode & 0o111),
                }
                if not same_file_snapshot(meta, path.lstat()):
                    raise ValueError("Bundle changed during measurement")
            else:
                raise ValueError("Unsupported bundle object")
            entries.append(entry)
            if len(entries) > MAX_ENTRIES:
                raise ValueError("Bundle entry limit")
    return validate_inventory(sorted(entries, key=lambda e: e["path"]))


def copy_xattrs(source, output):
    if not hasattr(os, "listxattr"):
        return
    for name in os.listxattr(source, follow_symlinks=False):
        # ACLs/ownership are not portable build metadata and must not broaden
        # private staging permissions. Refuse rather than silently strip them.
        if name in {
            "system.posix_acl_access",
            "system.posix_acl_default",
            "com.apple.system.Security",
        }:
            raise ValueError("Source ACL metadata requires native packaging review")
        os.setxattr(
            output,
            name,
            os.getxattr(source, name, follow_symlinks=False),
            follow_symlinks=False,
        )


def stage_tree(source, output, entries):
    source, output = Path(source), Path(output)
    validate_inventory(entries)
    if inventory_tree(source) != entries:
        raise ValueError("Source identity changed")
    output.mkdir(mode=0o700)  # exclusive; no reuse or partial-install activation
    copy_xattrs(source, output)
    for item in entries:
        if item["kind"] == "directory":
            (output / item["path"]).mkdir(mode=0o700)
            copy_xattrs(source / item["path"], output / item["path"])
    for item in entries:
        if item["kind"] != "file":
            continue
        src = source / item["path"]
        dst = output / item["path"]
        fd = os.open(
            src,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        with os.fdopen(fd, "rb") as inp, open(dst, "xb") as out:
            meta = os.fstat(inp.fileno())
            if not stat.S_ISREG(meta.st_mode) or meta.st_size != item["size"]:
                raise ValueError("Source changed")
            remaining = item["size"]
            while remaining:
                block = inp.read(min(65536, remaining))
                if not block:
                    raise ValueError("Truncated source")
                out.write(block)
                remaining -= len(block)
            if inp.read(1):
                raise ValueError("Source grew")
        os.chmod(dst, 0o500 if item["executable"] else 0o400)
        # Native execution snapshot independently preserves quarantine/xattrs;
        # this build-stage copier must not be used as a quarantine bypass.
        copy_xattrs(src, dst)
    for item in entries:
        if item["kind"] == "symlink":
            (output / item["path"]).symlink_to(item["target"])
            copy_xattrs(source / item["path"], output / item["path"])
    if inventory_tree(output) != entries:
        raise ValueError("Staged bundle identity mismatch")
