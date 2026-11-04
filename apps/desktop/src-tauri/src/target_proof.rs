//! Compiled-in target inventory plus direct supervisor/native observations.
//! Empty inventories, missing evidence and unsupported stability guards deny.
#[cfg(windows)]
use crate::artifact_verifier::is_link;
use crate::{
    artifact_verifier::verify_file,
    native_broker::{NativeProof, PreparedInference},
    supervisor::{DesktopSupervisor, Handle},
};
use serde::Deserialize;
use std::{
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
};
type FilePin = crate::bundle_inventory::Entry;
#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct EvidencePin {
    sha256: String,
    size: u64,
}
#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct RuntimePin {
    archive_sha256: String,
    files: Vec<FilePin>,
    evidence: EvidencePin,
    max_rss_bytes: u64,
}
#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct BrowserPin {
    browser_archive_sha256: String,
    browser_revision: String,
    browser_version: String,
    playwright_core_version: String,
    node_version: String,
    browser_files: Vec<FilePin>,
    driver_files: Vec<FilePin>,
    entry_files: Vec<FilePin>,
    evidence: EvidencePin,
}
#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct TdlibPin {
    artifact_sha256: String,
    commit: String,
    schema_sha256: String,
    files: Vec<FilePin>,
    evidence: EvidencePin,
}
#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct Target {
    platform: String,
    runtime: Option<RuntimePin>,
    browser: Option<BrowserPin>,
    tdlib: Option<TdlibPin>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Inventory {
    schema_version: u32,
    targets: Vec<Target>,
}
const INVENTORY: &str = include_str!("../native/target-inventory.json");
fn hash(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn pins(files: &[FilePin]) -> Option<crate::bundle_inventory::Inventory> {
    crate::bundle_inventory::Inventory::new(files.to_vec()).ok()
}
fn evidence_document(
    value: &serde_json::Value,
    platform: &str,
    purpose: &str,
    archive: &str,
    commit: Option<&str>,
) -> bool {
    let object = match value.as_object() {
        Some(v) => v,
        None => return false,
    };
    let keys = [
        "schema_version",
        "platform",
        "purpose",
        "result",
        "archive_sha256",
        "runtime_commit",
        "checks",
    ];
    if object.len() != keys.len()
        || keys.iter().any(|key| !object.contains_key(*key))
        || value["schema_version"] != 1
        || value["platform"] != platform
        || value["purpose"] != purpose
        || value["result"] != "passed"
        || value["archive_sha256"] != archive
    {
        return false;
    }
    if match commit {
        Some(v) => value["runtime_commit"] != v,
        None => !value["runtime_commit"].is_null(),
    } {
        return false;
    }
    let required: &[&str] = if purpose == "inference" {
        &[
            "artifact_identity",
            "connected_peer",
            "offline",
            "resource_limits",
            "crash_cleanup",
        ]
    } else if purpose == "browser" {
        &[
            "artifact_identity",
            "driver_ownership",
            "cdp_pipe",
            "closed_launch",
            "crash_cleanup",
        ]
    } else if purpose == "tdlib" {
        &[
            "artifact_identity",
            "loader_dependencies",
            "encrypted_restart",
            "preinit_offline",
            "pending_no_flush",
            "stop_pause_wake",
            "no_blind_replay",
            "ordered_outcomes",
            "zero_paid_sender",
            "data_minimization",
            "close_logout",
            "cache_privacy",
            "exit_cleanup",
        ]
    } else {
        return false;
    };
    let checks = match value["checks"].as_array() {
        Some(v) => v,
        None => return false,
    };
    checks.len() == required.len()
        && required
            .iter()
            .all(|name| checks.iter().filter(|v| v.as_str() == Some(*name)).count() == 1)
}
fn platform() -> Option<&'static str> {
    if cfg!(all(target_os = "windows", target_arch = "x86_64")) {
        Some("windows-x64")
    } else if cfg!(all(target_os = "macos", target_arch = "aarch64")) {
        Some("macos-arm64")
    } else {
        None
    }
}
fn select() -> Option<Target> {
    let inventory: Inventory = serde_json::from_str(INVENTORY).ok()?;
    if inventory.schema_version != 1 {
        return None;
    }
    let target = platform()?;
    let matches: Vec<_> = inventory
        .targets
        .into_iter()
        .filter(|entry| entry.platform == target)
        .collect();
    if matches.len() != 1 {
        return None;
    }
    matches.into_iter().next()
}
/// Constructed only by the native host from compiled pins and its own manager.
/// No renderer, env flag, caller JSON, or test fixture can mint this capability.
pub(crate) struct PackagedProof {
    owner: Arc<Mutex<DesktopSupervisor>>,
    parent: Arc<Handle>,
    resources: PathBuf,
    guarded: crate::verified_artifacts::GuardedResources,
    target: Option<Target>,
    holds: Mutex<std::collections::BTreeMap<PathBuf, std::fs::File>>,
}
impl PackagedProof {
    pub fn new(
        owner: Arc<Mutex<DesktopSupervisor>>,
        parent: Arc<Handle>,
        guarded: crate::verified_artifacts::GuardedResources,
    ) -> Self {
        Self {
            owner,
            parent,
            resources: guarded.root().to_owned(),
            guarded,
            target: select(),
            holds: Mutex::new(std::collections::BTreeMap::new()),
        }
    }
    fn runtime(&self) -> Option<&RuntimePin> {
        let value = self.target.as_ref()?.runtime.as_ref()?;
        if !hash(&value.archive_sha256)
            || !hash(&value.evidence.sha256)
            || value.evidence.size == 0
            || value.max_rss_bytes == 0
            || pins(&value.files).is_none()
        {
            return None;
        }
        let catalog: serde_json::Value = serde_json::from_str(include_str!(
            "../../../../worker/src/threadrelay/inference/manifest.json"
        ))
        .ok()?;
        let platform = &self.target.as_ref()?.platform;
        let artifact = catalog["runtime"]["artifacts"]
            .as_array()?
            .iter()
            .find(|v| v["platform"] == *platform)?;
        if artifact["sha256"] != value.archive_sha256 {
            return None;
        }
        Some(value)
    }
    fn browser(&self) -> Option<&BrowserPin> {
        let value = self.target.as_ref()?.browser.as_ref()?;
        if !hash(&value.browser_archive_sha256)
            || value.browser_revision != "1248"
            || value.browser_version != "156.0.8078.4"
            || value.playwright_core_version != "1.64.0-beta-1791568591000"
            || value.node_version != "24.22.1"
            || !hash(&value.evidence.sha256)
            || value.evidence.size == 0
            || pins(&value.browser_files).is_none()
            || pins(&value.driver_files).is_none()
            || pins(&value.entry_files).is_none()
        {
            return None;
        }
        Some(value)
    }
    fn tdlib(&self) -> Option<&TdlibPin> {
        let pin = self.target.as_ref()?.tdlib.as_ref()?;
        if pin.commit != "bc9c263e2bfee06aaab41e82db51a103376030bc"
            || pin.schema_sha256
                != "98b21543066e0124623b7d36f6496ef38cf86e9f9825aedcdd8e2527633fee4c"
            || !hash(&pin.artifact_sha256)
            || !hash(&pin.evidence.sha256)
            || pin.evidence.size == 0
            || pins(&pin.files).is_none()
        {
            return None;
        }
        Some(pin)
    }
    fn hold(&self, paths: &[PathBuf]) -> bool {
        #[cfg(windows)]
        {
            use std::os::windows::fs::OpenOptionsExt;
            const SHARE_READ: u32 = 1;
            const OPEN_REPARSE: u32 = 0x0020_0000;
            const BACKUP: u32 = 0x0200_0000;
            let mut held = match self.holds.lock() {
                Ok(v) => v,
                Err(_) => return false,
            };
            let mut guards = Vec::new();
            for path in paths {
                if held.contains_key(path) || guards.iter().any(|(existing, _)| existing == path) {
                    continue;
                }
                let metadata = match std::fs::symlink_metadata(path) {
                    Ok(v) => v,
                    Err(_) => return false,
                };
                if is_link(&metadata) {
                    return false;
                }
                let mut opts = std::fs::OpenOptions::new();
                opts.read(true)
                    .share_mode(SHARE_READ)
                    .custom_flags(OPEN_REPARSE | if metadata.is_dir() { BACKUP } else { 0 });
                let file = match opts.open(path) {
                    Ok(v) => v,
                    Err(_) => return false,
                };
                if file
                    .metadata()
                    .map(|m| is_link(&m) || m.is_dir() != metadata.is_dir())
                    .unwrap_or(true)
                {
                    return false;
                }
                guards.push((path.clone(), file));
            }
            held.extend(guards);
            true
        }
        #[cfg(target_os = "macos")]
        {
            paths.iter().all(|path| self.guarded.contains(path))
        }
        #[cfg(not(any(windows, target_os = "macos")))]
        {
            let _ = (&self.holds, paths, &self.guarded);
            false
        }
    }
    fn guarded_tree(&self, base: &Path, files: &[FilePin], cancelled: &dyn Fn() -> bool) -> bool {
        let values = match pins(files) {
            Some(v) => v,
            None => return false,
        };
        let mut paths = vec![
            self.resources.clone(),
            self.resources.join("native"),
            base.to_owned(),
        ];
        for name in values.entries.keys() {
            let mut path = base.to_owned();
            for component in Path::new(name).components() {
                path.push(component.as_os_str());
                paths.push(path.clone());
            }
        }
        self.hold(&paths)
            && crate::verified_artifacts::verify_bundle(base, &values, cancelled).is_ok()
    }

    fn evidence(
        &self,
        name: &str,
        pin: &EvidencePin,
        purpose: &str,
        archive: &str,
        commit: Option<&str>,
        cancelled: &dyn Fn() -> bool,
    ) -> bool {
        if pin.size > 1024 * 1024 {
            return false;
        }
        let path = self.resources.join("native").join(name);
        if !self.hold(&[
            self.resources.clone(),
            self.resources.join("native"),
            path.clone(),
        ]) {
            return false;
        }
        let data = match crate::artifact_verifier::read_verified_file(
            &path,
            &pin.sha256,
            pin.size,
            cancelled,
        ) {
            Ok(v) => v,
            Err(_) => return false,
        };
        let mut frame = Vec::with_capacity(data.len() + 4);
        frame.extend_from_slice(&(data.len() as u32).to_be_bytes());
        frame.extend_from_slice(&data);
        let document = match crate::ipc::read_frame(&mut std::io::Cursor::new(frame)) {
            Ok(Some(v)) => v,
            _ => return false,
        };
        self.target.as_ref().is_some_and(|target| {
            evidence_document(&document, &target.platform, purpose, archive, commit)
        })
    }
}
impl NativeProof for PackagedProof {
    fn tdlib_ready(&self) -> bool {
        cfg!(any(windows, target_os = "macos"))
            && self.tdlib().is_some()
            && self.resources.join("native/tdlib-evidence.json").is_file()
    }
    fn prepare_tdlib(&self, cancelled: &dyn Fn() -> bool) -> Option<PathBuf> {
        let pin = self.tdlib()?;
        if !self.evidence(
            "tdlib-evidence.json",
            &pin.evidence,
            "tdlib",
            &pin.artifact_sha256,
            Some(&pin.commit),
            cancelled,
        ) || !self.guarded_tree(&self.resources.join("native/tdlib"), &pin.files, cancelled)
        {
            return None;
        }
        let path = self.resources.join(if cfg!(windows) {
            "native/tdlib/tdjson.dll"
        } else {
            "native/tdlib/libtdjson.dylib"
        });
        if !path.is_file() || cancelled() {
            return None;
        }
        Some(path)
    }
    fn inference_ready(&self) -> bool {
        cfg!(any(windows, target_os = "macos"))
            && self.runtime().is_some()
            && self
                .resources
                .join("native/inference-evidence.json")
                .is_file()
            && self
                .resources
                .join("native/llama/llama-server")
                .with_extension(if cfg!(windows) { "exe" } else { "" })
                .is_file()
    }
    fn authorize_inference(
        &self,
        prepared: &mut PreparedInference,
        cancelled: &dyn Fn() -> bool,
    ) -> bool {
        let pin = match self.runtime() {
            Some(v) => v,
            None => return false,
        };
        if !self.evidence(
            "inference-evidence.json",
            &pin.evidence,
            "inference",
            &pin.archive_sha256,
            Some(&prepared.runtime_commit),
            cancelled,
        ) || !self.guarded_tree(&self.resources.join("native/llama"), &pin.files, cancelled)
        {
            return false;
        }
        #[cfg(target_os = "macos")]
        {
            let known = match self.owner.lock() {
                Ok(owner) => owner.artifact_roots(),
                Err(_) => return false,
            };
            let guard = match crate::verified_artifacts::prepare_model(
                &prepared.model_path,
                self.guarded.app_root(),
                &prepared.model_sha256,
                prepared.model_size,
                cancelled,
                &known,
            ) {
                Ok(v) => v,
                Err(_) => return false,
            };
            let name = match prepared.model_path.file_name() {
                Some(v) => v,
                None => return false,
            };
            let path = guard.root().join(name);
            let mut owner = match self.owner.lock() {
                Ok(v) => v,
                Err(_) => return false,
            };
            let mut guard = guard;
            if cancelled() || !owner.processes.is_live(&self.parent).unwrap_or(false) {
                if guard.cleanup().is_err() {
                    owner.retain_artifacts(guard);
                }
                return false;
            }
            owner.retain_artifacts(guard);
            prepared.model_path = path;
            true
        }
        #[cfg(not(target_os = "macos"))]
        {
            let model_parent = match prepared.model_path.parent() {
                Some(v) => v,
                None => return false,
            };
            let paths = [
                model_parent.parent().unwrap_or(model_parent).to_owned(),
                model_parent.to_owned(),
                prepared.model_path.clone(),
            ];
            if !self.hold(&paths)
                || verify_file(
                    &prepared.model_path,
                    &prepared.model_sha256,
                    prepared.model_size,
                    cancelled,
                )
                .is_err()
            {
                return false;
            }
            #[cfg(windows)]
            {
                let mut held = match self.holds.lock() {
                    Ok(v) => v,
                    Err(_) => return false,
                };
                let mut files = Vec::new();
                for path in &paths {
                    if let Some(file) = held.remove(path) {
                        files.push(file);
                    }
                }
                drop(held);
                let mut guard =
                    crate::verified_artifacts::VerifiedArtifactSet::retained_windows_files(
                        model_parent.to_owned(),
                        files,
                    );
                let mut owner = match self.owner.lock() {
                    Ok(v) => v,
                    Err(_) => return false,
                };
                if cancelled() || !owner.processes.is_live(&self.parent).unwrap_or(false) {
                    let _ = guard.cleanup();
                    return false;
                }
                owner.retain_artifacts(guard);
            }
            !cancelled()
        }
    }

    fn browser_ready(&self) -> bool {
        cfg!(any(windows,target_os="macos"))
            && self.browser().is_some()
            && self
                .resources
                .join("native/browser-evidence.json")
                .is_file()
            && self.resources.join(if cfg!(windows) {"native/browser/chrome.exe"} else {"native/browser/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"}).is_file()
            && self.resources.join(if cfg!(windows) {"native/playwright/node.exe"} else {"native/playwright/node"}).is_file()
            && self
                .resources
                .join("native/threadrelay/owned-driver.cjs")
                .is_file()
    }
    fn authorize_browser(
        &self,
        _browser: &Path,
        _driver: &Path,
        _entry: &Path,
        _profile: &Path,
        cancelled: &dyn Fn() -> bool,
    ) -> bool {
        let pin = match self.browser() {
            Some(v) => v,
            None => return false,
        };
        self.evidence(
            "browser-evidence.json",
            &pin.evidence,
            "browser",
            &pin.browser_archive_sha256,
            None,
            cancelled,
        ) && self.guarded_tree(
            &self.resources.join("native/browser"),
            &pin.browser_files,
            cancelled,
        ) && self.guarded_tree(
            &self.resources.join("native/playwright"),
            &pin.driver_files,
            cancelled,
        ) && self.guarded_tree(
            &self.resources.join("native/threadrelay"),
            &pin.entry_files,
            cancelled,
        )
    }
    fn driver_contained(&self, handle: &Handle) -> bool {
        self.owner
            .try_lock()
            .ok()
            .and_then(|mut owner| owner.processes.is_live(handle).ok())
            .unwrap_or(false)
    }
    fn endpoint_owned(&self, handle: &Handle, port: u16) -> bool {
        #[cfg(any(windows, target_os = "macos"))]
        {
            self.owner
                .try_lock()
                .ok()
                .and_then(|mut owner| owner.processes.inference_listener(handle, port).ok())
                .unwrap_or(false)
        }
        #[cfg(not(any(windows, target_os = "macos")))]
        {
            let _ = (handle, port);
            false
        }
    }
    fn within_resource_limits(&self, handle: &Handle) -> bool {
        let limit = match self.runtime() {
            Some(v) => v.max_rss_bytes,
            None => return false,
        };
        #[cfg(any(windows, target_os = "macos"))]
        {
            self.owner
                .try_lock()
                .ok()
                .and_then(|mut owner| {
                    owner
                        .processes
                        .evaluation_memory(&self.parent, Some(handle))
                        .ok()
                })
                .is_some_and(|(_, runtime)| runtime <= limit)
        }
        #[cfg(not(any(windows, target_os = "macos")))]
        {
            let _ = (handle, limit, &self.parent);
            false
        }
    }
    fn inference_peer(&self, worker: &Handle, runtime: &Handle, client: u16, server: u16) -> bool {
        #[cfg(any(windows, target_os = "macos"))]
        {
            self.owner
                .try_lock()
                .ok()
                .and_then(|mut owner| {
                    owner
                        .processes
                        .verify_inference_peer(worker, runtime, client, server)
                        .ok()
                })
                .unwrap_or(false)
        }
        #[cfg(not(any(windows, target_os = "macos")))]
        {
            let _ = (worker, runtime, client, server);
            false
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn absent_compiled_inventory_cannot_authorize() {
        assert!(select().is_none());
    }
    #[test]
    fn evidence_must_match_exact_target_purpose_and_all_required_checks() {
        let good = serde_json::json!({"schema_version":1,"platform":"windows-x64","purpose":"inference",
          "result":"passed","archive_sha256":"a".repeat(64),"runtime_commit":"b".repeat(40),
          "checks":["artifact_identity","connected_peer","offline","resource_limits","crash_cleanup"]});
        assert!(evidence_document(
            &good,
            "windows-x64",
            "inference",
            &"a".repeat(64),
            Some(&"b".repeat(40))
        ));
        let mut bad = good.clone();
        bad["checks"] = serde_json::json!(["artifact_identity"]);
        assert!(!evidence_document(
            &bad,
            "windows-x64",
            "inference",
            &"a".repeat(64),
            Some(&"b".repeat(40))
        ));
        assert!(!evidence_document(
            &good,
            "macos-arm64",
            "inference",
            &"a".repeat(64),
            Some(&"b".repeat(40))
        ));
    }

    #[test]
    fn incomplete_file_inventory_is_not_accepted() {
        assert!(pins(&[]).is_none());
        assert!(pins(&[FilePin::File {
            path: "x".into(),
            sha256: "fake".into(),
            size: 8,
            executable: false
        }])
        .is_none());
    }
}
