//! Closed host-side catalog and generation-owned dependency brokerage.
//! Native artifact, endpoint and resource proof are separate mandatory gates.
use crate::supervisor::SpawnSpec;
use serde_json::Value;
use std::{
    ffi::OsString,
    io,
    path::{Path, PathBuf},
};

const MODEL_CATALOG: &str =
    include_str!("../../../../worker/src/threadrelay/inference/manifest.json");

pub(crate) struct InferenceRequest {
    pub model_id: String,
    pub context_tokens: u32,
    pub cpu_threads: u32,
    pub token: String,
}
impl Drop for InferenceRequest {
    fn drop(&mut self) {
        crate::secure_auth::wipe_string(&mut self.token);
    }
}
pub(crate) struct InferenceCatalog {
    root: PathBuf,
    resources: PathBuf,
}
pub(crate) struct PreparedInference {
    pub model_id: String,
    pub model_path: PathBuf,
    pub model_sha256: String,
    pub model_size: u64,
    pub runtime_commit: String,
    executable: PathBuf,
    context_tokens: u32,
    cpu_threads: u32,
    token: String,
}
impl Drop for PreparedInference {
    fn drop(&mut self) {
        crate::secure_auth::wipe_string(&mut self.token);
    }
}
impl InferenceCatalog {
    pub fn new(root: &Path, resources: &Path) -> io::Result<Self> {
        if !root.is_absolute() || !resources.is_absolute() {
            return Err(io::Error::other("Invalid installed layout"));
        }
        Ok(Self {
            root: root.to_owned(),
            resources: resources.to_owned(),
        })
    }
    pub fn prepare(&self, mut request: InferenceRequest) -> io::Result<PreparedInference> {
        if !(2048..=65536).contains(&request.context_tokens)
            || !(1..=16).contains(&request.cpu_threads)
            || !(32..=128).contains(&request.token.len())
            || !request
                .token
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
        {
            return Err(io::Error::other("Invalid inference request"));
        }
        let catalog: serde_json::Value = serde_json::from_str(MODEL_CATALOG)
            .map_err(|_| io::Error::other("Invalid built-in catalog"))?;
        let record = catalog["models"]
            .as_array()
            .and_then(|models| {
                models
                    .iter()
                    .find(|m| m["manifest"]["id"] == request.model_id)
            })
            .ok_or_else(|| io::Error::other("Unknown model"))?;
        let filename = record["filename"]
            .as_str()
            .ok_or_else(|| io::Error::other("Invalid built-in catalog"))?;
        if Path::new(filename).file_name().and_then(|s| s.to_str()) != Some(filename) {
            return Err(io::Error::other("Invalid built-in catalog"));
        }
        let mut executable = self
            .resources
            .join("native")
            .join("llama")
            .join("llama-server");
        if cfg!(windows) {
            executable.set_extension("exe");
        }
        Ok(PreparedInference {
            model_id: request.model_id.clone(),
            model_path: self.root.join("models").join(filename),
            model_sha256: record["manifest"]["sha256"]
                .as_str()
                .ok_or_else(|| io::Error::other("Invalid built-in catalog"))?
                .into(),
            model_size: record["size_bytes"]
                .as_u64()
                .ok_or_else(|| io::Error::other("Invalid built-in catalog"))?,
            runtime_commit: catalog["runtime"]["commit"]
                .as_str()
                .ok_or_else(|| io::Error::other("Invalid built-in catalog"))?
                .into(),
            executable,
            context_tokens: request.context_tokens,
            cpu_threads: request.cpu_threads,
            token: std::mem::take(&mut request.token),
        })
    }
}
impl PreparedInference {
    /// Only a compiled host proof may bind its independently retained runtime.
    /// The renderer/private worker never supplies an executable path.
    pub(crate) fn bind_runtime_executable(&mut self, executable: PathBuf) -> io::Result<()> {
        if !executable.is_absolute()
            || executable.components().any(|part| {
                matches!(
                    part,
                    std::path::Component::ParentDir | std::path::Component::CurDir
                )
            })
            || executable.file_name().and_then(|v| v.to_str()) != Some("llama-server")
        {
            return Err(io::Error::other("Invalid retained runtime"));
        }
        self.executable = executable;
        Ok(())
    }
    fn spec(&mut self, port: u16) -> io::Result<SpawnSpec> {
        if port == 0 {
            return Err(io::Error::other("Invalid endpoint"));
        }
        let args: Vec<OsString> = vec![
            "--offline".into(),
            "--model".into(),
            self.model_path.as_os_str().into(),
            "--alias".into(),
            self.model_id.as_str().into(),
            "--host".into(),
            "127.0.0.1".into(),
            "--port".into(),
            port.to_string().into(),
            "--ctx-size".into(),
            self.context_tokens.to_string().into(),
            "--threads".into(),
            self.cpu_threads.to_string().into(),
            "--parallel".into(),
            "1".into(),
            "--device".into(),
            "none".into(),
            "--n-gpu-layers".into(),
            "0".into(),
            "--no-webui".into(),
        ];
        SpawnSpec::supervised_inference_root(
            &self.executable,
            args,
            std::mem::take(&mut self.token),
        )
    }
}

/// An already-private worker lane, never a user-selected endpoint or pipe name.
pub(crate) struct DriverLane {
    pub(crate) reader: Box<dyn std::io::Read + Send>,
    pub(crate) writer: Box<dyn std::io::Write + Send>,
}
#[cfg(unix)]
impl DriverLane {
    pub fn from_socket(socket: std::os::unix::net::UnixStream) -> io::Result<Self> {
        Ok(Self {
            reader: Box::new(socket.try_clone()?),
            writer: Box::new(socket),
        })
    }
}
#[cfg(windows)]
impl DriverLane {
    pub fn from_pipe(pipe: crate::windows_private::Duplex) -> Self {
        Self {
            reader: Box::new(pipe.reader),
            writer: Box::new(pipe.writer),
        }
    }
}

/// Trusted host-only proof, not renderer data or a JSON/native flag. Target
/// implementations must bind checks to the exact same-manager root and its
/// complete process set; unobserved/missing evidence returns false.
pub(crate) trait NativeProof: Send + Sync {
    fn tdlib_auth_ready(&self) -> bool {
        self.tdlib_ready()
    }
    fn tdlib_connection_only(&self) -> bool {
        false
    }
    fn prepare_tdlib_auth(&self, cancelled: &dyn Fn() -> bool) -> Option<PathBuf> {
        self.prepare_tdlib(cancelled)
    }
    fn embedded_ready(&self) -> bool {
        false
    }
    fn tdlib_ready(&self) -> bool {
        false
    }
    fn prepare_tdlib(&self, _cancelled: &dyn Fn() -> bool) -> Option<PathBuf> {
        None
    }
    fn inference_ready(&self) -> bool;
    fn authorize_inference(
        &self,
        prepared: &mut PreparedInference,
        cancelled: &dyn Fn() -> bool,
    ) -> bool;
    fn browser_ready(&self) -> bool;
    fn authorize_browser(
        &self,
        browser: &Path,
        driver: &Path,
        cli: &Path,
        profile: &Path,
        cancelled: &dyn Fn() -> bool,
    ) -> bool;
    fn driver_contained(&self, handle: &crate::supervisor::Handle) -> bool;
    fn endpoint_owned(&self, handle: &crate::supervisor::Handle, port: u16) -> bool;
    fn within_resource_limits(&self, handle: &crate::supervisor::Handle) -> bool;
    /// Exact established loopback 4-tuple owned by this worker and runtime.
    fn inference_peer(
        &self,
        worker: &crate::supervisor::Handle,
        runtime: &crate::supervisor::Handle,
        client_port: u16,
        server_port: u16,
    ) -> bool;
}
struct InferenceLease {
    id: String,
    handle: std::sync::Arc<crate::supervisor::Handle>,
    port: u16,
}
// Reviewed from the pinned Playwright Chromium switches. Keep a closed,
// sandboxed visible launch with background services disabled.
fn browser_arguments(profile: &Path) -> Vec<OsString> {
    let mut profile_arg = OsString::from("--user-data-dir=");
    profile_arg.push(profile.as_os_str());
    let args: Vec<OsString> = vec![
        "--remote-debugging-pipe".into(),
        profile_arg,
        "--no-first-run".into(),
        "--no-default-browser-check".into(),
        "--disable-field-trial-config".into(),
        "--disable-background-networking".into(),
        "--disable-breakpad".into(),
        "--disable-component-extensions-with-background-pages".into(),
        "--disable-component-update".into(),
        "--disable-default-apps".into(),
        "--disable-extensions".into(),
        "--disable-sync".into(),
        "--metrics-recording-only".into(),
        "--no-service-autorun".into(),
        "--disable-features=OptimizationHints,NetworkTimeServiceQuerying,AimEnabled,MediaRouter,DialMediaRouteProvider,Translate".into(),
        "about:blank".into(),
    ];
    // The native fixture never uses personal credentials or the real Keychain.
    // These flags are absent from application builds; this test difference is
    // reported and cannot mint a production/account acceptance receipt.
    #[cfg(all(test, target_os = "macos"))]
    let args = {
        let mut fixture_args = args;
        fixture_args.extend([
            "--use-mock-keychain".into(),
            "--password-store=basic".into(),
        ]);
        fixture_args
    };
    args
}

struct BrowserLease {
    id: String,
    browser: Option<crate::supervisor::Handle>,
    driver: Option<crate::supervisor::Handle>,
}
pub(crate) struct NativeBroker {
    embedded: std::sync::Mutex<Option<crate::embedded_threads::EmbeddedAuthority>>,
    embedded_installed: std::sync::atomic::AtomicBool,
    owner: std::sync::Arc<std::sync::Mutex<crate::supervisor::DesktopSupervisor>>,
    parent: std::sync::Arc<crate::supervisor::Handle>,
    catalog: InferenceCatalog,
    proof: std::sync::Arc<dyn NativeProof>,
    cancelled: std::sync::atomic::AtomicBool,
    inference: std::sync::Mutex<Option<InferenceLease>>,
    browser: std::sync::Mutex<Option<BrowserLease>>,
    lane: std::sync::Mutex<Option<DriverLane>>,
    evaluation_only: bool,
    evaluation: std::sync::Mutex<Option<String>>,
    evaluation_busy: std::sync::atomic::AtomicBool,
}
impl NativeBroker {
    pub(crate) fn set_embedded(
        &self,
        host: std::sync::Arc<dyn crate::embedded_threads::EmbeddedCollector>,
    ) -> io::Result<()> {
        let mut slot = self
            .embedded
            .lock()
            .map_err(|_| io::Error::other("Embedded Threads unavailable"))?;
        if slot.is_some() || self.evaluation_only || self.revoked() {
            return Err(io::Error::other("Embedded Threads unavailable"));
        }
        *slot = Some(crate::embedded_threads::EmbeddedAuthority::new(host));
        // Mode is immutable once installed. Cleanup may hold the authority
        // mutex across a bounded release; that is not CDP consumption.
        self.embedded_installed
            .store(true, std::sync::atomic::Ordering::Release);
        Ok(())
    }
    fn embedded_permitted(&self) -> bool {
        !self.evaluation_only
            && !self.revoked()
            && self.proof.embedded_ready()
            && self
                .owner
                .lock()
                .ok()
                .is_some_and(|mut owner| owner.processes.is_live(&self.parent).unwrap_or(false))
    }
    pub(crate) fn embedded_available(&self) -> bool {
        self.embedded_permitted()
            && self
                .embedded
                .lock()
                .ok()
                .is_some_and(|s| s.as_ref().is_some_and(|h| h.available()))
    }
    pub(crate) fn embedded_attach(&self) -> io::Result<Value> {
        if !self.embedded_permitted() {
            return Err(io::Error::other("Embedded Threads unavailable"));
        }
        let slot = self
            .embedded
            .lock()
            .map_err(|_| io::Error::other("Embedded Threads unavailable"))?;
        let host = slot
            .as_ref()
            .ok_or_else(|| io::Error::other("Embedded Threads unavailable"))?;
        let value = host.attach()?;
        if self.revoked() {
            host.cleanup();
            return Err(io::Error::other("Embedded Threads unavailable"));
        }
        Ok(value)
    }
    pub(crate) fn embedded_snapshot(&self, generation: u64) -> io::Result<Value> {
        if !self.embedded_permitted() {
            return Err(io::Error::other("Embedded Threads unavailable"));
        }
        let value = self
            .embedded
            .lock()
            .map_err(|_| io::Error::other("Embedded Threads unavailable"))?
            .as_ref()
            .ok_or_else(|| io::Error::other("Embedded Threads unavailable"))?
            .snapshot(generation)?;
        if !self.embedded_permitted() {
            return Err(io::Error::other("Embedded Threads unavailable"));
        }
        Ok(value)
    }
    pub(crate) fn embedded_scroll(&self, generation: u64) -> io::Result<()> {
        if !self.embedded_permitted() {
            return Err(io::Error::other("Embedded Threads unavailable"));
        }
        self.embedded
            .lock()
            .map_err(|_| io::Error::other("Embedded Threads unavailable"))?
            .as_ref()
            .ok_or_else(|| io::Error::other("Embedded Threads unavailable"))?
            .scroll(generation)?;
        if !self.embedded_permitted() {
            return Err(io::Error::other("Embedded Threads unavailable"));
        }
        Ok(())
    }
    pub(crate) fn embedded_poll(&self, generation: u64) -> bool {
        self.embedded_permitted()
            && self
                .embedded
                .lock()
                .ok()
                .is_some_and(|s| s.as_ref().is_some_and(|h| h.poll(generation)))
    }
    pub(crate) fn embedded_release(
        &self,
        generation: u64,
    ) -> io::Result<crate::supervisor::ShutdownReport> {
        self.embedded
            .lock()
            .map_err(|_| io::Error::other("Embedded Threads unavailable"))?
            .as_ref()
            .ok_or_else(|| io::Error::other("Embedded Threads unavailable"))?
            .release(generation)?;
        Ok(crate::supervisor::ShutdownReport {
            stopped: true,
            remaining: vec![],
        })
    }
    pub(crate) fn automatic_digest(
        &self,
        vault: &dyn crate::credentials::CredentialVault,
    ) -> Option<String> {
        if self.evaluation_only
            || self.revoked()
            || !self.proof.inference_ready()
            || !self.proof.tdlib_ready()
        {
            return None;
        }
        crate::automatic_evidence::verified_digest(&self.catalog.root, vault)
    }
    pub(crate) fn quarantine_telegram_profile(
        &self,
        profile: &str,
        marker: &str,
    ) -> io::Result<Value> {
        if self.evaluation_only || self.revoked() || self.tdlib_purpose() != "connection_only" {
            return Err(io::Error::other("Recovery unavailable"));
        }
        #[cfg(target_os = "macos")]
        {
            crate::telegram_quarantine::quarantine(&self.catalog.root, profile, marker)
        }
        #[cfg(not(target_os = "macos"))]
        {
            let _ = (profile, marker);
            Err(io::Error::other("Recovery unavailable on this platform"))
        }
    }
    pub(crate) fn verify_telegram_quarantine(&self, id: &str) -> io::Result<Value> {
        if self.evaluation_only || self.revoked() {
            return Err(io::Error::other("Recovery unavailable"));
        }
        #[cfg(target_os = "macos")]
        {
            crate::telegram_quarantine::verify(&self.catalog.root, id)
        }
        #[cfg(not(target_os = "macos"))]
        {
            let _ = id;
            Err(io::Error::other("Recovery unavailable on this platform"))
        }
    }
    pub(crate) fn prepare_telegram_login_profile(&self) -> io::Result<()> {
        if self.evaluation_only || self.revoked() || !self.tdlib_auth_ready() {
            #[cfg(debug_assertions)]
            eprintln!("ThreadRelay saved-profile preparation: runtime unavailable");
            return Err(io::Error::other("Login storage unavailable"));
        }
        #[cfg(target_os = "macos")]
        {
            let result = crate::telegram_quarantine::prepare_for_login(&self.catalog.root);
            #[cfg(debug_assertions)]
            if let Err(error) = &result {
                eprintln!("ThreadRelay saved-profile preparation: {error}");
            }
            result
        }
        #[cfg(not(target_os = "macos"))]
        {
            Ok(())
        }
    }
    pub(crate) fn remove_account(
        &self,
        vault: &dyn crate::credentials::CredentialVault,
    ) -> std::io::Result<()> {
        if self.evaluation_only || self.revoked() {
            return Err(std::io::Error::other("Cleanup unavailable"));
        }
        crate::secure_auth::remove_local(vault, &self.catalog.root)
    }
    pub fn tdlib_ready(&self) -> bool {
        !self.evaluation_only && !self.revoked() && self.proof.tdlib_ready()
    }
    pub fn tdlib_auth_ready(&self) -> bool {
        !self.evaluation_only && !self.revoked() && self.proof.tdlib_auth_ready()
    }
    pub fn tdlib_purpose(&self) -> &'static str {
        if self.proof.tdlib_connection_only() {
            "connection_only"
        } else {
            "delivery"
        }
    }
    pub fn prepare_tdlib(&self) -> io::Result<PathBuf> {
        if !self.tdlib_auth_ready() {
            return Err(io::Error::other("TDLib unavailable"));
        }
        self.proof
            .prepare_tdlib_auth(&|| self.revoked())
            .filter(|_| !self.revoked())
            .ok_or_else(|| io::Error::other("TDLib unavailable"))
    }
    pub fn new(
        owner: std::sync::Arc<std::sync::Mutex<crate::supervisor::DesktopSupervisor>>,
        parent: std::sync::Arc<crate::supervisor::Handle>,
        root: &Path,
        resources: &Path,
        proof: std::sync::Arc<dyn NativeProof>,
        lane: Option<DriverLane>,
    ) -> io::Result<Self> {
        Ok(Self {
            embedded: std::sync::Mutex::new(None),
            embedded_installed: std::sync::atomic::AtomicBool::new(false),
            owner,
            parent,
            catalog: InferenceCatalog::new(root, resources)?,
            proof,
            cancelled: std::sync::atomic::AtomicBool::new(false),
            inference: std::sync::Mutex::new(None),
            browser: std::sync::Mutex::new(None),
            lane: std::sync::Mutex::new(lane),
            evaluation_only: false,
            evaluation: std::sync::Mutex::new(None),
            evaluation_busy: std::sync::atomic::AtomicBool::new(false),
        })
    }
    pub fn new_evaluation(
        owner: std::sync::Arc<std::sync::Mutex<crate::supervisor::DesktopSupervisor>>,
        parent: std::sync::Arc<crate::supervisor::Handle>,
        root: &Path,
        resources: &Path,
        proof: std::sync::Arc<dyn NativeProof>,
    ) -> io::Result<Self> {
        let mut result = Self::new(owner, parent, root, resources, proof, None)?;
        result.evaluation_only = true;
        Ok(result)
    }
    pub fn evaluation_only(&self) -> bool {
        self.evaluation_only
    }
    pub fn reserve_evaluation(&self) -> io::Result<String> {
        if !self.evaluation_only || !self.inference_ready() {
            return Err(io::Error::other("Evaluation unavailable"));
        }
        let mut slot = self
            .evaluation
            .lock()
            .map_err(|_| io::Error::other("Evaluation unavailable"))?;
        if slot.is_some() || self.revoked() {
            return Err(io::Error::other("Evaluation unavailable"));
        }
        let live = self
            .owner
            .lock()
            .ok()
            .and_then(|mut owner| owner.processes.is_live(&self.parent).ok())
            .unwrap_or(false);
        if !live {
            return Err(io::Error::other("Evaluation unavailable"));
        }
        let mut random = [0u8; 32];
        getrandom::fill(&mut random).map_err(|_| io::Error::other("Evaluation unavailable"))?;
        let id: String = random.iter().map(|b| format!("{b:02x}")).collect();
        *slot = Some(id.clone());
        Ok(id)
    }
    pub fn poll_evaluation(&self, id: &str) -> bool {
        if !self.evaluation_only || self.revoked() {
            return false;
        }
        let valid = self
            .evaluation
            .lock()
            .ok()
            .is_some_and(|slot| slot.as_deref() == Some(id));
        valid
            && self
                .owner
                .lock()
                .ok()
                .and_then(|mut owner| owner.processes.is_live(&self.parent).ok())
                .unwrap_or(false)
    }
    pub fn sample_evaluation(&self, id: &str) -> Option<(u64, u64)> {
        if !self.evaluation_only || self.revoked() {
            return None;
        }
        let reservation = self.evaluation.try_lock().ok()?;
        if reservation.as_deref() != Some(id) {
            return None;
        }
        let slot = self.inference.try_lock().ok()?;
        let runtime = slot.as_ref().map(|lease| lease.handle.clone());
        let mut owner = self.owner.try_lock().ok()?;
        #[cfg(any(windows, target_os = "macos"))]
        {
            owner
                .processes
                .evaluation_memory(&self.parent, runtime.as_deref())
                .ok()
        }
        #[cfg(not(any(windows, target_os = "macos")))]
        {
            let _ = (&mut owner, runtime);
            None
        }
    }
    pub fn evaluation_hardware(&self, id: &str) -> Option<serde_json::Value> {
        if !self.poll_evaluation(id) {
            return None;
        }
        let lease_id = self.inference.try_lock().ok()?.as_ref()?.id.clone();
        if !self.poll_inference(&lease_id) {
            return None;
        }
        // These values describe the host's fixed CPU-only runtime argv, not a
        // guessed GPU measurement. Native acceptance still verifies that mode.
        Some(
            serde_json::json!({"acceleration_mode":"cpu_only","accelerator_model":"not_applicable_cpu_only",
            "accelerator_memory_bytes":"not_applicable_cpu_only","acceleration_backend":"none_cpu_only","gpu_offload_layers":"0"}),
        )
    }
    pub fn release_evaluation(&self, id: &str) -> bool {
        let mut slot = match self.evaluation.lock() {
            Ok(v) => v,
            Err(_) => return false,
        };
        if slot.as_deref() != Some(id)
            || self
                .evaluation_busy
                .load(std::sync::atomic::Ordering::Acquire)
            || self
                .inference
                .try_lock()
                .map(|v| v.is_some())
                .unwrap_or(true)
        {
            return false;
        }
        *slot = None;
        true
    }
    fn revoked(&self) -> bool {
        self.cancelled.load(std::sync::atomic::Ordering::Acquire)
    }
    pub fn revoke(&self) {
        self.cancelled
            .store(true, std::sync::atomic::Ordering::Release);
    }
    /// Collection cancellation is scoped to worker requests. The verified model
    /// lease belongs to this application generation and survives collection.
    pub fn cancel_collection(&self) {}
    pub fn begin_collection(&self) {}
    pub fn inference_ready(&self) -> bool {
        !self.revoked() && self.proof.inference_ready()
    }
    pub fn cleanup(&self) {
        if let Ok(slot) = self.embedded.try_lock() {
            if let Some(host) = slot.as_ref() {
                host.cleanup();
            }
        }
        let id = self
            .inference
            .try_lock()
            .ok()
            .and_then(|slot| slot.as_ref().map(|lease| lease.id.clone()));
        let deadline = std::time::Instant::now()
            + std::time::Duration::from_millis(crate::supervisor::PROPOSED_SHUTDOWN_MS);
        if let Some(id) = id {
            let _ = self.close_inference_with_budget(
                &id,
                deadline
                    .saturating_duration_since(std::time::Instant::now())
                    .as_millis() as u64,
            );
        }
        let id = self
            .browser
            .try_lock()
            .ok()
            .and_then(|slot| slot.as_ref().map(|lease| lease.id.clone()));
        if let Some(id) = id {
            let _ = self.close_browser_with_budget(
                &id,
                deadline
                    .saturating_duration_since(std::time::Instant::now())
                    .as_millis() as u64,
            );
        }
    }
    pub fn driver_consumed(&self) -> bool {
        if self
            .embedded_installed
            .load(std::sync::atomic::Ordering::Acquire)
        {
            return self.revoked();
        }
        self.revoked()
            || self
                .lane
                .try_lock()
                .map(|lane| lane.is_none())
                .unwrap_or(true)
    }
    pub fn browser_ready(&self) -> bool {
        if self.embedded.lock().ok().is_some_and(|s| s.is_some()) {
            return self.embedded_available();
        }
        if self.evaluation_only || self.revoked() || !self.proof.browser_ready() {
            return false;
        }
        if self.lane.try_lock().is_ok_and(|lane| lane.is_some()) {
            return true;
        }
        // A consumed lane is never reusable, but its live owned lease can
        // satisfy the final readiness check for this generation.
        let id = self
            .browser
            .try_lock()
            .ok()
            .and_then(|slot| slot.as_ref().map(|lease| lease.id.clone()));
        id.is_some_and(|id| self.poll_browser(&id))
    }
    pub fn start_inference(&self, request: InferenceRequest) -> io::Result<serde_json::Value> {
        let cancelled = || self.revoked();
        struct Busy<'a>(&'a std::sync::atomic::AtomicBool);
        impl Drop for Busy<'_> {
            fn drop(&mut self) {
                self.0.store(false, std::sync::atomic::Ordering::Release);
            }
        }
        let _busy = if self.evaluation_only {
            let guard = self
                .evaluation
                .lock()
                .map_err(|_| io::Error::other("Evaluation unavailable"))?;
            if guard.is_none()
                || self
                    .evaluation_busy
                    .swap(true, std::sync::atomic::Ordering::AcqRel)
            {
                return Err(io::Error::other("Evaluation unavailable"));
            }
            drop(guard);
            Some(Busy(&self.evaluation_busy))
        } else {
            None
        };
        let mut prepared = self.catalog.prepare(request)?;
        if cancelled()
            || !self
                .proof
                .authorize_inference(&mut prepared, &|| cancelled())
        {
            return Err(io::Error::other("Inference proof unavailable"));
        }
        let mut slot = self
            .inference
            .lock()
            .map_err(|_| io::Error::other("Inference unavailable"))?;
        if slot.is_some() || cancelled() {
            return Err(io::Error::other("Inference unavailable"));
        }
        let listener = std::net::TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0))?;
        let port = listener.local_addr()?.port();
        drop(listener);
        let spec = prepared.spec(port)?;
        #[cfg(unix)]
        let runtime = crate::runtime_environment::PrivateRuntime::prepare(
            &self.catalog.root,
            crate::runtime_environment::RuntimeKind::Inference,
        )?;
        #[cfg(unix)]
        let spec = spec.with_private_runtime(&runtime);
        let mut random = [0u8; 32];
        getrandom::fill(&mut random).map_err(|_| io::Error::other("Inference unavailable"))?;
        let id: String = random.iter().map(|b| format!("{b:02x}")).collect();
        let mut handle = {
            let mut owner = self
                .owner
                .lock()
                .map_err(|_| io::Error::other("Owner unavailable"))?;
            if cancelled() {
                return Err(io::Error::other("Inference unavailable"));
            }
            owner.processes.spawn_dependency(
                &self.parent,
                crate::supervisor::ProcessKind::Inference,
                &spec,
            )?
        };
        let pipes = handle.take_pipes();
        let handle = std::sync::Arc::new(handle);
        *slot = Some(InferenceLease {
            id: id.clone(),
            handle: handle.clone(),
            port,
        });
        if let Some(pipes) = pipes {
            // Never log either stream; both must be drained to avoid backpressure.
            let mut stdout = pipes.stdout;
            let mut stderr = pipes.stderr;
            std::thread::spawn(move || {
                let _ = std::io::copy(&mut stdout, &mut std::io::sink());
            });
            std::thread::spawn(move || {
                let _ = std::io::copy(&mut stderr, &mut std::io::sink());
            });
            // stdin is dropped: the runtime does not accept host commands there.
        }
        drop(slot);

        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(15);
        let proven = loop {
            let live = self
                .owner
                .lock()
                .ok()
                .and_then(|mut owner| {
                    Some(
                        owner.processes.is_live(&self.parent).ok()?
                            && owner.processes.is_live(&handle).ok()?,
                    )
                })
                .unwrap_or(false);
            if cancelled() || !live {
                break false;
            }
            if self.proof.endpoint_owned(&handle, port)
                && self.proof.within_resource_limits(&handle)
            {
                break true;
            }
            if std::time::Instant::now() >= deadline {
                break false;
            }
            std::thread::sleep(std::time::Duration::from_millis(50));
        };
        if !proven || cancelled() {
            let _ = self.close_inference_with_budget(&id, 0);
            return Err(io::Error::other("Inference proof unavailable"));
        }
        if !self
            .inference
            .lock()
            .ok()
            .is_some_and(|slot| slot.as_ref().is_some_and(|lease| lease.id == id))
        {
            return Err(io::Error::other("Inference unavailable"));
        }
        let platform = if cfg!(all(target_os = "windows", target_arch = "x86_64")) {
            "windows-x64"
        } else if cfg!(all(target_os = "macos", target_arch = "aarch64")) {
            "macos-arm64"
        } else {
            "unsupported"
        };
        let catalog: serde_json::Value = serde_json::from_str(MODEL_CATALOG)
            .map_err(|_| io::Error::other("Invalid built-in catalog"))?;
        let archive = catalog["runtime"]["artifacts"]
            .as_array()
            .and_then(|a| a.iter().find(|v| v["platform"] == platform))
            .and_then(|v| v["sha256"].as_str())
            .unwrap_or("unavailable");
        Ok(
            serde_json::json!({"lease":id,"port":port,"model_id":prepared.model_id,"model_sha256":prepared.model_sha256,"runtime_commit":prepared.runtime_commit,"platform":platform,"archive_sha256":archive,"loaded_model_path":prepared.model_path}),
        )
    }
    pub fn open_browser(self: &std::sync::Arc<Self>) -> io::Result<serde_json::Value> {
        if self.embedded.lock().map_or(true, |s| s.is_some()) {
            return Err(io::Error::other(
                "Embedded Threads requires an explicit visible session",
            ));
        }
        if !self.browser_ready() {
            return Err(io::Error::other("Browser proof unavailable"));
        }
        let mut slot = self
            .browser
            .lock()
            .map_err(|_| io::Error::other("Browser unavailable"))?;
        if slot.is_some() || self.revoked() {
            return Err(io::Error::other("Browser unavailable"));
        }
        let resources = &self.catalog.resources;
        let mut browser = resources.join("native/browser/chrome");
        let mut driver = resources.join("native/playwright/node");
        if cfg!(windows) {
            browser.set_extension("exe");
            driver.set_extension("exe");
        }
        if cfg!(target_os = "macos") {
            browser=resources.join("native/browser/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing");
        }
        let entrypoint = resources.join("native/threadrelay/owned-driver.cjs");
        #[cfg(unix)]
        let runtime = crate::runtime_environment::PrivateRuntime::prepare(
            &self.catalog.root,
            crate::runtime_environment::RuntimeKind::Browser,
        )?;
        #[cfg(unix)]
        let profile = runtime.browser_profile()?.to_owned();
        #[cfg(not(unix))]
        let profile = {
            let parent = self.catalog.root.join("browser-profiles");
            let profile = parent.join("threads");
            for path in [&parent, &profile] {
                match std::fs::create_dir(path) {
                    Ok(()) => (),
                    Err(e) if e.kind() == io::ErrorKind::AlreadyExists => (),
                    Err(e) => return Err(e),
                }
                let meta = std::fs::symlink_metadata(path)?;
                if !meta.is_dir() || meta.file_type().is_symlink() {
                    return Err(io::Error::other("Invalid dedicated profile"));
                }
            }
            profile
        };
        if !self
            .proof
            .authorize_browser(&browser, &driver, &entrypoint, &profile, &|| self.revoked())
            || self.revoked()
        {
            return Err(io::Error::other("Browser proof unavailable"));
        }
        let mut random = [0u8; 32];
        getrandom::fill(&mut random).map_err(|_| io::Error::other("Browser unavailable"))?;
        let id: String = random.iter().map(|b| format!("{b:02x}")).collect();
        let driver_spec = SpawnSpec::supervised_dependency_root(&driver, [entrypoint.as_os_str()]);
        #[cfg(unix)]
        let driver_spec = driver_spec.with_private_runtime(&runtime);
        let mut driver_handle = {
            let mut owner = self
                .owner
                .lock()
                .map_err(|_| io::Error::other("Owner unavailable"))?;
            if self.revoked() {
                return Err(io::Error::other("Browser unavailable"));
            }
            owner.processes.spawn_dependency(
                &self.parent,
                crate::supervisor::ProcessKind::BrowserDriver,
                &driver_spec,
            )?
        };
        let driver_pipes = driver_handle.take_pipes();
        *slot = Some(BrowserLease {
            id: id.clone(),
            browser: None,
            driver: Some(driver_handle),
        });
        let args = browser_arguments(&profile);
        let browser_spec = SpawnSpec::browser_pipe_root(&browser, args);
        #[cfg(unix)]
        let browser_spec = browser_spec.with_private_runtime(&runtime);
        let started = {
            let mut owner = self
                .owner
                .lock()
                .map_err(|_| io::Error::other("Owner unavailable"))?;
            if self.revoked() {
                Err(io::Error::other("Browser unavailable"))
            } else {
                owner.processes.spawn_dependency(
                    &self.parent,
                    crate::supervisor::ProcessKind::Browser,
                    &browser_spec,
                )
            }
        };
        let browser_cdp = match started {
            Ok(mut handle) => {
                let mut pipes = handle.take_pipes();
                let cdp = pipes.as_mut().and_then(|p| p.browser_cdp.take());
                slot.as_mut().expect("retained").browser = Some(handle);
                if let Some(pipes) = pipes {
                    let mut out = pipes.stdout;
                    let mut err = pipes.stderr;
                    std::thread::spawn(move || {
                        let _ = std::io::copy(&mut out, &mut std::io::sink());
                    });
                    std::thread::spawn(move || {
                        let _ = std::io::copy(&mut err, &mut std::io::sink());
                    });
                }
                cdp
            }
            Err(_) => {
                self.revoke();
                drop(slot);
                self.cleanup();
                return Err(io::Error::other("Browser unavailable"));
            }
        };
        let lease = slot.as_ref().expect("retained");
        let browser = lease.browser.as_ref().expect("registered");
        let driver = lease.driver.as_ref().expect("registered");
        let live = self
            .owner
            .lock()
            .ok()
            .and_then(|mut owner| {
                Some(
                    owner.processes.is_live(&self.parent).ok()?
                        && owner.processes.is_live(browser).ok()?
                        && owner.processes.is_live(driver).ok()?,
                )
            })
            .unwrap_or(false);
        if !live || self.revoked() || !self.proof.driver_contained(driver) {
            self.revoke();
            drop(slot);
            self.cleanup();
            return Err(io::Error::other("Browser proof unavailable"));
        }
        let lane = self.lane.lock().ok().and_then(|mut lane| lane.take());
        let (lane, chrome, pipes) = match (lane, browser_cdp, driver_pipes) {
            (Some(lane), Some(chrome), Some(pipes)) => (lane, chrome, pipes),
            _ => {
                self.revoke();
                drop(slot);
                self.cleanup();
                return Err(io::Error::other("Private driver unavailable"));
            }
        };
        let mut errors = pipes.stderr;
        std::thread::spawn(move || {
            let _ = std::io::copy(&mut errors, &mut std::io::sink());
        });
        drop(slot);
        let weak = std::sync::Arc::downgrade(self);
        crate::driver_mux::start(
            lane,
            chrome,
            Box::new(pipes.stdout),
            Box::new(pipes.stdin),
            std::sync::Arc::new(move || {
                if let Some(broker) = weak.upgrade() {
                    broker.revoke();
                    broker.cleanup();
                }
            }),
        );
        if !self.poll_browser(&id) {
            self.revoke();
            self.cleanup();
            return Err(io::Error::other("Browser unavailable"));
        }
        Ok(serde_json::json!({"lease":id,"source_session_id":id}))
    }
    pub fn poll_browser(&self, id: &str) -> bool {
        let slot = match self.browser.try_lock() {
            Ok(v) => v,
            Err(_) => return false,
        };
        let lease = match slot.as_ref() {
            Some(v) if v.id == id => v,
            _ => return false,
        };
        let (browser, driver) = match (&lease.browser, &lease.driver) {
            (Some(b), Some(d)) => (b, d),
            _ => return false,
        };
        let live = self
            .owner
            .try_lock()
            .ok()
            .and_then(|mut owner| {
                Some(
                    owner.processes.is_live(&self.parent).ok()?
                        && owner.processes.is_live(browser).ok()?
                        && owner.processes.is_live(driver).ok()?,
                )
            })
            .unwrap_or(false);
        live && !self.revoked() && self.proof.driver_contained(driver)
    }
    pub fn close_browser(&self, id: &str) -> io::Result<crate::supervisor::ShutdownReport> {
        self.close_browser_with_budget(id, crate::supervisor::PROPOSED_SHUTDOWN_MS)
    }
    fn close_browser_with_budget(
        &self,
        id: &str,
        budget_ms: u64,
    ) -> io::Result<crate::supervisor::ShutdownReport> {
        let mut slot = self
            .browser
            .lock()
            .map_err(|_| io::Error::other("Browser unavailable"))?;
        let lease = slot
            .as_mut()
            .filter(|v| v.id == id)
            .ok_or_else(|| io::Error::other("Unknown lease"))?;
        let deadline = std::time::Instant::now()
            + std::time::Duration::from_millis(
                budget_ms.min(crate::supervisor::PROPOSED_SHUTDOWN_MS),
            );
        let mut remaining = Vec::new();
        for (name, handle) in [
            ("browser", &mut lease.browser),
            ("browser_driver", &mut lease.driver),
        ] {
            if let Some(owned) = handle.as_ref() {
                let budget = deadline
                    .saturating_duration_since(std::time::Instant::now())
                    .as_millis() as u64;
                let report = self
                    .owner
                    .lock()
                    .map_err(|_| io::Error::other("Owner unavailable"))?
                    .processes
                    .close_dependency(owned, budget)?;
                if report.stopped {
                    *handle = None;
                } else {
                    remaining.push(name.to_owned());
                }
            }
        }
        let stopped = remaining.is_empty();
        if stopped {
            *slot = None;
        }
        Ok(crate::supervisor::ShutdownReport { stopped, remaining })
    }

    pub fn poll_inference(&self, id: &str) -> bool {
        let slot = match self.inference.lock() {
            Ok(slot) => slot,
            Err(_) => return false,
        };
        let lease = match slot.as_ref() {
            Some(lease) if lease.id == id => lease,
            _ => return false,
        };
        if self.revoked() {
            return false;
        }
        let live = self
            .owner
            .lock()
            .ok()
            .and_then(|mut owner| owner.processes.is_live(&lease.handle).ok())
            .unwrap_or(false);
        live && self.proof.endpoint_owned(&lease.handle, lease.port)
            && self.proof.within_resource_limits(&lease.handle)
            && !self.revoked()
    }
    pub fn verify_inference_peer(&self, id: &str, client_port: u16, server_port: u16) -> bool {
        if client_port == 0 || server_port == 0 || self.revoked() {
            return false;
        }
        let slot = match self.inference.lock() {
            Ok(v) => v,
            Err(_) => return false,
        };
        let lease = match slot.as_ref() {
            Some(v) if v.id == id && v.port == server_port => v,
            _ => return false,
        };
        let live = self
            .owner
            .lock()
            .ok()
            .and_then(|mut owner| {
                Some(
                    owner.processes.is_live(&self.parent).ok()?
                        && owner.processes.is_live(&lease.handle).ok()?,
                )
            })
            .unwrap_or(false);
        live && self
            .proof
            .inference_peer(&self.parent, &lease.handle, client_port, server_port)
            && !self.revoked()
    }
    pub fn close_inference(&self, id: &str) -> io::Result<crate::supervisor::ShutdownReport> {
        self.close_inference_with_budget(id, crate::supervisor::PROPOSED_SHUTDOWN_MS)
    }
    fn close_inference_with_budget(
        &self,
        id: &str,
        budget_ms: u64,
    ) -> io::Result<crate::supervisor::ShutdownReport> {
        let mut slot = self
            .inference
            .lock()
            .map_err(|_| io::Error::other("Inference unavailable"))?;
        let lease = slot
            .as_ref()
            .filter(|lease| lease.id == id)
            .ok_or_else(|| io::Error::other("Unknown lease"))?;
        let report = self
            .owner
            .lock()
            .map_err(|_| io::Error::other("Owner unavailable"))?
            .processes
            .close_dependency(&lease.handle, budget_ms)?;
        if report.stopped {
            *slot = None;
        }
        Ok(report)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn browser_arguments_keep_private_pipe_and_closed_background_policy() {
        let profile = Path::new("/private-project/owned-profile");
        let args = browser_arguments(profile)
            .into_iter()
            .map(|value| value.into_string().unwrap())
            .collect::<Vec<_>>();
        for required in [
            "--remote-debugging-pipe",
            "--user-data-dir=/private-project/owned-profile",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-sync",
            "--metrics-recording-only",
            "about:blank",
        ] {
            assert_eq!(
                args.iter().filter(|arg| arg.as_str() == required).count(),
                1
            );
        }
        assert!(!args
            .iter()
            .any(|arg| arg.starts_with("--remote-debugging-port")
                || arg == "--no-sandbox"
                || arg == "--disable-web-security"
                || arg == "--ignore-certificate-errors"));
    }

    #[cfg(unix)]
    #[test]
    fn evaluation_reservation_never_grants_browser_and_requires_exclusive_release() {
        use std::sync::Arc;
        struct SyntheticProof;
        impl NativeProof for SyntheticProof {
            fn inference_peer(
                &self,
                _: &crate::supervisor::Handle,
                _: &crate::supervisor::Handle,
                _: u16,
                _: u16,
            ) -> bool {
                true
            }
            fn inference_ready(&self) -> bool {
                true
            }
            fn authorize_inference(&self, _: &mut PreparedInference, _: &dyn Fn() -> bool) -> bool {
                true
            }
            fn browser_ready(&self) -> bool {
                true
            }
            fn authorize_browser(
                &self,
                _: &Path,
                _: &Path,
                _: &Path,
                _: &Path,
                _: &dyn Fn() -> bool,
            ) -> bool {
                true
            }
            fn driver_contained(&self, _: &crate::supervisor::Handle) -> bool {
                true
            }
            fn endpoint_owned(&self, _: &crate::supervisor::Handle, _: u16) -> bool {
                true
            }
            fn within_resource_limits(&self, _: &crate::supervisor::Handle) -> bool {
                true
            }
        }
        let root =
            std::env::temp_dir().join(format!("threadrelay-eval-broker-{}", std::process::id()));
        std::fs::create_dir_all(&root).unwrap();
        let mut supervisor = crate::supervisor::DesktopSupervisor::acquire(&root).unwrap();
        let mut parent = supervisor
            .processes
            .spawn(
                crate::supervisor::ProcessKind::Worker,
                &SpawnSpec::packaged_worker("/bin/sh"),
            )
            .unwrap();
        let pipes = parent.take_pipes().unwrap();
        let parent = Arc::new(parent);
        let owner = Arc::new(std::sync::Mutex::new(supervisor));
        let broker = Arc::new(
            NativeBroker::new_evaluation(
                owner.clone(),
                parent,
                &root,
                &root,
                Arc::new(SyntheticProof),
            )
            .unwrap(),
        );
        assert!(!broker.browser_ready() && broker.open_browser().is_err());
        let id = broker.reserve_evaluation().unwrap();
        assert!(broker.poll_evaluation(&id));
        assert!(broker.reserve_evaluation().is_err());
        assert!(!broker.release_evaluation("foreign"));
        assert!(broker.release_evaluation(&id));
        assert!(!broker.poll_evaluation(&id));
        drop(pipes);
        assert!(owner.lock().unwrap().close_generation(1000).stopped);
        drop(broker);
        drop(owner);
        let _ = std::fs::remove_dir_all(root);
    }

    #[cfg(unix)]
    #[test]
    fn browser_and_driver_are_both_supervised_and_driver_bytes_use_private_lane() {
        use std::{
            io::{Read, Write},
            os::unix::fs::PermissionsExt,
            sync::Arc,
        };
        struct SyntheticProof;
        impl NativeProof for SyntheticProof {
            fn inference_peer(
                &self,
                _: &crate::supervisor::Handle,
                _: &crate::supervisor::Handle,
                _: u16,
                _: u16,
            ) -> bool {
                true
            }
            fn inference_ready(&self) -> bool {
                false
            }
            fn authorize_inference(&self, _: &mut PreparedInference, _: &dyn Fn() -> bool) -> bool {
                false
            }
            fn browser_ready(&self) -> bool {
                true
            }
            fn authorize_browser(
                &self,
                _: &Path,
                _: &Path,
                _: &Path,
                _: &Path,
                _: &dyn Fn() -> bool,
            ) -> bool {
                true
            }
            fn endpoint_owned(&self, _: &crate::supervisor::Handle, _: u16) -> bool {
                true
            }
            fn within_resource_limits(&self, _: &crate::supervisor::Handle) -> bool {
                true
            }
            fn driver_contained(&self, _: &crate::supervisor::Handle) -> bool {
                true
            }
        }
        let root =
            std::env::temp_dir().join(format!("threadrelay-browser-broker-{}", std::process::id()));
        std::fs::create_dir_all(&root).unwrap();
        std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o700)).unwrap();
        let resource = root.join("resources");
        #[cfg(target_os = "macos")]
        let browser = resource.join(
            "native/browser/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        );
        #[cfg(not(target_os = "macos"))]
        let browser = resource.join("native/browser/chrome");
        let driver = resource.join("native/playwright/node");
        for path in [&browser, &driver] {
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        }
        std::fs::write(&browser, "#!/bin/sh\nwhile :; do /bin/sleep 1; done\n").unwrap();
        std::fs::write(&driver, "#!/bin/sh\nexec /bin/cat\n").unwrap();
        for path in [&browser, &driver] {
            std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700)).unwrap();
        }
        let mut owner = crate::supervisor::DesktopSupervisor::acquire(&root).unwrap();
        let mut worker = owner
            .processes
            .spawn(
                crate::supervisor::ProcessKind::Worker,
                &SpawnSpec::packaged_worker("/bin/sh"),
            )
            .unwrap();
        let pipes = worker.take_pipes().unwrap();
        let worker = Arc::new(worker);
        let owner = Arc::new(std::sync::Mutex::new(owner));
        let (lane, mut peer) = std::os::unix::net::UnixStream::pair().unwrap();
        peer.set_read_timeout(Some(std::time::Duration::from_secs(1)))
            .unwrap();
        let broker = Arc::new(
            NativeBroker::new(
                owner.clone(),
                worker.clone(),
                &root,
                &resource,
                Arc::new(SyntheticProof),
                Some(DriverLane::from_socket(lane).unwrap()),
            )
            .unwrap(),
        );
        assert!(broker.browser_ready());
        let result = broker.open_browser().unwrap();
        let id = result["lease"].as_str().unwrap();
        assert!(broker.poll_browser(id));
        assert!(broker.browser_ready());
        peer.write_all(&[2, 0, 0, 0, b'{', b'}']).unwrap();
        let mut bytes = [0; 6];
        peer.read_exact(&mut bytes).unwrap();
        assert_eq!(&bytes, &[2, 0, 0, 0, b'{', b'}']);
        assert!(broker.close_browser(id).unwrap().stopped);
        assert!(!broker.browser_ready());
        assert!(owner.lock().unwrap().processes.is_live(&worker).unwrap());
        drop(peer);
        drop(pipes);
        assert!(owner.lock().unwrap().close_generation(1000).stopped);
        drop(broker);
        drop(owner);
        let _ = std::fs::remove_dir_all(root);
    }

    #[cfg(unix)]
    #[test]
    fn broker_registers_real_fixture_dependency_and_closes_only_that_lease() {
        use std::{os::unix::fs::PermissionsExt, sync::Arc};
        struct SyntheticProof;
        impl NativeProof for SyntheticProof {
            fn inference_peer(
                &self,
                _: &crate::supervisor::Handle,
                _: &crate::supervisor::Handle,
                _: u16,
                _: u16,
            ) -> bool {
                true
            }
            fn inference_ready(&self) -> bool {
                true
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
            fn driver_contained(&self, _: &crate::supervisor::Handle) -> bool {
                false
            }
            fn authorize_inference(&self, _: &mut PreparedInference, _: &dyn Fn() -> bool) -> bool {
                true
            }
            fn endpoint_owned(&self, _: &crate::supervisor::Handle, _: u16) -> bool {
                true
            }
            fn within_resource_limits(&self, _: &crate::supervisor::Handle) -> bool {
                true
            }
        }
        let root = std::env::temp_dir().join(format!("threadrelay-broker-{}", std::process::id()));
        let resource = root.join("resources");
        let executable = resource.join("native/llama/llama-server");
        std::fs::create_dir_all(executable.parent().unwrap()).unwrap();
        std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o700)).unwrap();
        std::fs::write(&executable, "#!/bin/sh\nwhile :; do /bin/sleep 1; done\n").unwrap();
        std::fs::set_permissions(&executable, std::fs::Permissions::from_mode(0o700)).unwrap();
        let mut owner = crate::supervisor::DesktopSupervisor::acquire(&root).unwrap();
        let mut worker = owner
            .processes
            .spawn(
                crate::supervisor::ProcessKind::Worker,
                &SpawnSpec::packaged_worker("/bin/sh"),
            )
            .unwrap();
        let pipes = worker.take_pipes().unwrap();
        let worker = Arc::new(worker);
        let owner = Arc::new(std::sync::Mutex::new(owner));
        let broker = NativeBroker::new(
            owner.clone(),
            worker.clone(),
            &root,
            &resource,
            Arc::new(SyntheticProof),
            None,
        )
        .unwrap();
        let mut random = [0u8; 32];
        getrandom::fill(&mut random).unwrap();
        let token: String = random.iter().map(|b| format!("{b:02x}")).collect();
        let result = broker
            .start_inference(InferenceRequest {
                model_id: "h2o-lightning-4b-q4-k-m".into(),
                context_tokens: 4096,
                cpu_threads: 4,
                token,
            })
            .unwrap();
        let id = result["lease"].as_str().unwrap();
        assert!(broker.poll_inference(id));
        assert!(!broker.poll_inference("foreign"));
        assert!(broker.close_inference(id).unwrap().stopped);
        assert!(owner.lock().unwrap().processes.is_live(&worker).unwrap());
        drop(pipes);
        assert!(owner.lock().unwrap().close_generation(1000).stopped);
        drop(broker);
        drop(owner);
        let _ = std::fs::remove_dir_all(root);
    }

    #[cfg(unix)]
    #[test]
    fn broker_revocation_interrupts_owned_host_verification_before_spawn() {
        use std::sync::{
            atomic::{AtomicBool, Ordering},
            Arc,
        };
        struct WaitingProof(Arc<AtomicBool>);
        impl NativeProof for WaitingProof {
            fn inference_ready(&self) -> bool {
                true
            }
            fn authorize_inference(
                &self,
                _: &mut PreparedInference,
                cancelled: &dyn Fn() -> bool,
            ) -> bool {
                self.0.store(true, Ordering::Release);
                let deadline = std::time::Instant::now() + std::time::Duration::from_secs(3);
                while !cancelled() && std::time::Instant::now() < deadline {
                    std::thread::sleep(std::time::Duration::from_millis(2));
                }
                false
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
            fn driver_contained(&self, _: &crate::supervisor::Handle) -> bool {
                false
            }
            fn endpoint_owned(&self, _: &crate::supervisor::Handle, _: u16) -> bool {
                false
            }
            fn within_resource_limits(&self, _: &crate::supervisor::Handle) -> bool {
                false
            }
            fn inference_peer(
                &self,
                _: &crate::supervisor::Handle,
                _: &crate::supervisor::Handle,
                _: u16,
                _: u16,
            ) -> bool {
                false
            }
        }
        let root =
            std::env::temp_dir().join(format!("threadrelay-host-cancel-{}", std::process::id()));
        std::fs::create_dir_all(&root).unwrap();
        let mut supervisor = crate::supervisor::DesktopSupervisor::acquire(&root).unwrap();
        let mut worker = supervisor
            .processes
            .spawn(
                crate::supervisor::ProcessKind::Worker,
                &SpawnSpec::packaged_worker("/bin/sh"),
            )
            .unwrap();
        let pipes = worker.take_pipes().unwrap();
        let worker = Arc::new(worker);
        let owner = Arc::new(std::sync::Mutex::new(supervisor));
        let entered = Arc::new(AtomicBool::new(false));
        let broker = Arc::new(
            NativeBroker::new(
                owner.clone(),
                worker.clone(),
                &root,
                &root,
                Arc::new(WaitingProof(entered.clone())),
                None,
            )
            .unwrap(),
        );
        let background = broker.clone();
        let thread = std::thread::spawn(move || {
            background.start_inference(InferenceRequest {
                model_id: "h2o-lightning-4b-q4-k-m".into(),
                context_tokens: 4096,
                cpu_threads: 4,
                token: "a".repeat(64),
            })
        });
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(2);
        while !entered.load(Ordering::Acquire) && std::time::Instant::now() < deadline {
            std::thread::sleep(std::time::Duration::from_millis(2));
        }
        assert!(entered.load(Ordering::Acquire));
        broker.revoke();
        assert!(thread.join().unwrap().is_err());
        assert!(broker.inference.lock().unwrap().is_none());
        drop(pipes);
        assert!(owner.lock().unwrap().close_generation(1000).stopped);
        drop(broker);
        drop(owner);
        let _ = std::fs::remove_dir_all(root);
    }

    #[cfg(unix)]
    #[test]
    fn installed_embedded_mode_never_consumes_cdp_lane_during_mutex_contention() {
        use std::sync::Arc;
        struct Denied;
        impl NativeProof for Denied {
            fn inference_ready(&self) -> bool {
                false
            }
            fn authorize_inference(&self, _: &mut PreparedInference, _: &dyn Fn() -> bool) -> bool {
                false
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
            fn driver_contained(&self, _: &crate::supervisor::Handle) -> bool {
                false
            }
            fn endpoint_owned(&self, _: &crate::supervisor::Handle, _: u16) -> bool {
                false
            }
            fn within_resource_limits(&self, _: &crate::supervisor::Handle) -> bool {
                false
            }
            fn inference_peer(
                &self,
                _: &crate::supervisor::Handle,
                _: &crate::supervisor::Handle,
                _: u16,
                _: u16,
            ) -> bool {
                false
            }
        }
        impl crate::embedded_threads::EmbeddedCollector for Denied {
            fn available(&self) -> bool {
                false
            }
            fn attach(&self) -> io::Result<u64> {
                Err(io::Error::other("fixture unavailable"))
            }
            fn snapshot(&self, _: u64) -> io::Result<serde_json::Value> {
                Err(io::Error::other("fixture unavailable"))
            }
            fn scroll(&self, _: u64) -> io::Result<()> {
                Err(io::Error::other("fixture unavailable"))
            }
            fn poll(&self, _: u64) -> bool {
                false
            }
            fn release(&self, _: u64) -> io::Result<()> {
                Ok(())
            }
            fn close_all(&self) -> io::Result<()> {
                Ok(())
            }
        }
        let root =
            std::env::temp_dir().join(format!("threadrelay-embedded-mode-{}", std::process::id()));
        std::fs::create_dir_all(&root).unwrap();
        let mut supervisor = crate::supervisor::DesktopSupervisor::acquire(&root).unwrap();
        let mut worker = supervisor
            .processes
            .spawn(
                crate::supervisor::ProcessKind::Worker,
                &SpawnSpec::packaged_worker("/bin/sh"),
            )
            .unwrap();
        let pipes = worker.take_pipes().unwrap();
        let worker = Arc::new(worker);
        let owner = Arc::new(std::sync::Mutex::new(supervisor));
        let broker =
            NativeBroker::new(owner.clone(), worker, &root, &root, Arc::new(Denied), None).unwrap();
        assert!(
            broker.driver_consumed(),
            "legacy missing CDP lane must remain consumed"
        );
        broker.set_embedded(Arc::new(Denied)).unwrap();
        let held = broker.embedded.lock().unwrap();
        assert!(
            !broker.driver_consumed(),
            "embedded cleanup contention must not recycle the account worker"
        );
        drop(held);
        broker.revoke();
        assert!(broker.driver_consumed());
        drop(pipes);
        assert!(owner.lock().unwrap().close_generation(1000).stopped);
        drop(broker);
        drop(owner);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn collection_cancel_keeps_model_activation_until_application_revocation() {
        use std::sync::{
            atomic::{AtomicBool, Ordering},
            Arc,
        };
        struct WaitingProof(Arc<AtomicBool>);
        impl NativeProof for WaitingProof {
            fn tdlib_auth_ready(&self) -> bool {
                true
            }
            fn inference_ready(&self) -> bool {
                true
            }
            fn authorize_inference(
                &self,
                _: &mut PreparedInference,
                cancelled: &dyn Fn() -> bool,
            ) -> bool {
                self.0.store(true, Ordering::Release);
                let deadline = std::time::Instant::now() + std::time::Duration::from_secs(3);
                while !cancelled() && std::time::Instant::now() < deadline {
                    std::thread::sleep(std::time::Duration::from_millis(2));
                }
                false
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
            fn driver_contained(&self, _: &crate::supervisor::Handle) -> bool {
                false
            }
            fn endpoint_owned(&self, _: &crate::supervisor::Handle, _: u16) -> bool {
                false
            }
            fn within_resource_limits(&self, _: &crate::supervisor::Handle) -> bool {
                false
            }
            fn inference_peer(
                &self,
                _: &crate::supervisor::Handle,
                _: &crate::supervisor::Handle,
                _: u16,
                _: u16,
            ) -> bool {
                false
            }
        }
        let root = std::env::temp_dir().join(format!(
            "threadrelay-collection-cancel-{}",
            std::process::id()
        ));
        std::fs::create_dir_all(&root).unwrap();
        let mut supervisor = crate::supervisor::DesktopSupervisor::acquire(&root).unwrap();
        let mut worker = supervisor
            .processes
            .spawn(
                crate::supervisor::ProcessKind::Worker,
                &SpawnSpec::packaged_worker("/bin/sh"),
            )
            .unwrap();
        let pipes = worker.take_pipes().unwrap();
        let worker = Arc::new(worker);
        let owner = Arc::new(std::sync::Mutex::new(supervisor));
        let entered = Arc::new(AtomicBool::new(false));
        let broker = Arc::new(
            NativeBroker::new(
                owner.clone(),
                worker.clone(),
                &root,
                &root,
                Arc::new(WaitingProof(entered.clone())),
                None,
            )
            .unwrap(),
        );
        let background = broker.clone();
        let thread = std::thread::spawn(move || {
            background.start_inference(InferenceRequest {
                model_id: "h2o-lightning-4b-q4-k-m".into(),
                context_tokens: 4096,
                cpu_threads: 4,
                token: "a".repeat(64),
            })
        });
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(2);
        while !entered.load(Ordering::Acquire) && std::time::Instant::now() < deadline {
            std::thread::sleep(std::time::Duration::from_millis(2));
        }
        assert!(entered.load(Ordering::Acquire));
        assert!(broker.tdlib_auth_ready());
        let cancelled_at = std::time::Instant::now();
        broker.cancel_collection();
        // Reopening a new collection intent before the old verifier observes
        // cancellation must never revive that verifier's captured epoch.
        broker.begin_collection();
        assert!(broker.tdlib_auth_ready());
        assert!(!broker.revoked());
        std::thread::sleep(std::time::Duration::from_millis(30));
        assert!(
            !thread.is_finished(),
            "collection cancellation ended app model activation"
        );
        broker.revoke();
        assert!(thread.join().unwrap().is_err());
        assert!(
            cancelled_at.elapsed() < std::time::Duration::from_millis(500),
            "application revocation did not end the verifier"
        );
        assert!(broker.inference.lock().unwrap().is_none());
        drop(pipes);
        assert!(owner.lock().unwrap().close_generation(1000).stopped);
        drop(broker);
        drop(owner);
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn only_catalog_model_and_bounded_fixed_options_are_accepted() {
        let catalog = InferenceCatalog::new(
            std::path::Path::new("/trusted/data"),
            std::path::Path::new("/trusted/resources"),
        )
        .unwrap();
        let mut token = [0; 32];
        getrandom::fill(&mut token).unwrap();
        let token: String = token.iter().map(|b| format!("{b:02x}")).collect();
        let request = InferenceRequest {
            model_id: "h2o-lightning-4b-q4-k-m".into(),
            context_tokens: 4096,
            cpu_threads: 4,
            token: token.clone(),
        };
        let prepared = catalog.prepare(request).unwrap();
        assert_eq!(prepared.model_id, "h2o-lightning-4b-q4-k-m");
        assert!(prepared
            .model_path
            .ends_with("models/h2o-lightning-4b-Q4_K_M.gguf"));
        let invalid = InferenceRequest {
            model_id: "/tmp/other".into(),
            context_tokens: 4096,
            cpu_threads: 4,
            token,
        };
        assert!(catalog.prepare(invalid).is_err());
    }
}
