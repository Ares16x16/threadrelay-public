//! Trusted host-only process ownership. No type here is renderer-deserializable.
//! Proposed shutdown bound: 5 seconds, measured once for the registered group set.
//! No generic whole-process-tree containment is promised for arbitrary binaries.
use serde::Serialize;
use std::{
    collections::BTreeMap,
    ffi::{OsStr, OsString},
    fs::{File, OpenOptions},
    io::{self, Read, Write},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::atomic::{AtomicU64, Ordering as AtomicOrdering},
    time::{Duration, Instant},
};

pub const PROPOSED_SHUTDOWN_MS: u64 = 5_000;

/// The desktop owns this for its full lifetime, not just an active session.
/// A stopped session does not permit a second database/profile owner.
pub struct DesktopSupervisor {
    // Rust drops fields in declaration order: release process ownership before
    // releasing the profile/database lock, including on unwinding.
    pub processes: OwnedProcesses,
    artifacts: Vec<crate::verified_artifacts::VerifiedArtifactSet>,
    _lock: InstanceLock,
}
impl DesktopSupervisor {
    pub fn acquire(profile_dir: &Path) -> io::Result<Self> {
        std::fs::create_dir_all(profile_dir)?;
        Ok(Self {
            _lock: InstanceLock::acquire(&profile_dir.join("instance.lock"))?,
            processes: OwnedProcesses::default(),
            artifacts: Vec::new(),
        })
    }
    pub(crate) fn prepare_artifacts(
        &mut self,
        resources: &Path,
        app_root: &Path,
        cancelled: &dyn Fn() -> bool,
    ) -> io::Result<crate::verified_artifacts::GuardedResources> {
        let (paths, guard) =
            crate::verified_artifacts::prepare_installed(resources, app_root, cancelled)?;
        if let Some(guard) = guard {
            self.artifacts.push(guard);
        }
        Ok(paths)
    }
    #[cfg(target_os = "macos")]
    pub(crate) fn artifact_roots(&self) -> Vec<PathBuf> {
        self.artifacts.iter().map(|v| v.root().to_owned()).collect()
    }
    #[cfg(all(
        debug_assertions,
        target_os = "macos",
        target_arch = "aarch64",
        any(feature = "development-worker", test)
    ))]
    pub(crate) fn retains_stable_artifacts(
        &self,
        resources: &crate::verified_artifacts::GuardedResources,
        cancelled: &dyn Fn() -> bool,
    ) -> bool {
        self.artifacts.iter().any(|guard| {
            guard.root() == resources.root()
                && guard
                    .connected_artifacts_stable(resources.app_root(), cancelled)
                    .is_ok()
        })
    }
    pub(crate) fn retain_artifacts(
        &mut self,
        guard: crate::verified_artifacts::VerifiedArtifactSet,
    ) {
        self.artifacts.push(guard);
    }
    /// A manager is terminal after shutdown. Replace it only after every
    /// registered root is confirmed gone, while retaining the desktop lock.
    pub fn close_generation(&mut self, deadline_ms: u64) -> ShutdownReport {
        self.close_generation_with_grace(deadline_ms, 0)
    }
    pub fn close_generation_with_grace(
        &mut self,
        deadline_ms: u64,
        grace_ms: u64,
    ) -> ShutdownReport {
        let mut report = self.processes.shutdown_with_grace(deadline_ms, grace_ms);
        if report.stopped {
            let mut retained = Vec::new();
            for mut guard in self.artifacts.drain(..) {
                if let Err(_problem) = guard.cleanup() {
                    #[cfg(test)]
                    eprintln!(
                        "Generated artifact cleanup failed at {:?}: {_problem:?}",
                        guard.root()
                    );
                    retained.push(guard);
                }
            }
            self.artifacts = retained;
            if !self.artifacts.is_empty() {
                report.stopped = false;
                report.remaining.push("artifact_cleanup".into());
            } else {
                self.processes = OwnedProcesses::default();
            }
        }
        report
    }
}

/// Kept open for the lifetime of the desktop/database owner. Never unlink a
/// lockfile: another owner might then lock a different inode at the same path.
pub struct InstanceLock {
    _file: File,
}
impl InstanceLock {
    pub fn acquire(path: &Path) -> io::Result<Self> {
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(path)?;
        file.try_lock().map_err(io::Error::from)?;
        Ok(Self { _file: file })
    }
}
impl Drop for InstanceLock {
    fn drop(&mut self) {
        // Forked children can temporarily retain this same file description
        // before exec/guard cleanup. Closing only our descriptor would let an
        // inherited copy prolong the lock beyond the actual owner's lifetime.
        let _ = self._file.unlock();
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum ProcessKind {
    Worker,
    Browser,
    BrowserDriver,
    Inference,
}
impl ProcessKind {
    fn name(self) -> &'static str {
        match self {
            Self::Worker => "worker",
            Self::Browser => "browser",
            Self::BrowserDriver => "browser_driver",
            Self::Inference => "inference",
        }
    }
}

/// Opaque capability: cannot be reconstructed from a PID, integer or IPC data.
#[derive(Debug)]
pub struct Handle {
    id: u64,
    owner_id: u64,
    pipes: Option<OwnedPipes>,
}
pub struct BrowserPipes {
    pub reader: Box<dyn Read + Send + Sync>,
    pub writer: Box<dyn Write + Send + Sync>,
}
impl std::fmt::Debug for BrowserPipes {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("BrowserPipes([private])")
    }
}
pub struct OwnedPipes {
    pub browser_cdp: Option<BrowserPipes>,
    pub stdin: Box<dyn Write + Send + Sync>,
    pub stdout: Box<dyn Read + Send + Sync>,
    pub stderr: Box<dyn Read + Send + Sync>,
    #[cfg(unix)]
    pub bootstrap: Option<std::os::unix::net::UnixStream>,
    #[cfg(unix)]
    pub control: Option<std::os::unix::net::UnixStream>,
    #[cfg(unix)]
    pub driver: Option<std::os::unix::net::UnixStream>,
    #[cfg(windows)]
    pub bootstrap: Option<crate::windows_private::Duplex>,
    #[cfg(windows)]
    pub control: Option<crate::windows_private::Duplex>,
    #[cfg(windows)]
    pub driver: Option<crate::windows_private::Duplex>,
}
impl std::fmt::Debug for OwnedPipes {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("OwnedPipes([private])")
    }
}
impl Handle {
    pub fn take_pipes(&mut self) -> Option<OwnedPipes> {
        self.pipes.take()
    }
}

/// Launch policy is host-only, never renderer JSON. No raw PID registration.
#[derive(Clone, Copy, PartialEq, Eq)]
enum LaunchPolicy {
    UnverifiedLauncher,
    PackagedWorker,
    RegisteredDependencyRoot,
    BrowserPipe,
    #[cfg(test)]
    SyntheticGuardedGroup,
}

/// Concrete trusted adapters must launch every separate app-owned group through
/// this supervisor, not launch a wrapper which privately creates more groups.
/// `new` deliberately defers unverified launchers (including Playwright's own
/// nested process launcher). This is not arbitrary process-tree containment.
pub struct SpawnSpec {
    pub(crate) executable: PathBuf,
    pub(crate) args: Vec<OsString>,
    policy: LaunchPolicy,
    generation: u64,
    inference_token: Option<String>,
    #[cfg(unix)]
    private_runtime: Option<crate::runtime_environment::PrivateRuntime>,
}
impl SpawnSpec {
    pub fn new(
        executable: impl AsRef<Path>,
        args: impl IntoIterator<Item = impl AsRef<OsStr>>,
    ) -> Self {
        Self {
            executable: executable.as_ref().to_owned(),
            args: args.into_iter().map(|a| a.as_ref().to_owned()).collect(),
            policy: LaunchPolicy::UnverifiedLauncher,
            generation: 0,
            inference_token: None,
            #[cfg(unix)]
            private_runtime: None,
        }
    }
    /// Fixed-argv packaged worker. Its IPC starts Stopped; it must not use an
    /// independent browser/runtime launcher. Dependencies use spawn_dependency.
    pub fn packaged_worker(executable: impl AsRef<Path>) -> Self {
        Self {
            executable: executable.as_ref().to_owned(),
            args: vec![],
            policy: LaunchPolicy::PackagedWorker,
            generation: 1,
            inference_token: None,
            #[cfg(unix)]
            private_runtime: None,
        }
    }
    #[cfg(unix)]
    pub(crate) fn with_private_runtime(
        mut self,
        runtime: &crate::runtime_environment::PrivateRuntime,
    ) -> Self {
        self.private_runtime = Some(runtime.clone());
        self
    }
    pub fn packaged_worker_generation(executable: impl AsRef<Path>, generation: u64) -> Self {
        let mut spec = Self::packaged_worker(executable);
        spec.generation = generation;
        spec
    }
    /// Explicit debug feature only: the host supplies fixed checkout paths and
    /// retains the packaged worker's sanitized environment and private lanes.
    #[cfg(all(debug_assertions, any(feature = "development-worker", test)))]
    pub(crate) fn development_worker(
        executable: &Path,
        source: &Path,
        dependencies: &Path,
        generation: u64,
    ) -> Self {
        let mut spec = Self::packaged_worker_generation(executable, generation);
        spec.args = vec![
            "-I".into(),
            "-S".into(),
            "-B".into(),
            "-c".into(),
            "import runpy,sys;sys.path[:0]=sys.argv[1:];runpy.run_module('threadrelay.packaged_worker',run_name='__main__')".into(),
            source.as_os_str().into(),
            dependencies.as_os_str().into(),
        ];
        spec
    }
    /// A direct dependency group root, accepted only by spawn_dependency with a
    /// live same-owner capability. The host establishes THIS group's identity
    /// and death guard before exec/activation; this is not a binary verifier or
    /// generic process-tree containment. Same-group descendants are covered.
    /// A concrete trusted adapter MUST delegate every separate group to this
    /// API before activation. Raw Playwright nested launch does not satisfy that
    /// contract merely by choosing this constructor; it remains unsupported
    /// until Task 3 implements and tests a compliant direct-launch adapter.
    pub fn supervised_dependency_root(
        executable: impl AsRef<Path>,
        args: impl IntoIterator<Item = impl AsRef<OsStr>>,
    ) -> Self {
        let mut spec = Self::new(executable, args);
        spec.policy = LaunchPolicy::RegisteredDependencyRoot;
        spec
    }
    pub(crate) fn browser_pipe_root(
        executable: impl AsRef<Path>,
        args: impl IntoIterator<Item = impl AsRef<OsStr>>,
    ) -> Self {
        let mut spec = Self::new(executable, args);
        spec.policy = LaunchPolicy::BrowserPipe;
        spec
    }
    pub(crate) fn supervised_inference_root(
        executable: impl AsRef<Path>,
        args: impl IntoIterator<Item = impl AsRef<OsStr>>,
        mut token: String,
    ) -> io::Result<Self> {
        if !(32..=128).contains(&token.len())
            || !token
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
        {
            crate::secure_auth::wipe_string(&mut token);
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "Invalid private runtime token",
            ));
        }
        let mut spec = Self::supervised_dependency_root(executable, args);
        spec.inference_token = Some(token);
        Ok(spec)
    }
    #[cfg(test)]
    pub(crate) fn guarded_fixture(
        executable: impl AsRef<Path>,
        args: impl IntoIterator<Item = impl AsRef<OsStr>>,
    ) -> Self {
        let mut spec = Self::new(executable, args);
        spec.policy = LaunchPolicy::SyntheticGuardedGroup;
        spec
    }
    fn root_allowed(&self, kind: ProcessKind) -> bool {
        match self.policy {
            LaunchPolicy::PackagedWorker => kind == ProcessKind::Worker,
            #[cfg(test)]
            LaunchPolicy::SyntheticGuardedGroup => true,
            _ => false,
        }
    }
    fn dependency_allowed(&self, kind: ProcessKind) -> bool {
        if self.inference_token.is_some() && kind != ProcessKind::Inference {
            return false;
        }
        match self.policy {
            LaunchPolicy::BrowserPipe => kind == ProcessKind::Browser,
            LaunchPolicy::RegisteredDependencyRoot => {
                matches!(
                    kind,
                    ProcessKind::Browser | ProcessKind::BrowserDriver | ProcessKind::Inference
                )
            }
            #[cfg(test)]
            LaunchPolicy::SyntheticGuardedGroup => true,
            _ => false,
        }
    }
}

impl Drop for SpawnSpec {
    fn drop(&mut self) {
        if let Some(token) = &mut self.inference_token {
            crate::secure_auth::wipe_string(token);
        }
    }
}

pub trait Clock {
    fn elapsed(&self) -> Duration;
    fn wait(&self, duration: Duration);
}
pub struct MonotonicClock(Instant);
impl Default for MonotonicClock {
    fn default() -> Self {
        Self(Instant::now())
    }
}
impl Clock for MonotonicClock {
    fn elapsed(&self) -> Duration {
        self.0.elapsed()
    }
    fn wait(&self, duration: Duration) {
        std::thread::sleep(duration);
    }
}

#[cfg(windows)]
#[derive(Clone, Copy)]
pub struct NativeIdentity {
    pub(crate) pid: u32,
    pub(crate) process: windows_sys::Win32::Foundation::HANDLE,
    pub(crate) job: windows_sys::Win32::Foundation::HANDLE,
}
pub trait OwnedChild: Send {
    #[cfg(target_os = "macos")]
    fn native_group_leader(&self) -> Option<i32> {
        None
    }
    #[cfg(windows)]
    fn native_identity(&self) -> Option<NativeIdentity> {
        None
    }
    /// Setup may fail after OS process creation; return the still-owned child.
    fn launch_error(&self) -> Option<io::Error> {
        None
    }
    fn exited(&mut self) -> io::Result<bool>;
    fn terminate(&mut self) -> io::Result<()>;
    fn kill(&mut self) -> io::Result<()>;
    fn take_pipes(&mut self) -> Option<OwnedPipes> {
        None
    }
}
pub trait Spawner {
    fn spawn(&mut self, spec: &SpawnSpec) -> io::Result<Box<dyn OwnedChild>>;
}
pub struct SystemSpawner;

/// Confirmation covers all registered group roots, including dependencies even
/// after their original logical parent exits. It never asserts containment of
/// unknown/unregistered groups; launchers that may create those are unsupported.
#[derive(Debug, PartialEq, Eq, Serialize)]
pub struct ShutdownReport {
    pub stopped: bool,
    pub remaining: Vec<String>,
}

struct RegisteredChild {
    id: u64,
    process: Box<dyn OwnedChild>,
}
static NEXT_OWNER_ID: AtomicU64 = AtomicU64::new(1);

pub struct OwnedProcesses<C: Clock = MonotonicClock, S: Spawner = SystemSpawner> {
    clock: C,
    spawner: S,
    children: BTreeMap<ProcessKind, RegisteredChild>,
    owner_id: u64,
    next_id: u64,
    closed: bool,
}
impl Default for OwnedProcesses {
    fn default() -> Self {
        Self::with_dependencies(MonotonicClock::default(), SystemSpawner)
    }
}
impl<C: Clock, S: Spawner> OwnedProcesses<C, S> {
    pub fn with_dependencies(clock: C, spawner: S) -> Self {
        Self {
            clock,
            spawner,
            children: BTreeMap::new(),
            owner_id: NEXT_OWNER_ID
                .try_update(AtomicOrdering::Relaxed, AtomicOrdering::Relaxed, |id| {
                    id.checked_add(1)
                })
                .expect("Ownership identity exhausted"),
            next_id: 0,
            closed: false,
        }
    }
    /// Native acceptance readback, limited to this manager's retained groups.
    /// Never accept a caller PID or register an independently discovered child.
    #[cfg(all(test, target_os = "macos"))]
    pub(crate) fn native_test_groups(&mut self) -> io::Result<Vec<(ProcessKind, i32)>> {
        let mut result = Vec::new();
        for (kind, child) in &mut self.children {
            if !child.process.exited()? {
                let leader = child
                    .process
                    .native_group_leader()
                    .ok_or_else(|| io::Error::other("Owned group identity unavailable"))?;
                result.push((*kind, leader));
            }
        }
        Ok(result)
    }
    #[cfg(all(test, target_os = "macos"))]
    pub(crate) fn native_test_browser_pipe(&mut self) -> io::Result<bool> {
        let child = self
            .children
            .get_mut(&ProcessKind::Browser)
            .ok_or_else(|| io::Error::other("Owned browser unavailable"))?;
        if child.process.exited()? {
            return Ok(false);
        }
        let leader = child
            .process
            .native_group_leader()
            .ok_or_else(|| io::Error::other("Owned browser identity unavailable"))?;
        Ok(crate::macos_observer::browser_pipe(leader))
    }

    /// Failure injection can target only an already registered owned kind.
    #[cfg(all(test, target_os = "macos"))]
    pub(crate) fn native_test_kill(&mut self, kind: ProcessKind) -> io::Result<()> {
        let child = self
            .children
            .get_mut(&kind)
            .ok_or_else(|| io::Error::other("Owned child unavailable"))?;
        if child.process.exited()? {
            return Err(io::Error::other("Owned child already exited"));
        }
        child.process.kill()
    }

    pub fn spawn(&mut self, kind: ProcessKind, spec: &SpawnSpec) -> io::Result<Handle> {
        if !spec.root_allowed(kind) {
            return Err(io::Error::new(
                io::ErrorKind::Unsupported,
                "Unverified launcher or unregistered dependency",
            ));
        }
        self.spawn_owned(kind, spec)
    }
    /// Register and own a separate logical dependency group before exposing its
    /// activation/IPC handle. Registration requires a live same-manager parent,
    /// never a renderer PID. Every group keeps its own OS identity/death guard;
    /// original-parent exit cannot erase an unconfirmed dependency from reports.
    pub fn spawn_dependency(
        &mut self,
        parent: &Handle,
        kind: ProcessKind,
        spec: &SpawnSpec,
    ) -> io::Result<Handle> {
        if parent.owner_id != self.owner_id {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "Foreign process capability",
            ));
        }
        let parent_kind = self
            .children
            .iter()
            .find_map(|(kind, child)| (child.id == parent.id).then_some(*kind))
            .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "Expired process capability"))?;
        if self
            .children
            .get_mut(&parent_kind)
            .expect("Registered parent")
            .process
            .exited()?
        {
            self.children.remove(&parent_kind);
            return Err(io::Error::new(
                io::ErrorKind::NotFound,
                "Parent process exited",
            ));
        }
        if !spec.dependency_allowed(kind) {
            return Err(io::Error::new(
                io::ErrorKind::Unsupported,
                "Unverified nested launcher",
            ));
        }
        self.spawn_owned(kind, spec)
    }
    fn spawn_owned(&mut self, kind: ProcessKind, spec: &SpawnSpec) -> io::Result<Handle> {
        if self.closed || self.children.contains_key(&kind) {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "Owned process is unavailable",
            ));
        }
        let id = self
            .next_id
            .checked_add(1)
            .ok_or_else(|| io::Error::other("Process identity exhausted"))?;
        let mut child = self.spawner.spawn(spec)?;
        let launch_error = child.launch_error();
        let pipes = if launch_error.is_none() {
            child.take_pipes()
        } else {
            None
        };
        self.children
            .insert(kind, RegisteredChild { id, process: child });
        self.next_id = id;
        if let Some(error) = launch_error {
            return Err(error);
        }
        Ok(Handle {
            id,
            owner_id: self.owner_id,
            pipes,
        })
    }
    /// Observe only a registered capability from this manager; no PID input.
    pub fn is_live(&mut self, handle: &Handle) -> io::Result<bool> {
        if handle.owner_id != self.owner_id {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "Foreign process capability",
            ));
        }
        let child = self
            .children
            .values_mut()
            .find(|child| child.id == handle.id)
            .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "Expired process capability"))?;
        Ok(!child.process.exited()?)
    }
    #[cfg(windows)]
    pub(crate) fn verify_inference_peer(
        &mut self,
        worker: &Handle,
        runtime: &Handle,
        client: u16,
        server: u16,
    ) -> io::Result<bool> {
        if !self.is_live(worker)? || !self.is_live(runtime)? {
            return Ok(false);
        }
        let identity = |handle: &Handle| {
            self.children
                .values()
                .find(|child| child.id == handle.id)
                .and_then(|child| child.process.native_identity())
        };
        let (worker, runtime) = match (identity(worker), identity(runtime)) {
            (Some(w), Some(r)) => (w, r),
            _ => return Ok(false),
        };
        crate::windows_observer::verify_connected_peer(worker, runtime, client, server)
    }
    #[cfg(windows)]
    pub(crate) fn evaluation_memory(
        &mut self,
        worker: &Handle,
        runtime: Option<&Handle>,
    ) -> io::Result<(u64, u64)> {
        if !self.is_live(worker)? {
            return Err(io::Error::other("Worker unavailable"));
        }
        if let Some(runtime) = runtime {
            if !self.is_live(runtime)? {
                return Err(io::Error::other("Runtime unavailable"));
            }
        } else if self.children.contains_key(&ProcessKind::Inference) {
            return Err(io::Error::other("Runtime ownership unresolved"));
        }
        let identity = |handle: &Handle| {
            self.children
                .values()
                .find(|child| child.id == handle.id)
                .and_then(|child| child.process.native_identity())
        };
        let worker =
            identity(worker).ok_or_else(|| io::Error::other("Worker scope unavailable"))?;
        let runtime = match runtime {
            Some(handle) => Some(
                identity(handle).ok_or_else(|| io::Error::other("Runtime scope unavailable"))?,
            ),
            None => None,
        };
        crate::windows_observer::evaluation_memory(worker, runtime)
    }
    #[cfg(target_os = "macos")]
    pub(crate) fn verify_inference_peer(
        &mut self,
        worker: &Handle,
        runtime: &Handle,
        client: u16,
        server: u16,
    ) -> io::Result<bool> {
        if !self.is_live(worker)? || !self.is_live(runtime)? {
            return Ok(false);
        }
        let leader = |h: &Handle| {
            self.children
                .values()
                .find(|child| child.id == h.id)
                .and_then(|child| child.process.native_group_leader())
        };
        let (worker, runtime) = match (leader(worker), leader(runtime)) {
            (Some(w), Some(r)) => (w, r),
            _ => return Ok(false),
        };
        Ok(crate::macos_observer::peer(worker, runtime, client, server))
    }
    #[cfg(target_os = "macos")]
    pub(crate) fn evaluation_memory(
        &mut self,
        worker: &Handle,
        runtime: Option<&Handle>,
    ) -> io::Result<(u64, u64)> {
        if !self.is_live(worker)? {
            return Err(io::Error::other("Worker unavailable"));
        }
        if let Some(handle) = runtime {
            if !self.is_live(handle)? {
                return Err(io::Error::other("Runtime unavailable"));
            }
        } else if self.children.contains_key(&ProcessKind::Inference) {
            return Err(io::Error::other("Runtime ownership unresolved"));
        }
        let leader = |h: &Handle| {
            self.children
                .values()
                .find(|child| child.id == h.id)
                .and_then(|child| child.process.native_group_leader())
        };
        let worker = leader(worker).ok_or_else(|| io::Error::other("Worker scope unavailable"))?;
        let runtime = match runtime {
            Some(h) => {
                Some(leader(h).ok_or_else(|| io::Error::other("Runtime scope unavailable"))?)
            }
            None => None,
        };
        crate::macos_observer::rss(worker, runtime)
    }
    #[cfg(windows)]
    pub(crate) fn inference_listener(&mut self, handle: &Handle, port: u16) -> io::Result<bool> {
        if !self.is_live(handle)? {
            return Ok(false);
        }
        let identity = self
            .children
            .values()
            .find(|child| child.id == handle.id)
            .and_then(|child| child.process.native_identity())
            .ok_or_else(|| io::Error::other("Owned runtime unavailable"))?;
        crate::windows_observer::listener(identity, port)
    }
    #[cfg(target_os = "macos")]
    pub(crate) fn inference_listener(&mut self, handle: &Handle, port: u16) -> io::Result<bool> {
        if !self.is_live(handle)? {
            return Ok(false);
        }
        let leader = self
            .children
            .values()
            .find(|child| child.id == handle.id)
            .and_then(|child| child.process.native_group_leader())
            .ok_or_else(|| io::Error::other("Owned runtime unavailable"))?;
        Ok(crate::macos_observer::listener(leader, port))
    }
    /// Stop exactly one still-owned dependency, never its parent or siblings.
    /// Failure retains the registered capability for a later retry; unlike
    /// generation shutdown this operation does not close the manager.
    pub fn close_dependency(
        &mut self,
        handle: &Handle,
        deadline_ms: u64,
    ) -> io::Result<ShutdownReport> {
        if handle.owner_id != self.owner_id {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "Foreign process capability",
            ));
        }
        let kind = self
            .children
            .iter()
            .find_map(|(kind, child)| {
                (child.id == handle.id && *kind != ProcessKind::Worker).then_some(*kind)
            })
            .ok_or_else(|| {
                io::Error::new(io::ErrorKind::NotFound, "Expired dependency capability")
            })?;
        let deadline = self
            .clock
            .elapsed()
            .saturating_add(Duration::from_millis(deadline_ms.min(PROPOSED_SHUTDOWN_MS)));
        let child = &mut self
            .children
            .get_mut(&kind)
            .expect("Registered dependency")
            .process;
        let _ = child.terminate();
        let mut stopped = matches!(child.exited(), Ok(true));
        while !stopped && self.clock.elapsed() < deadline {
            self.clock
                .wait(Duration::from_millis(10).min(deadline.saturating_sub(self.clock.elapsed())));
            stopped = matches!(child.exited(), Ok(true));
        }
        if !stopped {
            let _ = child.kill();
            stopped = matches!(child.exited(), Ok(true));
        }
        if stopped {
            self.children.remove(&kind);
        }
        Ok(ShutdownReport {
            stopped,
            remaining: if stopped {
                vec![]
            } else {
                vec![kind.name().to_owned()]
            },
        })
    }

    /// Loss is reported to session authority; this method never relaunches.
    pub fn poll_exits(&mut self) -> io::Result<Vec<ProcessKind>> {
        let mut exited = Vec::new();
        for (kind, child) in &mut self.children {
            if child.process.exited()? {
                exited.push(*kind);
            }
        }
        for kind in &exited {
            self.children.remove(kind);
        }
        Ok(exited)
    }
    fn reap(&mut self) {
        self.children
            .retain(|_, child| !matches!(child.process.exited(), Ok(true)));
    }
    /// Allow an explicitly stopped worker to finish its own bounded cleanup
    /// before signalling. The same overall shutdown deadline still applies.
    pub fn shutdown_with_grace(&mut self, deadline_ms: u64, grace_ms: u64) -> ShutdownReport {
        self.closed = true;
        let budget = Duration::from_millis(deadline_ms.min(PROPOSED_SHUTDOWN_MS));
        let start = self.clock.elapsed();
        let grace = Duration::from_millis(grace_ms).min(budget);
        self.reap();
        while !self.children.is_empty() && self.clock.elapsed().saturating_sub(start) < grace {
            let remaining = grace.saturating_sub(self.clock.elapsed().saturating_sub(start));
            self.clock.wait(Duration::from_millis(10).min(remaining));
            self.reap();
        }
        let remaining = budget.saturating_sub(self.clock.elapsed().saturating_sub(start));
        self.shutdown(remaining.as_millis() as u64)
    }
    pub fn shutdown(&mut self, deadline_ms: u64) -> ShutdownReport {
        self.closed = true; // terminal; cleanup cannot be followed by hidden restart.
        let deadline = self
            .clock
            .elapsed()
            .saturating_add(Duration::from_millis(deadline_ms.min(PROPOSED_SHUTDOWN_MS)));
        for child in self.children.values_mut() {
            let _ = child.process.terminate();
        }
        self.reap();
        while !self.children.is_empty() && self.clock.elapsed() < deadline {
            self.clock
                .wait(Duration::from_millis(10).min(deadline.saturating_sub(self.clock.elapsed())));
            self.reap();
        }
        for child in self.children.values_mut() {
            let _ = child.process.kill();
        }
        // Never block waiting after the common deadline. Unconfirmed exits stay
        // in remaining and retain ownership for a later cleanup/reap.
        self.reap();
        ShutdownReport {
            stopped: self.children.is_empty(),
            remaining: self.children.keys().map(|k| k.name().to_owned()).collect(),
        }
    }
}
impl<C: Clock, S: Spawner> Drop for OwnedProcesses<C, S> {
    fn drop(&mut self) {
        let _ = self.shutdown(0);
    }
}

#[cfg(target_os = "macos")]
const MAX_OBSERVED_GROUP_MEMBERS: usize = 1024;

/// This checks only the pinned, registered group. It does not discover groups
/// created outside the direct-launch contract. A witnessed identity change is
/// terminal: a later snapshot cannot erase an escaped or substituted member.
#[cfg(target_os = "macos")]
struct MacosGroupObservation {
    uid: libc::uid_t,
    euid: libc::uid_t,
    parent: u32,
    identities: BTreeMap<i32, (u64, u64)>,
    identity_changed: bool,
}
#[cfg(target_os = "macos")]
impl MacosGroupObservation {
    fn new() -> Self {
        Self {
            uid: unsafe { libc::getuid() },
            euid: unsafe { libc::geteuid() },
            parent: std::process::id(),
            identities: BTreeMap::new(),
            identity_changed: false,
        }
    }
    fn stopped(&mut self, group: i32) -> io::Result<bool> {
        if self.identity_changed {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "Owned group identity changed",
            ));
        }
        let result = self.inspect(group);
        if result
            .as_ref()
            .is_err_and(|error| error.kind() == io::ErrorKind::InvalidData)
        {
            self.identity_changed = true;
        }
        result
    }
    fn inspect(&mut self, group: i32) -> io::Result<bool> {
        let mut buffer = [0; MAX_OBSERVED_GROUP_MEMBERS];
        let current = macos_group_members(group, &mut buffer)?;
        let targets: std::collections::BTreeSet<_> = current
            .iter()
            .copied()
            .chain(self.identities.keys().copied())
            .collect();
        if targets.len() > MAX_OBSERVED_GROUP_MEMBERS {
            return Err(io::Error::other("Owned group observation limit reached"));
        }
        let mut zombies = std::collections::BTreeSet::new();
        for pid in targets {
            let mut info = std::mem::MaybeUninit::<libc::proc_bsdinfo>::zeroed();
            let size = std::mem::size_of::<libc::proc_bsdinfo>();
            // arg=1 includes zombies. arg=0 would hide the pinned leader and
            // cannot establish that an EPERM group contains only exited tasks.
            let count = unsafe {
                libc::proc_pidinfo(
                    pid,
                    libc::PROC_PIDTBSDINFO,
                    1,
                    info.as_mut_ptr().cast(),
                    size as i32,
                )
            };
            if count != size as i32 {
                let error = io::Error::last_os_error();
                if count == 0
                    && error.raw_os_error() == Some(libc::ESRCH)
                    && !current.contains(&pid)
                {
                    // A previously observed member is now absent even from a
                    // zombie-aware lookup. Never signal its former PID.
                    self.identities.remove(&pid);
                    continue;
                }
                return Err(if count == 0 {
                    error
                } else {
                    io::Error::other("Incomplete owned process observation")
                });
            }
            let info = unsafe { info.assume_init() };
            let identity = (info.pbi_start_tvsec, info.pbi_start_tvusec);
            if info.pbi_pid != pid as u32
                || info.pbi_pgid != group as u32
                || info.pbi_ruid != self.uid
                || info.pbi_uid != self.euid
                || (pid == group && info.pbi_ppid != self.parent)
                || self
                    .identities
                    .get(&pid)
                    .is_some_and(|previous| *previous != identity)
            {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "Owned group identity changed",
                ));
            }
            self.identities.insert(pid, identity);
            if info.pbi_status == libc::SZOMB {
                zombies.insert(pid);
            }
        }
        // A member alive in the first snapshot could fork before it exits.
        // Check again after reading statuses, so a new live child is not hidden
        // by its parent's later zombie status. No member of a zombie-only set
        // can itself fork after this check.
        let final_members = macos_group_members(group, &mut buffer)?;
        Ok(final_members.is_subset(&zombies)
            && self.identities.keys().all(|pid| zombies.contains(pid)))
    }
}
#[cfg(target_os = "macos")]
fn macos_group_members(
    group: i32,
    buffer: &mut [i32],
) -> io::Result<std::collections::BTreeSet<i32>> {
    if group <= 0 || buffer.is_empty() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "Invalid owned group observation",
        ));
    }
    // This libproc wrapper returns PID COUNT, although proc_listpids returns
    // bytes. A full buffer may be silently truncated, so equality is unproven.
    let count = unsafe {
        libc::proc_listpgrppids(
            group,
            buffer.as_mut_ptr().cast(),
            std::mem::size_of_val(buffer) as i32,
        )
    };
    if count <= 0 || count as usize >= buffer.len() {
        return Err(io::Error::other("Incomplete owned group observation"));
    }
    let members: std::collections::BTreeSet<_> = buffer[..count as usize].iter().copied().collect();
    if members.len() != count as usize
        || members.iter().any(|pid| *pid <= 0)
        || !members.contains(&group)
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "Owned group identity changed",
        ));
    }
    Ok(members)
}

struct SystemChild {
    child: Child,
    #[cfg(unix)]
    _private_runtime: Option<crate::runtime_environment::PrivateRuntime>,
    browser_cdp: Option<BrowserPipes>,
    #[cfg(unix)]
    group_owned: bool,
    #[cfg(target_os = "macos")]
    group_observation: MacosGroupObservation,
    #[cfg(unix)]
    _parent_watchdog: std::os::unix::net::UnixStream,
    #[cfg(unix)]
    bootstrap: Option<std::os::unix::net::UnixStream>,
    #[cfg(unix)]
    control: Option<std::os::unix::net::UnixStream>,
    #[cfg(unix)]
    driver: Option<std::os::unix::net::UnixStream>,
    #[cfg(windows)]
    bootstrap: Option<crate::windows_private::Duplex>,
    #[cfg(windows)]
    control: Option<crate::windows_private::Duplex>,
    #[cfg(windows)]
    driver: Option<crate::windows_private::Duplex>,
    #[cfg(windows)]
    _job: Option<windows_job::Job>,
    #[cfg(windows)]
    launch_failed: bool,
}
impl Spawner for SystemSpawner {
    fn spawn(&mut self, spec: &SpawnSpec) -> io::Result<Box<dyn OwnedChild>> {
        if spec.policy == LaunchPolicy::UnverifiedLauncher {
            return Err(io::Error::new(
                io::ErrorKind::Unsupported,
                "Unverified process launcher",
            ));
        }
        #[cfg(windows)]
        if spec.policy == LaunchPolicy::BrowserPipe {
            return crate::windows_chromium::spawn(spec);
        }
        let mut command = Command::new(&spec.executable);
        // No inherited Python, Playwright, loader, proxy, debug or model
        // overrides. Specific native dependencies may add only reviewed fixed
        // entries through a future host-owned capability, never renderer data.
        #[cfg(test)]
        let sanitize = spec.policy != LaunchPolicy::SyntheticGuardedGroup;
        #[cfg(not(test))]
        let sanitize = true;
        if sanitize {
            command.env_clear();
            #[cfg(windows)]
            if let Some(root) = std::env::var_os("SystemRoot") {
                command.env("SystemRoot", root);
            }
        }
        #[cfg(unix)]
        if let Some(runtime) = &spec.private_runtime {
            runtime.configure(&mut command)?;
        }
        if let Some(token) = &spec.inference_token {
            command.env("LLAMA_API_KEY", token);
        }
        command
            .args(&spec.args)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        #[cfg(unix)]
        let (bootstrap_reader, bootstrap_owner) = if spec.policy == LaunchPolicy::PackagedWorker {
            use std::os::fd::{AsRawFd, FromRawFd};
            let (reader, owner) = std::os::unix::net::UnixStream::pair()?;
            let fd = unsafe { libc::fcntl(reader.as_raw_fd(), libc::F_DUPFD_CLOEXEC, 10) };
            if fd < 0 {
                return Err(io::Error::last_os_error());
            }
            (
                Some(unsafe { std::os::unix::net::UnixStream::from_raw_fd(fd) }),
                Some(owner),
            )
        } else {
            (None, None)
        };
        #[cfg(unix)]
        let (control_reader, control_owner) = if spec.policy == LaunchPolicy::PackagedWorker {
            use std::os::fd::{AsRawFd, FromRawFd};
            let (reader, owner) = std::os::unix::net::UnixStream::pair()?;
            let fd = unsafe { libc::fcntl(reader.as_raw_fd(), libc::F_DUPFD_CLOEXEC, 10) };
            if fd < 0 {
                return Err(io::Error::last_os_error());
            }
            (
                Some(unsafe { std::os::unix::net::UnixStream::from_raw_fd(fd) }),
                Some(owner),
            )
        } else {
            (None, None)
        };
        #[cfg(unix)]
        let (driver_reader, driver_owner) = if spec.policy == LaunchPolicy::PackagedWorker {
            use std::os::fd::{AsRawFd, FromRawFd};
            let (reader, owner) = std::os::unix::net::UnixStream::pair()?;
            let fd = unsafe { libc::fcntl(reader.as_raw_fd(), libc::F_DUPFD_CLOEXEC, 10) };
            if fd < 0 {
                return Err(io::Error::last_os_error());
            }
            (
                Some(unsafe { std::os::unix::net::UnixStream::from_raw_fd(fd) }),
                Some(owner),
            )
        } else {
            (None, None)
        };
        #[cfg(unix)]
        let (browser_input, browser_output, browser_cdp) = if spec.policy
            == LaunchPolicy::BrowserPipe
        {
            use std::os::fd::{AsRawFd, FromRawFd};
            let duplicate=|stream:&std::os::unix::net::UnixStream| -> io::Result<std::os::unix::net::UnixStream> {
                let fd=unsafe{libc::fcntl(stream.as_raw_fd(),libc::F_DUPFD_CLOEXEC,10)};
                if fd<0{return Err(io::Error::last_os_error());}
                Ok(unsafe{std::os::unix::net::UnixStream::from_raw_fd(fd)})
            };
            let (input, writer) = std::os::unix::net::UnixStream::pair()?;
            let (output, reader) = std::os::unix::net::UnixStream::pair()?;
            (
                Some(duplicate(&input)?),
                Some(duplicate(&output)?),
                Some(BrowserPipes {
                    reader: Box::new(reader),
                    writer: Box::new(writer),
                }),
            )
        } else {
            (None, None, None)
        };
        #[cfg(not(unix))]
        let browser_cdp = None;
        #[cfg(unix)]
        let (watchdog_reader, watchdog_owner) = {
            use std::os::fd::{AsRawFd, FromRawFd};
            let (reader, owner) = std::os::unix::net::UnixStream::pair()?;
            let fd = unsafe { libc::fcntl(reader.as_raw_fd(), libc::F_DUPFD_CLOEXEC, 3) };
            if fd < 0 {
                return Err(io::Error::last_os_error());
            }
            (
                unsafe { std::os::unix::net::UnixStream::from_raw_fd(fd) },
                owner,
            )
        };
        #[cfg(unix)]
        {
            use std::os::{fd::AsRawFd, unix::process::CommandExt};
            let reader = watchdog_reader.as_raw_fd();
            let bootstrap_fd = bootstrap_reader
                .as_ref()
                .or(browser_input.as_ref())
                .map(|stream| stream.as_raw_fd());
            let control_fd = control_reader
                .as_ref()
                .or(browser_output.as_ref())
                .map(|stream| stream.as_raw_fd());
            let driver_fd = driver_reader.as_ref().map(|stream| stream.as_raw_fd());
            let max_fd = unsafe { libc::sysconf(libc::_SC_OPEN_MAX) };
            if max_fd < 0 {
                return Err(io::Error::other("Process descriptor bound unavailable"));
            }
            command.process_group(0);
            // Fork a tiny death guard before exec, inside the *owned* group.
            // Only async-signal-safe syscalls occur after fork. The inherited
            // private socket EOFs on owner death even when Rust Drop cannot run.
            // No executable/renderer-supplied PID is accepted by this guard.
            unsafe {
                command.pre_exec(move || {
                    let group = libc::getpid();
                    let guard = libc::fork();
                    if guard < 0 {
                        return Err(io::Error::last_os_error());
                    }
                    if guard == 0 {
                        if libc::dup2(reader, 0) < 0 {
                            libc::kill(-group, libc::SIGKILL);
                            libc::_exit(127);
                        }
                        #[cfg(target_os = "linux")]
                        let closed =
                            libc::syscall(libc::SYS_close_range, 1u32, u32::MAX, 0u32) == 0;
                        #[cfg(not(target_os = "linux"))]
                        let closed = false;
                        if !closed {
                            for fd in 1..max_fd {
                                libc::close(fd as i32);
                            }
                        }
                        let mut byte = 0u8;
                        loop {
                            if libc::read(0, &mut byte as *mut u8 as _, 1) <= 0 {
                                libc::kill(-group, libc::SIGKILL);
                                libc::_exit(0);
                            }
                        }
                    }
                    if let Some(fd) = bootstrap_fd {
                        if libc::dup2(fd, 3) < 0 {
                            return Err(io::Error::last_os_error());
                        }
                    }
                    if let Some(fd) = control_fd {
                        if libc::dup2(fd, 4) < 0 {
                            return Err(io::Error::last_os_error());
                        }
                    }
                    if let Some(fd) = driver_fd {
                        if libc::dup2(fd, 5) < 0 {
                            return Err(io::Error::last_os_error());
                        }
                    }
                    Ok(())
                });
            }
        }
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            // Suspend before any untrusted/native child code can create a
            // descendant outside the kill-on-close job. Resume after assignment.
            command.creation_flags(windows_sys::Win32::System::Threading::CREATE_SUSPENDED);
        }
        let mut child = command.spawn()?;
        // A failed setup still returns the suspended child to the manager.
        // The manager records it before surfacing an error and retains it until
        // bounded termination/reap is confirmed; no usable handle escapes.
        #[cfg(windows)]
        let (job, bootstrap_owner, control_owner, driver_owner, launch_failed) = {
            match windows_job::Job::attach(&child) {
                Err(_) => (None, None, None, None, true),
                Ok(job) => {
                    let channels = if spec.policy == LaunchPolicy::PackagedWorker {
                        crate::windows_private::prepare(&mut child, spec.generation).map(
                            |(bootstrap, control, driver)| {
                                (Some(bootstrap), Some(control), Some(driver))
                            },
                        )
                    } else {
                        Ok((None, None, None))
                    };
                    match channels {
                        Err(_) => (Some(job), None, None, None, true),
                        Ok((bootstrap, control, driver)) => {
                            let failed = windows_job::Job::resume(&child).is_err();
                            (Some(job), bootstrap, control, driver, failed)
                        }
                    }
                }
            }
        };
        #[cfg(not(windows))]
        let _ = &mut child;
        Ok(Box::new(SystemChild {
            child,
            #[cfg(unix)]
            _private_runtime: spec.private_runtime.clone(),
            browser_cdp,
            #[cfg(unix)]
            group_owned: true,
            #[cfg(target_os = "macos")]
            group_observation: MacosGroupObservation::new(),
            #[cfg(unix)]
            _parent_watchdog: watchdog_owner,
            #[cfg(unix)]
            bootstrap: bootstrap_owner,
            #[cfg(unix)]
            control: control_owner,
            #[cfg(unix)]
            driver: driver_owner,
            #[cfg(windows)]
            bootstrap: bootstrap_owner,
            #[cfg(windows)]
            control: control_owner,
            #[cfg(windows)]
            driver: driver_owner,
            #[cfg(windows)]
            _job: job,
            #[cfg(windows)]
            launch_failed,
        }))
    }
}
impl SystemChild {
    #[cfg(unix)]
    fn signal_group(&self, signal: i32) -> io::Result<()> {
        if !self.group_owned {
            return Ok(());
        }
        // The unreaped group leader pins this group identity. No caller PID
        // or global process-name lookup is accepted.
        let result = unsafe { libc::kill(-(self.child.id() as i32), signal) };
        if result == 0 {
            Ok(())
        } else {
            let error = io::Error::last_os_error();
            if error.raw_os_error() == Some(libc::ESRCH) {
                Ok(())
            } else {
                Err(error)
            }
        }
    }
}
impl OwnedChild for SystemChild {
    #[cfg(target_os = "macos")]
    fn native_group_leader(&self) -> Option<i32> {
        if self.group_owned {
            i32::try_from(self.child.id()).ok()
        } else {
            None
        }
    }
    #[cfg(windows)]
    fn native_identity(&self) -> Option<NativeIdentity> {
        use std::os::windows::io::AsRawHandle;
        Some(NativeIdentity {
            pid: self.child.id(),
            process: self.child.as_raw_handle(),
            job: self._job.as_ref()?.raw(),
        })
    }
    #[cfg(windows)]
    fn launch_error(&self) -> Option<io::Error> {
        self.launch_failed
            .then(|| io::Error::other("Owned process setup failed"))
    }
    fn take_pipes(&mut self) -> Option<OwnedPipes> {
        Some(OwnedPipes {
            browser_cdp: self.browser_cdp.take(),
            stdin: Box::new(self.child.stdin.take()?),
            stdout: Box::new(self.child.stdout.take()?),
            stderr: Box::new(self.child.stderr.take()?),
            #[cfg(unix)]
            bootstrap: self.bootstrap.take(),
            #[cfg(unix)]
            control: self.control.take(),
            #[cfg(unix)]
            driver: self.driver.take(),
            #[cfg(windows)]
            bootstrap: self.bootstrap.take(),
            #[cfg(windows)]
            control: self.control.take(),
            #[cfg(windows)]
            driver: self.driver.take(),
        })
    }
    fn exited(&mut self) -> io::Result<bool> {
        #[cfg(unix)]
        {
            if self.group_owned {
                let mut info = std::mem::MaybeUninit::<libc::siginfo_t>::zeroed();
                // WNOWAIT observes exit without reaping/releasing the group PID.
                let result = unsafe {
                    libc::waitid(
                        libc::P_PID,
                        self.child.id(),
                        info.as_mut_ptr(),
                        libc::WEXITED | libc::WNOHANG | libc::WNOWAIT,
                    )
                };
                if result != 0 {
                    return Err(io::Error::last_os_error());
                }
                let observed = unsafe { info.assume_init().si_pid() };
                if observed == 0 {
                    return Ok(false);
                }
                if observed != self.child.id() as i32 {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "Owned child identity changed",
                    ));
                }
                #[cfg(target_os = "macos")]
                {
                    if let Err(error) = self.signal_group(libc::SIGKILL) {
                        if error.raw_os_error() != Some(libc::EPERM) {
                            return Err(error);
                        }
                    }
                    // Darwin killpg excludes zombies and can return EPERM when
                    // the pinned group contains only zombies. EPERM itself is
                    // never exit proof: observe the whole group before reaping.
                    if !self.group_observation.stopped(self.child.id() as i32)? {
                        return Ok(false);
                    }
                }
                #[cfg(not(target_os = "macos"))]
                self.signal_group(libc::SIGKILL)?;
                self.group_owned = false;
            }
        }
        let exited = self.child.try_wait()?.is_some();
        #[cfg(windows)]
        if exited {
            if let Some(job) = &self._job {
                if !job.empty()? {
                    let _ = job.terminate();
                    return Ok(false);
                }
            }
        }
        Ok(exited)
    }
    fn terminate(&mut self) -> io::Result<()> {
        #[cfg(unix)]
        {
            self.signal_group(libc::SIGTERM)
        }
        #[cfg(not(unix))]
        {
            self.kill()
        }
    }
    fn kill(&mut self) -> io::Result<()> {
        #[cfg(unix)]
        {
            self.signal_group(libc::SIGKILL)
        }
        #[cfg(windows)]
        {
            match &self._job {
                Some(job) => job.terminate(),
                None => self.child.kill(),
            }
        }
        #[cfg(not(any(unix, windows)))]
        {
            self.child.kill()
        }
    }
}
impl Drop for SystemChild {
    fn drop(&mut self) {
        let _ = self.kill();
        let _ = self.child.try_wait();
    }
}

#[cfg(windows)]
mod windows_job {
    use super::*;
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::{
        Foundation::{CloseHandle, HANDLE, INVALID_HANDLE_VALUE},
        System::{
            Diagnostics::ToolHelp::{
                CreateToolhelp32Snapshot, Thread32First, Thread32Next, TH32CS_SNAPTHREAD,
                THREADENTRY32,
            },
            JobObjects::{
                AssignProcessToJobObject, CreateJobObjectW, JobObjectBasicAccountingInformation,
                JobObjectExtendedLimitInformation, QueryInformationJobObject,
                SetInformationJobObject, TerminateJobObject,
                JOBOBJECT_BASIC_ACCOUNTING_INFORMATION, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
                JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
            },
            Threading::{OpenThread, ResumeThread, THREAD_SUSPEND_RESUME},
        },
    };
    struct OwnedHandle(HANDLE);
    // Kernel handles may be moved between host threads; ownership stays unique.
    unsafe impl Send for OwnedHandle {}
    impl Drop for OwnedHandle {
        fn drop(&mut self) {
            unsafe {
                CloseHandle(self.0);
            }
        }
    }
    pub struct Job(OwnedHandle);
    impl Job {
        pub fn attach(child: &Child) -> io::Result<Self> {
            unsafe {
                let raw = CreateJobObjectW(std::ptr::null(), std::ptr::null());
                if raw.is_null() {
                    return Err(io::Error::last_os_error());
                }
                let job = Self(OwnedHandle(raw));
                let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
                limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                if SetInformationJobObject(
                    raw,
                    JobObjectExtendedLimitInformation,
                    &limits as *const _ as _,
                    std::mem::size_of_val(&limits) as u32,
                ) == 0
                    || AssignProcessToJobObject(raw, child.as_raw_handle()) == 0
                {
                    return Err(io::Error::last_os_error());
                }
                Ok(job)
            }
        }
        pub fn resume(child: &Child) -> io::Result<()> {
            unsafe {
                let snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
                if snapshot == INVALID_HANDLE_VALUE {
                    return Err(io::Error::last_os_error());
                }
                let snapshot = OwnedHandle(snapshot);
                let mut entry: THREADENTRY32 = std::mem::zeroed();
                entry.dwSize = std::mem::size_of_val(&entry) as u32;
                let mut found = Thread32First(snapshot.0, &mut entry);
                while found != 0 {
                    if entry.th32OwnerProcessID == child.id() {
                        let thread = OpenThread(THREAD_SUSPEND_RESUME, 0, entry.th32ThreadID);
                        if thread.is_null() {
                            return Err(io::Error::last_os_error());
                        }
                        let thread = OwnedHandle(thread);
                        if ResumeThread(thread.0) == u32::MAX {
                            return Err(io::Error::last_os_error());
                        }
                        return Ok(());
                    }
                    found = Thread32Next(snapshot.0, &mut entry);
                }
                Err(io::Error::new(
                    io::ErrorKind::NotFound,
                    "Owned suspended thread unavailable",
                ))
            }
        }
        pub fn raw(&self) -> HANDLE {
            self.0 .0
        }
        pub fn empty(&self) -> io::Result<bool> {
            let mut info: JOBOBJECT_BASIC_ACCOUNTING_INFORMATION = unsafe { std::mem::zeroed() };
            if unsafe {
                QueryInformationJobObject(
                    self.0 .0,
                    JobObjectBasicAccountingInformation,
                    (&mut info as *mut JOBOBJECT_BASIC_ACCOUNTING_INFORMATION).cast(),
                    std::mem::size_of_val(&info) as u32,
                    std::ptr::null_mut(),
                )
            } == 0
            {
                Err(io::Error::last_os_error())
            } else {
                Ok(info.ActiveProcesses == 0)
            }
        }
        pub fn terminate(&self) -> io::Result<()> {
            if unsafe { TerminateJobObject(self.0 .0, 1) } != 0 {
                Ok(())
            } else {
                Err(io::Error::last_os_error())
            }
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        cell::Cell,
        rc::Rc,
        sync::{
            atomic::{AtomicBool, Ordering},
            Arc,
        },
    };

    #[cfg(unix)]
    #[test]
    fn unconfirmed_termination_retains_artifact_guard_until_exact_child_exits() {
        use crate::bundle_inventory::{Entry, Inventory};
        use sha2::{Digest, Sha256};
        struct Blocked(Arc<AtomicBool>);
        impl OwnedChild for Blocked {
            fn exited(&mut self) -> io::Result<bool> {
                Ok(self.0.load(Ordering::SeqCst))
            }
            fn terminate(&mut self) -> io::Result<()> {
                Ok(())
            }
            fn kill(&mut self) -> io::Result<()> {
                Ok(())
            }
        }
        let root = std::env::temp_dir().join(format!(
            "threadrelay-guard-retention-{}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(root.join("source")).unwrap();
        std::fs::write(root.join("source/code"), b"synthetic").unwrap();
        let inventory = Inventory::new(vec![Entry::File {
            path: "code".into(),
            sha256: format!("{:x}", Sha256::digest(b"synthetic")),
            size: 9,
            executable: false,
        }])
        .unwrap();
        let guard =
            crate::verified_artifacts::synthetic_snapshot(&root.join("source"), &root, &inventory)
                .unwrap();
        let guarded = guard.root().to_owned();
        let exited = Arc::new(AtomicBool::new(false));
        let mut owner = DesktopSupervisor::acquire(&root).unwrap();
        owner.artifacts.push(guard);
        owner.processes.children.insert(
            ProcessKind::Worker,
            RegisteredChild {
                id: 1,
                process: Box::new(Blocked(exited.clone())),
            },
        );
        assert!(!owner.close_generation(0).stopped);
        assert!(guarded.join("code").exists());
        exited.store(true, Ordering::SeqCst);
        assert!(owner.close_generation(0).stopped);
        assert!(!guarded.exists());
        assert!(root.join("source/code").exists());
        drop(owner);
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn lock_is_exclusive_until_owner_drops_and_lockfile_is_not_removed() {
        let path = std::env::temp_dir().join(format!("threadrelay-lock-{}", std::process::id()));
        let first = InstanceLock::acquire(&path).unwrap();
        assert!(InstanceLock::acquire(&path).is_err());
        drop(first);
        assert!(path.exists());
        let _second = InstanceLock::acquire(&path).unwrap();
    }

    #[test]
    #[cfg(unix)]
    fn lock_release_is_not_delayed_by_a_retained_inherited_file_description() {
        let path =
            std::env::temp_dir().join(format!("threadrelay-inherited-lock-{}", std::process::id()));
        let owner = InstanceLock::acquire(&path).unwrap();
        // A fork inherits the same open file description. Retaining a clone
        // models that inheritance deterministically, without scheduling sleeps.
        let inherited_description = owner._file.try_clone().unwrap();
        assert!(InstanceLock::acquire(&path).is_err());
        drop(owner);
        let _new_owner = InstanceLock::acquire(&path).expect(
            "owner drop must release its lock even while an inherited descriptor remains open",
        );
        drop(inherited_description);
        assert!(InstanceLock::acquire(&path).is_err());
    }

    struct FakeClock(Rc<Cell<u64>>);
    impl Clock for FakeClock {
        fn elapsed(&self) -> Duration {
            Duration::from_millis(self.0.get())
        }
        fn wait(&self, duration: Duration) {
            self.0.set(self.0.get() + duration.as_millis() as u64);
        }
    }

    struct FakeProcess {
        alive: Arc<AtomicBool>,
        stubborn: bool,
    }
    impl OwnedChild for FakeProcess {
        fn exited(&mut self) -> io::Result<bool> {
            Ok(!self.alive.load(Ordering::SeqCst))
        }
        fn terminate(&mut self) -> io::Result<()> {
            if !self.stubborn {
                self.alive.store(false, Ordering::SeqCst);
            }
            Ok(())
        }
        fn kill(&mut self) -> io::Result<()> {
            self.alive.store(false, Ordering::SeqCst);
            Ok(())
        }
    }

    struct FakeSpawner {
        launched: Rc<Cell<usize>>,
        children: Vec<(Arc<AtomicBool>, bool)>,
    }
    impl Spawner for FakeSpawner {
        fn spawn(&mut self, _spec: &SpawnSpec) -> io::Result<Box<dyn OwnedChild>> {
            self.launched.set(self.launched.get() + 1);
            let (alive, stubborn) = self.children.remove(0);
            alive.store(true, Ordering::SeqCst);
            Ok(Box::new(FakeProcess { alive, stubborn }))
        }
    }

    #[test]
    fn inference_token_is_only_an_explicit_private_environment_entry() {
        let mut token = [0u8; 32];
        getrandom::fill(&mut token).unwrap();
        let value: String = token.iter().map(|b| format!("{b:02x}")).collect();
        let spec = SpawnSpec::supervised_inference_root(
            "fixture",
            ["--offline", "--host", "127.0.0.1"],
            value.clone(),
        )
        .unwrap();
        assert_eq!(spec.inference_token.as_deref(), Some(value.as_str()));
        assert!(spec.dependency_allowed(ProcessKind::Inference));
        assert!(!spec.dependency_allowed(ProcessKind::Browser));
        assert!(!spec
            .args
            .iter()
            .any(|arg| arg == std::ffi::OsStr::new(&value)));
        assert!(SpawnSpec::supervised_inference_root(
            "fixture",
            std::iter::empty::<&str>(),
            "bad".into()
        )
        .is_err());
    }

    #[test]
    fn liveness_uses_only_a_registered_same_manager_capability() {
        let alive = Arc::new(AtomicBool::new(false));
        let mut owner = OwnedProcesses::with_dependencies(
            FakeClock(Rc::new(Cell::new(0))),
            FakeSpawner {
                launched: Rc::new(Cell::new(0)),
                children: vec![(alive.clone(), false)],
            },
        );
        let handle = owner
            .spawn(ProcessKind::Worker, &SpawnSpec::packaged_worker("fixture"))
            .unwrap();
        assert!(owner.is_live(&handle).unwrap());
        let mut foreign = OwnedProcesses::default();
        assert!(foreign.is_live(&handle).is_err());
        alive.store(false, Ordering::SeqCst);
        assert!(!owner.is_live(&handle).unwrap());
        assert!(owner.shutdown(0).stopped);
    }

    #[test]
    fn post_creation_failure_stays_owned_until_confirmed_cleanup() {
        struct FailedChild(Arc<AtomicBool>);
        impl OwnedChild for FailedChild {
            fn launch_error(&self) -> Option<io::Error> {
                Some(io::Error::other("setup failed"))
            }
            fn exited(&mut self) -> io::Result<bool> {
                Ok(!self.0.load(Ordering::SeqCst))
            }
            fn terminate(&mut self) -> io::Result<()> {
                Ok(())
            }
            fn kill(&mut self) -> io::Result<()> {
                Ok(())
            }
        }
        struct Failing(Arc<AtomicBool>);
        impl Spawner for Failing {
            fn spawn(&mut self, _: &SpawnSpec) -> io::Result<Box<dyn OwnedChild>> {
                Ok(Box::new(FailedChild(self.0.clone())))
            }
        }
        let alive = Arc::new(AtomicBool::new(true));
        let mut owner = OwnedProcesses::with_dependencies(
            FakeClock(Rc::new(Cell::new(0))),
            Failing(alive.clone()),
        );
        assert!(owner
            .spawn(ProcessKind::Worker, &SpawnSpec::packaged_worker("fixture"))
            .is_err());
        assert_eq!(
            owner.shutdown(0),
            ShutdownReport {
                stopped: false,
                remaining: vec!["worker".into()]
            }
        );
        alive.store(false, Ordering::SeqCst);
        assert!(owner.shutdown(0).stopped);
    }

    #[test]
    fn browser_and_driver_are_separately_registered_dependencies() {
        let children = (0..3)
            .map(|_| (Arc::new(AtomicBool::new(false)), false))
            .collect();
        let mut owner = OwnedProcesses::with_dependencies(
            FakeClock(Rc::new(Cell::new(0))),
            FakeSpawner {
                launched: Rc::new(Cell::new(0)),
                children,
            },
        );
        let parent = owner
            .spawn(
                ProcessKind::Worker,
                &SpawnSpec::guarded_fixture("fixture", std::iter::empty::<&str>()),
            )
            .unwrap();
        let spec = SpawnSpec::supervised_dependency_root("fixture", std::iter::empty::<&str>());
        let browser = owner
            .spawn_dependency(&parent, ProcessKind::Browser, &spec)
            .unwrap();
        let driver = owner
            .spawn_dependency(&parent, ProcessKind::BrowserDriver, &spec)
            .unwrap();
        assert!(owner.close_dependency(&driver, 0).unwrap().stopped);
        assert!(owner.close_dependency(&browser, 0).unwrap().stopped);
        assert!(owner.shutdown(0).stopped);
    }

    #[test]
    fn dependency_close_is_scoped_and_keeps_worker_and_manager_live() {
        let parent_alive = Arc::new(AtomicBool::new(false));
        let dep_alive = Arc::new(AtomicBool::new(false));
        let mut processes = OwnedProcesses::with_dependencies(
            FakeClock(Rc::new(Cell::new(0))),
            FakeSpawner {
                launched: Rc::new(Cell::new(0)),
                children: vec![(parent_alive.clone(), false), (dep_alive.clone(), false)],
            },
        );
        let spec = SpawnSpec::guarded_fixture("fixture", std::iter::empty::<&str>());
        let parent = processes.spawn(ProcessKind::Worker, &spec).unwrap();
        let dep = processes
            .spawn_dependency(&parent, ProcessKind::Inference, &spec)
            .unwrap();
        assert!(processes.close_dependency(&parent, 0).is_err());
        assert_eq!(
            processes.close_dependency(&dep, 100).unwrap(),
            ShutdownReport {
                stopped: true,
                remaining: vec![]
            }
        );
        assert!(parent_alive.load(Ordering::SeqCst));
        assert!(!dep_alive.load(Ordering::SeqCst));
        assert!(processes.close_dependency(&dep, 0).is_err());
        assert!(processes.shutdown(0).stopped);
    }

    #[test]
    fn shutdown_is_bounded_and_touches_only_owned_children() {
        let elapsed = Rc::new(Cell::new(0));
        let owned = Arc::new(AtomicBool::new(false));
        let unrelated_browser = Arc::new(AtomicBool::new(true));
        let mut processes = OwnedProcesses::with_dependencies(
            FakeClock(elapsed.clone()),
            FakeSpawner {
                launched: Rc::new(Cell::new(0)),
                children: vec![(owned.clone(), true)],
            },
        );
        let _opaque = processes
            .spawn(
                ProcessKind::Browser,
                &SpawnSpec::guarded_fixture("synthetic-browser", std::iter::empty::<&str>()),
            )
            .unwrap();
        assert!(owned.load(Ordering::SeqCst));
        assert_eq!(
            processes.shutdown(5000),
            ShutdownReport {
                stopped: true,
                remaining: vec![]
            }
        );
        assert_eq!(elapsed.get(), 5000);
        assert!(!owned.load(Ordering::SeqCst));
        assert!(unrelated_browser.load(Ordering::SeqCst));
    }

    #[test]
    fn owned_kind_is_singleton_shutdown_is_terminal_and_browser_loss_does_not_relaunch() {
        let launched = Rc::new(Cell::new(0));
        let owned = Arc::new(AtomicBool::new(false));
        let mut processes = OwnedProcesses::with_dependencies(
            FakeClock(Rc::new(Cell::new(0))),
            FakeSpawner {
                launched: launched.clone(),
                children: vec![(owned.clone(), false)],
            },
        );
        let spec = SpawnSpec::guarded_fixture("synthetic-browser", std::iter::empty::<&str>());
        processes.spawn(ProcessKind::Browser, &spec).unwrap();
        assert!(processes.spawn(ProcessKind::Browser, &spec).is_err());
        owned.store(false, Ordering::SeqCst);
        assert_eq!(processes.poll_exits().unwrap(), vec![ProcessKind::Browser]);
        assert_eq!(launched.get(), 1);
        processes.shutdown(0);
        assert!(processes.spawn(ProcessKind::Browser, &spec).is_err());
        assert_eq!(launched.get(), 1);
    }
    #[test]
    fn profile_supervisor_holds_exclusive_lock_until_full_owner_is_dropped() {
        let dir = std::env::temp_dir().join(format!("threadrelay-profile-{}", std::process::id()));
        let owner = DesktopSupervisor::acquire(&dir).unwrap();
        assert!(DesktopSupervisor::acquire(&dir).is_err());
        drop(owner);
        assert!(DesktopSupervisor::acquire(&dir).is_ok());
    }

    #[test]
    fn lock_fixture() {
        if let Some(path) = std::env::var_os("THREADRELAY_TEST_LOCK") {
            let locked = std::env::var_os("THREADRELAY_TEST_LOCKED").is_some();
            assert_eq!(InstanceLock::acquire(Path::new(&path)).is_err(), locked);
        }
    }

    #[test]
    fn lock_rejects_another_process_and_releases_on_owner_exit() {
        let path =
            std::env::temp_dir().join(format!("threadrelay-cross-process-{}", std::process::id()));
        let owner = InstanceLock::acquire(&path).unwrap();
        let run_child = |locked| {
            let mut command = Command::new(std::env::current_exe().unwrap());
            command
                .args(["--exact", "supervisor::tests::lock_fixture"])
                .env("THREADRELAY_TEST_LOCK", &path);
            if locked {
                command.env("THREADRELAY_TEST_LOCKED", "1");
            }
            assert!(command.output().unwrap().status.success());
        };
        run_child(true);
        drop(owner);
        run_child(false);
    }

    #[test]
    fn process_fixture() {
        // Only the explicitly selected child run waits on its private pipe.
        if std::env::args().any(|arg| arg == "supervisor::tests::process_fixture") {
            use std::io::{Read, Write};
            println!("FIXTURE_READY");
            std::io::stdout().flush().unwrap();
            let _ = std::io::stdin().read(&mut [0]);
        }
    }

    #[test]
    fn real_registered_dependency_groups_stop_and_unrelated_fake_browser_remains_alive() {
        use std::io::{BufRead, BufReader, Read};
        let exe = std::env::current_exe().unwrap();
        let args = [
            "--exact",
            "supervisor::tests::process_fixture",
            "--nocapture",
        ];
        let mut unrelated = Command::new(&exe)
            .args(args)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        let wait_ready = |reader: &mut dyn BufRead| loop {
            let mut line = String::new();
            assert_ne!(reader.read_line(&mut line).unwrap(), 0);
            if line.trim() == "FIXTURE_READY" {
                break;
            }
        };
        let mut unrelated_stdout = BufReader::new(unrelated.stdout.take().unwrap());
        wait_ready(&mut unrelated_stdout);
        struct YieldClock(Instant);
        impl Clock for YieldClock {
            fn elapsed(&self) -> Duration {
                self.0.elapsed()
            }
            fn wait(&self, _: Duration) {
                std::thread::yield_now();
            }
        }
        let mut owner =
            OwnedProcesses::with_dependencies(YieldClock(Instant::now()), SystemSpawner);
        let mut parent = owner
            .spawn(ProcessKind::Worker, &SpawnSpec::guarded_fixture(&exe, args))
            .unwrap();
        let mut browser = owner
            .spawn_dependency(
                &parent,
                ProcessKind::Browser,
                &SpawnSpec::supervised_dependency_root(&exe, args),
            )
            .unwrap();
        let parent_pipes = parent.take_pipes().unwrap();
        let browser_pipes = browser.take_pipes().unwrap();
        let _keep_inputs_open = (parent_pipes.stdin, browser_pipes.stdin);
        let mut parent_stdout = BufReader::new(parent_pipes.stdout);
        let mut browser_stdout = BufReader::new(browser_pipes.stdout);
        wait_ready(&mut parent_stdout);
        wait_ready(&mut browser_stdout);
        assert!(owner.shutdown(5000).stopped);
        parent_stdout.read_to_end(&mut Vec::new()).unwrap();
        browser_stdout.read_to_end(&mut Vec::new()).unwrap();
        assert!(owner.shutdown(0).stopped);
        assert!(unrelated.try_wait().unwrap().is_none());
        unrelated.kill().unwrap();
        unrelated.wait().unwrap();
    }
    #[cfg(target_os = "macos")]
    #[test]
    fn zombie_only_group_is_confirmed_before_releasing_the_leader_identity() {
        let mut child = SystemSpawner
            .spawn(&SpawnSpec::packaged_worker("/bin/sh"))
            .unwrap();
        let _pipes = child.take_pipes().unwrap();
        let group = child.native_group_leader().unwrap();
        child.terminate().unwrap();
        let deadline = Instant::now() + Duration::from_secs(2);
        loop {
            let mut info = std::mem::MaybeUninit::<libc::siginfo_t>::zeroed();
            assert_eq!(
                unsafe {
                    libc::waitid(
                        libc::P_PID,
                        group as u32,
                        info.as_mut_ptr(),
                        libc::WEXITED | libc::WNOHANG | libc::WNOWAIT,
                    )
                },
                0
            );
            let exited = unsafe { info.assume_init().si_pid() } == group;
            let signal = unsafe { libc::kill(-group, 0) };
            let error = io::Error::last_os_error();
            if exited && signal == -1 && error.raw_os_error() == Some(libc::EPERM) {
                break;
            }
            assert!(
                Instant::now() < deadline,
                "owned group did not reach its zombie-only state"
            );
            std::thread::sleep(Duration::from_millis(5));
        }
        let result = child.exited();
        assert!(
            matches!(result, Ok(true)),
            "zombie-only group remained owned: {result:?}"
        );
        assert!(child.native_group_leader().is_none());
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn macos_group_observation_keeps_a_live_watchdog_owned_after_leader_exit() {
        use std::io::Write;
        let mut child = SystemSpawner
            .spawn(&SpawnSpec::packaged_worker("/bin/sh"))
            .unwrap();
        let mut pipes = child.take_pipes().unwrap();
        let group = child.native_group_leader().unwrap();
        pipes.stdin.write_all(b"exit 0\n").unwrap();
        let deadline = Instant::now() + Duration::from_secs(2);
        loop {
            let mut info = std::mem::MaybeUninit::<libc::siginfo_t>::zeroed();
            assert_eq!(
                unsafe {
                    libc::waitid(
                        libc::P_PID,
                        group as u32,
                        info.as_mut_ptr(),
                        libc::WEXITED | libc::WNOHANG | libc::WNOWAIT,
                    )
                },
                0
            );
            if unsafe { info.assume_init().si_pid() } == group {
                break;
            }
            assert!(Instant::now() < deadline);
            std::thread::sleep(Duration::from_millis(5));
        }
        let mut observation = MacosGroupObservation::new();
        assert!(
            !observation.stopped(group).unwrap(),
            "live watchdog must keep cleanup pending"
        );
        while !child.exited().unwrap() {
            assert!(Instant::now() < deadline);
            std::thread::sleep(Duration::from_millis(5));
        }
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn macos_group_observation_rejects_incomplete_and_foreign_identity_evidence() {
        let mut child = SystemSpawner
            .spawn(&SpawnSpec::packaged_worker("/bin/sh"))
            .unwrap();
        let _pipes = child.take_pipes().unwrap();
        let group = child.native_group_leader().unwrap();
        assert!(
            macos_group_members(group, &mut [0; 1]).is_err(),
            "capacity equality can hide members"
        );
        assert!(
            macos_group_members(i32::MAX, &mut [0; 4]).is_err(),
            "a missing pinned group is not completion proof"
        );
        let mut observation = MacosGroupObservation::new();
        observation.uid = observation.uid.wrapping_add(1);
        assert_eq!(
            observation.stopped(group).unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
        observation.uid = observation.uid.wrapping_sub(1);
        assert!(
            observation.stopped(group).is_err(),
            "identity changes must remain latched"
        );
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn group_escape_process_fixture() {
        if std::env::args().any(|arg| arg == "supervisor::tests::group_escape_process_fixture") {
            use std::io::{Read, Write};
            println!("GROUP_MEMBER_READY");
            std::io::stdout().flush().unwrap();
            std::io::stdin().read_exact(&mut [0]).unwrap();
            assert_eq!(unsafe { libc::setpgid(0, 0) }, 0);
            println!("GROUP_MEMBER_ESCAPED");
            std::io::stdout().flush().unwrap();
            let _ = std::io::stdin().read(&mut [0]);
        }
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn macos_group_observation_latches_an_observed_member_escape() {
        use std::{
            io::{BufRead, BufReader, Write},
            os::{fd::AsRawFd, unix::process::CommandExt},
        };
        let mut child = SystemSpawner
            .spawn(&SpawnSpec::packaged_worker("/bin/sh"))
            .unwrap();
        let _pipes = child.take_pipes().unwrap();
        let group = child.native_group_leader().unwrap();
        let mut member = Command::new(std::env::current_exe().unwrap());
        member
            .args([
                "--exact",
                "supervisor::tests::group_escape_process_fixture",
                "--nocapture",
            ])
            .process_group(group)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped());
        let mut member = member.spawn().unwrap();
        let mut output = BufReader::new(member.stdout.take().unwrap());
        let mut wait_marker = |marker| loop {
            if output.buffer().is_empty() {
                let mut event = libc::pollfd {
                    fd: output.get_ref().as_raw_fd(),
                    events: libc::POLLIN,
                    revents: 0,
                };
                assert_eq!(unsafe { libc::poll(&mut event, 1, 2000) }, 1);
            }
            let mut line = String::new();
            assert_ne!(output.read_line(&mut line).unwrap(), 0);
            if line.trim() == marker {
                break;
            }
        };
        wait_marker("GROUP_MEMBER_READY");
        let mut observation = MacosGroupObservation::new();
        assert!(!observation.stopped(group).unwrap());
        member.stdin.as_mut().unwrap().write_all(b"m").unwrap();
        wait_marker("GROUP_MEMBER_ESCAPED");
        let escaped = observation.stopped(group);
        drop(member.stdin.take());
        let deadline = Instant::now() + Duration::from_secs(2);
        while member.try_wait().unwrap().is_none() {
            assert!(Instant::now() < deadline);
            std::thread::sleep(Duration::from_millis(5));
        }
        assert_eq!(escaped.unwrap_err().kind(), io::ErrorKind::InvalidData);
        assert!(
            observation.stopped(group).is_err(),
            "a later group snapshot must not erase a witnessed escape"
        );
    }

    #[test]
    fn uncooperative_process_fixture() {
        if std::env::args().any(|arg| arg == "supervisor::tests::uncooperative_process_fixture") {
            use std::io::Write;
            println!("FIXTURE_PID:{}", std::process::id());
            std::io::stdout().flush().unwrap();
            loop {
                std::thread::park();
            }
        }
    }

    #[test]
    fn crashing_owner_fixture() {
        if std::env::args().any(|arg| arg == "supervisor::tests::crashing_owner_fixture") {
            use std::io::{BufRead, BufReader, Read, Write};
            let exe = std::env::current_exe().unwrap();
            let args = [
                "--exact",
                "supervisor::tests::uncooperative_process_fixture",
                "--nocapture",
            ];
            let mut owner = OwnedProcesses::default();
            let mut parent = owner
                .spawn(ProcessKind::Worker, &SpawnSpec::guarded_fixture(&exe, args))
                .unwrap();
            let mut browser = owner
                .spawn_dependency(
                    &parent,
                    ProcessKind::Browser,
                    &SpawnSpec::supervised_dependency_root(&exe, args),
                )
                .unwrap();
            let mut inputs = Vec::new();
            for handle in [&mut parent, &mut browser] {
                let pipes = handle.take_pipes().unwrap();
                inputs.push(pipes.stdin);
                let mut output = BufReader::new(pipes.stdout);
                loop {
                    let mut line = String::new();
                    assert_ne!(output.read_line(&mut line).unwrap(), 0);
                    if line.starts_with("FIXTURE_PID:") {
                        print!("{}", line);
                        std::io::stdout().flush().unwrap();
                        break;
                    }
                }
            }
            std::io::stdin().read_exact(&mut [0]).unwrap();
            std::process::exit(0); // no destructors: both independent guards act.
        }
    }

    #[test]
    #[cfg(target_os = "linux")]
    fn owner_crash_cannot_leave_an_uncooperative_owned_browser_running() {
        use std::{
            io::{BufRead, BufReader, Write},
            os::fd::{AsRawFd, FromRawFd, OwnedFd},
        };
        let mut owner = Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "supervisor::tests::crashing_owner_fixture",
                "--nocapture",
            ])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        let mut output = BufReader::new(owner.stdout.take().unwrap());
        let mut processes = Vec::new();
        let mut pids = Vec::new();
        while processes.len() < 2 {
            let mut line = String::new();
            assert_ne!(output.read_line(&mut line).unwrap(), 0);
            if let Some(pid) = line.trim().strip_prefix("FIXTURE_PID:") {
                let pid = pid.parse::<u32>().unwrap();
                let raw = unsafe { libc::syscall(libc::SYS_pidfd_open, pid, 0) } as i32;
                assert!(raw >= 0);
                processes.push(unsafe { OwnedFd::from_raw_fd(raw) });
                pids.push(pid);
            }
        }
        assert_ne!(unsafe { libc::getpgid(pids[0] as i32) }, unsafe {
            libc::getpgid(pids[1] as i32)
        });
        owner.stdin.take().unwrap().write_all(b"x").unwrap();
        assert!(owner.wait().unwrap().success());
        for process in processes {
            let mut event = libc::pollfd {
                fd: process.as_raw_fd(),
                events: libc::POLLIN,
                revents: 0,
            };
            let ready = unsafe { libc::poll(&mut event, 1, 2000) };
            if ready != 1 {
                unsafe {
                    libc::syscall(
                        libc::SYS_pidfd_send_signal,
                        process.as_raw_fd(),
                        libc::SIGKILL,
                        std::ptr::null::<libc::siginfo_t>(),
                        0,
                    );
                }
            }
            assert_eq!(
                ready, 1,
                "registered independent group survived owner crash"
            );
            assert_ne!(event.revents & libc::POLLIN, 0);
        }
    }

    #[test]
    fn one_shared_deadline_bounds_multiple_children_and_reports_unconfirmed_names() {
        struct Resistant;
        impl OwnedChild for Resistant {
            fn exited(&mut self) -> io::Result<bool> {
                Ok(false)
            }
            fn terminate(&mut self) -> io::Result<()> {
                Err(io::Error::other("fixture refuses terminate"))
            }
            fn kill(&mut self) -> io::Result<()> {
                Err(io::Error::other("fixture refuses kill"))
            }
        }
        struct ResistantSpawner;
        impl Spawner for ResistantSpawner {
            fn spawn(&mut self, _: &SpawnSpec) -> io::Result<Box<dyn OwnedChild>> {
                Ok(Box::new(Resistant))
            }
        }
        let elapsed = Rc::new(Cell::new(0));
        let mut owner =
            OwnedProcesses::with_dependencies(FakeClock(elapsed.clone()), ResistantSpawner);
        let spec = SpawnSpec::guarded_fixture("fixture", std::iter::empty::<&str>());
        owner.spawn(ProcessKind::Worker, &spec).unwrap();
        owner.spawn(ProcessKind::Browser, &spec).unwrap();
        let report = owner.shutdown(999_999);
        assert_eq!(elapsed.get(), 5000);
        assert_eq!(
            report,
            ShutdownReport {
                stopped: false,
                remaining: vec!["worker".into(), "browser".into()]
            }
        );
        let value = serde_json::to_value(report).unwrap();
        crate::ipc::validate_contract("ShutdownReport", &value).unwrap();
    }
    #[test]
    fn unverified_launch_is_rejected_before_any_adapter_can_activate() {
        let mut owner = OwnedProcesses::default();
        let spec = SpawnSpec::new(
            std::env::current_exe().unwrap(),
            [
                "--exact",
                "supervisor::tests::process_fixture",
                "--nocapture",
            ],
        );
        let error = owner.spawn(ProcessKind::Browser, &spec).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::Unsupported);
        assert_eq!(
            owner.shutdown(0),
            ShutdownReport {
                stopped: true,
                remaining: vec![]
            }
        );
    }

    #[test]
    fn registered_nested_groups_remain_owned_when_parent_exit_is_confirmed() {
        let parent_alive = Arc::new(AtomicBool::new(false));
        let browser_alive = Arc::new(AtomicBool::new(false));
        let mut owner = OwnedProcesses::with_dependencies(
            FakeClock(Rc::new(Cell::new(0))),
            FakeSpawner {
                launched: Rc::new(Cell::new(0)),
                children: vec![(parent_alive.clone(), false), (browser_alive.clone(), true)],
            },
        );
        let spec = SpawnSpec::guarded_fixture("fixture", std::iter::empty::<&str>());
        let parent = owner.spawn(ProcessKind::Worker, &spec).unwrap();
        owner
            .spawn_dependency(&parent, ProcessKind::Browser, &spec)
            .unwrap();
        parent_alive.store(false, Ordering::SeqCst);
        assert_eq!(owner.poll_exits().unwrap(), vec![ProcessKind::Worker]);
        assert!(browser_alive.load(Ordering::SeqCst));
        assert!(owner.shutdown(0).stopped);
        assert!(!browser_alive.load(Ordering::SeqCst));
    }

    #[test]
    fn foreign_and_expired_parent_capabilities_cannot_register_or_activate_children() {
        let parent_alive = Arc::new(AtomicBool::new(false));
        let make = |children| {
            OwnedProcesses::with_dependencies(
                FakeClock(Rc::new(Cell::new(0))),
                FakeSpawner {
                    launched: Rc::new(Cell::new(0)),
                    children,
                },
            )
        };
        let mut owner = make(vec![(parent_alive.clone(), false)]);
        let mut unrelated_owner = make(vec![]);
        let spec = SpawnSpec::guarded_fixture("fixture", std::iter::empty::<&str>());
        let parent = owner.spawn(ProcessKind::Worker, &spec).unwrap();
        assert_eq!(
            unrelated_owner
                .spawn_dependency(&parent, ProcessKind::Browser, &spec)
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidInput
        );
        parent_alive.store(false, Ordering::SeqCst);
        assert_eq!(
            owner
                .spawn_dependency(&parent, ProcessKind::Browser, &spec)
                .unwrap_err()
                .kind(),
            io::ErrorKind::NotFound
        );
        assert!(owner.shutdown(0).stopped);
    }

    #[test]
    fn unconfirmed_registered_identity_is_reported_and_cannot_activate_a_dependency() {
        struct Unconfirmed;
        impl OwnedChild for Unconfirmed {
            fn exited(&mut self) -> io::Result<bool> {
                Err(io::Error::other("fixture identity unavailable"))
            }
            fn terminate(&mut self) -> io::Result<()> {
                Ok(())
            }
            fn kill(&mut self) -> io::Result<()> {
                Ok(())
            }
        }
        struct UnconfirmedSpawner;
        impl Spawner for UnconfirmedSpawner {
            fn spawn(&mut self, _: &SpawnSpec) -> io::Result<Box<dyn OwnedChild>> {
                Ok(Box::new(Unconfirmed))
            }
        }
        let mut owner =
            OwnedProcesses::with_dependencies(FakeClock(Rc::new(Cell::new(0))), UnconfirmedSpawner);
        let spec = SpawnSpec::guarded_fixture("fixture", std::iter::empty::<&str>());
        let parent = owner.spawn(ProcessKind::Worker, &spec).unwrap();
        assert!(owner
            .spawn_dependency(&parent, ProcessKind::Browser, &spec)
            .is_err());
        assert_eq!(
            owner.shutdown(0),
            ShutdownReport {
                stopped: false,
                remaining: vec!["worker".into()]
            }
        );
    }
    #[test]
    fn production_worker_and_dependency_contracts_cannot_be_interchanged() {
        let make = || {
            OwnedProcesses::with_dependencies(
                FakeClock(Rc::new(Cell::new(0))),
                FakeSpawner {
                    launched: Rc::new(Cell::new(0)),
                    children: vec![
                        (Arc::new(AtomicBool::new(false)), false),
                        (Arc::new(AtomicBool::new(false)), false),
                    ],
                },
            )
        };
        let mut owner = make();
        let worker = SpawnSpec::packaged_worker("packaged-worker");
        let dependency =
            SpawnSpec::supervised_dependency_root("direct-browser", std::iter::empty::<&str>());
        assert_eq!(
            owner
                .spawn(ProcessKind::Browser, &worker)
                .unwrap_err()
                .kind(),
            io::ErrorKind::Unsupported
        );
        assert_eq!(
            owner
                .spawn(ProcessKind::Browser, &dependency)
                .unwrap_err()
                .kind(),
            io::ErrorKind::Unsupported
        );
        let parent = owner.spawn(ProcessKind::Worker, &worker).unwrap();
        assert_eq!(
            owner
                .spawn_dependency(&parent, ProcessKind::Inference, &worker)
                .unwrap_err()
                .kind(),
            io::ErrorKind::Unsupported
        );
        assert_eq!(
            owner
                .spawn_dependency(
                    &parent,
                    ProcessKind::Browser,
                    &SpawnSpec::new("playwright-launcher", std::iter::empty::<&str>())
                )
                .unwrap_err()
                .kind(),
            io::ErrorKind::Unsupported
        );
        owner
            .spawn_dependency(&parent, ProcessKind::Browser, &dependency)
            .unwrap();
        assert!(owner.shutdown(0).stopped);
    }

    #[test]
    fn unconfirmed_dependency_is_not_erased_when_original_parent_exits() {
        struct Dependency {
            parent: bool,
            alive: Arc<AtomicBool>,
        }
        impl OwnedChild for Dependency {
            fn exited(&mut self) -> io::Result<bool> {
                Ok(self.parent && !self.alive.load(Ordering::SeqCst))
            }
            fn terminate(&mut self) -> io::Result<()> {
                Ok(())
            }
            fn kill(&mut self) -> io::Result<()> {
                Err(io::Error::other(
                    "fixture cannot confirm dependency cleanup",
                ))
            }
        }
        struct Dependencies {
            first: bool,
            alive: Arc<AtomicBool>,
        }
        impl Spawner for Dependencies {
            fn spawn(&mut self, _: &SpawnSpec) -> io::Result<Box<dyn OwnedChild>> {
                let parent = self.first;
                self.first = false;
                Ok(Box::new(Dependency {
                    parent,
                    alive: self.alive.clone(),
                }))
            }
        }
        let alive = Arc::new(AtomicBool::new(true));
        let mut owner = OwnedProcesses::with_dependencies(
            FakeClock(Rc::new(Cell::new(0))),
            Dependencies {
                first: true,
                alive: alive.clone(),
            },
        );
        let parent = owner
            .spawn(ProcessKind::Worker, &SpawnSpec::packaged_worker("worker"))
            .unwrap();
        owner
            .spawn_dependency(
                &parent,
                ProcessKind::Browser,
                &SpawnSpec::supervised_dependency_root("browser", std::iter::empty::<&str>()),
            )
            .unwrap();
        alive.store(false, Ordering::SeqCst);
        assert_eq!(owner.poll_exits().unwrap(), vec![ProcessKind::Worker]);
        assert_eq!(
            owner.shutdown(0),
            ShutdownReport {
                stopped: false,
                remaining: vec!["browser".into()]
            }
        );
    }
    #[cfg(unix)]
    #[test]
    fn packaged_worker_receives_distinct_inherited_bootstrap_channel() {
        use std::io::{Read, Write};
        let mut owner = OwnedProcesses::default();
        let mut handle = owner
            .spawn(ProcessKind::Worker, &SpawnSpec::packaged_worker("/bin/sh"))
            .unwrap();
        let mut pipes = handle.take_pipes().unwrap();
        let mut bootstrap = pipes.bootstrap.take().unwrap();
        bootstrap.write_all(b"private").unwrap();
        bootstrap.shutdown(std::net::Shutdown::Write).unwrap();
        pipes.stdin.write_all(b"cat <&3\n").unwrap();
        let mut response = [0; 7];
        pipes.stdout.read_exact(&mut response).unwrap();
        assert_eq!(&response, b"private");
        assert!(owner.shutdown(100).stopped);
    }

    #[cfg(unix)]
    #[test]
    fn packaged_worker_does_not_inherit_python_or_playwright_overrides() {
        use std::io::{Read, Write};
        std::env::set_var("PYTHONPATH", "fixture-untrusted");
        let mut owner = OwnedProcesses::default();
        let mut handle = owner
            .spawn(ProcessKind::Worker, &SpawnSpec::packaged_worker("/bin/sh"))
            .unwrap();
        let mut pipes = handle.take_pipes().unwrap();
        pipes
            .stdin
            .write_all(b"printf '%s' \"${PYTHONPATH-unset}\"\n")
            .unwrap();
        let mut value = [0; 5];
        pipes.stdout.read_exact(&mut value).unwrap();
        std::env::remove_var("PYTHONPATH");
        assert_eq!(&value, b"unset");
        assert!(owner.shutdown(100).stopped);
    }

    #[test]
    fn cooperative_shutdown_waits_without_signalling_and_obeys_one_deadline() {
        use std::sync::atomic::AtomicU64;
        struct StepClock(Arc<AtomicU64>);
        impl Clock for StepClock {
            fn elapsed(&self) -> Duration {
                Duration::from_millis(self.0.load(Ordering::SeqCst))
            }
            fn wait(&self, duration: Duration) {
                self.0
                    .fetch_add(duration.as_millis() as u64, Ordering::SeqCst);
            }
        }
        struct Cooperative {
            elapsed: Arc<AtomicU64>,
            signalled: Arc<AtomicBool>,
        }
        impl OwnedChild for Cooperative {
            fn exited(&mut self) -> io::Result<bool> {
                Ok(self.elapsed.load(Ordering::SeqCst) >= 30)
            }
            fn terminate(&mut self) -> io::Result<()> {
                self.signalled.store(true, Ordering::SeqCst);
                Ok(())
            }
            fn kill(&mut self) -> io::Result<()> {
                self.signalled.store(true, Ordering::SeqCst);
                Ok(())
            }
        }
        struct FixtureSpawner {
            elapsed: Arc<AtomicU64>,
            signalled: Arc<AtomicBool>,
        }
        impl Spawner for FixtureSpawner {
            fn spawn(&mut self, _: &SpawnSpec) -> io::Result<Box<dyn OwnedChild>> {
                Ok(Box::new(Cooperative {
                    elapsed: self.elapsed.clone(),
                    signalled: self.signalled.clone(),
                }))
            }
        }
        let elapsed = Arc::new(AtomicU64::new(0));
        let signalled = Arc::new(AtomicBool::new(false));
        let mut owner = OwnedProcesses::with_dependencies(
            StepClock(elapsed.clone()),
            FixtureSpawner {
                elapsed: elapsed.clone(),
                signalled: signalled.clone(),
            },
        );
        owner
            .spawn(ProcessKind::Worker, &SpawnSpec::packaged_worker("fixture"))
            .unwrap();
        assert!(owner.shutdown_with_grace(100, 50).stopped);
        assert_eq!(elapsed.load(Ordering::SeqCst), 30);
        assert!(!signalled.load(Ordering::SeqCst));
        assert!(owner
            .spawn(ProcessKind::Worker, &SpawnSpec::packaged_worker("fixture"))
            .is_err());
    }
    #[cfg(unix)]
    #[test]
    fn browser_dependency_owns_distinct_cdp_pipes() {
        use std::io::{Read, Write};
        let mut owner = OwnedProcesses::default();
        let mut parent = owner
            .spawn(ProcessKind::Worker, &SpawnSpec::packaged_worker("/bin/sh"))
            .unwrap();
        let parent_pipes = parent.take_pipes().unwrap();
        let spec = SpawnSpec::browser_pipe_root("/bin/sh", std::iter::empty::<&str>());
        let mut browser = owner
            .spawn_dependency(&parent, ProcessKind::Browser, &spec)
            .unwrap();
        let mut pipes = browser.take_pipes().unwrap();
        let mut cdp = pipes.browser_cdp.take().unwrap();
        pipes.stdin.write_all(b"cat <&3 >&4\n").unwrap();
        cdp.writer.write_all(b"owned-cdp").unwrap();
        cdp.writer.flush().unwrap();
        let mut reply = [0; 9];
        cdp.reader.read_exact(&mut reply).unwrap();
        assert_eq!(&reply, b"owned-cdp");
        drop(parent_pipes);
        assert!(owner.shutdown(100).stopped);
    }

    #[cfg(unix)]
    #[test]
    fn packaged_worker_has_separate_owned_driver_lane() {
        use std::io::{Read, Write};
        let mut owner = OwnedProcesses::default();
        let mut worker = owner
            .spawn(ProcessKind::Worker, &SpawnSpec::packaged_worker("/bin/sh"))
            .unwrap();
        let mut pipes = worker.take_pipes().unwrap();
        let mut driver = pipes.driver.take().unwrap();
        driver.write_all(b"driver").unwrap();
        driver.shutdown(std::net::Shutdown::Write).unwrap();
        pipes.stdin.write_all(b"cat <&5\n").unwrap();
        let mut reply = [0; 6];
        pipes.stdout.read_exact(&mut reply).unwrap();
        assert_eq!(&reply, b"driver");
        assert!(owner.shutdown(100).stopped);
    }

    #[cfg(unix)]
    #[test]
    fn packaged_worker_has_separate_private_control_channel() {
        use std::io::{Read, Write};
        let mut owner = OwnedProcesses::default();
        let mut handle = owner
            .spawn(ProcessKind::Worker, &SpawnSpec::packaged_worker("/bin/sh"))
            .unwrap();
        let mut pipes = handle.take_pipes().unwrap();
        let mut control = pipes.control.take().unwrap();
        control.write_all(b"broker").unwrap();
        control.shutdown(std::net::Shutdown::Write).unwrap();
        pipes.stdin.write_all(b"cat <&4\n").unwrap();
        let mut response = [0; 6];
        pipes.stdout.read_exact(&mut response).unwrap();
        assert_eq!(&response, b"broker");
        assert!(owner.shutdown(100).stopped);
    }
}
