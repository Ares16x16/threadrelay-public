//! OS credential store boundary. No Tauri command or generic renderer bridge.
//! Secrets are never logged or serialized by this module. Unsupported platforms
//! and denied access fail closed; there is no plaintext fallback. The separate
//! macOS local vault handles explicitly remembered, encrypted host credentials.
use std::fmt;

pub(crate) const INTERACTIVE_AUTH_TIMEOUT: std::time::Duration =
    std::time::Duration::from_secs(120);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CredentialError {
    InvalidInput,
    Unavailable,
    NotFound,
}

/// Non-cloneable secret buffer with redacted debug and best-effort zeroization.
pub struct SecretBytes(pub(crate) Vec<u8>);
impl SecretBytes {
    pub(crate) fn new(bytes: Vec<u8>) -> Self {
        Self(bytes)
    }
    #[cfg(test)]
    pub(crate) fn for_test(bytes: Vec<u8>) -> Self {
        Self(bytes)
    }
    pub fn expose(&self) -> &[u8] {
        &self.0
    }
}
impl fmt::Debug for SecretBytes {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("SecretBytes([redacted])")
    }
}
impl Drop for SecretBytes {
    fn drop(&mut self) {
        for byte in &mut self.0 {
            unsafe { std::ptr::write_volatile(byte, 0) }
        }
        std::sync::atomic::compiler_fence(std::sync::atomic::Ordering::SeqCst);
    }
}

pub trait CredentialVault {
    /// Nonsecret presence only. Unsupported backends must not fall back to get.
    fn contains(&self, _reference: &str) -> Result<bool, CredentialError> {
        Err(CredentialError::Unavailable)
    }
    fn put(&self, reference: &str, secret: &[u8]) -> Result<(), CredentialError>;
    fn get(&self, reference: &str) -> Result<SecretBytes, CredentialError>;
    /// Explicit owner-initiated authentication only; passive callers use get.
    fn get_interactive(&self, reference: &str) -> Result<SecretBytes, CredentialError> {
        self.get(reference)
    }
    fn delete(&self, reference: &str) -> Result<(), CredentialError>;
}

#[cfg(any(target_os = "macos", test))]
#[derive(Default)]
struct SecretRead {
    pending: std::sync::Arc<std::sync::atomic::AtomicBool>,
}
#[cfg(any(target_os = "macos", test))]
impl SecretRead {
    fn read(
        &self,
        timeout: std::time::Duration,
        lookup: impl FnOnce() -> Result<Vec<u8>, CredentialError> + Send + 'static,
    ) -> Result<Vec<u8>, CredentialError> {
        use std::sync::atomic::Ordering;
        if self.pending.swap(true, Ordering::AcqRel) {
            return Err(CredentialError::Unavailable);
        }
        let pending = self.pending.clone();
        let (reply, received) = std::sync::mpsc::sync_channel(1);
        let spawned = std::thread::Builder::new()
            .name("keychain-secret-read".into())
            .spawn(move || {
                struct Release(std::sync::Arc<std::sync::atomic::AtomicBool>);
                impl Drop for Release {
                    fn drop(&mut self) {
                        self.0.store(false, Ordering::Release);
                    }
                }
                let _release = Release(pending);
                // A timed-out caller drops its receiver. Any late successful
                // result then drops SecretBytes and wipes its owned copy;
                // another login attempt can never adopt that old result.
                let _ = reply.send(lookup().map(SecretBytes));
            });
        if spawned.is_err() {
            self.pending.store(false, Ordering::Release);
            return Err(CredentialError::Unavailable);
        }
        received
            .recv_timeout(timeout)
            .map_err(|_| CredentialError::Unavailable)?
            .map(|mut secret| std::mem::take(&mut secret.0))
    }
}

/// A separate fixed Keychain namespace for never-sent connection profiles.
/// The private worker still sees its single opaque account reference; it
/// cannot select, import, or replace an old delivery credential/database key.
pub(crate) struct ConnectionVault<T>(pub(crate) T);
impl<T> CredentialVault for ConnectionVault<T>
where
    T: std::ops::Deref,
    T::Target: CredentialVault,
{
    fn contains(&self, reference: &str) -> Result<bool, CredentialError> {
        if reference != "vault:threadrelay_account" {
            return Err(CredentialError::InvalidInput);
        }
        self.0.contains("vault:threadrelay_connection_account")
    }
    fn put(&self, reference: &str, secret: &[u8]) -> Result<(), CredentialError> {
        if reference != "vault:threadrelay_account" {
            return Err(CredentialError::InvalidInput);
        }
        self.0.put("vault:threadrelay_connection_account", secret)
    }
    fn get(&self, reference: &str) -> Result<SecretBytes, CredentialError> {
        if reference != "vault:threadrelay_account" {
            return Err(CredentialError::InvalidInput);
        }
        self.0.get("vault:threadrelay_connection_account")
    }
    fn get_interactive(&self, reference: &str) -> Result<SecretBytes, CredentialError> {
        if reference != "vault:threadrelay_account" {
            return Err(CredentialError::InvalidInput);
        }
        self.0
            .get_interactive("vault:threadrelay_connection_account")
    }
    fn delete(&self, reference: &str) -> Result<(), CredentialError> {
        if reference != "vault:threadrelay_account" {
            return Err(CredentialError::InvalidInput);
        }
        self.0.delete("vault:threadrelay_connection_account")
    }
}

#[cfg(any(target_os = "macos", windows, test))]
struct PresenceProbe {
    result: std::sync::Mutex<Option<Result<bool, CredentialError>>>,
    completed: std::sync::Condvar,
}
/// Passive status returns even if the OS credential service is stalled. Keep
/// one process-local fixed-record lookup pending; polling cannot accumulate
/// blocked threads. Only a bool/error enters this cache, never blob bytes.
#[cfg(any(target_os = "macos", windows, test))]
fn bounded_presence(
    reference: &str,
    lookup: impl FnOnce(String) -> Result<bool, CredentialError> + Send + 'static,
) -> Result<bool, CredentialError> {
    use std::sync::{Arc, Mutex, OnceLock};
    static PENDING: OnceLock<Mutex<std::collections::HashMap<String, Arc<PresenceProbe>>>> =
        OnceLock::new();
    let pending = PENDING.get_or_init(|| Mutex::new(std::collections::HashMap::new()));
    let (probe, start) = {
        let mut probes = pending.lock().map_err(|_| CredentialError::Unavailable)?;
        match probes.get(reference) {
            Some(probe) => (probe.clone(), false),
            None => {
                let probe = Arc::new(PresenceProbe {
                    result: Mutex::new(None),
                    completed: std::sync::Condvar::new(),
                });
                probes.insert(reference.to_owned(), probe.clone());
                (probe, true)
            }
        }
    };
    if start {
        let lookup_probe = probe.clone();
        let owned_reference = reference.to_owned();
        if std::thread::Builder::new()
            .name("threadrelay-vault-presence".into())
            .spawn(move || {
                let result = lookup(owned_reference);
                if let Ok(mut slot) = lookup_probe.result.lock() {
                    *slot = Some(result);
                    lookup_probe.completed.notify_all();
                }
            })
            .is_err()
        {
            let mut probes = pending.lock().map_err(|_| CredentialError::Unavailable)?;
            if probes
                .get(reference)
                .is_some_and(|current| Arc::ptr_eq(current, &probe))
            {
                probes.remove(reference);
            }
            return Err(CredentialError::Unavailable);
        }
    }
    let slot = probe
        .result
        .lock()
        .map_err(|_| CredentialError::Unavailable)?;
    let (slot, _) = probe
        .completed
        .wait_timeout_while(slot, std::time::Duration::from_millis(250), |result| {
            result.is_none()
        })
        .map_err(|_| CredentialError::Unavailable)?;
    let result = slot.as_ref().copied().ok_or(CredentialError::Unavailable)?;
    drop(slot);
    let mut probes = pending.lock().map_err(|_| CredentialError::Unavailable)?;
    if probes
        .get(reference)
        .is_some_and(|current| Arc::ptr_eq(current, &probe))
    {
        probes.remove(reference);
    }
    result
}

pub struct PlatformVault;
impl PlatformVault {
    pub fn new() -> Self {
        Self
    }
}
impl Default for PlatformVault {
    fn default() -> Self {
        Self::new()
    }
}
pub(crate) fn valid_ref(reference: &str) -> bool {
    reference.strip_prefix("vault:").is_some_and(|s| {
        !s.is_empty()
            && s.len() <= 100
            && s.bytes()
                .all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-')
    })
}
impl CredentialVault for PlatformVault {
    fn contains(&self, reference: &str) -> Result<bool, CredentialError> {
        if !valid_ref(reference) {
            return Err(CredentialError::InvalidInput);
        }
        #[cfg(any(target_os = "macos", windows))]
        {
            platform::contains(reference)
        }
        #[cfg(not(any(target_os = "macos", windows)))]
        {
            Err(CredentialError::Unavailable)
        }
    }
    fn put(&self, reference: &str, secret: &[u8]) -> Result<(), CredentialError> {
        if !valid_ref(reference) || secret.is_empty() || secret.len() > 2560 {
            return Err(CredentialError::InvalidInput);
        }
        platform::put(reference, secret)
    }
    fn get(&self, reference: &str) -> Result<SecretBytes, CredentialError> {
        if !valid_ref(reference) {
            return Err(CredentialError::InvalidInput);
        }
        platform::get(reference).map(SecretBytes)
    }
    fn get_interactive(&self, reference: &str) -> Result<SecretBytes, CredentialError> {
        if !valid_ref(reference) {
            return Err(CredentialError::InvalidInput);
        }
        #[cfg(target_os = "macos")]
        let value = platform::get_interactive(reference);
        #[cfg(not(target_os = "macos"))]
        let value = platform::get(reference);
        value.map(SecretBytes)
    }
    fn delete(&self, reference: &str) -> Result<(), CredentialError> {
        if !valid_ref(reference) {
            return Err(CredentialError::InvalidInput);
        }
        platform::delete(reference)
    }
}

#[cfg(not(any(target_os = "windows", target_os = "macos")))]
mod platform {
    use super::CredentialError;
    pub fn put(_: &str, _: &[u8]) -> Result<(), CredentialError> {
        Err(CredentialError::Unavailable)
    }
    pub fn get(_: &str) -> Result<Vec<u8>, CredentialError> {
        Err(CredentialError::Unavailable)
    }
    pub fn delete(_: &str) -> Result<(), CredentialError> {
        Err(CredentialError::Unavailable)
    }
}

#[cfg(any(windows, test))]
mod windows_presence {
    use super::CredentialError;
    pub(super) struct Returned {
        pub item: *mut std::ffi::c_void,
        pub blob: *mut u8,
        pub size: usize,
    }
    pub(super) fn target(reference: &str) -> Vec<u16> {
        format!("ThreadRelay/{reference}\0")
            .encode_utf16()
            .collect()
    }
    /// Caller owns the native allocation and guarantees the blob range is valid
    /// for its documented size. No blob byte is read, decoded or copied.
    pub(super) unsafe fn query(
        target: &[u16],
        read: impl FnOnce(&[u16], u32, u32) -> Result<Option<Returned>, u32>,
        free: impl FnOnce(*mut std::ffi::c_void),
    ) -> Result<bool, CredentialError> {
        // WinCred CRED_TYPE_GENERIC=1, reserved flags=0; ERROR_NOT_FOUND=1168.
        let record = match read(target, 1, 0) {
            Err(1168) => return Ok(false),
            Err(_) => return Err(CredentialError::Unavailable),
            Ok(None) => return Err(CredentialError::Unavailable),
            Ok(Some(record)) => record,
        };
        if record.item.is_null() {
            return Err(CredentialError::Unavailable);
        }
        let valid = record.size <= 2560 && (record.size == 0 || !record.blob.is_null());
        if valid {
            for offset in 0..record.size {
                unsafe {
                    std::ptr::write_volatile(record.blob.add(offset), 0);
                }
            }
            std::sync::atomic::compiler_fence(std::sync::atomic::Ordering::SeqCst);
        }
        free(record.item);
        if valid {
            Ok(true)
        } else {
            Err(CredentialError::Unavailable)
        }
    }
}

#[cfg(target_os = "windows")]
mod platform {
    use super::CredentialError;
    use std::ptr::null_mut;
    use windows_sys::Win32::Foundation::{GetLastError, ERROR_NOT_FOUND};
    use windows_sys::Win32::Security::Credentials::{
        CredDeleteW, CredFree, CredReadW, CredWriteW, CREDENTIALW, CRED_PERSIST_LOCAL_MACHINE,
        CRED_TYPE_GENERIC,
    };
    fn target(reference: &str) -> Vec<u16> {
        super::windows_presence::target(reference)
    }
    fn error() -> CredentialError {
        if unsafe { GetLastError() } == ERROR_NOT_FOUND {
            CredentialError::NotFound
        } else {
            CredentialError::Unavailable
        }
    }
    pub fn contains(reference: &str) -> Result<bool, CredentialError> {
        super::bounded_presence(reference, |reference| {
            let name = target(&reference);
            // CredReadW has no metadata-only flag: Windows returns an OS-owned
            // block including the blob. Query exactly one private generic item;
            // never copy/parse/log its blob, and wipe it before CredFree.
            unsafe {
                super::windows_presence::query(
                    &name,
                    |name, kind, flags| {
                        let mut item: *mut CREDENTIALW = null_mut();
                        if CredReadW(name.as_ptr(), kind, flags, &mut item) == 0 {
                            let code = GetLastError(); // preserve before any cleanup call
                            if !item.is_null() {
                                CredFree(item.cast());
                            }
                            return Err(code);
                        }
                        if item.is_null() {
                            return Ok(None);
                        }
                        Ok(Some(super::windows_presence::Returned {
                            item: item.cast(),
                            blob: (*item).CredentialBlob,
                            size: (*item).CredentialBlobSize as usize,
                        }))
                    },
                    |item| CredFree(item),
                )
            }
        })
    }
    pub fn put(reference: &str, secret: &[u8]) -> Result<(), CredentialError> {
        let mut name = target(reference);
        let mut credential = CREDENTIALW::default();
        credential.Type = CRED_TYPE_GENERIC;
        credential.TargetName = name.as_mut_ptr();
        credential.CredentialBlobSize = secret.len() as u32;
        credential.CredentialBlob = secret.as_ptr() as *mut u8;
        credential.Persist = CRED_PERSIST_LOCAL_MACHINE;
        if unsafe { CredWriteW(&credential, 0) } == 0 {
            Err(error())
        } else {
            Ok(())
        }
    }
    pub fn get(reference: &str) -> Result<Vec<u8>, CredentialError> {
        let name = target(reference);
        let mut item: *mut CREDENTIALW = null_mut();
        if unsafe { CredReadW(name.as_ptr(), CRED_TYPE_GENERIC, 0, &mut item) } == 0 {
            return Err(error());
        }
        let result = unsafe {
            let n = (*item).CredentialBlobSize as usize;
            let p = (*item).CredentialBlob;
            if n == 0 || n > 2560 || p.is_null() {
                Err(CredentialError::Unavailable)
            } else {
                Ok(std::slice::from_raw_parts(p, n).to_vec())
            }
        };
        unsafe { CredFree(item.cast()) };
        result
    }
    pub fn delete(reference: &str) -> Result<(), CredentialError> {
        let name = target(reference);
        if unsafe { CredDeleteW(name.as_ptr(), CRED_TYPE_GENERIC, 0) } == 0 {
            Err(error())
        } else {
            Ok(())
        }
    }
}

#[cfg(target_os = "macos")]
mod platform {
    use super::CredentialError;
    use std::ffi::c_void;
    use std::ptr::{null, null_mut};
    type Item = *mut c_void;
    const SERVICE: &[u8] = b"ThreadRelay";
    const NOT_FOUND: i32 = -25300;
    #[link(name = "Security", kind = "framework")]
    extern "C" {
        fn SecKeychainFindGenericPassword(
            keychain: Item,
            service_len: u32,
            service: *const u8,
            account_len: u32,
            account: *const u8,
            length: *mut u32,
            data: *mut *mut c_void,
            item: *mut Item,
        ) -> i32;
        fn SecKeychainAddGenericPassword(
            keychain: Item,
            service_len: u32,
            service: *const u8,
            account_len: u32,
            account: *const u8,
            length: u32,
            data: *const c_void,
            item: *mut Item,
        ) -> i32;
        fn SecKeychainItemModifyAttributesAndData(
            item: Item,
            attributes: *const c_void,
            length: u32,
            data: *const c_void,
        ) -> i32;
        fn SecKeychainItemDelete(item: Item) -> i32;
        fn SecItemCopyMatching(query: *const c_void, result: *mut *const c_void) -> i32;
        static kSecClass: *const c_void;
        static kSecClassGenericPassword: *const c_void;
        static kSecAttrService: *const c_void;
        static kSecAttrAccount: *const c_void;
        static kSecMatchLimit: *const c_void;
        static kSecMatchLimitOne: *const c_void;
        static kSecReturnData: *const c_void;
        static kSecUseAuthenticationUI: *const c_void;
        static kSecUseAuthenticationUIFail: *const c_void;
        static kSecUseAuthenticationUIAllow: *const c_void;
    }
    #[link(name = "CoreFoundation", kind = "framework")]
    extern "C" {
        fn CFRelease(value: *const c_void);
        fn CFStringCreateWithBytes(
            allocator: *const c_void,
            bytes: *const u8,
            length: isize,
            encoding: u32,
            external: u8,
        ) -> *const c_void;
        fn CFDictionaryCreate(
            allocator: *const c_void,
            keys: *const *const c_void,
            values: *const *const c_void,
            count: isize,
            key_callbacks: *const c_void,
            value_callbacks: *const c_void,
        ) -> *const c_void;
        fn CFGetTypeID(value: *const c_void) -> usize;
        fn CFDataGetTypeID() -> usize;
        fn CFDataGetLength(data: *const c_void) -> isize;
        fn CFDataGetBytePtr(data: *const c_void) -> *const u8;
        static kCFBooleanTrue: *const c_void;
    }
    struct Owned(*const c_void);
    impl Owned {
        fn new(value: *const c_void) -> Result<Self, CredentialError> {
            if value.is_null() {
                Err(CredentialError::Unavailable)
            } else {
                Ok(Self(value))
            }
        }
    }
    impl Drop for Owned {
        fn drop(&mut self) {
            unsafe { CFRelease(self.0) }
        }
    }
    fn string(value: &[u8]) -> Result<Owned, CredentialError> {
        Owned::new(unsafe {
            CFStringCreateWithBytes(null(), value.as_ptr(), value.len() as isize, 0x08000100, 0)
        })
    }
    fn secret_query(
        reference: &str,
        lookup: impl FnOnce(*const c_void) -> (i32, Option<Owned>),
    ) -> Result<Vec<u8>, CredentialError> {
        secret_query_with_ui(reference, false, lookup)
    }
    fn secret_query_with_ui(
        reference: &str,
        interactive: bool,
        lookup: impl FnOnce(*const c_void) -> (i32, Option<Owned>),
    ) -> Result<Vec<u8>, CredentialError> {
        let service = string(SERVICE)?;
        let account = string(reference.as_bytes())?;
        // Null callbacks borrow the values. Hold both strings and the query
        // through the synchronous call; all other values are framework globals.
        // Do not select the Data Protection keychain or change legacy records.
        let keys = unsafe {
            [
                kSecClass,
                kSecAttrService,
                kSecAttrAccount,
                kSecMatchLimit,
                kSecReturnData,
                kSecUseAuthenticationUI,
            ]
        };
        let values = unsafe {
            [
                kSecClassGenericPassword,
                service.0,
                account.0,
                kSecMatchLimitOne,
                kCFBooleanTrue,
                if interactive {
                    kSecUseAuthenticationUIAllow
                } else {
                    kSecUseAuthenticationUIFail
                },
            ]
        };
        let query = Owned::new(unsafe {
            CFDictionaryCreate(
                null(),
                keys.as_ptr(),
                values.as_ptr(),
                keys.len() as isize,
                null(),
                null(),
            )
        })?;
        let (status, data) = lookup(query.0);
        if status != 0 {
            return Err(error(status));
        }
        let data = data.ok_or(CredentialError::Unavailable)?;
        if unsafe { CFGetTypeID(data.0) != CFDataGetTypeID() } {
            return Err(CredentialError::Unavailable);
        }
        let length = unsafe { CFDataGetLength(data.0) };
        if !(1..=2560).contains(&length) {
            return Err(CredentialError::Unavailable);
        }
        let bytes = unsafe { CFDataGetBytePtr(data.0) };
        if bytes.is_null() {
            return Err(CredentialError::Unavailable);
        }
        Ok(unsafe { std::slice::from_raw_parts(bytes, length as usize) }.to_vec())
    }
    fn error(code: i32) -> CredentialError {
        if code == NOT_FOUND {
            CredentialError::NotFound
        } else {
            CredentialError::Unavailable
        }
    }
    fn find(reference: &str, length: *mut u32, data: *mut *mut c_void, item: *mut Item) -> i32 {
        unsafe {
            SecKeychainFindGenericPassword(
                null_mut(),
                SERVICE.len() as u32,
                SERVICE.as_ptr(),
                reference.len() as u32,
                reference.as_ptr(),
                length,
                data,
                item,
            )
        }
    }
    pub fn contains(reference: &str) -> Result<bool, CredentialError> {
        super::bounded_presence(reference, |reference| find_presence(&reference))
    }
    fn find_presence(reference: &str) -> Result<bool, CredentialError> {
        let mut item: Item = null_mut();
        // Apple SDK SecKeychain.h passwordData documentation explicitly permits
        // NULL for finding only the retained item reference. Request neither
        // password length nor bytes: passive status must not decrypt a secret.
        let status = find(reference, null_mut(), null_mut(), &mut item);
        let found = !item.is_null();
        if found {
            unsafe {
                CFRelease(item);
            }
        }
        match status {
            0 if found => Ok(true),
            NOT_FOUND => Ok(false),
            _ => Err(CredentialError::Unavailable),
        }
    }
    pub fn put(reference: &str, secret: &[u8]) -> Result<(), CredentialError> {
        let mut item: Item = null_mut();
        let status = find(reference, null_mut(), null_mut(), &mut item);
        let result = if status == NOT_FOUND {
            unsafe {
                SecKeychainAddGenericPassword(
                    null_mut(),
                    SERVICE.len() as u32,
                    SERVICE.as_ptr(),
                    reference.len() as u32,
                    reference.as_ptr(),
                    secret.len() as u32,
                    secret.as_ptr().cast(),
                    null_mut(),
                )
            }
        } else if status == 0 && !item.is_null() {
            unsafe {
                SecKeychainItemModifyAttributesAndData(
                    item,
                    null(),
                    secret.len() as u32,
                    secret.as_ptr().cast(),
                )
            }
        } else {
            status
        };
        if !item.is_null() {
            unsafe { CFRelease(item) }
        }
        if result == 0 {
            Ok(())
        } else {
            Err(error(result))
        }
    }
    pub fn get(reference: &str) -> Result<Vec<u8>, CredentialError> {
        get_with_ui(reference, false)
    }
    pub fn get_interactive(reference: &str) -> Result<Vec<u8>, CredentialError> {
        get_with_ui(reference, true)
    }
    fn get_with_ui(reference: &str, interactive: bool) -> Result<Vec<u8>, CredentialError> {
        static READ: std::sync::OnceLock<super::SecretRead> = std::sync::OnceLock::new();
        let reference = reference.to_owned();
        // Interactive reads occur before dispatching the worker Begin command.
        // The host can cancel its generation while the OS dialog is open; the
        // single read slot still prevents abandoned dialogs accumulating.
        READ.get_or_init(super::SecretRead::default).read(
            if interactive {
                super::INTERACTIVE_AUTH_TIMEOUT
            } else {
                std::time::Duration::from_secs(2)
            },
            move || {
                let lookup = |query| {
                    let mut result = null();
                    let status = unsafe { SecItemCopyMatching(query, &mut result) };
                    (status, Owned::new(result).ok())
                };
                if interactive {
                    secret_query_with_ui(&reference, true, lookup)
                } else {
                    secret_query(&reference, lookup)
                }
            },
        )
    }
    #[cfg(test)]
    mod secret_query_tests {
        use super::*;
        #[link(name = "CoreFoundation", kind = "framework")]
        extern "C" {
            fn CFDictionaryGetCount(value: *const c_void) -> isize;
            fn CFDictionaryGetValue(value: *const c_void, key: *const c_void) -> *const c_void;
            fn CFEqual(first: *const c_void, second: *const c_void) -> u8;
            fn CFDataCreate(
                allocator: *const c_void,
                bytes: *const u8,
                length: isize,
            ) -> *const c_void;
        }
        fn data(value: &[u8]) -> Owned {
            Owned::new(unsafe { CFDataCreate(null(), value.as_ptr(), value.len() as isize) })
                .unwrap()
        }
        #[test]
        fn exact_secret_query_is_single_record_and_noninteractive_without_migration() {
            let reference = "vault:threadrelay_connection_account";
            let result = secret_query(reference, |query| {
                unsafe {
                    assert_eq!(CFDictionaryGetCount(query), 6);
                    for (key, value) in [
                        (kSecClass, kSecClassGenericPassword),
                        (kSecMatchLimit, kSecMatchLimitOne),
                        (kSecReturnData, kCFBooleanTrue),
                        (kSecUseAuthenticationUI, kSecUseAuthenticationUIFail),
                    ] {
                        assert_eq!(CFDictionaryGetValue(query, key), value);
                    }
                    let service = string(b"ThreadRelay").unwrap();
                    let account = string(reference.as_bytes()).unwrap();
                    assert_ne!(
                        CFEqual(CFDictionaryGetValue(query, kSecAttrService), service.0),
                        0
                    );
                    assert_ne!(
                        CFEqual(CFDictionaryGetValue(query, kSecAttrAccount), account.0),
                        0
                    );
                }
                (0, Some(data(b"synthetic fixture")))
            });
            assert_eq!(result.unwrap(), b"synthetic fixture");
        }
        #[test]
        fn explicit_owner_authorization_allows_ui_for_only_the_same_fixed_record() {
            let reference = "vault:threadrelay_connection_account";
            let result = secret_query_with_ui(reference, true, |query| {
                unsafe {
                    assert_eq!(CFDictionaryGetCount(query), 6);
                    assert_eq!(
                        CFDictionaryGetValue(query, kSecUseAuthenticationUI),
                        kSecUseAuthenticationUIAllow
                    );
                    assert_eq!(
                        CFDictionaryGetValue(query, kSecMatchLimit),
                        kSecMatchLimitOne
                    );
                    assert_eq!(CFDictionaryGetValue(query, kSecReturnData), kCFBooleanTrue);
                    let service = string(b"ThreadRelay").unwrap();
                    let account = string(reference.as_bytes()).unwrap();
                    assert_ne!(
                        CFEqual(CFDictionaryGetValue(query, kSecAttrService), service.0),
                        0
                    );
                    assert_ne!(
                        CFEqual(CFDictionaryGetValue(query, kSecAttrAccount), account.0),
                        0
                    );
                }
                (0, Some(data(b"synthetic fixture")))
            });
            assert_eq!(result.unwrap(), b"synthetic fixture");
        }
        #[test]
        fn secret_query_rejects_missing_denied_wrong_type_and_invalid_lengths() {
            for (status, expected) in [
                (NOT_FOUND, CredentialError::NotFound),
                (-25308, CredentialError::Unavailable),
                (-50, CredentialError::Unavailable),
            ] {
                assert_eq!(
                    secret_query("vault:fixture", |_| (status, None)),
                    Err(expected)
                );
            }
            assert_eq!(
                secret_query("vault:fixture", |_| (0, None)),
                Err(CredentialError::Unavailable)
            );
            assert_eq!(
                secret_query("vault:fixture", |_| (0, Some(string(b"not data").unwrap()))),
                Err(CredentialError::Unavailable)
            );
            for bytes in [vec![], vec![0; 2561]] {
                assert_eq!(
                    secret_query("vault:fixture", |_| (0, Some(data(&bytes)))),
                    Err(CredentialError::Unavailable)
                );
            }
            assert_eq!(
                secret_query("vault:fixture", |_| (0, Some(data(&vec![3; 2560])))).unwrap(),
                vec![3; 2560]
            );
        }
    }
    pub fn delete(reference: &str) -> Result<(), CredentialError> {
        let mut item: Item = null_mut();
        let status = find(reference, null_mut(), null_mut(), &mut item);
        if status != 0 {
            return Err(error(status));
        }
        if item.is_null() {
            return Err(CredentialError::Unavailable);
        }
        let status = unsafe { SecKeychainItemDelete(item) };
        unsafe { CFRelease(item) };
        if status == 0 {
            Ok(())
        } else {
            Err(error(status))
        }
    }
}

#[cfg(test)]
mod connection_tests {
    use super::*;
    use std::sync::Mutex;
    #[test]
    fn secret_read_times_out_without_accumulating_or_reusing_late_results() {
        use std::sync::{
            atomic::{AtomicUsize, Ordering},
            mpsc, Arc,
        };
        use std::time::{Duration, Instant};
        let reader = SecretRead::default();
        let calls = Arc::new(AtomicUsize::new(0));
        let counted = calls.clone();
        let (release, wait) = mpsc::sync_channel(1);
        let started = Instant::now();
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(300));
            let _ = release.send(());
        });
        let result = reader.read(Duration::from_millis(20), move || {
            counted.fetch_add(1, Ordering::AcqRel);
            wait.recv().unwrap();
            Ok(b"late synthetic secret".to_vec())
        });
        assert!(matches!(result, Err(CredentialError::Unavailable)));
        assert!(started.elapsed() < Duration::from_millis(150));
        let duplicate = calls.clone();
        assert!(reader
            .read(Duration::from_millis(20), move || {
                duplicate.fetch_add(1, Ordering::AcqRel);
                Ok(vec![1])
            })
            .is_err());
        assert_eq!(calls.load(Ordering::Acquire), 1);
        let deadline = Instant::now() + Duration::from_secs(2);
        while reader.pending.load(Ordering::Acquire) {
            assert!(Instant::now() < deadline);
            std::thread::sleep(Duration::from_millis(5));
        }
        assert_eq!(
            reader
                .read(Duration::from_secs(1), || Ok(
                    b"fresh synthetic secret".to_vec()
                ))
                .unwrap(),
            b"fresh synthetic secret"
        );
    }
    struct Memory(Mutex<Vec<String>>);
    impl CredentialVault for Memory {
        fn contains(&self, r: &str) -> Result<bool, CredentialError> {
            self.0.lock().unwrap().push(r.into());
            Ok(true)
        }
        fn put(&self, r: &str, _: &[u8]) -> Result<(), CredentialError> {
            self.0.lock().unwrap().push(r.into());
            Ok(())
        }
        fn get(&self, r: &str) -> Result<SecretBytes, CredentialError> {
            self.0.lock().unwrap().push(r.into());
            Ok(SecretBytes::for_test(vec![1]))
        }
        fn get_interactive(&self, r: &str) -> Result<SecretBytes, CredentialError> {
            self.0.lock().unwrap().push(format!("interactive:{r}"));
            Ok(SecretBytes::for_test(vec![2]))
        }
        fn delete(&self, r: &str) -> Result<(), CredentialError> {
            self.0.lock().unwrap().push(r.into());
            Ok(())
        }
    }
    #[test]
    fn passive_presence_is_bounded_and_reuses_one_pending_os_query() {
        use std::sync::{
            atomic::{AtomicUsize, Ordering},
            mpsc, Arc,
        };
        let key = format!("vault:presence-fixture-{}", std::process::id());
        let (release, wait) = mpsc::sync_channel(1);
        let calls = Arc::new(AtomicUsize::new(0));
        let counted = calls.clone();
        let first = bounded_presence(&key, move |_| {
            counted.fetch_add(1, Ordering::AcqRel);
            wait.recv_timeout(std::time::Duration::from_secs(5))
                .unwrap();
            Ok(true)
        });
        let second_calls = calls.clone();
        let second = bounded_presence(&key, move |_| {
            second_calls.fetch_add(1, Ordering::AcqRel);
            Ok(false)
        });
        release.send(()).unwrap();
        assert_eq!(
            first,
            Err(CredentialError::Unavailable),
            "passive presence waited for an unbounded OS lookup"
        );
        assert_eq!(second, Err(CredentialError::Unavailable));
        assert_eq!(
            bounded_presence(&key, |_| panic!("pending probe was replaced")),
            Ok(true)
        );
        assert_eq!(
            calls.load(Ordering::Acquire),
            1,
            "polling must not accumulate blocked OS queries"
        );
    }

    #[test]
    fn windows_presence_queries_one_exact_item_and_wipes_before_free() {
        let name = windows_presence::target("vault:threadrelay_account");
        assert_eq!(
            String::from_utf16(&name[..name.len() - 1]).unwrap(),
            "ThreadRelay/vault:threadrelay_account"
        );
        assert_eq!(name.last(), Some(&0));
        let mut blob = [91u8, 17, 22, 51];
        let pointer = blob.as_mut_ptr();
        let freed = std::cell::Cell::new(false);
        let result = unsafe {
            windows_presence::query(
                &name,
                |target, kind, flags| {
                    assert_eq!(target, name);
                    assert_eq!(kind, 1);
                    assert_eq!(flags, 0);
                    Ok(Some(windows_presence::Returned {
                        item: pointer.cast(),
                        blob: pointer,
                        size: 4,
                    }))
                },
                |item| {
                    assert_eq!(item, pointer.cast());
                    assert_eq!(std::slice::from_raw_parts(pointer, 4), &[0, 0, 0, 0]);
                    freed.set(true);
                },
            )
        };
        assert_eq!(result, Ok(true));
        assert!(freed.get());
        assert_eq!(blob, [0; 4]);
    }
    #[test]
    fn windows_presence_distinguishes_missing_errors_null_and_invalid_buffer() {
        let name = windows_presence::target("vault:threadrelay_connection_account");
        for (code, expected) in [
            (1168, Ok(false)),
            (5, Err(CredentialError::Unavailable)),
            (1312, Err(CredentialError::Unavailable)),
        ] {
            assert_eq!(
                unsafe {
                    windows_presence::query(
                        &name,
                        |_, _, _| Err(code),
                        |_| panic!("failed read had no owned item"),
                    )
                },
                expected
            );
        }
        assert_eq!(
            unsafe {
                windows_presence::query(
                    &name,
                    |_, _, _| Ok(None),
                    |_| panic!("null success must not free"),
                )
            },
            Err(CredentialError::Unavailable)
        );
        let freed = std::cell::Cell::new(false);
        assert_eq!(
            unsafe {
                windows_presence::query(
                    &name,
                    |_, _, _| {
                        Ok(Some(windows_presence::Returned {
                            item: std::ptr::dangling_mut::<u8>().cast(),
                            blob: std::ptr::null_mut(),
                            size: 2561,
                        }))
                    },
                    |_| freed.set(true),
                )
            },
            Err(CredentialError::Unavailable)
        );
        assert!(freed.get());
    }

    #[test]
    fn connection_scope_never_reads_or_reuses_delivery_key() {
        let memory = Memory(Mutex::new(Vec::new()));
        let scoped = ConnectionVault(&memory);
        scoped.put("vault:threadrelay_account", b"fixture").unwrap();
        assert_eq!(
            scoped.get("vault:threadrelay_account").unwrap().expose(),
            &[1]
        );
        scoped.delete("vault:threadrelay_account").unwrap();
        assert!(scoped.contains("vault:threadrelay_account").unwrap());
        assert!(matches!(
            scoped.contains("vault:threadrelay_connection_account"),
            Err(CredentialError::InvalidInput)
        ));
        assert!(matches!(
            scoped.get("vault:threadrelay_connection_account"),
            Err(CredentialError::InvalidInput)
        ));
        assert_eq!(
            *memory.0.lock().unwrap(),
            vec!["vault:threadrelay_connection_account"; 4]
        );
    }
    #[test]
    fn explicit_connection_authorization_keeps_fixed_namespace_and_interactive_intent() {
        let memory = Memory(Mutex::new(Vec::new()));
        let scoped = ConnectionVault(&memory);
        assert_eq!(
            scoped
                .get_interactive("vault:threadrelay_account")
                .unwrap()
                .expose(),
            &[2]
        );
        assert!(matches!(
            scoped.get_interactive("vault:threadrelay_connection_account"),
            Err(CredentialError::InvalidInput)
        ));
        assert_eq!(
            *memory.0.lock().unwrap(),
            vec!["interactive:vault:threadrelay_connection_account"]
        );
    }
}
