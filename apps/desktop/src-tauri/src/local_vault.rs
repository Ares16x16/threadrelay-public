//! Host-only macOS remembered credentials. The local random key and authenticated
//! ciphertext stay in owner-only storage; no Keychain access is needed to reopen.
//! This protects against other local users, not code running as the same user.
use crate::credentials::{valid_ref, CredentialError, CredentialVault, PlatformVault, SecretBytes};
use std::{
    ffi::{CString, OsStr},
    fs::File,
    io::{Read, Write},
    os::{
        fd::{AsRawFd, FromRawFd},
        unix::{ffi::OsStrExt, fs::MetadataExt},
    },
    path::{Component, Path},
};

const STORE: &str = "remembered-credentials";
const KEY: &str = "app-key-v1";
const LOCK: &str = "store-lock";
const MAGIC: &[u8; 8] = b"TRVAULT1";
const MAX_SECRET: usize = 2560;
const MAX_RECORD: usize = 8 + 16 + 2576 + 32;
type Result<T> = std::result::Result<T, CredentialError>;

unsafe extern "C" {
    fn tr_private_fd(fd: libc::c_int) -> libc::c_int;
    fn tr_parent_fd(fd: libc::c_int) -> libc::c_int;
    fn CCCrypt(
        operation: u32,
        algorithm: u32,
        options: u32,
        key: *const u8,
        key_len: usize,
        iv: *const u8,
        input: *const u8,
        input_len: usize,
        output: *mut u8,
        capacity: usize,
        written: *mut usize,
    ) -> i32;
    fn CCHmac(
        algorithm: u32,
        key: *const u8,
        key_len: usize,
        input: *const u8,
        input_len: usize,
        output: *mut u8,
    );
}

fn unavailable<T>() -> Result<T> {
    Err(CredentialError::Unavailable)
}
fn io(error: std::io::Error) -> CredentialError {
    if error.kind() == std::io::ErrorKind::NotFound {
        CredentialError::NotFound
    } else {
        CredentialError::Unavailable
    }
}
fn c(value: &OsStr) -> Result<CString> {
    CString::new(value.as_bytes()).map_err(|_| CredentialError::InvalidInput)
}
pub(crate) fn open_at(parent: &File, name: &str, flags: i32, mode: u32) -> Result<File> {
    let name = c(OsStr::new(name))?;
    let fd = unsafe {
        libc::openat(
            parent.as_raw_fd(),
            name.as_ptr(),
            flags | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC,
            mode,
        )
    };
    if fd < 0 {
        return Err(io(std::io::Error::last_os_error()));
    }
    Ok(unsafe { File::from_raw_fd(fd) })
}
pub(crate) fn private(file: &File, directory: bool) -> Result<()> {
    let m = file.metadata().map_err(io)?;
    if m.uid() != unsafe { libc::geteuid() }
        || m.mode() & 0o7777 != if directory { 0o700 } else { 0o600 }
        || if directory {
            !m.is_dir()
        } else {
            !m.is_file() || m.nlink() != 1
        }
        || unsafe { tr_private_fd(file.as_raw_fd()) } != 0
    {
        return unavailable();
    }
    Ok(())
}
pub(crate) fn root(path: &Path) -> Result<File> {
    if !path.is_absolute() {
        return Err(CredentialError::InvalidInput);
    }
    let mut current = File::open("/").map_err(io)?;
    for component in path.components() {
        let m = current.metadata().map_err(io)?;
        let sticky_root = m.uid() == 0 && m.mode() & 0o1000 != 0;
        if (m.uid() != 0 && m.uid() != unsafe { libc::geteuid() })
            || (m.mode() & 0o022 != 0 && !sticky_root)
            || unsafe { tr_parent_fd(current.as_raw_fd()) } != 0
        {
            return unavailable();
        }
        match component {
            Component::RootDir => (),
            Component::Normal(part) => {
                current = open_at(
                    &current,
                    part.to_str().ok_or(CredentialError::InvalidInput)?,
                    libc::O_RDONLY | libc::O_DIRECTORY,
                    0,
                )?;
            }
            _ => return Err(CredentialError::InvalidInput),
        }
    }
    private(&current, true)?;
    Ok(current)
}
fn record(reference: &str) -> Result<String> {
    if !valid_ref(reference) {
        return Err(CredentialError::InvalidInput);
    }
    Ok(format!("{}.record", &reference[6..]))
}
fn read_file(dir: &File, name: &str, max: usize) -> Result<SecretBytes> {
    let mut file = open_at(dir, name, libc::O_RDONLY, 0)?;
    private(&file, false)?;
    if file.metadata().map_err(io)?.len() > max as u64 {
        return unavailable();
    }
    // Allocate once so a plaintext key never passes through an abandoned
    // realloc buffer. The extra byte detects growth past the fixed bound.
    let mut bytes = SecretBytes::new(Vec::with_capacity(max + 1));
    Read::by_ref(&mut file)
        .take(max as u64 + 1)
        .read_to_end(&mut bytes.0)
        .map_err(io)?;
    private(&file, false)?;
    if bytes.expose().len() > max {
        return unavailable();
    }
    Ok(bytes)
}

/// Pins a verified app root; missing remembered storage is not created by reads.
pub(crate) struct LocalVault {
    root: File,
}
impl LocalVault {
    pub(crate) fn new(app_root: &Path) -> Result<Self> {
        Ok(Self {
            root: root(app_root)?,
        })
    }
    fn store(&self, create: bool) -> Result<File> {
        private(&self.root, true)?;
        if create {
            let name = c(OsStr::new(STORE))?;
            if unsafe { libc::mkdirat(self.root.as_raw_fd(), name.as_ptr(), 0o700) } != 0
                && std::io::Error::last_os_error().kind() != std::io::ErrorKind::AlreadyExists
            {
                return unavailable();
            }
            self.root.sync_all().map_err(io)?;
        }
        let dir = open_at(&self.root, STORE, libc::O_RDONLY | libc::O_DIRECTORY, 0)?;
        private(&dir, true)?;
        Ok(dir)
    }
    fn locked(&self, create: bool) -> Result<(File, File)> {
        let dir = self.store(create)?;
        let flags = libc::O_RDWR | if create { libc::O_CREAT } else { 0 };
        let mut attempts = 0;
        let lock = loop {
            match open_at(&dir, LOCK, flags, 0o600) {
                Ok(file) => break file,
                // Darwin can report ENOENT when concurrent O_CREAT opens race,
                // even once another opener has installed the file. Retry the
                // descriptor-relative open without relaxing its checks.
                Err(CredentialError::NotFound) if create && attempts < 16 => {
                    attempts += 1;
                    std::thread::sleep(std::time::Duration::from_millis(5));
                }
                Err(CredentialError::NotFound) if !create && names(&dir)?.is_empty() => {
                    // A crash between mkdir and lock creation leaves no data.
                    return Err(CredentialError::NotFound);
                }
                Err(CredentialError::NotFound) => return unavailable(),
                Err(error) => return Err(error),
            }
        };
        private(&lock, false)?;
        // Every operation uses a distinct open file description, so flock
        // serializes separate handles in this process and other app processes.
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(2);
        loop {
            if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0 {
                break;
            }
            let error = std::io::Error::last_os_error().raw_os_error();
            if !matches!(error, Some(libc::EWOULDBLOCK) | Some(libc::EINTR))
                || std::time::Instant::now() >= deadline
            {
                return unavailable();
            }
            std::thread::sleep(std::time::Duration::from_millis(5));
        }
        private(&dir, true)?;
        Ok((dir, lock))
    }
    fn key(&self, dir: &File, create: bool) -> Result<SecretBytes> {
        match read_file(dir, KEY, 64) {
            Ok(key) if key.expose().len() == 64 => Ok(key),
            Ok(_) => unavailable(),
            Err(CredentialError::NotFound) if create => {
                // An absent key alongside ANY previous data is data loss,
                // never an invitation to silently generate a replacement key.
                if names(dir)?.iter().any(|n| n != LOCK) {
                    return unavailable();
                }
                let mut bytes = SecretBytes::new(vec![0; 64]);
                getrandom::fill(&mut bytes.0).map_err(|_| CredentialError::Unavailable)?;
                replace(dir, KEY, bytes.expose())?;
                Ok(bytes)
            }
            Err(e) => Err(e),
        }
    }
    fn put_locked(&self, dir: &File, reference: &str, secret: &[u8]) -> Result<()> {
        let name = record(reference)?;
        match open_at(dir, &name, libc::O_RDONLY, 0) {
            Ok(existing) => private(&existing, false)?,
            Err(CredentialError::NotFound) => (),
            Err(e) => return Err(e),
        }
        let key = self.key(dir, true)?;
        let sealed = seal(reference, secret, key.expose())?;
        replace(dir, &name, &sealed)
    }
    fn migrate(&self, reference: &str, secret: &[u8]) -> Result<SecretBytes> {
        validate(reference, secret)?;
        let (dir, _lock) = self.locked(true)?;
        match self.get_locked(&dir, reference) {
            Ok(existing) => {
                // Never replace a concurrently stored database key. If it is
                // different, reject rather than returning a mismatched blob.
                if equal(existing.expose(), secret) {
                    Ok(existing)
                } else {
                    unavailable()
                }
            }
            Err(CredentialError::NotFound) => {
                self.put_locked(&dir, reference, secret)?;
                Ok(SecretBytes::new(secret.to_vec()))
            }
            Err(e) => Err(e),
        }
    }
    fn get_locked(&self, dir: &File, reference: &str) -> Result<SecretBytes> {
        let bytes = match read_file(dir, &record(reference)?, MAX_RECORD) {
            Ok(bytes) => bytes,
            Err(CredentialError::NotFound) => {
                // A missing requested record must not mask a damaged existing
                // key and trigger a legacy password prompt. Empty fresh stores
                // remain eligible for their first exact-byte migration.
                match self.key(dir, false) {
                    Ok(_) => return Err(CredentialError::NotFound),
                    Err(CredentialError::NotFound)
                        if names(dir)?.iter().all(|name| name == LOCK) =>
                    {
                        return Err(CredentialError::NotFound);
                    }
                    _ => return unavailable(),
                }
            }
            Err(error) => return Err(error),
        };
        let key = self
            .key(dir, false)
            .map_err(|_| CredentialError::Unavailable)?;
        unseal(reference, bytes.expose(), key.expose())
    }
}
impl CredentialVault for LocalVault {
    fn contains(&self, reference: &str) -> Result<bool> {
        let name = record(reference)?;
        let dir = match self.store(false) {
            Err(CredentialError::NotFound) => return Ok(false),
            other => other?,
        };
        match open_at(&dir, &name, libc::O_RDONLY, 0) {
            Ok(file) => {
                private(&file, false)?;
                let size = file.metadata().map_err(io)?.len();
                if !(72..=MAX_RECORD as u64).contains(&size) {
                    return unavailable();
                }
                Ok(true)
            }
            Err(CredentialError::NotFound) => Ok(false),
            Err(e) => Err(e),
        }
    }
    fn put(&self, reference: &str, secret: &[u8]) -> Result<()> {
        validate(reference, secret)?;
        let (dir, _lock) = self.locked(true)?;
        self.put_locked(&dir, reference, secret)
    }
    fn get(&self, reference: &str) -> Result<SecretBytes> {
        record(reference)?;
        let (dir, _lock) = self.locked(false)?;
        self.get_locked(&dir, reference)
    }
    fn delete(&self, reference: &str) -> Result<()> {
        let name = record(reference)?;
        let (dir, _lock) = self.locked(false)?;
        let file = open_at(&dir, &name, libc::O_RDONLY, 0)?;
        private(&file, false)?;
        unlink(&dir, &name)?;
        dir.sync_all().map_err(io)
    }
}

/// Legacy Keychain reads happen only during explicit owner authentication.
/// Migration preserves the legacy backup and stores the exact original blob.
pub(crate) struct RememberedVault<T = PlatformVault> {
    local: LocalVault,
    legacy: T,
}
impl<T> RememberedVault<T> {
    pub(crate) fn new(local: LocalVault, legacy: T) -> Self {
        Self { local, legacy }
    }
}
impl<T: CredentialVault> CredentialVault for RememberedVault<T> {
    fn contains(&self, reference: &str) -> Result<bool> {
        if self.local.contains(reference)? {
            Ok(true)
        } else {
            self.legacy.contains(reference)
        }
    }
    fn put(&self, reference: &str, secret: &[u8]) -> Result<()> {
        self.local.put(reference, secret)
    }
    fn get(&self, reference: &str) -> Result<SecretBytes> {
        self.local.get(reference)
    }
    fn get_interactive(&self, reference: &str) -> Result<SecretBytes> {
        match self.local.get(reference) {
            Ok(secret) => Ok(secret),
            Err(CredentialError::NotFound) => {
                let secret = self.legacy.get_interactive(reference)?;
                self.local.migrate(reference, secret.expose())
            }
            Err(e) => Err(e),
        }
    }
    fn delete(&self, reference: &str) -> Result<()> {
        match self.local.delete(reference) {
            Ok(()) | Err(CredentialError::NotFound) => (),
            Err(e) => return Err(e),
        }
        match self.legacy.delete(reference) {
            Ok(()) | Err(CredentialError::NotFound) => Ok(()),
            Err(e) => Err(e),
        }
    }
}

fn validate(reference: &str, secret: &[u8]) -> Result<()> {
    record(reference)?;
    if secret.is_empty() || secret.len() > MAX_SECRET {
        return Err(CredentialError::InvalidInput);
    }
    Ok(())
}
fn equal(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    a.iter()
        .zip(b)
        .fold(0u8, |difference, (a, b)| difference | (a ^ b))
        == 0
}
pub(crate) fn tag(reference: &str, bytes: &[u8], key: &[u8]) -> [u8; 32] {
    // Length-prefix the reference so its authenticated boundary is unambiguous.
    let mut input = Vec::with_capacity(4 + reference.len() + bytes.len());
    input.extend_from_slice(&(reference.len() as u32).to_be_bytes());
    input.extend_from_slice(reference.as_bytes());
    input.extend_from_slice(bytes);
    let mut result = [0u8; 32];
    // CommonCrypto kCCHmacAlgSHA256=2.
    unsafe {
        CCHmac(
            2,
            key.as_ptr(),
            key.len(),
            input.as_ptr(),
            input.len(),
            result.as_mut_ptr(),
        )
    };
    result
}
fn crypt(operation: u32, input: &[u8], key: &[u8], iv: &[u8]) -> Result<SecretBytes> {
    let mut output = SecretBytes::new(vec![0; input.len() + 16]);
    let mut written = 0;
    // CommonCrypto kCCAlgorithmAES=0, kCCOptionPKCS7Padding=1, AES-256 key.
    let status = unsafe {
        CCCrypt(
            operation,
            0,
            1,
            key.as_ptr(),
            key.len(),
            iv.as_ptr(),
            input.as_ptr(),
            input.len(),
            output.0.as_mut_ptr(),
            output.0.len(),
            &mut written,
        )
    };
    if status != 0 || written > output.0.len() {
        return unavailable();
    }
    output.0.truncate(written);
    Ok(output)
}
fn seal(reference: &str, secret: &[u8], key: &[u8]) -> Result<Vec<u8>> {
    validate(reference, secret)?;
    if key.len() != 64 {
        return unavailable();
    }
    let mut iv = [0u8; 16];
    getrandom::fill(&mut iv).map_err(|_| CredentialError::Unavailable)?;
    let ciphertext = crypt(0, secret, &key[..32], &iv)?;
    let mut bytes = Vec::with_capacity(MAX_RECORD);
    bytes.extend_from_slice(MAGIC);
    bytes.extend_from_slice(&iv);
    bytes.extend_from_slice(ciphertext.expose());
    let authentication = tag(reference, &bytes, &key[32..]);
    bytes.extend_from_slice(&authentication);
    Ok(bytes)
}
fn unseal(reference: &str, bytes: &[u8], key: &[u8]) -> Result<SecretBytes> {
    if key.len() != 64
        || bytes.len() < 72
        || bytes.len() > MAX_RECORD
        || &bytes[..8] != MAGIC
        || (bytes.len() - 56) % 16 != 0
    {
        return unavailable();
    }
    let end = bytes.len() - 32;
    if !equal(&tag(reference, &bytes[..end], &key[32..]), &bytes[end..]) {
        return unavailable();
    }
    // Only authenticated bytes reach the padding-sensitive decrypt primitive.
    let secret = crypt(1, &bytes[24..end], &key[..32], &bytes[8..24])?;
    validate(reference, secret.expose()).map_err(|_| CredentialError::Unavailable)?;
    Ok(secret)
}
fn unlink(dir: &File, name: &str) -> Result<()> {
    let name = c(OsStr::new(name))?;
    if unsafe { libc::unlinkat(dir.as_raw_fd(), name.as_ptr(), 0) } != 0 {
        return Err(io(std::io::Error::last_os_error()));
    }
    Ok(())
}
fn replace(dir: &File, name: &str, bytes: &[u8]) -> Result<()> {
    let mut random = [0u8; 16];
    getrandom::fill(&mut random).map_err(|_| CredentialError::Unavailable)?;
    let temp = format!(
        ".write-{}",
        random
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect::<String>()
    );
    let mut file = open_at(
        dir,
        &temp,
        libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL,
        0o600,
    )?;
    let result = (|| {
        private(&file, false)?;
        file.write_all(bytes).map_err(io)?;
        file.sync_all().map_err(io)?;
        private(&file, false)?;
        let from = c(OsStr::new(&temp))?;
        let to = c(OsStr::new(name))?;
        if unsafe { libc::renameat(dir.as_raw_fd(), from.as_ptr(), dir.as_raw_fd(), to.as_ptr()) }
            != 0
        {
            return unavailable();
        }
        dir.sync_all().map_err(io)
    })();
    if result.is_err() {
        let _ = unlink(dir, &temp);
    }
    result
}
fn names(dir: &File) -> Result<Vec<String>> {
    use std::os::fd::IntoRawFd;
    let fd = open_at(dir, ".", libc::O_RDONLY | libc::O_DIRECTORY, 0)?.into_raw_fd();
    let raw = unsafe { libc::fdopendir(fd) };
    if raw.is_null() {
        unsafe { libc::close(fd) };
        return unavailable();
    }
    let mut result = Vec::new();
    loop {
        unsafe { *libc::__error() = 0 };
        let entry = unsafe { libc::readdir(raw) };
        if entry.is_null() {
            let error = std::io::Error::last_os_error().raw_os_error();
            unsafe { libc::closedir(raw) };
            return if error == Some(0) {
                Ok(result)
            } else {
                unavailable()
            };
        }
        let name = unsafe { std::ffi::CStr::from_ptr((*entry).d_name.as_ptr()) }.to_bytes();
        if name != b"." && name != b".." {
            result.push(String::from_utf8_lossy(name).into_owned());
        }
        // Only a handful of fixed host credential references are expected.
        if result.len() > 256 {
            unsafe { libc::closedir(raw) };
            return unavailable();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::{symlink, DirBuilderExt, PermissionsExt};
    use std::{
        path::PathBuf,
        sync::{
            atomic::{AtomicUsize, Ordering},
            Mutex,
        },
    };
    const REFERENCE: &str = "vault:threadrelay_account";
    struct Fixture(PathBuf);
    impl Fixture {
        fn new() -> Self {
            static NEXT: AtomicUsize = AtomicUsize::new(0);
            // /private/tmp avoids macOS's symlinked /tmp ancestor. All content
            // is synthetic and isolated from real app-data and Keychain.
            let path = PathBuf::from("/private/tmp").join(format!(
                "threadrelay-local-vault-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            std::fs::DirBuilder::new()
                .mode(0o700)
                .create(&path)
                .unwrap();
            Self(path)
        }
        fn vault(&self) -> LocalVault {
            LocalVault::new(&self.0).unwrap()
        }
        fn store(&self) -> PathBuf {
            self.0.join(STORE)
        }
        fn record(&self) -> PathBuf {
            self.store().join("threadrelay_account.record")
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }
    fn secret() -> &'static [u8] {
        b"synthetic credential containing exact existing database key"
    }

    #[test]
    fn missing_passive_reads_do_not_create_store_or_key() {
        let fixture = Fixture::new();
        let vault = fixture.vault();
        assert_eq!(vault.contains(REFERENCE), Ok(false));
        assert!(matches!(
            vault.get(REFERENCE),
            Err(CredentialError::NotFound)
        ));
        assert!(!fixture.store().exists());
        for reference in [
            "",
            "vault:",
            "../bad",
            "vault:../bad",
            "vault:slash/path",
            "vault:é",
        ] {
            assert!(vault.contains(reference).is_err());
            assert!(vault.put(reference, secret()).is_err());
        }
        assert!(!fixture.store().exists());
    }
    #[test]
    fn reopen_roundtrip_ciphertext_and_owner_only_modes() {
        let fixture = Fixture::new();
        fixture.vault().put(REFERENCE, secret()).unwrap();
        let bytes = std::fs::read(fixture.record()).unwrap();
        assert!(!bytes.windows(secret().len()).any(|value| value == secret()));
        assert_eq!(fixture.vault().get(REFERENCE).unwrap().expose(), secret());
        assert_eq!(fixture.vault().contains(REFERENCE), Ok(true));
        for path in [fixture.0.clone(), fixture.store()] {
            assert_eq!(std::fs::metadata(path).unwrap().mode() & 0o7777, 0o700);
        }
        for path in [
            fixture.store().join(KEY),
            fixture.store().join(LOCK),
            fixture.record(),
        ] {
            let m = std::fs::metadata(path).unwrap();
            assert_eq!(m.mode() & 0o7777, 0o600);
            assert_eq!(m.uid(), unsafe { libc::geteuid() });
            assert_eq!(m.nlink(), 1);
        }
        fixture.vault().put(REFERENCE, secret()).unwrap();
        assert_ne!(
            bytes,
            std::fs::read(fixture.record()).unwrap(),
            "every encryption needs a fresh IV"
        );
    }
    #[test]
    fn crypto_authenticates_version_reference_iv_ciphertext_and_tag() {
        let key = [11; 64];
        for length in [1, 15, 16, 17, 2560] {
            let plaintext = vec![0x71; length];
            let sealed = seal(REFERENCE, &plaintext, &key).unwrap();
            assert_eq!(
                unseal(REFERENCE, &sealed, &key).unwrap().expose(),
                plaintext
            );
            assert!(unseal("vault:different", &sealed, &key).is_err());
            assert!(unseal(REFERENCE, &sealed, &[12; 64]).is_err());
            for at in [0, 8, 24, sealed.len() - 1] {
                let mut bad = sealed.clone();
                bad[at] ^= 1;
                assert!(unseal(REFERENCE, &bad, &key).is_err());
            }
            for end in [0, 8, 24, 55, 56, 71, sealed.len() - 1] {
                assert!(unseal(REFERENCE, &sealed[..end.min(sealed.len())], &key).is_err());
            }
        }
        assert!(seal(REFERENCE, &[], &key).is_err());
        assert!(seal(REFERENCE, &vec![1; 2561], &key).is_err());
    }
    #[test]
    fn missing_or_corrupted_key_is_never_regenerated_over_records() {
        let fixture = Fixture::new();
        let vault = fixture.vault();
        vault.put(REFERENCE, secret()).unwrap();
        let record = std::fs::read(fixture.record()).unwrap();
        std::fs::remove_file(fixture.store().join(KEY)).unwrap();
        assert!(vault.get(REFERENCE).is_err());
        assert!(vault.put("vault:second", b"synthetic second").is_err());
        assert!(!fixture.store().join(KEY).exists());
        assert_eq!(std::fs::read(fixture.record()).unwrap(), record);
        std::fs::write(fixture.store().join(KEY), [1; 63]).unwrap();
        std::fs::set_permissions(
            fixture.store().join(KEY),
            std::fs::Permissions::from_mode(0o600),
        )
        .unwrap();
        assert!(vault.put(REFERENCE, secret()).is_err());
        assert_eq!(
            std::fs::read(fixture.store().join(KEY)).unwrap(),
            vec![1; 63]
        );
    }
    #[test]
    fn reject_links_at_root_store_key_lock_and_record() {
        let fixture = Fixture::new();
        let linked = fixture.0.join("linked");
        symlink(&fixture.0, &linked).unwrap();
        assert!(LocalVault::new(&linked).is_err());
        for leaf in [KEY, LOCK, "threadrelay_account.record"] {
            let fixture = Fixture::new();
            let vault = fixture.vault();
            vault.put(REFERENCE, secret()).unwrap();
            let original = fixture.store().join(leaf);
            let backup = fixture.0.join("backup");
            std::fs::rename(&original, &backup).unwrap();
            symlink(&backup, &original).unwrap();
            assert!(vault.get(REFERENCE).is_err(), "followed symlink {leaf}");
            assert!(
                vault.put(REFERENCE, secret()).is_err(),
                "replaced symlink {leaf}"
            );
        }
        let fixture = Fixture::new();
        let other = Fixture::new();
        symlink(&other.0, fixture.store()).unwrap();
        assert!(fixture.vault().put(REFERENCE, secret()).is_err());
        assert_eq!(std::fs::read_dir(&other.0).unwrap().count(), 0);
    }
    #[test]
    fn reject_hardlinks_wide_modes_and_extended_acl() {
        for leaf in [KEY, LOCK, "threadrelay_account.record"] {
            let fixture = Fixture::new();
            let vault = fixture.vault();
            vault.put(REFERENCE, secret()).unwrap();
            std::fs::hard_link(fixture.store().join(leaf), fixture.0.join("hardlink")).unwrap();
            assert!(vault.get(REFERENCE).is_err(), "accepted hardlink {leaf}");
            assert!(vault.put(REFERENCE, secret()).is_err());
        }
        for suffix in [
            "",
            STORE,
            "remembered-credentials/app-key-v1",
            "remembered-credentials/store-lock",
            "remembered-credentials/threadrelay_account.record",
        ] {
            let fixture = Fixture::new();
            let vault = fixture.vault();
            vault.put(REFERENCE, secret()).unwrap();
            let path = fixture.0.join(suffix);
            let mode = if path.is_dir() { 0o755 } else { 0o644 };
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(mode)).unwrap();
            assert!(
                vault.get(REFERENCE).is_err(),
                "accepted wide permissions {suffix}"
            );
        }
        let fixture = Fixture::new();
        let vault = fixture.vault();
        vault.put(REFERENCE, secret()).unwrap();
        assert!(std::process::Command::new("/bin/chmod")
            .args(["+a", "everyone allow read"])
            .arg(fixture.record())
            .status()
            .unwrap()
            .success());
        assert!(vault.get(REFERENCE).is_err());
    }
    #[test]
    fn oversized_truncated_and_corrupt_records_fail_closed() {
        for bad in [vec![], vec![7; 71], vec![7; MAX_RECORD + 1], vec![7; 72]] {
            let fixture = Fixture::new();
            let vault = fixture.vault();
            vault.put(REFERENCE, secret()).unwrap();
            std::fs::write(fixture.record(), bad).unwrap();
            assert!(vault.get(REFERENCE).is_err());
        }
    }
    #[test]
    fn concurrent_initial_creation_keeps_one_key_and_readable_records() {
        let fixture = Fixture::new();
        let threads: Vec<_> = (0..8)
            .map(|number| {
                let path = fixture.0.clone();
                std::thread::spawn(move || {
                    let vault = LocalVault::new(&path).unwrap();
                    let (dir, _lock) = vault
                        .locked(true)
                        .map_err(|e| format!("locked {number}: {e:?}"))?;
                    vault
                        .put_locked(&dir, &format!("vault:fixture{number}"), &[number as u8 + 1])
                        .map_err(|e| format!("put {number}: {e:?}"))
                })
            })
            .collect();
        for thread in threads {
            thread.join().unwrap().unwrap();
        }
        for number in 0..8 {
            assert_eq!(
                fixture
                    .vault()
                    .get(&format!("vault:fixture{number}"))
                    .unwrap()
                    .expose(),
                &[number as u8 + 1]
            );
        }
        assert_eq!(std::fs::read(fixture.store().join(KEY)).unwrap().len(), 64);
        assert!(!std::fs::read_dir(fixture.store())
            .unwrap()
            .any(|entry| entry
                .unwrap()
                .file_name()
                .to_string_lossy()
                .starts_with(".write-")));
    }
    struct Legacy {
        calls: Mutex<Vec<&'static str>>,
        value: Vec<u8>,
    }
    impl CredentialVault for Legacy {
        fn contains(&self, _: &str) -> Result<bool> {
            self.calls.lock().unwrap().push("contains");
            Ok(true)
        }
        fn put(&self, _: &str, _: &[u8]) -> Result<()> {
            panic!("new saves must never write legacy")
        }
        fn get(&self, _: &str) -> Result<SecretBytes> {
            panic!("passive read must never access legacy")
        }
        fn get_interactive(&self, _: &str) -> Result<SecretBytes> {
            self.calls.lock().unwrap().push("interactive");
            Ok(SecretBytes::new(self.value.clone()))
        }
        fn delete(&self, _: &str) -> Result<()> {
            self.calls.lock().unwrap().push("delete");
            Ok(())
        }
    }
    fn remembered(fixture: &Fixture) -> RememberedVault<Legacy> {
        RememberedVault::new(
            fixture.vault(),
            Legacy {
                calls: Mutex::new(vec![]),
                value: secret().to_vec(),
            },
        )
    }
    #[test]
    fn exact_legacy_migration_is_explicit_once_and_preserves_backup() {
        let fixture = Fixture::new();
        let vault = remembered(&fixture);
        assert_eq!(vault.contains(REFERENCE), Ok(true));
        assert!(matches!(
            vault.get(REFERENCE),
            Err(CredentialError::NotFound)
        ));
        assert_eq!(*vault.legacy.calls.lock().unwrap(), vec!["contains"]);
        assert_eq!(vault.get_interactive(REFERENCE).unwrap().expose(), secret());
        assert_eq!(vault.get_interactive(REFERENCE).unwrap().expose(), secret());
        assert_eq!(fixture.vault().get(REFERENCE).unwrap().expose(), secret());
        assert_eq!(
            *vault.legacy.calls.lock().unwrap(),
            vec!["contains", "interactive"]
        );
        assert_eq!(vault.legacy.value, secret());
        vault.delete(REFERENCE).unwrap();
        assert_eq!(
            *vault.legacy.calls.lock().unwrap(),
            vec!["contains", "interactive", "delete"]
        );
    }
    #[test]
    fn bad_local_never_uses_legacy_and_migration_never_replaces_existing_key() {
        let fixture = Fixture::new();
        let vault = remembered(&fixture);
        vault.put(REFERENCE, b"existing synthetic key").unwrap();
        assert!(vault.local.migrate(REFERENCE, secret()).is_err());
        assert_eq!(
            vault.get(REFERENCE).unwrap().expose(),
            b"existing synthetic key"
        );
        std::fs::write(fixture.record(), [1; 72]).unwrap();
        assert!(vault.get_interactive(REFERENCE).is_err());
        assert!(vault.legacy.calls.lock().unwrap().is_empty());
    }
    #[test]
    fn empty_directory_after_crash_can_migrate_without_passive_writes() {
        let fixture = Fixture::new();
        std::fs::DirBuilder::new()
            .mode(0o700)
            .create(fixture.store())
            .unwrap();
        let vault = remembered(&fixture);
        assert!(matches!(
            vault.get(REFERENCE),
            Err(CredentialError::NotFound)
        ));
        assert_eq!(std::fs::read_dir(fixture.store()).unwrap().count(), 0);
        assert_eq!(vault.get_interactive(REFERENCE).unwrap().expose(), secret());
    }
    #[test]
    fn bad_key_or_missing_lock_with_other_data_never_prompts_legacy() {
        for damage in ["missing-key", "bad-key", "missing-lock"] {
            let fixture = Fixture::new();
            let vault = remembered(&fixture);
            vault.put("vault:another_record", secret()).unwrap();
            match damage {
                "missing-key" => std::fs::remove_file(fixture.store().join(KEY)).unwrap(),
                "bad-key" => std::fs::write(fixture.store().join(KEY), [7; 63]).unwrap(),
                _ => std::fs::remove_file(fixture.store().join(LOCK)).unwrap(),
            }
            assert!(
                vault.get_interactive(REFERENCE).is_err(),
                "accepted {damage}"
            );
            assert!(
                vault.legacy.calls.lock().unwrap().is_empty(),
                "prompted for {damage}"
            );
        }
    }
}
