//! Private generation-scoped host control; never available to renderer IPC.
use crate::credentials::{CredentialError, CredentialVault, SecretBytes};
use std::time::{Duration, Instant};
use std::{
    io::{Read, Write},
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Arc, Condvar, Mutex,
    },
};

pub struct HostControl {
    #[cfg(unix)]
    socket: std::os::unix::net::UnixStream,
    armed: Arc<AtomicBool>,
    revoked: Arc<AtomicBool>,
    preparation: Arc<AccountPreparation>,
}

// One atomic word binds arming and terminal observations to the same attempt.
// A late read from an earlier recovery cannot cancel a newly authorized one.
#[derive(Default)]
struct RecoveryScope(AtomicU64);
impl RecoveryScope {
    fn arm(&self) -> Option<u64> {
        self.0
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |v| {
                v.checked_add(4).map(|n| (n & !3) | 1)
            })
            .ok()
            .map(|previous| (previous + 4) >> 2)
    }
    fn cancel(&self) {
        if self
            .0
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |v| {
                v.checked_add(4).map(|n| n & !3)
            })
            .is_err()
        {
            self.0.fetch_and(!3, Ordering::AcqRel);
        }
    }
    fn consume(&self) -> bool {
        self.0.fetch_and(!1, Ordering::AcqRel) & 1 != 0
    }
    fn snapshot(&self) -> (u64, bool) {
        let v = self.0.load(Ordering::Acquire);
        (v >> 2, v & 2 != 0)
    }
    fn started(&self, ticket: u64) {
        let _ = self
            .0
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |v| {
                if v >> 2 == ticket {
                    Some(v | 2)
                } else {
                    None
                }
            });
    }
    fn finish(&self, ticket: u64) {
        let _ = self
            .0
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |v| {
                if v >> 2 == ticket {
                    Some(v & !3)
                } else {
                    None
                }
            });
    }
}
struct PreparationState {
    ticket: u64,
    reading: bool,
    required: bool,
    ready: Option<crate::secure_auth::AccountSecret>,
    error: Option<CredentialError>,
}
struct AccountPreparation {
    recovery_scope: RecoveryScope,
    vault: Arc<dyn CredentialVault + Send + Sync>,
    revoked: Arc<AtomicBool>,
    state: Mutex<PreparationState>,
    changed: Condvar,
}
struct InteractiveVault(Arc<dyn CredentialVault + Send + Sync>);
impl CredentialVault for InteractiveVault {
    fn put(&self, _: &str, _: &[u8]) -> Result<(), CredentialError> {
        Err(CredentialError::InvalidInput)
    }
    fn get(&self, reference: &str) -> Result<SecretBytes, CredentialError> {
        self.0.get_interactive(reference)
    }
    fn delete(&self, _: &str) -> Result<(), CredentialError> {
        Err(CredentialError::InvalidInput)
    }
}
impl AccountPreparation {
    fn new(vault: Arc<dyn CredentialVault + Send + Sync>, revoked: Arc<AtomicBool>) -> Self {
        Self {
            recovery_scope: RecoveryScope::default(),
            vault,
            revoked,
            state: Mutex::new(PreparationState {
                ticket: 0,
                reading: false,
                required: false,
                ready: None,
                error: None,
            }),
            changed: Condvar::new(),
        }
    }
    fn cancel(&self) {
        self.recovery_scope.cancel();
        let mut state = self.state.lock().unwrap_or_else(|error| error.into_inner());
        state.ticket = state.ticket.wrapping_add(1);
        state.required = true;
        state.ready = None;
        state.error = Some(CredentialError::Unavailable);
        self.changed.notify_all();
    }
    fn require_prepared(&self) {
        self.state
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .required = true;
    }
    fn allow_passive(&self) -> bool {
        let mut state = self.state.lock().unwrap_or_else(|error| error.into_inner());
        if state.reading || state.ready.is_some() {
            return false;
        }
        state.required = false;
        true
    }
    fn take(&self) -> Result<Option<crate::secure_auth::AccountSecret>, CredentialError> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| CredentialError::Unavailable)?;
        if self.revoked.load(Ordering::Acquire) {
            return Err(CredentialError::Unavailable);
        }
        if let Some(secret) = state.ready.take() {
            return Ok(Some(secret));
        }
        if state.required || state.reading {
            return Err(CredentialError::Unavailable);
        }
        Ok(None)
    }
    fn prepare(self: &Arc<Self>, cancelled: impl Fn() -> bool) -> Result<(), CredentialError> {
        if cancelled() || self.revoked.load(Ordering::Acquire) {
            return Err(CredentialError::Unavailable);
        }
        let ticket = {
            let mut state = self
                .state
                .lock()
                .map_err(|_| CredentialError::Unavailable)?;
            if state.reading || state.ready.is_some() {
                return Err(CredentialError::Unavailable);
            }
            state.ticket = state.ticket.wrapping_add(1);
            state.required = true;
            state.reading = true;
            state.error = None;
            state.ticket
        };
        let owner = self.clone();
        let launched = std::thread::Builder::new()
            .name("threadrelay-account-authorization".into())
            .spawn(move || {
                let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    crate::secure_auth::resolve_account(&InteractiveVault(owner.vault.clone()))
                }))
                .unwrap_or(Err(CredentialError::Unavailable));
                let mut state = owner
                    .state
                    .lock()
                    .unwrap_or_else(|error| error.into_inner());
                state.reading = false;
                if state.ticket == ticket && !owner.revoked.load(Ordering::Acquire) {
                    match result {
                        Ok(secret) => state.ready = Some(secret),
                        Err(error) => state.error = Some(error),
                    }
                }
                // A cancelled generation never adopts a late result. AccountSecret
                // owns SecretBytes, so dropping it wipes this copy of the record.
                owner.changed.notify_all();
            });
        if launched.is_err() {
            let mut state = self.state.lock().unwrap_or_else(|error| error.into_inner());
            state.reading = false;
            state.error = Some(CredentialError::Unavailable);
            self.changed.notify_all();
            return Err(CredentialError::Unavailable);
        }
        let deadline = Instant::now() + crate::credentials::INTERACTIVE_AUTH_TIMEOUT;
        loop {
            if cancelled() || self.revoked.load(Ordering::Acquire) || Instant::now() >= deadline {
                self.cancel();
                return Err(CredentialError::Unavailable);
            }
            let state = self
                .state
                .lock()
                .map_err(|_| CredentialError::Unavailable)?;
            if state.ticket != ticket {
                return Err(CredentialError::Unavailable);
            }
            if state.ready.is_some() {
                return Ok(());
            }
            if let Some(error) = state.error {
                return Err(error);
            }
            let _ = self
                .changed
                .wait_timeout(state, Duration::from_millis(50))
                .map_err(|_| CredentialError::Unavailable)?;
        }
    }
}
impl HostControl {
    /// Explicit owner authorization happens before a worker command is queued,
    /// keeping its normal reads and lifecycle cancellation responsive.
    pub(crate) fn prepare_account(
        &self,
        cancelled: impl Fn() -> bool,
    ) -> Result<(), CredentialError> {
        self.preparation.prepare(cancelled)
    }
    pub(crate) fn cancel_account_preparation(&self) {
        self.armed.store(false, Ordering::Release);
        self.preparation.cancel();
    }
    pub(crate) fn arm_for_command(
        &self,
        command: &serde_json::Value,
        delivery_ready: bool,
    ) -> (u64, bool) {
        if command["type"] == "BeginTelegramRecovery" && !self.revoked.load(Ordering::Acquire) {
            self.cancel_account_preparation();
            if let Some(ticket) = self.preparation.recovery_scope.arm() {
                return (ticket, false);
            }
        } else if command["type"] == "BeginTelegramLogin" {
            self.preparation.require_prepared();
            self.arm();
        } else if command["type"] == "RestoreTelegramSession" && self.preparation.allow_passive() {
            self.arm();
        } else if command["type"] == "Start"
            && command["payload"]["mode"] != "dry_run"
            && delivery_ready
            && self.preparation.allow_passive()
        {
            self.arm();
        }
        self.recovery_observation()
    }
    pub(crate) fn recovery_observation(&self) -> (u64, bool) {
        self.preparation.recovery_scope.snapshot()
    }
    pub(crate) fn recovery_started(&self, ticket: u64) {
        self.preparation.recovery_scope.started(ticket);
    }
    pub(crate) fn finish_recovery(&self, ticket: u64) {
        self.preparation.recovery_scope.finish(ticket);
    }
    pub(crate) fn disarm_recovery(&self) {
        self.preparation.recovery_scope.cancel();
    }
    pub fn arm(&self) {
        if !self.revoked.load(Ordering::Acquire) {
            self.armed.store(true, Ordering::Release);
            if self.revoked.load(Ordering::Acquire) {
                self.disarm();
            }
        }
    }
    pub fn disarm(&self) {
        self.cancel_account_preparation();
    }
    pub fn revoke(&self) {
        self.revoked.store(true, Ordering::Release);
        self.disarm();
        #[cfg(unix)]
        let _ = self.socket.shutdown(std::net::Shutdown::Both);
    }
}
impl Drop for HostControl {
    fn drop(&mut self) {
        self.revoke();
    }
}

#[cfg(unix)]
pub(crate) fn start(
    socket: std::os::unix::net::UnixStream,
    generation: u64,
    vault: Arc<dyn CredentialVault + Send + Sync>,
    broker: Option<Arc<crate::native_broker::NativeBroker>>,
) -> std::io::Result<HostControl> {
    let revoker = socket.try_clone()?;
    socket.set_write_timeout(Some(Duration::from_secs(5)))?;
    let closer = socket.try_clone()?;
    let (armed, revoked, preparation) = launch(socket, generation, vault, broker, move || {
        let _ = closer.shutdown(std::net::Shutdown::Both);
    });
    Ok(HostControl {
        socket: revoker,
        armed,
        revoked,
        preparation,
    })
}

#[cfg(windows)]
pub(crate) fn start_windows(
    stream: crate::windows_private::Duplex,
    generation: u64,
    vault: Arc<dyn CredentialVault + Send + Sync>,
    broker: Option<Arc<crate::native_broker::NativeBroker>>,
) -> HostControl {
    let (armed, revoked, preparation) = launch(stream, generation, vault, broker, || {});
    HostControl {
        armed,
        revoked,
        preparation,
    }
}
fn launch<S: Read + Write + Send + 'static>(
    mut stream: S,
    generation: u64,
    vault: Arc<dyn CredentialVault + Send + Sync>,
    broker: Option<Arc<crate::native_broker::NativeBroker>>,
    on_exit: impl FnOnce() + Send + 'static,
) -> (Arc<AtomicBool>, Arc<AtomicBool>, Arc<AccountPreparation>) {
    let armed = Arc::new(AtomicBool::new(false));
    let permitted = armed.clone();
    let revoked = Arc::new(AtomicBool::new(false));
    let thread_revoked = revoked.clone();
    let preparation = Arc::new(AccountPreparation::new(vault.clone(), revoked.clone()));
    let resolver = preparation.clone();
    std::thread::spawn(move || {
        loop {
            let mut header = [0u8; 4];
            if stream.read_exact(&mut header).is_err() {
                break;
            }
            let size = u32::from_be_bytes(header) as usize;
            if !(1..=8192).contains(&size) {
                break;
            }
            let mut framed = vec![0; size + 4];
            framed[..4].copy_from_slice(&header);
            if stream.read_exact(&mut framed[4..]).is_err() {
                break;
            }
            let parsed = crate::ipc::read_frame(&mut std::io::Cursor::new(&framed));
            for byte in &mut framed {
                unsafe {
                    std::ptr::write_volatile(byte, 0);
                }
            }
            let mut request = match parsed {
                Ok(Some(v)) => v,
                _ => break,
            };
            if request["generation"].as_u64() != Some(generation) {
                break;
            }
            let kind = request["type"].as_str().unwrap_or("").to_owned();
            let count = request.as_object().map(|o| o.len()).unwrap_or(0);
            let mut message = match kind.as_str() {
                "ResolveAccount" if count == 2 => {
                    let resolved = if broker.as_ref().is_some_and(|b| b.evaluation_only())
                        || !permitted.swap(false, Ordering::AcqRel)
                    {
                        Err(CredentialError::Unavailable)
                    } else {
                        resolver.take().and_then(|prepared| match prepared {
                            Some(secret) => Ok(secret),
                            None => crate::secure_auth::resolve_account(&*vault),
                        })
                    };
                    if thread_revoked.load(Ordering::Acquire) {
                        break;
                    }
                    match resolved {
                        Ok(secret) => {
                            let mut key: String =
                                secret.key().iter().map(|b| format!("{b:02x}")).collect();
                            let value = serde_json::json!({"type":"AccountCredentials","generation":generation,
                                "api_id":secret.api_id(),"api_hash":secret.hash(),"database_key":key});
                            crate::secure_auth::wipe_string(&mut key);
                            value
                        }
                        Err(_) => {
                            serde_json::json!({"type":"AccountUnavailable","generation":generation})
                        }
                    }
                }
                "Capabilities" if count == 2 => {
                    serde_json::json!({"type":"Capabilities","generation":generation,
                    "browser":broker.as_ref().is_some_and(|b|b.browser_ready()),"inference":broker.as_ref().is_some_and(|b|b.inference_ready()),"telegram":broker.as_ref().is_some_and(|b|b.tdlib_ready()),"telegram_auth":broker.as_ref().is_some_and(|b|b.tdlib_auth_ready()),"automatic":broker.as_ref().and_then(|b|b.automatic_digest(&*vault)).is_some()})
                }
                "EmbeddedThreadsAvailable" if count == 2 => {
                    serde_json::json!({"type":"EmbeddedThreadsAvailability","generation":generation,
                        "available":broker.as_ref().is_some_and(|b|b.embedded_available())})
                }
                "AttachEmbeddedThreads" if count == 2 => {
                    match broker.as_ref().and_then(|b| b.embedded_attach().ok()) {
                        Some(mut value) => {
                            value["type"] = "EmbeddedThreadsAttached".into();
                            value["generation"] = generation.into();
                            value
                        }
                        None => serde_json::json!({"type":"ControlError","generation":generation}),
                    }
                }
                "SnapshotEmbeddedThreads"
                | "ScrollEmbeddedThreads"
                | "PollEmbeddedThreads"
                | "ReleaseEmbeddedThreads"
                    if count == 3 =>
                {
                    let view = request["webview_generation"].as_u64().filter(|v| *v > 0);
                    match (broker.as_ref(), view) {
                        (Some(broker), Some(view)) => match kind.as_str() {
                            "SnapshotEmbeddedThreads" => match broker.embedded_snapshot(view) {
                                Ok(payload) => {
                                    serde_json::json!({"type":"EmbeddedThreadsSnapshot","generation":generation,"webview_generation":view,"payload":payload})
                                }
                                Err(_) => {
                                    serde_json::json!({"type":"ControlError","generation":generation})
                                }
                            },
                            "ScrollEmbeddedThreads" => match broker.embedded_scroll(view) {
                                Ok(()) => {
                                    serde_json::json!({"type":"EmbeddedThreadsScrolled","generation":generation,"webview_generation":view})
                                }
                                Err(_) => {
                                    serde_json::json!({"type":"ControlError","generation":generation})
                                }
                            },
                            "PollEmbeddedThreads" => {
                                serde_json::json!({"type":"EmbeddedThreadsStatus","generation":generation,"webview_generation":view,"active":broker.embedded_poll(view)})
                            }
                            _ => match broker.embedded_release(view) {
                                Ok(report) => {
                                    serde_json::json!({"type":"EmbeddedThreadsReleased","generation":generation,"webview_generation":view,"report":report})
                                }
                                Err(_) => {
                                    serde_json::json!({"type":"ControlError","generation":generation})
                                }
                            },
                        },
                        _ => serde_json::json!({"type":"ControlError","generation":generation}),
                    }
                }
                "AutomaticEvidence" if count == 2 => {
                    serde_json::json!({"type":"AutomaticEvidence","generation":generation,
                        "digest":broker.as_ref().and_then(|b|b.automatic_digest(&*vault))})
                }
                "QuarantineTelegramProfile"
                    if count == 4 && !thread_revoked.load(Ordering::Acquire) =>
                {
                    let result = if resolver.recovery_scope.consume() {
                        match (
                            request["expected_profile_id"].as_str(),
                            request["expected_marker_sha256"].as_str(),
                            broker.as_ref(),
                        ) {
                            (Some(profile), Some(marker), Some(b)) => {
                                b.quarantine_telegram_profile(profile, marker).ok()
                            }
                            _ => None,
                        }
                    } else {
                        None
                    };
                    match result {
                        Some(receipt) => {
                            serde_json::json!({"type":"TelegramProfileQuarantined","generation":generation,"receipt":receipt})
                        }
                        None => serde_json::json!({"type":"ControlError","generation":generation}),
                    }
                }
                "VerifyTelegramQuarantine"
                    if count == 3 && !thread_revoked.load(Ordering::Acquire) =>
                {
                    let result = match (request["recovery_id"].as_str(), broker.as_ref()) {
                        (Some(id), Some(b)) => b.verify_telegram_quarantine(id).ok(),
                        _ => None,
                    };
                    match result {
                        Some(receipt) => {
                            serde_json::json!({"type":"TelegramQuarantineVerified","generation":generation,"receipt":receipt})
                        }
                        None => serde_json::json!({"type":"ControlError","generation":generation}),
                    }
                }
                "RemoveLocalAccount" if count == 2 && !thread_revoked.load(Ordering::Acquire) => {
                    let removed = broker
                        .as_ref()
                        .is_some_and(|b| b.remove_account(&*vault).is_ok());
                    serde_json::json!({"type":"AccountRemoved","generation":generation,"removed":removed})
                }
                "PreparePinnedTdlib" if count == 2 && !thread_revoked.load(Ordering::Acquire) => {
                    match broker.as_ref().and_then(|b| b.prepare_tdlib().ok()) {
                        Some(path) if !thread_revoked.load(Ordering::Acquire) => {
                            serde_json::json!({"type":"TdlibPrepared","generation":generation,"library":path,"purpose":broker.as_ref().unwrap().tdlib_purpose()})
                        }
                        _ => serde_json::json!({"type":"Unavailable","generation":generation}),
                    }
                }
                "StartPinnedInference" if count == 6 => {
                    let model = request["model_id"].as_str().map(str::to_owned);
                    let context = request["context_tokens"]
                        .as_u64()
                        .and_then(|v| u32::try_from(v).ok());
                    let threads = request["cpu_threads"]
                        .as_u64()
                        .and_then(|v| u32::try_from(v).ok());
                    let token = request.as_object_mut().and_then(|o| o.remove("auth_token"));
                    let result = match (broker.as_ref(), model, context, threads, token) {
                        (
                            Some(broker),
                            Some(model_id),
                            Some(context_tokens),
                            Some(cpu_threads),
                            Some(serde_json::Value::String(token)),
                        ) => broker.start_inference(crate::native_broker::InferenceRequest {
                            model_id,
                            context_tokens,
                            cpu_threads,
                            token,
                        }),
                        (_, _, _, _, Some(serde_json::Value::String(mut token))) => {
                            crate::secure_auth::wipe_string(&mut token);
                            Err(std::io::Error::other("Unavailable"))
                        }
                        _ => Err(std::io::Error::other("Unavailable")),
                    };
                    match result {
                        Ok(mut value) => {
                            value["type"] = "InferenceStarted".into();
                            value["generation"] = generation.into();
                            value
                        }
                        Err(_) => {
                            serde_json::json!({"type":"ControlError","generation":generation})
                        }
                    }
                }
                "ReserveEvaluation" if count == 2 => {
                    match broker.as_ref().and_then(|b| b.reserve_evaluation().ok()) {
                        Some(id) => {
                            serde_json::json!({"type":"EvaluationReserved","generation":generation,"reservation":id})
                        }
                        None => serde_json::json!({"type":"ControlError","generation":generation}),
                    }
                }
                "PollEvaluation" if count == 3 => {
                    let id = request["reservation"].as_str().unwrap_or("");
                    serde_json::json!({"type":"EvaluationStatus","generation":generation,"reservation":id,
                        "active":broker.as_ref().is_some_and(|b|b.poll_evaluation(id))})
                }
                "EvaluationHardware" if count == 3 => {
                    let id = request["reservation"].as_str().unwrap_or("");
                    match broker.as_ref().and_then(|b| b.evaluation_hardware(id)) {
                        Some(hardware) => {
                            serde_json::json!({"type":"EvaluationHardware","generation":generation,"reservation":id,"hardware":hardware})
                        }
                        None => {
                            serde_json::json!({"type":"ObservationUnavailable","generation":generation,"reservation":id})
                        }
                    }
                }
                "ReleaseEvaluation" if count == 3 => {
                    let id = request["reservation"].as_str().unwrap_or("");
                    serde_json::json!({"type":"EvaluationReleased","generation":generation,"reservation":id,
                        "released":broker.as_ref().is_some_and(|b|b.release_evaluation(id))})
                }
                "OpenDedicatedBrowser" if count == 2 => {
                    match broker.as_ref().and_then(|b| b.open_browser().ok()) {
                        Some(mut value) => {
                            value["type"] = "BrowserOpened".into();
                            value["generation"] = generation.into();
                            value
                        }
                        None => serde_json::json!({"type":"ControlError","generation":generation}),
                    }
                }
                "PollBrowser" if count == 3 => {
                    let id = request["lease"].as_str().unwrap_or("");
                    let active = broker.as_ref().is_some_and(|b| b.poll_browser(id));
                    serde_json::json!({"type":"BrowserStatus","generation":generation,"lease":id,"active":active})
                }
                "CloseBrowser" if count == 3 => {
                    let id = request["lease"].as_str().unwrap_or("");
                    match broker.as_ref().and_then(|b| b.close_browser(id).ok()) {
                        Some(report) => {
                            serde_json::json!({"type":"BrowserClosed","generation":generation,"lease":id,"report":report})
                        }
                        None => serde_json::json!({"type":"ControlError","generation":generation}),
                    }
                }
                "VerifyInferencePeer" if count == 5 => {
                    let id = request["lease"].as_str().unwrap_or("");
                    let client = request["client_port"]
                        .as_u64()
                        .and_then(|v| u16::try_from(v).ok());
                    let server = request["server_port"]
                        .as_u64()
                        .and_then(|v| u16::try_from(v).ok());
                    let verified = match (broker.as_ref(), client, server) {
                        (Some(broker), Some(client), Some(server)) => {
                            broker.verify_inference_peer(id, client, server)
                        }
                        _ => false,
                    };
                    serde_json::json!({"type":"InferencePeer","generation":generation,"lease":id,"verified":verified})
                }
                "PollInference" if count == 3 => {
                    let id = request["lease"].as_str().unwrap_or("");
                    let active = broker.as_ref().is_some_and(|b| b.poll_inference(id));
                    serde_json::json!({"type":"InferenceStatus","generation":generation,"lease":id,"active":active})
                }
                "CloseInference" if count == 3 => {
                    let id = request["lease"].as_str().unwrap_or("");
                    match broker.as_ref().and_then(|b| b.close_inference(id).ok()) {
                        Some(report) => {
                            serde_json::json!({"type":"InferenceClosed","generation":generation,"lease":id,"report":report})
                        }
                        None => serde_json::json!({"type":"ControlError","generation":generation}),
                    }
                }
                _ => {
                    if let Some(serde_json::Value::String(token)) = request.get_mut("auth_token") {
                        crate::secure_auth::wipe_string(token);
                    }
                    break;
                }
            };
            if thread_revoked.load(Ordering::Acquire) {
                break;
            }
            let encoded = crate::ipc::encode_frame(&message);
            for field in ["api_hash", "database_key"] {
                if let Some(serde_json::Value::String(value)) = message.get_mut(field) {
                    crate::secure_auth::wipe_string(value);
                }
            }
            drop(message);
            let mut frame = match encoded {
                Ok(v) => v,
                Err(_) => break,
            };
            let limit = if kind == "SnapshotEmbeddedThreads" {
                crate::ipc::MAX_FRAME_BYTES
            } else {
                8192
            };
            if frame.len() > limit + 4 {
                break;
            }
            let result = stream.write_all(&frame).and_then(|_| stream.flush());
            for b in &mut frame {
                unsafe {
                    std::ptr::write_volatile(b, 0);
                }
            }
            std::sync::atomic::compiler_fence(Ordering::SeqCst);
            if result.is_err() {
                break;
            }
        }
        thread_revoked.store(true, Ordering::Release);
        permitted.store(false, Ordering::Release);
        resolver.cancel();
        on_exit();
        if let Some(broker) = broker {
            broker.revoke();
            broker.cleanup();
        }
    });
    (armed, revoked, preparation)
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use crate::credentials::{CredentialError, CredentialVault, SecretBytes};
    use std::io::Write;
    struct Vault;
    impl CredentialVault for Vault {
        fn put(&self, _: &str, _: &[u8]) -> Result<(), CredentialError> {
            unreachable!()
        }
        fn get(&self, _: &str) -> Result<SecretBytes, CredentialError> {
            let mut bytes = b"TR1".to_vec();
            bytes.extend_from_slice(&123u32.to_be_bytes());
            let mut random = [0u8; 48];
            getrandom::fill(&mut random).unwrap();
            for byte in &random[..16] {
                bytes.extend_from_slice(format!("{byte:02x}").as_bytes());
            }
            bytes.extend_from_slice(&random[16..]);
            Ok(SecretBytes::for_test(bytes))
        }
        fn delete(&self, _: &str) -> Result<(), CredentialError> {
            unreachable!()
        }
    }
    struct WaitingVault {
        entered: std::sync::mpsc::SyncSender<()>,
        release: std::sync::Mutex<std::sync::mpsc::Receiver<()>>,
        reads: std::sync::atomic::AtomicUsize,
    }
    impl CredentialVault for WaitingVault {
        fn put(&self, _: &str, _: &[u8]) -> Result<(), CredentialError> {
            unreachable!()
        }
        fn get(&self, _: &str) -> Result<SecretBytes, CredentialError> {
            panic!("interactive preparation must never use passive get")
        }
        fn get_interactive(&self, reference: &str) -> Result<SecretBytes, CredentialError> {
            assert_eq!(reference, "vault:threadrelay_account");
            self.reads.fetch_add(1, Ordering::AcqRel);
            self.entered.send(()).unwrap();
            self.release.lock().unwrap().recv().unwrap();
            Vault.get(reference)
        }
        fn delete(&self, _: &str) -> Result<(), CredentialError> {
            unreachable!()
        }
    }
    #[test]
    fn interactive_preparation_keeps_control_reads_live_and_supplies_exactly_one_begin() {
        let (entered, began) = std::sync::mpsc::sync_channel(1);
        let (release, blocked) = std::sync::mpsc::sync_channel(1);
        let vault = Arc::new(WaitingVault {
            entered,
            release: std::sync::Mutex::new(blocked),
            reads: std::sync::atomic::AtomicUsize::new(0),
        });
        let (host, mut worker) = std::os::unix::net::UnixStream::pair().unwrap();
        worker
            .set_read_timeout(Some(std::time::Duration::from_secs(1)))
            .unwrap();
        let owner = Arc::new(start(host, 7, vault.clone(), None).unwrap());
        let preparing = owner.clone();
        let pending = std::thread::spawn(move || preparing.prepare_account(|| false));
        began
            .recv_timeout(std::time::Duration::from_secs(1))
            .unwrap();
        let start = std::time::Instant::now();
        worker
            .write_all(
                &crate::ipc::encode_frame(
                    &serde_json::json!({"type":"Capabilities","generation":7}),
                )
                .unwrap(),
            )
            .unwrap();
        assert_eq!(
            crate::ipc::read_frame(&mut worker).unwrap().unwrap()["type"],
            "Capabilities"
        );
        assert!(start.elapsed() < std::time::Duration::from_millis(250));
        release.send(()).unwrap();
        assert_eq!(pending.join().unwrap(), Ok(()));
        owner.arm_for_command(&serde_json::json!({"type":"BeginTelegramLogin"}), false);
        let request =
            crate::ipc::encode_frame(&serde_json::json!({"type":"ResolveAccount","generation":7}))
                .unwrap();
        worker.write_all(&request).unwrap();
        assert_eq!(
            crate::ipc::read_frame(&mut worker).unwrap().unwrap()["type"],
            "AccountCredentials"
        );
        worker.write_all(&request).unwrap();
        assert_eq!(
            crate::ipc::read_frame(&mut worker).unwrap().unwrap(),
            serde_json::json!({"type":"AccountUnavailable","generation":7})
        );
        assert_eq!(vault.reads.load(Ordering::Acquire), 1);
    }

    #[test]
    fn cancelling_interactive_preparation_returns_promptly_and_discards_late_credentials() {
        let (entered, began) = std::sync::mpsc::sync_channel(1);
        let (release, blocked) = std::sync::mpsc::sync_channel(1);
        let vault = Arc::new(WaitingVault {
            entered,
            release: std::sync::Mutex::new(blocked),
            reads: std::sync::atomic::AtomicUsize::new(0),
        });
        let (host, mut worker) = std::os::unix::net::UnixStream::pair().unwrap();
        worker
            .set_read_timeout(Some(std::time::Duration::from_secs(1)))
            .unwrap();
        let owner = Arc::new(start(host, 7, vault.clone(), None).unwrap());
        let preparing = owner.clone();
        let (finished, result) = std::sync::mpsc::sync_channel(1);
        let pending = std::thread::spawn(move || {
            finished.send(preparing.prepare_account(|| false)).unwrap();
        });
        began
            .recv_timeout(std::time::Duration::from_secs(1))
            .unwrap();
        owner.cancel_account_preparation();
        assert_eq!(
            result
                .recv_timeout(std::time::Duration::from_millis(250))
                .unwrap(),
            Err(CredentialError::Unavailable)
        );
        assert_eq!(
            owner.prepare_account(|| false),
            Err(CredentialError::Unavailable)
        );
        assert_eq!(vault.reads.load(Ordering::Acquire), 1);
        release.send(()).unwrap();
        pending.join().unwrap();
        // A cancelled preparation can never silently fall back to a vault get.
        owner.arm_for_command(&serde_json::json!({"type":"BeginTelegramLogin"}), false);
        worker
            .write_all(
                &crate::ipc::encode_frame(
                    &serde_json::json!({"type":"ResolveAccount","generation":7}),
                )
                .unwrap(),
            )
            .unwrap();
        assert_eq!(
            crate::ipc::read_frame(&mut worker).unwrap().unwrap(),
            serde_json::json!({"type":"AccountUnavailable","generation":7})
        );
        assert_eq!(vault.reads.load(Ordering::Acquire), 1);
    }
    #[test]
    fn lifecycle_cancellation_predicate_does_not_wait_for_the_os_dialog() {
        let (entered, began) = std::sync::mpsc::sync_channel(1);
        let (release, blocked) = std::sync::mpsc::sync_channel(1);
        let vault = Arc::new(WaitingVault {
            entered,
            release: std::sync::Mutex::new(blocked),
            reads: std::sync::atomic::AtomicUsize::new(0),
        });
        let (host, _worker) = std::os::unix::net::UnixStream::pair().unwrap();
        let owner = Arc::new(start(host, 7, vault, None).unwrap());
        let cancelled = Arc::new(AtomicBool::new(false));
        let observing = cancelled.clone();
        let preparing = owner.clone();
        let (finished, result) = std::sync::mpsc::sync_channel(1);
        let pending = std::thread::spawn(move || {
            finished
                .send(preparing.prepare_account(|| observing.load(Ordering::Acquire)))
                .unwrap();
        });
        began
            .recv_timeout(std::time::Duration::from_secs(1))
            .unwrap();
        cancelled.store(true, Ordering::Release);
        assert_eq!(
            result
                .recv_timeout(std::time::Duration::from_millis(250))
                .unwrap(),
            Err(CredentialError::Unavailable)
        );
        release.send(()).unwrap();
        pending.join().unwrap();
        assert!(owner.preparation.take().is_err());
    }
    #[test]
    fn cancelled_ready_preparation_denies_resolve_without_closing_collection_control() {
        let (host, mut worker) = std::os::unix::net::UnixStream::pair().unwrap();
        worker
            .set_read_timeout(Some(Duration::from_secs(1)))
            .unwrap();
        let owner = start(host, 7, Arc::new(Vault), None).unwrap();
        owner.prepare_account(|| false).unwrap();
        owner.arm_for_command(&serde_json::json!({"type":"BeginTelegramLogin"}), false);
        owner.cancel_account_preparation();
        for _ in 0..2 {
            worker
                .write_all(
                    &crate::ipc::encode_frame(
                        &serde_json::json!({"type":"ResolveAccount","generation":7}),
                    )
                    .unwrap(),
                )
                .unwrap();
            assert_eq!(
                crate::ipc::read_frame(&mut worker).unwrap().unwrap(),
                serde_json::json!({"type":"AccountUnavailable","generation":7})
            );
            worker
                .write_all(
                    &crate::ipc::encode_frame(
                        &serde_json::json!({"type":"Capabilities","generation":7}),
                    )
                    .unwrap(),
                )
                .unwrap();
            assert_eq!(
                crate::ipc::read_frame(&mut worker).unwrap().unwrap()["type"],
                "Capabilities"
            );
        }
        assert!(!owner.revoked.load(Ordering::Acquire));
    }
    #[test]
    fn malformed_or_unknown_generation_requests_still_close_private_control() {
        for request in [
            serde_json::json!({"type":"ResolveAccount","generation":8}),
            serde_json::json!({"type":"ResolveAccount","generation":7,"extra":true}),
            serde_json::json!({"type":"UnknownControlRequest","generation":7}),
        ] {
            let (host, mut worker) = std::os::unix::net::UnixStream::pair().unwrap();
            worker
                .set_read_timeout(Some(Duration::from_secs(1)))
                .unwrap();
            let _owner = start(host, 7, Arc::new(Vault), None).unwrap();
            worker
                .write_all(&crate::ipc::encode_frame(&request).unwrap())
                .unwrap();
            assert!(crate::ipc::read_frame(&mut worker).unwrap().is_none());
        }
    }
    #[test]
    fn capabilities_are_inert_and_closed_without_a_native_broker() {
        let (host, mut worker) = std::os::unix::net::UnixStream::pair().unwrap();
        worker
            .set_read_timeout(Some(std::time::Duration::from_secs(1)))
            .unwrap();
        let _owner = start(host, 7, std::sync::Arc::new(Vault), None).unwrap();
        worker
            .write_all(
                &crate::ipc::encode_frame(
                    &serde_json::json!({"type":"Capabilities","generation":7}),
                )
                .unwrap(),
            )
            .unwrap();
        let value = crate::ipc::read_frame(&mut worker).unwrap().unwrap();
        assert_eq!(value["type"], "Capabilities");
        assert_eq!(value["inference"], false);
        assert_eq!(value["browser"], false);
    }

    #[test]
    fn stale_recovery_queries_cannot_disarm_or_publish_into_a_new_scope() {
        let scope = RecoveryScope::default();
        let first = scope.arm().unwrap();
        scope.started(first);
        let second = scope.arm().unwrap();
        let (_, started) = scope.snapshot();
        assert!(!started);
        assert_ne!(first, second);
        scope.finish(first);
        scope.started(first);
        assert_eq!(scope.snapshot(), (second, false));
        assert!(scope.consume());
        scope.cancel();
        let third = scope.arm().unwrap();
        scope.started(second);
        scope.finish(second);
        assert_eq!(scope.snapshot(), (third, false));
        assert!(scope.consume());
    }
    #[test]
    fn arming_returns_its_own_ticket_even_when_another_command_rearms_before_observation() {
        let (host, _worker) = std::os::unix::net::UnixStream::pair().unwrap();
        let control = start(host, 7, std::sync::Arc::new(Vault), None).unwrap();
        let command = serde_json::json!({"type":"BeginTelegramRecovery"});
        let (first, _) = control.arm_for_command(&command, false);
        let (second, _) = control.arm_for_command(&command, false);
        control.finish_recovery(first);
        control.recovery_started(first);
        assert_eq!(control.recovery_observation(), (second, false));
        assert!(control.preparation.recovery_scope.consume());
    }
    #[test]
    fn recovery_scope_is_separate_one_shot_and_revocable_without_reading_credentials() {
        let (host, _worker) = std::os::unix::net::UnixStream::pair().unwrap();
        let control = start(host, 7, std::sync::Arc::new(Vault), None).unwrap();
        control.arm_for_command(&serde_json::json!({"type":"BeginTelegramRecovery"}), false);
        assert!(control.preparation.recovery_scope.consume());
        assert!(!control.preparation.recovery_scope.consume());
        assert!(!control.armed.load(Ordering::Acquire));
        control.arm_for_command(&serde_json::json!({"type":"BeginTelegramRecovery"}), false);
        control.disarm_recovery();
        assert!(!control.preparation.recovery_scope.consume());
        control.arm_for_command(&serde_json::json!({"type":"BeginTelegramRecovery"}), false);
        control.revoke();
        assert!(!control.preparation.recovery_scope.consume());
    }
    #[test]
    fn guest_review_never_arms_credentials_and_delivery_start_preserves_legacy_scope() {
        let (host, _worker) = std::os::unix::net::UnixStream::pair().unwrap();
        let control = start(host, 7, std::sync::Arc::new(Vault), None).unwrap();
        let command =
            |kind: &str, mode: &str| serde_json::json!({"type":kind,"payload":{"mode":mode}});
        control.arm_for_command(&command("Start", "review"), false);
        assert!(
            !control.armed.load(Ordering::Acquire),
            "guest Review must not grant credential access"
        );
        control.arm_for_command(&command("Start", "dry_run"), true);
        assert!(!control.armed.load(Ordering::Acquire));
        control.arm_for_command(&command("RestoreTelegramSession", ""), false);
        assert!(
            control.armed.load(Ordering::Acquire),
            "saved restore grants one passive credential read"
        );
        control.disarm();
        control.arm_for_command(&command("BeginTelegramLogin", ""), false);
        assert!(control.armed.load(Ordering::Acquire));
        control.arm_for_command(&command("Start", "review"), false);
        assert!(
            control.armed.load(Ordering::Acquire),
            "guest Review must preserve explicit pending login permission"
        );
        control.disarm();
        for mode in ["review", "auto"] {
            control.arm_for_command(&command("Start", mode), true);
            assert!(
                control.armed.load(Ordering::Acquire),
                "accepted delivery Start must keep credential bootstrap"
            );
            control.disarm();
        }
    }

    #[test]
    fn only_armed_current_generation_gets_one_fixed_account() {
        let (host, mut worker) = std::os::unix::net::UnixStream::pair().unwrap();
        worker
            .set_read_timeout(Some(std::time::Duration::from_secs(1)))
            .unwrap();
        let owner = start(host, 7, std::sync::Arc::new(Vault), None).unwrap();
        owner.arm();
        let request =
            crate::ipc::encode_frame(&serde_json::json!({"type":"ResolveAccount","generation":7}))
                .unwrap();
        worker.write_all(&request).unwrap();
        let value = crate::ipc::read_frame(&mut worker).unwrap().unwrap();
        assert_eq!(value["type"], "AccountCredentials");
        assert_eq!(value["generation"], 7);
        worker.write_all(&request).unwrap();
        assert_eq!(
            crate::ipc::read_frame(&mut worker).unwrap().unwrap(),
            serde_json::json!({"type":"AccountUnavailable","generation":7})
        );
        owner.revoke();
    }
}
