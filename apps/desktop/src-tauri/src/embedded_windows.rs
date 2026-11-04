//! Windows-only private storage preparation for the fixed embedded Threads view.
//! This is source support, not Windows/WebView2 or collection acceptance.
//! Existing ACLs are inspected, never repaired. The native view retains the
//! directory lease through close; no path replacement gets a fresh authority.

#[cfg(any(windows, test))]
fn safe_component(value: &[u16]) -> bool {
    !value.is_empty()
        && value != [46]
        && value != [46, 46]
        && !matches!(value.last(), Some(32 | 46))
        && !value
            .iter()
            .any(|c| *c < 32 || [34, 42, 47, 58, 60, 62, 63, 92, 124].contains(c))
}

#[cfg(any(windows, test))]
struct AclPolicy {
    owner_matches: bool,
    protected: bool,
    ace_count: u16,
    ace_type: u8,
    ace_flags: u8,
    mask: u32,
    trustee_matches: bool,
}

#[cfg(any(windows, test))]
impl AclPolicy {
    fn private(&self, inherit_to_children: bool) -> bool {
        self.owner_matches
            && self.protected
            && self.ace_count == 1
            && self.ace_type == 0 // ACCESS_ALLOWED_ACE_TYPE, never callback/object ACE.
            && (self.ace_flags == 3 || (!inherit_to_children && self.ace_flags == 0))
            && self.mask == 0x001f01ff // FILE_ALL_ACCESS, no unrelated principal.
            && self.trustee_matches
    }
}

#[cfg(windows)]
pub(crate) struct PrivateProfile(native::Lease);

#[cfg(windows)]
impl PrivateProfile {
    pub(crate) fn path(&self) -> &std::path::Path {
        &self.0.path
    }
    pub(crate) fn recheck(&self) -> std::io::Result<()> {
        self.0.recheck()
    }
}

#[cfg(windows)]
pub(crate) fn private_profile(app_root: &std::path::Path) -> std::io::Result<PrivateProfile> {
    native::profile(app_root)
        .map(PrivateProfile)
        .map_err(|_| std::io::Error::other("Private embedded profile unavailable"))
}

/// Invoke before the generic supervisor can create the application data root.
/// Only the final app-owned directory is created; its parent must already exist.
/// Existing permissive roots are rejected without rewriting their DACL.
#[cfg(windows)]
pub(crate) fn prepare_app_root(app_root: &std::path::Path) -> std::io::Result<()> {
    native::root(app_root, true)
        .map(|_| ())
        .map_err(|_| std::io::Error::other("Private application storage unavailable"))
}

#[cfg(windows)]
mod native {
    use std::{
        ffi::c_void,
        fs::{File, OpenOptions},
        io,
        mem::{size_of, zeroed},
        os::windows::{
            ffi::OsStrExt,
            fs::{MetadataExt, OpenOptionsExt},
            io::AsRawHandle,
        },
        path::{Component, Path, PathBuf, Prefix},
        ptr::{addr_of_mut, null_mut},
    };
    use windows_sys::Win32::{
        Foundation::{CloseHandle, LocalFree, ERROR_ALREADY_EXISTS, HANDLE},
        Security::{
            Authorization::{
                ConvertStringSecurityDescriptorToSecurityDescriptorW, ConvertStringSidToSidW,
                GetSecurityInfo, SE_FILE_OBJECT,
            },
            EqualSid, GetAce, GetLengthSid, GetSecurityDescriptorControl, GetTokenInformation,
            IsValidAcl, IsValidSecurityDescriptor, IsValidSid, TokenUser, ACCESS_ALLOWED_ACE, ACL,
            DACL_SECURITY_INFORMATION, OWNER_SECURITY_INFORMATION, PSECURITY_DESCRIPTOR, PSID,
            SECURITY_ATTRIBUTES, SE_DACL_PRESENT, SE_DACL_PROTECTED, TOKEN_QUERY, TOKEN_USER,
        },
        Storage::FileSystem::{
            CreateDirectoryW, FILE_ATTRIBUTE_REPARSE_POINT, FILE_FLAG_BACKUP_SEMANTICS,
            FILE_FLAG_OPEN_REPARSE_POINT, FILE_READ_ATTRIBUTES, FILE_SHARE_READ,
        },
        System::Threading::{GetCurrentProcess, OpenProcessToken},
    };

    const READ_CONTROL: u32 = 0x00020000;

    struct Local(*mut c_void);
    impl Drop for Local {
        fn drop(&mut self) {
            if !self.0.is_null() {
                unsafe {
                    LocalFree(self.0);
                }
            }
        }
    }
    struct Handle(HANDLE);
    impl Drop for Handle {
        fn drop(&mut self) {
            unsafe {
                CloseHandle(self.0);
            }
        }
    }
    fn denied() -> io::Error {
        io::Error::other("Private embedded profile unavailable")
    }
    fn wide(value: &std::ffi::OsStr) -> io::Result<Vec<u16>> {
        let mut result: Vec<_> = value.encode_wide().collect();
        if result.contains(&0) {
            return Err(denied());
        }
        result.push(0);
        Ok(result)
    }

    struct Identity {
        // Word storage provides the alignment required by TOKEN_USER.
        token_user: Vec<usize>,
        owner_rights: Local,
    }
    impl Identity {
        fn current() -> io::Result<Self> {
            let mut raw = null_mut();
            if unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut raw) } == 0 {
                return Err(denied());
            }
            let token = Handle(raw);
            let mut length = 0;
            unsafe {
                GetTokenInformation(token.0, TokenUser, null_mut(), 0, &mut length);
            }
            if !(size_of::<TOKEN_USER>()..=65536).contains(&(length as usize)) {
                return Err(denied());
            }
            let mut token_user = vec![0usize; (length as usize).div_ceil(size_of::<usize>())];
            if unsafe {
                GetTokenInformation(
                    token.0,
                    TokenUser,
                    token_user.as_mut_ptr().cast(),
                    length,
                    &mut length,
                )
            } == 0
            {
                return Err(denied());
            }
            let mut owner_rights = Local(null_mut());
            let sid: Vec<u16> = "S-1-3-4\0".encode_utf16().collect();
            if unsafe { ConvertStringSidToSidW(sid.as_ptr(), &mut owner_rights.0) } == 0 {
                return Err(denied());
            }
            let identity = Self {
                token_user,
                owner_rights,
            };
            if unsafe { IsValidSid(identity.sid()) } == 0 {
                return Err(denied());
            }
            Ok(identity)
        }
        fn sid(&self) -> PSID {
            unsafe { (*(self.token_user.as_ptr().cast::<TOKEN_USER>())).User.Sid }
        }
        fn matches(&self, sid: PSID) -> bool {
            !sid.is_null() && unsafe { IsValidSid(sid) != 0 && EqualSid(sid, self.sid()) != 0 }
        }
    }

    fn open_directory(path: &Path) -> io::Result<File> {
        let file = OpenOptions::new()
            .access_mode(FILE_READ_ATTRIBUTES | READ_CONTROL)
            .share_mode(FILE_SHARE_READ)
            .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS)
            .open(path)?;
        let metadata = file.metadata()?;
        if !metadata.is_dir() || metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
            return Err(denied());
        }
        Ok(file)
    }

    fn verify_private(file: &File, identity: &Identity, inherit: bool) -> io::Result<()> {
        let mut owner: PSID = null_mut();
        let mut dacl: *mut ACL = null_mut();
        let mut descriptor = Local(null_mut());
        let result = unsafe {
            GetSecurityInfo(
                file.as_raw_handle(),
                SE_FILE_OBJECT,
                OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
                &mut owner,
                null_mut(),
                &mut dacl,
                null_mut(),
                &mut descriptor.0,
            )
        };
        if result != 0
            || descriptor.0.is_null()
            || dacl.is_null()
            || unsafe { IsValidSecurityDescriptor(descriptor.0) == 0 || IsValidAcl(dacl) == 0 }
        {
            return Err(denied());
        }
        let mut control = 0;
        let mut revision = 0;
        if unsafe { GetSecurityDescriptorControl(descriptor.0, &mut control, &mut revision) } == 0
            || control & SE_DACL_PRESENT == 0
            || unsafe { (*dacl).AceCount } != 1
        {
            return Err(denied());
        }
        let mut raw = null_mut();
        if unsafe { GetAce(dacl, 0, &mut raw) } == 0 || raw.is_null() {
            return Err(denied());
        }
        let ace = raw.cast::<ACCESS_ALLOWED_ACE>();
        let header = unsafe { (*ace).Header };
        if header.AceType != 0
            || !matches!(header.AceFlags, 0 | 3)
            || usize::from(header.AceSize) < size_of::<ACCESS_ALLOWED_ACE>()
        {
            return Err(denied());
        }
        let sid = unsafe { addr_of_mut!((*ace).SidStart).cast() };
        if unsafe { IsValidSid(sid) } == 0
            || unsafe { GetLengthSid(sid) } as usize > usize::from(header.AceSize) - 8
        {
            return Err(denied());
        }
        let policy = super::AclPolicy {
            owner_matches: identity.matches(owner),
            protected: control & SE_DACL_PROTECTED != 0,
            ace_count: unsafe { (*dacl).AceCount },
            ace_type: header.AceType,
            ace_flags: header.AceFlags,
            mask: unsafe { (*ace).Mask },
            trustee_matches: identity.matches(sid)
                || unsafe { EqualSid(sid, identity.owner_rights.0) != 0 },
        };
        if !policy.private(inherit) {
            return Err(denied());
        }
        Ok(())
    }

    fn create_private(path: &Path, identity: &Identity) -> io::Result<File> {
        // OI/CI ensures Chromium-created files/directories inherit owner-only
        // access. P protects this DACL from a permissive parent's inheritance.
        let sddl: Vec<u16> = "D:P(A;OICI;FA;;;OW)\0".encode_utf16().collect();
        let mut descriptor = Local(null_mut());
        if unsafe {
            ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl.as_ptr(),
                1,
                &mut descriptor.0 as *mut PSECURITY_DESCRIPTOR,
                null_mut(),
            )
        } == 0
        {
            return Err(denied());
        }
        let mut attributes: SECURITY_ATTRIBUTES = unsafe { zeroed() };
        attributes.nLength = size_of::<SECURITY_ATTRIBUTES>() as u32;
        attributes.lpSecurityDescriptor = descriptor.0;
        attributes.bInheritHandle = 0;
        let path_wide = wide(path.as_os_str())?;
        if unsafe { CreateDirectoryW(path_wide.as_ptr(), &attributes) } == 0
            && io::Error::last_os_error().raw_os_error() != Some(ERROR_ALREADY_EXISTS as i32)
        {
            return Err(denied());
        }
        // An existing permissive object is never changed, deleted or replaced.
        let file = open_directory(path)?;
        verify_private(&file, identity, true)?;
        Ok(file)
    }

    pub(super) struct Lease {
        pub(super) path: PathBuf,
        guards: Vec<(File, Option<bool>)>,
    }
    impl Lease {
        pub(super) fn recheck(&self) -> io::Result<()> {
            let identity = Identity::current()?;
            for (file, private) in &self.guards {
                let metadata = file.metadata()?;
                if !metadata.is_dir()
                    || metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
                {
                    return Err(denied());
                }
                if let Some(inherit) = private {
                    verify_private(file, &identity, *inherit)?;
                }
            }
            Ok(())
        }
    }

    pub(super) fn root(app_root: &Path, create: bool) -> io::Result<Lease> {
        if !app_root.is_absolute() {
            return Err(denied());
        }
        let mut components = app_root.components();
        if !matches!(components.next(), Some(Component::Prefix(p))
            if matches!(p.kind(), Prefix::Disk(_) | Prefix::VerbatimDisk(_)))
            || components.next() != Some(Component::RootDir)
        {
            return Err(denied()); // No UNC/network/device namespace.
        }
        for part in components {
            match part {
                Component::Normal(name)
                    if super::safe_component(&name.encode_wide().collect::<Vec<_>>()) =>
                {
                    ()
                }
                _ => return Err(denied()),
            }
        }
        let identity = Identity::current()?;
        let mut guards = Vec::new();
        let mut path = PathBuf::new();
        for part in app_root.components() {
            path.push(part);
            if !matches!(part, Component::Prefix(_)) {
                let file = if create && path == app_root {
                    create_private(&path, &identity)?
                } else {
                    open_directory(&path)?
                };
                guards.push((file, None));
            }
        }
        let root = guards.last_mut().ok_or_else(denied)?;
        verify_private(&root.0, &identity, false)?;
        root.1 = Some(false);
        let lease = Lease { path, guards };
        lease.recheck()?;
        Ok(lease)
    }

    pub(super) fn profile(app_root: &Path) -> io::Result<Lease> {
        let identity = Identity::current()?;
        let mut lease = root(app_root, false)?;
        for name in ["embedded-browser", "threads"] {
            lease.path.push(name);
            lease
                .guards
                .push((create_private(&lease.path, &identity)?, Some(true)));
        }
        lease.recheck()?;
        Ok(lease)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn windows_profile_components_reject_aliases_streams_and_traversal() {
        let valid = |s: &str| safe_component(&s.encode_utf16().collect::<Vec<_>>());
        assert!(valid("embedded-browser"));
        assert!(valid("用戶"));
        for value in [
            "",
            ".",
            "..",
            "profile.",
            "profile ",
            "profile:stream",
            "x/y",
            "x\\y",
            "x\0y",
        ] {
            assert!(!valid(value), "{value:?}");
        }
    }
    fn private() -> AclPolicy {
        AclPolicy {
            owner_matches: true,
            protected: true,
            ace_count: 1,
            ace_type: 0,
            ace_flags: 3,
            mask: 0x001f01ff,
            trustee_matches: true,
        }
    }
    #[test]
    fn windows_profile_rejects_foreign_inherited_or_permissive_acl() {
        assert!(private().private(true));
        let mut cases = Vec::new();
        let mut p = private();
        p.owner_matches = false;
        cases.push(p);
        let mut p = private();
        p.protected = false;
        cases.push(p);
        let mut p = private();
        p.ace_count = 2;
        cases.push(p);
        let mut p = private();
        p.ace_type = 5;
        cases.push(p);
        let mut p = private();
        p.ace_flags = 0x13;
        cases.push(p);
        let mut p = private();
        p.mask = 0x10000000;
        cases.push(p);
        let mut p = private();
        p.trustee_matches = false;
        cases.push(p);
        for p in cases {
            assert!(!p.private(true));
        }
    }
    #[test]
    fn profile_requires_child_inheritance_without_mutating_existing_acl() {
        let mut p = private();
        p.ace_flags = 0;
        assert!(p.private(false)); // Existing trusted app root only.
        assert!(!p.private(true)); // Chromium descendants must stay private.
    }
}
