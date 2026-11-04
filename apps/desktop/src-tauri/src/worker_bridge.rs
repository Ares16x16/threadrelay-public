//! Bounded, single-owner private framed worker connection.
//! A failed/expired exchange permanently revokes this bridge. No operation is replayed.
use serde_json::Value;
use std::{
    io::{Read, Write},
    sync::{
        atomic::{AtomicBool, AtomicU64, AtomicU8, Ordering},
        mpsc::{self, SyncSender, TrySendError},
        Arc,
    },
    time::{Duration, Instant},
};

// Routes mounts three independent reads alongside state observation. Queue
// their short burst while keeping memory and total admitted work bounded.
const MAX_PENDING_REQUESTS: usize = 8;
// With the normal 5s exchange budget, one isolated sample gets at most120s
// for pinned-model verification, native startup, classification and cleanup.
// Explicit login prepares native code and resolves credentials over separate
// bounded private exchanges; its outer budget must contain those exchanges.
// Start delegates preparation to Coordinator; ordinary reads stay short.
const SAMPLE_TIMEOUT_MULTIPLIER: u32 = 24;
const LOGIN_TIMEOUT_MULTIPLIER: u32 = 4;
const REQUEST_QUEUED: u8 = 0;
const REQUEST_STARTED: u8 = 1;
const REQUEST_CANCELLED: u8 = 2;

struct Request {
    command: Value,
    reply: SyncSender<Result<Value, &'static str>>,
    started: Option<SyncSender<Instant>>,
    state: Arc<AtomicU8>,
    sample_slot: Option<Arc<AtomicBool>>,
}
impl Drop for Request {
    fn drop(&mut self) {
        if let Some(slot) = &self.sample_slot {
            slot.store(false, Ordering::Release);
        }
    }
}

pub struct WorkerBridge {
    requests: SyncSender<Request>,
    revoked: Arc<AtomicBool>,
    timeout: Duration,
    sample_slot: Arc<AtomicBool>,
}

fn expected(command: &str, event: &Value) -> bool {
    let kind = event["type"].as_str().unwrap_or("");
    if kind == "Error" {
        return true;
    }
    let wanted = match command {
        "QueryState" | "Start" | "Pause" | "Stop" | "SuspendCollection" | "StopCollection" => {
            "State"
        }
        "QueryAppStatus" => "AppStatus",
        "QueryActivity" => "Activity",
        "QueryTelegramTest" => "TelegramTestState",
        "BeginTelegramRecovery" | "QueryTelegramRecovery" => "TelegramRecoveryState",
        "PreviewManualHistorySend" => "ManualHistoryPreview",
        "QueryManualHistorySend" => "ManualHistorySendState",
        "QueryTelegramDestinations" => "TelegramDestinations",
        "QuerySavedTelegramDestinations" => "SavedTelegramDestinations",
        "QueryTopics" => "Topics",
        "QueryRoutes" => "Routes",
        "QueryRouteQueue" => "RouteQueue",
        "QueryHistory" => "History",
        "QueryReview" => "Review",
        "QueryCollection" => "Collection",
        "QueryArchivePosts" => "ArchivePosts",
        "QueryArchiveRuns" => "ArchiveRuns",
        "QueryArchiveDeliveries" => "ArchiveDeliveries",
        "ReadArchiveDetail" => "ArchiveDetailChunk",
        "QueryDeliveryWorkspace" => "DeliveryWorkspace",
        "SaveMessageTemplate" => "MessageTemplate",
        "ConfigureDeliveryDraft" | "GetDeliveryDraft" => "DeliveryDraft",
        "PreviewDeliveryDraft" => "DeliveryPreview",
        "ApproveDeliveryDraft" | "QueryDeliveryJobs" => "DeliveryJobs",
        "PauseDeliveryJob" | "ResumeDeliveryJob" | "CancelDeliveryJob" => "DeliveryJob",
        "QueryAccount" => "Account",
        "QueryReadiness" => "Readiness",
        "QueryDestination" => "DestinationStatus",
        "QueryRetention" => "Retention",
        "QueryAutoStatus" => "AutoStatus",
        "TestSample" => "Sample",
        "BulkApproveReview" | "BulkDismissReview" => "BulkReview",
        "PreviewAutoRoute" => "AutoRoutePreview",
        _ => "Acknowledged",
    };
    kind == wanted && (kind != "Acknowledged" || event["payload"]["operation"] == command)
}

impl WorkerBridge {
    pub fn new<W: Write + Send + 'static, R: Read + Send + 'static>(
        mut input: W,
        mut output: R,
        timeout: Duration,
    ) -> Self {
        let (requests, receiver) = mpsc::sync_channel::<Request>(MAX_PENDING_REQUESTS);
        let revoked = Arc::new(AtomicBool::new(false));
        let thread_revoked = revoked.clone();
        std::thread::spawn(move || {
            while let Ok(request) = receiver.recv() {
                if thread_revoked.load(Ordering::Acquire) {
                    break;
                }
                // An undispatched caller can abandon its queue slot without
                // revoking healthy work or executing an unobserved mutation.
                if request
                    .state
                    .compare_exchange(
                        REQUEST_QUEUED,
                        REQUEST_STARTED,
                        Ordering::AcqRel,
                        Ordering::Acquire,
                    )
                    .is_err()
                {
                    continue;
                }
                if request
                    .started
                    .as_ref()
                    .is_some_and(|started| started.send(Instant::now()).is_err())
                {
                    continue;
                }
                let result = (|| {
                    let bytes = crate::ipc::encode_frame(&request.command)
                        .map_err(|_| "Invalid request")?;
                    input
                        .write_all(&bytes)
                        .and_then(|_| input.flush())
                        .map_err(|_| "Worker connection lost")?;
                    let event = crate::ipc::read_frame(&mut output)
                        .map_err(|_| "Worker response invalid")?
                        .ok_or("Worker connection lost")?;
                    crate::ipc::validate_contract("Event", &event)
                        .map_err(|_| "Worker response invalid")?;
                    if event["id"] != request.command["id"]
                        || !expected(request.command["type"].as_str().unwrap_or(""), &event)
                    {
                        return Err("Worker response mismatch");
                    }
                    Ok(event)
                })();
                if result.is_err() {
                    thread_revoked.store(true, Ordering::Release);
                }
                let _ = request.reply.send(result);
                if thread_revoked.load(Ordering::Acquire) {
                    break;
                }
            }
        });
        Self {
            requests,
            revoked,
            timeout,
            sample_slot: Arc::new(AtomicBool::new(false)),
        }
    }

    pub fn request(&self, command: Value) -> Result<Value, &'static str> {
        crate::ipc::validate_contract("Command", &command).map_err(|_| "Invalid request")?;
        crate::ipc::encode_frame(&command).map_err(|_| "Invalid request")?;
        if self.revoked.load(Ordering::Acquire) {
            return Err("Worker connection unavailable");
        }
        let kind = command["type"].as_str().unwrap_or("");
        let lifecycle = matches!(
            kind,
            "Pause" | "Stop" | "SuspendCollection" | "StopCollection"
        );
        let sample = kind == "TestSample";
        if sample
            && self
                .sample_slot
                .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
                .is_err()
        {
            return Err("Worker busy");
        }
        let execution_budget = if sample {
            self.timeout.saturating_mul(SAMPLE_TIMEOUT_MULTIPLIER)
        } else if kind == "BeginTelegramLogin" {
            self.timeout.saturating_mul(LOGIN_TIMEOUT_MULTIPLIER)
        } else {
            self.timeout
        };
        let admitted = Instant::now();
        let (reply, received) = mpsc::sync_channel(1);
        let (started, starting) = mpsc::sync_channel(1);
        let state = Arc::new(AtomicU8::new(REQUEST_QUEUED));
        match self.requests.try_send(Request {
            command,
            reply,
            started: Some(started),
            state: state.clone(),
            sample_slot: sample.then(|| self.sample_slot.clone()),
        }) {
            Ok(()) => {}
            Err(TrySendError::Full(_)) => {
                if lifecycle {
                    self.revoke();
                    return Err("Worker connection unavailable");
                }
                return Err("Worker busy");
            }
            Err(TrySendError::Disconnected(_)) => {
                self.revoked.store(true, Ordering::Release);
                return Err("Worker connection unavailable");
            }
        }
        // Ordinary reads queued behind a healthy sample have not begun a
        // worker exchange yet. Their own short deadline starts on dispatch.
        // Only one sample is admitted, so queue waiting also has a fixed cap.
        let queue_budget = if lifecycle {
            self.timeout
        } else {
            self.timeout
                .saturating_mul(SAMPLE_TIMEOUT_MULTIPLIER + MAX_PENDING_REQUESTS as u32)
        };
        let began = match starting.recv_timeout(queue_budget) {
            Ok(began) => began,
            Err(mpsc::RecvTimeoutError::Timeout) => {
                if state
                    .compare_exchange(
                        REQUEST_QUEUED,
                        REQUEST_CANCELLED,
                        Ordering::AcqRel,
                        Ordering::Acquire,
                    )
                    .is_ok()
                {
                    if lifecycle {
                        self.revoke();
                        return Err("Worker connection unavailable");
                    }
                    return Err("Worker busy");
                }
                // Dispatch won the race with queue cancellation. It may have
                // written already, so keep correlation or revoke; never retry.
                let remaining = if lifecycle {
                    (admitted + self.timeout).saturating_duration_since(Instant::now())
                } else {
                    self.timeout
                };
                match starting.recv_timeout(remaining) {
                    Ok(began) => began,
                    Err(_) => {
                        self.revoke();
                        return Err("Worker connection unavailable");
                    }
                }
            }
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                self.revoke();
                return Err("Worker connection unavailable");
            }
        };
        let deadline = if lifecycle {
            admitted + self.timeout
        } else {
            began + execution_budget
        };
        match received.recv_timeout(deadline.saturating_duration_since(Instant::now())) {
            Ok(Ok(event)) if !self.revoked.load(Ordering::Acquire) => Ok(event),
            Ok(Err(error)) => Err(error),
            _ => {
                self.revoked.store(true, Ordering::Release);
                Err("Worker connection unavailable")
            }
        }
    }
    pub fn revoke(&self) {
        self.revoked.store(true, Ordering::Release);
    }
}

use crate::supervisor::{DesktopSupervisor, Handle, ProcessKind, SpawnSpec, PROPOSED_SHUTDOWN_MS};
use std::{
    path::{Path, PathBuf},
    sync::Mutex,
};

struct ActiveGeneration {
    number: u64,
    bridge: Arc<WorkerBridge>,
    // Retain the same-manager opaque parent capability for future dependency brokers.
    _worker: Arc<Handle>,
    #[cfg(any(unix, windows))]
    broker: Arc<crate::native_broker::NativeBroker>,
    #[cfg(any(unix, windows))]
    private: Arc<PrivatePipe>,
    #[cfg(any(unix, windows))]
    control: Arc<crate::host_control::HostControl>,
}
#[cfg(unix)]
struct PrivatePipe {
    socket: std::os::unix::net::UnixStream,
    serial: Mutex<()>,
}
#[cfg(unix)]
impl PrivatePipe {
    fn revoke(&self) {
        let _ = self.socket.shutdown(std::net::Shutdown::Both);
    }
    fn submit(
        &self,
        generation: u64,
        epoch: u64,
        challenge: &str,
        kind: &str,
        mut value: String,
    ) -> Result<bool, &'static str> {
        if !matches!(kind, "phone" | "code" | "password")
            || challenge.is_empty()
            || challenge.len() > 128
            || value.is_empty()
            || value.len() > 4096
        {
            crate::secure_auth::wipe_string(&mut value);
            return Err("Private input unavailable");
        }
        let _guard = match self.serial.try_lock() {
            Ok(guard) => guard,
            Err(_) => {
                crate::secure_auth::wipe_string(&mut value);
                return Err("Private input busy");
            }
        };
        let mut random = [0u8; 32];
        if getrandom::fill(&mut random).is_err() {
            crate::secure_auth::wipe_string(&mut value);
            return Err("Private input unavailable");
        }
        let handle: String = random.iter().map(|b| format!("{b:02x}")).collect();
        let mut message = serde_json::json!({"type":"SubmitSecret","generation":generation,"epoch":epoch,
            "challenge_id":challenge,"kind":kind,"handle":handle,"value":value});
        let encoded = crate::ipc::encode_frame(&message);
        if let Some(Value::String(secret)) = message.get_mut("value") {
            unsafe {
                for b in secret.as_bytes_mut() {
                    std::ptr::write_volatile(b, 0);
                }
            }
        }
        drop(message);
        let mut frame = encoded.map_err(|_| "Private input unavailable")?;
        let mut stream = &self.socket;
        let written = stream.write_all(&frame).and_then(|_| stream.flush());
        for b in &mut frame {
            unsafe {
                std::ptr::write_volatile(b, 0);
            }
        }
        std::sync::atomic::compiler_fence(Ordering::SeqCst);
        written.map_err(|_| "Private connection lost")?;
        let mut header = [0; 4];
        stream
            .read_exact(&mut header)
            .map_err(|_| "Private connection lost")?;
        let size = u32::from_be_bytes(header) as usize;
        if !(1..=8192).contains(&size) {
            return Err("Private connection lost");
        }
        let mut bytes = vec![0; size + 4];
        bytes[..4].copy_from_slice(&header);
        stream
            .read_exact(&mut bytes[4..])
            .map_err(|_| "Private connection lost")?;
        let response = crate::ipc::read_frame(&mut std::io::Cursor::new(bytes))
            .map_err(|_| "Private connection lost")?
            .ok_or("Private connection lost")?;
        if response.as_object().is_none_or(|o| o.len() != 2) || response["type"] != "PrivateResult"
        {
            return Err("Private connection lost");
        }
        response["ok"].as_bool().ok_or("Private connection lost")
    }
    fn take_qr(
        &self,
        generation: u64,
        epoch: u64,
        challenge: &str,
    ) -> Result<String, &'static str> {
        if challenge.is_empty() || challenge.len() > 128 {
            return Err("Private QR unavailable");
        }
        let _guard = self.serial.try_lock().map_err(|_| "Private input busy")?;
        let message = serde_json::json!({"type":"TakeQr", "generation":generation, "epoch":epoch, "challenge_id":challenge});
        let frame = crate::ipc::encode_frame(&message).map_err(|_| "Private QR unavailable")?;
        let mut stream = &self.socket;
        stream
            .write_all(&frame)
            .and_then(|_| stream.flush())
            .map_err(|_| "Private connection lost")?;
        let mut header = [0; 4];
        stream
            .read_exact(&mut header)
            .map_err(|_| "Private connection lost")?;
        let size = u32::from_be_bytes(header) as usize;
        if !(1..=8192).contains(&size) {
            return Err("Private connection lost");
        }
        let mut bytes = vec![0; size + 4];
        bytes[..4].copy_from_slice(&header);
        stream
            .read_exact(&mut bytes[4..])
            .map_err(|_| "Private connection lost")?;
        let parsed = crate::ipc::read_frame(&mut std::io::Cursor::new(&bytes));
        for byte in &mut bytes {
            unsafe {
                std::ptr::write_volatile(byte, 0);
            }
        }
        std::sync::atomic::compiler_fence(Ordering::SeqCst);
        let mut response = parsed
            .map_err(|_| "Private connection lost")?
            .ok_or("Private connection lost")?;
        let valid = response.as_object().is_some_and(|o| o.len() == 2)
            && response["type"] == "PrivateQr"
            && response
                .get("value")
                .and_then(Value::as_str)
                .is_some_and(|v| !v.is_empty() && v.len() <= 4096);
        if !valid {
            if let Some(Value::String(value)) = response.get_mut("value") {
                crate::secure_auth::wipe_string(value);
            }
            return Err("Private QR unavailable");
        }
        let value = response
            .as_object_mut()
            .expect("validated")
            .remove("value")
            .unwrap();
        match value {
            Value::String(value) => Ok(value),
            _ => Err("Private QR unavailable"),
        }
    }
}

#[cfg(windows)]
struct WipeQr(String);
#[cfg(windows)]
impl Drop for WipeQr {
    fn drop(&mut self) {
        crate::secure_auth::wipe_string(&mut self.0);
    }
}
#[cfg(windows)]
enum PrivateReply {
    Result(bool),
    Qr(WipeQr),
}
#[cfg(windows)]
struct PrivatePipe {
    requests: SyncSender<(Vec<u8>, SyncSender<Result<PrivateReply, &'static str>>)>,
    serial: Mutex<()>,
    revoked: Arc<AtomicBool>,
}
#[cfg(windows)]
impl PrivatePipe {
    fn new(mut pipe: crate::windows_private::Duplex) -> Self {
        let (requests, receiver) =
            mpsc::sync_channel::<(Vec<u8>, SyncSender<Result<PrivateReply, &'static str>>)>(1);
        let revoked = Arc::new(AtomicBool::new(false));
        let flag = revoked.clone();
        std::thread::spawn(move || {
            while let Ok((mut frame, reply)) = receiver.recv() {
                if flag.load(Ordering::Acquire) {
                    for byte in &mut frame {
                        unsafe {
                            std::ptr::write_volatile(byte, 0);
                        }
                    }
                    break;
                }
                let result = (|| {
                    let written = pipe.write_all(&frame).and_then(|_| pipe.flush());
                    for byte in &mut frame {
                        unsafe {
                            std::ptr::write_volatile(byte, 0);
                        }
                    }
                    std::sync::atomic::compiler_fence(Ordering::SeqCst);
                    written.map_err(|_| "Private connection lost")?;
                    let mut header = [0; 4];
                    pipe.read_exact(&mut header)
                        .map_err(|_| "Private connection lost")?;
                    let size = u32::from_be_bytes(header) as usize;
                    if !(1..=8192).contains(&size) {
                        return Err("Private connection lost");
                    }
                    let mut bytes = vec![0; size + 4];
                    bytes[..4].copy_from_slice(&header);
                    pipe.read_exact(&mut bytes[4..])
                        .map_err(|_| "Private connection lost")?;
                    let parsed = crate::ipc::read_frame(&mut std::io::Cursor::new(&bytes));
                    for byte in &mut bytes {
                        unsafe {
                            std::ptr::write_volatile(byte, 0);
                        }
                    }
                    std::sync::atomic::compiler_fence(Ordering::SeqCst);
                    let mut response = parsed
                        .map_err(|_| "Private connection lost")?
                        .ok_or("Private connection lost")?;
                    let valid_shape = response.as_object().is_some_and(|o| o.len() == 2);
                    if valid_shape && response["type"] == "PrivateResult" {
                        return response["ok"]
                            .as_bool()
                            .map(PrivateReply::Result)
                            .ok_or("Private connection lost");
                    }
                    let qr = response.as_object_mut().and_then(|o| o.remove("value"));
                    if let Some(Value::String(value)) = qr {
                        let value = WipeQr(value);
                        if valid_shape
                            && response["type"] == "PrivateQr"
                            && !value.0.is_empty()
                            && value.0.len() <= 4096
                        {
                            return Ok(PrivateReply::Qr(value));
                        }
                    }
                    Err("Private connection lost")
                })();
                for byte in &mut frame {
                    unsafe {
                        std::ptr::write_volatile(byte, 0);
                    }
                }
                if result.is_err() {
                    flag.store(true, Ordering::Release);
                }
                let _ = reply.send(result);
            }
            flag.store(true, Ordering::Release);
        });
        Self {
            requests,
            serial: Mutex::new(()),
            revoked,
        }
    }
    fn revoke(&self) {
        self.revoked.store(true, Ordering::Release);
    }
    fn submit(
        &self,
        generation: u64,
        epoch: u64,
        challenge: &str,
        kind: &str,
        mut value: String,
    ) -> Result<bool, &'static str> {
        if !matches!(kind, "phone" | "code" | "password")
            || challenge.is_empty()
            || challenge.len() > 128
            || value.is_empty()
            || value.len() > 4096
        {
            crate::secure_auth::wipe_string(&mut value);
            return Err("Private input unavailable");
        }
        if self.revoked.load(Ordering::Acquire) {
            crate::secure_auth::wipe_string(&mut value);
            return Err("Private input unavailable");
        }
        let _serial = match self.serial.try_lock() {
            Ok(guard) => guard,
            Err(_) => {
                crate::secure_auth::wipe_string(&mut value);
                return Err("Private input busy");
            }
        };
        let mut random = [0u8; 32];
        if getrandom::fill(&mut random).is_err() {
            crate::secure_auth::wipe_string(&mut value);
            return Err("Private input unavailable");
        }
        let handle: String = random.iter().map(|b| format!("{b:02x}")).collect();
        let mut message = serde_json::json!({"type":"SubmitSecret", "generation":generation, "epoch":epoch,
            "challenge_id":challenge, "kind":kind, "handle":handle, "value":value});
        let encoded = crate::ipc::encode_frame(&message);
        if let Some(Value::String(secret)) = message.get_mut("value") {
            crate::secure_auth::wipe_string(secret);
        }
        let frame = encoded.map_err(|_| "Private input unavailable")?;
        let (sender, receiver) = mpsc::sync_channel(1);
        match self.requests.try_send((frame, sender)) {
            Ok(()) => (),
            Err(error) => {
                let (mut frame, _) = match error {
                    TrySendError::Full(v) | TrySendError::Disconnected(v) => v,
                };
                for byte in &mut frame {
                    unsafe {
                        std::ptr::write_volatile(byte, 0);
                    }
                }
                self.revoke();
                return Err("Private connection lost");
            }
        }
        match receiver.recv_timeout(Duration::from_secs(5)) {
            Ok(Ok(PrivateReply::Result(result))) if !self.revoked.load(Ordering::Acquire) => {
                Ok(result)
            }
            Ok(Err(error)) => Err(error),
            _ => {
                self.revoke();
                Err("Private connection lost")
            }
        }
    }
    fn take_qr(
        &self,
        generation: u64,
        epoch: u64,
        challenge: &str,
    ) -> Result<String, &'static str> {
        if challenge.is_empty() || challenge.len() > 128 || self.revoked.load(Ordering::Acquire) {
            return Err("Private QR unavailable");
        }
        let _serial = self.serial.try_lock().map_err(|_| "Private input busy")?;
        let frame = crate::ipc::encode_frame(&serde_json::json!({"type":"TakeQr", "generation":generation, "epoch":epoch, "challenge_id":challenge}))
            .map_err(|_| "Private QR unavailable")?;
        let (sender, receiver) = mpsc::sync_channel(1);
        if self.requests.try_send((frame, sender)).is_err() {
            self.revoke();
            return Err("Private connection lost");
        }
        match receiver.recv_timeout(Duration::from_secs(5)) {
            Ok(Ok(PrivateReply::Qr(mut value))) if !self.revoked.load(Ordering::Acquire) => {
                Ok(std::mem::take(&mut value.0))
            }
            Ok(Err(error)) => Err(error),
            _ => {
                self.revoke();
                Err("Private QR unavailable")
            }
        }
    }
}

enum PreparedWorker {
    Packaged {
        executable: PathBuf,
        guarded: crate::verified_artifacts::GuardedResources,
    },
    #[cfg(all(debug_assertions, any(feature = "development-worker", test)))]
    Development(crate::development_worker::DevelopmentWorker),
    #[cfg(all(
        debug_assertions,
        target_os = "macos",
        target_arch = "aarch64",
        any(feature = "development-worker", test)
    ))]
    Connected {
        worker: crate::development_worker::DevelopmentWorker,
        guarded: crate::verified_artifacts::GuardedResources,
        inference: Option<crate::verified_artifacts::GuardedResources>,
    },
}
impl PreparedWorker {
    fn spec(&self, number: u64) -> SpawnSpec {
        match self {
            Self::Packaged { executable, .. } => {
                SpawnSpec::packaged_worker_generation(executable, number)
            }
            #[cfg(all(debug_assertions, any(feature = "development-worker", test)))]
            Self::Development(worker) => worker.spec(number),
            #[cfg(all(
                debug_assertions,
                target_os = "macos",
                target_arch = "aarch64",
                any(feature = "development-worker", test)
            ))]
            Self::Connected { worker, .. } => worker.spec(number),
        }
    }
    fn resources(&self) -> &Path {
        match self {
            Self::Packaged { guarded, .. } => guarded.root(),
            #[cfg(all(debug_assertions, any(feature = "development-worker", test)))]
            Self::Development(worker) => worker.resources(),
            #[cfg(all(
                debug_assertions,
                target_os = "macos",
                target_arch = "aarch64",
                any(feature = "development-worker", test)
            ))]
            Self::Connected { guarded, .. } => guarded.root(),
        }
    }
    fn proof(
        &self,
        owner: Arc<Mutex<DesktopSupervisor>>,
        worker: Arc<Handle>,
    ) -> Arc<dyn crate::native_broker::NativeProof> {
        match self {
            Self::Packaged { guarded, .. } => Arc::new(crate::target_proof::PackagedProof::new(
                owner,
                worker,
                guarded.clone(),
            )),
            #[cfg(all(debug_assertions, any(feature = "development-worker", test)))]
            Self::Development(_) => Arc::new(crate::development_worker::NoNativeCapabilities),
            #[cfg(all(
                debug_assertions,
                target_os = "macos",
                target_arch = "aarch64",
                any(feature = "development-worker", test)
            ))]
            Self::Connected {
                guarded, inference, ..
            } => Arc::new(
                crate::connected_local_proof::ConnectedLocalProof::new_with_inference(
                    owner,
                    worker,
                    guarded.clone(),
                    inference.clone(),
                ),
            ),
        }
    }
}

enum HostPhase {
    Disconnected,
    Active(ActiveGeneration),
    Closing,
    CleanupUnconfirmed,
}
struct HostState {
    phase: HostPhase,
    next: u64,
}

/// Host-only owner. Paths come exclusively from Tauri's installed layout and
/// app-data resolver, never from the renderer. The desktop lock survives every
/// confirmed generation shutdown. No supervisor lock is held awaiting replies.
pub struct WorkerHost {
    embedded: Mutex<Option<Arc<dyn crate::embedded_threads::EmbeddedCollector>>>,
    supervisor: Arc<Mutex<DesktopSupervisor>>,
    state: Mutex<HostState>,
    root: PathBuf,
    executable: PathBuf,
    #[cfg(all(debug_assertions, any(feature = "development-worker", test)))]
    development: Option<crate::development_worker::DevelopmentWorker>,
    terminal: AtomicBool,
    power_suspended: AtomicBool,
    power_epoch: AtomicU64,
    active_number: AtomicU64,
    active_power_epoch: AtomicU64,
    preparation_epoch: AtomicU64,
    #[cfg(test)]
    preparation_hook: Mutex<
        Option<
            Arc<
                dyn Fn(
                        &mut DesktopSupervisor,
                    ) -> Option<crate::verified_artifacts::GuardedResources>
                    + Send
                    + Sync,
            >,
        >,
    >,
}
impl WorkerHost {
    pub(crate) fn app_root(&self) -> &Path {
        &self.root
    }
    pub(crate) fn set_embedded(
        &self,
        host: Arc<dyn crate::embedded_threads::EmbeddedCollector>,
    ) -> std::io::Result<()> {
        let mut slot = self
            .embedded
            .lock()
            .map_err(|_| std::io::Error::other("Embedded Threads unavailable"))?;
        if slot.is_some() {
            return Err(std::io::Error::other("Embedded Threads unavailable"));
        }
        *slot = Some(host);
        Ok(())
    }
    fn close_embedded(
        &self,
        ticket: Option<&(Arc<dyn crate::embedded_threads::EmbeddedCollector>, u64)>,
    ) -> Result<(), &'static str> {
        if let Some((host, generation)) = ticket {
            host.close_generation(*generation)
                .map_err(|_| "Embedded Threads close unconfirmed")?;
        }
        Ok(())
    }
    fn revoke_embedded(
        &self,
    ) -> Option<(Arc<dyn crate::embedded_threads::EmbeddedCollector>, u64)> {
        let host = self.embedded.try_lock().ok()?.clone()?;
        let generation = host.revoke();
        Some((host, generation))
    }
    fn defer_embedded_close(
        &self,
        ticket: Option<(Arc<dyn crate::embedded_threads::EmbeddedCollector>, u64)>,
    ) {
        if let Some((host, generation)) = ticket {
            if std::thread::Builder::new()
                .name("embedded-close".into())
                .spawn(move || {
                    let _ = host.close_generation(generation);
                })
                .is_err()
            {
                eprintln!(
                    "Embedded Threads authority revoked; native close scheduling unavailable"
                );
            }
        }
    }
    fn defer_embedded_destroy(
        &self,
        ticket: Option<(Arc<dyn crate::embedded_threads::EmbeddedCollector>, u64)>,
    ) {
        if let Some((host, _capture_epoch)) = ticket {
            if std::thread::Builder::new()
                .name("embedded-destroy".into())
                .spawn(move || {
                    let _ = host.close_all();
                })
                .is_err()
            {
                eprintln!(
                    "Embedded capture revoked; app-exit browser cleanup scheduling unavailable"
                );
            }
        }
    }
    pub fn new(root: &Path, executable: &Path) -> std::io::Result<Self> {
        if !root.is_absolute() || !executable.is_absolute() {
            return Err(std::io::Error::other("Invalid installed layout"));
        }
        Ok(Self {
            supervisor: Arc::new(Mutex::new(DesktopSupervisor::acquire(root)?)),
            embedded: Mutex::new(None),
            state: Mutex::new(HostState {
                phase: HostPhase::Disconnected,
                next: 0,
            }),
            root: root.to_owned(),
            executable: executable.to_owned(),
            #[cfg(all(debug_assertions, any(feature = "development-worker", test)))]
            development: None,
            terminal: AtomicBool::new(false),
            power_suspended: AtomicBool::new(false),
            power_epoch: AtomicU64::new(0),
            active_number: AtomicU64::new(0),
            active_power_epoch: AtomicU64::new(0),
            preparation_epoch: AtomicU64::new(0),
            #[cfg(test)]
            preparation_hook: Mutex::new(None),
        })
    }
    /// Explicit local source mode. Release builds have no such constructor.
    #[cfg(all(debug_assertions, any(feature = "development-worker", test)))]
    pub fn development(root: &Path) -> std::io::Result<Self> {
        let worker = crate::development_worker::DevelopmentWorker::discover(root)?;
        let mut host = Self::new(root, worker.executable())?;
        host.development = Some(worker);
        Ok(host)
    }
    #[cfg(all(debug_assertions, any(feature = "development-worker", test)))]
    pub fn connected(root: &Path) -> std::io::Result<Self> {
        let worker = crate::development_worker::DevelopmentWorker::connected(root)?;
        let mut host = Self::new(root, worker.executable())?;
        host.development = Some(worker);
        Ok(host)
    }
    fn connection_only(&self) -> bool {
        #[cfg(all(debug_assertions, any(feature = "development-worker", test)))]
        if self.development.as_ref().is_some_and(|w| w.is_connected()) {
            return true;
        }
        false
    }
    fn local_only(&self) -> bool {
        #[cfg(all(debug_assertions, any(feature = "development-worker", test)))]
        if self.development.as_ref().is_some_and(|w| !w.is_connected()) {
            return true;
        }
        false
    }
    pub(crate) fn stored_vault(
        &self,
    ) -> Arc<dyn crate::credentials::CredentialVault + Send + Sync> {
        #[cfg(target_os = "macos")]
        {
            match crate::local_vault::LocalVault::new(&self.root) {
                Ok(local) => Arc::new(crate::local_vault::RememberedVault::new(
                    local,
                    crate::credentials::PlatformVault::new(),
                )),
                Err(_) => Arc::new(UnavailableVault),
            }
        }
        #[cfg(not(target_os = "macos"))]
        {
            Arc::new(crate::credentials::PlatformVault::new())
        }
    }
    fn remembered_account_present(&self) -> bool {
        #[cfg(target_os = "macos")]
        {
            use crate::credentials::CredentialVault;
            let reference = if self.connection_only() {
                "vault:threadrelay_connection_account"
            } else {
                "vault:threadrelay_account"
            };
            crate::local_vault::LocalVault::new(&self.root)
                .and_then(|vault| vault.contains(reference))
                .unwrap_or(false)
        }
        #[cfg(not(target_os = "macos"))]
        {
            false
        }
    }
    fn control_vault(&self) -> Arc<dyn crate::credentials::CredentialVault + Send + Sync> {
        #[cfg(all(debug_assertions, any(feature = "development-worker", test)))]
        if self.local_only() {
            return Arc::new(crate::development_worker::NoCredentials);
        }
        if self.connection_only() {
            Arc::new(crate::credentials::ConnectionVault(self.stored_vault()))
        } else {
            self.stored_vault()
        }
    }
    fn control_broker(
        &self,
        broker: &Arc<crate::native_broker::NativeBroker>,
    ) -> Option<Arc<crate::native_broker::NativeBroker>> {
        if let Some(host) = self.embedded.lock().ok()?.clone() {
            broker.set_embedded(host).ok()?;
        }
        if self.local_only() && self.embedded.lock().map_or(true, |s| s.is_none()) {
            None
        } else {
            Some(broker.clone())
        }
    }
    fn prepare_worker(
        &self,
        owner: &mut DesktopSupervisor,
        preparation_epoch: u64,
    ) -> std::io::Result<PreparedWorker> {
        #[cfg(all(debug_assertions, any(feature = "development-worker", test)))]
        if let Some(worker) = &self.development {
            worker.validate()?;
            if worker.is_connected() {
                #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
                {
                    let guarded =
                        crate::connected_local_proof::prepare(owner, &self.root, &|| {
                            self.preparation_cancelled(preparation_epoch)
                        })?;
                    let inference =
                        crate::connected_local_proof::prepare_inference(owner, &self.root, &|| {
                            self.preparation_cancelled(preparation_epoch)
                        })
                        .ok();
                    return Ok(PreparedWorker::Connected {
                        worker: worker.clone(),
                        guarded,
                        inference,
                    });
                }
                #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
                return Err(std::io::Error::other(
                    "Connected local mode requires macOS Apple silicon",
                ));
            }
            return Ok(PreparedWorker::Development(worker.clone()));
        }
        let original_resources = self
            .executable
            .parent()
            .and_then(Path::parent)
            .ok_or_else(|| std::io::Error::other("Invalid installed layout"))?;
        #[cfg(test)]
        let test_guarded = self
            .preparation_hook
            .lock()
            .unwrap()
            .take()
            .and_then(|hook| hook(owner));
        #[cfg(not(test))]
        let test_guarded: Option<crate::verified_artifacts::GuardedResources> = None;
        let guarded = match test_guarded {
            Some(guarded) => guarded,
            None => owner.prepare_artifacts(original_resources, &self.root, &|| {
                self.preparation_cancelled(preparation_epoch)
            })?,
        };
        #[cfg(any(windows, target_os = "macos"))]
        let executable = guarded.worker(std::env::consts::OS);
        #[cfg(not(any(windows, target_os = "macos")))]
        let executable = self.executable.clone();
        Ok(PreparedWorker::Packaged {
            executable,
            guarded,
        })
    }
    fn clean(&self, grace_ms: u64) -> bool {
        self.supervisor
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .close_generation_with_grace(PROPOSED_SHUTDOWN_MS, grace_ms)
            .stopped
    }
    fn finish_cleanup(&self, grace_ms: u64) -> bool {
        let stopped = self.clean(grace_ms);
        self.state.lock().unwrap_or_else(|e| e.into_inner()).phase = if stopped {
            HostPhase::Disconnected
        } else {
            HostPhase::CleanupUnconfirmed
        };
        stopped
    }
    fn invalidate(&self, number: u64) {
        let bridge = {
            let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
            if !matches!(&state.phase, HostPhase::Active(active) if active.number == number) {
                return;
            }
            match std::mem::replace(&mut state.phase, HostPhase::Closing) {
                HostPhase::Active(active) => active,
                _ => unreachable!(),
            }
        };
        self.active_number
            .compare_exchange(number, 0, Ordering::AcqRel, Ordering::Acquire)
            .ok();
        #[cfg(any(unix, windows))]
        {
            bridge.broker.revoke();
            bridge.private.revoke();
            bridge.control.revoke();
        }
        bridge.bridge.revoke();
        self.finish_cleanup(0);
    }
    fn active(&self) -> Option<(u64, Arc<WorkerBridge>)> {
        let state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        match &state.phase {
            HostPhase::Active(a)
                if !self.power_suspended.load(Ordering::Acquire)
                    && self.active_power_epoch.load(Ordering::Acquire)
                        == self.power_epoch.load(Ordering::Acquire) =>
            {
                Some((a.number, a.bridge.clone()))
            }
            _ => None,
        }
    }
    fn preparation_cancelled(&self, epoch: u64) -> bool {
        self.terminal.load(Ordering::Acquire)
            || self.power_suspended.load(Ordering::Acquire)
            || self.preparation_epoch.load(Ordering::Acquire) != epoch
    }
    fn initialize(&self, preparation_epoch: u64) -> Result<(u64, Arc<WorkerBridge>), &'static str> {
        if self.preparation_cancelled(preparation_epoch) {
            return Err("Worker preparation cancelled");
        }
        let power_epoch = self.power_epoch.load(Ordering::Acquire);
        if self.power_suspended.load(Ordering::Acquire) {
            return Err("System suspended");
        }
        if self.terminal.load(Ordering::Acquire) {
            return Err("Worker host closed");
        }
        let retry_cleanup = {
            let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
            match &state.phase {
                HostPhase::Active(a)
                    if self.active_power_epoch.load(Ordering::Acquire) == power_epoch =>
                {
                    return Ok((a.number, a.bridge.clone()))
                }
                HostPhase::Active(_) => return Err("Worker cleanup pending"),
                HostPhase::Closing => return Err("Worker busy"),
                HostPhase::CleanupUnconfirmed => {
                    state.phase = HostPhase::Closing;
                    true
                }
                HostPhase::Disconnected => {
                    state.phase = HostPhase::Closing;
                    false
                }
            }
        };
        if retry_cleanup && !self.clean(0) {
            self.state.lock().unwrap_or_else(|e| e.into_inner()).phase =
                HostPhase::CleanupUnconfirmed;
            return Err("Worker cleanup unconfirmed");
        }
        let number = {
            let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
            let next = state
                .next
                .checked_add(1)
                .filter(|n| *n <= 9_007_199_254_740_991)
                .ok_or("Worker generation exhausted")?;
            state.next = next;
            next
        };
        let created = self.spawn(number, preparation_epoch);
        match created {
            Ok(active) => {
                let bridge = active.bridge.clone();
                let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
                self.active_power_epoch
                    .store(power_epoch, Ordering::Release);
                self.active_number.store(number, Ordering::Release);
                if self.preparation_cancelled(preparation_epoch)
                    || self.power_epoch.load(Ordering::Acquire) != power_epoch
                {
                    self.active_number.store(0, Ordering::Release);
                    drop(state);
                    active.broker.revoke();
                    bridge.revoke();
                    self.finish_cleanup(0);
                    return Err("Worker host unavailable");
                }
                state.phase = HostPhase::Active(active);
                Ok((number, bridge))
            }
            Err(error) => {
                self.finish_cleanup(0);
                if error.kind() == std::io::ErrorKind::AlreadyExists {
                    Err("Execution snapshot cleanup pending; prior generation retained")
                } else {
                    Err("Worker connection unavailable")
                }
            }
        }
    }
    #[cfg(unix)]
    fn spawn(&self, number: u64, preparation_epoch: u64) -> std::io::Result<ActiveGeneration> {
        let mut owner = self.supervisor.lock().unwrap_or_else(|e| e.into_inner());
        if self.terminal.load(Ordering::Acquire) {
            return Err(std::io::Error::other("Worker host closed"));
        }
        let prepared = self.prepare_worker(&mut owner, preparation_epoch)?;
        if self.preparation_cancelled(preparation_epoch) {
            return Err(std::io::Error::other("Worker preparation cancelled"));
        }
        let mut worker = owner
            .processes
            .spawn(ProcessKind::Worker, &prepared.spec(number))?;
        let mut pipes = worker
            .take_pipes()
            .ok_or_else(|| std::io::Error::other("Worker pipes unavailable"))?;
        let worker = Arc::new(worker);
        let resources = prepared.resources();
        let lane = crate::native_broker::DriverLane::from_socket(
            pipes
                .driver
                .take()
                .ok_or_else(|| std::io::Error::other("Driver lane unavailable"))?,
        )?;
        let broker = Arc::new(crate::native_broker::NativeBroker::new(
            self.supervisor.clone(),
            worker.clone(),
            &self.root,
            resources,
            prepared.proof(self.supervisor.clone(), worker.clone()),
            Some(lane),
        )?);
        let mut bootstrap = pipes
            .bootstrap
            .take()
            .ok_or_else(|| std::io::Error::other("Private bootstrap unavailable"))?;
        let control_socket = pipes
            .control
            .take()
            .ok_or_else(|| std::io::Error::other("Private control unavailable"))?;
        let control = Arc::new(crate::host_control::start(
            control_socket,
            number,
            self.control_vault(),
            self.control_broker(&broker),
        )?);
        let root = self
            .root
            .to_str()
            .ok_or_else(|| std::io::Error::other("Invalid data root"))?;
        if root.len() > 4096 {
            return Err(std::io::Error::other("Invalid data root"));
        }
        let message = serde_json::json!({"version":1,"type":"LocalWorkerBootstrap","data_root":root,"generation":number});
        let frame = crate::ipc::encode_frame(&message)
            .map_err(|_| std::io::Error::other("Private bootstrap invalid"))?;
        bootstrap.write_all(&frame)?;
        bootstrap.set_read_timeout(Some(Duration::from_secs(5)))?;
        bootstrap.set_write_timeout(Some(Duration::from_secs(5)))?;
        let private = Arc::new(PrivatePipe {
            socket: bootstrap,
            serial: Mutex::new(()),
        });
        std::thread::spawn(move || {
            let _ = std::io::copy(&mut pipes.stderr, &mut std::io::sink());
        });
        let bridge = Arc::new(WorkerBridge::new(
            pipes.stdin,
            pipes.stdout,
            Duration::from_secs(5),
        ));
        Ok(ActiveGeneration {
            number,
            bridge,
            _worker: worker,
            broker,
            private,
            control,
        })
    }
    #[cfg(windows)]
    fn spawn(&self, number: u64, preparation_epoch: u64) -> std::io::Result<ActiveGeneration> {
        let mut owner = self.supervisor.lock().unwrap_or_else(|e| e.into_inner());
        if self.terminal.load(Ordering::Acquire) {
            return Err(std::io::Error::other("Worker host closed"));
        }
        let prepared = self.prepare_worker(&mut owner, preparation_epoch)?;
        if self.preparation_cancelled(preparation_epoch) {
            return Err(std::io::Error::other("Worker preparation cancelled"));
        }
        let mut worker = owner
            .processes
            .spawn(ProcessKind::Worker, &prepared.spec(number))?;
        let mut pipes = worker
            .take_pipes()
            .ok_or_else(|| std::io::Error::other("Worker pipes unavailable"))?;
        let worker = Arc::new(worker);
        let resources = prepared.resources();
        let lane = crate::native_broker::DriverLane::from_pipe(
            pipes
                .driver
                .take()
                .ok_or_else(|| std::io::Error::other("Driver lane unavailable"))?,
        );
        let broker = Arc::new(crate::native_broker::NativeBroker::new(
            self.supervisor.clone(),
            worker.clone(),
            &self.root,
            resources,
            prepared.proof(self.supervisor.clone(), worker.clone()),
            Some(lane),
        )?);
        let mut bootstrap = pipes
            .bootstrap
            .take()
            .ok_or_else(|| std::io::Error::other("Private bootstrap unavailable"))?;
        let control_stream = pipes
            .control
            .take()
            .ok_or_else(|| std::io::Error::other("Private control unavailable"))?;
        let control = Arc::new(crate::host_control::start_windows(
            control_stream,
            number,
            self.control_vault(),
            self.control_broker(&broker),
        ));
        let root = self
            .root
            .to_str()
            .ok_or_else(|| std::io::Error::other("Invalid data root"))?;
        if root.len() > 4096 {
            return Err(std::io::Error::other("Invalid data root"));
        }
        let frame = crate::ipc::encode_frame(&serde_json::json!({"version":1,"type":"LocalWorkerBootstrap","data_root":root,"generation":number}))
            .map_err(|_| std::io::Error::other("Private bootstrap invalid"))?;
        bootstrap.write_all(&frame)?;
        let private = Arc::new(PrivatePipe::new(bootstrap));
        std::thread::spawn(move || {
            let _ = std::io::copy(&mut pipes.stderr, &mut std::io::sink());
        });
        let bridge = Arc::new(WorkerBridge::new(
            pipes.stdin,
            pipes.stdout,
            Duration::from_secs(5),
        ));
        Ok(ActiveGeneration {
            number,
            bridge,
            _worker: worker,
            broker,
            private,
            control,
        })
    }
    #[cfg(not(any(unix, windows)))]
    fn spawn(&self, _: u64) -> std::io::Result<ActiveGeneration> {
        Err(std::io::Error::new(
            std::io::ErrorKind::Unsupported,
            "Private bootstrap unavailable on this target",
        ))
    }
    #[cfg(any(unix, windows))]
    fn replace_consumed_for_start(&self) -> Result<(), &'static str> {
        let (number, bridge, broker) = {
            let state = self.state.lock().unwrap_or_else(|e| e.into_inner());
            match &state.phase {
                HostPhase::Active(active) => {
                    (active.number, active.bridge.clone(), active.broker.clone())
                }
                _ => return Ok(()),
            }
        };
        if !broker.driver_consumed() {
            return Ok(());
        }
        let query = serde_json::json!({"version":1,"id":"owned-start-state","type":"QueryState","payload":{}});
        let event = bridge.request(query)?;
        if event["type"] != "State" {
            return Err("Worker connection unavailable");
        }
        match event["payload"]["state"].as_str() {
            Some("running" | "starting" | "pausing" | "stopping") => return Ok(()),
            Some("stopped" | "paused" | "needs_attention" | "failed") => (),
            _ => return Err("Worker connection unavailable"),
        }
        let active = {
            let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
            if !matches!(&state.phase,HostPhase::Active(a) if a.number==number) {
                return Err("Worker connection unavailable");
            }
            match std::mem::replace(&mut state.phase, HostPhase::Closing) {
                HostPhase::Active(a) => a,
                _ => unreachable!(),
            }
        };
        active.broker.revoke();
        active.private.revoke();
        active.control.revoke();
        let stop =
            serde_json::json!({"version":1,"id":"owned-start-stop","type":"Stop","payload":{}});
        let response = bridge.request(stop);
        bridge.revoke();
        let grace = if matches!(&response,Ok(event) if event["type"]=="State") {
            1_000
        } else {
            0
        };
        if !self.finish_cleanup(grace) {
            return Err("Worker cleanup unconfirmed");
        }
        if !matches!(response,Ok(event) if event["type"]=="State") {
            return Err("Worker connection unavailable");
        }
        Ok(())
    }
    pub fn request(&self, mut command: Value) -> Result<Value, &'static str> {
        crate::ipc::validate_contract("Command", &command).map_err(|_| "Invalid request")?;
        crate::ipc::encode_frame(&command).map_err(|_| "Invalid request")?;
        let kind = command["type"].as_str().unwrap_or("").to_owned();
        // Native view failure must never prevent revoking collection/send
        // authority. Report it only after the worker stop/pause has proceeded.
        let closing_embedded = matches!(
            kind.as_str(),
            "Stop" | "Pause" | "SuspendCollection" | "StopCollection"
        );
        let embedded_ticket = if closing_embedded {
            self.revoke_embedded()
        } else {
            None
        };
        #[cfg(all(debug_assertions, any(feature = "development-worker", test)))]
        if self.local_only() && !crate::development_worker::local_command(&command) {
            return Err("Unavailable in local development mode");
        }
        #[cfg(all(debug_assertions, any(feature = "development-worker", test)))]
        if self.connection_only() && !crate::development_worker::connected_command(&command) {
            return Err("Delivery is not enabled by connection-only mode");
        }
        // Reopening never invokes legacy Keychain UI. Only a previously saved
        // local record may grant this one bounded restore attempt.
        if kind == "RestoreTelegramSession" && !self.remembered_account_present() {
            return Ok(
                serde_json::json!({"version":1,"id":command["id"],"type":"Acknowledged","payload":{"operation":"RestoreTelegramSession"}}),
            );
        }
        let preparation_epoch = self.preparation_epoch.load(Ordering::Acquire);
        if matches!(
            kind.as_str(),
            "Stop" | "Pause" | "SuspendCollection" | "StopCollection"
        ) {
            self.preparation_epoch.fetch_add(1, Ordering::AcqRel);
            let state = self.state.lock().unwrap_or_else(|e| e.into_inner());
            if matches!(state.phase, HostPhase::Closing) {
                // Cancellation acknowledgement is not a stopped/cleanup claim.
                return Ok(
                    serde_json::json!({"version":1,"id":command["id"],"type":"State","payload":{"state":"stopping","counters":{"observed":0,"matched":0,"queued":0,"sent":0},"attention_reason":null}}),
                );
            }
        }
        #[cfg(any(unix, windows))]
        if matches!(kind.as_str(), "Start" | "InitializeLocalWorkspace") {
            self.replace_consumed_for_start()?;
        }
        let (number, bridge) = if kind == "InitializeLocalWorkspace" || kind == "Start" {
            self.initialize(preparation_epoch)?
        } else {
            match self.active() {
                Some(active) => active,
                None => {
                    if closing_embedded {
                        self.close_embedded(embedded_ticket.as_ref())?;
                    }
                    return Err("Worker connection unavailable");
                }
            }
        };
        if kind == "InitializeLocalWorkspace" {
            command["type"] = Value::String("QueryState".into());
        }
        // Cancel host hashing/spawning immediately, without waiting for the
        // serial private control exchange or Python owner to return. A later
        // explicit Start replaces this permanently revoked generation.
        #[cfg(any(unix, windows))]
        if kind == "Pause" {
            let state = self.state.lock().unwrap_or_else(|e| e.into_inner());
            if let HostPhase::Active(active) = &state.phase {
                if active.number == number {
                    active.broker.revoke();
                }
            }
        }
        #[cfg(any(unix, windows))]
        if matches!(kind.as_str(), "SuspendCollection" | "StopCollection") {
            let state = self.state.lock().unwrap_or_else(|e| e.into_inner());
            if let HostPhase::Active(active) = &state.phase {
                if active.number == number {
                    active.broker.cancel_collection();
                }
            }
        }
        if kind == "Stop" {
            {
                let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
                if !matches!(&state.phase, HostPhase::Active(a) if a.number == number) {
                    return Err("Worker connection unavailable");
                }
                let active = match std::mem::replace(&mut state.phase, HostPhase::Closing) {
                    HostPhase::Active(active) => active,
                    _ => unreachable!(),
                };
                #[cfg(any(unix, windows))]
                {
                    active.broker.revoke();
                    active.private.revoke();
                    active.control.revoke();
                }
            }
            let result = bridge.request(command);
            bridge.revoke();
            // A validated Stop response closed worker authority; give the
            // worker a short share of the common deadline to close cleanly.
            let grace = if matches!(&result, Ok(event) if event["type"] == "State") {
                1_000
            } else {
                0
            };
            if !self.finish_cleanup(grace) {
                return Err("Worker cleanup unconfirmed");
            }
            self.close_embedded(embedded_ticket.as_ref())?;
            return result;
        }
        #[cfg(any(unix, windows))]
        let (control, credential_broker) = {
            let state = self.state.lock().unwrap_or_else(|e| e.into_inner());
            match &state.phase {
                HostPhase::Active(a) if a.number == number => {
                    (Some(a.control.clone()), Some(a.broker.clone()))
                }
                _ => (None, None),
            }
        };
        #[cfg(any(unix, windows))]
        if matches!(kind.as_str(), "Start" | "TestSample") {
            if let Some(broker) = credential_broker.as_ref() {
                broker.begin_collection();
            }
        }
        #[cfg(any(unix, windows))]
        let mut recovery_observation = None;
        #[cfg(any(unix, windows))]
        if let Some(control) = &control {
            if matches!(kind.as_str(), "CancelTelegramLogin" | "LogoutTelegram") {
                control.cancel_account_preparation();
            }
            if kind == "BeginTelegramLogin" {
                // Reject known delivery blockers before an old Keychain record
                // can request migration permission. The actual Begin/network
                // writer independently rechecks against concurrent changes.
                let mut preflight = bridge.request(serde_json::json!({"version":1,"id":"telegram-auth-preflight","type":"CheckTelegramLogin","payload":{}}))?;
                if preflight["type"] == "Error" {
                    preflight["id"] = command["id"].clone();
                    return Ok(preflight);
                }
                if let Some(broker) = credential_broker.as_ref() {
                    broker
                        .prepare_telegram_login_profile()
                        .map_err(|_| "Telegram login storage could not be prepared")?;
                }
                // Interactive Keychain authorization is outside the worker
                // exchange and every host-state mutex. Queries and Pause/Stop
                // remain available while the owner completes the OS prompt.
                control
                    .prepare_account(|| {
                        self.preparation_cancelled(preparation_epoch)
                            || self.active_number.load(Ordering::Acquire) != number
                    })
                    .map_err(|_| "Secure sign-in permission was not completed")?;
                if self.preparation_cancelled(preparation_epoch)
                    || self.active_number.load(Ordering::Acquire) != number
                {
                    control.cancel_account_preparation();
                    return Err("Secure sign-in preparation cancelled");
                }
            }
            // Guest classification grants no account credential authority.
            // Observe the live delivery proof after releasing the host state
            // lock; explicit Begin still grants its own one-shot auth scope.
            let delivery_ready = kind == "Start"
                && command["payload"]["mode"] != "dry_run"
                && credential_broker
                    .as_ref()
                    .is_some_and(|broker| broker.tdlib_ready());
            recovery_observation = Some(control.arm_for_command(&command, delivery_ready));
            if matches!(kind.as_str(), "CancelTelegramLogin" | "LogoutTelegram") {
                control.disarm();
            }
        }
        if matches!(kind.as_str(), "Start" | "InitializeLocalWorkspace")
            && self.preparation_cancelled(preparation_epoch)
        {
            return Err("Worker preparation cancelled");
        }
        #[cfg(debug_assertions)]
        let auth_started = (kind == "BeginTelegramLogin").then(Instant::now);
        let result = bridge.request(command);
        #[cfg(debug_assertions)]
        if let Some(started) = auth_started {
            eprintln!(
                "ThreadRelay login exchange: elapsed_ms={} result={}",
                started.elapsed().as_millis(),
                match &result {
                    Ok(event) if event["type"] == "Acknowledged" => "acknowledged",
                    Ok(_) => "rejected",
                    Err(error) => error,
                }
            );
        }
        #[cfg(any(unix, windows))]
        if let (Some(control), Some((ticket, was_started))) = (&control, recovery_observation) {
            let closing = matches!(&result,Ok(event) if event["type"]=="TelegramRecoveryState" && event["payload"]["state"]=="closing");
            if kind == "BeginTelegramRecovery" && closing {
                control.recovery_started(ticket);
            } else if (kind == "BeginTelegramRecovery"
                || (kind == "QueryTelegramRecovery" && was_started))
                && !closing
            {
                control.finish_recovery(ticket);
            }
        }
        #[cfg(any(unix, windows))]
        if kind == "BeginTelegramLogin"
            && !matches!(&result, Ok(event) if event["type"] == "Acknowledged")
        {
            if let Some(control) = &control {
                control.disarm();
            }
        }
        if matches!(result, Err(error) if error != "Worker busy") {
            self.invalidate(number);
        }
        if !matches!(self.active(), Some((current, _)) if current == number) {
            return Err("Worker connection unavailable");
        }
        if closing_embedded {
            self.close_embedded(embedded_ticket.as_ref())?;
        }
        result
    }
    pub fn secure_status(
        &self,
        vault: &dyn crate::credentials::CredentialVault,
    ) -> serde_json::Value {
        let unavailable = |reason: &str| serde_json::json!({"available":false,"configured":false,"reason":reason});
        if self.local_only() {
            return unavailable("telegram_runtime_unavailable");
        }
        #[cfg(any(unix, windows))]
        {
            let (number, broker, connection_only) = {
                let state = self.state.lock().unwrap_or_else(|e| e.into_inner());
                match &state.phase {
                    HostPhase::Active(active)
                        if !self.terminal.load(Ordering::Acquire)
                            && !self.power_suspended.load(Ordering::Acquire)
                            && self.active_power_epoch.load(Ordering::Acquire)
                                == self.power_epoch.load(Ordering::Acquire) =>
                    {
                        (active.number, active.broker.clone(), self.connection_only())
                    }
                    _ => return unavailable("app_not_ready"),
                }
            };
            // OS keychain/proof operations may wait. Never retain the host state
            // mutex across them: unrelated queries and shutdown must progress.
            let presence = if connection_only {
                crate::secure_auth::record_presence(&crate::credentials::ConnectionVault(vault))
            } else {
                crate::secure_auth::record_presence(vault)
            };
            let Ok(configured) = presence else {
                return unavailable("vault_unavailable");
            };
            let available = broker.tdlib_auth_ready();
            let state = self.state.lock().unwrap_or_else(|e| e.into_inner());
            let current = matches!(&state.phase,HostPhase::Active(active) if active.number==number&&Arc::ptr_eq(&active.broker,&broker))
                && !self.terminal.load(Ordering::Acquire)
                && !self.power_suspended.load(Ordering::Acquire)
                && self.active_power_epoch.load(Ordering::Acquire)
                    == self.power_epoch.load(Ordering::Acquire);
            if current {
                return serde_json::json!({"available":available,"configured":configured,"reason":if available { None } else { Some("telegram_runtime_unavailable") }});
            }
        }
        unavailable("app_not_ready")
    }
    /// Configuration is an explicit dedicated action. A stale account or
    /// existing native session directory prevents replacing its encryption key.
    pub fn secure_configure(
        &self,
        vault: &dyn crate::credentials::CredentialVault,
        api_id: &str,
        api_hash: &str,
    ) -> Result<(), &'static str> {
        if self.local_only() {
            return Err("Secure setup unavailable");
        }
        let (number, bridge) = self.active().ok_or("Secure setup unavailable")?;
        #[cfg(any(unix, windows))]
        if self.connection_only() {
            let broker = {
                let state = self.state.lock().unwrap_or_else(|e| e.into_inner());
                match &state.phase {
                    HostPhase::Active(active) if active.number == number => active.broker.clone(),
                    _ => return Err("Secure setup unavailable"),
                }
            };
            if !broker.tdlib_auth_ready() {
                return Err("Secure setup unavailable");
            }
        }
        let query = serde_json::json!({"version":1,"id":"private-config-preflight","type":"QueryAccount","payload":{}});
        let response = bridge
            .request(query)
            .map_err(|_| "Secure setup unavailable")?;
        if !matches!(self.active(), Some((current, _)) if current == number)
            || response["type"] != "Account"
            || !response["payload"]["snapshot"]["account_id"].is_null()
            || response["payload"]["snapshot"]["auth_state"] != "logged_out"
            || !response["payload"]["challenge"].is_null()
        {
            return Err("Secure setup unavailable");
        }
        let sessions = self.root.join("telegram");
        match std::fs::symlink_metadata(&sessions) {
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => (),
            Ok(meta) if meta.is_dir() && !meta.file_type().is_symlink() => {
                if std::fs::read_dir(sessions)
                    .map_err(|_| "Secure setup unavailable")?
                    .next()
                    .is_some()
                {
                    return Err("Secure setup unavailable");
                }
            }
            _ => return Err("Secure setup unavailable"),
        }
        let configured = if self.connection_only() {
            crate::secure_auth::configure(
                &crate::credentials::ConnectionVault(vault),
                api_id,
                api_hash,
            )
        } else {
            crate::secure_auth::configure(vault, api_id, api_hash)
        };
        configured.map_err(|_| "Secure setup unavailable")
    }
    #[cfg(any(unix, windows))]
    pub fn secure_submit(
        &self,
        challenge: &str,
        kind: &str,
        epoch: u64,
        mut value: String,
    ) -> Result<(), &'static str> {
        if self.local_only() {
            crate::secure_auth::wipe_string(&mut value);
            return Err("Private input unavailable");
        }
        let (number, private) = {
            let state = self.state.lock().unwrap_or_else(|e| e.into_inner());
            match &state.phase {
                HostPhase::Active(active) => (active.number, active.private.clone()),
                _ => {
                    crate::secure_auth::wipe_string(&mut value);
                    return Err("Private input unavailable");
                }
            }
        };
        let result = private.submit(number, epoch, challenge, kind, value);
        if matches!(result, Err("Private connection lost")) {
            self.invalidate(number);
        }
        if !matches!(self.active(), Some((current, _)) if current == number) {
            return Err("Private input unavailable");
        }
        match result {
            Ok(true) => Ok(()),
            _ => Err("Private input unavailable"),
        }
    }
    #[cfg(any(unix, windows))]
    pub fn secure_qr(&self, challenge: &str, epoch: u64) -> Result<String, &'static str> {
        if self.local_only() {
            return Err("Private QR unavailable");
        }
        let (number, private) = {
            let state = self.state.lock().unwrap_or_else(|e| e.into_inner());
            match &state.phase {
                HostPhase::Active(active) => (active.number, active.private.clone()),
                _ => return Err("Private QR unavailable"),
            }
        };
        let result = private.take_qr(number, epoch, challenge);
        if matches!(result, Err("Private connection lost")) {
            self.invalidate(number);
        }
        if !matches!(self.active(), Some((current, _)) if current == number) {
            if let Ok(mut value) = result {
                crate::secure_auth::wipe_string(&mut value);
            }
            return Err("Private QR unavailable");
        }
        result
    }
    #[cfg(not(any(unix, windows)))]
    pub fn secure_qr(&self, _: &str, _: u64) -> Result<String, &'static str> {
        Err("Private QR unavailable")
    }
    #[cfg(not(any(unix, windows)))]
    pub fn secure_submit(
        &self,
        _: &str,
        _: &str,
        _: u64,
        mut value: String,
    ) -> Result<(), &'static str> {
        crate::secure_auth::wipe_string(&mut value);
        Err("Private input unavailable")
    }
    /// Native power callbacks capture the current generation, then return
    /// without waiting for cleanup or any worker response.
    pub fn power_event(self: &Arc<Self>, suspended: bool) {
        let previous = self.power_suspended.swap(suspended, Ordering::AcqRel);
        if previous == suspended {
            return;
        } // duplicate wake/sleep cannot target a later generation
        let embedded_ticket = self.revoke_embedded();
        self.defer_embedded_close(embedded_ticket);
        self.power_epoch.fetch_add(1, Ordering::AcqRel);
        self.preparation_epoch.fetch_add(1, Ordering::AcqRel);
        let generation = self.active_number.load(Ordering::Acquire);
        if let Ok(state) = self.state.try_lock() {
            if let HostPhase::Active(active) = &state.phase {
                if active.number == generation {
                    #[cfg(any(unix, windows))]
                    active.broker.revoke();
                    active.bridge.revoke();
                }
            }
        }
        if generation != 0 {
            let host = self.clone();
            if std::thread::Builder::new()
                .name("power-revocation".into())
                .spawn(move || host.invalidate(generation))
                .is_err()
            {
                // A host unable to schedule revocation must not keep authority.
                std::process::exit(2);
            }
        }
    }
    pub fn shutdown(&self) {
        self.terminal.store(true, Ordering::Release);
        let embedded_ticket = self.revoke_embedded();
        let active = {
            let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
            match std::mem::replace(&mut state.phase, HostPhase::Closing) {
                HostPhase::Active(a) => Some(a),
                _ => None,
            }
        };
        if let Some(active) = active {
            #[cfg(any(unix, windows))]
            {
                active.broker.revoke();
                active.private.revoke();
                active.control.revoke();
            }
            active.bridge.revoke();
        }
        self.defer_embedded_destroy(embedded_ticket);
        self.finish_cleanup(0);
    }
}
impl Drop for WorkerHost {
    fn drop(&mut self) {
        self.shutdown();
        if matches!(
            self.state.lock().unwrap_or_else(|e| e.into_inner()).phase,
            HostPhase::CleanupUnconfirmed
        ) {
            // Keep instance lock, original process capabilities and execution
            // guards alive; pipe EOF/timeout must not free lazily loaded code.
            std::mem::forget(self.supervisor.clone());
        }
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use serde_json::json;
    use std::io::{Read, Write};
    use std::os::unix::net::UnixStream;
    use std::time::Duration;

    fn sample_response(id: &Value) -> Value {
        json!({"version":1,"id":id,"type":"Sample","payload":{"classification":{"post_id":"synthetic-sample","model_version":"synthetic-v1","topic_results":[],"abstained":false},"sending_possible":false}})
    }

    #[test]
    fn qr_uses_private_generation_and_challenge_bound_request() {
        let (client, mut server) = UnixStream::pair().unwrap();
        client
            .set_read_timeout(Some(Duration::from_secs(1)))
            .unwrap();
        let pipe = PrivatePipe {
            socket: client,
            serial: Mutex::new(()),
        };
        std::thread::spawn(move || {
            let request = crate::ipc::read_frame(&mut server).unwrap().unwrap();
            assert_eq!(
                request,
                json!({"type":"TakeQr", "generation":7, "epoch":3, "challenge_id":"challenge"})
            );
            let mut random = [0u8; 16];
            getrandom::fill(&mut random).unwrap();
            let token: String = random.iter().map(|b| format!("{b:02x}")).collect();
            let response = json!({"type":"PrivateQr", "value":token});
            server
                .write_all(&crate::ipc::encode_frame(&response).unwrap())
                .unwrap();
        });
        let result = pipe.take_qr(7, 3, "challenge").unwrap();
        assert_eq!(result.len(), 32);
    }

    #[test]
    fn every_feature_response_keeps_its_typed_correlation() {
        for (command, kind) in [
            ("QueryRetention", "Retention"),
            ("QueryCollection", "Collection"),
            ("QueryAutoStatus", "AutoStatus"),
            ("TestSample", "Sample"),
            ("BulkApproveReview", "BulkReview"),
            ("BulkDismissReview", "BulkReview"),
            ("PreviewAutoRoute", "AutoRoutePreview"),
        ] {
            assert!(expected(command, &json!({"type":kind,"payload":{}})));
            assert!(!expected(
                command,
                &json!({"type":"Acknowledged","payload":{"operation":command}})
            ));
        }
        assert!(!expected(
            "SaveTopic",
            &json!({"type":"Acknowledged","payload":{"operation":"SaveRoute"}})
        ));
    }

    #[test]
    fn matching_framed_response_is_returned() {
        let (client, mut server) = UnixStream::pair().unwrap();
        let reader = client.try_clone().unwrap();
        let bridge = WorkerBridge::new(client, reader, Duration::from_secs(1));
        std::thread::spawn(move || {
            let request = crate::ipc::read_frame(&mut server).unwrap().unwrap();
            let event = json!({"version":1,"id":request["id"],"type":"Topics","payload":[]});
            server
                .write_all(&crate::ipc::encode_frame(&event).unwrap())
                .unwrap();
        });
        assert_eq!(
            bridge
                .request(json!({"version":1,"id":"a","type":"QueryTopics","payload":{}}))
                .unwrap()["type"],
            "Topics"
        );
    }

    #[test]
    fn collection_reply_keeps_the_worker_connected_for_the_next_query() {
        let (client, mut server) = UnixStream::pair().unwrap();
        let reader = client.try_clone().unwrap();
        let bridge = WorkerBridge::new(client, reader, Duration::from_secs(1));
        let server_thread = std::thread::spawn(move || {
            for kind in ["Collection", "Topics"] {
                let request = crate::ipc::read_frame(&mut server).unwrap().unwrap();
                server
                    .write_all(
                        &crate::ipc::encode_frame(
                            &json!({"version":1,"id":request["id"],"type":kind,"payload":[]}),
                        )
                        .unwrap(),
                    )
                    .unwrap();
            }
        });
        assert_eq!(
            bridge
                .request(
                    json!({"version":1,"id":"collection","type":"QueryCollection","payload":{}})
                )
                .unwrap()["type"],
            "Collection"
        );
        assert_eq!(
            bridge
                .request(json!({"version":1,"id":"topics","type":"QueryTopics","payload":{}}))
                .unwrap()["type"],
            "Topics"
        );
        server_thread.join().unwrap();
        assert!(!bridge.revoked.load(Ordering::Acquire));
    }

    #[test]
    fn concurrent_routes_queries_wait_behind_an_active_exchange() {
        let (client, mut server) = UnixStream::pair().unwrap();
        server
            .set_read_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        server
            .set_write_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        let reader = client.try_clone().unwrap();
        let bridge = Arc::new(WorkerBridge::new(client, reader, Duration::from_secs(2)));
        let (entered, ready) = mpsc::sync_channel(1);
        let (release, released) = mpsc::sync_channel(1);
        let server_thread = std::thread::spawn(move || {
            for index in 0..4 {
                let request = crate::ipc::read_frame(&mut server).unwrap().unwrap();
                if index == 0 {
                    entered.send(()).unwrap();
                    released.recv_timeout(Duration::from_secs(2)).unwrap();
                }
                let (kind, payload) = match request["type"].as_str().unwrap() {
                    "QueryHistory" => ("History", json!([])),
                    "QueryRoutes" => ("Routes", json!([])),
                    "QueryTopics" => ("Topics", json!([])),
                    "QueryAccount" => (
                        "Account",
                        json!({"snapshot":{"account_id":null,"epoch":0,"auth_state":"logged_out","display_name":null,"network_state":"offline","revocation_pending":false},"challenge":null,"status":"idle","pending_revocations":[]}),
                    ),
                    _ => panic!("unexpected query"),
                };
                let event = json!({"version":1,"id":request["id"],"type":kind,"payload":payload});
                server
                    .write_all(&crate::ipc::encode_frame(&event).unwrap())
                    .unwrap();
            }
        });
        let active = bridge.clone();
        let first = std::thread::spawn(move || {
            active.request(json!({"version":1,"id":"active","type":"QueryHistory","payload":{}}))
        });
        ready.recv_timeout(Duration::from_secs(2)).unwrap();
        let mut pending = Vec::new();
        // Keep the first response blocked until all three Routes view requests
        // are admitted. This reproduces the former capacity-one failure without
        // depending on thread scheduling or sleeps.
        for (command, wanted) in [
            ("QueryRoutes", "Routes"),
            ("QueryTopics", "Topics"),
            ("QueryAccount", "Account"),
        ] {
            let command = json!({"version":1,"id":command,"type":command,"payload":{}});
            crate::ipc::validate_contract("Command", &command).unwrap();
            let (reply, received) = mpsc::sync_channel(1);
            assert!(
                bridge
                    .requests
                    .try_send(Request {
                        command,
                        reply,
                        started: None,
                        state: Arc::new(AtomicU8::new(REQUEST_QUEUED)),
                        sample_slot: None
                    })
                    .is_ok(),
                "concurrent Routes reads must fit the bounded queue"
            );
            pending.push((wanted, received));
        }
        release.send(()).unwrap();
        assert_eq!(first.join().unwrap().unwrap()["type"], "History");
        for (wanted, received) in pending {
            assert_eq!(
                received
                    .recv_timeout(Duration::from_secs(2))
                    .unwrap()
                    .unwrap()["type"],
                wanted
            );
        }
        server_thread.join().unwrap();
        assert!(!bridge.revoked.load(Ordering::Acquire));
    }

    #[test]
    fn archive_and_delivery_pages_roundtrip_without_losing_worker_connection() {
        let (client, mut server) = UnixStream::pair().unwrap();
        let reader = client.try_clone().unwrap();
        let bridge = WorkerBridge::new(client, reader, Duration::from_secs(2));
        let service = std::thread::spawn(move || {
            for _ in 0..4 {
                let request = crate::ipc::read_frame(&mut server).unwrap().unwrap();
                let (kind, payload) = match request["type"].as_str().unwrap() {
                    "QueryArchivePosts" => ("ArchivePosts", json!({"items":[],"next_cursor":null})),
                    "QueryArchiveRuns" => ("ArchiveRuns", json!({"items":[],"next_cursor":null})),
                    "QueryDeliveryJobs" => ("DeliveryJobs", json!({"items":[],"next_cursor":null})),
                    "QueryTopics" => ("Topics", json!([])),
                    _ => panic!("unexpected read"),
                };
                let event = json!({"version":1,"id":request["id"],"type":kind,"payload":payload});
                if server
                    .write_all(&crate::ipc::encode_frame(&event).unwrap())
                    .is_err()
                {
                    return;
                }
            }
        });
        let posts = bridge
            .request(json!({"version":1,"id":"posts","type":"QueryArchivePosts","payload":{}}))
            .unwrap();
        assert_eq!(posts["payload"]["items"], json!([]));
        let runs = bridge
            .request(json!({"version":1,"id":"runs","type":"QueryArchiveRuns","payload":{}}))
            .unwrap();
        assert_eq!(runs["payload"]["next_cursor"], Value::Null);
        let jobs = bridge.request(json!({"version":1,"id":"jobs","type":"QueryDeliveryJobs","payload":{"cursor":null,"limit":10,"status":null}})).unwrap();
        assert_eq!(jobs["payload"]["items"], json!([]));
        assert_eq!(
            bridge
                .request(json!({"version":1,"id":"alive","type":"QueryTopics","payload":{}}))
                .unwrap()["payload"],
            json!([])
        );
        service.join().unwrap();
        assert!(!bridge.revoked.load(Ordering::Acquire));
    }

    #[test]
    fn explicit_login_preparation_keeps_queued_reads_connected_without_replay() {
        let (client, mut server) = UnixStream::pair().unwrap();
        let reader = client.try_clone().unwrap();
        let bridge = Arc::new(WorkerBridge::new(client, reader, Duration::from_millis(80)));
        let (entered, ready) = mpsc::sync_channel(1);
        let (release, released) = mpsc::sync_channel(1);
        let service = std::thread::spawn(move || {
            let request = crate::ipc::read_frame(&mut server).unwrap().unwrap();
            assert_eq!(request["type"], "BeginTelegramLogin");
            entered.send(()).unwrap();
            released.recv_timeout(Duration::from_secs(2)).unwrap();
            let response = json!({"version":1,"id":request["id"],"type":"Acknowledged","payload":{"operation":"BeginTelegramLogin"}});
            if server
                .write_all(&crate::ipc::encode_frame(&response).unwrap())
                .is_err()
            {
                return;
            }
            let request = crate::ipc::read_frame(&mut server).unwrap().unwrap();
            assert_eq!(request["type"], "QueryTopics");
            let response = json!({"version":1,"id":request["id"],"type":"Topics","payload":[]});
            let _ = server.write_all(&crate::ipc::encode_frame(&response).unwrap());
        });
        let preparing = bridge.clone();
        let login = std::thread::spawn(move || {
            preparing
                .request(json!({"version":1,"id":"login","type":"BeginTelegramLogin","payload":{}}))
        });
        ready.recv_timeout(Duration::from_secs(2)).unwrap();
        let observing = bridge.clone();
        let read = std::thread::spawn(move || {
            observing
                .request(json!({"version":1,"id":"after-login","type":"QueryTopics","payload":{}}))
        });
        std::thread::sleep(Duration::from_millis(240));
        release.send(()).unwrap();
        assert_eq!(login.join().unwrap().unwrap()["type"], "Acknowledged");
        assert_eq!(read.join().unwrap().unwrap()["type"], "Topics");
        service.join().unwrap();
        assert!(!bridge.revoked.load(Ordering::Acquire));
    }

    #[test]
    fn slow_sample_and_queued_poll_keep_their_own_execution_deadlines() {
        let (client, mut server) = UnixStream::pair().unwrap();
        let reader = client.try_clone().unwrap();
        let bridge = Arc::new(WorkerBridge::new(client, reader, Duration::from_millis(80)));
        let (entered, ready) = mpsc::sync_channel(1);
        let (release, released) = mpsc::sync_channel(1);
        let server_thread = std::thread::spawn(move || {
            for index in 0..3 {
                let Ok(Some(request)) = crate::ipc::read_frame(&mut server) else {
                    return;
                };
                let event = if index == 0 {
                    assert_eq!(request["type"], "TestSample");
                    entered.send(()).unwrap();
                    released.recv_timeout(Duration::from_secs(2)).unwrap();
                    sample_response(&request["id"])
                } else {
                    assert_eq!(request["type"], "QueryTopics");
                    json!({"version":1,"id":request["id"],"type":"Topics","payload":[]})
                };
                if server
                    .write_all(&crate::ipc::encode_frame(&event).unwrap())
                    .is_err()
                {
                    return;
                }
            }
        });
        let active = bridge.clone();
        let sample = std::thread::spawn(move || {
            active.request(json!({"version":1,"id":"slow-sample","type":"TestSample","payload":{"text":"Synthetic local sample"}}))
        });
        ready.recv_timeout(Duration::from_secs(2)).unwrap();
        let polling = bridge.clone();
        let poll = std::thread::spawn(move || {
            polling
                .request(json!({"version":1,"id":"queued-poll","type":"QueryTopics","payload":{}}))
        });
        // Model verification/startup exceeds the ordinary read budget, while
        // the queued poll has not begun its own worker exchange yet.
        std::thread::sleep(Duration::from_millis(240));
        release.send(()).unwrap();
        assert_eq!(sample.join().unwrap().unwrap()["type"], "Sample");
        assert_eq!(poll.join().unwrap().unwrap()["type"], "Topics");
        assert_eq!(
            bridge
                .request(json!({"version":1,"id":"after-sample","type":"QueryTopics","payload":{}}))
                .unwrap()["type"],
            "Topics"
        );
        server_thread.join().unwrap();
        assert!(!bridge.revoked.load(Ordering::Acquire));
    }

    #[test]
    fn pause_and_stop_keep_short_total_deadlines_during_a_sample() {
        for kind in ["Pause", "Stop"] {
            let (client, mut server) = UnixStream::pair().unwrap();
            let reader = client.try_clone().unwrap();
            let bridge = Arc::new(WorkerBridge::new(client, reader, Duration::from_millis(80)));
            let (entered, ready) = mpsc::sync_channel(1);
            let (release, released) = mpsc::sync_channel(1);
            let server_thread = std::thread::spawn(move || {
                let request = crate::ipc::read_frame(&mut server).unwrap().unwrap();
                entered.send(()).unwrap();
                released.recv_timeout(Duration::from_secs(2)).unwrap();
                let _ = server.write_all(
                    &crate::ipc::encode_frame(&sample_response(&request["id"])).unwrap(),
                );
                // The expired lifecycle operation must never execute later.
                assert!(crate::ipc::read_frame(&mut server).unwrap().is_none());
            });
            let active = bridge.clone();
            let sample = std::thread::spawn(move || {
                active.request(json!({"version":1,"id":"active-sample","type":"TestSample","payload":{"text":"Synthetic local sample"}}))
            });
            ready.recv_timeout(Duration::from_secs(2)).unwrap();
            assert_eq!(bridge.request(json!({"version":1,"id":"duplicate-sample","type":"TestSample","payload":{"text":"Another synthetic sample"}})), Err("Worker busy"));
            assert!(!bridge.revoked.load(Ordering::Acquire));
            let start = Instant::now();
            assert!(bridge
                .request(json!({"version":1,"id":"cancel","type":kind,"payload":{}}))
                .is_err());
            assert!(
                start.elapsed() < Duration::from_millis(500),
                "{kind} must not inherit the longer sample deadline"
            );
            assert!(bridge.revoked.load(Ordering::Acquire));
            release.send(()).unwrap();
            assert!(sample.join().unwrap().is_err());
            server_thread.join().unwrap();
        }
    }

    #[test]
    fn sample_execution_still_has_a_bounded_terminal_deadline() {
        let (client, mut server) = UnixStream::pair().unwrap();
        let reader = client.try_clone().unwrap();
        let bridge = WorkerBridge::new(client, reader, Duration::from_millis(10));
        let (release, released) = mpsc::sync_channel(1);
        let server_thread = std::thread::spawn(move || {
            let request = crate::ipc::read_frame(&mut server).unwrap().unwrap();
            released.recv_timeout(Duration::from_secs(2)).unwrap();
            let _ = server
                .write_all(&crate::ipc::encode_frame(&sample_response(&request["id"])).unwrap());
        });
        let command = json!({"version":1,"id":"hung-sample","type":"TestSample","payload":{"text":"Synthetic local sample"}});
        // Exclude one-time schema compilation from the dispatched-work clock.
        crate::ipc::validate_contract("Command", &command).unwrap();
        let start = Instant::now();
        assert!(bridge.request(command).is_err());
        assert!(start.elapsed() < Duration::from_secs(1));
        assert!(bridge.revoked.load(Ordering::Acquire));
        release.send(()).unwrap();
        assert!(bridge
            .request(json!({"version":1,"id":"no-replay","type":"QueryTopics","payload":{}}))
            .is_err());
        server_thread.join().unwrap();
    }

    #[test]
    fn mismatch_latches_disconnection_without_replaying() {
        let (client, mut server) = UnixStream::pair().unwrap();
        let reader = client.try_clone().unwrap();
        let bridge = WorkerBridge::new(client, reader, Duration::from_secs(1));
        std::thread::spawn(move || {
            let _ = crate::ipc::read_frame(&mut server).unwrap().unwrap();
            server
                .write_all(
                    &crate::ipc::encode_frame(
                        &json!({"version":1,"id":"wrong","type":"Topics","payload":[]}),
                    )
                    .unwrap(),
                )
                .unwrap();
            let mut b = [0; 1];
            let _ = server.read(&mut b);
        });
        let request = json!({"version":1,"id":"a","type":"QueryTopics","payload":{}});
        assert!(bridge.request(request.clone()).is_err());
        assert!(bridge.request(request).is_err());
    }
}

#[cfg(all(test, unix))]
mod host_tests {
    use super::*;
    use serde_json::json;
    use std::{
        fs,
        os::unix::fs::PermissionsExt,
        path::{Path, PathBuf},
    };
    struct FailingEmbeddedClose;
    impl crate::embedded_threads::EmbeddedCollector for FailingEmbeddedClose {
        fn available(&self) -> bool {
            false
        }
        fn attach(&self) -> std::io::Result<u64> {
            Err(std::io::Error::other("fixture unavailable"))
        }
        fn snapshot(&self, _: u64) -> std::io::Result<Value> {
            Err(std::io::Error::other("fixture unavailable"))
        }
        fn scroll(&self, _: u64) -> std::io::Result<()> {
            Err(std::io::Error::other("fixture unavailable"))
        }
        fn poll(&self, _: u64) -> bool {
            false
        }
        fn release(&self, _: u64) -> std::io::Result<()> {
            Err(std::io::Error::other("fixture close unconfirmed"))
        }
        fn close_all(&self) -> std::io::Result<()> {
            Err(std::io::Error::other("fixture close unconfirmed"))
        }
    }
    struct BlockingEmbeddedClose {
        revoked: AtomicBool,
        release: Mutex<mpsc::Receiver<()>>,
    }
    struct TicketedEmbeddedClose {
        generation: AtomicU64,
        open: AtomicBool,
    }
    impl crate::embedded_threads::EmbeddedCollector for TicketedEmbeddedClose {
        fn revoke(&self) -> u64 {
            self.generation.load(Ordering::Acquire)
        }
        fn close_generation(&self, generation: u64) -> std::io::Result<()> {
            if generation == self.generation.load(Ordering::Acquire) {
                self.open.store(false, Ordering::Release);
            }
            Ok(())
        }
        fn available(&self) -> bool {
            self.open.load(Ordering::Acquire)
        }
        fn attach(&self) -> std::io::Result<u64> {
            Ok(self.generation.load(Ordering::Acquire))
        }
        fn snapshot(&self, _: u64) -> std::io::Result<Value> {
            Err(std::io::Error::other("fixture unavailable"))
        }
        fn scroll(&self, _: u64) -> std::io::Result<()> {
            Ok(())
        }
        fn poll(&self, generation: u64) -> bool {
            self.available() && generation == self.generation.load(Ordering::Acquire)
        }
        fn release(&self, generation: u64) -> std::io::Result<()> {
            self.close_generation(generation)
        }
        fn close_all(&self) -> std::io::Result<()> {
            self.open.store(false, Ordering::Release);
            Ok(())
        }
    }
    #[test]
    fn captured_cleanup_ticket_does_not_close_a_new_user_opened_generation() {
        let root =
            std::env::temp_dir().join(format!("threadrelay-close-ticket-{}", std::process::id()));
        fs::create_dir(&root).unwrap();
        let host = WorkerHost::new(&root, &root.join("no-worker")).unwrap();
        let native = Arc::new(TicketedEmbeddedClose {
            generation: AtomicU64::new(7),
            open: AtomicBool::new(true),
        });
        host.set_embedded(native.clone()).unwrap();
        let old_job = host.revoke_embedded();
        native.generation.store(8, Ordering::Release);
        native.open.store(true, Ordering::Release);
        host.close_embedded(old_job.as_ref()).unwrap();
        assert!(native.open.load(Ordering::Acquire));
        assert_eq!(native.generation.load(Ordering::Acquire), 8);
        drop(host);
        fs::remove_dir_all(root).unwrap();
    }
    impl crate::embedded_threads::EmbeddedCollector for BlockingEmbeddedClose {
        fn revoke(&self) -> u64 {
            self.revoked.store(true, Ordering::Release);
            1
        }
        fn available(&self) -> bool {
            false
        }
        fn attach(&self) -> std::io::Result<u64> {
            Err(std::io::Error::other("fixture unavailable"))
        }
        fn snapshot(&self, _: u64) -> std::io::Result<Value> {
            Err(std::io::Error::other("fixture unavailable"))
        }
        fn scroll(&self, _: u64) -> std::io::Result<()> {
            Err(std::io::Error::other("fixture unavailable"))
        }
        fn poll(&self, _: u64) -> bool {
            false
        }
        fn release(&self, _: u64) -> std::io::Result<()> {
            self.close_all()
        }
        fn close_all(&self) -> std::io::Result<()> {
            let _ = self
                .release
                .lock()
                .unwrap()
                .recv_timeout(Duration::from_secs(2));
            Ok(())
        }
    }
    #[test]
    fn application_shutdown_destroys_browser_but_power_only_detaches_capture() {
        struct LifecycleProbe(std::sync::mpsc::SyncSender<&'static str>);
        impl crate::embedded_threads::EmbeddedCollector for LifecycleProbe {
            fn revoke(&self) -> u64 {
                7
            }
            fn close_generation(&self, _: u64) -> std::io::Result<()> {
                let _ = self.0.try_send("detach");
                Ok(())
            }
            fn available(&self) -> bool {
                false
            }
            fn attach(&self) -> std::io::Result<u64> {
                Err(std::io::Error::other("unavailable"))
            }
            fn snapshot(&self, _: u64) -> std::io::Result<Value> {
                Err(std::io::Error::other("unavailable"))
            }
            fn scroll(&self, _: u64) -> std::io::Result<()> {
                Err(std::io::Error::other("unavailable"))
            }
            fn poll(&self, _: u64) -> bool {
                false
            }
            fn release(&self, _: u64) -> std::io::Result<()> {
                Ok(())
            }
            fn close_all(&self) -> std::io::Result<()> {
                let _ = self.0.try_send("destroy");
                Ok(())
            }
        }
        for (mode, wanted) in [("power", "detach"), ("shutdown", "destroy")] {
            let root = std::env::temp_dir().join(format!(
                "threadrelay-browser-life-{}-{mode}",
                std::process::id()
            ));
            fs::create_dir(&root).unwrap();
            let host = Arc::new(WorkerHost::new(&root, &root.join("no-worker")).unwrap());
            let (sent, received) = mpsc::sync_channel(8);
            host.set_embedded(Arc::new(LifecycleProbe(sent))).unwrap();
            if mode == "power" {
                host.power_event(true);
            } else {
                host.shutdown();
            }
            assert_eq!(
                received.recv_timeout(Duration::from_secs(2)).unwrap(),
                wanted
            );
            drop(host);
            fs::remove_dir_all(root).unwrap();
        }
    }
    #[test]
    fn power_and_shutdown_revoke_without_waiting_for_native_close() {
        for mode in ["power", "shutdown"] {
            let root = std::env::temp_dir().join(format!(
                "threadrelay-native-close-nonblocking-{}-{mode}",
                std::process::id()
            ));
            fs::create_dir(&root).unwrap();
            let host = Arc::new(WorkerHost::new(&root, &root.join("no-worker")).unwrap());
            let (release, receiver) = mpsc::channel();
            let native = Arc::new(BlockingEmbeddedClose {
                revoked: AtomicBool::new(false),
                release: Mutex::new(receiver),
            });
            host.set_embedded(native.clone()).unwrap();
            let (done, returned) = mpsc::sync_channel(1);
            let active = host.clone();
            let thread = std::thread::spawn(move || {
                if mode == "power" {
                    active.power_event(true);
                } else {
                    active.shutdown();
                }
                let _ = done.send(());
            });
            let nonblocking = returned.recv_timeout(Duration::from_millis(250)).is_ok();
            let revoked = native.revoked.load(Ordering::Acquire);
            let _ = release.send(());
            drop(release);
            thread.join().unwrap();
            drop(host);
            assert!(
                nonblocking,
                "native UI cleanup cannot block {mode} callback"
            );
            assert!(revoked, "{mode} must revoke before returning");
            fs::remove_dir_all(root).unwrap();
        }
    }
    #[test]
    fn native_close_failure_does_not_prevent_pause_or_stop_revocation() {
        for kind in ["Pause", "Stop"] {
            let root = std::env::temp_dir().join(format!(
                "threadrelay-close-error-{}-{kind}",
                std::process::id()
            ));
            let (host, _, _, _) = create_host_fixture(&root, None);
            host.set_embedded(Arc::new(FailingEmbeddedClose)).unwrap();
            let command = |kind: &str| json!({"version":1,"id":kind,"type":kind,"payload":{}});
            host.request(command("InitializeLocalWorkspace")).unwrap();
            assert_eq!(
                host.request(command(kind)).unwrap_err(),
                "Embedded Threads close unconfirmed"
            );
            if kind == "Stop" {
                assert!(host.active().is_none());
            } else {
                let state = host.state.lock().unwrap();
                let HostPhase::Active(active) = &state.phase else {
                    panic!("pause must preserve queries")
                };
                assert!(
                    active.broker.driver_consumed(),
                    "Pause must revoke even when native close fails"
                );
                drop(state);
                assert!(matches!(
                    host.request(command("QueryState")).unwrap()["payload"]["state"].as_str(),
                    Some("paused" | "stopped")
                ));
            }
            drop(host);
            fs::remove_dir_all(root).unwrap();
        }
    }

    fn create_host_fixture(
        root: &Path,
        marker: Option<&Path>,
    ) -> (WorkerHost, PathBuf, PathBuf, PathBuf) {
        let resources = root.join("resources");
        let profile = root.join("profile");
        let worker = resources.join("sidecars/threadrelay-worker");
        fs::create_dir_all(worker.parent().unwrap()).unwrap();
        let python =
            PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../worker/.venv/bin/python");
        let marker_line = marker
            .map(|path| format!("echo spawned > '{}'\n", path.display()))
            .unwrap_or_default();
        fs::write(
            &worker,
            format!(
                "#!/bin/sh\n{marker_line}exec '{}' -m threadrelay.packaged_worker\n",
                python.display()
            ),
        )
        .unwrap();
        fs::set_permissions(&worker, fs::Permissions::from_mode(0o700)).unwrap();
        let host = WorkerHost::new(&profile, &worker).unwrap();
        install_fixture_preparation(&host, &resources, &profile, &worker);
        (host, resources, profile, worker)
    }

    fn synthetic_fixture_resources(
        resources: &Path,
        profile: &Path,
        worker: &Path,
    ) -> (
        crate::verified_artifacts::GuardedResources,
        crate::verified_artifacts::VerifiedArtifactSet,
    ) {
        use sha2::{Digest, Sha256};
        let bytes = fs::read(worker).unwrap();
        let inventory = crate::bundle_inventory::Inventory::new(vec![
            crate::bundle_inventory::Entry::Directory {
                path: "sidecars".into(),
            },
            crate::bundle_inventory::Entry::File {
                path: "sidecars/threadrelay-worker".into(),
                sha256: format!("{:x}", Sha256::digest(&bytes)),
                size: bytes.len() as u64,
                executable: true,
            },
        ])
        .unwrap();
        crate::verified_artifacts::synthetic_resources(resources, profile, &inventory).unwrap()
    }

    fn install_fixture_preparation(
        host: &WorkerHost,
        resources: &Path,
        profile: &Path,
        worker: &Path,
    ) {
        let resources = resources.to_owned();
        let profile = profile.to_owned();
        let worker = worker.to_owned();
        *host.preparation_hook.lock().unwrap() = Some(Arc::new(move |owner| {
            let (guarded, guard) = synthetic_fixture_resources(&resources, &profile, &worker);
            owner.retain_artifacts(guard);
            Some(guarded)
        }));
    }

    #[test]
    fn delayed_secure_status_does_not_block_queries_or_publish_a_replaced_generation() {
        use crate::credentials::{CredentialError, CredentialVault, SecretBytes};
        use std::sync::mpsc;
        struct DelayedVault {
            entered: mpsc::SyncSender<()>,
            release: Mutex<mpsc::Receiver<()>>,
        }
        impl CredentialVault for DelayedVault {
            fn put(&self, _: &str, _: &[u8]) -> Result<(), CredentialError> {
                panic!("passive status wrote credentials")
            }
            fn contains(&self, reference: &str) -> Result<bool, CredentialError> {
                assert_eq!(reference, "vault:threadrelay_account");
                self.entered.send(()).unwrap();
                self.release
                    .lock()
                    .unwrap()
                    .recv_timeout(Duration::from_secs(10))
                    .unwrap();
                Ok(true)
            }
            fn get(&self, _: &str) -> Result<SecretBytes, CredentialError> {
                panic!("passive status decrypted secret")
            }
            fn delete(&self, _: &str) -> Result<(), CredentialError> {
                panic!("passive status deleted credentials")
            }
        }
        let root =
            std::env::temp_dir().join(format!("threadrelay-delayed-status-{}", std::process::id()));
        assert!(!root.exists());
        let (host, resources, profile, worker) = create_host_fixture(&root, None);
        let host = Arc::new(host);
        let command = |kind: &str| json!({"version":1,"id":kind,"type":kind,"payload":{}});
        host.request(command("InitializeLocalWorkspace")).unwrap();
        let first = host.active().unwrap().0;
        let (entered, entry) = mpsc::sync_channel(1);
        let (release, wait) = mpsc::sync_channel(1);
        let pending = host.clone();
        let status = std::thread::spawn(move || {
            pending.secure_status(&DelayedVault {
                entered,
                release: Mutex::new(wait),
            })
        });
        entry.recv_timeout(Duration::from_secs(2)).unwrap();
        let reading = host.clone();
        let (answer, read) = mpsc::sync_channel(1);
        let query = std::thread::spawn(move || {
            let active = reading.active().is_some();
            let result = reading.request(
                json!({"version":1,"id":"while-vault-blocked","type":"QueryState","payload":{}}),
            );
            let _ = answer.send((active, result));
        });
        let response = read.recv_timeout(Duration::from_millis(300));
        if response.is_ok() {
            host.request(command("Stop")).unwrap();
            install_fixture_preparation(&host, &resources, &profile, &worker);
            host.request(command("InitializeLocalWorkspace")).unwrap();
            assert!(host.active().unwrap().0 > first);
        }
        // Always release and clean the owned fixture before asserting red, so
        // a regression cannot strand the test threads behind the vault gate.
        release.send(()).unwrap();
        let observed = status.join().unwrap();
        query.join().unwrap();
        host.request(command("Stop")).unwrap();
        drop(host);
        fs::remove_dir_all(root).unwrap();
        assert!(
            response.as_ref().is_ok_and(|(active, event)| *active
                && event.as_ref().is_ok_and(|value| value["type"] == "State")),
            "a delayed vault lookup held WorkerHost state and blocked queries"
        );
        assert_eq!(
            observed,
            json!({"available":false,"configured":false,"reason":"app_not_ready"}),
            "status from a replaced generation must not be promoted"
        );
    }

    #[test]
    fn credential_configuration_never_starts_a_worker() {
        use crate::credentials::{CredentialError, CredentialVault, SecretBytes};
        struct Vault;
        impl CredentialVault for Vault {
            fn put(&self, _: &str, _: &[u8]) -> Result<(), CredentialError> {
                panic!("vault write without worker")
            }
            fn get(&self, _: &str) -> Result<SecretBytes, CredentialError> {
                Err(CredentialError::NotFound)
            }
            fn delete(&self, _: &str) -> Result<(), CredentialError> {
                Ok(())
            }
        }
        let root =
            std::env::temp_dir().join(format!("threadrelay-secure-test-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        let host = WorkerHost::new(&root, &root.join("no-worker")).unwrap();
        assert!(host
            .secure_configure(&Vault, "12345", &"a".repeat(32))
            .is_err());
        drop(host);
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn stop_during_blocked_preparation_prevents_late_worker_and_start_only_own_cleanup() {
        for kind in ["Stop", "Pause"] {
            blocked_preparation_cancellation(kind);
        }
    }
    fn blocked_preparation_cancellation(cancel_kind: &str) {
        use std::sync::Barrier;
        let root = std::env::temp_dir().join(format!(
            "threadrelay-preparation-cancel-{}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&root);
        let marker = root.join("spawned");
        let (host, resources, profile, worker) = create_host_fixture(&root, Some(&marker));
        let host = Arc::new(host);
        let entered = Arc::new(Barrier::new(2));
        let released = Arc::new(Barrier::new(2));
        let (e, r) = (entered.clone(), released.clone());
        let (hook_resources, hook_profile, hook_worker) =
            (resources.clone(), profile.clone(), worker.clone());
        *host.preparation_hook.lock().unwrap() = Some(Arc::new(move |owner| {
            let (guarded, guard) =
                synthetic_fixture_resources(&hook_resources, &hook_profile, &hook_worker);
            owner.retain_artifacts(guard);
            e.wait();
            r.wait();
            Some(guarded)
        }));
        let command =
            |kind: &str, payload| json!({"version":1,"id":kind,"type":kind,"payload":payload});
        let pending = host.clone();
        let thread = std::thread::spawn(move || {
            pending.request(json!({"version":1,"id":"start","type":"Start","payload":{"feed_url":"https://www.threads.com/","mode":"dry_run"}}))
        });
        entered.wait();
        let stop = host.request(command(cancel_kind, json!({})));
        released.wait();
        let result = thread.join().unwrap();
        assert!(
            result.is_err(),
            "cancelled preparation must not publish late Start authority"
        );
        assert!(stop.is_ok(), "Stop must reach pending preparation");
        let stop = stop.unwrap();
        crate::ipc::validate_contract("Event", &stop).unwrap();
        assert_eq!(stop["payload"]["state"], "stopping");
        assert!(
            !marker.exists(),
            "cancelled preparation must not spawn worker"
        );
        assert!(host.active().is_none());
        assert_eq!(
            fs::read_dir(profile.join("execution-staging"))
                .unwrap()
                .count(),
            0
        );
        install_fixture_preparation(&host, &resources, &profile, &worker);
        host.request(command("InitializeLocalWorkspace", json!({})))
            .unwrap();
        assert!(
            host.active().is_some(),
            "later generation must have fresh cancellation scope"
        );
        host.request(command("Stop", json!({}))).unwrap();
        drop(host);
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn deliberate_start_replaces_consumed_driver_generation_only_after_cleanup() {
        let root =
            std::env::temp_dir().join(format!("threadrelay-driver-replace-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let (host, resources, profile, worker) = create_host_fixture(&root, None);
        let cmd =
            |kind: &str, payload| json!({"version":1,"id":kind,"type":kind,"payload":payload});
        host.request(cmd("InitializeLocalWorkspace", json!({})))
            .unwrap();
        let first = {
            let state = host.state.lock().unwrap();
            match &state.phase {
                HostPhase::Active(a) => {
                    assert!(!a.broker.driver_consumed());
                    a.number
                }
                _ => panic!(),
            }
        };
        host.request(cmd("Pause", json!({}))).unwrap();
        {
            let state = host.state.lock().unwrap();
            match &state.phase {
                HostPhase::Active(a) => assert!(a.broker.driver_consumed()),
                _ => panic!(),
            }
        }
        assert_eq!(
            host.request(cmd("QueryTopics", json!({}))).unwrap()["type"],
            "Topics"
        );
        assert_eq!(host.active().unwrap().0, first);
        install_fixture_preparation(&host, &resources, &profile, &worker);
        let response = host
            .request(cmd(
                "Start",
                json!({"feed_url":"https://www.threads.com/","mode":"dry_run"}),
            ))
            .unwrap();
        assert_eq!(response["payload"]["state"], "needs_attention");
        assert!(host.active().unwrap().0 > first);
        host.request(cmd("Stop", json!({}))).unwrap();
        drop(host);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn reconnect_replaces_a_paused_revoked_generation_without_starting_collection() {
        let root = std::env::temp_dir().join(format!(
            "threadrelay-reconnect-paused-{}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&root);
        let (host, resources, profile, worker) = create_host_fixture(&root, None);
        let cmd = |kind: &str| json!({"version":1,"id":kind,"type":kind,"payload":{}});
        host.request(cmd("InitializeLocalWorkspace")).unwrap();
        let first = host.active().unwrap().0;
        host.request(cmd("Pause")).unwrap();
        install_fixture_preparation(&host, &resources, &profile, &worker);
        let result = host.request(cmd("InitializeLocalWorkspace")).unwrap();
        assert_eq!(result["type"], "State");
        assert_eq!(result["payload"]["state"], "stopped");
        assert!(host.active().unwrap().0 > first);
        host.request(cmd("Stop")).unwrap();
        drop(host);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn power_revokes_current_generation_and_stale_cleanup_cannot_close_replacement() {
        let root =
            std::env::temp_dir().join(format!("threadrelay-power-test-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let (host, resources, profile, worker) = create_host_fixture(&root, None);
        let host = Arc::new(host);
        let cmd = |kind: &str| json!({"version":1,"id":kind,"type":kind,"payload":{}});
        host.request(cmd("InitializeLocalWorkspace")).unwrap();
        let first = host.active().unwrap().0;
        host.power_event(true);
        assert!(host.request(cmd("QueryState")).is_err());
        assert!(host.request(cmd("InitializeLocalWorkspace")).is_err());
        host.power_event(false);
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        while !matches!(host.state.lock().unwrap().phase, HostPhase::Disconnected) {
            assert!(std::time::Instant::now() < deadline);
            std::thread::sleep(Duration::from_millis(10));
        }
        assert!(host.request(cmd("QueryState")).is_err());
        install_fixture_preparation(&host, &resources, &profile, &worker);
        host.request(cmd("InitializeLocalWorkspace")).unwrap();
        let next = host.active().unwrap().0;
        assert!(next > first);
        host.invalidate(first); // delayed event captured the older generation
        host.power_event(false); // duplicate native wake is not a new revocation
        assert_eq!(host.active().unwrap().0, next);
        host.request(cmd("Stop")).unwrap();
        drop(host);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn collection_controls_keep_the_same_account_worker_generation() {
        let root = std::env::temp_dir().join(format!(
            "threadrelay-collection-lifetime-{}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&root);
        let (host, _, _, _) = create_host_fixture(&root, None);
        let cmd = |kind: &str| json!({"version":1,"id":kind,"type":kind,"payload":{}});
        host.request(cmd("InitializeLocalWorkspace")).unwrap();
        let generation = host.active().unwrap().0;
        // No remembered credentials: startup restore is a passive no-op, never
        // a legacy Keychain read or an authentication/collection start.
        assert_eq!(
            host.request(cmd("RestoreTelegramSession")).unwrap()["type"],
            "Acknowledged"
        );
        let queue = host.request(cmd("QueryRouteQueue")).unwrap();
        assert_eq!(queue["type"], "RouteQueue");
        assert_eq!(queue["payload"]["items"], json!([]));
        for kind in ["SuspendCollection", "StopCollection"] {
            assert_eq!(host.request(cmd(kind)).unwrap()["type"], "State");
            assert_eq!(host.active().unwrap().0, generation);
            {
                let state = host.state.lock().unwrap();
                if let HostPhase::Active(active) = &state.phase {
                    assert!(!active.broker.driver_consumed());
                } else {
                    panic!("worker replaced");
                }
            }
            assert_eq!(
                host.request(cmd("QueryAccount")).unwrap()["type"],
                "Account"
            );
        }
        host.request(cmd("Stop")).unwrap();
        assert!(host.active().is_none());
        drop(host);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn explicit_initialization_is_only_spawn_and_persists_across_stop() {
        let root =
            std::env::temp_dir().join(format!("threadrelay-host-test-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let (host, resources, profile, worker) = create_host_fixture(&root, None);
        let cmd =
            |kind: &str, payload| json!({"version":1,"id":kind,"type":kind,"payload":payload});
        assert!(host.request(cmd("QueryTopics", json!({}))).is_err());
        assert_eq!(
            host.request(cmd("InitializeLocalWorkspace", json!({})))
                .unwrap()["type"],
            "State"
        );
        let topic = json!({"id":"a","version":1,"description":"Local","positive_examples":["y"],"negative_examples":[],"exclusions":[],"enabled":true});
        assert_eq!(
            host.request(cmd("SaveTopic", topic.clone())).unwrap()["type"],
            "Acknowledged"
        );
        // A private synthetic submission remains denied by the native gate;
        // it never enters ordinary Command/Event framing.
        assert!(host
            .secure_submit("challenge", "code", 0, "synthetic-value".to_owned())
            .is_err());
        assert_eq!(
            host.request(cmd("QueryTopics", json!({}))).unwrap()["type"],
            "Topics"
        );

        assert_eq!(
            host.request(cmd("Stop", json!({}))).unwrap()["payload"]["state"],
            "stopped"
        );
        assert!(host.request(cmd("QueryAccount", json!({}))).is_err());
        assert!(host.request(cmd("QueryTopics", json!({}))).is_err());
        install_fixture_preparation(&host, &resources, &profile, &worker);
        assert_eq!(
            host.request(cmd("InitializeLocalWorkspace", json!({})))
                .unwrap()["type"],
            "State"
        );
        assert_eq!(
            host.request(cmd("QueryTopics", json!({}))).unwrap()["payload"],
            json!([topic])
        );
        assert_eq!(
            host.request(cmd("Stop", json!({}))).unwrap()["type"],
            "State"
        );
        drop(host);
        fs::remove_dir_all(root).unwrap();
    }
}

#[cfg(all(test, unix, debug_assertions))]
mod development_tests {
    use super::*;
    use serde_json::json;
    use std::{
        fs,
        os::unix::fs::{DirBuilderExt, PermissionsExt},
    };

    struct LocalRoot(PathBuf);
    impl LocalRoot {
        fn new() -> Self {
            crate::dev_paths::resources().unwrap();
            let project = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .ancestors()
                .nth(3)
                .unwrap()
                .to_owned();
            let root = project
                .join(".cache/threadrelay-dev")
                .join(format!("source-worker-test-{}", std::process::id()));
            assert!(
                !root.exists(),
                "prior local test state must be inspected before reuse"
            );
            let mut builder = fs::DirBuilder::new();
            builder.mode(0o700).create(&root).unwrap();
            Self(root)
        }
    }
    impl Drop for LocalRoot {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    #[test]
    #[ignore = "Opt-in real source worker and pinned immutable native snapshots; no account or native client"]
    fn actual_connected_worker_initializes_auth_form_without_connecting_account() {
        use crate::credentials::{CredentialError, CredentialVault, SecretBytes};
        struct NoAccountVault;
        impl CredentialVault for NoAccountVault {
            fn contains(&self, reference: &str) -> Result<bool, CredentialError> {
                assert_eq!(reference, "vault:threadrelay_connection_account");
                Ok(false)
            }
            fn put(&self, _: &str, _: &[u8]) -> Result<(), CredentialError> {
                panic!("no-account fixture attempted credential write")
            }
            fn get(&self, reference: &str) -> Result<SecretBytes, CredentialError> {
                assert_eq!(reference, "vault:threadrelay_connection_account");
                Err(CredentialError::NotFound)
            }
            fn delete(&self, _: &str) -> Result<(), CredentialError> {
                panic!("no-account fixture attempted credential deletion")
            }
        }
        let root = LocalRoot::new();
        let host = WorkerHost::connected(&root.0).unwrap();
        let command = |kind: &str| json!({"version":1,"id":kind,"type":kind,"payload":{}});
        assert_eq!(
            host.request(command("InitializeLocalWorkspace")).unwrap()["type"],
            "State"
        );
        assert_eq!(
            host.secure_status(&NoAccountVault),
            json!({"available":true,"configured":false,"reason":null})
        );
        let account = host.request(command("QueryAccount")).unwrap();
        assert_eq!(account["type"], "Account");
        assert!(account["payload"]["snapshot"]["account_id"].is_null());
        assert_eq!(account["payload"]["snapshot"]["auth_state"], "logged_out");
        assert!(
            !root.0.join("telegram").exists(),
            "workspace initialization must never create/open native account data"
        );
        let broker = {
            let state = host.state.lock().unwrap();
            match &state.phase {
                HostPhase::Active(active) => active.broker.clone(),
                _ => panic!(),
            }
        };
        assert!(broker.tdlib_auth_ready());
        assert!(
            broker.inference_ready(),
            "the selected local runtime snapshot must be independently available"
        );
        assert!(!broker.tdlib_ready());
        assert!(!broker.browser_ready());
        assert!(host.request(json!({"version":1,"id":"deny","type":"Start","payload":{"feed_url":"https://www.threads.com/","mode":"auto"}})).is_err());
        assert_eq!(host.request(command("Stop")).unwrap()["type"], "State");
        assert!(!broker.tdlib_auth_ready());
        assert!(!root.0.join("telegram").exists());
        drop(host);
    }

    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    #[test]
    #[ignore = "Opt-in real connected-worker Q4 sample using an isolated APFS model clone; no account or send"]
    fn actual_connected_sample_finishes_and_keeps_subsequent_queries_usable() {
        use std::{ffi::CString, os::unix::ffi::OsStrExt};
        crate::dev_paths::resources().unwrap();
        let project = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .ancestors()
            .nth(3)
            .unwrap()
            .to_owned();
        let root = LocalRoot(
            project
                .join(".cache/threadrelay-dev")
                .join(format!("source-worker-sample-test-{}", std::process::id())),
        );
        assert!(
            !root.0.exists(),
            "prior isolated sample state must be inspected before reuse"
        );
        fs::DirBuilder::new().mode(0o700).create(&root.0).unwrap();
        let models = root.0.join("models");
        fs::DirBuilder::new().mode(0o700).create(&models).unwrap();
        let name = "h2o-lightning-4b-Q4_K_M.gguf";
        let source = project
            .join(".cache/threadrelay-dev/app-data/models")
            .join(name);
        let destination = models.join(name);
        let source_c = CString::new(source.as_os_str().as_bytes()).unwrap();
        let destination_c = CString::new(destination.as_os_str().as_bytes()).unwrap();
        // Independent COW inode, never a hardlink or a change to the live
        // workspace's source model. The normal runtime verifies all model pins.
        assert_eq!(
            unsafe { libc::clonefile(source_c.as_ptr(), destination_c.as_ptr(), 0) },
            0,
            "isolated APFS clone: {}",
            std::io::Error::last_os_error()
        );
        fs::set_permissions(&destination, fs::Permissions::from_mode(0o600)).unwrap();
        let host = WorkerHost::connected(&root.0).unwrap();
        let command = |kind: &str, payload: Value| json!({"version":1,"id":kind,"type":kind,"payload":payload});
        assert_eq!(
            host.request(command("InitializeLocalWorkspace", json!({})))
                .unwrap()["type"],
            "State"
        );
        let topic = json!({"id":"synthetic-ai-topic","version":1,"description":"AI model releases and local AI development","positive_examples":["A new AI language model is available for local inference."],"negative_examples":["A recipe for dinner."],"exclusions":[],"enabled":true});
        assert_eq!(
            host.request(command("SaveTopic", topic)).unwrap()["type"],
            "Acknowledged"
        );
        let start = Instant::now();
        let sample = host.request(command("TestSample", json!({"text":"A new AI language model release supports local inference and developer tools."}))).unwrap();
        eprintln!(
            "Isolated connected TestSample elapsed: {:?}",
            start.elapsed()
        );
        assert_eq!(
            sample["type"], "Sample",
            "the actual verified runtime must finish within its bounded sample deadline"
        );
        assert_eq!(sample["payload"]["sending_possible"], false);
        assert_eq!(
            sample["payload"]["classification"]["topic_results"][0]["topic_id"],
            "synthetic-ai-topic"
        );
        assert_eq!(
            host.request(command("QueryCollection", json!({}))).unwrap()["type"],
            "Collection"
        );
        assert_eq!(
            host.request(command("QueryState", json!({}))).unwrap()["payload"]["state"],
            "stopped"
        );
        assert!(!root.0.join("telegram").exists());
        assert_eq!(
            host.request(command("Stop", json!({}))).unwrap()["type"],
            "State"
        );
        assert!(host.active().is_none());
        assert!(host.clean(0));
        drop(host);
    }

    #[test]
    #[ignore = "requires the prepared checkout-local Python 3.12.16 runtime"]
    fn actual_development_worker_roundtrip_persistence_and_repeated_shutdown() {
        use crate::credentials::{CredentialError, CredentialVault, SecretBytes};
        struct NeverVault;
        impl CredentialVault for NeverVault {
            fn put(&self, _: &str, _: &[u8]) -> Result<(), CredentialError> {
                panic!("development accessed vault")
            }
            fn get(&self, _: &str) -> Result<SecretBytes, CredentialError> {
                panic!("development accessed vault")
            }
            fn delete(&self, _: &str) -> Result<(), CredentialError> {
                panic!("development accessed vault")
            }
        }
        let root = LocalRoot::new();
        let host = WorkerHost::development(&root.0).unwrap();
        let command = |kind: &str, payload: Value| json!({"version":1,"id":kind,"type":kind,"payload":payload});
        let native_commands = [
            command(
                "Start",
                json!({"feed_url":"https://www.threads.com/","mode":"dry_run"}),
            ),
            command("OpenThreadsLogin", json!({})),
            command("BeginTelegramLogin", json!({})),
            command("TestSample", json!({"text":"Inert local test"})),
        ];
        for request in &native_commands {
            assert_eq!(
                host.request(request.clone()),
                Err("Unavailable in local development mode")
            );
        }
        assert!(
            host.active().is_none(),
            "forbidden operations must not initialize the worker"
        );
        assert!(!root.0.join("threadrelay.sqlite3").exists());
        assert_eq!(
            host.secure_status(&NeverVault),
            json!({"available":false,"configured":false,"reason":"telegram_runtime_unavailable"})
        );
        assert!(host
            .secure_configure(&NeverVault, "12345", &"a".repeat(32))
            .is_err());
        assert!(host
            .secure_submit("challenge", "code", 0, "inert".to_owned())
            .is_err());
        assert!(host.secure_qr("challenge", 0).is_err());
        let topic = json!({"id":"local-topic","version":1,"description":"Local development topic","positive_examples":["local"],"negative_examples":[],"exclusions":[],"enabled":true});
        let route = json!({"id":"local-route","version":1,"topic_ids":["local-topic"],"destination_id":"-100123","mode":"review","enabled":false,"account_id":"123","account_epoch":0,"sender":{"kind":"user","id":"123"}});
        let retention = json!({"text_days":5,"dedup_days":30,"diagnostic_days":7,"confirmed":true});
        for generation in 1..=3 {
            assert_eq!(
                host.request(command("InitializeLocalWorkspace", json!({})))
                    .unwrap()["type"],
                "State"
            );
            assert_eq!(host.active().unwrap().0, generation);
            assert_eq!(
                host.request(command("InitializeLocalWorkspace", json!({})))
                    .unwrap()["type"],
                "State"
            );
            assert_eq!(
                host.active().unwrap().0,
                generation,
                "duplicate initialization must retain the active worker"
            );
            assert!(
                DesktopSupervisor::acquire(&root.0).is_err(),
                "one database owner required"
            );
            std::thread::scope(|scope| {
                let pending: Vec<_> = [
                    ("QueryRoutes", "Routes"),
                    ("QueryTopics", "Topics"),
                    ("QueryAccount", "Account"),
                    ("QueryDestination", "DestinationStatus"),
                    ("QueryAutoStatus", "AutoStatus"),
                ]
                .into_iter()
                .map(|(kind, wanted)| {
                    let host = &host;
                    scope.spawn(move || {
                        (
                            wanted,
                            host.request(json!({"version":1,"id":kind,"type":kind,"payload":{}})),
                        )
                    })
                })
                .collect();
                for request in pending {
                    let (wanted, result) = request.join().unwrap();
                    let event = result.unwrap();
                    assert_eq!(event["type"], wanted);
                    if wanted == "DestinationStatus" {
                        assert!(event["payload"]["snapshot"].is_null());
                    }
                    if wanted == "AutoStatus" {
                        assert_eq!(event["payload"]["eligible"], false);
                    }
                }
            });
            let broker = {
                let state = host.state.lock().unwrap();
                match &state.phase {
                    HostPhase::Active(a) => a.broker.clone(),
                    _ => panic!(),
                }
            };
            assert!(!broker.tdlib_ready());
            assert!(!broker.browser_ready());
            assert!(!broker.inference_ready());
            assert_eq!(
                host.secure_status(&NeverVault),
                json!({"available":false,"configured":false,"reason":"telegram_runtime_unavailable"})
            );
            if generation == 1 {
                assert_eq!(
                    host.request(command("SaveTopic", topic.clone())).unwrap()["type"],
                    "Acknowledged"
                );
                assert_eq!(
                    host.request(command("SaveRoute", route.clone())).unwrap()["type"],
                    "Acknowledged"
                );
                assert_eq!(
                    host.request(command("SaveRetention", retention.clone()))
                        .unwrap()["type"],
                    "Acknowledged"
                );
            }
            assert_eq!(
                host.request(command("QueryTopics", json!({}))).unwrap()["payload"],
                json!([topic])
            );
            assert_eq!(
                host.request(command("QueryRoutes", json!({}))).unwrap()["payload"],
                json!([route])
            );
            assert_eq!(
                host.request(command("QueryRetention", json!({}))).unwrap()["payload"],
                retention
            );
            for kind in ["QueryHistory", "QueryReview"] {
                assert_eq!(
                    host.request(command(kind, json!({}))).unwrap()["payload"],
                    json!([])
                );
            }
            assert_eq!(
                host.request(command("QueryAccount", json!({}))).unwrap()["payload"]["snapshot"]
                    ["auth_state"],
                "logged_out"
            );
            assert_eq!(
                host.request(command("QueryDestination", json!({})))
                    .unwrap()["type"],
                "DestinationStatus"
            );
            assert_eq!(
                host.request(command("QueryAutoStatus", json!({}))).unwrap()["type"],
                "AutoStatus"
            );
            let readiness = host
                .request(command(
                    "QueryReadiness",
                    json!({"feed_url":"https://www.threads.com/","mode":"review"}),
                ))
                .unwrap();
            assert_eq!(readiness["type"], "Readiness");
            assert_eq!(readiness["payload"]["can_start"], false);
            for capability in ["browser", "inference", "telegram", "automatic"] {
                assert_eq!(readiness["payload"][capability], "unavailable");
            }
            for request in &native_commands {
                assert!(host.request(request.clone()).is_err());
            }
            let mut enabled = route.clone();
            enabled["enabled"] = json!(true);
            assert_eq!(
                host.request(command("SaveRoute", enabled)),
                Err("Unavailable in local development mode")
            );
            assert_eq!(
                host.request(command("Stop", json!({}))).unwrap()["payload"]["state"],
                "stopped"
            );
            assert!(host.active().is_none());
            assert!(host.request(command("Stop", json!({}))).is_err());
            assert!(
                host.active().is_none(),
                "duplicate Stop must never initialize a worker"
            );
            assert!(host.clean(0), "owned generation shutdown must be confirmed");
            assert!(host.request(command("QueryTopics", json!({}))).is_err());
        }
        assert_eq!(
            &fs::read(root.0.join("threadrelay.sqlite3")).unwrap()[..16],
            b"SQLite format 3\0"
        );
        assert_eq!(
            fs::metadata(&root.0).unwrap().permissions().mode() & 0o777,
            0o700
        );
        drop(host);
        assert!(DesktopSupervisor::acquire(&root.0).is_ok());
        let packaged = WorkerHost::new(
            &root.0,
            &root.0.join("resources/sidecars/threadrelay-worker"),
        )
        .unwrap();
        assert!(
            packaged
                .request(command("InitializeLocalWorkspace", json!({})))
                .is_err(),
            "ordinary constructor must still reject empty production inventory"
        );
    }
}

#[cfg(target_os = "macos")]
struct UnavailableVault;
#[cfg(target_os = "macos")]
impl crate::credentials::CredentialVault for UnavailableVault {
    fn put(&self, _: &str, _: &[u8]) -> Result<(), crate::credentials::CredentialError> {
        Err(crate::credentials::CredentialError::Unavailable)
    }
    fn get(
        &self,
        _: &str,
    ) -> Result<crate::credentials::SecretBytes, crate::credentials::CredentialError> {
        Err(crate::credentials::CredentialError::Unavailable)
    }
    fn delete(&self, _: &str) -> Result<(), crate::credentials::CredentialError> {
        Err(crate::credentials::CredentialError::Unavailable)
    }
}
