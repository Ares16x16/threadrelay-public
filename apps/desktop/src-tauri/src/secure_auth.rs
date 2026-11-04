//! Dedicated credential configuration, never an ordinary worker command.
//! Existing vault data is never overwritten: replacing an encryption key could
//! orphan the native account database. Secret bytes are not Debug or logged.
use crate::credentials::{CredentialError, CredentialVault, SecretBytes};
use std::sync::Mutex;

const ACCOUNT_REFERENCE: &str = "vault:threadrelay_account";
static CONFIGURE_LOCK: Mutex<()> = Mutex::new(());

struct Wipe(Vec<u8>);
impl Drop for Wipe {
    fn drop(&mut self) {
        for byte in &mut self.0 {
            unsafe { std::ptr::write_volatile(byte, 0) };
        }
        std::sync::atomic::compiler_fence(std::sync::atomic::Ordering::SeqCst);
    }
}

pub fn wipe_string(value: &mut String) {
    unsafe {
        for byte in value.as_bytes_mut() {
            std::ptr::write_volatile(byte, 0);
        }
    }
    std::sync::atomic::compiler_fence(std::sync::atomic::Ordering::SeqCst);
}

pub fn configure(
    vault: &dyn CredentialVault,
    api_id: &str,
    api_hash: &str,
) -> Result<(), CredentialError> {
    if api_id.is_empty()
        || api_id.len() > 10
        || !api_id.bytes().all(|b| b.is_ascii_digit())
        || api_hash.len() != 32
        || !api_hash.bytes().all(|b| b.is_ascii_hexdigit())
    {
        return Err(CredentialError::InvalidInput);
    }
    let id: u32 = api_id.parse().map_err(|_| CredentialError::InvalidInput)?;
    if id == 0 || id > i32::MAX as u32 {
        return Err(CredentialError::InvalidInput);
    }
    let _guard = CONFIGURE_LOCK
        .lock()
        .map_err(|_| CredentialError::Unavailable)?;
    match vault.get_interactive(ACCOUNT_REFERENCE) {
        Err(CredentialError::NotFound) => (),
        _ => return Err(CredentialError::Unavailable),
    }
    let mut bytes = Wipe(Vec::with_capacity(71));
    bytes.0.extend_from_slice(b"TR1");
    bytes.0.extend_from_slice(&id.to_be_bytes());
    bytes.0.extend_from_slice(api_hash.as_bytes());
    let mut key = [0u8; 32];
    getrandom::fill(&mut key).map_err(|_| CredentialError::Unavailable)?;
    bytes.0.extend_from_slice(&key);
    for byte in &mut key {
        unsafe { std::ptr::write_volatile(byte, 0) };
    }
    vault.put(ACCOUNT_REFERENCE, &bytes.0)
}

/// Read only the application-owned fixed account record. The owning buffer
/// zeroizes on drop and no reference, path or renderer input selects the item.
pub struct AccountSecret(SecretBytes);
impl AccountSecret {
    pub fn api_id(&self) -> u32 {
        u32::from_be_bytes(self.0.expose()[3..7].try_into().expect("validated"))
    }
    pub fn hash(&self) -> &str {
        std::str::from_utf8(&self.0.expose()[7..39]).expect("validated")
    }
    pub fn key(&self) -> &[u8] {
        &self.0.expose()[39..71]
    }
}
pub fn resolve_account(vault: &dyn CredentialVault) -> Result<AccountSecret, CredentialError> {
    let secret = vault.get(ACCOUNT_REFERENCE)?;
    let data = secret.expose();
    if data.len() != 71
        || &data[..3] != b"TR1"
        || !data[7..39].iter().all(u8::is_ascii_hexdigit)
        || u32::from_be_bytes(data[3..7].try_into().expect("validated")) == 0
        || u32::from_be_bytes(data[3..7].try_into().expect("validated")) > i32::MAX as u32
    {
        return Err(CredentialError::Unavailable);
    }
    Ok(AccountSecret(secret))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::credentials::{CredentialError, CredentialVault, SecretBytes};
    use std::sync::Mutex;
    struct Vault(Mutex<Vec<u8>>);
    impl CredentialVault for Vault {
        fn put(&self, _: &str, value: &[u8]) -> Result<(), CredentialError> {
            *self.0.lock().unwrap() = value.to_vec();
            Ok(())
        }
        fn get(&self, _: &str) -> Result<SecretBytes, CredentialError> {
            if self.0.lock().unwrap().is_empty() {
                Err(CredentialError::NotFound)
            } else {
                Err(CredentialError::Unavailable)
            }
        }
        fn delete(&self, _: &str) -> Result<(), CredentialError> {
            Ok(())
        }
    }
    #[test]
    fn account_resolution_accepts_only_fixed_well_formed_vault_record() {
        struct ReadVault(Vec<u8>);
        impl CredentialVault for ReadVault {
            fn put(&self, _: &str, _: &[u8]) -> Result<(), CredentialError> {
                unreachable!()
            }
            fn get(&self, name: &str) -> Result<SecretBytes, CredentialError> {
                assert_eq!(name, "vault:threadrelay_account");
                Ok(SecretBytes::for_test(self.0.clone()))
            }
            fn delete(&self, _: &str) -> Result<(), CredentialError> {
                unreachable!()
            }
        }
        let mut record = b"TR1".to_vec();
        record.extend_from_slice(&123u32.to_be_bytes());
        let mut random = [0u8; 48];
        getrandom::fill(&mut random).unwrap();
        for byte in &random[..16] {
            record.extend_from_slice(format!("{byte:02x}").as_bytes());
        }
        record.extend_from_slice(&random[16..]);
        let value = resolve_account(&ReadVault(record.clone())).unwrap();
        assert_eq!(value.api_id(), 123);
        assert_eq!(value.key().len(), 32);
        record[0] = 0;
        assert!(resolve_account(&ReadVault(record)).is_err());
    }

    #[test]
    fn passive_configured_status_never_reads_the_secret() {
        struct PresenceOnly;
        impl CredentialVault for PresenceOnly {
            fn contains(&self, reference: &str) -> Result<bool, CredentialError> {
                assert_eq!(reference, "vault:threadrelay_account");
                Ok(true)
            }
            fn put(&self, _: &str, _: &[u8]) -> Result<(), CredentialError> {
                panic!("passive status wrote secret")
            }
            fn get(&self, _: &str) -> Result<SecretBytes, CredentialError> {
                panic!("passive status decrypted secret")
            }
            fn delete(&self, _: &str) -> Result<(), CredentialError> {
                panic!("passive status deleted secret")
            }
        }
        assert!(configured(&PresenceOnly));
    }

    #[test]
    fn configuration_validates_and_stores_with_a_fresh_random_database_key() {
        let vault = Vault(Mutex::new(Vec::new()));
        configure(&vault, "0", &"a".repeat(32)).unwrap_err();
        assert!(vault.0.lock().unwrap().is_empty());
        configure(&vault, "12345", &"a".repeat(32)).unwrap();
        let first = vault.0.lock().unwrap().clone();
        assert!(configure(&vault, "12345", &"a".repeat(32)).is_err());
        assert_eq!(*vault.0.lock().unwrap(), first);
        let another = Vault(Mutex::new(Vec::new()));
        configure(&another, "12345", &"a".repeat(32)).unwrap();
        assert_ne!(first, *another.0.lock().unwrap());
        assert!(first.len() > 64);
    }
    #[test]
    fn explicit_configuration_checks_existing_record_interactively_without_replacing_its_key() {
        struct Existing {
            record: Vec<u8>,
            reads: std::sync::atomic::AtomicUsize,
        }
        impl CredentialVault for Existing {
            fn put(&self, _: &str, _: &[u8]) -> Result<(), CredentialError> {
                panic!("existing credential and encryption key must never be overwritten")
            }
            fn get(&self, _: &str) -> Result<SecretBytes, CredentialError> {
                panic!("explicit configuration must not use a passive two-second read")
            }
            fn get_interactive(&self, reference: &str) -> Result<SecretBytes, CredentialError> {
                assert_eq!(reference, "vault:threadrelay_account");
                self.reads.fetch_add(1, std::sync::atomic::Ordering::AcqRel);
                Ok(SecretBytes::for_test(self.record.clone()))
            }
            fn delete(&self, _: &str) -> Result<(), CredentialError> {
                unreachable!()
            }
        }
        let record = b"synthetic existing database encryption key".to_vec();
        let vault = Existing {
            record: record.clone(),
            reads: std::sync::atomic::AtomicUsize::new(0),
        };
        assert_eq!(
            configure(&vault, "123", &"a".repeat(32)),
            Err(CredentialError::Unavailable)
        );
        assert_eq!(vault.record, record);
        assert_eq!(vault.reads.load(std::sync::atomic::Ordering::Acquire), 1);
    }
}

/// Nonsecret status: no client construction, secret value or filesystem path in response.
pub fn record_presence(vault: &dyn CredentialVault) -> Result<bool, CredentialError> {
    vault.contains(ACCOUNT_REFERENCE)
}
pub fn configured(vault: &dyn CredentialVault) -> bool {
    // Presence is deliberately not credential validity. Explicit Begin still
    // resolves and validates the one fixed record before any native auth use.
    record_presence(vault).unwrap_or(false)
}

/// Only the fixed app-owned encrypted session directory is removable. The worker
/// calls this after revoking its epoch and observing native Closed (or proving no
/// client was constructed). Link/reparse/special entries fail closed; backups are
/// not discovered or deleted. Partial failure is reported, never a cleanup claim.
pub(crate) fn remove_local(
    vault: &dyn CredentialVault,
    root: &std::path::Path,
) -> std::io::Result<()> {
    use sha2::{Digest, Sha256};
    let _guard = CONFIGURE_LOCK
        .lock()
        .map_err(|_| std::io::Error::other("Cleanup unavailable"))?;
    fn inspect_tree(
        path: &std::path::Path,
        depth: usize,
        count: &mut usize,
    ) -> std::io::Result<()> {
        if depth > 32 || *count >= 100_000 {
            return Err(std::io::Error::other("Cleanup bound exceeded"));
        }
        *count += 1;
        let m = std::fs::symlink_metadata(path)?;
        #[cfg(windows)]
        {
            use std::os::windows::fs::MetadataExt;
            if m.file_attributes() & 0x400 != 0 {
                return Err(std::io::Error::other("Cleanup link rejected"));
            }
        }
        if m.file_type().is_symlink() {
            return Err(std::io::Error::other("Cleanup link rejected"));
        }
        if m.is_dir() {
            for child in std::fs::read_dir(path)? {
                inspect_tree(&child?.path(), depth + 1, count)?;
            }
            Ok(())
        } else if m.is_file() {
            Ok(())
        } else {
            Err(std::io::Error::other("Cleanup entry rejected"))
        }
    }
    // Protect ancestors too; never follow a replaced telegram root.
    let mut current = root.to_path_buf();
    loop {
        let m = std::fs::symlink_metadata(&current)?;
        if m.file_type().is_symlink() || !m.is_dir() {
            return Err(std::io::Error::other("Cleanup root rejected"));
        }
        #[cfg(windows)]
        {
            use std::os::windows::fs::MetadataExt;
            if m.file_attributes() & 0x400 != 0 {
                return Err(std::io::Error::other("Cleanup root rejected"));
            }
        }
        if !current.pop() {
            break;
        }
    }
    let parent = root.join("telegram");
    match std::fs::symlink_metadata(&parent) {
        Ok(m) => {
            if m.file_type().is_symlink() || !m.is_dir() {
                return Err(std::io::Error::other("Cleanup root rejected"));
            }
            #[cfg(windows)]
            {
                use std::os::windows::fs::MetadataExt;
                if m.file_attributes() & 0x400 != 0 {
                    return Err(std::io::Error::other("Cleanup root rejected"));
                }
            }
            let exact = parent.join(format!(
                "{:x}",
                Sha256::digest(ACCOUNT_REFERENCE.as_bytes())
            ));
            match std::fs::symlink_metadata(&exact) {
                Ok(m) if m.is_dir() && !m.file_type().is_symlink() => {
                    inspect_tree(&exact, 0, &mut 0)?;
                    // The standard recursive remover does not traverse symbolic
                    // links. Do not implement destructive path-walking ourselves:
                    // an entry swapped after inspection must not escape the root.
                    std::fs::remove_dir_all(&exact)?;
                }
                Ok(_) => return Err(std::io::Error::other("Cleanup root rejected")),
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => (),
                Err(e) => return Err(e),
            }
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => (),
        Err(e) => return Err(e),
    }
    match vault.delete(ACCOUNT_REFERENCE) {
        Ok(()) | Err(CredentialError::NotFound) => Ok(()),
        Err(_) => Err(std::io::Error::other("Vault cleanup unconfirmed")),
    }
}

#[cfg(test)]
mod cleanup_tests {
    use super::*;
    use std::sync::atomic::{AtomicBool, Ordering};
    struct Vault(AtomicBool);
    impl CredentialVault for Vault {
        fn put(&self, _: &str, _: &[u8]) -> Result<(), CredentialError> {
            unreachable!()
        }
        fn get(&self, _: &str) -> Result<SecretBytes, CredentialError> {
            Err(CredentialError::NotFound)
        }
        fn delete(&self, name: &str) -> Result<(), CredentialError> {
            assert_eq!(name, ACCOUNT_REFERENCE);
            self.0.store(true, Ordering::SeqCst);
            Ok(())
        }
    }
    #[test]
    fn cleanup_is_exact_and_preserves_other_directories_and_backups() {
        use sha2::{Digest, Sha256};
        let root = std::env::temp_dir().join(format!(
            "threadrelay-account-cleanup-{}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&root);
        let exact = root.join("telegram").join(format!(
            "{:x}",
            Sha256::digest(ACCOUNT_REFERENCE.as_bytes())
        ));
        std::fs::create_dir_all(&exact).unwrap();
        std::fs::write(exact.join("fixture-cache"), b"synthetic").unwrap();
        let other = root.join("telegram/other-account");
        std::fs::create_dir(&other).unwrap();
        let backup = root.join("backup");
        std::fs::write(&backup, b"synthetic").unwrap();
        let vault = Vault(AtomicBool::new(false));
        remove_local(&vault, &root).unwrap();
        assert!(!exact.exists());
        assert!(other.exists() && backup.exists());
        assert!(vault.0.load(Ordering::SeqCst));
        #[cfg(unix)]
        {
            std::fs::create_dir(&exact).unwrap();
            std::os::unix::fs::symlink(&backup, exact.join("link")).unwrap();
            vault.0.store(false, Ordering::SeqCst);
            assert!(remove_local(&vault, &root).is_err());
            assert!(!vault.0.load(Ordering::SeqCst));
            assert!(backup.exists());
        }
        std::fs::remove_dir_all(root).unwrap();
    }
}
