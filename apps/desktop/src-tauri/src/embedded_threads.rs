//! Same-session embedded Threads authority. The remote child has no IPC rights;
//! only this host selects navigation and the compiled read-only snapshot script.
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::io;
use std::path::Path;

pub const SNAPSHOT_MAX_BYTES: usize = 1_000_000;
struct GenerationGate(std::sync::atomic::AtomicU64);
impl GenerationGate {
    fn new() -> Self {
        Self(std::sync::atomic::AtomicU64::new(0))
    }
    fn activate(&self, generation: u64) {
        self.0
            .store((generation << 1) | 1, std::sync::atomic::Ordering::Release);
    }
    fn live(&self, generation: u64) -> bool {
        self.0.load(std::sync::atomic::Ordering::Acquire) == ((generation << 1) | 1)
    }
    fn revoke(&self) -> u64 {
        self.0.fetch_and(!1, std::sync::atomic::Ordering::AcqRel) >> 1
    }
}
// Independent from the persistent view generation: each attachment gets a
// fresh authority epoch, and hide/Stop permanently revokes that epoch.
struct CaptureGate(std::sync::atomic::AtomicU64);
impl CaptureGate {
    fn new() -> Self {
        Self(std::sync::atomic::AtomicU64::new(0))
    }
    fn attach(&self) -> io::Result<u64> {
        self.0
            .try_update(
                std::sync::atomic::Ordering::AcqRel,
                std::sync::atomic::Ordering::Acquire,
                |old| {
                    let next = (old >> 1).checked_add(1)?;
                    (next <= 9_007_199_254_740_991).then_some((next << 1) | 1)
                },
            )
            .map(|old| (old >> 1) + 1)
            .map_err(|_| unavailable())
    }
    fn epoch(&self) -> u64 {
        self.0.load(std::sync::atomic::Ordering::Acquire) >> 1
    }
    fn live(&self, epoch: u64) -> bool {
        epoch != 0 && self.0.load(std::sync::atomic::Ordering::Acquire) == ((epoch << 1) | 1)
    }
    fn revoke(&self) -> u64 {
        self.0.fetch_and(!1, std::sync::atomic::Ordering::AcqRel) >> 1
    }
}
struct VisibilityIntent(std::sync::atomic::AtomicU64);
impl VisibilityIntent {
    fn new() -> Self {
        Self(std::sync::atomic::AtomicU64::new(1))
    }
    fn request(&self, revision: u64, show: bool) -> io::Result<()> {
        if revision == 0 || revision > 9_007_199_254_740_991 {
            return Err(unavailable());
        }
        self.0
            .try_update(
                std::sync::atomic::Ordering::AcqRel,
                std::sync::atomic::Ordering::Acquire,
                |old| (revision > (old >> 1)).then_some((revision << 1) | u64::from(show)),
            )
            .map(|_| ())
            .map_err(|_| unavailable())
    }
    fn current(&self, revision: u64, show: bool) -> bool {
        revision <= 9_007_199_254_740_991
            && self.0.load(std::sync::atomic::Ordering::Acquire)
                == ((revision << 1) | u64::from(show))
    }
    fn shown(&self) -> bool {
        self.0.load(std::sync::atomic::Ordering::Acquire) & 1 == 1
    }
}
fn handoff_retained<T>(sender: &std::sync::mpsc::SyncSender<T>, value: T, release: impl FnOnce(T)) {
    if let Err(error) = sender.try_send(value) {
        match error {
            std::sync::mpsc::TrySendError::Full(value)
            | std::sync::mpsc::TrySendError::Disconnected(value) => release(value),
        }
    }
}
fn clear_unsubmitted_close(
    current: u64,
    closing: &mut Option<std::sync::Arc<std::sync::atomic::AtomicBool>>,
    generation: u64,
    attempt: &std::sync::Arc<std::sync::atomic::AtomicBool>,
    submitted: bool,
) {
    if !submitted
        && current == generation
        && closing
            .as_ref()
            .is_some_and(|pending| std::sync::Arc::ptr_eq(pending, attempt))
    {
        *closing = None;
    }
}
fn release_owns_attachment(
    requested: u64,
    current: u64,
    epoch: u64,
    attached_epoch: u64,
) -> io::Result<bool> {
    if requested == 0 || requested > current || epoch == 0 {
        return Err(unavailable());
    }
    Ok(requested == current && epoch == attached_epoch)
}

const CHILD_LABEL: &str = "threads-embedded";
const THREADS_URL: &str = "https://www.threads.com/";

pub trait EmbeddedCollector: Send + Sync {
    /// Immediate authority revocation; must not wait for any UI or mutex.
    fn revoke(&self) -> u64 {
        0
    }
    fn close_generation(&self, generation: u64) -> io::Result<()> {
        self.release(generation)
    }
    fn capture_epoch(&self, _generation: u64) -> u64 {
        0
    }
    fn snapshot_lease(&self, generation: u64, _epoch: u64) -> io::Result<Value> {
        self.snapshot(generation)
    }
    fn scroll_lease(&self, generation: u64, _epoch: u64) -> io::Result<()> {
        self.scroll(generation)
    }
    fn release_lease(&self, generation: u64, _epoch: u64) -> io::Result<()> {
        self.release(generation)
    }
    fn available(&self) -> bool;
    fn attach(&self) -> io::Result<u64>;
    fn snapshot(&self, generation: u64) -> io::Result<Value>;
    fn scroll(&self, generation: u64) -> io::Result<()>;
    fn poll(&self, generation: u64) -> bool;
    fn release(&self, generation: u64) -> io::Result<()>;
    fn close_all(&self) -> io::Result<()>;
}

/// One worker generation's binding to an already-open native view. Callers
/// cannot manufacture provenance or substitute a different webview generation.
pub(crate) struct EmbeddedAuthority {
    host: std::sync::Arc<dyn EmbeddedCollector>,
    lease: std::sync::Mutex<Option<(u64, u64, String)>>,
}
impl EmbeddedAuthority {
    pub(crate) fn new(host: std::sync::Arc<dyn EmbeddedCollector>) -> Self {
        Self {
            host,
            lease: std::sync::Mutex::new(None),
        }
    }
    pub(crate) fn available(&self) -> bool {
        self.host.available()
    }
    pub(crate) fn attach(&self) -> io::Result<Value> {
        let mut lease = self.lease.lock().map_err(|_| unavailable())?;
        if lease.is_some() || !self.host.available() {
            return Err(unavailable());
        }
        let mut random = [0u8; 32];
        getrandom::fill(&mut random).map_err(|_| unavailable())?;
        let source: String = random.iter().map(|v| format!("{v:02x}")).collect();
        let generation = self.host.attach()?;
        if generation == 0 {
            return Err(unavailable());
        }
        *lease = Some((
            generation,
            self.host.capture_epoch(generation),
            source.clone(),
        ));
        Ok(serde_json::json!({"webview_generation":generation,"source_session_id":source}))
    }
    fn lease_epoch(&self, generation: u64) -> Option<u64> {
        self.lease
            .lock()
            .ok()?
            .as_ref()
            .filter(|(g, _, _)| *g == generation)
            .map(|(_, epoch, _)| *epoch)
    }
    fn current_epoch(&self, generation: u64, epoch: u64) -> bool {
        generation != 0
            && self.lease_epoch(generation) == Some(epoch)
            && self.host.capture_epoch(generation) == epoch
            && self.host.poll(generation)
            && self.host.capture_epoch(generation) == epoch
            && self.lease_epoch(generation) == Some(epoch)
    }
    fn current(&self, generation: u64) -> bool {
        self.lease_epoch(generation)
            .is_some_and(|epoch| self.current_epoch(generation, epoch))
    }
    pub(crate) fn snapshot(&self, generation: u64) -> io::Result<Value> {
        if !self.current(generation) {
            return Err(unavailable());
        }
        let epoch = self
            .lease
            .lock()
            .map_err(|_| unavailable())?
            .as_ref()
            .filter(|(g, _, _)| *g == generation)
            .map(|(_, e, _)| *e)
            .ok_or_else(unavailable)?;
        let value = self.host.snapshot_lease(generation, epoch)?;
        if !self.current_epoch(generation, epoch) {
            return Err(unavailable());
        }
        checked_snapshot(&serde_json::to_string(&value).map_err(|_| unavailable())?)
    }
    pub(crate) fn scroll(&self, generation: u64) -> io::Result<()> {
        if !self.current(generation) {
            return Err(unavailable());
        }
        let epoch = self
            .lease
            .lock()
            .map_err(|_| unavailable())?
            .as_ref()
            .filter(|(g, _, _)| *g == generation)
            .map(|(_, e, _)| *e)
            .ok_or_else(unavailable)?;
        self.host.scroll_lease(generation, epoch)?;
        if !self.current_epoch(generation, epoch) {
            return Err(unavailable());
        }
        Ok(())
    }
    pub(crate) fn poll(&self, generation: u64) -> bool {
        self.current(generation)
    }
    pub(crate) fn release(&self, generation: u64) -> io::Result<()> {
        let mut lease = self.lease.lock().map_err(|_| unavailable())?;
        let epoch = lease
            .as_ref()
            .filter(|(g, _, _)| *g == generation)
            .map(|(_, e, _)| *e)
            .ok_or_else(unavailable)?;
        self.host.release_lease(generation, epoch)?;
        *lease = None;
        Ok(())
    }
    pub(crate) fn cleanup(&self) {
        let generation = self
            .lease
            .lock()
            .ok()
            .and_then(|s| s.as_ref().map(|(g, _, _)| *g));
        if let Some(generation) = generation {
            let _ = self.release(generation);
        }
    }
}

#[derive(Clone, Copy, Deserialize, Debug)]
#[serde(deny_unknown_fields)]
pub struct Bounds {
    pub x: f64,
    pub y: f64,
    pub width: f64,
    pub height: f64,
}

#[derive(Clone, Copy, Serialize, Debug, PartialEq, Eq)]
pub struct Status {
    pub open: bool,
    pub generation: u64,
}

fn unavailable() -> io::Error {
    io::Error::other("Embedded Threads unavailable")
}
pub(crate) fn trusted_invoker(label: &str, url: &url::Url, development: bool) -> bool {
    if label != "main" || !url.username().is_empty() || url.password().is_some() {
        return false;
    }
    let packaged =
        url.port().is_none() && url.host_str() == Some("localhost") && url.scheme() == "tauri";
    let webview2 = url.port().is_none()
        && url.host_str() == Some("tauri.localhost")
        && matches!(url.scheme(), "http" | "https");
    let local_dev = development
        && url.scheme() == "http"
        && url.host_str() == Some("127.0.0.1")
        && url.port() == Some(1420);
    packaged || webview2 || local_dev
}
fn threads_origin(url: &url::Url) -> bool {
    url.scheme() == "https"
        && url.username().is_empty()
        && url.password().is_none()
        && url.port().is_none()
        && matches!(
            url.host_str(),
            Some("threads.com" | "www.threads.com" | "threads.net" | "www.threads.net")
        )
}
fn navigation_allowed(url: &url::Url) -> bool {
    if threads_origin(url) {
        return true;
    }
    url.scheme() == "https"
        && url.username().is_empty()
        && url.password().is_none()
        && url.port().is_none()
        && url.host_str() == Some("www.instagram.com")
        && (url.path() == "/accounts/login"
            || url.path().starts_with("/accounts/login/")
            // The Threads "Continue with Instagram" button first visits SSO,
            // then returns through OIDC after the owner completes sign-in.
            || matches!(url.path(), "/threads/sso/" | "/oauth/oidc/")
            || url.path().starts_with("/challenge/")
            || url.path().starts_with("/accounts/two_factor_login/"))
}

fn location_state(url: &url::Url) -> &'static str {
    if !navigation_allowed(url) {
        return "unexpected_navigation";
    }
    if !threads_origin(url)
        || url.path().starts_with("/login")
        || url.path().starts_with("/challenge")
    {
        return "manual_login_or_challenge";
    }
    if url.query().is_none() && url.fragment().is_none() && url.path() == "/" {
        "ready"
    } else {
        "unexpected_navigation"
    }
}

fn store_identifier(root: &Path) -> [u8; 16] {
    let mut hash = Sha256::new();
    hash.update(b"app.threadrelay.desktop/embedded-threads/v1\0");
    hash.update(root.as_os_str().as_encoded_bytes());
    let mut id: [u8; 16] = hash.finalize()[..16]
        .try_into()
        .expect("fixed digest length");
    id[6] = (id[6] & 0x0f) | 0x50;
    id[8] = (id[8] & 0x3f) | 0x80;
    id
}

impl Bounds {
    fn clamped(self, width: f64, height: f64) -> io::Result<Self> {
        if ![self.x, self.y, self.width, self.height, width, height]
            .iter()
            .all(|v| v.is_finite())
            || self.x < 0.0
            || self.y < 0.0
            || self.width <= 0.0
            || self.height <= 0.0
            || width <= self.x
            || height <= self.y
            || width > 16384.0
            || height > 16384.0
        {
            return Err(unavailable());
        }
        Ok(Self {
            width: self.width.min(width - self.x),
            height: self.height.min(height - self.y),
            ..self
        })
    }
}

fn empty_snapshot(state: &str) -> Value {
    serde_json::json!({"schema_version":1,"state":state,"cards":[],"scroll_y":0,"viewport_height":1})
}

fn checked_snapshot(raw: &str) -> io::Result<Value> {
    if raw.len() > SNAPSHOT_MAX_BYTES {
        return Ok(empty_snapshot("snapshot_limit"));
    }
    let mut framed = Vec::with_capacity(raw.len() + 4);
    framed.extend_from_slice(&(raw.len() as u32).to_be_bytes());
    framed.extend_from_slice(raw.as_bytes());
    let value = crate::ipc::read_frame(&mut std::io::Cursor::new(framed))
        .map_err(|_| unavailable())?
        .ok_or_else(unavailable)?;
    let object = value.as_object().ok_or_else(unavailable)?;
    if object.len() != 5
        || value["schema_version"] != 1
        || !matches!(
            value["state"].as_str(),
            Some(
                "ready"
                    | "manual_login_or_challenge"
                    | "unexpected_navigation"
                    | "unknown_layout"
                    | "snapshot_limit"
            )
        )
        || !value["scroll_y"]
            .as_f64()
            .is_some_and(|n| n.is_finite() && (0.0..=1e9).contains(&n))
        || !value["viewport_height"]
            .as_f64()
            .is_some_and(|n| n.is_finite() && (1.0..=16384.0).contains(&n))
    {
        return Err(unavailable());
    }
    let cards = value["cards"].as_array().ok_or_else(unavailable)?;
    if cards.len() > 100 {
        return Ok(empty_snapshot("snapshot_limit"));
    }
    if cards.iter().any(|c| !c.is_object()) || (value["state"] != "ready" && !cards.is_empty()) {
        return Err(unavailable());
    }
    Ok(value)
}

#[cfg(feature = "desktop")]
mod native {
    use super::*;
    use std::{
        path::PathBuf,
        sync::{
            atomic::{AtomicBool, Ordering},
            Arc, Mutex,
        },
        time::Duration,
    };
    use tauri::{AppHandle, Manager, Webview};
    struct Slot {
        generation: u64,
        closed_through: u64,
        view: Option<Webview>,
        opening: bool,
        attached: bool,
        background_capture: bool,
        capture_epoch: u64,
        visibility: Arc<VisibilityIntent>,
        fixture: bool,
        closing: Option<Arc<AtomicBool>>,
    }
    pub struct EmbeddedThreads {
        root: PathBuf,
        supported: bool,
        gate: Arc<GenerationGate>,
        capture: CaptureGate,
        operations: Mutex<()>,
        slot: Mutex<Slot>,
        #[cfg(windows)]
        profile: Option<crate::embedded_windows::PrivateProfile>,
    }
    fn refresh(slot: &mut Slot) {
        if slot
            .closing
            .as_ref()
            .is_some_and(|v| v.load(Ordering::Acquire))
        {
            slot.closed_through = slot.generation;
            slot.view = None;
            slot.closing = None;
            slot.attached = false;
        }
    }
    impl EmbeddedThreads {
        pub fn new(root: &Path) -> io::Result<Self> {
            if !root.is_absolute() {
                return Err(unavailable());
            }
            let canonical = root.canonicalize()?;
            #[cfg(unix)]
            if canonical != root {
                return Err(unavailable());
            }
            #[cfg(windows)]
            let profile = crate::embedded_windows::private_profile(&canonical).ok();
            let supported = platform_supported();
            #[cfg(windows)]
            let supported = supported && profile.is_some();
            Ok(Self {
                #[cfg(windows)]
                profile,
                root: canonical,
                supported,
                gate: Arc::new(GenerationGate::new()),
                capture: CaptureGate::new(),
                operations: Mutex::new(()),
                slot: Mutex::new(Slot {
                    generation: 0,
                    closed_through: 0,
                    view: None,
                    opening: false,
                    attached: false,
                    background_capture: false,
                    capture_epoch: 0,
                    visibility: Arc::new(VisibilityIntent::new()),
                    fixture: false,
                    closing: None,
                }),
            })
        }
        pub fn status(&self) -> Status {
            let mut slot = self.slot.lock().unwrap_or_else(|e| e.into_inner());
            refresh(&mut slot);
            Status {
                open: slot.view.is_some() || slot.opening,
                generation: slot.generation,
            }
        }
        pub fn open(&self, app: &AppHandle, bounds: Bounds) -> io::Result<Status> {
            self.open_inner(app, bounds, false)
        }
        fn open_inner(&self, app: &AppHandle, bounds: Bounds, fixture: bool) -> io::Result<Status> {
            if !self.supported {
                return Err(unavailable());
            }
            #[cfg(windows)]
            self.profile.as_ref().ok_or_else(unavailable)?.recheck()?;
            #[cfg(not(all(debug_assertions, feature = "development-worker")))]
            if fixture {
                return Err(unavailable());
            }
            let window = app.get_window("main").ok_or_else(unavailable)?;
            let size = window.inner_size().map_err(|_| unavailable())?;
            let scale = window.scale_factor().map_err(|_| unavailable())?;
            let bounds = bounds.clamped(
                f64::from(size.width) / scale,
                f64::from(size.height) / scale,
            )?;
            let generation = {
                let mut slot = self.slot.lock().map_err(|_| unavailable())?;
                refresh(&mut slot);
                if slot.opening || slot.closing.is_some() {
                    return Err(unavailable());
                }
                if slot.view.is_some() {
                    if slot.fixture != fixture {
                        return Err(unavailable());
                    }
                    if !self.gate.live(slot.generation) {
                        return Err(unavailable());
                    }
                    let generation = slot.generation;
                    drop(slot);
                    self.resize(generation, bounds)?;
                    return Ok(Status {
                        open: true,
                        generation,
                    });
                }
                if app.get_webview(CHILD_LABEL).is_some() {
                    return Err(unavailable());
                }
                slot.generation = slot
                    .generation
                    .checked_add(1)
                    .filter(|v| *v <= 9_007_199_254_740_991)
                    .ok_or_else(unavailable)?;
                slot.opening = true;
                slot.visibility = Arc::new(VisibilityIntent::new());
                slot.attached = false;
                slot.background_capture = false;
                slot.fixture = fixture;
                self.gate.activate(slot.generation);
                slot.generation
            };
            let nav_gate = self.gate.clone();
            #[cfg(all(debug_assertions, feature = "development-worker", target_os = "macos"))]
            let fixture_loaded = Arc::new(AtomicBool::new(false));
            let fixed_url = if fixture { "about:blank" } else { THREADS_URL };
            let builder = tauri::webview::WebviewBuilder::new(
                CHILD_LABEL,
                tauri::WebviewUrl::External(fixed_url.parse().expect("fixed URL")),
            )
            .devtools(false)
            .on_navigation(move |url| {
                #[cfg(all(debug_assertions, feature = "development-worker", target_os = "macos"))]
                if fixture {
                    eprintln!("ThreadRelay fixture navigation: {url}");
                }
                let allowed = nav_gate.live(generation)
                    && if fixture {
                        url.as_str() == "about:blank"
                    } else {
                        navigation_allowed(url)
                    };
                #[cfg(all(debug_assertions, feature = "development-worker"))]
                if !allowed && !fixture {
                    // Fixed route categories only: never log credentials, query
                    // parameters, user handles or platform challenge identifiers.
                    let origin = match url.host_str() {
                        Some("www.instagram.com") => "instagram",
                        Some("www.threads.com" | "threads.com") => "threads",
                        _ => "other",
                    };
                    let mut parts = url.path().split('/').filter(|part| !part.is_empty());
                    let first = parts.next().unwrap_or("");
                    let category = match first {
                        "accounts" | "oauth" | "threads" | "auth_platform" | "challenge"
                        | "checkpoint" | "login" => first,
                        _ => "other",
                    };
                    eprintln!(
                        "ThreadRelay blocked navigation: origin={origin} category={category}"
                    );
                }
                allowed
            })
            .on_new_window(|_, _| tauri::webview::NewWindowResponse::Deny)
            .on_download(|_, _| false)
            .on_permission_request(|_, _| tauri::webview::PermissionResponse::Deny);
            #[cfg(all(debug_assertions, feature = "development-worker", target_os = "macos"))]
            let builder = if fixture {
                let loaded = fixture_loaded.clone();
                builder.on_page_load(move |_, payload| {
                    eprintln!(
                        "ThreadRelay fixture load: {:?} {}",
                        payload.event(),
                        payload.url()
                    );
                    if payload.event() == tauri::webview::PageLoadEvent::Finished
                        && payload.url().as_str() == "about:blank"
                    {
                        loaded.store(true, Ordering::Release);
                    }
                })
            } else {
                builder
            };
            #[cfg(target_os = "macos")]
            let builder = builder.data_store_identifier(store_identifier(&self.root));
            #[cfg(windows)]
            let builder = builder
                .data_directory(
                    self.profile
                        .as_ref()
                        .ok_or_else(unavailable)?
                        .path()
                        .to_owned(),
                )
                .browser_extensions_enabled(false);
            let result = window.add_child(
                builder,
                tauri::LogicalPosition::new(bounds.x, bounds.y),
                tauri::LogicalSize::new(bounds.width, bounds.height),
            );
            let cancelled = !self.gate.live(generation);
            let view = match result {
                Ok(view) => view,
                Err(_) => {
                    let mut slot = self.slot.lock().map_err(|_| unavailable())?;
                    if slot.generation == generation {
                        slot.opening = false;
                        self.gate.revoke();
                    }
                    return Err(unavailable());
                }
            };
            {
                let mut slot = self.slot.lock().map_err(|_| unavailable())?;
                if slot.generation != generation {
                    return Err(unavailable());
                }
                slot.view = Some(view);
                slot.opening = false;
            }
            if cancelled || !self.gate.live(generation) {
                let _ = self.close(generation);
                return Err(unavailable());
            }
            #[cfg(all(debug_assertions, feature = "development-worker", target_os = "macos"))]
            if fixture {
                eprintln!("ThreadRelay fixture waiting for native load");
                // Tauri deliberately omits an initial about:blank URL. Submit
                // the fixed navigation after registration so WebKit commits a
                // document and Wry can retain evaluation callbacks.
                self.view(generation, false)?
                    .navigate("about:blank".parse().expect("fixed fixture URL"))
                    .map_err(|_| unavailable())?;
                // Wry queues scripts before the initial navigation commits but
                // does not retain their result callbacks. Wait for the native
                // load event before exercising callback-based fixture scripts.
                let deadline = std::time::Instant::now() + Duration::from_secs(15);
                while !fixture_loaded.load(Ordering::Acquire) {
                    if std::time::Instant::now() >= deadline {
                        let _ = self.close(generation);
                        return Err(unavailable());
                    }
                    std::thread::sleep(Duration::from_millis(25));
                }
            }
            Ok(Status {
                open: true,
                generation,
            })
        }
        #[cfg(all(debug_assertions, feature = "development-worker", target_os = "macos"))]
        pub(crate) fn run_local_fixture(self: &Arc<Self>, app: &AppHandle) -> io::Result<Value> {
            let note = |stage: &str| {
                use std::io::Write;
                let path=Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../.cache/completion-2026-10-31/browser/native-webkit-fixture.progress.txt");
                if let Ok(mut file) = std::fs::OpenOptions::new()
                    .create(true)
                    .append(true)
                    .open(path)
                {
                    let _ = writeln!(file, "{} {stage}", std::process::id());
                }
            };
            note("begin");
            let bounds = Bounds {
                x: 180.0,
                y: 160.0,
                width: 650.0,
                height: 420.0,
            };
            let first = self.open_inner(app, bounds, true)?;
            note("child_created");
            let view = self.view(first.generation, false)?;
            let html =
                serde_json::to_string(include_str!("../../../../fixtures/threads/basic.html"))
                    .map_err(|_| unavailable())?;
            eval_fixed(&view,&format!("(()=>{{document.open();document.write({html});document.close();return true;}})()"))?;
            if !visible(&view)? {
                return Err(unavailable());
            }
            note("html_loaded_visible");
            let main = app.get_webview("main").ok_or_else(unavailable)?;
            let (tx, rx) = std::sync::mpsc::sync_channel(1);
            main.with_webview(move |p| {
                let _ = tx.try_send(unsafe { tr_embedded_retain(p.inner()) } as usize);
            })
            .map_err(|_| unavailable())?;
            let main_raw = rx
                .recv_timeout(Duration::from_secs(2))
                .map_err(|_| unavailable())?;
            let (tx, rx) = std::sync::mpsc::sync_channel(1);
            view.with_webview(move |p| {
                let distinct = unsafe {
                    tr_embedded_distinct_store(main_raw as *mut std::ffi::c_void, p.inner()) == 1
                };
                unsafe {
                    tr_embedded_detached_release(main_raw as *mut std::ffi::c_void);
                };
                let _ = tx.try_send(distinct);
            })
            .map_err(|_| unavailable())?;
            if !rx
                .recv_timeout(Duration::from_secs(2))
                .map_err(|_| unavailable())?
            {
                return Err(unavailable());
            }
            note("native_store_isolated");
            // Test-only lexical location stands in for the public origin; the
            // actual view remains about:blank and never requests a service.
            let script=format!("(()=>{{const location={{protocol:'https:',hostname:'www.threads.com',pathname:'/',port:'',search:'',hash:''}};return ({})();}})()",include_str!("../native/embedded-snapshot.js"));
            let snapshot = checked_snapshot(&eval_fixed(&view, &script)?)?;
            let visible_cards = snapshot["cards"].as_array().map(|cards| {
                cards
                    .iter()
                    .filter(|card| card["author"] == "@alice" && card["issue"].is_null())
                    .count()
            });
            if snapshot["state"] != "ready"
                || visible_cards != Some(1)
                || snapshot["cards"]
                    .as_array()
                    .is_none_or(|cards| cards.len() != 2 || cards[1]["issue"] != "not_visible")
            {
                return Err(unavailable());
            }
            let denial="(()=>{window.__threadrelayFixtureInvoke='pending';if(!window.__TAURI_INTERNALS__)window.__threadrelayFixtureInvoke='denied';else window.__TAURI_INTERNALS__.invoke('embedded_threads_status',{}).then(()=>window.__threadrelayFixtureInvoke='allowed',()=>window.__threadrelayFixtureInvoke='denied');return true;})()";
            eval_fixed(&view, denial)?;
            let mut denied = false;
            for _ in 0..40 {
                let value = eval_fixed(&view, "window.__threadrelayFixtureInvoke")?;
                if value == "\"denied\"" {
                    denied = true;
                    break;
                }
                if value == "\"allowed\"" {
                    return Err(unavailable());
                }
                std::thread::sleep(Duration::from_millis(25));
            }
            if !denied {
                return Err(unavailable());
            }
            note("child_invoke_denied");
            // Tauri dispatches these native mutations asynchronously. Require
            // their observed result, allowing the window animation to finish.
            let await_visibility = |expected: bool| -> io::Result<()> {
                let deadline = std::time::Instant::now() + Duration::from_secs(3);
                loop {
                    if visible(&view).is_ok_and(|current| current == expected) {
                        return Ok(());
                    }
                    if std::time::Instant::now() >= deadline {
                        return Err(unavailable());
                    }
                    std::thread::sleep(Duration::from_millis(25));
                }
            };
            view.hide().map_err(|_| unavailable())?;
            await_visibility(false)?;
            note("native_hidden_observed");
            view.show().map_err(|_| unavailable())?;
            await_visibility(true)?;
            note("native_shown_observed");
            view.window().minimize().map_err(|_| unavailable())?;
            await_visibility(false)?;
            note("native_minimized_observed");
            view.window().unminimize().map_err(|_| unavailable())?;
            await_visibility(true)?;
            note("hide_and_minimize_denied");
            let old = EmbeddedAuthority::new(self.clone());
            old.attach()?;
            eval_fixed(&view, "window.__threadrelayPersistentFixture=7")?;
            self.hide(first.generation, 1)?;
            if visible(&view)?
                || old.poll(first.generation)
                || old.snapshot(first.generation).is_ok()
                || old.scroll(first.generation).is_ok()
            {
                return Err(unavailable());
            }
            self.show(first.generation, 2, bounds)?;
            if self.status() != first
                || eval_fixed(&view, "window.__threadrelayPersistentFixture")? != "7"
                || old.poll(first.generation)
            {
                return Err(unavailable());
            }
            let next = EmbeddedAuthority::new(self.clone());
            next.attach()?;
            old.release(first.generation)?;
            if !next.poll(first.generation) {
                return Err(unavailable());
            }
            self.background(first.generation, 3)?;
            if !next.poll(first.generation) {
                return Err(unavailable());
            }
            if eval_fixed(&view, "document.visibilityState")? != "\"visible\"" {
                return Err(unavailable());
            }
            let background_snapshot = checked_snapshot(&eval_fixed(&view, &script)?)?;
            if background_snapshot["state"] != "ready"
                || background_snapshot["cards"][0]["author"] != "@alice"
            {
                return Err(unavailable());
            }
            eval_fixed(&view, "document.body.style.minHeight='2000px'")?;
            let scroll_script=format!("(()=>{{const location={{protocol:'https:',hostname:'www.threads.com',pathname:'/',port:'',search:'',hash:''}};return ({})();}})()",include_str!("../native/embedded-scroll.js"));
            let background_scroll = checked_snapshot(&eval_fixed(&view, &scroll_script)?)?;
            if background_scroll["state"] != "ready"
                || background_scroll["scroll_y"].as_f64().unwrap_or(0.0) <= 0.0
            {
                return Err(unavailable());
            }
            eval_fixed(&view, "window.scrollTo(0,0)")?;
            eval_fixed(&view, "window.__threadrelayBackgroundTick=0;requestAnimationFrame(()=>{window.__threadrelayBackgroundTick=1})")?;
            let deadline = std::time::Instant::now() + Duration::from_secs(3);
            while eval_fixed(&view, "window.__threadrelayBackgroundTick")? != "1" {
                if std::time::Instant::now() >= deadline {
                    return Err(unavailable());
                }
                std::thread::sleep(Duration::from_millis(25));
            }
            self.show(first.generation, 4, bounds)?;
            if !next.poll(first.generation) {
                return Err(unavailable());
            }
            next.release(first.generation)?;
            if self.status() != first || !visible(&view)? {
                return Err(unavailable());
            }
            let lease = self.attach()?;
            let ticket = self.revoke();
            self.close_generation(ticket)?;
            if self.poll(lease) || self.status() != first || !visible(&view)? {
                return Err(unavailable());
            }
            note("persistent_hide_show_and_detach_observed");
            let binding = EmbeddedAuthority::new(self.clone());
            let attached = binding.attach()?;
            if attached["webview_generation"] != first.generation {
                return Err(unavailable());
            }
            self.close(first.generation)?;
            if self.status().open {
                return Err(unavailable());
            }
            let second = self.open_inner(app, bounds, true)?;
            note("replacement_child_created");
            binding.release(first.generation)?;
            note("late_old_worker_release_acknowledged");
            self.close(first.generation)?;
            if self.status() != second {
                return Err(unavailable());
            }
            let mut preview_status = second;
            for _ in 0..9 {
                self.close(preview_status.generation)?;
                if self.status().open {
                    return Err(unavailable());
                }
                preview_status = self.open_inner(app, bounds, true)?;
                if !visible(&self.view(preview_status.generation, false)?)? {
                    return Err(unavailable());
                }
                note("close_reopen_cycle_observed");
            }
            let preview = self.view(preview_status.generation, false)?;
            eval_fixed(&preview,&format!("(()=>{{document.open();document.write({html});document.close();return true;}})()"))?;
            Ok(
                serde_json::json!({"schema_version":1,"purpose":"no-account-native-webkit-fixture","native_acceptance":"not_minted","result":"observed","child_visible":true,"isolated_native_data_store":true,"child_invoke_denied":true,"hidden_and_minimized_denied":true,"close_native_detachment_observed":true,"ui_close_then_worker_release":true,"stale_close_preserves_replacement":true,"close_reopen_cycles":10,"hide_show_preserves_browser":true,"explicit_background_preserves_capture":true,"background_fixed_snapshot_and_scroll":true,"background_main_view_hit_test":true,"hide_revokes_capture_without_show_revival":true,"collector_release_preserves_browser":true,"snapshot_state":snapshot["state"],"snapshot_cards":1,"preview_generation":preview_status.generation,"preview_open":true,"service_navigation":false}),
            )
        }
        pub fn resize_visible(
            &self,
            generation: u64,
            revision: u64,
            bounds: Bounds,
        ) -> io::Result<()> {
            let _operation = self.operations.lock().map_err(|_| unavailable())?;
            let (view, intent) = {
                let slot = self.slot.lock().map_err(|_| unavailable())?;
                if slot.generation != generation
                    || !self.gate.live(generation)
                    || !slot.visibility.current(revision, true)
                {
                    return Err(unavailable());
                }
                (
                    slot.view.clone().ok_or_else(unavailable)?,
                    slot.visibility.clone(),
                )
            };
            self.resize(generation, bounds)?;
            // SetBounds and this callback use the same off-thread FIFO. Drain
            // the old resize before Hide/Show can finish and apply new bounds.
            let (tx, rx) = std::sync::mpsc::sync_channel(1);
            view.with_webview(move |_| {
                let _ = tx.try_send(intent.current(revision, true));
            })
            .map_err(|_| unavailable())?;
            if rx.recv_timeout(Duration::from_secs(3)) == Ok(true) {
                Ok(())
            } else {
                Err(unavailable())
            }
        }
        pub fn resize(&self, generation: u64, bounds: Bounds) -> io::Result<()> {
            let view = self.view(generation, false)?;
            let window = view.window();
            let size = window.inner_size().map_err(|_| unavailable())?;
            let scale = window.scale_factor().map_err(|_| unavailable())?;
            view.set_bounds(rect(bounds.clamped(
                f64::from(size.width) / scale,
                f64::from(size.height) / scale,
            )?))
            .map_err(|_| unavailable())
        }
        pub fn hide(&self, generation: u64, revision: u64) -> io::Result<()> {
            self.visibility(generation, revision, None, false)
        }
        pub fn show(&self, generation: u64, revision: u64, bounds: Bounds) -> io::Result<()> {
            self.visibility(generation, revision, Some(bounds), false)
        }
        pub fn background(&self, generation: u64, revision: u64) -> io::Result<()> {
            self.visibility(generation, revision, None, true)
        }
        fn visibility(
            &self,
            generation: u64,
            revision: u64,
            bounds: Option<Bounds>,
            keep_capture: bool,
        ) -> io::Result<()> {
            let show = bounds.is_some();
            let (view, intent) = {
                let mut slot = self.slot.lock().map_err(|_| unavailable())?;
                refresh(&mut slot);
                if generation == 0
                    || slot.generation != generation
                    || slot.closing.is_some()
                    || !self.gate.live(generation)
                {
                    return Err(unavailable());
                }
                if keep_capture && (!slot.attached || !self.capture.live(slot.capture_epoch)) {
                    return Err(unavailable());
                }
                slot.visibility.request(revision, show)?;
                if keep_capture {
                    slot.background_capture = true;
                } else if !show {
                    slot.background_capture = false;
                }
                // Revocation precedes waiting for any native callback. Showing
                // never reactivates or reuses an earlier collection epoch.
                if !show && !keep_capture {
                    self.capture.revoke();
                    slot.attached = false;
                }
                (
                    slot.view.clone().ok_or_else(unavailable)?,
                    slot.visibility.clone(),
                )
            };
            let _operation = self.operations.lock().map_err(|_| unavailable())?;
            if !intent.current(revision, show) || !self.gate.live(generation) {
                return Err(unavailable());
            }
            if let Some(bounds) = bounds {
                self.resize(generation, bounds)?;
            }
            let gate = self.gate.clone();
            let (tx, rx) = std::sync::mpsc::sync_channel(1);
            view.with_webview(move |platform| {
                let permitted = gate.live(generation) && intent.current(revision, show);
                #[cfg(target_os = "macos")]
                let observed = permitted
                    && unsafe {
                        if keep_capture {
                            tr_embedded_set_background(platform.inner()) == 1
                        } else {
                            tr_embedded_set_visible(platform.inner(), i32::from(show)) == 1
                        }
                    };
                #[cfg(not(target_os = "macos"))]
                let observed = {
                    let _ = platform;
                    let _ = permitted;
                    false
                };
                let _ = tx.try_send(observed);
            })
            .map_err(|_| unavailable())?;
            if rx.recv_timeout(Duration::from_secs(3)) != Ok(true) {
                return Err(unavailable());
            }
            let mut slot = self.slot.lock().map_err(|_| unavailable())?;
            if slot.generation != generation || !slot.visibility.current(revision, show) {
                return Err(unavailable());
            }
            if show {
                slot.background_capture = false;
            }
            Ok(())
        }
        pub fn close(&self, generation: u64) -> io::Result<()> {
            {
                let mut slot = self.slot.lock().map_err(|_| unavailable())?;
                if slot.generation == generation {
                    self.gate.revoke();
                    self.capture.revoke();
                    slot.attached = false;
                }
            }
            self.close_current(Some(generation))
        }
        fn close_current(&self, expected: Option<u64>) -> io::Result<()> {
            let _operation = self.operations.lock().map_err(|_| unavailable())?;
            let (view, generation, observed) = {
                let mut slot = self.slot.lock().map_err(|_| unavailable())?;
                refresh(&mut slot);
                if expected.is_some_and(|g| g != slot.generation) {
                    return Ok(());
                }
                self.gate.revoke();
                self.capture.revoke();
                slot.attached = false;
                if slot.opening {
                    return Err(unavailable());
                }
                if slot.closing.is_some() {
                    return Err(unavailable());
                }
                let Some(view) = slot.view.clone() else {
                    return Ok(());
                };
                let observed = Arc::new(AtomicBool::new(false));
                slot.closing = Some(observed.clone());
                (view, slot.generation, observed)
            };
            let (submitted, result) = observed_close(&view, observed.clone());
            let mut slot = self.slot.lock().map_err(|_| unavailable())?;
            if slot.generation == generation {
                refresh(&mut slot);
                clear_unsubmitted_close(
                    slot.generation,
                    &mut slot.closing,
                    generation,
                    &observed,
                    submitted,
                );
            }
            result
        }
        fn view(&self, generation: u64, attached: bool) -> io::Result<Webview> {
            let mut slot = self.slot.lock().map_err(|_| unavailable())?;
            refresh(&mut slot);
            if generation == 0
                || slot.generation != generation
                || !self.gate.live(generation)
                || slot.closing.is_some()
                || (attached
                    && (!slot.attached
                        || (!slot.visibility.shown() && !slot.background_capture)
                        || !self.capture.live(slot.capture_epoch)))
            {
                return Err(unavailable());
            }
            slot.view.clone().ok_or_else(unavailable)
        }
        fn capture_visible(&self, generation: u64, view: &Webview) -> io::Result<bool> {
            let background = {
                let slot = self.slot.lock().map_err(|_| unavailable())?;
                slot.generation == generation
                    && slot.background_capture
                    && slot.attached
                    && self.capture.live(slot.capture_epoch)
            };
            if background {
                attached_to_visible_window(view)
            } else {
                visible(view)
            }
        }
        fn evaluate(&self, generation: u64, epoch: u64, script: &'static str) -> io::Result<Value> {
            let _operation = self.operations.lock().map_err(|_| unavailable())?;
            if !self.capture.live(epoch) {
                return Err(unavailable());
            }
            let view = self.view(generation, true)?;
            if !self.capture_visible(generation, &view)? {
                return Err(unavailable());
            }
            let before = view.url().map_err(|_| unavailable())?;
            let state = location_state(&before);
            if state != "ready" {
                return Ok(empty_snapshot(state));
            }
            let background = self
                .slot
                .lock()
                .map_err(|_| unavailable())?
                .background_capture;
            let raw=eval_fixed(&view,&format!("(()=>{{const hidden=()=>({{schema_version:1,state:'unknown_layout',cards:[],scroll_y:0,viewport_height:1}});if(!{background}&&document.visibilityState!=='visible')return hidden();const result=({script})();if(!{background}&&document.visibilityState!=='visible')return hidden();return result;}})()"))?;
            self.view(generation, true)?;
            if !self.capture_visible(generation, &view)? {
                return Err(unavailable());
            }
            let after = view.url().map_err(|_| unavailable())?;
            if before != after || location_state(&after) != "ready" {
                return Ok(empty_snapshot("unexpected_navigation"));
            }
            self.view(generation, true)?;
            if !self.capture.live(epoch) {
                return Err(unavailable());
            }
            let snapshot = checked_snapshot(&raw)?;
            #[cfg(all(debug_assertions, feature = "development-worker"))]
            {
                let cards = snapshot["cards"]
                    .as_array()
                    .map(Vec::as_slice)
                    .unwrap_or(&[]);
                let unknown = cards
                    .iter()
                    .filter(|card| card["issue"] == "unknown_layout")
                    .count();
                let recognized = cards
                    .iter()
                    .filter(|card| card.get("issue").is_none())
                    .count();
                if snapshot["state"] == "unknown_layout" || unknown > 0 {
                    let metadata = eval_fixed_bounded(&view, "({pageVisible:document.visibilityState==='visible', candidateCount:document.querySelectorAll('div[data-pressable-container=\"true\"][data-interactive-id=\"\"],main [role=feed]>article').length, viewportPositive:innerHeight>0})", Duration::from_millis(200))
                        .ok().and_then(|raw| serde_json::from_str::<Value>(&raw).ok()).unwrap_or(Value::Null);
                    let page_visible = metadata["pageVisible"].as_bool();
                    let candidates = metadata["candidateCount"]
                        .as_u64()
                        .filter(|count| *count <= 1000);
                    let viewport_positive = metadata["viewportPositive"].as_bool();
                    eprintln!("ThreadRelay snapshot metadata: cards={} recognized={recognized} unknown={unknown} visible={page_visible:?} candidates={candidates:?} viewport={viewport_positive:?}", cards.len());
                    let diagnostic = format!(
                        "({})()",
                        include_str!("../native/embedded-layout-diagnostics.js")
                    );
                    if let Some(layout) =
                        eval_fixed_bounded(&view, &diagnostic, Duration::from_millis(200))
                            .ok()
                            .and_then(|raw| serde_json::from_str::<Value>(&raw).ok())
                    {
                        for sample in layout["samples"]
                            .as_array()
                            .map(Vec::as_slice)
                            .unwrap_or(&[])
                            .iter()
                            .take(4)
                        {
                            let reason = match sample["reason"].as_str() {
                                Some(
                                    value @ ("recognized"
                                    | "diagnostic_error"
                                    | "page_attention"
                                    | "not_displayed"
                                    | "unrecognized_card"
                                    | "outside_viewport"
                                    | "semantic_columns"
                                    | "semantic_author_count"
                                    | "semantic_permalink_count"
                                    | "semantic_text_clipped"
                                    | "semantic_text_layout"
                                    | "semantic_partial_text"
                                    | "semantic_text_limit"
                                    | "public_nested_card"
                                    | "public_columns"
                                    | "public_columns_hidden"
                                    | "public_nested_boundary"
                                    | "public_author_count"
                                    | "public_author_label"
                                    | "public_time_count"
                                    | "public_permalink_identity"
                                    | "public_content_post_link"
                                    | "public_body_count"
                                    | "public_text_clipped"
                                    | "public_text_layout"
                                    | "public_text_limit"),
                                ) => value,
                                _ => "invalid_diagnostic",
                            };
                            let count =
                                |key: &str| sample[key].as_u64().filter(|count| *count <= 101);
                            eprintln!("ThreadRelay layout metadata: reason={reason} columns={:?} anchors={:?} times={:?} spans={:?} nested={:?}", count("column_count"), count("header_anchor_count"), count("header_time_count"), count("content_span_count"), count("nested_boundary_count"));
                            let author_counts = [
                                "own_header_link_count",
                                "platform_link_count",
                                "profile_path_count",
                                "profile_exact_count",
                                "profile_displayed_count",
                                "profile_visible_count",
                                "profile_layout_supported_count",
                                "filtered_header_link_count",
                                "filtered_author_count",
                            ]
                            .map(|key| {
                                sample["author_checks"][key]
                                    .as_u64()
                                    .filter(|count| *count <= 101)
                            });
                            let author_flags = [
                                "profile_transform",
                                "profile_transform_nontranslation",
                                "profile_mask",
                                "profile_clip",
                                "profile_filter",
                                "profile_content_visibility",
                                "profile_viewport_clipped",
                                "profile_self_overflow",
                                "profile_ancestor_clipped",
                            ]
                            .map(|key| sample["author_checks"][key].as_bool());
                            eprintln!("ThreadRelay author metadata: counts={author_counts:?} flags={author_flags:?}");
                            let time_counts = [
                                "time_link_count",
                                "time_link_visible_count",
                                "time_link_layout_count",
                                "time_link_filtered_count",
                                "time_node_displayed_count",
                                "time_node_visible_count",
                                "time_display_contents_count",
                            ]
                            .map(|key| {
                                sample["author_checks"][key]
                                    .as_u64()
                                    .filter(|count| *count <= 101)
                            });
                            let time_flags = [
                                "time_anchor_present",
                                "time_anchor_role",
                                "time_hidden",
                                "time_viewport_clipped",
                                "time_self_overflow",
                                "time_ancestor_clipped",
                                "time_nontranslation",
                                "time_mask_clip_filter",
                                "clip_x_subpixel",
                                "clip_y_subpixel",
                                "clip_inline",
                                "clip_zero_client",
                            ]
                            .map(|key| sample["author_checks"][key].as_bool());
                            eprintln!("ThreadRelay time metadata: counts={time_counts:?} flags={time_flags:?}");
                            let geometry = sample["author_checks"]["clip_geometry"]
                                .as_array()
                                .filter(|rows| rows.len() <= 3)
                                .and_then(|rows| {
                                    rows.iter()
                                        .map(|row| {
                                            row.as_array()
                                                .filter(|values| values.len() == 19)
                                                .and_then(|values| {
                                                    values
                                                        .iter()
                                                        .map(|value| {
                                                            value.as_f64().filter(|number| {
                                                                number.is_finite()
                                                                    && number.abs() <= 10000.0
                                                            })
                                                        })
                                                        .collect::<Option<Vec<_>>>()
                                                })
                                        })
                                        .collect::<Option<Vec<_>>>()
                                });
                            eprintln!("ThreadRelay clip geometry metadata: {geometry:?}");
                            let bodies = sample["author_checks"]["body_geometry"]
                                .as_array()
                                .filter(|rows| rows.len() <= 6)
                                .and_then(|rows| {
                                    rows.iter()
                                        .map(|row| {
                                            row.as_array()
                                                .filter(|values| values.len() == 13)
                                                .and_then(|values| {
                                                    values
                                                        .iter()
                                                        .map(|value| {
                                                            value.as_f64().filter(|number| {
                                                                number.is_finite()
                                                                    && (-1.0..=100001.0)
                                                                        .contains(number)
                                                            })
                                                        })
                                                        .collect::<Option<Vec<_>>>()
                                                })
                                        })
                                        .collect::<Option<Vec<_>>>()
                                });
                            eprintln!("ThreadRelay body shape metadata: count={:?} common_parent={:?} rows={bodies:?}", sample["author_checks"]["body_candidate_count"].as_u64().filter(|count| *count <= 101), sample["author_checks"]["body_common_parent"].as_bool());
                        }
                    }
                }
            }
            Ok(snapshot)
        }
    }
    impl EmbeddedCollector for EmbeddedThreads {
        fn revoke(&self) -> u64 {
            self.capture.revoke()
        }
        fn close_generation(&self, epoch: u64) -> io::Result<()> {
            let mut slot = self.slot.lock().map_err(|_| unavailable())?;
            if slot.capture_epoch == epoch {
                slot.attached = false;
            }
            Ok(())
        }
        fn capture_epoch(&self, _generation: u64) -> u64 {
            self.capture.epoch()
        }
        fn snapshot_lease(&self, generation: u64, epoch: u64) -> io::Result<Value> {
            self.evaluate(
                generation,
                epoch,
                include_str!("../native/embedded-snapshot.js"),
            )
        }
        fn scroll_lease(&self, generation: u64, epoch: u64) -> io::Result<()> {
            let value = self.evaluate(
                generation,
                epoch,
                include_str!("../native/embedded-scroll.js"),
            )?;
            if value["state"] == "ready" {
                Ok(())
            } else {
                Err(unavailable())
            }
        }
        fn release_lease(&self, generation: u64, epoch: u64) -> io::Result<()> {
            let mut slot = self.slot.lock().map_err(|_| unavailable())?;
            refresh(&mut slot);
            if release_owns_attachment(generation, slot.generation, epoch, slot.capture_epoch)? {
                self.capture.revoke();
                slot.attached = false;
            }
            Ok(())
        }
        fn available(&self) -> bool {
            if !self.supported {
                return false;
            }
            let generation = self.status().generation;
            self.view(generation, false)
                .is_ok_and(|view| visible(&view).unwrap_or(false))
        }
        fn attach(&self) -> io::Result<u64> {
            // A replacement lease cannot overtake an already submitted fixed
            // script from the revoked lease. No slot mutex spans UI work.
            let _operation = self.operations.lock().map_err(|_| unavailable())?;
            let (generation, view, fixture) = {
                let slot = self.slot.lock().map_err(|_| unavailable())?;
                if (slot.attached && self.capture.live(slot.capture_epoch))
                    || !slot.visibility.shown()
                    || !self.gate.live(slot.generation)
                    || slot.closing.is_some()
                {
                    return Err(unavailable());
                }
                (
                    slot.generation,
                    slot.view.clone().ok_or_else(unavailable)?,
                    slot.fixture,
                )
            };
            let url = view.url().map_err(|_| unavailable())?;
            #[cfg(all(debug_assertions, feature = "development-worker"))]
            let fixture_location = fixture && url.as_str() == "about:blank";
            #[cfg(not(all(debug_assertions, feature = "development-worker")))]
            let fixture_location = {
                let _ = fixture;
                false
            };
            if !visible(&view)? || (!navigation_allowed(&url) && !fixture_location) {
                return Err(unavailable());
            }
            let mut slot = self.slot.lock().map_err(|_| unavailable())?;
            if slot.generation != generation
                || (slot.attached && self.capture.live(slot.capture_epoch))
                || !slot.visibility.shown()
                || slot.view.is_none()
                || !self.gate.live(generation)
                || slot.closing.is_some()
            {
                return Err(unavailable());
            }
            slot.capture_epoch = self.capture.attach()?;
            slot.background_capture = false;
            slot.attached = true;
            Ok(generation)
        }
        fn snapshot(&self, generation: u64) -> io::Result<Value> {
            self.snapshot_lease(generation, self.capture.epoch())
        }
        fn scroll(&self, generation: u64) -> io::Result<()> {
            let value = self.evaluate(
                generation,
                self.capture.epoch(),
                include_str!("../native/embedded-scroll.js"),
            )?;
            if value["state"] == "ready" {
                Ok(())
            } else {
                Err(unavailable())
            }
        }
        fn poll(&self, generation: u64) -> bool {
            self.view(generation, true).is_ok_and(|view| {
                let fixture = self.slot.lock().ok().is_some_and(|slot| slot.fixture);
                self.capture_visible(generation, &view).unwrap_or(false)
                    && view.url().is_ok_and(|url| {
                        navigation_allowed(&url)
                            || (cfg!(all(debug_assertions, feature = "development-worker"))
                                && fixture
                                && url.as_str() == "about:blank")
                    })
            }) && self.view(generation, true).is_ok()
        }
        fn release(&self, generation: u64) -> io::Result<()> {
            self.release_lease(generation, self.capture.epoch())
        }
        fn close_all(&self) -> io::Result<()> {
            self.gate.revoke();
            self.capture.revoke();
            self.close_current(None)
        }
    }
    fn eval_fixed(view: &Webview, script: &str) -> io::Result<String> {
        eval_fixed_bounded(view, script, Duration::from_secs(3))
    }
    fn eval_fixed_bounded(view: &Webview, script: &str, budget: Duration) -> io::Result<String> {
        let (tx, rx) = std::sync::mpsc::sync_channel(1);
        view.eval_with_callback(script.to_owned(), move |raw| {
            let _ = tx.try_send(raw);
        })
        .map_err(|_| unavailable())?;
        rx.recv_timeout(budget).map_err(|_| unavailable())
    }
    fn attached_to_visible_window(view: &Webview) -> io::Result<bool> {
        let (tx, rx) = std::sync::mpsc::sync_channel(1);
        view.with_webview(move |platform| {
            #[cfg(target_os = "macos")]
            let result = unsafe { tr_embedded_attached(platform.inner()) == 1 };
            #[cfg(not(target_os = "macos"))]
            let result = {
                let _ = platform;
                false
            };
            let _ = tx.try_send(result);
        })
        .map_err(|_| unavailable())?;
        rx.recv_timeout(Duration::from_secs(2))
            .map_err(|_| unavailable())
    }
    fn visible(view: &Webview) -> io::Result<bool> {
        let window = view.window();
        if !window.is_visible().map_err(|_| unavailable())?
            || window.is_minimized().map_err(|_| unavailable())?
        {
            return Ok(false);
        }
        let (tx, rx) = std::sync::mpsc::sync_channel(1);
        view.with_webview(move |platform| {
            #[cfg(target_os = "macos")]
            let result = unsafe { tr_embedded_visible(platform.inner()) == 1 };
            #[cfg(not(target_os = "macos"))]
            let result = {
                let _ = platform;
                false
            };
            let _ = tx.try_send(result);
        })
        .map_err(|_| unavailable())?;
        rx.recv_timeout(Duration::from_secs(2))
            .map_err(|_| unavailable())
    }
    #[cfg(target_os = "macos")]
    struct RetainedView {
        raw: Option<usize>,
        window: tauri::Window,
    }
    #[cfg(target_os = "macos")]
    impl RetainedView {
        fn observe_detachment(mut self) -> bool {
            let Some(raw) = self.raw.take() else {
                return false;
            };
            unsafe { tr_embedded_detached_release(raw as *mut std::ffi::c_void) == 1 }
        }
    }
    #[cfg(target_os = "macos")]
    impl Drop for RetainedView {
        fn drop(&mut self) {
            if let Some(raw) = self.raw.take() {
                let _ = self.window.run_on_main_thread(move || unsafe {
                    tr_embedded_detached_release(raw as *mut std::ffi::c_void);
                });
            }
        }
    }
    fn observed_close(view: &Webview, observed: Arc<AtomicBool>) -> (bool, io::Result<()>) {
        #[cfg(target_os = "macos")]
        let retained = {
            let (tx, rx) = std::sync::mpsc::sync_channel(1);
            let window = view.window();
            if view
                .with_webview(move |platform| {
                    let raw = unsafe { tr_embedded_retain(platform.inner()) } as usize;
                    let token = RetainedView {
                        raw: if raw == 0 { None } else { Some(raw) },
                        window,
                    };
                    // RAII also covers the race where send succeeds just after
                    // timeout but before the receiver drops its queued result.
                    handoff_retained(&tx, token, drop);
                })
                .is_err()
            {
                return (false, Err(unavailable()));
            }
            match rx.recv_timeout(Duration::from_secs(2)) {
                Ok(token) if token.raw.is_some() => token,
                _ => return (false, Err(unavailable())),
            }
        };
        if view.close().is_err() {
            return (false, Err(unavailable()));
        }
        let (tx, rx) = std::sync::mpsc::sync_channel(1);
        // Close and Task share the pinned runtime's FIFO proxy off-thread;
        // on-thread Close has already completed synchronously before Task.
        if view
            .window()
            .run_on_main_thread(move || {
                #[cfg(target_os = "macos")]
                // Consume the whole owner. Capturing its Copy raw field alone
                // would leave Drop owning the same retain and release twice.
                let detached = retained.observe_detachment();
                #[cfg(not(target_os = "macos"))]
                let detached = true;
                if detached {
                    observed.store(true, Ordering::Release);
                }
                let _ = tx.try_send(detached);
            })
            .is_err()
        {
            return (true, Err(unavailable()));
        }
        let result = match rx.recv_timeout(Duration::from_secs(3)) {
            Ok(true) => Ok(()),
            _ => Err(unavailable()),
        };
        (true, result)
    }
    fn rect(b: Bounds) -> tauri::Rect {
        tauri::Rect {
            position: tauri::LogicalPosition::new(b.x, b.y).into(),
            size: tauri::LogicalSize::new(b.width, b.height).into(),
        }
    }
    #[cfg(target_os = "macos")]
    unsafe extern "C" {
        fn tr_embedded_visible(view: *mut std::ffi::c_void) -> libc::c_int;
        fn tr_embedded_attached(view: *mut std::ffi::c_void) -> libc::c_int;
        fn tr_embedded_set_background(view: *mut std::ffi::c_void) -> libc::c_int;
        fn tr_embedded_set_visible(view: *mut std::ffi::c_void, show: libc::c_int) -> libc::c_int;
        fn tr_embedded_retain(view: *mut std::ffi::c_void) -> *mut std::ffi::c_void;
        fn tr_embedded_detached_release(view: *mut std::ffi::c_void) -> libc::c_int;
        fn tr_embedded_distinct_store(
            main: *mut std::ffi::c_void,
            child: *mut std::ffi::c_void,
        ) -> libc::c_int;
    }
    #[cfg(target_os = "macos")]
    fn platform_supported() -> bool {
        let mut data = [0u8; 64];
        let mut size = data.len();
        if unsafe {
            libc::sysctlbyname(
                b"kern.osproductversion\0".as_ptr().cast(),
                data.as_mut_ptr().cast(),
                &mut size,
                std::ptr::null_mut(),
                0,
            )
        } != 0
        {
            return false;
        }
        std::str::from_utf8(&data[..size.min(data.len())])
            .ok()
            .and_then(|v| v.split('.').next())
            .and_then(|v| v.trim_end_matches('\0').parse::<u32>().ok())
            .is_some_and(|major| major >= 14)
    }
    #[cfg(windows)]
    fn platform_supported() -> bool {
        true
    }
    #[cfg(not(any(windows, target_os = "macos")))]
    fn platform_supported() -> bool {
        false
    }
}
#[cfg(feature = "desktop")]
pub use native::EmbeddedThreads;

#[cfg(test)]
mod tests {
    use super::*;
    struct PersistentFixture {
        visible: AtomicBool,
        attached: AtomicBool,
        epoch: AtomicU64,
        change_on_poll: AtomicBool,
    }
    impl PersistentFixture {
        fn new() -> Self {
            Self {
                visible: AtomicBool::new(true),
                attached: AtomicBool::new(false),
                epoch: AtomicU64::new(0),
                change_on_poll: AtomicBool::new(false),
            }
        }
        fn hide(&self) {
            self.visible.store(false, Ordering::Release);
            self.attached.store(false, Ordering::Release);
            self.epoch.fetch_add(1, Ordering::AcqRel);
        }
        fn show(&self) {
            self.visible.store(true, Ordering::Release);
        }
    }
    impl EmbeddedCollector for PersistentFixture {
        fn capture_epoch(&self, _: u64) -> u64 {
            self.epoch.load(Ordering::Acquire)
        }
        fn release_lease(&self, g: u64, epoch: u64) -> io::Result<()> {
            if epoch == self.capture_epoch(g) {
                self.release(g)
            } else {
                Ok(())
            }
        }
        fn available(&self) -> bool {
            self.visible.load(Ordering::Acquire)
        }
        fn attach(&self) -> io::Result<u64> {
            if !self.available() || self.attached.swap(true, Ordering::AcqRel) {
                return Err(unavailable());
            }
            self.epoch.fetch_add(1, Ordering::AcqRel);
            Ok(7)
        }
        fn snapshot(&self, _: u64) -> io::Result<Value> {
            Ok(
                serde_json::json!({"schema_version":1,"state":"ready","cards":[],"scroll_y":0,"viewport_height":1}),
            )
        }
        fn scroll(&self, _: u64) -> io::Result<()> {
            Ok(())
        }
        fn poll(&self, g: u64) -> bool {
            if self.change_on_poll.swap(false, Ordering::AcqRel) {
                self.epoch.fetch_add(1, Ordering::AcqRel);
            }
            g == 7 && self.available() && self.attached.load(Ordering::Acquire)
        }
        fn release(&self, _: u64) -> io::Result<()> {
            self.attached.store(false, Ordering::Release);
            Ok(())
        }
        fn close_all(&self) -> io::Result<()> {
            self.visible.store(false, Ordering::Release);
            Ok(())
        }
    }
    #[test]
    fn capture_epoch_is_rechecked_after_a_native_poll_roundtrip() {
        let host = Arc::new(PersistentFixture::new());
        let binding = EmbeddedAuthority::new(host.clone());
        binding.attach().unwrap();
        host.change_on_poll.store(true, Ordering::Release);
        assert!(
            !binding.poll(7),
            "a native visibility roundtrip must not hide a replaced capture epoch"
        );
    }
    #[test]
    fn hiding_and_showing_same_browser_never_revives_an_old_collector_lease() {
        let host = Arc::new(PersistentFixture::new());
        let old = EmbeddedAuthority::new(host.clone());
        old.attach().unwrap();
        host.hide();
        assert!(!old.poll(7));
        host.show();
        let replacement = EmbeddedAuthority::new(host.clone());
        replacement.attach().unwrap();
        assert!(
            !old.poll(7),
            "same browser generation must not revive an earlier capture lease"
        );
        assert!(old.snapshot(7).is_err());
        assert!(old.scroll(7).is_err());
        old.release(7).unwrap();
        assert!(
            replacement.poll(7),
            "old release must not detach the replacement lease"
        );
    }

    #[test]
    fn capture_revocation_cannot_revive_and_old_detach_does_not_match_new_epoch() {
        let capture = CaptureGate::new();
        let first = capture.attach().unwrap();
        assert!(capture.live(first));
        assert_eq!(capture.revoke(), first);
        assert!(!capture.live(first));
        let next = capture.attach().unwrap();
        assert_ne!(first, next);
        assert!(!capture.live(first));
        assert!(capture.live(next));
        assert_eq!(capture.epoch(), next);
    }
    #[test]
    fn newest_hide_intent_rejects_a_late_show_and_duplicate_revision() {
        let visibility = VisibilityIntent::new();
        visibility.request(1, true).unwrap();
        visibility.request(2, false).unwrap();
        assert!(!visibility.current(1, true));
        assert!(!visibility.shown());
        assert!(visibility.request(1, true).is_err());
        assert!(visibility.request(2, true).is_err());
        visibility.request(3, true).unwrap();
        assert!(visibility.current(3, true));
        assert!(visibility.request(0, false).is_err());
    }
    #[test]
    fn confirmed_closed_or_replaced_lease_does_not_own_a_new_attachment() {
        assert!(!release_owns_attachment(1, 2, 1, 2).unwrap());
        assert!(!release_owns_attachment(2, 2, 1, 2).unwrap());
        assert!(release_owns_attachment(2, 2, 2, 2).unwrap());
        assert!(release_owns_attachment(0, 2, 1, 2).is_err());
        assert!(release_owns_attachment(3, 2, 1, 2).is_err());
        assert!(release_owns_attachment(2, 2, 0, 2).is_err());
    }
    #[test]
    fn failed_close_preparation_can_retry_but_submitted_close_stays_pending() {
        let attempt = Arc::new(AtomicBool::new(false));
        let mut closing = Some(attempt.clone());
        clear_unsubmitted_close(7, &mut closing, 7, &attempt, false);
        assert!(closing.is_none());
        closing = Some(attempt.clone());
        clear_unsubmitted_close(7, &mut closing, 7, &attempt, true);
        assert!(closing.is_some());
        clear_unsubmitted_close(8, &mut closing, 7, &attempt, false);
        assert!(closing.is_some());
    }
    #[test]
    fn native_retain_handoff_releases_late_and_unreceived_results_once() {
        struct Retain(Arc<AtomicUsize>);
        impl Drop for Retain {
            fn drop(&mut self) {
                self.0.fetch_add(1, Ordering::AcqRel);
            }
        }
        for receiver_dropped_first in [true, false] {
            let dropped = Arc::new(AtomicUsize::new(0));
            let (tx, rx) = std::sync::mpsc::sync_channel(1);
            if receiver_dropped_first {
                drop(rx);
                handoff_retained(&tx, Retain(dropped.clone()), drop);
            } else {
                handoff_retained(&tx, Retain(dropped.clone()), drop);
                drop(rx);
            }
            assert_eq!(dropped.load(Ordering::Acquire), 1);
        }
    }
    #[test]
    fn revocation_ticket_captures_old_generation_without_revoking_replacement() {
        let gate = GenerationGate::new();
        gate.activate(7);
        let ticket = gate.revoke();
        assert_eq!(ticket, 7);
        assert!(!gate.live(7));
        gate.activate(8);
        assert_eq!(ticket, 7);
        assert!(gate.live(8));
        assert!(!gate.live(7));
    }
    #[test]
    fn invoking_child_or_remote_main_cannot_gain_local_command_authority() {
        for (label, url, debug, allowed) in [
            ("main", "tauri://localhost/index.html", false, true),
            ("main", "http://tauri.localhost/index.html", false, true),
            (
                "threads-embedded",
                "tauri://localhost/index.html",
                true,
                false,
            ),
            ("threads-embedded", "https://www.threads.com/", true, false),
            ("main", "https://www.threads.com/", true, false),
            ("main", "http://127.0.0.1:1420/", true, true),
            ("main", "http://127.0.0.1:1420/", false, false),
            ("main", "http://127.0.0.1:1421/", true, false),
            (
                "main",
                "https://tauri.localhost.evil.example/",
                false,
                false,
            ),
        ] {
            assert_eq!(
                trusted_invoker(label, &url.parse().unwrap(), debug),
                allowed,
                "{label} {url}"
            );
        }
    }
    use std::sync::{
        atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering},
        Arc,
    };
    #[test]
    fn rejects_duplicate_snapshot_fields_and_nonready_cards() {
        assert!(checked_snapshot(r#"{"schema_version":1,"state":"ready","state":"ready","cards":[],"scroll_y":0,"viewport_height":700}"#).is_err());
        assert!(checked_snapshot(r#"{"schema_version":1,"state":"manual_login_or_challenge","cards":[{}],"scroll_y":0,"viewport_height":700}"#).is_err());
    }
    #[test]
    fn bounds_clamp_to_client_area_and_reject_nonfinite_rectangles() {
        let input = Bounds {
            x: 100.0,
            y: 150.0,
            width: 1000.0,
            height: 2000.0,
        };
        let output = input.clamped(960.0, 700.0).unwrap();
        assert_eq!(
            (output.x, output.y, output.width, output.height),
            (100.0, 150.0, 860.0, 550.0)
        );
        for value in [
            Bounds { x: -1.0, ..input },
            Bounds {
                width: 0.0,
                ..input
            },
            Bounds {
                height: f64::NAN,
                ..input
            },
            Bounds { x: 960.0, ..input },
        ] {
            assert!(value.clamped(960.0, 700.0).is_err());
        }
    }
    #[test]
    fn snapshot_location_never_treats_login_profile_or_post_as_home_feed() {
        for (url, state) in [
            ("https://www.threads.com/", "ready"),
            ("https://www.threads.com/login", "manual_login_or_challenge"),
            (
                "https://www.instagram.com/accounts/login/",
                "manual_login_or_challenge",
            ),
            ("https://www.threads.com/@name", "unexpected_navigation"),
            (
                "https://www.threads.com/?query=private",
                "unexpected_navigation",
            ),
            ("https://evil.example/", "unexpected_navigation"),
        ] {
            assert_eq!(location_state(&url.parse().unwrap()), state);
        }
    }
    #[test]
    fn dedicated_store_identity_is_stable_and_separate_for_each_app_root() {
        assert_eq!(
            store_identifier(Path::new("/private/app-one")),
            store_identifier(Path::new("/private/app-one"))
        );
        assert_ne!(
            store_identifier(Path::new("/private/app-one")),
            store_identifier(Path::new("/private/app-two"))
        );
        assert_ne!(store_identifier(Path::new("/private/app-one")), [0; 16]);
    }
    struct FixtureHost {
        generation: AtomicU64,
        open: AtomicBool,
        reads: AtomicUsize,
        release_fails: AtomicBool,
    }
    impl EmbeddedCollector for FixtureHost {
        fn available(&self) -> bool {
            self.open.load(Ordering::Acquire)
        }
        fn attach(&self) -> io::Result<u64> {
            if self.available() {
                Ok(self.generation.load(Ordering::Acquire))
            } else {
                Err(unavailable())
            }
        }
        fn snapshot(&self, _: u64) -> io::Result<Value> {
            self.reads.fetch_add(1, Ordering::AcqRel);
            Ok(empty_snapshot("ready"))
        }
        fn scroll(&self, _: u64) -> io::Result<()> {
            Ok(())
        }
        fn poll(&self, generation: u64) -> bool {
            self.available() && self.generation.load(Ordering::Acquire) == generation
        }
        fn release(&self, generation: u64) -> io::Result<()> {
            if self.release_fails.load(Ordering::Acquire) || !self.poll(generation) {
                Err(unavailable())
            } else {
                self.open.store(false, Ordering::Release);
                Ok(())
            }
        }
        fn close_all(&self) -> io::Result<()> {
            self.open.store(false, Ordering::Release);
            Ok(())
        }
    }
    #[test]
    fn worker_binding_denies_foreign_and_replaced_generations_before_dom_read() {
        let host = Arc::new(FixtureHost {
            generation: AtomicU64::new(7),
            open: AtomicBool::new(true),
            reads: AtomicUsize::new(0),
            release_fails: AtomicBool::new(false),
        });
        let binding = EmbeddedAuthority::new(host.clone());
        assert!(binding.snapshot(7).is_err());
        let lease = binding.attach().unwrap();
        assert_eq!(lease["webview_generation"], 7);
        assert_eq!(lease["source_session_id"].as_str().unwrap().len(), 64);
        assert!(binding.attach().is_err());
        assert!(binding.snapshot(8).is_err());
        assert_eq!(host.reads.load(Ordering::Acquire), 0);
        assert!(binding.snapshot(7).is_ok());
        host.generation.store(8, Ordering::Release);
        assert!(binding.snapshot(7).is_err());
        assert!(binding.release(7).is_err());
        assert!(host.available());
        assert_eq!(host.reads.load(Ordering::Acquire), 1);
    }
    #[test]
    fn failed_native_close_retains_worker_binding_until_confirmed() {
        let host = Arc::new(FixtureHost {
            generation: AtomicU64::new(2),
            open: AtomicBool::new(true),
            reads: AtomicUsize::new(0),
            release_fails: AtomicBool::new(true),
        });
        let binding = EmbeddedAuthority::new(host.clone());
        binding.attach().unwrap();
        assert!(binding.release(2).is_err());
        assert!(binding.attach().is_err());
        host.release_fails.store(false, Ordering::Release);
        binding.release(2).unwrap();
        assert!(!binding.poll(2));
    }
    #[test]
    fn accepts_only_bounded_closed_snapshot_shape() {
        let valid =
            r#"{"schema_version":1,"state":"ready","cards":[],"scroll_y":0,"viewport_height":700}"#;
        assert!(checked_snapshot(valid).is_ok());
        assert!(checked_snapshot(r#"{"schema_version":1,"state":"ready","cards":[],"scroll_y":0,"viewport_height":700,"cookie":"secret"}"#).is_err());
    }
    #[test]
    fn threads_instagram_sso_chain_remains_manual_inside_the_embed() {
        for path in ["/threads/sso/", "/oauth/oidc/"] {
            let url: url::Url = format!("https://www.instagram.com{path}?state=fixture")
                .parse()
                .unwrap();
            assert!(navigation_allowed(&url), "{path}");
            assert_eq!(location_state(&url), "manual_login_or_challenge");
        }
        for value in [
            "https://www.instagram.com/threads/sso/other",
            "https://www.instagram.com/oauth/oidc/other",
            "https://instagram.com/threads/sso/",
            "https://www.instagram.com.evil.example/oauth/oidc/",
            "http://www.instagram.com/threads/sso/",
        ] {
            assert!(!navigation_allowed(&value.parse().unwrap()), "{value}");
        }
    }
    #[test]
    fn navigation_permits_only_fixed_https_service_origins() {
        for value in [
            "https://www.threads.com/",
            "https://www.threads.net/login",
            "https://www.instagram.com/accounts/login/",
        ] {
            assert!(navigation_allowed(&value.parse().unwrap()), "{value}");
        }
        for value in [
            "http://www.threads.com/",
            "https://www.threads.com.evil.example/",
            "https://evil.example/?next=https://www.threads.com",
            "https://user@www.threads.com/",
            "https://www.threads.com:444/",
            "file:///tmp/test",
            "javascript:alert(1)",
            "tauri://localhost/index.html",
            "https://www.instagram.com/explore/",
        ] {
            assert!(!navigation_allowed(&value.parse().unwrap()), "{value}");
        }
    }
}
