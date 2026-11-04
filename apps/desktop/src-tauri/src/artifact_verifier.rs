//! Pinned installed-tree verification; catalog is compiled into the host.
//! Empty or incomplete target inventories cannot authorize native activation.
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    io::{self, Read},
    path::{Component, Path, PathBuf},
};

pub(crate) fn is_link(meta: &std::fs::Metadata) -> bool {
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        meta.file_type().is_symlink() || meta.file_attributes() & 0x400 != 0
    }
    #[cfg(not(windows))]
    {
        meta.file_type().is_symlink()
    }
}

/// Validate a complete, immutable, compile-time inventory. Production callers
/// must use the built-in audited inventory, not renderer/JSON-supplied hashes.
pub(crate) fn verify_tree(
    root: &Path,
    expected: &[(String, String)],
    cancelled: &dyn Fn() -> bool,
) -> io::Result<()> {
    if !root.is_absolute()
        || expected.is_empty()
        || expected.len() > crate::bundle_inventory::MAX_ENTRIES
        || cancelled()
    {
        return Err(io::Error::other("Artifact inventory unavailable"));
    }
    let meta = fs::symlink_metadata(root)?;
    if !meta.is_dir() || is_link(&meta) {
        return Err(io::Error::other("Artifact root unavailable"));
    }
    let mut entries = BTreeMap::new();
    for (name, hash) in expected {
        let path = Path::new(name);
        if path
            .components()
            .any(|c| !matches!(c, Component::Normal(_)))
            || name.len() > 1024
            || hash.len() != 64
            || !hash
                .bytes()
                .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
            || entries.insert(PathBuf::from(name), hash.as_str()).is_some()
        {
            return Err(io::Error::other("Invalid artifact inventory"));
        }
    }
    let mut pending = vec![(root.to_owned(), PathBuf::new())];
    let mut found = BTreeSet::new();
    while let Some((dir, relative)) = pending.pop() {
        for item in fs::read_dir(dir)? {
            if cancelled() {
                return Err(io::Error::other("Artifact verification cancelled"));
            }
            let item = item?;
            let path = item.path();
            let rel = relative.join(item.file_name());
            let meta = fs::symlink_metadata(&path)?;
            if is_link(&meta) {
                return Err(io::Error::other("Artifact link unavailable"));
            }
            if meta.is_dir() {
                pending.push((path, rel));
                continue;
            }
            if !meta.is_file() || found.len() >= crate::bundle_inventory::MAX_ENTRIES {
                return Err(io::Error::other("Invalid artifact file"));
            }
            let expected = entries
                .get(&rel)
                .ok_or_else(|| io::Error::other("Unexpected artifact file"))?;
            let mut options = fs::OpenOptions::new();
            options.read(true);
            #[cfg(unix)]
            {
                use std::os::unix::fs::OpenOptionsExt;
                options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
            }
            #[cfg(windows)]
            {
                use std::os::windows::fs::OpenOptionsExt;
                options.custom_flags(0x0020_0000);
            }
            let mut file = options.open(&path)?;
            let before = file.metadata()?;
            if !before.is_file() || before.len() != meta.len() {
                return Err(io::Error::other("Artifact changed"));
            }
            let mut digest = Sha256::new();
            let mut buf = [0u8; 65536];
            loop {
                if cancelled() {
                    return Err(io::Error::other("Artifact verification cancelled"));
                }
                let n = file.read(&mut buf)?;
                if n == 0 {
                    break;
                }
                digest.update(&buf[..n]);
            }
            let after = file.metadata()?;
            let current = fs::symlink_metadata(&path)?;
            if before.len() != after.len()
                || before.modified().ok() != after.modified().ok()
                || is_link(&current)
                || current.len() != after.len()
                || format!("{:x}", digest.finalize()) != *expected
            {
                return Err(io::Error::other("Artifact changed"));
            }
            found.insert(rel);
        }
    }
    if found.len() != entries.len() {
        return Err(io::Error::other("Artifact inventory incomplete"));
    }
    Ok(())
}

pub(crate) fn verify_file(
    path: &Path,
    expected: &str,
    expected_size: u64,
    cancelled: &dyn Fn() -> bool,
) -> io::Result<()> {
    if expected.len() != 64 || cancelled() {
        return Err(io::Error::other("Invalid artifact pin"));
    }
    let meta = fs::symlink_metadata(path)?;
    if !meta.is_file() || is_link(&meta) || meta.len() != expected_size {
        return Err(io::Error::other("Artifact unavailable"));
    }
    let mut options = fs::OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        options.custom_flags(0x0020_0000);
    }
    let mut file = options.open(path)?;
    let before = file.metadata()?;
    if !before.is_file() || before.len() != expected_size {
        return Err(io::Error::other("Artifact unavailable"));
    }
    let mut hash = Sha256::new();
    let mut buffer = [0u8; 65536];
    loop {
        if cancelled() {
            return Err(io::Error::other("Artifact verification cancelled"));
        }
        let n = file.read(&mut buffer)?;
        if n == 0 {
            break;
        }
        hash.update(&buffer[..n]);
    }
    let after = file.metadata()?;
    let current = fs::symlink_metadata(path)?;
    if before.len() != after.len()
        || before.modified().ok() != after.modified().ok()
        || is_link(&current)
        || current.len() != after.len()
        || format!("{:x}", hash.finalize()) != expected
    {
        return Err(io::Error::other("Artifact changed"));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn evidence_is_parsed_from_the_exact_verified_bytes() {
        let path =
            std::env::temp_dir().join(format!("threadrelay-evidence-{}", std::process::id()));
        let bytes = b"{\"synthetic\":true}";
        std::fs::write(&path, bytes).unwrap();
        let data = read_verified_file(
            &path,
            &format!("{:x}", Sha256::digest(bytes)),
            bytes.len() as u64,
            &|| false,
        )
        .unwrap();
        std::fs::write(&path, b"changed").unwrap();
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&data).unwrap()["synthetic"],
            true
        );
        assert!(read_verified_file(&path, &"a".repeat(64), 7, &|| false).is_err());
        std::fs::remove_file(path).unwrap();
    }
    #[test]
    fn tree_verifier_rejects_missing_extra_and_changed_files() {
        let root =
            std::env::temp_dir().join(format!("threadrelay-artifact-tree-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(root.join("program"), b"synthetic").unwrap();
        let expected = vec![(
            "program".to_owned(),
            format!("{:x}", sha2::Sha256::digest(b"synthetic")),
        )];
        assert!(verify_tree(&root, &expected, &|| false).is_ok());
        let checks = std::cell::Cell::new(0);
        assert!(verify_tree(&root, &expected, &|| {
            checks.set(checks.get() + 1);
            checks.get() >= 3
        })
        .is_err());
        assert!(
            checks.get() >= 3,
            "verification observes cancellation while reading"
        );
        std::fs::write(root.join("extra"), b"x").unwrap();
        assert!(verify_tree(&root, &expected, &|| false).is_err());
        std::fs::remove_file(root.join("extra")).unwrap();
        std::fs::write(root.join("program"), b"changed").unwrap();
        assert!(verify_tree(&root, &expected, &|| false).is_err());
        std::fs::remove_dir_all(root).unwrap();
    }
}

/// Hash and return one bounded evidence read. Never reopen between hash/parse.
pub(crate) fn read_verified_file(
    path: &Path,
    expected: &str,
    size: u64,
    cancelled: &dyn Fn() -> bool,
) -> io::Result<Vec<u8>> {
    if size == 0 || size > 1_048_576 || expected.len() != 64 || cancelled() {
        return Err(io::Error::other("Evidence unavailable"));
    }
    let mut options = fs::OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        options.custom_flags(0x0020_0000);
    }
    let file = options.open(path)?;
    let metadata = file.metadata()?;
    if !metadata.is_file() || is_link(&metadata) || metadata.len() != size {
        return Err(io::Error::other("Evidence unavailable"));
    }
    let mut data = Vec::with_capacity(size as usize);
    file.take(size + 1).read_to_end(&mut data)?;
    if cancelled()
        || data.len() as u64 != size
        || format!("{:x}", Sha256::digest(&data)) != expected
    {
        return Err(io::Error::other("Evidence unavailable"));
    }
    Ok(data)
}
