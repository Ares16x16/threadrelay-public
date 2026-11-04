//! Compiled identity and direct same-manager observations for explicit local
//! inference use. This does not create a release/native acceptance receipt.
use crate::{
    bundle_inventory::{Entry, Inventory},
    native_broker::PreparedInference,
    supervisor::{DesktopSupervisor, Handle},
    verified_artifacts::GuardedResources,
};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeSet,
    fs::OpenOptions,
    io::{self, Read},
    os::unix::fs::OpenOptionsExt,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
};

const ARCHIVE: &str = "d618da5e7bbb19077f086e6a7e1d77eab55b9bfd481f7531aadada86d13db9ed";
const COMMIT: &str = "8ca4060b7e30e76baaa5670ee1ec1b760b7f8552";
const MODEL_ID: &str = "h2o-lightning-4b-q4-k-m";
const MODEL_NAME: &str = "h2o-lightning-4b-Q4_K_M.gguf";
const MODEL_SHA256: &str = "03b90d043897e0de78983e36702f182d19fb6b2321586707841533ea7d8c407d";
const MODEL_SIZE: u64 = 2_708_808_736;
const MAX_RUNTIME_RSS: u64 = 8 * 1024 * 1024 * 1024;
const MAX_LOAD_COMMAND_BYTES: usize = 65_536;
const SYSTEM_IMPORTS: [&str; 10] = [
    "/System/Library/Frameworks/Accelerate.framework/Versions/A/Accelerate",
    "/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation",
    "/System/Library/Frameworks/Foundation.framework/Versions/C/Foundation",
    "/System/Library/Frameworks/Metal.framework/Versions/A/Metal",
    "/System/Library/Frameworks/MetalKit.framework/Versions/A/MetalKit",
    "/System/Library/Frameworks/Security.framework/Versions/A/Security",
    "/usr/lib/libSystem.B.dylib",
    "/usr/lib/libc++.1.dylib",
    "/usr/lib/libiconv.2.dylib",
    "/usr/lib/libobjc.A.dylib",
];

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct LoaderPin {
    path: String,
    filetype: u32,
    load_commands_sha256: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Pins {
    schema_version: u32,
    scope: String,
    platform: String,
    archive_sha256: String,
    runtime_commit: String,
    files: Vec<Entry>,
    loaders: Vec<LoaderPin>,
}

fn unavailable() -> io::Error {
    io::Error::other("Selected local inference capability unavailable")
}

fn selection() -> io::Result<(Inventory, Vec<LoaderPin>)> {
    let pins: Pins = serde_json::from_str(include_str!(
        "../native/connected-local-inference-pins.json"
    ))?;
    if pins.schema_version != 1
        || pins.scope != "explicit-connected-debug-runtime-identity-only"
        || pins.platform != "macos-arm64"
        || pins.archive_sha256 != ARCHIVE
        || pins.runtime_commit != COMMIT
    {
        return Err(unavailable());
    }
    let inventory = Inventory::new(pins.files)?;
    let expected: BTreeSet<_> = inventory
        .entries
        .values()
        .filter_map(|entry| match entry {
            Entry::File {
                path,
                executable: true,
                ..
            } => Some(path.as_str()),
            _ => None,
        })
        .collect();
    let mut observed = BTreeSet::new();
    for pin in &pins.loaders {
        if !matches!(pin.filetype, 2 | 6)
            || pin.load_commands_sha256.len() != 64
            || !pin
                .load_commands_sha256
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            || !observed.insert(pin.path.as_str())
        {
            return Err(unavailable());
        }
    }
    if expected != observed || !expected.contains("llama-server") {
        return Err(unavailable());
    }
    Ok((inventory, pins.loaders))
}

pub(crate) fn source_root() -> io::Result<PathBuf> {
    Ok(super::source_root()?.join(".cache/llama-native/runtime/llama-b11946"))
}

pub(crate) fn inventory() -> io::Result<Inventory> {
    selection().map(|(inventory, _)| inventory)
}

pub(crate) fn prepare(
    owner: &mut DesktopSupervisor,
    app_root: &Path,
    cancelled: &dyn Fn() -> bool,
) -> io::Result<GuardedResources> {
    let known = owner.artifact_roots();
    let (resources, guard) =
        crate::verified_artifacts::prepare_connected_inference(app_root, cancelled, &known)?;
    owner.retain_artifacts(guard);
    if !owner.retains_stable_artifacts(&resources, cancelled)
        || verify_loaders(resources.root(), cancelled).is_err()
        || cancelled()
    {
        return Err(unavailable());
    }
    Ok(resources)
}

fn word(bytes: &[u8], offset: usize) -> io::Result<u32> {
    super::word(bytes, offset)
}

fn header(bytes: &[u8], filetype: u32) -> io::Result<(usize, usize)> {
    if bytes.len() < 32
        || word(bytes, 0)? != 0xfeed_facf
        || word(bytes, 4)? != 0x0100_000c
        || word(bytes, 12)? != filetype
    {
        return Err(unavailable());
    }
    let count = word(bytes, 16)? as usize;
    let size = word(bytes, 20)? as usize;
    if count == 0 || count > 128 || size > MAX_LOAD_COMMAND_BYTES || size < count * 8 {
        return Err(unavailable());
    }
    Ok((count, size))
}

fn command_name(command: &[u8], minimum: usize) -> io::Result<&str> {
    let offset = word(command, 8)? as usize;
    if offset < minimum {
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

fn local_library<'a>(name: &'a str, pins: &Inventory) -> io::Result<String> {
    let local = name.strip_prefix("@rpath/").ok_or_else(unavailable)?;
    if Path::new(local)
        .file_name()
        .and_then(|value| value.to_str())
        != Some(local)
    {
        return Err(unavailable());
    }
    let resolved = pins.resolve(local)?;
    if !matches!(
        pins.entries.get(&resolved),
        Some(Entry::File {
            executable: true,
            ..
        })
    ) || !resolved.ends_with(".dylib")
    {
        return Err(unavailable());
    }
    Ok(resolved)
}

fn check_loader(bytes: &[u8], pin: &LoaderPin, pins: &Inventory) -> io::Result<()> {
    let (count, size) = header(bytes, pin.filetype)?;
    if bytes.len() != 32 + size
        || format!("{:x}", Sha256::digest(bytes)) != pin.load_commands_sha256
    {
        return Err(unavailable());
    }
    let mut offset = 32usize;
    let (mut identities, mut rpaths, mut dylinkers, mut local_imports) = (0, 0, 0, 0);
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
                let name = command_name(command, 24)?;
                if !SYSTEM_IMPORTS.contains(&name) {
                    local_library(name, pins)?;
                    local_imports += 1;
                }
            }
            0x8000_0018 => {
                // The official CPU/runtime closure weakly imports this OS
                // library. It is not an ambient/project search-path allowance.
                if command_name(command, 24)? != "/usr/lib/librdma.dylib" {
                    return Err(unavailable());
                }
            }
            0x0d => {
                identities += 1;
                if local_library(command_name(command, 24)?, pins)? != pin.path {
                    return Err(unavailable());
                }
            }
            0x8000_001c => {
                rpaths += 1;
                if command_name(command, 12)? != "@loader_path" {
                    return Err(unavailable());
                }
            }
            0x0e => {
                dylinkers += 1;
                if command_name(command, 12)? != "/usr/lib/dyld" {
                    return Err(unavailable());
                }
            }
            0x19 | 0x8000_0034 | 0x8000_0033 | 0x8000_0022 | 0x8000_0028 | 0x02 | 0x0b | 0x1b
            | 0x32 | 0x2a | 0x26 | 0x29 | 0x1d => (),
            _ => return Err(unavailable()),
        }
        offset = end;
    }
    if offset != bytes.len()
        || rpaths > 1
        || (local_imports > 0 && rpaths != 1)
        || (pin.filetype == 6 && (identities != 1 || dylinkers != 0))
        || (pin.filetype == 2 && (identities != 0 || dylinkers != 1))
    {
        return Err(unavailable());
    }
    Ok(())
}

fn verify_loaders(root: &Path, cancelled: &dyn Fn() -> bool) -> io::Result<()> {
    let (pins, loaders) = selection()?;
    for pin in loaders {
        if cancelled() {
            return Err(unavailable());
        }
        let mut file = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
            .open(root.join(&pin.path))?;
        if !file.metadata()?.is_file() {
            return Err(unavailable());
        }
        let mut bytes = vec![0u8; 32];
        file.read_exact(&mut bytes)?;
        let (_, size) = header(&bytes, pin.filetype)?;
        bytes.resize(32 + size, 0);
        file.read_exact(&mut bytes[32..])?;
        check_loader(&bytes, &pin, &pins)?;
    }
    Ok(())
}

pub(crate) struct LocalInference {
    owner: Arc<Mutex<DesktopSupervisor>>,
    parent: Arc<Handle>,
    resources: GuardedResources,
    selected: bool,
    model: Mutex<Option<GuardedResources>>,
}
impl LocalInference {
    pub(crate) fn new(
        owner: Arc<Mutex<DesktopSupervisor>>,
        parent: Arc<Handle>,
        resources: GuardedResources,
    ) -> Self {
        let selected = inventory().is_ok_and(|pins| {
            crate::verified_artifacts::verify_bundle(resources.root(), &pins, &|| false).is_ok()
                && verify_loaders(resources.root(), &|| false).is_ok()
        });
        Self {
            owner,
            parent,
            resources,
            selected,
            model: Mutex::new(None),
        }
    }

    fn live(
        &self,
        owner: &mut DesktopSupervisor,
        cancelled: &dyn Fn() -> bool,
        model: Option<&GuardedResources>,
    ) -> bool {
        self.selected
            && !cancelled()
            && owner.retains_stable_artifacts(&self.resources, cancelled)
            && model.is_none_or(|model| owner.retains_stable_artifacts(model, cancelled))
            && owner.processes.is_live(&self.parent).unwrap_or(false)
            && !cancelled()
    }

    pub(crate) fn ready(&self) -> bool {
        self.owner
            .lock()
            .ok()
            .is_some_and(|mut owner| self.live(&mut owner, &|| false, None))
    }

    pub(crate) fn authorize(
        &self,
        prepared: &mut PreparedInference,
        cancelled: &dyn Fn() -> bool,
    ) -> bool {
        #[cfg(test)]
        let diagnostics = std::time::Instant::now();
        #[cfg(test)]
        eprintln!("Connected inference: authorization requested");
        // The native prerequisite observations cover this selected Q4 model.
        // Other catalog models need their own local-use validation.
        if prepared.model_id != MODEL_ID
            || prepared.model_sha256 != MODEL_SHA256
            || prepared.model_size != MODEL_SIZE
            || prepared.runtime_commit != COMMIT
            || prepared.model_path != self.resources.app_root().join("models").join(MODEL_NAME)
            || cancelled()
        {
            return false;
        }
        let known = {
            let Ok(mut owner) = self.owner.lock() else {
                return false;
            };
            if !self.live(&mut owner, cancelled, None) {
                #[cfg(test)]
                eprintln!("Connected inference: live runtime guard denied");
                return false;
            }
            owner.artifact_roots()
        };
        if !inventory().is_ok_and(|pins| {
            crate::verified_artifacts::verify_bundle(self.resources.root(), &pins, cancelled)
                .is_ok()
        }) || verify_loaders(self.resources.root(), cancelled).is_err()
        {
            #[cfg(test)]
            eprintln!("Connected inference: runtime closure verification denied");
            return false;
        }
        #[cfg(test)]
        eprintln!(
            "Connected inference: runtime closure verified after {:?}",
            diagnostics.elapsed()
        );
        let cached = self.model.lock().ok().and_then(|model| model.clone());
        if let Some(cached) = cached {
            let live = self
                .owner
                .lock()
                .ok()
                .is_some_and(|mut owner| self.live(&mut owner, cancelled, Some(&cached)));
            if live {
                prepared.model_path = cached.root().join(MODEL_NAME);
                return prepared
                    .bind_runtime_executable(self.resources.root().join("llama-server"))
                    .is_ok();
            }
            return false;
        }
        let mut guard = match crate::verified_artifacts::prepare_model(
            &prepared.model_path,
            self.resources.app_root(),
            MODEL_SHA256,
            MODEL_SIZE,
            cancelled,
            &known,
        ) {
            Ok(guard) => guard,
            Err(problem) => {
                #[cfg(test)]
                eprintln!(
                    "Connected inference: model guard failed after {:?}: {problem}",
                    diagnostics.elapsed()
                );
                #[cfg(not(test))]
                let _ = problem;
                return false;
            }
        };
        #[cfg(test)]
        eprintln!(
            "Connected inference: model guard prepared after {:?}",
            diagnostics.elapsed()
        );
        let model = GuardedResources::from_connected_guard(&guard, self.resources.app_root());
        let mut owner = match self.owner.lock() {
            Ok(owner) => owner,
            Err(_) => {
                let _ = guard.cleanup();
                return false;
            }
        };
        if !self.live(&mut owner, cancelled, None) {
            #[cfg(test)]
            eprintln!("Connected inference: runtime/worker expired during model preparation");
            if guard.cleanup().is_err() {
                owner.retain_artifacts(guard);
            }
            return false;
        }
        owner.retain_artifacts(guard);
        if !owner.retains_stable_artifacts(&model, cancelled) {
            #[cfg(test)]
            eprintln!("Connected inference: retained model stability denied");
            return false;
        }
        prepared.model_path = model.root().join(MODEL_NAME);
        if prepared
            .bind_runtime_executable(self.resources.root().join("llama-server"))
            .is_err()
        {
            return false;
        }
        match self.model.lock() {
            Ok(mut slot) => *slot = Some(model),
            Err(_) => return false,
        }
        #[cfg(test)]
        eprintln!(
            "Connected inference: authorization complete after {:?}",
            diagnostics.elapsed()
        );
        !cancelled()
    }

    fn observed<T>(&self, observe: impl FnOnce(&mut DesktopSupervisor) -> Option<T>) -> Option<T> {
        let model = self.model.lock().ok()?.clone()?;
        let mut owner = self.owner.try_lock().ok()?;
        if !self.live(&mut owner, &|| false, Some(&model)) {
            return None;
        }
        observe(&mut owner)
    }

    pub(crate) fn endpoint_owned(&self, handle: &Handle, port: u16) -> bool {
        self.observed(|owner| owner.processes.inference_listener(handle, port).ok()) == Some(true)
    }

    pub(crate) fn within_resource_limits(&self, handle: &Handle) -> bool {
        self.observed(|owner| {
            owner
                .processes
                .evaluation_memory(&self.parent, Some(handle))
                .ok()
        })
        .is_some_and(|(_, runtime)| runtime > 0 && runtime <= MAX_RUNTIME_RSS)
    }

    pub(crate) fn peer(&self, worker: &Handle, runtime: &Handle, client: u16, server: u16) -> bool {
        if !std::ptr::eq(worker, self.parent.as_ref()) {
            return false;
        }
        self.observed(|owner| {
            owner
                .processes
                .verify_inference_peer(worker, runtime, client, server)
                .ok()
        }) == Some(true)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn command(kind: u32, name: &str) -> Vec<u8> {
        let start = if matches!(kind, 0x8000_001c | 0x0e) {
            12
        } else {
            24
        };
        let length = (start + name.len() + 1).div_ceil(8) * 8;
        let mut bytes = vec![0u8; length];
        bytes[..4].copy_from_slice(&kind.to_le_bytes());
        bytes[4..8].copy_from_slice(&(length as u32).to_le_bytes());
        bytes[8..12].copy_from_slice(&(start as u32).to_le_bytes());
        bytes[start..start + name.len()].copy_from_slice(name.as_bytes());
        bytes
    }

    fn table(commands: &[Vec<u8>]) -> Vec<u8> {
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

    fn check_table(bytes: &[u8]) -> io::Result<()> {
        check_loader(
            bytes,
            &LoaderPin {
                path: "libggml-base.0.27.0.dylib".into(),
                filetype: 6,
                load_commands_sha256: format!("{:x}", Sha256::digest(bytes)),
            },
            &inventory()?,
        )
    }

    #[test]
    fn compiled_runtime_selection_covers_every_executable_and_symlink_target() {
        let (pins, loaders) = selection().unwrap();
        assert_eq!(pins.entries.len(), 61);
        assert_eq!(loaders.len(), 42);
        assert_eq!(
            pins.resolve("libggml-base.dylib").unwrap(),
            "libggml-base.0.27.0.dylib"
        );
        assert_eq!(
            pins.resolve("libllama.0.dylib").unwrap(),
            "libllama.0.7.0.dylib"
        );
        assert!(pins.resolve("../other.dylib").is_err());
        assert!(pins.resolve("libexternal.dylib").is_err());
    }

    #[test]
    fn system_and_local_loader_names_do_not_create_search_path_allowances() {
        let pins = inventory().unwrap();
        assert_eq!(
            local_library("@rpath/libllama.0.dylib", &pins).unwrap(),
            "libllama.0.7.0.dylib"
        );
        for name in [
            "@rpath/../libllama.0.dylib",
            "@loader_path/libllama.0.dylib",
            "libllama.0.dylib",
            "/opt/homebrew/lib/libllama.0.dylib",
            "@rpath/llama-server",
            "@rpath/unknown.dylib",
        ] {
            assert!(local_library(name, &pins).is_err(), "{name}");
        }
    }

    #[test]
    fn loader_policy_rejects_escape_external_rpath_weak_reexport_and_bad_commands() {
        let selected = vec![
            command(0x0d, "@rpath/libggml-base.0.dylib"),
            command(0x0c, "@rpath/libllama.0.dylib"),
            command(0x8000_001c, "@loader_path"),
        ];
        assert!(check_table(&table(&selected)).is_ok());
        for extra in [
            command(0x0c, "/opt/homebrew/lib/libssl.dylib"),
            command(0x0c, "@rpath/../libllama.0.dylib"),
            command(0x0c, "@rpath/unknown.dylib"),
            command(0x8000_0018, "@rpath/libllama.0.dylib"),
            command(0x8000_001f, "@rpath/libllama.0.dylib"),
            command(0x8000_001c, "@loader_path"),
            command(0x8000_001c, "@executable_path/../other"),
            command(0x27, "DYLD_LIBRARY_PATH=/private/other"),
        ] {
            let mut commands = selected.clone();
            commands.push(extra);
            assert!(check_table(&table(&commands)).is_err());
        }
        assert!(check_table(&table(&selected[..2])).is_err());
        let valid = table(&selected);
        for (offset, value) in [
            (0, 0xcafe_babeu32),
            (4, 0x0100_0007),
            (16, 0),
            (20, 65_544),
            (36, 0),
            (36, 7),
            (40, 4),
        ] {
            let mut bytes = valid.clone();
            bytes[offset..offset + 4].copy_from_slice(&value.to_le_bytes());
            assert!(check_table(&bytes).is_err(), "offset {offset}");
        }
    }

    #[test]
    #[ignore = "Opt-in exact local runtime snapshot and owned-worker smoke; no account, model load or send"]
    fn selected_runtime_snapshot_readiness_is_independent_and_expires_with_worker() {
        use crate::{
            native_broker::{InferenceCatalog, InferenceRequest},
            supervisor::{ProcessKind, SpawnSpec},
        };
        use std::os::unix::fs::PermissionsExt;
        let app = Path::new("/private/tmp").join(format!(
            "threadrelay-connected-runtime-{}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&app);
        std::fs::create_dir(&app).unwrap();
        std::fs::set_permissions(&app, std::fs::Permissions::from_mode(0o700)).unwrap();
        let mut owner = DesktopSupervisor::acquire(&app.join("profile")).unwrap();
        let resources = prepare(&mut owner, &app, &|| false).unwrap();
        let snapshot = resources.root().to_owned();
        assert_ne!(snapshot, source_root().unwrap());
        assert!(owner.retains_stable_artifacts(&resources, &|| false));
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
        let proof = LocalInference::new(owner.clone(), parent, resources);
        assert!(proof.ready());
        let catalog = InferenceCatalog::new(&app, &snapshot).unwrap();
        let mut prepared = catalog
            .prepare(InferenceRequest {
                model_id: MODEL_ID.into(),
                context_tokens: 2048,
                cpu_threads: 2,
                token: "synthetic".repeat(8),
            })
            .unwrap();
        assert!(!proof.authorize(&mut prepared, &|| true));
        assert!(
            !proof.authorize(&mut prepared, &|| false),
            "missing model must not be authorized"
        );
        assert!(
            proof.ready(),
            "missing model must not remove the independently guarded runtime"
        );
        assert_eq!(owner.lock().unwrap().artifact_roots().len(), 1);
        assert!(!proof.endpoint_owned(&proof.parent, 0));
        assert!(owner.lock().unwrap().close_generation(1_000).stopped);
        assert!(!proof.ready());
        assert!(!snapshot.exists());
        drop(proof);
        drop(owner);
        std::fs::remove_dir_all(app).unwrap();
    }
}
