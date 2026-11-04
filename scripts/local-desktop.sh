#!/bin/sh
# Local connected desktop; inert fixtures remain an explicit test mode.
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
project=$(CDPATH= cd -- "$script_dir/.." && pwd -P)
mode=${1:-run}
case "$mode" in run|build|run-built|check|run-inert|build-inert|run-built-inert|check-inert) ;; *) echo "Usage: $0 [run|build|run-built|check|run-inert|build-inert|run-built-inert|check-inert]" >&2; exit 2 ;; esac
[ "$#" -le 1 ] || { echo "Unexpected arguments" >&2; exit 2; }
cd "$project"
[ ! -L "$project/.cache" ] || { echo "Project cache must not be a symlink" >&2; exit 2; }
for name in cargo-home rustup-home cargo-target tmp npm-cache xdg-cache; do
    [ ! -L "$project/.cache/$name" ] || { echo "Project cache must not be a symlink" >&2; exit 2; }
    mkdir -p "$project/.cache/$name"
done
export CARGO_HOME="$project/.cache/cargo-home"
export RUSTUP_HOME="$project/.cache/rustup-home"
export CARGO_TARGET_DIR="$project/.cache/cargo-target"
export TMPDIR="$project/.cache/tmp"
export XDG_CACHE_HOME="$project/.cache/xdg-cache"
export npm_config_cache="$project/.cache/npm-cache"
export CARGO_NET_OFFLINE=true
case $(uname -s):$(uname -m) in
    Darwin:arm64) toolchain="$RUSTUP_HOME/toolchains/stable-aarch64-apple-darwin/bin" ;;
    *) echo "This prepared local workflow requires macOS Apple silicon" >&2; exit 2 ;;
esac
export PATH="$toolchain:$PATH"
export RUSTC="$toolchain/rustc"
export RUSTDOC="$toolchain/rustdoc"
python="$project/.cache/build-inputs/install/python-3.12.16/bin/python3.12"
[ -x "$python" ] || { echo "Prepare the project-local Python 3.12.16 environment first" >&2; exit 2; }
"$python" -I -S -B -c 'import sys; assert sys.version_info[:3] == (3,12,16); sys.path[:0]=sys.argv[1:]; import threadrelay.packaged_worker; from importlib.metadata import version; assert version("jsonschema") == "4.25.1"; print("Project-local Python 3.12.16 source worker ready; authentication starts only on explicit owner action.")' "$project/worker/src" "$project/.cache/venvs/worker-3.12.16/lib/python3.12/site-packages"
case "$mode" in check|check-inert) exit 0 ;; esac
# The local .app is a development container, not an installer or release bundle.
# Its launcher lock survives exec; the worker owner retains its separate data lock.
exec "$python" -I -S -B - "$project" "$mode" "$toolchain" <<'PY_LOCAL_APP'
import ctypes
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys
import tempfile

project, mode, toolchain = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
inert = mode.endswith("-inert")
if inert: mode = mode[:-6]
root_fd = os.open(project, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
libsystem = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
libsystem.acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
libsystem.acl_get_fd_np.restype = ctypes.c_void_p
libsystem.acl_get_entry.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
libsystem.acl_get_entry.restype = ctypes.c_int
libsystem.acl_free.argtypes = [ctypes.c_void_p]
libsystem.acl_free.restype = ctypes.c_int

def check_acl(fd):
    # Darwin ACL_TYPE_EXTENDED / ACL_FIRST_ENTRY, as used by dev_paths.rs.
    ctypes.set_errno(0)
    acl = libsystem.acl_get_fd_np(fd, 0x100)
    if not acl:
        if ctypes.get_errno() == errno.ENOENT:
            return
        raise SystemExit("Local app ACL inspection unavailable")
    try:
        entry = ctypes.c_void_p()
        ctypes.set_errno(0)
        result = libsystem.acl_get_entry(acl, 0, ctypes.byref(entry))
        error = ctypes.get_errno()
        if result != -1 or error != errno.EINVAL:
            raise SystemExit("Local app paths with extended ACLs are refused; ACLs were preserved")
    finally:
        libsystem.acl_free(acl)

root_meta = os.fstat(root_fd)
if root_meta.st_uid != os.geteuid() or root_meta.st_mode & 0o022:
    raise SystemExit("Project root requires ownership without shared writes")
check_acl(root_fd)

def directory(relative):
    fd = os.dup(root_fd)
    private = False
    for name in Path(relative).parts:
        if name in {"", ".", ".."}:
            raise SystemExit("Invalid local app path")
        try:
            os.mkdir(name, 0o700, dir_fd=fd)
        except FileExistsError:
            pass
        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        os.close(fd)
        fd = child
        meta = os.fstat(fd)
        if meta.st_uid != os.geteuid() or meta.st_mode & 0o022:
            raise SystemExit("Local app paths require owned directories without shared writes")
        check_acl(fd)
        private = private or name in {"local-desktop", "threadrelay-dev"}
        if private:
            os.fchmod(fd, 0o700)
            check_acl(fd)
    return fd

def regular(parent, name, flags=os.O_RDONLY):
    fd = os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600, dir_fd=parent)
    meta = os.fstat(fd)
    if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.geteuid() or meta.st_nlink != 1 or meta.st_mode & 0o022:
        raise SystemExit("Local app files must be owned regular files without links or shared writes")
    check_acl(fd)
    return fd

def lock(parent, name):
    fd = regular(parent, name, os.O_RDWR | os.O_CREAT)
    os.fchmod(fd, 0o600)
    check_acl(fd)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("Quit ThreadRelay before building or opening the local app") from None
    return fd

def digest(fd):
    os.lseek(fd, 0, os.SEEK_SET)
    value = hashlib.sha256()
    while block := os.read(fd, 1024 * 1024):
        value.update(block)
    return value.digest()

def identity_of(meta):
    return meta.st_dev, meta.st_ino, meta.st_size, meta.st_mtime_ns, meta.st_ctime_ns

def attributes(path):
    result = subprocess.run(["/usr/bin/xattr", "-lx", str(path)], check=True, capture_output=True)
    return result.stdout

def inactive(path):
    if path.exists():
        # Ordinary read descriptors (including this launcher's current_fd)
        # do not mean the Mach-O is executing. Observe executable mappings only.
        result = subprocess.run(["/usr/sbin/lsof", "-t", "-a", "-d", "txt", "--", str(path)], capture_output=True)
        if result.returncode not in (0, 1) or result.stderr.strip() or result.stdout.strip():
            raise SystemExit("Quit ThreadRelay before refreshing the local app executable")

local_fd = directory(".cache/local-desktop")
launcher_fd = lock(local_fd, "launcher.lock")
data_fd = directory(".cache/threadrelay-dev/app-data")
instance_fd = lock(data_fd, "instance.lock")
bundle = project / ".cache/local-desktop/ThreadRelay.app"
executable = bundle / "Contents/MacOS/threadrelay-desktop"
source = project / ".cache/cargo-target/debug/threadrelay-desktop"
inactive(executable)
inactive(source)
if mode != "run-built":
    pnpm = project / ".cache/pnpm-cli/node_modules/pnpm/bin/pnpm.cjs"
    if not pnpm.is_file():
        raise SystemExit("Prepare the pinned project-local pnpm 10.20.0 CLI first")
    if subprocess.check_output(["node", str(pnpm), "--version"], text=True).strip() != "10.20.0":
        raise SystemExit("Pinned pnpm 10.20.0 is required")
    subprocess.run(["node", str(pnpm), "--dir", str(project / "apps/desktop"), "build"], check=True)
    subprocess.run([str(toolchain / "cargo"), "build", "--offline", "--locked", "--manifest-path",
        str(project / "apps/desktop/src-tauri/Cargo.toml"), "--features",
        "desktop,custom-protocol,development-worker" + ("" if inert else ",connected-worker"), "--bin", "threadrelay-desktop"], check=True)
source_dir = directory(".cache/cargo-target/debug")
source_fd = regular(source_dir, "threadrelay-desktop")
identity = os.fstat(source_fd)
source_digest = digest(source_fd)
# Bind run-built to the explicit feature mode and exact executable bytes.
# An inert build or an unrelated direct Cargo build cannot silently become the
# normal connected launcher (or the reverse).
receipt_path = project / ".cache/local-desktop/build-mode.json"
expected_mode = "inert" if inert else "connected"
receipt = {"schema_version":1,"mode":expected_mode,"sha256":source_digest.hex()}
if mode == "run-built":
    try:
        receipt_fd = regular(local_fd,"build-mode.json")
        try: actual = json.loads(os.read(receipt_fd,1025))
        finally: os.close(receipt_fd)
    except (OSError, ValueError):
        raise SystemExit("Build this explicit local mode before using run-built") from None
    if actual != receipt:
        raise SystemExit("Desktop bytes or mode changed; build the requested local mode first")
else:
    try:
        previous = regular(local_fd,"build-mode.json")
    except FileNotFoundError:
        previous = None
    if previous is not None: os.close(previous)
    receipt_fd,receipt_name=tempfile.mkstemp(prefix=".build-mode-",dir=receipt_path.parent)
    try:
        with os.fdopen(receipt_fd,"wb") as output:
            output.write(json.dumps(receipt,sort_keys=True,separators=(",",":")).encode("ascii"))
            output.flush();os.fsync(output.fileno())
        os.replace(receipt_name,receipt_path)
    finally:
        if os.path.lexists(receipt_name):os.unlink(receipt_name)
# The compiled binary confirms feature mode without creating AppKit or a
# worker; the record additionally binds it to bytes produced by this launcher.
observed_mode=subprocess.run([str(source),"--print-local-mode"],check=True,capture_output=True,timeout=5)
if observed_mode.stdout != (expected_mode+"\n").encode("ascii") or observed_mode.stderr:
    raise SystemExit("Desktop compiled mode differs; build the requested local mode first")
source_attributes = attributes(source)
app_fd = directory(".cache/local-desktop/ThreadRelay.app")
contents_fd = directory(".cache/local-desktop/ThreadRelay.app/Contents")
macos_fd = directory(".cache/local-desktop/ThreadRelay.app/Contents/MacOS")
for fd, allowed in [(app_fd, {"Contents"}), (contents_fd, {"Info.plist", "MacOS"}),
                    (macos_fd, {"threadrelay-desktop"})]:
    if set(os.listdir(fd)) - allowed:
        raise SystemExit("Unexpected files in the local development app container")
try:
    current_fd = regular(macos_fd, "threadrelay-desktop")
except FileNotFoundError:
    current_fd = None
if current_fd is None or digest(current_fd) != source_digest or attributes(executable) != source_attributes:
    staged_fd, staged_name = tempfile.mkstemp(prefix=".threadrelay-desktop-", dir=executable.parent)
    os.close(staged_fd)
    try:
        # ditto explicitly retains xattrs and quarantine; no removal or signing bypass.
        subprocess.run(["/usr/bin/ditto", "--rsrc", "--extattr", "--qtn", "--acl", str(source), staged_name],
            check=True, env={"TMPDIR": str(project / ".cache/tmp")})
        copied_fd = regular(macos_fd, Path(staged_name).name, os.O_RDWR)
        try:
            if digest(copied_fd) != source_digest or attributes(staged_name) != source_attributes:
                raise SystemExit("Local app executable bytes or metadata did not copy exactly")
            os.fchmod(copied_fd, 0o700)
            os.fsync(copied_fd)
        finally:
            os.close(copied_fd)
        if identity_of(os.fstat(source_fd)) != identity_of(identity) or identity_of(os.stat(source, follow_symlinks=False)) != identity_of(identity):
            raise SystemExit("Desktop build changed during local app preparation; retry after the build finishes")
        inactive(executable)
        os.replace(staged_name, executable)
    finally:
        if os.path.lexists(staged_name):
            os.unlink(staged_name)
else:
    os.fchmod(current_fd, 0o700)
if current_fd is not None:
    os.close(current_fd)
info = plistlib.dumps({"CFBundleIdentifier":"threadrelay-desktop", "CFBundleName":"ThreadRelay",
    "CFBundleDisplayName":"ThreadRelay", "CFBundleExecutable":"threadrelay-desktop",
    "CFBundlePackageType":"APPL", "CFBundleShortVersionString":"0.1.0", "CFBundleVersion":"0.1.0",
    "LSMinimumSystemVersion":"14.0", "NSHighResolutionCapable":True, "NSPrincipalClass":"NSApplication"})
try:
    info_fd = regular(contents_fd, "Info.plist")
except FileNotFoundError:
    info_fd = None
if info_fd is None or os.read(info_fd, len(info) + 1) != info:
    if info_fd is not None and attributes(bundle / "Contents/Info.plist"):
        raise SystemExit("Existing app metadata must be preserved; refusing to replace Info.plist")
    staged_fd, staged_name = tempfile.mkstemp(prefix=".Info-", dir=bundle / "Contents")
    try:
        with os.fdopen(staged_fd, "wb") as output:
            output.write(info)
            output.flush()
            os.fsync(output.fileno())
        os.replace(staged_name, bundle / "Contents/Info.plist")
    finally:
        if os.path.lexists(staged_name):
            os.unlink(staged_name)
if info_fd is not None:
    os.close(info_fd)
# Refuse ACL drift after copying without removing or editing any ACL.
for fd in [root_fd, local_fd, data_fd, source_dir, app_fd, contents_fd, macos_fd, source_fd]:
    check_acl(fd)
for relative in [".cache/local-desktop/ThreadRelay.app/Contents/MacOS", ".cache/cargo-target/debug"]:
    os.close(directory(relative))
for parent, name in [(macos_fd, "threadrelay-desktop"), (contents_fd, "Info.plist")]:
    fd = regular(parent, name)
    os.close(fd)
if identity_of(os.fstat(source_fd)) != identity_of(identity) or identity_of(os.stat(source, follow_symlinks=False)) != identity_of(identity):
    raise SystemExit("Desktop build changed during local app preparation; retry after the build finishes")
print("Local development app ready: " + str(bundle), flush=True)
fcntl.flock(instance_fd, fcntl.LOCK_UN)
os.close(instance_fd)
if mode == "build":
    raise SystemExit(0)
# Keep the launcher lock on the executing desktop; a second script cannot refresh it.
os.set_inheritable(launcher_fd, True)
os.execv(executable, [str(executable)])
PY_LOCAL_APP
