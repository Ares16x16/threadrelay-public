//! Explicit debug-build connection capability for the selected local TDLib.
//! This is not native acceptance, delivery authorization, or a release receipt.
//! The only source paths and hashes are compiled here; callers supply only the
//! already private app root owned by the desktop supervisor.
use crate::{
    bundle_inventory::{Entry, Inventory},
    native_broker::{NativeProof, PreparedInference},
    supervisor::{DesktopSupervisor, Handle},
    verified_artifacts::GuardedResources,
};
use std::{
    fs::OpenOptions,
    io::{self, Read},
    os::unix::fs::OpenOptionsExt,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
};

#[path = "connected_local_inference.rs"]
pub(crate) mod inference;

const LIBRARY: &str = ".cache/tdlib-native/build-02/install/lib/libtdjson.1.8.67.dylib";
const SCHEMA: &str = ".cache/tdlib-source/td/generate/scheme/td_api.tl";
const LIBRARY_SHA256: &str = "c0c31e8cc76c9f7b67f98dbe0a5e75c4083149ccafa670ba943552b53a1eafed";
const SCHEMA_SHA256: &str = "98b21543066e0124623b7d36f6496ef38cf86e9f9825aedcdd8e2527633fee4c";
const SYSTEM_IMPORTS: [&str; 2] = ["/usr/lib/libc++.1.dylib", "/usr/lib/libSystem.B.dylib"];
const MAX_LOAD_COMMAND_BYTES: usize = 65_536;

fn unavailable() -> io::Error {
    io::Error::other("Selected local TDLib connection capability unavailable")
}

pub(crate) fn source_root() -> io::Result<&'static Path> {
    // CARGO_MANIFEST_DIR is supplied at compile time, never by the renderer,
    // command line, runtime environment, or an artifact selection dialog.
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(3)
        .filter(|path| path.is_absolute())
        .ok_or_else(unavailable)
}

pub(crate) fn inventory() -> io::Result<Inventory> {
    Inventory::new(vec![
        Entry::File {
            path: LIBRARY.into(),
            sha256: LIBRARY_SHA256.into(),
            size: 28_177_800,
            executable: true,
        },
        Entry::File {
            path: SCHEMA.into(),
            sha256: SCHEMA_SHA256.into(),
            size: 1_152_504,
            executable: false,
        },
    ])
}

/// Prepare before spawning the connected worker, and retain the sealed guard
/// under that worker's eventual process manager. Cancellation after retention
/// never drops or unseals a generation that the supervisor must still clean.
pub(crate) fn prepare(
    owner: &mut DesktopSupervisor,
    app_root: &Path,
    cancelled: &dyn Fn() -> bool,
) -> io::Result<GuardedResources> {
    let known = owner.artifact_roots();
    let (resources, guard) =
        crate::verified_artifacts::prepare_connected_tdlib(app_root, cancelled, &known)?;
    owner.retain_artifacts(guard);
    if !owner.retains_stable_artifacts(&resources, cancelled)
        || verify_loader(&resources.root().join(LIBRARY)).is_err()
        || cancelled()
    {
        return Err(unavailable());
    }
    Ok(resources)
}

pub(crate) fn prepare_inference(
    owner: &mut DesktopSupervisor,
    app_root: &Path,
    cancelled: &dyn Fn() -> bool,
) -> io::Result<GuardedResources> {
    inference::prepare(owner, app_root, cancelled)
}

fn word(bytes: &[u8], offset: usize) -> io::Result<u32> {
    let end = offset.checked_add(4).ok_or_else(unavailable)?;
    let value = bytes.get(offset..end).ok_or_else(unavailable)?;
    Ok(u32::from_le_bytes(
        value.try_into().map_err(|_| unavailable())?,
    ))
}

fn header(bytes: &[u8]) -> io::Result<(usize, usize)> {
    if bytes.len() < 32
        || word(bytes, 0)? != 0xfeed_facf // thin little-endian Mach-O 64
        || word(bytes, 4)? != 0x0100_000c // arm64
        || word(bytes, 12)? != 6
    // MH_DYLIB
    {
        return Err(unavailable());
    }
    let commands = word(bytes, 16)? as usize;
    let size = word(bytes, 20)? as usize;
    if commands == 0 || commands > 128 || size > MAX_LOAD_COMMAND_BYTES || size < commands * 8 {
        return Err(unavailable());
    }
    Ok((commands, size))
}

fn dylib_name(command: &[u8]) -> io::Result<&str> {
    if command.len() < 24 {
        return Err(unavailable());
    }
    let offset = word(command, 8)? as usize;
    if offset < 24 {
        return Err(unavailable());
    }
    let tail = command.get(offset..).ok_or_else(unavailable)?;
    let nul = tail
        .iter()
        .position(|byte| *byte == 0)
        .ok_or_else(unavailable)?;
    if tail[nul..].iter().any(|byte| *byte != 0) {
        return Err(unavailable());
    }
    std::str::from_utf8(&tail[..nul]).map_err(|_| unavailable())
}

/// Audit the complete loader command table from the exact pinned snapshot.
/// Only the two absolute macOS system import names are permitted. Their code
/// belongs to the OS/dyld shared cache; no project or ambient loader dependency
/// is allowed, and no release acceptance claim is inferred from this check.
fn loader_commands(bytes: &[u8]) -> io::Result<()> {
    let (count, size) = header(bytes)?;
    if bytes.len() != 32 + size {
        return Err(unavailable());
    }
    let mut imports = std::collections::BTreeSet::new();
    let mut identities = 0;
    let mut offset = 32usize;
    for _ in 0..count {
        let kind = word(bytes, offset)?;
        let length = word(bytes, offset.checked_add(4).ok_or_else(unavailable)?)? as usize;
        if length < 8 || length % 8 != 0 {
            return Err(unavailable());
        }
        let end = offset.checked_add(length).ok_or_else(unavailable)?;
        let command = bytes.get(offset..end).ok_or_else(unavailable)?;
        match kind {
            0x0c => {
                let name = dylib_name(command)?;
                if !SYSTEM_IMPORTS.contains(&name) || !imports.insert(name) {
                    return Err(unavailable());
                }
            }
            0x0d => {
                identities += 1;
                if identities != 1 || dylib_name(command)? != "libtdjson.1.8.67.dylib" {
                    return Err(unavailable());
                }
            }
            // Exact non-loader command types present in the pinned build-02.
            // RPATH, weak/reexport/upward/lazy loads, dylinkers, injected
            // environments, and every unrecognized command fail closed.
            0x19 | 0x8000_0034 | 0x8000_0033 | 0x02 | 0x0b | 0x1b | 0x32 | 0x2a | 0x26 | 0x29
            | 0x1d => (),
            _ => return Err(unavailable()),
        }
        offset = end;
    }
    if offset != bytes.len() || identities != 1 || imports.len() != SYSTEM_IMPORTS.len() {
        return Err(unavailable());
    }
    Ok(())
}

fn verify_loader(path: &Path) -> io::Result<()> {
    let mut file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)?;
    if !file.metadata()?.is_file() {
        return Err(unavailable());
    }
    let mut bytes = vec![0u8; 32];
    file.read_exact(&mut bytes)?;
    let (_, size) = header(&bytes)?;
    bytes.resize(32 + size, 0);
    file.read_exact(&mut bytes[32..])?;
    loader_commands(&bytes)
}

/// The host creates this only for its explicit connected-local debug worker.
/// TDLib selection permits account authorization only. Inference uses its own
/// independently pinned guard and direct observations. Telegram delivery and
/// the legacy Chrome broker remain denied.
pub(crate) struct ConnectedLocalProof {
    owner: Arc<Mutex<DesktopSupervisor>>,
    parent: Arc<Handle>,
    guarded: GuardedResources,
    selected: bool,
    inference: Option<inference::LocalInference>,
}
impl ConnectedLocalProof {
    pub(crate) fn new(
        owner: Arc<Mutex<DesktopSupervisor>>,
        parent: Arc<Handle>,
        guarded: GuardedResources,
    ) -> Self {
        // Hash the selected closure once at construction. Every later query
        // checks the original retained inode identities, ACLs, modes and
        // immutable flags, keeping frequent host capability queries bounded.
        let selected = inventory().is_ok_and(|pins| {
            crate::verified_artifacts::verify_bundle(guarded.root(), &pins, &|| false).is_ok()
                && verify_loader(&guarded.root().join(LIBRARY)).is_ok()
        });
        Self {
            owner,
            parent,
            guarded,
            selected,
            inference: None,
        }
    }

    pub(crate) fn new_with_inference(
        owner: Arc<Mutex<DesktopSupervisor>>,
        parent: Arc<Handle>,
        guarded: GuardedResources,
        inference: Option<GuardedResources>,
    ) -> Self {
        let app_root = guarded.app_root().to_owned();
        let mut proof = Self::new(owner.clone(), parent.clone(), guarded);
        proof.inference = inference
            .filter(|resources| resources.app_root() == app_root)
            .map(|resources| inference::LocalInference::new(owner, parent, resources));
        proof
    }

    fn parent_live(&self) -> bool {
        self.owner
            .try_lock()
            .ok()
            .is_some_and(|mut owner| owner.processes.is_live(&self.parent).unwrap_or(false))
    }

    fn authorization_live(&self, cancelled: &dyn Fn() -> bool) -> bool {
        if !self.selected || cancelled() {
            return false;
        }
        let mut owner = match self.owner.lock() {
            Ok(owner) => owner,
            Err(_) => return false,
        };
        owner.retains_stable_artifacts(&self.guarded, cancelled)
            && owner.processes.is_live(&self.parent).unwrap_or(false)
            && !cancelled()
    }
}
impl NativeProof for ConnectedLocalProof {
    fn embedded_ready(&self) -> bool {
        // Current host availability, visible view, private store and worker
        // generation are separately enforced by EmbeddedAuthority/NativeBroker.
        cfg!(feature = "desktop") && self.parent_live()
    }
    fn tdlib_auth_ready(&self) -> bool {
        self.authorization_live(&|| false)
    }
    fn tdlib_connection_only(&self) -> bool {
        true
    }
    fn prepare_tdlib_auth(&self, cancelled: &dyn Fn() -> bool) -> Option<PathBuf> {
        self.authorization_live(cancelled)
            .then(|| self.guarded.root().join(LIBRARY))
    }
    fn tdlib_ready(&self) -> bool {
        false
    }
    fn prepare_tdlib(&self, _cancelled: &dyn Fn() -> bool) -> Option<PathBuf> {
        None
    }
    fn inference_ready(&self) -> bool {
        self.inference
            .as_ref()
            .is_some_and(inference::LocalInference::ready)
    }
    fn authorize_inference(
        &self,
        prepared: &mut PreparedInference,
        cancelled: &dyn Fn() -> bool,
    ) -> bool {
        self.inference
            .as_ref()
            .is_some_and(|inference| inference.authorize(prepared, cancelled))
    }
    fn browser_ready(&self) -> bool {
        false
    }
    fn authorize_browser(
        &self,
        _: &Path,
        _: &Path,
        _: &Path,
        _: &Path,
        _: &dyn Fn() -> bool,
    ) -> bool {
        false
    }
    fn driver_contained(&self, _: &Handle) -> bool {
        false
    }
    fn endpoint_owned(&self, handle: &Handle, port: u16) -> bool {
        self.inference
            .as_ref()
            .is_some_and(|inference| inference.endpoint_owned(handle, port))
    }
    fn within_resource_limits(&self, handle: &Handle) -> bool {
        self.inference
            .as_ref()
            .is_some_and(|inference| inference.within_resource_limits(handle))
    }
    fn inference_peer(&self, worker: &Handle, runtime: &Handle, client: u16, server: u16) -> bool {
        self.inference
            .as_ref()
            .is_some_and(|inference| inference.peer(worker, runtime, client, server))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    fn private_test_root(name: &str) -> PathBuf {
        let root = Path::new("/private/tmp").join(format!(
            "threadrelay-connected-{name}-{}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir(&root).unwrap();
        std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o700)).unwrap();
        root
    }

    fn command(kind: u32, name: &str) -> Vec<u8> {
        let length = (24 + name.len() + 1).div_ceil(8) * 8;
        let mut bytes = vec![0u8; length];
        bytes[..4].copy_from_slice(&kind.to_le_bytes());
        bytes[4..8].copy_from_slice(&(length as u32).to_le_bytes());
        bytes[8..12].copy_from_slice(&24u32.to_le_bytes());
        bytes[24..24 + name.len()].copy_from_slice(name.as_bytes());
        bytes
    }

    fn macho(commands: &[Vec<u8>]) -> Vec<u8> {
        let mut bytes = vec![0u8; 32];
        bytes[..4].copy_from_slice(&0xfeed_facfu32.to_le_bytes());
        bytes[4..8].copy_from_slice(&0x0100_000cu32.to_le_bytes());
        bytes[12..16].copy_from_slice(&6u32.to_le_bytes());
        bytes[16..20].copy_from_slice(&(commands.len() as u32).to_le_bytes());
        bytes[20..24]
            .copy_from_slice(&(commands.iter().map(Vec::len).sum::<usize>() as u32).to_le_bytes());
        for command in commands {
            bytes.extend_from_slice(command);
        }
        bytes
    }

    fn selected_commands() -> Vec<Vec<u8>> {
        vec![
            command(0x0d, "libtdjson.1.8.67.dylib"),
            command(0x0c, SYSTEM_IMPORTS[0]),
            command(0x0c, SYSTEM_IMPORTS[1]),
        ]
    }

    #[test]
    fn loader_closure_rejects_external_weak_rpath_missing_and_duplicate_loads() {
        let selected = selected_commands();
        assert!(loader_commands(&macho(&selected)).is_ok());
        for extra in [
            command(0x0c, "/opt/homebrew/lib/libssl.dylib"),
            command(0x0c, "@rpath/libssl.dylib"),
            command(0x8000_0018, SYSTEM_IMPORTS[0]),
            command(0x8000_001f, SYSTEM_IMPORTS[0]),
            command(0x8000_0023, SYSTEM_IMPORTS[0]),
            command(0x8000_001c, "/private/other"),
            command(0x0c, SYSTEM_IMPORTS[0]),
        ] {
            let mut changed = selected.clone();
            changed.push(extra);
            assert!(loader_commands(&macho(&changed)).is_err());
        }
        assert!(loader_commands(&macho(&selected[..2])).is_err());
        assert!(loader_commands(&macho(&selected[1..])).is_err());
    }

    #[test]
    fn malformed_or_foreign_loader_tables_fail_closed() {
        let valid = macho(&selected_commands());
        for (offset, value) in [
            (0, 0xcafe_babeu32),
            (4, 0x0100_0007),
            (12, 2),
            (16, 0),
            (20, MAX_LOAD_COMMAND_BYTES as u32 + 8),
            (36, 0),
            (36, 7),
            (40, 4),
        ] {
            let mut changed = valid.clone();
            changed[offset..offset + 4].copy_from_slice(&value.to_le_bytes());
            assert!(loader_commands(&changed).is_err(), "offset {offset}");
        }
        for length in [0, 31, valid.len() - 1] {
            assert!(loader_commands(&valid[..length]).is_err());
        }
        let mut trailing = valid;
        trailing.push(0);
        assert!(loader_commands(&trailing).is_err());
    }

    #[test]
    fn compiled_selection_is_two_exact_files_with_no_evidence_or_delivery_receipt() {
        let selected = inventory().unwrap();
        let files: Vec<_> = selected
            .entries
            .values()
            .filter_map(|entry| match entry {
                Entry::File { path, .. } => Some(path.as_str()),
                _ => None,
            })
            .collect();
        assert_eq!(files, vec![LIBRARY, SCHEMA]);
        assert!(selected
            .entries
            .values()
            .all(|entry| !matches!(entry, Entry::Symlink { .. })));
        assert_eq!(
            source_root().unwrap().join("apps/desktop/src-tauri"),
            Path::new(env!("CARGO_MANIFEST_DIR"))
        );
    }

    #[test]
    fn cancelled_or_nonprivate_app_root_does_not_publish_a_snapshot() {
        let app = private_test_root("cancel");
        let mut owner = DesktopSupervisor::acquire(&app.join("profile")).unwrap();
        assert!(prepare(&mut owner, &app, &|| true).is_err());
        assert!(owner.artifact_roots().is_empty());
        let staging = app.join("execution-staging");
        assert!(std::fs::read_dir(&staging).unwrap().next().is_none());
        std::fs::set_permissions(&app, std::fs::Permissions::from_mode(0o755)).unwrap();
        assert!(prepare(&mut owner, &app, &|| false).is_err());
        assert!(owner.artifact_roots().is_empty());
        std::fs::set_permissions(&app, std::fs::Permissions::from_mode(0o700)).unwrap();
        assert!(owner.close_generation(100).stopped);
        drop(owner);
        std::fs::remove_dir_all(app).unwrap();
    }

    #[test]
    fn selected_inventory_rejects_changed_hash_missing_and_extra_snapshot_files() {
        use sha2::{Digest, Sha256};
        let root = private_test_root("pins");
        for name in [LIBRARY, SCHEMA] {
            let path = root.join(name);
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            std::fs::write(path, b"synthetic").unwrap();
        }
        std::fs::set_permissions(root.join(LIBRARY), std::fs::Permissions::from_mode(0o500))
            .unwrap();
        let small = || {
            Inventory::new(
                [LIBRARY, SCHEMA]
                    .into_iter()
                    .map(|name| Entry::File {
                        path: name.into(),
                        sha256: format!("{:x}", Sha256::digest(b"synthetic")),
                        size: 9,
                        executable: name == LIBRARY,
                    })
                    .collect(),
            )
            .unwrap()
        };
        assert!(crate::verified_artifacts::verify_bundle(&root, &small(), &|| false).is_ok());
        assert!(
            crate::verified_artifacts::verify_bundle(&root, &inventory().unwrap(), &|| false)
                .is_err()
        );
        std::fs::write(root.join(SCHEMA), b"different").unwrap();
        assert!(crate::verified_artifacts::verify_bundle(&root, &small(), &|| false).is_err());
        std::fs::remove_file(root.join(SCHEMA)).unwrap();
        assert!(crate::verified_artifacts::verify_bundle(&root, &small(), &|| false).is_err());
        std::fs::write(root.join(SCHEMA), b"synthetic").unwrap();
        std::fs::write(root.join("extra.dylib"), b"synthetic").unwrap();
        assert!(crate::verified_artifacts::verify_bundle(&root, &small(), &|| false).is_err());
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    #[ignore = "Opt-in exact local build-02 snapshot and same-manager worker smoke; no account or send"]
    fn selected_native_snapshot_grants_auth_only_and_expires_on_supervisor_close() {
        use crate::supervisor::{ProcessKind, SpawnSpec};
        let app = private_test_root("native-auth");
        let mut owner = DesktopSupervisor::acquire(&app.join("profile")).unwrap();
        let resources = prepare(&mut owner, &app, &|| false).unwrap();
        assert_ne!(resources.root(), source_root().unwrap());
        let snapshot = resources.root().to_owned();
        let parent = Arc::new(
            owner
                .processes
                .spawn(
                    ProcessKind::Worker,
                    &SpawnSpec::guarded_fixture("/bin/sleep", ["30"]),
                )
                .unwrap(),
        );
        let owner = Arc::new(Mutex::new(owner));
        let proof = ConnectedLocalProof::new(owner.clone(), parent, resources);
        assert!(proof.tdlib_auth_ready());
        assert!(proof.tdlib_connection_only());
        assert_eq!(
            proof.prepare_tdlib_auth(&|| false),
            Some(snapshot.join(LIBRARY))
        );
        assert!(proof.prepare_tdlib_auth(&|| true).is_none());
        assert!(!proof.tdlib_ready());
        assert!(proof.prepare_tdlib(&|| false).is_none());
        assert!(!proof.inference_ready());
        assert!(!proof.browser_ready());
        assert_eq!(proof.embedded_ready(), cfg!(feature = "desktop"));
        assert!(owner.lock().unwrap().close_generation(1_000).stopped);
        assert!(!proof.tdlib_auth_ready());
        assert!(proof.prepare_tdlib_auth(&|| false).is_none());
        assert!(!snapshot.exists());
        drop(proof);
        drop(owner);
        std::fs::remove_dir_all(app).unwrap();
    }
}
