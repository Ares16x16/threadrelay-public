//! Explicit recovery isolates old transport bytes; it does not resolve delivery.
//! Only fixed app-owned directories are selected. No credential is read here.
use crate::{credentials::SecretBytes, local_vault as fs};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    ffi::CString,
    fs::File,
    io::{self, Read, Write},
    os::{fd::AsRawFd, unix::fs::MetadataExt},
    path::Path,
};
const AREA: &str = "telegram-recovery";
const PROFILE_REF: &str = "vault:threadrelay_account";
const DOMAIN: &str = "ThreadRelay/transport-isolation/v1";
unsafe extern "C" {
    fn renameatx_np(
        from: i32,
        name: *const libc::c_char,
        to: i32,
        target: *const libc::c_char,
        flags: u32,
    ) -> i32;
}
#[track_caller]
fn denied<T>() -> io::Result<T> {
    #[cfg(debug_assertions)]
    eprintln!(
        "ThreadRelay profile validation rejected at {}",
        std::panic::Location::caller()
    );
    Err(io::Error::other("Telegram recovery unavailable"))
}
fn adapt<T>(value: Result<T, crate::credentials::CredentialError>) -> io::Result<T> {
    value.map_err(|e| {
        if e == crate::credentials::CredentialError::NotFound {
            io::Error::new(io::ErrorKind::NotFound, "Recovery entry unavailable")
        } else {
            io::Error::other("Private recovery storage unavailable")
        }
    })
}
fn hex(value: &[u8]) -> String {
    value.iter().map(|v| format!("{v:02x}")).collect()
}
fn valid(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|v| v.is_ascii_digit() || (b'a'..=b'f').contains(&v))
}
fn name(value: &str) -> io::Result<CString> {
    CString::new(value).map_err(|_| io::Error::other("Invalid recovery name"))
}
fn open(dir: &File, value: &str, flags: i32, mode: u32) -> io::Result<File> {
    let file = adapt(fs::open_at(dir, value, flags, mode))?;
    // An extant child must be on its trusted parent's current filesystem.
    // Historical st_dev numbers can change when the same volume is remounted.
    if file.metadata()?.dev() != dir.metadata()?.dev() {
        return denied();
    }
    Ok(file)
}
fn private(file: &File, dir: bool) -> io::Result<()> {
    adapt(fs::private(file, dir))
}
fn mkdir(dir: &File, value: &str) -> io::Result<File> {
    let c = name(value)?;
    if unsafe { libc::mkdirat(dir.as_raw_fd(), c.as_ptr(), 0o700) } != 0
        && io::Error::last_os_error().kind() != io::ErrorKind::AlreadyExists
    {
        return Err(io::Error::last_os_error());
    }
    let result = open(dir, value, libc::O_RDONLY | libc::O_DIRECTORY, 0)?;
    private(&result, true)?;
    dir.sync_all()?;
    Ok(result)
}
fn read(dir: &File, value: &str, max: usize) -> io::Result<SecretBytes> {
    let mut f = open(dir, value, libc::O_RDONLY, 0)?;
    private(&f, false)?;
    if f.metadata()?.len() > max as u64 {
        return denied();
    }
    let mut data = SecretBytes::new(Vec::with_capacity(max + 1));
    Read::by_ref(&mut f)
        .take(max as u64 + 1)
        .read_to_end(&mut data.0)?;
    private(&f, false)?;
    if data.expose().len() > max {
        return denied();
    }
    Ok(data)
}
fn entries(dir: &File) -> io::Result<Vec<String>> {
    // fdopendir owns this CLOEXEC duplicate, never a /dev/fd path alias.
    let fd = unsafe { libc::fcntl(dir.as_raw_fd(), libc::F_DUPFD_CLOEXEC, 0) };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    let stream = unsafe { libc::fdopendir(fd) };
    if stream.is_null() {
        unsafe {
            libc::close(fd);
        }
        return Err(io::Error::last_os_error());
    }
    struct Guard(*mut libc::DIR);
    impl Drop for Guard {
        fn drop(&mut self) {
            unsafe {
                libc::closedir(self.0);
            }
        }
    }
    let _guard = Guard(stream);
    unsafe {
        libc::rewinddir(stream);
    }
    let mut result = Vec::new();
    loop {
        unsafe {
            *libc::__error() = 0;
        }
        let entry = unsafe { libc::readdir(stream) };
        if entry.is_null() {
            if unsafe { *libc::__error() } != 0 {
                return Err(io::Error::last_os_error());
            }
            break;
        }
        let value = unsafe { std::ffi::CStr::from_ptr((*entry).d_name.as_ptr()) }
            .to_str()
            .map_err(|_| io::Error::other("Recovery name unavailable"))?;
        if matches!(value, "." | "..") {
            continue;
        }
        if result.len() >= 1000 {
            return denied();
        }
        result.push(value.to_owned());
    }
    Ok(result)
}
fn rename(from: &File, old: &str, to: &File, new: &str, exclusive: bool) -> io::Result<()> {
    let a = name(old)?;
    let b = name(new)?;
    if unsafe {
        renameatx_np(
            from.as_raw_fd(),
            a.as_ptr(),
            to.as_raw_fd(),
            b.as_ptr(),
            if exclusive { libc::RENAME_EXCL } else { 0 },
        )
    } != 0
    {
        return Err(io::Error::last_os_error());
    }
    from.sync_all()?;
    to.sync_all()
}
fn write(dir: &File, target: &str, data: &[u8], exclusive: bool) -> io::Result<()> {
    let mut random = [0u8; 32];
    getrandom::fill(&mut random)
        .map_err(|_| io::Error::other("Recovery randomness unavailable"))?;
    let temporary = format!(".write-{}", hex(&random));
    let mut file = open(
        dir,
        &temporary,
        libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL,
        0o600,
    )?;
    file.write_all(data)?;
    file.sync_all()?;
    private(&file, false)?;
    if !exclusive {
        let old = open(dir, target, libc::O_RDONLY, 0)?;
        private(&old, false)?;
    }
    rename(dir, &temporary, dir, target, exclusive)
}
fn marker(dir: &File) -> io::Result<(String, String)> {
    let data = read(dir, "connection-purpose.json", 4096)?;
    let v: Value = serde_json::from_slice(data.expose())
        .map_err(|_| io::Error::other("Profile marker unavailable"))?;
    let id = v["body"]["profile_id"]
        .as_str()
        .filter(|v| valid(v))
        .ok_or_else(|| io::Error::other("Profile identity unavailable"))?;
    Ok((id.to_owned(), hex(&Sha256::digest(data.expose()))))
}
fn identity(file: &File) -> io::Result<(u64, u64)> {
    let m = file.metadata()?;
    Ok((m.dev(), m.ino()))
}
fn recorded_identity(body: &Value, prefix: &str, volume: &File) -> io::Result<(u64, u64)> {
    // Keep legacy device numbers authenticated/type checked as metadata. Every
    // actual descriptor is checked against its current parent volume by open().
    number(body, &format!("{prefix}_device"))?;
    Ok((
        volume.metadata()?.dev(),
        number(body, &format!("{prefix}_inode"))?,
    ))
}
fn number(v: &Value, key: &str) -> io::Result<u64> {
    v[key]
        .as_u64()
        .ok_or_else(|| io::Error::other("Recovery receipt invalid"))
}
fn text<'a>(v: &'a Value, key: &str) -> io::Result<&'a str> {
    v[key]
        .as_str()
        .ok_or_else(|| io::Error::other("Recovery receipt invalid"))
}
fn load(area: &File, id: &str, key: &SecretBytes) -> io::Result<Value> {
    if !valid(id) {
        return denied();
    }
    let data = read(area, &format!("{id}.json"), 8192)?;
    let envelope: Value = serde_json::from_slice(data.expose())
        .map_err(|_| io::Error::other("Recovery receipt invalid"))?;
    let body = &envelope["body"];
    let bytes = serde_json::to_vec(body)?;
    let expected = hex(&fs::tag(DOMAIN, &bytes, key.expose()));
    let mac = envelope["mac"].as_str().unwrap_or("");
    if mac.len() != expected.len()
        || mac
            .bytes()
            .zip(expected.bytes())
            .fold(0u8, |a, (b, c)| a | (b ^ c))
            != 0
    {
        return denied();
    }
    if body["schema_version"] != 1
        || text(body, "recovery_id")? != id
        || text(body, "archive_id")? != id
        || !valid(text(body, "old_profile_id")?)
        || !valid(text(body, "old_marker_sha256")?)
        || !matches!(text(body, "stage")?, "prepared" | "ready")
    {
        return denied();
    }
    for field in [
        "old_directory_device",
        "old_directory_inode",
        "new_directory_device",
        "new_directory_inode",
    ] {
        number(body, field)?;
    }
    Ok(body.clone())
}
fn save(area: &File, body: &Value, key: &SecretBytes, fresh: bool) -> io::Result<()> {
    let bytes = serde_json::to_vec(body)?;
    let record = json!({"body":body,"mac":hex(&fs::tag(DOMAIN,&bytes,key.expose()))});
    write(
        area,
        &format!("{}.json", text(body, "recovery_id")?),
        &serde_json::to_vec(&record)?,
        fresh,
    )
}
fn reset_records(area: &File, key: &SecretBytes) -> io::Result<Vec<Value>> {
    let mut records = Vec::new();
    for file in entries(area)? {
        let Some(id) = file.strip_prefix("reset-") else {
            continue;
        };
        if !valid(id) {
            return denied();
        }
        let data = read(area, &file, 4096)?;
        let record: Value = serde_json::from_slice(data.expose())?;
        let body = &record["body"];
        let expected = hex(&fs::tag(DOMAIN, &serde_json::to_vec(body)?, key.expose()));
        let mac = record["mac"].as_str().unwrap_or("");
        if mac.len() != expected.len()
            || mac
                .bytes()
                .zip(expected.bytes())
                .fold(0u8, |a, (b, c)| a | (b ^ c))
                != 0
            || body["kind"] != "reseed"
            || body["id"] != id
            || !matches!(text(body, "stage")?, "prepared" | "ready")
        {
            return denied();
        }
        for field in [
            "from_device",
            "from_inode",
            "new_directory_device",
            "new_directory_inode",
        ] {
            number(body, field)?;
        }
        records.push(body.clone());
    }
    Ok(records)
}
fn save_reset(area: &File, body: &Value, key: &SecretBytes, fresh: bool) -> io::Result<()> {
    let bytes = serde_json::to_vec(body)?;
    let record = json!({"body":body,"mac":hex(&fs::tag(DOMAIN,&bytes,key.expose()))});
    write(
        area,
        &format!("reset-{}", text(body, "id")?),
        &serde_json::to_vec(&record)?,
        fresh,
    )
}
fn key(area: &File, create: bool) -> io::Result<SecretBytes> {
    match read(area, ".signing-key", 32) {
        Ok(v) if v.expose().len() == 32 => Ok(v),
        Ok(_) => denied(),
        Err(e) if e.kind() == io::ErrorKind::NotFound && create => {
            for entry in entries(area)? {
                if entry == ".lock" {
                    continue;
                }
                // An interrupted first key write predates all journals/profiles.
                // Keep its bytes untouched; no existing proof may depend on it.
                if !entry.strip_prefix(".write-").is_some_and(valid) {
                    return denied();
                }
                let orphan = open(area, &entry, libc::O_RDONLY, 0)?;
                private(&orphan, false)?;
                if orphan.metadata()?.len() > 32 {
                    return denied();
                }
            }
            let mut v = SecretBytes::new(vec![0; 32]);
            getrandom::fill(&mut v.0)
                .map_err(|_| io::Error::other("Recovery randomness unavailable"))?;
            write(area, ".signing-key", v.expose(), true)?;
            Ok(v)
        }
        Err(e) => Err(e),
    }
}
fn absent(dir: &File, n: &str) -> io::Result<bool> {
    let c = name(n)?;
    let mut s = std::mem::MaybeUninit::<libc::stat>::uninit();
    if unsafe {
        libc::fstatat(
            dir.as_raw_fd(),
            c.as_ptr(),
            s.as_mut_ptr(),
            libc::AT_SYMLINK_NOFOLLOW,
        )
    } == 0
    {
        return Ok(false);
    }
    if io::Error::last_os_error().kind() == io::ErrorKind::NotFound {
        Ok(true)
    } else {
        Err(io::Error::last_os_error())
    }
}
fn validate(area: &File, parent: &File, body: &Value, ready: bool) -> io::Result<()> {
    if ready && body["stage"] != "ready" {
        return denied();
    }
    let id = text(body, "archive_id")?;
    let archived = open(
        area,
        &format!("archived-{id}"),
        libc::O_RDONLY | libc::O_DIRECTORY,
        0,
    )?;
    private(&archived, true)?;
    if identity(&archived)? != recorded_identity(body, "old_directory", &area)?
        || marker(&archived)?
            != (
                text(body, "old_profile_id")?.into(),
                text(body, "old_marker_sha256")?.into(),
            )
    {
        return denied();
    }
    let canonical = hex(&Sha256::digest(PROFILE_REF.as_bytes()));
    let fresh = open(parent, &canonical, libc::O_RDONLY | libc::O_DIRECTORY, 0)?;
    private(&fresh, true)?;
    if identity(&fresh)? != recorded_identity(body, "new_directory", &area)? {
        return denied();
    }
    if !absent(&fresh, "connection-purpose.json")?
        && marker(&fresh)?.0 == text(body, "old_profile_id")?
    {
        return denied();
    }
    Ok(())
}
fn recovery_records(area: &File, key: &SecretBytes) -> io::Result<Vec<Value>> {
    let mut records = Vec::new();
    for file in entries(area)? {
        if let Some(id) = file.strip_suffix(".json") {
            let body = load(area, id, key)?;
            if body["stage"] == "ready" {
                records.push(body);
            }
        }
    }
    Ok(records)
}
fn validate_archive(area: &File, body: &Value) -> io::Result<()> {
    let archived = open(
        area,
        &format!("archived-{}", text(body, "archive_id")?),
        libc::O_RDONLY | libc::O_DIRECTORY,
        0,
    )?;
    private(&archived, true)?;
    if identity(&archived)? != recorded_identity(body, "old_directory", &area)?
        || marker(&archived)?
            != (
                text(body, "old_profile_id")?.into(),
                text(body, "old_marker_sha256")?.into(),
            )
    {
        return denied();
    }
    Ok(())
}
fn chain_tip(
    area: &File,
    first: &Value,
    records: &[Value],
    resets: &[Value],
) -> io::Result<(u64, u64)> {
    if first["stage"] != "ready" {
        return denied();
    }
    validate_archive(area, first)?;
    let mut next = recorded_identity(first, "new_directory", &area)?;
    let mut visited = std::collections::HashSet::new();
    loop {
        if !visited.insert(next) || visited.len() > 1000 {
            return denied();
        }
        let mut successor = None;
        for body in records {
            if recorded_identity(body, "old_directory", &area)? == next {
                validate_archive(area, body)?;
                if successor.is_some() {
                    return denied();
                }
                successor = Some(recorded_identity(body, "new_directory", &area)?);
            }
        }
        for body in resets {
            if body["stage"] == "ready" && recorded_identity(body, "from", &area)? == next {
                if successor.is_some() {
                    return denied();
                }
                successor = Some(recorded_identity(body, "new_directory", &area)?);
            }
        }
        match successor {
            Some(value) => next = value,
            None => return Ok(next),
        }
    }
}
fn validate_chain(area: &File, parent: &File, first: &Value, key: &SecretBytes) -> io::Result<()> {
    let canonical = hex(&Sha256::digest(PROFILE_REF.as_bytes()));
    let current = open(parent, &canonical, libc::O_RDONLY | libc::O_DIRECTORY, 0)?;
    private(&current, true)?;
    let records = recovery_records(area, key)?;
    let resets = reset_records(area, key)?;
    if !absent(&current, "connection-purpose.json")? {
        let profile = marker(&current)?.0;
        if records.iter().any(|body| body["old_profile_id"] == profile) {
            return denied();
        }
    }
    if chain_tip(area, first, &records, &resets)? != identity(&current)? {
        return denied();
    }
    Ok(())
}
fn reseed_removed(area: &File, parent: &File, first: &Value, key: &SecretBytes) -> io::Result<()> {
    let canonical = hex(&Sha256::digest(PROFILE_REF.as_bytes()));
    let records = recovery_records(area, key)?;
    let resets = reset_records(area, key)?;
    let tip = chain_tip(area, first, &records, &resets)?;
    let prepared: Vec<_> = resets
        .iter()
        .filter(|body| body["stage"] == "prepared")
        .collect();
    if !absent(parent, &canonical)? && prepared.is_empty() {
        return Ok(());
    }
    if prepared.len() > 1 {
        return denied();
    }
    let mut body = if let Some(body) = prepared.first() {
        if recorded_identity(body, "from", &area)? != tip {
            return denied();
        }
        (*body).clone()
    } else {
        // The exact canonical path is absent. Never replace an existing session
        // or infer remote logout. This only creates fresh private empty storage.
        let mut random = [0; 32];
        getrandom::fill(&mut random)
            .map_err(|_| io::Error::other("Recovery randomness unavailable"))?;
        let id = hex(&random);
        let fresh = mkdir(area, &format!("reseed-{id}"))?;
        let (device, inode) = identity(&fresh)?;
        let body = json!({"kind":"reseed","id":id,"stage":"prepared","from_device":tip.0,"from_inode":tip.1,
            "new_directory_device":device,"new_directory_inode":inode});
        save_reset(area, &body, key, true)?;
        body
    };
    if absent(parent, &canonical)? {
        rename(
            area,
            &format!("reseed-{}", text(&body, "id")?),
            parent,
            &canonical,
            true,
        )?;
    }
    let current = open(parent, &canonical, libc::O_RDONLY | libc::O_DIRECTORY, 0)?;
    private(&current, true)?;
    if identity(&current)? != recorded_identity(&body, "new_directory", &area)?
        || !entries(&current)?.is_empty()
    {
        return denied();
    }
    body["stage"] = json!("ready");
    save_reset(area, &body, key, false)
}

fn receipt(body: &Value) -> Value {
    json!({"recovery_id":body["recovery_id"],"old_profile_id":body["old_profile_id"],"old_marker_sha256":body["old_marker_sha256"],"archive_id":body["archive_id"],"new_directory_device":body["new_directory_device"],"new_directory_inode":body["new_directory_inode"]})
}
pub(crate) fn quarantine(root: &Path, expected_id: &str, expected_hash: &str) -> io::Result<Value> {
    if !valid(expected_id) || !valid(expected_hash) {
        return denied();
    }
    let root = adapt(fs::root(root))?;
    let area = mkdir(&root, AREA)?;
    let lock = open(&area, ".lock", libc::O_RDWR | libc::O_CREAT, 0o600)?;
    private(&lock, false)?;
    if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
        return denied();
    }
    let key = key(&area, true)?;
    let parent = open(&root, "telegram", libc::O_RDONLY | libc::O_DIRECTORY, 0)?;
    private(&parent, true)?;
    let canonical = hex(&Sha256::digest(PROFILE_REF.as_bytes()));
    let mut body = None;
    for file in entries(&area)? {
        if let Some(id) = file.strip_suffix(".json") {
            let found = load(&area, id, &key)?;
            if found["old_profile_id"] == expected_id && found["old_marker_sha256"] == expected_hash
            {
                if body.is_some() {
                    return denied();
                }
                body = Some(found);
            }
        }
    }
    let mut body = match body {
        Some(v) => v,
        None => {
            let old = open(&parent, &canonical, libc::O_RDONLY | libc::O_DIRECTORY, 0)?;
            private(&old, true)?;
            if marker(&old)? != (expected_id.into(), expected_hash.into()) {
                return denied();
            }
            let (od, oi) = identity(&old)?;
            let mut random = [0; 32];
            getrandom::fill(&mut random)
                .map_err(|_| io::Error::other("Recovery randomness unavailable"))?;
            let id = hex(&random);
            let fresh = mkdir(&area, &format!("fresh-{id}"))?;
            let (nd, ni) = identity(&fresh)?;
            let v = json!({"schema_version":1,"stage":"prepared","recovery_id":id,"archive_id":id,"old_profile_id":expected_id,"old_marker_sha256":expected_hash,"old_directory_device":od,"old_directory_inode":oi,"new_directory_device":nd,"new_directory_inode":ni});
            save(&area, &v, &key, true)?;
            v
        }
    };
    let id = text(&body, "archive_id")?.to_owned();
    if body["stage"] == "prepared" {
        if absent(&area, &format!("archived-{id}"))? {
            let old = open(&parent, &canonical, libc::O_RDONLY | libc::O_DIRECTORY, 0)?;
            private(&old, true)?;
            if identity(&old)? != recorded_identity(&body, "old_directory", &area)?
                || marker(&old)? != (expected_id.into(), expected_hash.into())
            {
                return denied();
            }
            rename(&parent, &canonical, &area, &format!("archived-{id}"), true)?;
        }
        if absent(&parent, &canonical)? {
            rename(&area, &format!("fresh-{id}"), &parent, &canonical, true)?;
        }
        validate(&area, &parent, &body, false)?;
        body["stage"] = json!("ready");
        save(&area, &body, &key, false)?;
    }
    reseed_removed(&area, &parent, &body, &key)?;
    validate_chain(&area, &parent, &body, &key)?;
    Ok(receipt(&body))
}
pub(crate) fn verify(root: &Path, id: &str) -> io::Result<Value> {
    if !valid(id) {
        return denied();
    }
    let root = adapt(fs::root(root))?;
    let area = open(&root, AREA, libc::O_RDONLY | libc::O_DIRECTORY, 0)?;
    private(&area, true)?;
    let key = key(&area, false)?;
    let parent = open(&root, "telegram", libc::O_RDONLY | libc::O_DIRECTORY, 0)?;
    private(&parent, true)?;
    let body = load(&area, id, &key)?;
    validate_chain(&area, &parent, &body, &key)?;
    Ok(receipt(&body))
}

/// Explicit Begin-login preparation. Passive status/restore never repairs a
/// missing profile. Original archived attempts and receipts remain unchanged.
pub(crate) fn prepare_for_login(root_path: &Path) -> io::Result<()> {
    let root = adapt(fs::root(root_path))?;
    if absent(&root, AREA)? {
        return Ok(());
    }
    let area = open(&root, AREA, libc::O_RDONLY | libc::O_DIRECTORY, 0)?;
    private(&area, true)?;
    let key = key(&area, false)?;
    let records = recovery_records(&area, &key)?;
    for body in records {
        quarantine(
            root_path,
            text(&body, "old_profile_id")?,
            text(&body, "old_marker_sha256")?,
        )?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::{symlink, DirBuilderExt, OpenOptionsExt, PermissionsExt};
    struct Fixture {
        root: std::path::PathBuf,
        old: std::path::PathBuf,
        marker: Vec<u8>,
    }
    impl Fixture {
        fn new() -> Self {
            let mut random = [0; 32];
            getrandom::fill(&mut random).unwrap();
            let root =
                std::env::temp_dir().join(format!("threadrelay-quarantine-{}", hex(&random)));
            std::fs::DirBuilder::new()
                .mode(0o700)
                .create(&root)
                .unwrap();
            let parent = root.join("telegram");
            std::fs::DirBuilder::new()
                .mode(0o700)
                .create(&parent)
                .unwrap();
            let old = parent.join(hex(&Sha256::digest(PROFILE_REF.as_bytes())));
            std::fs::DirBuilder::new().mode(0o700).create(&old).unwrap();
            let marker = serde_json::to_vec(
                &json!({"body":{"profile_id":"a".repeat(64)},"mac":"synthetic-marker"}),
            )
            .unwrap();
            for (file, data) in [
                ("connection-purpose.json", marker.as_slice()),
                ("td.binlog", b"synthetic-transport".as_slice()),
            ] {
                let mut f = std::fs::OpenOptions::new()
                    .write(true)
                    .create_new(true)
                    .mode(0o600)
                    .open(old.join(file))
                    .unwrap();
                f.write_all(data).unwrap();
            }
            Self { root, old, marker }
        }
        fn begin(&self) -> Value {
            quarantine(
                &self.root,
                &"a".repeat(64),
                &hex(&Sha256::digest(&self.marker)),
            )
            .unwrap()
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            std::fs::remove_dir_all(&self.root).unwrap();
        }
    }
    #[test]
    fn isolates_transport_without_credentials_or_delivery_claims_and_verifies_after_reopen() {
        let f = Fixture::new();
        let old = std::fs::metadata(&f.old).unwrap();
        let result = f.begin();
        let archived = f.root.join(AREA).join(format!(
            "archived-{}",
            result["archive_id"].as_str().unwrap()
        ));
        assert_eq!(
            std::fs::read(archived.join("td.binlog")).unwrap(),
            b"synthetic-transport"
        );
        assert!(!f.old.join("td.binlog").exists());
        assert_ne!(std::fs::metadata(&f.old).unwrap().ino(), old.ino());
        assert_eq!(
            verify(&f.root, result["recovery_id"].as_str().unwrap()).unwrap(),
            result
        );
        assert!(!f.root.join("remembered-credentials").exists());
        assert_eq!(
            f.begin(),
            result,
            "lost return resumes same durable recovery"
        );
    }
    #[test]
    fn wrong_marker_or_profile_cannot_move_existing_transport() {
        let f = Fixture::new();
        assert!(quarantine(&f.root, &"b".repeat(64), &hex(&Sha256::digest(&f.marker))).is_err());
        assert!(quarantine(&f.root, &"a".repeat(64), &"b".repeat(64)).is_err());
        assert!(f.old.join("td.binlog").exists());
    }
    #[test]
    fn edited_receipt_or_restored_old_marker_cannot_release_uncertainty() {
        let f = Fixture::new();
        let result = f.begin();
        let id = result["recovery_id"].as_str().unwrap();
        let receipt = f.root.join(AREA).join(format!("{id}.json"));
        let before = std::fs::read(&receipt).unwrap();
        let mut changed: Value = serde_json::from_slice(&before).unwrap();
        changed["body"]["new_directory_inode"] = json!(1);
        std::fs::write(&receipt, serde_json::to_vec(&changed).unwrap()).unwrap();
        assert!(verify(&f.root, id).is_err());
        std::fs::write(&receipt, before).unwrap();
        let mut marker = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(f.old.join("connection-purpose.json"))
            .unwrap();
        marker.write_all(&f.marker).unwrap();
        assert!(verify(&f.root, id).is_err());
    }
    #[test]
    fn links_and_nonprivate_files_are_rejected_without_following_them() {
        let f = Fixture::new();
        let marker = f.old.join("connection-purpose.json");
        let outside = f.root.join("outside");
        std::fs::rename(&marker, &outside).unwrap();
        symlink(&outside, &marker).unwrap();
        assert!(quarantine(&f.root, &"a".repeat(64), &hex(&Sha256::digest(&f.marker))).is_err());
        assert!(f.old.join("td.binlog").exists());
        std::fs::remove_file(marker).unwrap();
        std::fs::rename(outside, f.old.join("connection-purpose.json")).unwrap();
        std::fs::set_permissions(&f.old, std::fs::Permissions::from_mode(0o755)).unwrap();
        assert!(quarantine(&f.root, &"a".repeat(64), &hex(&Sha256::digest(&f.marker))).is_err());
    }
    #[test]
    fn prepared_move_resumes_after_archive_was_durable_but_new_folder_was_not_installed() {
        let f = Fixture::new();
        let result = f.begin();
        let id = result["recovery_id"].as_str().unwrap();
        let root = adapt(fs::root(&f.root)).unwrap();
        let area = open(&root, AREA, libc::O_RDONLY | libc::O_DIRECTORY, 0).unwrap();
        let key = key(&area, false).unwrap();
        let mut body = load(&area, id, &key).unwrap();
        body["stage"] = json!("prepared");
        save(&area, &body, &key, false).unwrap();
        let parent = open(&root, "telegram", libc::O_RDONLY | libc::O_DIRECTORY, 0).unwrap();
        let canonical = hex(&Sha256::digest(PROFILE_REF.as_bytes()));
        rename(&parent, &canonical, &area, &format!("fresh-{id}"), true).unwrap();
        assert!(verify(&f.root, id).is_err());
        assert_eq!(f.begin(), result);
    }
    #[test]
    fn successive_recoveries_verify_the_full_signed_chain_and_reject_old_profiles() {
        let f = Fixture::new();
        let first = f.begin();
        let next = serde_json::to_vec(
            &json!({"body":{"profile_id":"b".repeat(64)},"mac":"synthetic-second"}),
        )
        .unwrap();
        let mut file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(f.old.join("connection-purpose.json"))
            .unwrap();
        file.write_all(&next).unwrap();
        drop(file);
        let second = quarantine(&f.root, &"b".repeat(64), &hex(&Sha256::digest(&next))).unwrap();
        assert_eq!(
            verify(&f.root, first["recovery_id"].as_str().unwrap()).unwrap(),
            first
        );
        assert_eq!(
            verify(&f.root, second["recovery_id"].as_str().unwrap()).unwrap(),
            second
        );
        for contents in [&f.marker, &next] {
            let target = f.old.join("connection-purpose.json");
            let mut file = std::fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(&target)
                .unwrap();
            file.write_all(contents).unwrap();
            drop(file);
            assert!(verify(&f.root, first["recovery_id"].as_str().unwrap()).is_err());
            assert!(verify(&f.root, second["recovery_id"].as_str().unwrap()).is_err());
            std::fs::remove_file(target).unwrap();
        }
    }
    #[test]
    fn interrupted_first_key_installation_does_not_strand_recovery() {
        let f = Fixture::new();
        let area = f.root.join(AREA);
        std::fs::DirBuilder::new()
            .mode(0o700)
            .create(&area)
            .unwrap();
        let mut orphan = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(area.join(format!(".write-{}", "d".repeat(64))))
            .unwrap();
        orphan.write_all(b"partial").unwrap();
        drop(orphan);
        let result = f.begin();
        assert!(verify(&f.root, result["recovery_id"].as_str().unwrap()).is_ok());
        assert_eq!(
            std::fs::read(area.join(format!(".write-{}", "d".repeat(64)))).unwrap(),
            b"partial"
        );
    }
    #[test]
    fn explicit_reconnect_recreates_removed_profile_without_reviving_the_archived_session() {
        let f = Fixture::new();
        let first = f.begin();
        let id = first["recovery_id"].as_str().unwrap();
        std::fs::remove_dir_all(&f.old).unwrap();
        assert!(verify(&f.root, id).is_err());
        assert!(
            !f.old.exists(),
            "status verification must never repair storage"
        );
        assert_eq!(f.begin(), first);
        assert_eq!(verify(&f.root, id).unwrap(), first);
        assert!(std::fs::read_dir(&f.old).unwrap().next().is_none());
        assert_eq!(
            std::fs::read(f.root.join(AREA).join(format!("archived-{id}/td.binlog"))).unwrap(),
            b"synthetic-transport"
        );
        std::fs::remove_dir_all(&f.old).unwrap();
        assert_eq!(f.begin(), first, "a later local removal must also recover");
        assert_eq!(verify(&f.root, id).unwrap(), first);
    }
    #[test]
    fn different_current_filesystem_is_rejected_even_for_a_directory() {
        let root = File::open("/").unwrap();
        // macOS /dev is a different filesystem; no app or credential data used.
        let dev = File::open("/dev").unwrap();
        assert_ne!(
            root.metadata().unwrap().dev(),
            dev.metadata().unwrap().dev()
        );
        assert!(open(&root, "dev", libc::O_RDONLY | libc::O_DIRECTORY, 0).is_err());
    }
    #[test]
    fn recorded_mount_device_number_can_change_without_invalidating_the_archive() {
        let f = Fixture::new();
        let first = f.begin();
        let id = first["recovery_id"].as_str().unwrap();
        let root = adapt(fs::root(&f.root)).unwrap();
        let area = open(&root, AREA, libc::O_RDONLY | libc::O_DIRECTORY, 0).unwrap();
        let key = key(&area, false).unwrap();
        let mut body = load(&area, id, &key).unwrap();
        // Simulate a journal signed on an earlier mount. Inodes, profile bytes,
        // archive placement and the signing key remain exactly the same.
        body["old_directory_device"] = json!(number(&body, "old_directory_device").unwrap() + 100);
        body["new_directory_device"] = json!(number(&body, "new_directory_device").unwrap() + 100);
        save(&area, &body, &key, false).unwrap();
        let expected = receipt(&body);
        assert_eq!(verify(&f.root, id).unwrap(), expected);
        prepare_for_login(&f.root).unwrap();
        assert_eq!(verify(&f.root, id).unwrap(), expected);
    }
    #[test]
    fn populated_fresh_profile_can_reconnect_after_a_completed_reseed() {
        let f = Fixture::new();
        let first = f.begin();
        std::fs::remove_dir_all(&f.old).unwrap();
        prepare_for_login(&f.root).unwrap();
        let mut marker = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(f.old.join("connection-purpose.json"))
            .unwrap();
        marker
            .write_all(
                serde_json::to_vec(
                    &json!({"body":{"profile_id":"c".repeat(64)},"mac":"synthetic"}),
                )
                .unwrap()
                .as_slice(),
            )
            .unwrap();
        drop(marker);
        prepare_for_login(&f.root).unwrap();
        assert_eq!(
            verify(&f.root, first["recovery_id"].as_str().unwrap()).unwrap(),
            first
        );
    }
    #[test]
    fn explicit_login_resumes_prepared_reseed_and_rejects_modified_reset_proof() {
        let f = Fixture::new();
        let first = f.begin();
        let id = first["recovery_id"].as_str().unwrap();
        std::fs::remove_dir_all(&f.old).unwrap();
        prepare_for_login(&f.root).unwrap();
        let root = adapt(fs::root(&f.root)).unwrap();
        let area = open(&root, AREA, libc::O_RDONLY | libc::O_DIRECTORY, 0).unwrap();
        let parent = open(&root, "telegram", libc::O_RDONLY | libc::O_DIRECTORY, 0).unwrap();
        let key = key(&area, false).unwrap();
        let mut reset = reset_records(&area, &key).unwrap().pop().unwrap();
        reset["stage"] = json!("prepared");
        save_reset(&area, &reset, &key, false).unwrap();
        let canonical = hex(&Sha256::digest(PROFILE_REF.as_bytes()));
        rename(
            &parent,
            &canonical,
            &area,
            &format!("reseed-{}", reset["id"].as_str().unwrap()),
            true,
        )
        .unwrap();
        assert!(verify(&f.root, id).is_err());
        prepare_for_login(&f.root).unwrap();
        assert_eq!(verify(&f.root, id).unwrap(), first);
        let path = f
            .root
            .join(AREA)
            .join(format!("reset-{}", reset["id"].as_str().unwrap()));
        let mut changed: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        changed["body"]["from_inode"] = json!(1);
        std::fs::write(path, serde_json::to_vec(&changed).unwrap()).unwrap();
        assert!(verify(&f.root, id).is_err());
        assert!(prepare_for_login(&f.root).is_err());
    }
    #[test]
    fn missing_proof_key_is_not_replaced_over_existing_receipts() {
        let f = Fixture::new();
        let result = f.begin();
        std::fs::remove_file(f.root.join(AREA).join(".signing-key")).unwrap();
        assert!(verify(&f.root, result["recovery_id"].as_str().unwrap()).is_err());
        assert!(quarantine(&f.root, &"a".repeat(64), &hex(&Sha256::digest(&f.marker))).is_err());
        assert!(!f.root.join(AREA).join(".signing-key").exists());
    }
}
