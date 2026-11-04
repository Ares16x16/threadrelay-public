//! Host-owned private homes and temporary storage for native child processes.
//! Only the trusted host supplies `app_root`; runtime paths are never accepted
//! from renderer messages or caller-provided environment variables.
#[cfg(unix)]
use std::sync::Arc;
use std::{io, path::Path, process::Command};

#[derive(Clone, Copy)]
pub(crate) enum RuntimeKind {
    Browser,
    Inference,
    Worker,
}

/// Retains every directory identity used to construct the child environment.
/// Clones share descriptors; dropping a guard never deletes or changes paths.
#[derive(Clone)]
pub(crate) struct PrivateRuntime {
    #[cfg(unix)]
    inner: Arc<unix::Runtime>,
}

impl PrivateRuntime {
    pub(crate) fn prepare(app_root: &Path, kind: RuntimeKind) -> io::Result<Self> {
        #[cfg(unix)]
        {
            Ok(Self {
                inner: Arc::new(unix::Runtime::prepare(app_root, kind)?),
            })
        }
        #[cfg(not(unix))]
        {
            let _ = (app_root, kind);
            Err(io::Error::new(
                io::ErrorKind::Unsupported,
                "Private Unix runtime unavailable",
            ))
        }
    }

    pub(crate) fn browser_profile(&self) -> io::Result<&Path> {
        #[cfg(unix)]
        {
            let profile = self.inner.browser_profile.as_deref().ok_or_else(|| {
                io::Error::new(io::ErrorKind::InvalidInput, "Browser runtime required")
            })?;
            self.inner.recheck()?;
            Ok(profile)
        }
        #[cfg(not(unix))]
        {
            Err(io::Error::new(
                io::ErrorKind::Unsupported,
                "Private Unix runtime unavailable",
            ))
        }
    }

    /// Call after clearing inherited environment and immediately before spawn.
    /// Validation finishes before any command environment or cwd is changed.
    pub(crate) fn configure(&self, command: &mut Command) -> io::Result<()> {
        #[cfg(unix)]
        {
            self.inner.recheck()?;
            for (name, path) in &self.inner.environment {
                command.env(name, path);
            }
            // Foundation resolves NSHomeDirectory independently of HOME.
            // Keep the process-local native home on the same retained path.
            #[cfg(target_os = "macos")]
            command.env("CFFIXED_USER_HOME", &self.inner.environment[0].1);
            // Pinned Chromium f842f52 base/files/file_util_apple.mm explicitly
            // reads MAC_CHROMIUM_TMPDIR before NSTemporaryDirectory; TMPDIR
            // alone does not control the macOS singleton socket directory.
            #[cfg(target_os = "macos")]
            if self.inner.browser_profile.is_some() {
                command.env("MAC_CHROMIUM_TMPDIR", &self.inner.environment[1].1);
            }
            command.current_dir(&self.inner.environment[0].1);
            Ok(())
        }
        #[cfg(not(unix))]
        {
            let _ = command;
            Err(io::Error::new(
                io::ErrorKind::Unsupported,
                "Private Unix runtime unavailable",
            ))
        }
    }
}

#[cfg(unix)]
mod unix {
    use super::*;
    use std::{
        ffi::{CString, OsStr, OsString},
        fs::{File, Metadata},
        os::{
            fd::{AsRawFd, FromRawFd},
            unix::{ffi::OsStrExt, fs::MetadataExt},
        },
        path::{Component, PathBuf},
    };

    fn unavailable() -> io::Error {
        io::Error::new(
            io::ErrorKind::PermissionDenied,
            "Private native runtime storage unavailable",
        )
    }

    fn cvt(result: libc::c_int) -> io::Result<libc::c_int> {
        if result < 0 {
            Err(io::Error::last_os_error())
        } else {
            Ok(result)
        }
    }

    fn name(value: &OsStr) -> io::Result<CString> {
        CString::new(value.as_bytes()).map_err(|_| unavailable())
    }

    const DIRECTORY_FLAGS: libc::c_int =
        libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK;

    fn open_root() -> io::Result<File> {
        let fd = cvt(unsafe { libc::open(b"/\0".as_ptr().cast(), DIRECTORY_FLAGS) })?;
        // The successful open transfers ownership of this new descriptor.
        Ok(unsafe { File::from_raw_fd(fd) })
    }

    fn open_dir(parent: &File, child: &OsStr) -> io::Result<File> {
        let child = name(child)?;
        let fd = cvt(unsafe { libc::openat(parent.as_raw_fd(), child.as_ptr(), DIRECTORY_FLAGS) })?;
        Ok(unsafe { File::from_raw_fd(fd) })
    }

    #[cfg(target_os = "macos")]
    unsafe extern "C" {
        fn tr_private_fd(fd: libc::c_int) -> libc::c_int;
        fn tr_parent_fd(fd: libc::c_int) -> libc::c_int;
    }

    #[cfg(target_os = "macos")]
    fn check_acl(directory: &File, private: bool) -> io::Result<()> {
        // The SDK-bound helpers reject every private ACL and every ancestor
        // allow ACL, while permitting a standard ancestor deny-delete ACL.
        cvt(unsafe {
            if private {
                tr_private_fd(directory.as_raw_fd())
            } else {
                tr_parent_fd(directory.as_raw_fd())
            }
        })?;
        Ok(())
    }

    #[cfg(target_os = "linux")]
    fn check_acl(directory: &File, _: bool) -> io::Result<()> {
        // Check both access and inheritable ACLs without following any path.
        // A filesystem with no POSIX ACL support still enforces Unix modes.
        for attribute in [
            b"system.posix_acl_access\0".as_slice(),
            b"system.posix_acl_default\0".as_slice(),
        ] {
            let result = unsafe {
                libc::fgetxattr(
                    directory.as_raw_fd(),
                    attribute.as_ptr().cast(),
                    std::ptr::null_mut(),
                    0,
                )
            };
            if result >= 0 {
                return Err(unavailable());
            }
            let error = io::Error::last_os_error();
            if !matches!(
                error.raw_os_error(),
                Some(libc::ENODATA) | Some(libc::ENOTSUP)
            ) {
                return Err(error);
            }
        }
        Ok(())
    }

    #[cfg(not(any(target_os = "macos", target_os = "linux")))]
    fn check_acl(_: &File, _: bool) -> io::Result<()> {
        Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "Native runtime ACL checks unavailable",
        ))
    }

    fn check_owner_mode(metadata: &Metadata, private: bool) -> io::Result<()> {
        let uid = unsafe { libc::geteuid() };
        if uid == 0 || unsafe { libc::getuid() } != uid || !metadata.is_dir() {
            return Err(unavailable());
        }
        if private {
            if metadata.uid() != uid || metadata.mode() & 0o7777 != 0o700 {
                return Err(unavailable());
            }
        } else {
            let trusted_sticky_root = metadata.uid() == 0 && metadata.mode() & 0o1000 != 0;
            if (metadata.uid() != 0 && metadata.uid() != uid)
                || (metadata.mode() & 0o022 != 0 && !trusted_sticky_root)
            {
                return Err(unavailable());
            }
        }
        Ok(())
    }

    fn check_directory(directory: &File, private: bool) -> io::Result<Metadata> {
        check_owner_mode(&directory.metadata()?, private)?;
        check_acl(directory, private)?;
        let metadata = directory.metadata()?;
        check_owner_mode(&metadata, private)?;
        Ok(metadata)
    }

    struct Directory {
        handle: File,
        parent: Option<usize>,
        name: OsString,
        dev: u64,
        ino: u64,
        private: bool,
    }

    fn retain(
        directories: &mut Vec<Directory>,
        handle: File,
        parent: Option<usize>,
        name: OsString,
        private: bool,
    ) -> io::Result<usize> {
        let metadata = check_directory(&handle, private)?;
        let index = directories.len();
        directories.push(Directory {
            handle,
            parent,
            name,
            dev: metadata.dev(),
            ino: metadata.ino(),
            private,
        });
        Ok(index)
    }

    fn private_child(
        directories: &mut Vec<Directory>,
        parent: usize,
        child: &'static str,
    ) -> io::Result<usize> {
        let encoded = name(OsStr::new(child))?;
        let parent_handle = &directories[parent].handle;
        let result = unsafe { libc::mkdirat(parent_handle.as_raw_fd(), encoded.as_ptr(), 0o700) };
        if result < 0 {
            let error = io::Error::last_os_error();
            if error.raw_os_error() != Some(libc::EEXIST) {
                return Err(error);
            }
        }
        // Existing objects are checked exactly as found. No chmod, ACL edits,
        // removal, symlink following, or retry with weaker flags is permitted.
        let handle = open_dir(parent_handle, OsStr::new(child))?;
        retain(directories, handle, Some(parent), child.into(), true)
    }

    pub(super) struct Runtime {
        directories: Vec<Directory>,
        pub(super) environment: [(&'static str, PathBuf); 5],
        pub(super) browser_profile: Option<PathBuf>,
    }

    impl Runtime {
        pub(super) fn prepare(app_root: &Path, kind: RuntimeKind) -> io::Result<Self> {
            if !app_root.is_absolute() {
                return Err(unavailable());
            }
            let mut components = Vec::new();
            for part in app_root.components() {
                match part {
                    Component::RootDir => (),
                    Component::Normal(part) => components.push(part),
                    _ => return Err(unavailable()),
                }
            }
            if components.is_empty() {
                return Err(unavailable());
            }
            let mut directories = Vec::new();
            let mut parent = retain(&mut directories, open_root()?, None, OsString::new(), false)?;
            let mut checked_root = PathBuf::from("/");
            for (index, part) in components.iter().enumerate() {
                let handle = open_dir(&directories[parent].handle, part)?;
                parent = retain(
                    &mut directories,
                    handle,
                    Some(parent),
                    (*part).to_owned(),
                    index + 1 == components.len(),
                )?;
                checked_root.push(part);
            }
            let native = private_child(&mut directories, parent, "native-runtime")?;
            let kind_name = match kind {
                RuntimeKind::Browser => "browser",
                RuntimeKind::Inference => "inference",
                RuntimeKind::Worker => "worker",
            };
            let runtime = private_child(&mut directories, native, kind_name)?;
            let runtime_path = checked_root.join("native-runtime").join(kind_name);
            let environment = [
                ("HOME", "home"),
                ("TMPDIR", "tmp"),
                ("XDG_CACHE_HOME", "cache"),
                ("XDG_CONFIG_HOME", "config"),
                ("XDG_DATA_HOME", "data"),
            ]
            .map(|(variable, child)| (variable, runtime_path.join(child)));
            for child in ["home", "tmp", "cache", "config", "data"] {
                private_child(&mut directories, runtime, child)?;
            }
            let browser_profile = if matches!(kind, RuntimeKind::Browser) {
                // Preserve the collector's fixed profile contract. The host
                // validates existing directories without migrating or editing.
                let profiles = private_child(&mut directories, parent, "browser-profiles")?;
                private_child(&mut directories, profiles, "threads")?;
                Some(checked_root.join("browser-profiles/threads"))
            } else {
                None
            };
            let result = Self {
                directories,
                environment,
                browser_profile,
            };
            result.recheck()?;
            Ok(result)
        }

        pub(super) fn recheck(&self) -> io::Result<()> {
            let mut current = Vec::<File>::with_capacity(self.directories.len());
            for directory in &self.directories {
                // Retained handles cover privacy changes even after a rename;
                // reopening from the fixed root also detects replaced links.
                let retained = check_directory(&directory.handle, directory.private)?;
                if retained.dev() != directory.dev || retained.ino() != directory.ino {
                    return Err(unavailable());
                }
                let handle = match directory.parent {
                    Some(parent) => open_dir(&current[parent], &directory.name)?,
                    None => open_root()?,
                };
                let metadata = check_directory(&handle, directory.private)?;
                if metadata.dev() != directory.dev || metadata.ino() != directory.ino {
                    return Err(unavailable());
                }
                current.push(handle);
            }
            Ok(())
        }
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::{
        collections::BTreeMap,
        ffi::{CString, OsString},
        fs::{self, DirBuilder},
        os::unix::{
            ffi::OsStrExt,
            fs::{symlink, DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt},
        },
        path::PathBuf,
        sync::atomic::{AtomicU64, Ordering},
    };

    struct TestRoot(PathBuf);
    impl TestRoot {
        fn new() -> Self {
            static NEXT: AtomicU64 = AtomicU64::new(0);
            let project = Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("../../..")
                .canonicalize()
                .unwrap();
            let cache = project.join(".cache");
            match DirBuilder::new().mode(0o700).create(&cache) {
                Ok(()) => (),
                Err(error) if error.kind() == io::ErrorKind::AlreadyExists => (),
                Err(error) => panic!("Cannot create project test cache: {error}"),
            }
            // An existing cache may be 0755, but must never be followed through
            // a link or changed. Create the fixture through its checked fd.
            let cache_directory = fs::OpenOptions::new()
                .read(true)
                .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
                .open(&cache)
                .unwrap();
            let metadata = cache_directory.metadata().unwrap();
            assert_eq!(metadata.uid(), unsafe { libc::geteuid() });
            assert_eq!(metadata.mode() & 0o022, 0);
            let basename = format!(
                "runtime-environment-test-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed),
            );
            let encoded = CString::new(basename.as_bytes()).unwrap();
            use std::os::fd::AsRawFd;
            assert_eq!(
                unsafe { libc::mkdirat(cache_directory.as_raw_fd(), encoded.as_ptr(), 0o700) },
                0
            );
            let base = cache.join(basename);
            let fixture = Self(base);
            fixture.create("container/app");
            fixture
        }
        fn app(&self) -> PathBuf {
            self.0.join("container/app")
        }
        fn create(&self, name: &str) -> PathBuf {
            let mut path = self.0.clone();
            for part in name.split('/') {
                path.push(part);
                if !path.exists() {
                    DirBuilder::new().mode(0o700).create(&path).unwrap();
                }
            }
            path
        }
        fn private_parent(&self, relative: &str) -> PathBuf {
            let path = self.app().join(relative);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            let mut parent = path.parent().unwrap();
            while parent.starts_with(self.app()) {
                fs::set_permissions(parent, fs::Permissions::from_mode(0o700)).unwrap();
                if parent == self.app() {
                    break;
                }
                parent = parent.parent().unwrap();
            }
            path
        }
    }
    impl Drop for TestRoot {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    const DIRECTORIES: [&str; 7] = [
        "native-runtime",
        "native-runtime/worker",
        "native-runtime/worker/home",
        "native-runtime/worker/tmp",
        "native-runtime/worker/cache",
        "native-runtime/worker/config",
        "native-runtime/worker/data",
    ];

    fn configured(command: &Command) -> BTreeMap<OsString, Option<OsString>> {
        command
            .get_envs()
            .map(|(k, v)| (k.to_owned(), v.map(ToOwned::to_owned)))
            .collect()
    }
    fn rejected_without_environment_changes(runtime: &PrivateRuntime) {
        let mut command = Command::new("unused-native-child");
        command
            .env_clear()
            .env("HOME", "untrusted")
            .env("KEEP", "preserved")
            .current_dir("untrusted-cwd");
        let before = configured(&command);
        let cwd = command.get_current_dir().map(ToOwned::to_owned);
        assert!(runtime.configure(&mut command).is_err());
        assert_eq!(configured(&command), before);
        assert_eq!(command.get_current_dir(), cwd.as_deref());
    }

    #[test]
    fn creates_private_runtime_paths_and_sets_only_fixed_environment() {
        let fixture = TestRoot::new();
        for (kind, name) in [
            (RuntimeKind::Browser, "browser"),
            (RuntimeKind::Inference, "inference"),
            (RuntimeKind::Worker, "worker"),
        ] {
            let runtime = PrivateRuntime::prepare(&fixture.app(), kind).unwrap();
            let mut command = Command::new("unused-native-child");
            command.env_clear().env("KEEP", "preserved");
            for variable in [
                "HOME",
                "TMPDIR",
                "XDG_CACHE_HOME",
                "XDG_CONFIG_HOME",
                "XDG_DATA_HOME",
            ] {
                command.env(variable, "/untrusted/caller/path");
            }
            runtime.configure(&mut command).unwrap();
            let root = fixture.app().join("native-runtime").join(name);
            assert_eq!(command.get_current_dir(), Some(root.join("home").as_path()));
            let actual = configured(&command);
            let browser_native_override =
                cfg!(target_os = "macos") && matches!(kind, RuntimeKind::Browser);
            assert_eq!(
                actual.len(),
                6 + usize::from(cfg!(target_os = "macos")) + usize::from(browser_native_override)
            );
            assert_eq!(
                actual.get(&OsString::from("KEEP")),
                Some(&Some(OsString::from("preserved")))
            );
            for (variable, directory) in [
                ("HOME", "home"),
                ("TMPDIR", "tmp"),
                ("XDG_CACHE_HOME", "cache"),
                ("XDG_CONFIG_HOME", "config"),
                ("XDG_DATA_HOME", "data"),
            ] {
                let path = root.join(directory);
                assert_eq!(
                    actual.get(&OsString::from(variable)),
                    Some(&Some(path.clone().into_os_string()))
                );
                let metadata = fs::symlink_metadata(&path).unwrap();
                assert!(metadata.is_dir());
                assert_eq!(metadata.mode() & 0o7777, 0o700);
                assert_eq!(metadata.uid(), unsafe { libc::geteuid() });
                assert_eq!(fs::read_dir(path).unwrap().count(), 0);
            }
        }
    }

    #[test]
    #[cfg(target_os = "macos")]
    fn native_foundation_home_uses_retained_private_home() {
        let fixture = TestRoot::new();
        for (kind, name) in [
            (RuntimeKind::Browser, "browser"),
            (RuntimeKind::Inference, "inference"),
            (RuntimeKind::Worker, "worker"),
        ] {
            let runtime = PrivateRuntime::prepare(&fixture.app(), kind).unwrap();
            let mut command = Command::new("unused-native-child");
            command
                .env_clear()
                .env("CFFIXED_USER_HOME", "/untrusted/caller/path");
            runtime.configure(&mut command).unwrap();
            assert_eq!(
                configured(&command).get(&OsString::from("CFFIXED_USER_HOME")),
                Some(&Some(
                    fixture
                        .app()
                        .join("native-runtime")
                        .join(name)
                        .join("home")
                        .into_os_string()
                ))
            );
        }
    }

    #[test]
    #[cfg(target_os = "macos")]
    fn browser_uses_chromium_native_temporary_directory_override() {
        let fixture = TestRoot::new();
        let runtime = PrivateRuntime::prepare(&fixture.app(), RuntimeKind::Browser).unwrap();
        let mut command = Command::new("unused-native-child");
        command
            .env_clear()
            .env("MAC_CHROMIUM_TMPDIR", "/untrusted/caller/path");
        runtime.configure(&mut command).unwrap();
        assert_eq!(
            configured(&command).get(&OsString::from("MAC_CHROMIUM_TMPDIR")),
            Some(&Some(
                fixture
                    .app()
                    .join("native-runtime/browser/tmp")
                    .into_os_string()
            ))
        );
    }

    #[test]
    fn browser_profile_is_fixed_private_and_unavailable_to_other_runtimes() {
        let fixture = TestRoot::new();
        let browser = PrivateRuntime::prepare(&fixture.app(), RuntimeKind::Browser).unwrap();
        let expected = fixture.app().join("browser-profiles/threads");
        assert_eq!(browser.browser_profile().unwrap(), expected);
        assert_eq!(fs::metadata(&expected).unwrap().mode() & 0o7777, 0o700);
        for kind in [RuntimeKind::Inference, RuntimeKind::Worker] {
            assert!(PrivateRuntime::prepare(&fixture.app(), kind)
                .unwrap()
                .browser_profile()
                .is_err());
        }
        fs::rename(&expected, expected.with_file_name("retained-profile")).unwrap();
        DirBuilder::new().mode(0o700).create(&expected).unwrap();
        assert!(browser.browser_profile().is_err());
        rejected_without_environment_changes(&browser);
    }

    #[test]
    fn rejects_unexpected_existing_browser_profile_directories() {
        use std::os::unix::fs::FileTypeExt;
        for directory in ["browser-profiles", "browser-profiles/threads"] {
            let fixture = TestRoot::new();
            let path = fixture.private_parent(directory);
            DirBuilder::new().mode(0o755).create(&path).unwrap();
            fs::write(path.join("sentinel"), b"preserved").unwrap();
            assert!(PrivateRuntime::prepare(&fixture.app(), RuntimeKind::Browser).is_err());
            assert_eq!(fs::metadata(&path).unwrap().mode() & 0o7777, 0o755);
            assert_eq!(fs::read(path.join("sentinel")).unwrap(), b"preserved");

            let fixture = TestRoot::new();
            let path = fixture.private_parent(directory);
            let target = fixture.create("outside");
            symlink(&target, &path).unwrap();
            assert!(PrivateRuntime::prepare(&fixture.app(), RuntimeKind::Browser).is_err());
            assert!(fs::symlink_metadata(path).unwrap().file_type().is_symlink());
            assert_eq!(fs::read_dir(target).unwrap().count(), 0);

            let fixture = TestRoot::new();
            let path = fixture.private_parent(directory);
            let cpath = CString::new(path.as_os_str().as_bytes()).unwrap();
            assert_eq!(unsafe { libc::mkfifo(cpath.as_ptr(), 0o600) }, 0);
            assert!(PrivateRuntime::prepare(&fixture.app(), RuntimeKind::Browser).is_err());
            assert!(fs::symlink_metadata(path).unwrap().file_type().is_fifo());
        }
    }

    #[test]
    fn reuses_private_directories_and_clones_retain_validation() {
        let fixture = TestRoot::new();
        let runtime = PrivateRuntime::prepare(&fixture.app(), RuntimeKind::Worker).unwrap();
        let sentinel = fixture.app().join("native-runtime/worker/home/sentinel");
        fs::write(&sentinel, b"preserved").unwrap();
        let clone = runtime.clone();
        drop(runtime);
        clone
            .configure(&mut Command::new("unused-native-child"))
            .unwrap();
        PrivateRuntime::prepare(&fixture.app(), RuntimeKind::Worker).unwrap();
        assert_eq!(fs::read(sentinel).unwrap(), b"preserved");
    }

    #[test]
    fn rejects_nonprivate_app_root_without_changing_it() {
        for mode in [0o755, 0o770, 0o1700, 0o2700] {
            let fixture = TestRoot::new();
            fs::set_permissions(fixture.app(), fs::Permissions::from_mode(mode)).unwrap();
            fs::write(fixture.app().join("sentinel"), b"preserved").unwrap();
            assert!(PrivateRuntime::prepare(&fixture.app(), RuntimeKind::Worker).is_err());
            assert_eq!(fs::metadata(fixture.app()).unwrap().mode() & 0o7777, mode);
            assert_eq!(
                fs::read(fixture.app().join("sentinel")).unwrap(),
                b"preserved"
            );
            assert!(!fixture.app().join("native-runtime").exists());
        }
    }

    #[test]
    fn rejects_nonprivate_existing_runtime_directories_without_changing_them() {
        for directory in DIRECTORIES {
            let fixture = TestRoot::new();
            let path = fixture.private_parent(directory);
            DirBuilder::new().mode(0o755).create(&path).unwrap();
            fs::write(path.join("sentinel"), b"preserved").unwrap();
            assert!(
                PrivateRuntime::prepare(&fixture.app(), RuntimeKind::Worker).is_err(),
                "{directory}"
            );
            assert_eq!(fs::metadata(&path).unwrap().mode() & 0o7777, 0o755);
            assert_eq!(fs::read(path.join("sentinel")).unwrap(), b"preserved");
        }
    }

    #[test]
    fn rejects_symlinks_at_app_root_and_every_runtime_directory() {
        let fixture = TestRoot::new();
        let target = fixture.create("outside");
        let link = fixture.0.join("linked-app");
        symlink(&target, &link).unwrap();
        assert!(PrivateRuntime::prepare(&link, RuntimeKind::Worker).is_err());
        for directory in DIRECTORIES {
            let fixture = TestRoot::new();
            let target = fixture.create("outside");
            fs::write(target.join("sentinel"), b"preserved").unwrap();
            let link = fixture.private_parent(directory);
            symlink(&target, &link).unwrap();
            assert!(
                PrivateRuntime::prepare(&fixture.app(), RuntimeKind::Worker).is_err(),
                "{directory}"
            );
            assert!(fs::symlink_metadata(&link)
                .unwrap()
                .file_type()
                .is_symlink());
            assert_eq!(fs::read(target.join("sentinel")).unwrap(), b"preserved");
            assert_eq!(fs::read_dir(target).unwrap().count(), 1);
        }
    }

    #[test]
    fn rejects_fifos_at_every_runtime_directory_without_opening_them_for_io() {
        use std::os::unix::fs::FileTypeExt;
        for directory in DIRECTORIES {
            let fixture = TestRoot::new();
            let path = fixture.private_parent(directory);
            let cpath = CString::new(path.as_os_str().as_bytes()).unwrap();
            assert_eq!(unsafe { libc::mkfifo(cpath.as_ptr(), 0o600) }, 0);
            assert!(
                PrivateRuntime::prepare(&fixture.app(), RuntimeKind::Worker).is_err(),
                "{directory}"
            );
            assert!(fs::symlink_metadata(&path).unwrap().file_type().is_fifo());
        }
    }

    #[test]
    fn rejects_unsafe_ancestors_including_user_owned_sticky_directories() {
        for mode in [0o770, 0o777, 0o1777] {
            let fixture = TestRoot::new();
            let parent = fixture.0.join("container");
            fs::set_permissions(&parent, fs::Permissions::from_mode(mode)).unwrap();
            assert!(PrivateRuntime::prepare(&fixture.app(), RuntimeKind::Worker).is_err());
            assert_eq!(fs::metadata(parent).unwrap().mode() & 0o7777, mode);
            assert!(!fixture.app().join("native-runtime").exists());
        }
    }

    #[test]
    fn rejects_relative_and_parent_traversal_roots() {
        let fixture = TestRoot::new();
        for path in [
            PathBuf::from("relative/app"),
            fixture.app().join("../app"),
            PathBuf::from("/"),
        ] {
            assert!(PrivateRuntime::prepare(&path, RuntimeKind::Worker).is_err());
        }
        assert!(!fixture.app().join("native-runtime").exists());
    }

    #[test]
    fn configure_rejects_replaced_runtime_directory_without_mutating_command() {
        for directory in DIRECTORIES {
            let fixture = TestRoot::new();
            let runtime = PrivateRuntime::prepare(&fixture.app(), RuntimeKind::Worker).unwrap();
            let original = fixture.app().join(directory);
            let saved = original.with_file_name("retained-original");
            fs::rename(&original, saved).unwrap();
            DirBuilder::new().mode(0o700).create(&original).unwrap();
            rejected_without_environment_changes(&runtime);
        }
    }

    #[test]
    fn configure_rejects_replaced_app_root_and_ancestor() {
        for relative in ["container/app", "container"] {
            let fixture = TestRoot::new();
            let runtime = PrivateRuntime::prepare(&fixture.app(), RuntimeKind::Worker).unwrap();
            let original = fixture.0.join(relative);
            fs::rename(&original, original.with_file_name("retained-original")).unwrap();
            DirBuilder::new().mode(0o700).create(&original).unwrap();
            rejected_without_environment_changes(&runtime);
        }
    }

    #[test]
    fn configure_rejects_permissions_changed_after_preparation() {
        for directory in DIRECTORIES.into_iter().chain(["", ".."]) {
            let fixture = TestRoot::new();
            let runtime = PrivateRuntime::prepare(&fixture.app(), RuntimeKind::Worker).unwrap();
            let path = fixture.app().join(directory);
            fs::set_permissions(path, fs::Permissions::from_mode(0o770)).unwrap();
            rejected_without_environment_changes(&runtime);
        }
    }

    #[cfg(target_os = "macos")]
    fn add_acl(path: &Path, entry: &str) {
        assert!(Command::new("/bin/chmod")
            .args(["+a", entry])
            .arg(path)
            .status()
            .unwrap()
            .success());
    }
    #[cfg(target_os = "macos")]
    fn acl_listing(path: &Path) -> Vec<u8> {
        let output = Command::new("/bin/ls")
            .arg("-lde")
            .arg(path)
            .output()
            .unwrap();
        assert!(output.status.success());
        output.stdout
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn rejects_existing_private_directory_acls_without_modifying_them() {
        for (directory, kind) in DIRECTORIES
            .into_iter()
            .chain([""])
            .map(|d| (d, RuntimeKind::Worker))
            .chain([
                ("browser-profiles", RuntimeKind::Browser),
                ("browser-profiles/threads", RuntimeKind::Browser),
            ])
        {
            let fixture = TestRoot::new();
            let path = if directory.is_empty() {
                fixture.app()
            } else {
                let path = fixture.private_parent(directory);
                DirBuilder::new().mode(0o700).create(&path).unwrap();
                path
            };
            fs::write(path.join("sentinel"), b"preserved").unwrap();
            add_acl(&path, "everyone allow list,search");
            let before = acl_listing(&path);
            assert!(
                PrivateRuntime::prepare(&fixture.app(), kind).is_err(),
                "{directory}"
            );
            assert_eq!(acl_listing(&path), before);
            assert_eq!(fs::read(path.join("sentinel")).unwrap(), b"preserved");
        }
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn ancestor_deny_acl_is_allowed_and_allow_acl_is_rejected() {
        let fixture = TestRoot::new();
        let ancestor = fixture.0.join("container");
        add_acl(&ancestor, "everyone deny delete");
        let runtime = PrivateRuntime::prepare(&fixture.app(), RuntimeKind::Worker).unwrap();
        runtime
            .configure(&mut Command::new("unused-native-child"))
            .unwrap();
        add_acl(&ancestor, "everyone allow list,search");
        let before = acl_listing(&ancestor);
        assert!(PrivateRuntime::prepare(&fixture.app(), RuntimeKind::Worker).is_err());
        rejected_without_environment_changes(&runtime);
        assert_eq!(acl_listing(&ancestor), before);
        // Undo only the deny entry created by this test, after demonstrating
        // that production validation preserved it. Permit owned fixture cleanup.
        assert!(Command::new("/bin/chmod")
            .args(["-a", "everyone deny delete"])
            .arg(&ancestor)
            .status()
            .unwrap()
            .success());
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn configure_rechecks_private_directory_acls() {
        let fixture = TestRoot::new();
        let runtime = PrivateRuntime::prepare(&fixture.app(), RuntimeKind::Worker).unwrap();
        let temporary = fixture.app().join("native-runtime/worker/tmp");
        add_acl(&temporary, "everyone allow list,search");
        let before = acl_listing(&temporary);
        rejected_without_environment_changes(&runtime);
        assert_eq!(acl_listing(&temporary), before);
    }
}
