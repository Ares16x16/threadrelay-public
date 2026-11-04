//! Supervisor-retained execution capability, never a caller-supplied proof.
use crate::bundle_inventory::{Entry, Inventory};
use std::{
    fs::File,
    io,
    path::{Path, PathBuf},
};

/// Drop intentionally does NOT unseal/delete: a timeout/pipe EOF is not proof
/// that every process using lazy-loaded code has terminated. Supervisor calls
/// cleanup only after confirmed whole-generation termination. Stale snapshots
/// are never reused or automatically unsealed on a later run.
pub struct VerifiedArtifactSet {
    root: PathBuf,
    #[cfg(unix)]
    records: Vec<unix::Record>,
    #[cfg(unix)]
    parent: File,
    #[cfg(unix)]
    directory: File,
    #[cfg(windows)]
    holds: Option<Vec<File>>,
}
impl VerifiedArtifactSet {
    #[cfg(windows)]
    pub(crate) fn retained_windows_files(root: PathBuf, holds: Vec<File>) -> Self {
        Self {
            root,
            holds: Some(holds),
        }
    }
    pub fn root(&self) -> &Path {
        &self.root
    }
    #[cfg(all(
        debug_assertions,
        target_os = "macos",
        target_arch = "aarch64",
        any(feature = "development-worker", test)
    ))]
    pub(crate) fn connected_artifacts_stable(
        &self,
        app_root: &Path,
        cancelled: &dyn Fn() -> bool,
    ) -> io::Result<()> {
        unix::connected_artifacts_stable(self, app_root, cancelled)
    }
    pub fn cleanup(&mut self) -> io::Result<()> {
        #[cfg(unix)]
        {
            unix::cleanup(self)
        }
        #[cfg(windows)]
        {
            self.holds.take();
            Ok(())
        }
        #[cfg(not(any(unix, windows)))]
        {
            Err(io::Error::other("Unsupported artifact guard"))
        }
    }
}

#[cfg(windows)]
impl Drop for VerifiedArtifactSet {
    fn drop(&mut self) {
        if let Some(holds) = self.holds.take() {
            std::mem::forget(holds);
        }
    }
}

#[cfg(unix)]
mod unix {
    use super::*;
    #[cfg(test)]
    use sha2::{Digest, Sha256};
    use std::{
        ffi::{CStr, CString},
        io::{Read, Write},
        os::{
            fd::{AsRawFd, FromRawFd},
            unix::fs::MetadataExt,
        },
    };
    use threadrelay_artifact_hash::Sha256 as StreamingSha256;
    fn error() -> io::Error {
        io::Error::other("Verified execution snapshot unavailable")
    }
    fn c(value: &str) -> io::Result<CString> {
        CString::new(value).map_err(|_| error())
    }
    fn cvt(value: i32) -> io::Result<i32> {
        if value < 0 {
            Err(io::Error::last_os_error())
        } else {
            Ok(value)
        }
    }
    fn openat(dir: &File, name: &str, flags: i32, mode: u32) -> io::Result<File> {
        let name = c(name)?;
        let fd = cvt(unsafe {
            libc::openat(
                dir.as_raw_fd(),
                name.as_ptr(),
                flags | libc::O_CLOEXEC | libc::O_NOFOLLOW | libc::O_NONBLOCK,
                mode,
            )
        })?;
        Ok(unsafe { File::from_raw_fd(fd) })
    }
    pub(super) fn root_dir(path: &Path) -> io::Result<File> {
        if !path.is_absolute() {
            return Err(error());
        }
        let mut dir = File::open("/")?;
        for part in path.components() {
            match part {
                std::path::Component::RootDir => (),
                std::path::Component::Normal(name) => {
                    dir = openat(
                        &dir,
                        name.to_str().ok_or_else(error)?,
                        libc::O_RDONLY | libc::O_DIRECTORY,
                        0,
                    )?;
                }
                _ => return Err(error()),
            }
        }
        Ok(dir)
    }
    fn descend(root: &File, name: &str) -> io::Result<(File, String)> {
        let mut parts = name.split('/').collect::<Vec<_>>();
        let leaf = parts.pop().ok_or_else(error)?.to_owned();
        let mut dir = root.try_clone()?;
        for part in parts {
            dir = openat(&dir, part, libc::O_RDONLY | libc::O_DIRECTORY, 0)?;
        }
        Ok((dir, leaf))
    }
    fn names(dir: &File) -> io::Result<Vec<String>> {
        let duplicate = openat(dir, ".", libc::O_RDONLY | libc::O_DIRECTORY, 0)?;
        use std::os::fd::IntoRawFd;
        let fd = duplicate.into_raw_fd();
        let raw = unsafe { libc::fdopendir(fd) };
        if raw.is_null() {
            unsafe { libc::close(fd) };
            return Err(error());
        }
        let mut result = Vec::new();
        // readdir returns borrowed names, copied before the next call.
        loop {
            #[cfg(target_os = "macos")]
            unsafe {
                *libc::__error() = 0;
            }
            #[cfg(target_os = "linux")]
            unsafe {
                *libc::__errno_location() = 0;
            }
            let item = unsafe { libc::readdir(raw) };
            if item.is_null() {
                let err = io::Error::last_os_error();
                unsafe {
                    libc::closedir(raw);
                }
                if err.raw_os_error().unwrap_or(0) != 0 {
                    return Err(err);
                }
                break;
            }
            let name = unsafe { CStr::from_ptr((*item).d_name.as_ptr()) }.to_bytes();
            if name == b"." || name == b".." {
                continue;
            }
            match std::str::from_utf8(name) {
                Ok(v) => result.push(v.to_owned()),
                Err(_) => {
                    unsafe { libc::closedir(raw) };
                    return Err(error());
                }
            }
            if result.len() > crate::bundle_inventory::MAX_ENTRIES {
                unsafe { libc::closedir(raw) };
                return Err(error());
            }
        }
        Ok(result)
    }
    pub(super) fn check_tree(
        root: &File,
        inventory: &Inventory,
        cancelled: &dyn Fn() -> bool,
    ) -> io::Result<()> {
        let mut pending = vec![String::new()];
        let mut seen = 0;
        while let Some(prefix) = pending.pop() {
            let dir = if prefix.is_empty() {
                root.try_clone()?
            } else {
                let (parent, leaf) = descend(root, &prefix)?;
                openat(&parent, &leaf, libc::O_RDONLY | libc::O_DIRECTORY, 0)?
            };
            for name in names(&dir)? {
                if cancelled() {
                    return Err(error());
                }
                let path = if prefix.is_empty() {
                    name.clone()
                } else {
                    format!("{prefix}/{name}")
                };
                match inventory.entries.get(&path).ok_or_else(error)? {
                    Entry::Directory { .. } => {
                        let _ = openat(&dir, &name, libc::O_RDONLY | libc::O_DIRECTORY, 0)?;
                        pending.push(path);
                    }
                    Entry::Symlink { target, .. } => {
                        let mut buffer = [0u8; 1025];
                        let name = c(&name)?;
                        let count = unsafe {
                            libc::readlinkat(
                                dir.as_raw_fd(),
                                name.as_ptr(),
                                buffer.as_mut_ptr().cast(),
                                buffer.len(),
                            )
                        };
                        if count < 0 || &buffer[..count as usize] != target.as_bytes() {
                            return Err(error());
                        }
                    }
                    Entry::File {
                        size,
                        sha256,
                        executable,
                        ..
                    } => {
                        let mut file = openat(&dir, &name, libc::O_RDONLY, 0)?;
                        let meta = file.metadata()?;
                        if !meta.is_file()
                            || meta.nlink() != 1
                            || meta.len() != *size
                            || (meta.mode() & 0o111 != 0) != *executable
                            || meta.mode() & 0o6000 != 0
                        {
                            return Err(error());
                        }
                        let mut digest = StreamingSha256::new();
                        let mut buf = [0; 65536];
                        let mut total = 0;
                        loop {
                            if cancelled() {
                                return Err(error());
                            }
                            let n = file.read(&mut buf)?;
                            if n == 0 {
                                break;
                            }
                            total += n as u64;
                            if total > *size {
                                return Err(error());
                            }
                            digest.update(&buf[..n]);
                        }
                        if total != *size || digest.finish_hex() != *sha256 {
                            return Err(error());
                        }
                    }
                }
                seen += 1;
            }
        }
        if seen != inventory.entries.len() {
            return Err(error());
        }
        Ok(())
    }
    #[cfg(target_os = "macos")]
    unsafe extern "C" {
        fn tr_private_fd(fd: i32) -> i32;
        fn tr_parent_fd(fd: i32) -> i32;
        fn tr_seal_fd(fd: i32, mode: u32) -> i32;
        fn tr_unseal_fd(fd: i32) -> i32;
        fn tr_clone_fd(source: i32, parent: i32, name: *const libc::c_char) -> i32;
        fn tr_copy_xattrs(source: i32, dest: i32) -> i32;
        fn tr_copy_quarantine(source: i32, dest: i32) -> i32;
    }
    fn private(file: &File) -> io::Result<()> {
        let meta = file.metadata()?;
        if unsafe { libc::geteuid() } == 0
            || unsafe { libc::getuid() } != unsafe { libc::geteuid() }
            || meta.uid() != unsafe { libc::geteuid() }
            || meta.mode() & 0o077 != 0
        {
            return Err(error());
        }
        #[cfg(target_os = "macos")]
        {
            cvt(unsafe { tr_private_fd(file.as_raw_fd()) })?;
        }
        Ok(())
    }
    fn protected_ancestors(path: &Path) -> io::Result<()> {
        for ancestor in path.ancestors() {
            let file = root_dir(ancestor)?;
            let meta = file.metadata()?;
            let trusted_sticky_root = meta.uid() == 0 && meta.mode() & 0o1000 != 0;
            if (meta.uid() != 0 && meta.uid() != unsafe { libc::geteuid() })
                || (meta.mode() & 0o022 != 0 && !trusted_sticky_root)
                || (ancestor == path && meta.uid() != unsafe { libc::geteuid() })
            {
                return Err(error());
            }
            #[cfg(target_os = "macos")]
            {
                cvt(unsafe { tr_parent_fd(file.as_raw_fd()) })?;
            }
        }
        Ok(())
    }
    pub(super) struct Record {
        // Empty is the generation root; others are exact manifest paths.
        name: String,
        dev: u64,
        ino: u64,
        directory: bool,
        symlink: bool,
        #[cfg(all(debug_assertions, target_os = "macos"))]
        sealed: Option<SealStamp>,
    }
    #[cfg(all(debug_assertions, target_os = "macos"))]
    #[derive(PartialEq, Eq)]
    struct SealStamp {
        length: u64,
        mode: u32,
        uid: u32,
        links: u64,
        modified: (i64, i64),
        changed: (i64, i64),
        flags: u32,
    }
    #[cfg(all(debug_assertions, target_os = "macos"))]
    fn seal_stamp(file: &File) -> io::Result<SealStamp> {
        let mut stat = std::mem::MaybeUninit::<libc::stat>::uninit();
        cvt(unsafe { libc::fstat(file.as_raw_fd(), stat.as_mut_ptr()) })?;
        Ok(stat_stamp(&unsafe { stat.assume_init() }))
    }
    #[cfg(all(debug_assertions, target_os = "macos"))]
    fn stat_stamp(stat: &libc::stat) -> SealStamp {
        SealStamp {
            length: stat.st_size as u64,
            mode: stat.st_mode as u32,
            uid: stat.st_uid,
            links: stat.st_nlink as u64,
            modified: (stat.st_mtime, stat.st_mtime_nsec),
            changed: (stat.st_ctime, stat.st_ctime_nsec),
            flags: stat.st_flags,
        }
    }
    fn record(
        parent: &File,
        leaf: &str,
        relative: &str,
        directory: bool,
        symlink: bool,
    ) -> io::Result<Record> {
        let mut stat = std::mem::MaybeUninit::<libc::stat>::uninit();
        cvt(unsafe {
            libc::fstatat(
                parent.as_raw_fd(),
                c(leaf)?.as_ptr(),
                stat.as_mut_ptr(),
                libc::AT_SYMLINK_NOFOLLOW,
            )
        })?;
        let stat = unsafe { stat.assume_init() };
        Ok(Record {
            name: relative.into(),
            dev: stat.st_dev as u64,
            ino: stat.st_ino as u64,
            directory,
            symlink,
            #[cfg(all(debug_assertions, target_os = "macos"))]
            sealed: symlink.then(|| stat_stamp(&stat)),
        })
    }
    fn location(guard: &VerifiedArtifactSet, item: &Record) -> io::Result<(File, String)> {
        if item.name.is_empty() {
            Ok((
                guard.parent.try_clone()?,
                guard
                    .root
                    .file_name()
                    .and_then(|v| v.to_str())
                    .ok_or_else(error)?
                    .into(),
            ))
        } else {
            descend(&guard.directory, &item.name)
        }
    }
    fn checked_file(guard: &VerifiedArtifactSet, item: &Record) -> io::Result<File> {
        let (parent, name) = location(guard, item)?;
        let file = openat(
            &parent,
            &name,
            libc::O_RDONLY | if item.directory { libc::O_DIRECTORY } else { 0 },
            0,
        )?;
        let metadata = file.metadata()?;
        if metadata.dev() != item.dev
            || metadata.ino() != item.ino
            || metadata.is_dir() != item.directory
        {
            return Err(error());
        }
        Ok(file)
    }
    #[cfg(all(
        debug_assertions,
        target_os = "macos",
        target_arch = "aarch64",
        any(feature = "development-worker", test)
    ))]
    pub(super) fn connected_artifacts_stable(
        guard: &VerifiedArtifactSet,
        app_root: &Path,
        cancelled: &dyn Fn() -> bool,
    ) -> io::Result<()> {
        if cancelled()
            || guard.records.is_empty()
            || guard.root.parent().and_then(Path::parent) != Some(app_root)
        {
            return Err(error());
        }
        protected_ancestors(app_root)?;
        private(&root_dir(app_root)?)?;
        let staging = root_dir(guard.root.parent().ok_or_else(error)?)?;
        private(&staging)?;
        let published = root_dir(&guard.root)?;
        for (path, held) in [(&staging, &guard.parent), (&published, &guard.directory)] {
            let a = path.metadata()?;
            let b = held.metadata()?;
            if a.dev() != b.dev() || a.ino() != b.ino() {
                return Err(error());
            }
        }
        for item in &guard.records {
            if cancelled() {
                return Err(error());
            }
            if item.symlink {
                // Symlink contents cannot change in place. Verify its original
                // inode and change stamp without following it; every containing
                // directory is separately checked against its immutable stamp.
                let (parent, leaf) = location(guard, item)?;
                let current = record(&parent, &leaf, &item.name, false, true)?;
                if current.dev != item.dev
                    || current.ino != item.ino
                    || current.sealed != item.sealed
                    || current.sealed.as_ref().is_none_or(|stamp| {
                        stamp.uid != unsafe { libc::geteuid() }
                            || stamp.links != 1
                            || stamp.mode & libc::S_IFMT as u32 != libc::S_IFLNK as u32
                    })
                {
                    return Err(error());
                }
                continue;
            }
            let file = checked_file(guard, item)?;
            private(&file)?;
            let current = seal_stamp(&file)?;
            if item.sealed.as_ref() != Some(&current)
                || current.flags & libc::UF_IMMUTABLE == 0
                || current.mode & 0o222 != 0
                || (item.directory && current.mode & 0o777 != 0o500)
                || (!item.directory && (current.links != 1 || !file.metadata()?.is_file()))
            {
                return Err(error());
            }
        }
        if cancelled() {
            return Err(error());
        }
        Ok(())
    }
    pub(super) fn cleanup(guard: &mut VerifiedArtifactSet) -> io::Result<()> {
        // Reopen only recorded identities through the retained root descriptor.
        // Keep two descriptors per snapshot, not thousands of per-file locks.
        // Immutable files/directories retain integrity after temporary fds close.
        for item in &guard.records {
            if item.symlink {
                continue;
            }
            let file = checked_file(guard, item)?;
            #[cfg(target_os = "macos")]
            {
                cvt(unsafe { tr_unseal_fd(file.as_raw_fd()) })?;
            }
            if item.directory {
                cvt(unsafe { libc::fchmod(file.as_raw_fd(), 0o700) })?;
            }
        }
        while let Some(item) = guard.records.last() {
            let (parent, name) = location(guard, item)?;
            let current = record(&parent, &name, &item.name, item.directory, item.symlink)?;
            if current.dev != item.dev || current.ino != item.ino {
                return Err(error());
            }
            cvt(unsafe {
                libc::unlinkat(
                    parent.as_raw_fd(),
                    c(&name)?.as_ptr(),
                    if item.directory {
                        libc::AT_REMOVEDIR
                    } else {
                        0
                    },
                )
            })?;
            guard.records.pop();
        }
        Ok(())
    }
    struct CopyOps<'a> {
        clone_file: &'a dyn Fn(&File, &File, &str) -> io::Result<bool>,
        open_clone: &'a dyn Fn(&File, &str) -> io::Result<File>,
    }
    fn native_clone(source: &File, parent: &File, name: &str) -> io::Result<bool> {
        #[cfg(target_os = "macos")]
        {
            return Ok(cvt(unsafe {
                tr_clone_fd(source.as_raw_fd(), parent.as_raw_fd(), c(name)?.as_ptr())
            })? == 1);
        }
        #[cfg(not(target_os = "macos"))]
        {
            let _ = (source, parent, name);
            Ok(false)
        }
    }
    fn open_clone(parent: &File, name: &str) -> io::Result<File> {
        openat(parent, name, libc::O_RDONLY, 0)
    }
    fn prepare(
        source: &Path,
        app_root: &Path,
        inventory: &Inventory,
        cancelled: &dyn Fn() -> bool,
        complete_source: bool,
        known_generations: &[PathBuf],
        seal: &dyn Fn(&File, u32) -> io::Result<()>,
        copy: CopyOps<'_>,
    ) -> io::Result<VerifiedArtifactSet> {
        #[cfg(target_os = "macos")]
        let source_path = source;
        let source = root_dir(source)?;
        if complete_source {
            check_tree(&source, inventory, cancelled)?;
        }
        protected_ancestors(app_root)?;
        let app = root_dir(app_root)?;
        let staging = "execution-staging";
        let status = unsafe { libc::mkdirat(app.as_raw_fd(), c(staging)?.as_ptr(), 0o700) };
        if status < 0 && io::Error::last_os_error().raw_os_error() != Some(libc::EEXIST) {
            return Err(io::Error::last_os_error());
        }
        let parent = openat(&app, staging, libc::O_RDONLY | libc::O_DIRECTORY, 0)?;
        private(&parent)?;
        for name in names(&parent)? {
            if !known_generations.contains(&app_root.join(staging).join(name)) {
                return Err(io::Error::new(
                    io::ErrorKind::AlreadyExists,
                    "Execution snapshot cleanup pending",
                ));
            }
        }
        let mut random = [0; 24];
        getrandom::fill(&mut random).map_err(|_| error())?;
        let generation = random
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect::<String>();
        cvt(unsafe { libc::mkdirat(parent.as_raw_fd(), c(&generation)?.as_ptr(), 0o700) })?;
        let dest = openat(&parent, &generation, libc::O_RDONLY | libc::O_DIRECTORY, 0)?;
        private(&dest)?;
        let mut guard = VerifiedArtifactSet {
            root: app_root.join(staging).join(&generation),
            records: vec![record(&parent, &generation, "", true, false)?],
            directory: dest.try_clone()?,
            parent,
        };
        let result = (|| {
            #[cfg(target_os = "macos")]
            {
                cvt(unsafe { tr_copy_xattrs(source.as_raw_fd(), dest.as_raw_fd()) })?;
                // Bundle-level quarantine may live on .app rather than the
                // Resources subtree. Preserve the nearest ancestor marker too.
                for ancestor in source_path.ancestors() {
                    let fd = root_dir(ancestor)?;
                    let copied =
                        cvt(unsafe { tr_copy_quarantine(fd.as_raw_fd(), dest.as_raw_fd()) })?;
                    if copied == 1 {
                        break;
                    }
                }
            }
            for (name, entry) in &inventory.entries {
                if cancelled() {
                    return Err(error());
                }
                let (from, leaf) = descend(&source, name)?;
                let (to, _) = descend(&dest, name)?;
                match entry {
                    Entry::Directory { .. } => {
                        cvt(unsafe { libc::mkdirat(to.as_raw_fd(), c(&leaf)?.as_ptr(), 0o700) })?;
                        guard.records.push(record(&to, &leaf, name, true, false)?);
                        let file = openat(&to, &leaf, libc::O_RDONLY | libc::O_DIRECTORY, 0)?;
                        private(&file)?;
                        #[cfg(target_os = "macos")]
                        {
                            let src = openat(&from, &leaf, libc::O_RDONLY | libc::O_DIRECTORY, 0)?;
                            cvt(unsafe { tr_copy_xattrs(src.as_raw_fd(), file.as_raw_fd()) })?;
                        }
                    }
                    Entry::Symlink { target, .. } => {
                        // Source links were compared, never traversed or copied.
                        cvt(unsafe {
                            libc::symlinkat(c(target)?.as_ptr(), to.as_raw_fd(), c(&leaf)?.as_ptr())
                        })?;
                        guard.records.push(record(&to, &leaf, name, false, true)?);
                    }
                    Entry::File {
                        size, executable, ..
                    } => {
                        let mut input = openat(&from, &leaf, libc::O_RDONLY, 0)?;
                        let meta = input.metadata()?;
                        if !meta.is_file() || meta.len() != *size || meta.nlink() != 1 {
                            return Err(error());
                        }
                        let cloned = (copy.clone_file)(&input, &to, &leaf)?;
                        let mut output = if cloned {
                            // Clone preserves read-only package modes. Record its
                            // identity before any subsequent fallible operation.
                            guard.records.push(record(&to, &leaf, name, false, false)?);
                            (copy.open_clone)(&to, &leaf)?
                        } else {
                            openat(
                                &to,
                                &leaf,
                                libc::O_RDWR | libc::O_CREAT | libc::O_EXCL,
                                0o600,
                            )?
                        };
                        if !cloned {
                            guard.records.push(record(&to, &leaf, name, false, false)?);
                            let mut buf = [0; 65536];
                            let mut total = 0;
                            loop {
                                if cancelled() {
                                    return Err(error());
                                }
                                let n = input.read(&mut buf)?;
                                if n == 0 {
                                    break;
                                }
                                total += n as u64;
                                if total > *size {
                                    return Err(error());
                                }
                                output.write_all(&buf[..n])?;
                            }
                            if total != *size {
                                return Err(error());
                            }
                            #[cfg(target_os = "macos")]
                            {
                                cvt(unsafe {
                                    tr_copy_xattrs(input.as_raw_fd(), output.as_raw_fd())
                                })?;
                            }
                        }
                        cvt(unsafe {
                            libc::fchmod(
                                output.as_raw_fd(),
                                if *executable { 0o500 } else { 0o400 },
                            )
                        })?;
                        if !cloned {
                            output.sync_all()?;
                        }
                        drop(output);
                    }
                }
            }
            check_tree(&dest, inventory, cancelled)?;
            // Every writable descriptor was closed above. Reopen by recorded
            // identity, seal bottom-up, then close each temporary descriptor.
            for index in (0..guard.records.len()).rev() {
                if cancelled() {
                    return Err(error());
                }
                let item = &guard.records[index];
                if item.symlink {
                    continue;
                }
                let file = checked_file(&guard, item)?;
                seal(
                    &file,
                    if item.directory {
                        0o500
                    } else {
                        file.metadata()?.mode() & 0o777
                    },
                )?;
                #[cfg(all(debug_assertions, target_os = "macos"))]
                {
                    // Metadata makes an unseal/change/reseal on the same inode
                    // observable to fast capability checks without rehashing.
                    guard.records[index].sealed = Some(seal_stamp(&file)?);
                }
            }
            check_tree(&dest, inventory, cancelled)?;
            Ok(())
        })();
        if let Err(err) = result {
            if cleanup(&mut guard).is_err() {
                return Err(io::Error::other(
                    "Snapshot preparation failed; generated cleanup pending",
                ));
            }
            return Err(err);
        }
        Ok(guard)
    }
    #[cfg(target_os = "macos")]
    pub(super) fn prepare_native(
        source: &Path,
        app: &Path,
        inventory: &Inventory,
        cancelled: &dyn Fn() -> bool,
    ) -> io::Result<VerifiedArtifactSet> {
        prepare(
            source,
            app,
            inventory,
            cancelled,
            true,
            &[],
            &|file, mode| {
                private(file)?;
                cvt(unsafe { tr_seal_fd(file.as_raw_fd(), mode) })?;
                Ok(())
            },
            CopyOps {
                clone_file: &native_clone,
                open_clone: &open_clone,
            },
        )
    }
    #[cfg(target_os = "macos")]
    pub(super) fn prepare_model(
        source: &Path,
        app: &Path,
        inventory: &Inventory,
        cancelled: &dyn Fn() -> bool,
        known_generations: &[PathBuf],
    ) -> io::Result<VerifiedArtifactSet> {
        prepare(
            source,
            app,
            inventory,
            cancelled,
            false,
            known_generations,
            &|file, mode| {
                private(file)?;
                cvt(unsafe { tr_seal_fd(file.as_raw_fd(), mode) })?;
                Ok(())
            },
            CopyOps {
                clone_file: &native_clone,
                open_clone: &open_clone,
            },
        )
    }
    #[cfg(all(
        debug_assertions,
        target_os = "macos",
        target_arch = "aarch64",
        any(feature = "development-worker", test)
    ))]
    pub(super) fn prepare_connected_tdlib(
        app: &Path,
        cancelled: &dyn Fn() -> bool,
        known_generations: &[PathBuf],
    ) -> io::Result<VerifiedArtifactSet> {
        // This selected copy never enumerates the checkout. Only compiled
        // library/schema paths are opened and copied, then the destination is
        // checked against every compiled pin before it is sealed.
        private(&root_dir(app)?)?;
        let guard = prepare_model(
            crate::connected_local_proof::source_root()?,
            app,
            &crate::connected_local_proof::inventory()?,
            cancelled,
            known_generations,
        )?;
        Ok(guard)
    }
    #[cfg(all(
        debug_assertions,
        target_os = "macos",
        target_arch = "aarch64",
        any(feature = "development-worker", test)
    ))]
    pub(super) fn prepare_connected_inference(
        app: &Path,
        cancelled: &dyn Fn() -> bool,
        known_generations: &[PathBuf],
    ) -> io::Result<VerifiedArtifactSet> {
        private(&root_dir(app)?)?;
        prepare(
            &crate::connected_local_proof::inference::source_root()?,
            app,
            &crate::connected_local_proof::inference::inventory()?,
            cancelled,
            true,
            known_generations,
            &|file, mode| {
                private(file)?;
                cvt(unsafe { tr_seal_fd(file.as_raw_fd(), mode) })?;
                Ok(())
            },
            CopyOps {
                clone_file: &native_clone,
                open_clone: &open_clone,
            },
        )
    }
    #[cfg(test)]
    pub(super) fn prepare_test(
        source: &Path,
        app: &Path,
        inventory: &Inventory,
        cancelled: &dyn Fn() -> bool,
        fail_seal: bool,
    ) -> io::Result<VerifiedArtifactSet> {
        prepare(
            source,
            app,
            inventory,
            cancelled,
            true,
            &[],
            &|_, _| {
                if fail_seal {
                    Err(error())
                } else {
                    Ok(())
                }
            },
            CopyOps {
                clone_file: &native_clone,
                open_clone: &open_clone,
            },
        )
    }

    #[test]
    fn readonly_clone_and_post_create_open_failure_cleanup() {
        use std::os::unix::fs::PermissionsExt;
        let base = std::env::temp_dir().join(format!("threadrelay-clone-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(base.join("source")).unwrap();
        for mode in [0o400, 0o500] {
            std::fs::write(base.join("source/code"), b"synthetic").unwrap();
            std::fs::set_permissions(
                base.join("source/code"),
                std::fs::Permissions::from_mode(mode),
            )
            .unwrap();
            let inventory = Inventory::new(vec![Entry::File {
                path: "code".into(),
                sha256: format!("{:x}", Sha256::digest(b"synthetic")),
                size: 9,
                executable: mode == 0o500,
            }])
            .unwrap();
            let clone = |_: &File, to: &File, name: &str| {
                let mut output = openat(
                    to,
                    name,
                    libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL,
                    0o600,
                )?;
                output.write_all(b"synthetic")?;
                cvt(unsafe { libc::fchmod(output.as_raw_fd(), mode as libc::mode_t) })?;
                Ok(true)
            };
            let mut guard = prepare(
                &base.join("source"),
                &base,
                &inventory,
                &|| false,
                true,
                &[],
                &|_, _| Ok(()),
                CopyOps {
                    clone_file: &clone,
                    open_clone: &open_clone,
                },
            )
            .expect("read-only clone must be usable");
            assert_ne!(guard.root(), base.join("source"));
            assert_eq!(
                std::fs::read(guard.root().join("code")).unwrap(),
                b"synthetic"
            );
            guard.cleanup().unwrap();
            assert!(prepare(
                &base.join("source"),
                &base,
                &inventory,
                &|| false,
                true,
                &[],
                &|_, _| Ok(()),
                CopyOps {
                    clone_file: &clone,
                    open_clone: &|_, _| Err(error())
                }
            )
            .is_err());
            assert_eq!(
                std::fs::read_dir(base.join("execution-staging"))
                    .unwrap()
                    .count(),
                0,
                "post-create failure must clean the recorded clone"
            );
            assert_eq!(
                std::fs::read(base.join("source/code")).unwrap(),
                b"synthetic"
            );
            assert_eq!(
                std::fs::metadata(base.join("source/code")).unwrap().mode() & 0o777,
                mode
            );
            std::fs::remove_file(base.join("source/code")).unwrap();
        }
        std::fs::remove_dir_all(base).unwrap();
    }
}

#[derive(Clone)]
pub(crate) struct GuardedResources {
    root: PathBuf,
    app_root: PathBuf,
}
impl GuardedResources {
    #[cfg(all(
        debug_assertions,
        target_os = "macos",
        target_arch = "aarch64",
        any(feature = "development-worker", test)
    ))]
    pub(crate) fn from_connected_guard(guard: &VerifiedArtifactSet, app_root: &Path) -> Self {
        Self {
            root: guard.root().to_owned(),
            app_root: app_root.to_owned(),
        }
    }
    pub(crate) fn worker(&self, target: &str) -> PathBuf {
        crate::installed_layout::worker(&self.root, target)
    }
    pub(crate) fn root(&self) -> &Path {
        &self.root
    }
    pub(crate) fn app_root(&self) -> &Path {
        &self.app_root
    }
    pub(crate) fn contains(&self, path: &Path) -> bool {
        path.starts_with(&self.root)
            && !path
                .components()
                .any(|c| matches!(c, std::path::Component::ParentDir))
    }
}
/// Selected debug-build TDLib inputs, independent of installed inventories.
/// This does not mint a packaged resource or native acceptance capability.
#[cfg(all(
    debug_assertions,
    target_os = "macos",
    target_arch = "aarch64",
    any(feature = "development-worker", test)
))]
pub(crate) fn prepare_connected_tdlib(
    app_root: &Path,
    cancelled: &dyn Fn() -> bool,
    known_generations: &[PathBuf],
) -> io::Result<(GuardedResources, VerifiedArtifactSet)> {
    let guard = unix::prepare_connected_tdlib(app_root, cancelled, known_generations)?;
    let resources = GuardedResources {
        root: guard.root().to_owned(),
        app_root: app_root.to_owned(),
    };
    Ok((resources, guard))
}
#[cfg(all(
    debug_assertions,
    target_os = "macos",
    target_arch = "aarch64",
    any(feature = "development-worker", test)
))]
pub(crate) fn prepare_connected_inference(
    app_root: &Path,
    cancelled: &dyn Fn() -> bool,
    known_generations: &[PathBuf],
) -> io::Result<(GuardedResources, VerifiedArtifactSet)> {
    let guard = unix::prepare_connected_inference(app_root, cancelled, known_generations)?;
    let resources = GuardedResources::from_connected_guard(&guard, app_root);
    Ok((resources, guard))
}
#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct PackageInventory {
    schema_version: u32,
    targets: Vec<PackageTarget>,
}
#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct PackageTarget {
    platform: String,
    entries: Vec<Entry>,
}
fn installed_inventory(platform: &str) -> io::Result<Inventory> {
    let value: PackageInventory =
        serde_json::from_str(include_str!("../native/package-inventory.json"))?;
    let mut targets = value.targets.into_iter().filter(|v| v.platform == platform);
    let target = targets
        .next()
        .ok_or_else(|| io::Error::other("Installed worker inventory unavailable"))?;
    if value.schema_version != 1 || targets.next().is_some() {
        return Err(io::Error::other("Invalid package inventory"));
    }
    Inventory::new(target.entries)
}
/// Called only on explicit initialization, before ANY packaged worker executes.
/// Native acceptance is deliberately separate from local-worker integrity.
pub(crate) fn prepare_installed(
    source: &Path,
    app_root: &Path,
    cancelled: &dyn Fn() -> bool,
) -> io::Result<(GuardedResources, Option<VerifiedArtifactSet>)> {
    #[cfg(any(windows, target_os = "macos"))]
    {
        if !cfg!(any(
            all(windows, target_arch = "x86_64"),
            all(target_os = "macos", target_arch = "aarch64")
        )) {
            return Err(io::Error::other("Unsupported package target"));
        }
        let platform = if cfg!(windows) {
            "windows-x64"
        } else {
            "macos-arm64"
        };
        let inventory = installed_inventory(platform)?;
        #[cfg(target_os = "macos")]
        let guard = unix::prepare_native(source, app_root, &inventory, cancelled)?;
        #[cfg(windows)]
        let guard = windows_prepare(source, &inventory, cancelled, true)?;
        Ok((
            GuardedResources {
                root: guard.root.clone(),
                app_root: app_root.to_owned(),
            },
            Some(guard),
        ))
    }
    #[cfg(not(any(windows, target_os = "macos")))]
    {
        // Unsupported Linux is used for source-only framed-worker fixtures, not
        // a releasable target. NativeProof still denies native capabilities.
        let _ = (app_root, cancelled);
        Ok((
            GuardedResources {
                root: source.to_owned(),
                app_root: app_root.to_owned(),
            },
            None,
        ))
    }
}
// Fixed top-level files owned by Tauri/NSIS, never executable resources.
// Nested objects and other executables remain subject to the exact inventory.
#[cfg(any(windows, test))]
const WINDOWS_INSTALLED_HOST_FILES: [&str; 3] = [
    "threadrelay-desktop.exe",
    "threadrelay-evaluate.exe",
    "uninstall.exe",
];

// The same installed-layout closure check is exercised on source-test hosts;
// Windows handle retention and hardlink checks remain in windows_prepare.
#[cfg(any(windows, test))]
fn check_windows_tree(
    root: &Path,
    inventory: &Inventory,
    cancelled: &dyn Fn() -> bool,
    installed_root: bool,
) -> io::Result<()> {
    let mut pending = vec![root.to_owned()];
    let mut found = std::collections::BTreeSet::new();
    while let Some(dir) = pending.pop() {
        for entry in std::fs::read_dir(dir)? {
            if cancelled() {
                return Err(io::Error::other("Artifact verification cancelled"));
            }
            let path = entry?.path();
            let relative = path
                .strip_prefix(root)
                .map_err(|_| io::Error::other("Invalid bundle path"))?
                .to_str()
                .ok_or_else(|| io::Error::other("Invalid bundle path"))?
                .replace('\\', "/");
            if installed_root && WINDOWS_INSTALLED_HOST_FILES.contains(&relative.as_str()) {
                let meta = std::fs::symlink_metadata(&path)?;
                if crate::artifact_verifier::is_link(&meta) || !meta.is_file() {
                    return Err(io::Error::other("Invalid installed host file"));
                }
                continue;
            }
            let expected = inventory
                .entries
                .get(&relative)
                .ok_or_else(|| io::Error::other("Unexpected bundle object"))?;
            let meta = std::fs::symlink_metadata(&path)?;
            if crate::artifact_verifier::is_link(&meta)
                || meta.is_dir() != matches!(expected, Entry::Directory { .. })
                || (!meta.is_dir() && !meta.is_file())
            {
                return Err(io::Error::other("Bundle type mismatch"));
            }
            if meta.is_dir() {
                pending.push(path);
            }
            found.insert(relative);
        }
    }
    if found.len() != inventory.entries.len() {
        return Err(io::Error::other("Incomplete bundle"));
    }
    Ok(())
}

#[cfg(windows)]
fn windows_prepare(
    root: &Path,
    inventory: &Inventory,
    cancelled: &dyn Fn() -> bool,
    installed_root: bool,
) -> io::Result<VerifiedArtifactSet> {
    use std::os::windows::{fs::OpenOptionsExt, io::AsRawHandle};
    use windows_sys::Win32::{
        Foundation::CloseHandle,
        Security::{GetTokenInformation, TokenElevation, TOKEN_ELEVATION, TOKEN_QUERY},
        Storage::FileSystem::{GetFileInformationByHandle, BY_HANDLE_FILE_INFORMATION},
        System::Threading::{GetCurrentProcess, OpenProcessToken},
    };
    let mut token = std::ptr::null_mut();
    if unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) } == 0 {
        return Err(io::Error::last_os_error());
    }
    let mut elevation = TOKEN_ELEVATION { TokenIsElevated: 1 };
    let mut size = 0;
    let valid = unsafe {
        GetTokenInformation(
            token,
            TokenElevation,
            (&mut elevation as *mut TOKEN_ELEVATION).cast(),
            std::mem::size_of::<TOKEN_ELEVATION>() as u32,
            &mut size,
        )
    };
    unsafe { CloseHandle(token) };
    if valid == 0 || elevation.TokenIsElevated != 0 {
        return Err(io::Error::other("Non-elevated execution required"));
    }
    let mut holds = Vec::new();
    let mut files = Vec::new();
    let mut paths = vec![root.to_owned()];
    if installed_root {
        for name in WINDOWS_INSTALLED_HOST_FILES {
            let path = root.join(name);
            match std::fs::symlink_metadata(&path) {
                Ok(_) => paths.push(path),
                Err(e) if e.kind() == io::ErrorKind::NotFound => (),
                Err(e) => return Err(e),
            }
        }
    }
    for entry in inventory.entries.values() {
        if matches!(entry, Entry::Symlink { .. }) {
            return Err(io::Error::other("Windows bundle links unsupported"));
        }
        paths.push(root.join(entry.path()));
        if let Entry::File {
            path, sha256, size, ..
        } = entry
        {
            files.push((path.clone(), sha256.clone(), *size));
        }
    }
    // Hold ancestors too: replacement/rename of a parent must fail while lazy
    // imports/DLL/helper execution can still occur. No write/delete sharing.
    let mut ancestor = root.parent();
    while let Some(path) = ancestor {
        paths.push(path.to_owned());
        ancestor = path.parent();
    }
    for path in paths {
        if cancelled() {
            return Err(io::Error::other("Artifact verification cancelled"));
        }
        let meta = std::fs::symlink_metadata(&path)?;
        if crate::artifact_verifier::is_link(&meta) {
            return Err(io::Error::other("Artifact link unavailable"));
        }
        let file = std::fs::OpenOptions::new()
            .read(true)
            .share_mode(1)
            .custom_flags(0x0020_0000 | if meta.is_dir() { 0x0200_0000 } else { 0 })
            .open(path)?;
        if crate::artifact_verifier::is_link(&file.metadata()?) {
            return Err(io::Error::other("Artifact link unavailable"));
        }
        if !meta.is_dir() {
            let mut identity = std::mem::MaybeUninit::<BY_HANDLE_FILE_INFORMATION>::uninit();
            if unsafe {
                GetFileInformationByHandle(file.as_raw_handle().cast(), identity.as_mut_ptr())
            } == 0
                || unsafe { identity.assume_init() }.nNumberOfLinks != 1
            {
                return Err(io::Error::other("Hardlinked artifact unavailable"));
            }
        }
        holds.push(file);
    }
    check_windows_tree(root, inventory, cancelled, installed_root)?;
    for (path, hash, size) in files {
        crate::artifact_verifier::verify_file(&root.join(path), &hash, size, cancelled)?;
    }
    Ok(VerifiedArtifactSet {
        root: root.to_owned(),
        holds: Some(holds),
    })
}

pub(crate) fn verify_bundle(
    root: &Path,
    inventory: &Inventory,
    cancelled: &dyn Fn() -> bool,
) -> io::Result<()> {
    #[cfg(unix)]
    {
        unix::check_tree(&unix::root_dir(root)?, inventory, cancelled)
    }
    #[cfg(windows)]
    {
        let mut temporary = windows_prepare(root, inventory, cancelled, false)?;
        // The supervisor already retains the complete installed guard. This
        // redundant subtree verification owns no processes and can close now.
        temporary.cleanup()
    }
    #[cfg(not(any(unix, windows)))]
    {
        let _ = (root, inventory, cancelled);
        Err(io::Error::other("Unsupported bundle"))
    }
}
#[cfg(target_os = "macos")]
pub(crate) fn prepare_model(
    path: &Path,
    app_root: &Path,
    sha256: &str,
    size: u64,
    cancelled: &dyn Fn() -> bool,
    known_generations: &[PathBuf],
) -> io::Result<VerifiedArtifactSet> {
    let name = path
        .file_name()
        .and_then(|v| v.to_str())
        .ok_or_else(|| io::Error::other("Model unavailable"))?;
    let inventory = Inventory::new(vec![Entry::File {
        path: name.into(),
        sha256: sha256.into(),
        size,
        executable: false,
    }])?;
    unix::prepare_model(
        path.parent()
            .ok_or_else(|| io::Error::other("Model unavailable"))?,
        app_root,
        &inventory,
        cancelled,
        known_generations,
    )
}
/// Native acceptance harness: retain the production snapshot/sealing path.
/// This constructor is absent from every non-test build and does not mint a
/// compiled target receipt or modify the installed inventories.
#[cfg(all(test, target_os = "macos"))]
pub(crate) fn native_test_resources(
    source: &Path,
    app_root: &Path,
    inventory: &Inventory,
    cancelled: &dyn Fn() -> bool,
) -> io::Result<(GuardedResources, VerifiedArtifactSet)> {
    let guard = unix::prepare_native(source, app_root, inventory, cancelled)?;
    let resources = GuardedResources {
        root: guard.root().to_owned(),
        app_root: app_root.to_owned(),
    };
    Ok((resources, guard))
}

#[cfg(all(test, unix))]
pub(crate) fn synthetic_snapshot(
    source: &Path,
    app: &Path,
    inventory: &Inventory,
) -> io::Result<VerifiedArtifactSet> {
    unix::prepare_test(source, app, inventory, &|| false, false)
}
#[cfg(all(test, unix))]
pub(crate) fn synthetic_resources(
    source: &Path,
    app_root: &Path,
    inventory: &Inventory,
) -> io::Result<(GuardedResources, VerifiedArtifactSet)> {
    let guard = synthetic_snapshot(source, app_root, inventory)?;
    let resources = GuardedResources {
        root: guard.root().to_owned(),
        app_root: app_root.to_owned(),
    };
    Ok((resources, guard))
}
#[cfg(test)]
mod tests {
    use super::*;
    use crate::bundle_inventory::{Entry, Inventory};
    use sha2::{Digest, Sha256};
    #[test]
    fn installed_nsis_layout_keeps_strict_resource_closure() {
        let root =
            std::env::temp_dir().join(format!("threadrelay-nsis-layout-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(root.join("sidecars")).unwrap();
        std::fs::write(root.join("sidecars/worker.exe"), b"synthetic").unwrap();
        for name in [
            "threadrelay-desktop.exe",
            "threadrelay-evaluate.exe",
            "uninstall.exe",
        ] {
            std::fs::write(root.join(name), b"installer-owned synthetic").unwrap();
        }
        let inventory = Inventory::new(vec![
            Entry::Directory {
                path: "sidecars".into(),
            },
            Entry::File {
                path: "sidecars/worker.exe".into(),
                sha256: format!("{:x}", Sha256::digest(b"synthetic")),
                size: 9,
                executable: true,
            },
        ])
        .unwrap();
        assert!(check_windows_tree(&root, &inventory, &|| false, true).is_ok());
        assert!(check_windows_tree(&root, &inventory, &|| false, false).is_err());
        for name in [
            "unexpected.exe",
            "sidecars/uninstall.exe",
            "sidecars/threadrelay-desktop.exe",
        ] {
            std::fs::write(root.join(name), b"unexpected").unwrap();
            assert!(
                check_windows_tree(&root, &inventory, &|| false, true).is_err(),
                "{name}"
            );
            std::fs::remove_file(root.join(name)).unwrap();
        }
        std::fs::remove_file(root.join("uninstall.exe")).unwrap();
        std::fs::create_dir(root.join("uninstall.exe")).unwrap();
        assert!(check_windows_tree(&root, &inventory, &|| false, true).is_err());
        std::fs::remove_dir(root.join("uninstall.exe")).unwrap();
        #[cfg(unix)]
        {
            std::os::unix::fs::symlink(
                root.join("sidecars/worker.exe"),
                root.join("uninstall.exe"),
            )
            .unwrap();
            assert!(check_windows_tree(&root, &inventory, &|| false, true).is_err());
        }
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn no_target_package_inventory_is_not_a_load_capability() {
        assert!(installed_inventory("windows-x64").is_err());
        assert!(installed_inventory("macos-arm64").is_err());
    }
    #[test]
    #[cfg(unix)]
    fn links_hardlinks_and_mid_preparation_cancellation_fail_without_source_mutation() {
        let base = std::env::temp_dir().join(format!(
            "threadrelay-snapshot-attacks-{}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(base.join("source")).unwrap();
        let bytes = b"synthetic";
        std::fs::write(base.join("source/code"), bytes).unwrap();
        let inventory = Inventory::new(vec![Entry::File {
            path: "code".into(),
            sha256: format!("{:x}", Sha256::digest(bytes)),
            size: 9,
            executable: false,
        }])
        .unwrap();
        std::fs::hard_link(base.join("source/code"), base.join("alias")).unwrap();
        assert!(
            unix::prepare_test(&base.join("source"), &base, &inventory, &|| false, false).is_err()
        );
        std::fs::remove_file(base.join("alias")).unwrap();
        let cancelled = || {
            base.join("execution-staging")
                .read_dir()
                .is_ok_and(|mut dirs| dirs.next().is_some())
        };
        assert!(
            unix::prepare_test(&base.join("source"), &base, &inventory, &cancelled, false).is_err()
        );
        assert_eq!(std::fs::read(base.join("source/code")).unwrap(), bytes);
        assert_eq!(
            std::fs::read_dir(base.join("execution-staging"))
                .unwrap()
                .count(),
            0
        );
        std::fs::remove_dir_all(base).unwrap();
    }
    #[test]
    #[cfg(unix)]
    fn stale_generation_is_retained_and_reported_not_reused_or_unsealed() {
        use std::os::unix::fs::PermissionsExt;
        let base =
            std::env::temp_dir().join(format!("threadrelay-stale-snapshot-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(base.join("source")).unwrap();
        std::fs::write(base.join("source/code"), b"synthetic").unwrap();
        std::fs::create_dir_all(base.join("execution-staging/prior-generation")).unwrap();
        std::fs::set_permissions(
            base.join("execution-staging"),
            std::fs::Permissions::from_mode(0o700),
        )
        .unwrap();
        let inventory = Inventory::new(vec![Entry::File {
            path: "code".into(),
            sha256: format!("{:x}", Sha256::digest(b"synthetic")),
            size: 9,
            executable: false,
        }])
        .unwrap();
        let result = unix::prepare_test(&base.join("source"), &base, &inventory, &|| false, false);
        assert!(matches!(result,Err(e) if e.kind()==io::ErrorKind::AlreadyExists));
        assert!(base.join("execution-staging/prior-generation").is_dir());
        assert_eq!(
            std::fs::read_dir(base.join("execution-staging"))
                .unwrap()
                .count(),
            1
        );
        std::fs::remove_dir_all(base).unwrap();
    }
    #[test]
    fn execution_path_is_returned_from_guard_not_original_source() {
        let capability = GuardedResources {
            root: PathBuf::from("/private/generation"),
            app_root: PathBuf::from("/app"),
        };
        assert_eq!(
            capability.worker("macos"),
            Path::new("/private/generation/sidecars/threadrelay-worker")
        );
        assert_eq!(
            capability.worker("windows"),
            Path::new("/private/generation/sidecars/threadrelay-worker.exe")
        );
    }
    #[test]
    #[cfg(all(debug_assertions, target_os = "macos", target_arch = "aarch64"))]
    fn connected_guard_rechecks_private_paths_retention_flags_and_resealing_drift() {
        use std::os::{fd::AsRawFd, unix::fs::PermissionsExt};
        let base = Path::new("/private/tmp").join(format!(
            "threadrelay-connected-guard-{}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&base);
        for path in [&base, &base.join("app"), &base.join("source")] {
            std::fs::create_dir(path).unwrap();
            std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700)).unwrap();
        }
        std::fs::write(base.join("source/code"), b"synthetic").unwrap();
        let pins = Inventory::new(vec![Entry::File {
            path: "code".into(),
            sha256: format!("{:x}", Sha256::digest(b"synthetic")),
            size: 9,
            executable: false,
        }])
        .unwrap();
        let app = base.join("app");
        let (resources, guard) =
            native_test_resources(&base.join("source"), &app, &pins, &|| false).unwrap();
        let mut owner = crate::supervisor::DesktopSupervisor::acquire(&base.join("owner")).unwrap();
        assert!(!owner.retains_stable_artifacts(&resources, &|| false));
        owner.retain_artifacts(guard);
        assert!(owner.retains_stable_artifacts(&resources, &|| false));
        assert!(!owner.retains_stable_artifacts(&resources, &|| true));
        let mut wrong = resources.clone();
        wrong.app_root = base.clone();
        assert!(!owner.retains_stable_artifacts(&wrong, &|| false));
        wrong = resources.clone();
        wrong.root = resources.root.join("..");
        assert!(!owner.retains_stable_artifacts(&wrong, &|| false));
        let other = crate::supervisor::DesktopSupervisor::acquire(&base.join("other")).unwrap();
        assert!(!other.retains_stable_artifacts(&resources, &|| false));

        std::fs::set_permissions(&app, std::fs::Permissions::from_mode(0o755)).unwrap();
        assert!(!owner.retains_stable_artifacts(&resources, &|| false));
        std::fs::set_permissions(&app, std::fs::Permissions::from_mode(0o700)).unwrap();
        assert!(owner.retains_stable_artifacts(&resources, &|| false));

        let file = std::fs::File::open(resources.root.join("code")).unwrap();
        assert_eq!(unsafe { libc::fchflags(file.as_raw_fd(), 0) }, 0);
        assert!(!owner.retains_stable_artifacts(&resources, &|| false));
        assert_eq!(
            unsafe { libc::fchflags(file.as_raw_fd(), libc::UF_IMMUTABLE) },
            0
        );
        // Even a same-inode reseal cannot restore the captured change time.
        assert!(!owner.retains_stable_artifacts(&resources, &|| false));
        drop(file);
        assert!(owner.close_generation(100).stopped);
        assert!(!resources.root.exists());
        drop(other);
        drop(owner);
        std::fs::remove_dir_all(base).unwrap();
    }
    #[test]
    #[cfg(unix)]
    fn writable_app_parent_cannot_publish_snapshot_paths() {
        use std::os::unix::fs::PermissionsExt;
        let base = std::env::temp_dir().join(format!(
            "threadrelay-parent-snapshot-{}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(base.join("source")).unwrap();
        std::fs::write(base.join("source/code"), b"synthetic").unwrap();
        let inventory = Inventory::new(vec![Entry::File {
            path: "code".into(),
            sha256: format!("{:x}", Sha256::digest(b"synthetic")),
            size: 9,
            executable: false,
        }])
        .unwrap();
        std::fs::set_permissions(&base, std::fs::Permissions::from_mode(0o777)).unwrap();
        let result = unix::prepare_test(&base.join("source"), &base, &inventory, &|| false, false);
        assert!(
            result.is_err(),
            "private child is insufficient beneath writable app root"
        );
        std::fs::set_permissions(&base, std::fs::Permissions::from_mode(0o700)).unwrap();
        std::fs::remove_dir_all(base).unwrap();
    }
    #[test]
    #[cfg(target_os = "linux")]
    fn complete_large_inventory_does_not_retain_one_fd_per_file() {
        let base = std::env::temp_dir().join(format!(
            "threadrelay-fd-bounded-snapshot-{}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(base.join("source")).unwrap();
        let entries = (0..300)
            .map(|n| {
                let path = format!("file-{n:04}");
                std::fs::write(base.join("source").join(&path), b"synthetic").unwrap();
                Entry::File {
                    path,
                    sha256: format!("{:x}", Sha256::digest(b"synthetic")),
                    size: 9,
                    executable: false,
                }
            })
            .collect();
        let inventory = Inventory::new(entries).unwrap();
        let before = std::fs::read_dir("/proc/self/fd").unwrap().count();
        let mut guard =
            unix::prepare_test(&base.join("source"), &base, &inventory, &|| false, false).unwrap();
        let after = std::fs::read_dir("/proc/self/fd").unwrap().count();
        assert!(
            after.saturating_sub(before) < 128,
            "snapshot must not exhaust target default fd limits: {}",
            after.saturating_sub(before)
        );
        guard.cleanup().unwrap();
        std::fs::remove_dir_all(base).unwrap();
    }
    #[test]
    #[cfg(unix)]
    fn copied_paths_are_independent_and_failure_unwinds_only_generated_objects() {
        let base =
            std::env::temp_dir().join(format!("threadrelay-snapshot-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(base.join("source/dir")).unwrap();
        std::fs::write(base.join("source/dir/code"), b"synthetic").unwrap();
        std::os::unix::fs::symlink("dir/code", base.join("source/alias")).unwrap();
        let inventory = Inventory::new(vec![
            Entry::File {
                path: "dir/code".into(),
                sha256: format!("{:x}", Sha256::digest(b"synthetic")),
                size: 9,
                executable: false,
            },
            Entry::Symlink {
                path: "alias".into(),
                target: "dir/code".into(),
            },
        ])
        .unwrap();
        let mut guard =
            unix::prepare_test(&base.join("source"), &base, &inventory, &|| false, false).unwrap();
        assert_ne!(guard.root(), base.join("source"));
        std::fs::write(base.join("source/dir/code"), b"replaced!").unwrap();
        assert_eq!(
            std::fs::read(guard.root().join("alias")).unwrap(),
            b"synthetic"
        );
        assert!(guard.cleanup().is_ok());
        assert!(base.join("source/dir/code").exists());
        std::fs::write(base.join("source/dir/code"), b"synthetic").unwrap();
        assert!(
            unix::prepare_test(&base.join("source"), &base, &inventory, &|| true, false).is_err()
        );
        assert!(
            unix::prepare_test(&base.join("source"), &base, &inventory, &|| false, true).is_err()
        );
        assert_eq!(
            std::fs::read_dir(base.join("execution-staging"))
                .unwrap()
                .count(),
            0
        );
        std::fs::write(base.join("source/extra"), b"unexpected").unwrap();
        assert!(
            unix::prepare_test(&base.join("source"), &base, &inventory, &|| false, false).is_err()
        );
        std::fs::remove_dir_all(base).unwrap();
    }
}
