//! Project-local state and resource roots used by debug desktop builds only.
use std::{
    io,
    path::{Path, PathBuf},
};

fn project_root() -> io::Result<PathBuf> {
    let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let root = manifest
        .ancestors()
        .nth(3)
        .ok_or_else(|| io::Error::other("Project root unavailable"))?;
    if !root.is_absolute() {
        return Err(io::Error::other("Project root must be absolute"));
    }
    Ok(root.to_owned())
}

fn ensure_dir_at(project: &Path, name: &str) -> io::Result<PathBuf> {
    if !project.is_absolute() || !matches!(name, "app-data" | "resources") {
        return Err(io::Error::other("Debug storage path unavailable"));
    }
    let path = project.join(".cache/threadrelay-dev").join(name);
    #[cfg(unix)]
    unix::ensure_dir(project, name)?;
    #[cfg(not(unix))]
    // Windows ACL privacy still needs native verification. Keep its existing
    // development behavior; this helper does not grant native capabilities.
    std::fs::create_dir_all(&path)?;
    Ok(path)
}

#[cfg(unix)]
mod unix {
    use super::*;
    use std::{
        ffi::{CString, OsStr},
        fs::File,
        os::{
            fd::{AsRawFd, FromRawFd},
            unix::{ffi::OsStrExt, fs::MetadataExt},
        },
        path::Component,
    };

    fn unavailable() -> io::Error {
        io::Error::other("Private debug storage unavailable")
    }

    fn open_dir(parent: &File, name: &OsStr) -> io::Result<File> {
        let name = CString::new(name.as_bytes()).map_err(|_| unavailable())?;
        let fd = unsafe {
            libc::openat(
                parent.as_raw_fd(),
                name.as_ptr(),
                libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            )
        };
        if fd < 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(unsafe { File::from_raw_fd(fd) })
    }

    #[cfg(target_os = "macos")]
    fn check_acl(dir: &File) -> io::Result<()> {
        unsafe extern "C" {
            fn acl_get_fd_np(fd: libc::c_int, kind: libc::c_int) -> *mut libc::c_void;
            fn acl_get_entry(
                acl: *mut libc::c_void,
                selector: libc::c_int,
                entry: *mut *mut libc::c_void,
            ) -> libc::c_int;
            fn acl_free(value: *mut libc::c_void) -> libc::c_int;
        }
        // Darwin ACL_TYPE_EXTENDED and ACL_FIRST_ENTRY. An absent ACL is
        // ENOENT; an existing empty ACL exhausts entries with EINVAL.
        let acl = unsafe { acl_get_fd_np(dir.as_raw_fd(), 0x100) };
        if acl.is_null() {
            return if io::Error::last_os_error().raw_os_error() == Some(libc::ENOENT) {
                Ok(())
            } else {
                Err(unavailable())
            };
        }
        let mut entry = std::ptr::null_mut();
        let result = unsafe { acl_get_entry(acl, 0, &mut entry) };
        let error = io::Error::last_os_error();
        unsafe { acl_free(acl) };
        if result == -1 && error.raw_os_error() == Some(libc::EINVAL) {
            Ok(())
        } else {
            // Reject every extended entry; never edit an unexpected ACL.
            Err(unavailable())
        }
    }

    #[cfg(not(target_os = "macos"))]
    fn check_acl(_: &File) -> io::Result<()> {
        Ok(())
    }

    fn check_directory(dir: &File, private: bool) -> io::Result<()> {
        let metadata = dir.metadata()?;
        let uid = unsafe { libc::geteuid() };
        if !metadata.is_dir()
            || metadata.uid() != uid
            || unsafe { libc::getuid() } != uid
            || metadata.mode() & 0o022 != 0
        {
            return Err(unavailable());
        }
        check_acl(dir)?;
        if private {
            // Tighten the previous helper's 0755 roots through the already
            // opened descriptor, preserving data and never following a link.
            // Shared-writable or foreign-owned roots were rejected above.
            if metadata.mode() & 0o700 != 0o700 {
                return Err(unavailable());
            }
            if metadata.mode() & 0o7777 != 0o700
                && unsafe { libc::fchmod(dir.as_raw_fd(), 0o700) } != 0
            {
                return Err(io::Error::last_os_error());
            }
            let after = dir.metadata()?;
            if after.uid() != uid || after.mode() & 0o7777 != 0o700 {
                return Err(unavailable());
            }
            check_acl(dir)?;
        }
        Ok(())
    }

    pub(super) fn child(parent: &File, name: &str, private: bool) -> io::Result<File> {
        let name_c = CString::new(name).map_err(|_| unavailable())?;
        if unsafe { libc::mkdirat(parent.as_raw_fd(), name_c.as_ptr(), 0o700) } != 0 {
            let error = io::Error::last_os_error();
            if error.kind() != io::ErrorKind::AlreadyExists {
                return Err(error);
            }
        }
        let dir = open_dir(parent, OsStr::new(name))?;
        check_directory(&dir, private)?;
        Ok(dir)
    }

    pub(super) fn project_dir(project: &Path) -> io::Result<File> {
        if !project.is_absolute() {
            return Err(unavailable());
        }
        let mut dir = File::open("/")?;
        for component in project.components() {
            match component {
                Component::RootDir => (),
                Component::Normal(name) => dir = open_dir(&dir, name)?,
                _ => return Err(unavailable()),
            }
        }
        check_directory(&dir, false)?;
        Ok(dir)
    }

    pub(super) fn ensure_dir(project: &Path, name: &str) -> io::Result<()> {
        let dir = project_dir(project)?;
        let cache = child(&dir, ".cache", false)?;
        let private = child(&cache, "threadrelay-dev", true)?;
        child(&private, name, true)?;
        Ok(())
    }
}

pub fn app_data() -> io::Result<PathBuf> {
    ensure_dir_at(&project_root()?, "app-data")
}

pub fn resources() -> io::Result<PathBuf> {
    ensure_dir_at(&project_root()?, "resources")
}

#[cfg(test)]
mod tests {
    use super::*;

    struct TestProject(PathBuf);
    impl TestProject {
        fn new() -> Self {
            use std::sync::atomic::{AtomicUsize, Ordering};
            static NEXT: AtomicUsize = AtomicUsize::new(0);
            let root = project_root().unwrap();
            #[cfg(unix)]
            {
                let project = unix::project_dir(&root).unwrap();
                let cache = unix::child(&project, ".cache", false).unwrap();
                unix::child(&cache, "tmp", false).unwrap();
            }
            #[cfg(not(unix))]
            std::fs::create_dir_all(root.join(".cache/tmp")).unwrap();
            let path = root.join(".cache/tmp").join(format!(
                "dev-paths-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            std::fs::create_dir(&path).unwrap();
            Self(path)
        }
    }
    impl Drop for TestProject {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    #[cfg(unix)]
    #[test]
    fn debug_directories_are_private_on_creation_and_reopen() {
        use std::os::unix::fs::{MetadataExt, PermissionsExt};
        let project = TestProject::new();
        let data = ensure_dir_at(&project.0, "app-data").unwrap();
        let resources = ensure_dir_at(&project.0, "resources").unwrap();
        for path in [
            project.0.join(".cache/threadrelay-dev"),
            data.clone(),
            resources,
        ] {
            assert_eq!(std::fs::metadata(&path).unwrap().mode() & 0o777, 0o700);
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
        std::fs::write(data.join("preserved"), b"local state").unwrap();
        ensure_dir_at(&project.0, "app-data").unwrap();
        ensure_dir_at(&project.0, "resources").unwrap();
        assert_eq!(
            std::fs::read(data.join("preserved")).unwrap(),
            b"local state"
        );
        for suffix in [
            ".cache/threadrelay-dev",
            ".cache/threadrelay-dev/app-data",
            ".cache/threadrelay-dev/resources",
        ] {
            assert_eq!(
                std::fs::metadata(project.0.join(suffix)).unwrap().mode() & 0o777,
                0o700
            );
        }
    }

    #[cfg(unix)]
    #[test]
    fn debug_directories_reject_symlinks_at_each_component() {
        use std::os::unix::fs::symlink;
        for component in [
            ".cache",
            ".cache/threadrelay-dev",
            ".cache/threadrelay-dev/app-data",
            ".cache/threadrelay-dev/resources",
        ] {
            let project = TestProject::new();
            let outside = TestProject::new();
            std::fs::write(outside.0.join("sentinel"), b"untouched").unwrap();
            let link = project.0.join(component);
            std::fs::create_dir_all(link.parent().unwrap()).unwrap();
            symlink(&outside.0, &link).unwrap();
            let name = if component.ends_with("resources") {
                "resources"
            } else {
                "app-data"
            };
            assert!(
                ensure_dir_at(&project.0, name).is_err(),
                "followed {component}"
            );
            assert_eq!(
                std::fs::read(outside.0.join("sentinel")).unwrap(),
                b"untouched"
            );
            assert_eq!(
                std::fs::read_dir(&outside.0).unwrap().count(),
                1,
                "created entries outside project"
            );
        }
    }

    #[cfg(unix)]
    #[test]
    fn debug_directories_reject_symlinked_project_ancestor() {
        use std::os::unix::fs::symlink;
        let fixture = TestProject::new();
        let project = fixture.0.join("real");
        std::fs::create_dir(&project).unwrap();
        let link = fixture.0.join("linked");
        symlink(&project, &link).unwrap();
        assert!(ensure_dir_at(&link, "app-data").is_err());
        assert!(!project.join(".cache").exists());
    }

    #[cfg(unix)]
    #[test]
    fn debug_directories_reject_writable_cache_parent() {
        use std::os::unix::fs::PermissionsExt;
        let project = TestProject::new();
        std::fs::create_dir(project.0.join(".cache")).unwrap();
        std::fs::set_permissions(
            project.0.join(".cache"),
            std::fs::Permissions::from_mode(0o777),
        )
        .unwrap();
        assert!(ensure_dir_at(&project.0, "app-data").is_err());
        assert!(!project.0.join(".cache/threadrelay-dev").exists());
    }

    #[test]
    fn debug_directories_reject_non_directory_and_path_escape() {
        let project = TestProject::new();
        std::fs::write(project.0.join(".cache"), b"sentinel").unwrap();
        assert!(ensure_dir_at(&project.0, "app-data").is_err());
        assert_eq!(
            std::fs::read(project.0.join(".cache")).unwrap(),
            b"sentinel"
        );
        std::fs::remove_file(project.0.join(".cache")).unwrap();
        for name in ["../escaped", "/escaped", "", ".", "app-data/nested"] {
            assert!(
                ensure_dir_at(&project.0, name).is_err(),
                "accepted {name:?}"
            );
        }
        assert!(!project.0.join(".cache").exists());
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn debug_directories_reject_extended_acl_without_modifying_it() {
        let project = TestProject::new();
        let data = ensure_dir_at(&project.0, "app-data").unwrap();
        let status = std::process::Command::new("/bin/chmod")
            .args(["+a", "everyone allow list,search"])
            .arg(&data)
            .status()
            .unwrap();
        assert!(status.success());
        std::fs::write(data.join("sentinel"), b"preserved").unwrap();
        assert!(ensure_dir_at(&project.0, "app-data").is_err());
        assert_eq!(std::fs::read(data.join("sentinel")).unwrap(), b"preserved");
    }

    #[test]
    fn debug_storage_roots_are_absolute_and_checkout_local() {
        let project = project_root().unwrap();
        let app_data = app_data().unwrap();
        let resources = resources().unwrap();
        assert!(app_data.is_absolute());
        assert!(resources.is_absolute());
        assert!(app_data.starts_with(&project));
        assert!(resources.starts_with(&project));
        assert_eq!(app_data, project.join(".cache/threadrelay-dev/app-data"));
        assert_eq!(resources, project.join(".cache/threadrelay-dev/resources"));
    }
}
