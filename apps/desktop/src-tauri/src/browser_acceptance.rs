//! Opt-in macOS source integration: real sealed Chrome + Node + private CDP.
//! This module is absent from non-test builds. It never mints packaged/native
//! acceptance, modifies trust catalogs, navigates a service, or loads an account.
use crate::{
    bundle_inventory::{Entry, Inventory},
    native_broker::{DriverLane, NativeBroker, NativeProof, PreparedInference},
    supervisor::{DesktopSupervisor, Handle, ProcessKind, SpawnSpec, PROPOSED_SHUTDOWN_MS},
};
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, OpenOptions},
    io::{self, BufRead, BufReader, Read, Write},
    path::{Path, PathBuf},
    process::Command,
    sync::{mpsc, Arc, Mutex},
    time::{Duration, Instant},
};

const CORE_SHA256: &str = "829521b67ed7be90c7d6d6098ff39f2241562d47e4ad7ccab13710924a56001d";
const ARCHIVE_SHA256: &str = "987d2f8499c87d21676f78178550cfcbfb1bb53786821e2cff46c5a7e99b58be";
fn error(message: impl Into<String>) -> io::Error {
    io::Error::other(message.into())
}
fn hash(path: &Path) -> io::Result<String> {
    let mut file = fs::File::open(path)?;
    let mut digest = Sha256::new();
    let mut buffer = [0; 65536];
    loop {
        let size = file.read(&mut buffer)?;
        if size == 0 {
            break;
        }
        digest.update(&buffer[..size]);
    }
    Ok(format!("{:x}", digest.finalize()))
}
fn private_dir(path: &Path) -> io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    match fs::create_dir(path) {
        Ok(()) => fs::set_permissions(path, fs::Permissions::from_mode(0o700))?,
        Err(problem) if problem.kind() == io::ErrorKind::AlreadyExists => (),
        Err(problem) => return Err(problem),
    }
    let meta = fs::symlink_metadata(path)?;
    if !meta.is_dir() || meta.file_type().is_symlink() || meta.permissions().mode() & 0o077 != 0 {
        return Err(error("Private fixture directory required"));
    }
    Ok(())
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Fixture {
    schema_version: u32,
    project: PathBuf,
    resources: PathBuf,
    inventory: PathBuf,
    inventory_sha256: String,
    archive_sha256: String,
    python: PathBuf,
    dependencies: PathBuf,
    worker_script: PathBuf,
    output: PathBuf,
    run_root: PathBuf,
    scenario: String,
}
impl Fixture {
    fn load() -> io::Result<Self> {
        let path = std::env::var_os("THREADRELAY_BROWSER_NATIVE_FIXTURE")
            .ok_or_else(|| error("Explicit native browser fixture path required"))?;
        let value: Self = serde_json::from_slice(&fs::read(path)?)?;
        let project = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../..")
            .canonicalize()?;
        if value.schema_version != 1
            || value.project != project
            || value.archive_sha256 != ARCHIVE_SHA256
            || value.resources != project.join(".cache/native-browser/resources")
            || value.inventory
                != project.join(".cache/native-browser/evidence/resource-inventory.json")
            || value.python
                != project.join(".cache/build-inputs/install/python-3.12.16/bin/python3.12")
            || value.dependencies
                != project.join(".cache/venvs/worker-3.12.16/lib/python3.12/site-packages")
            || value.worker_script != project.join("scripts/verify-browser-worker.py")
            || !value
                .output
                .starts_with(project.join(".cache/native-browser/evidence"))
            || !value
                .run_root
                .starts_with(project.join(".cache/native-browser/runs"))
            || !matches!(
                value.scenario.as_str(),
                "normal_close"
                    | "browser_loss"
                    | "driver_loss"
                    | "worker_loss"
                    | "malformed_frame"
                    | "partial_spawn"
                    | "identity_drift"
            )
            || hash(&value.inventory)? != value.inventory_sha256
        {
            return Err(error("Native browser fixture identity/scope mismatch"));
        }
        for path in [
            &value.resources,
            &value.inventory,
            &value.worker_script,
            &value.run_root,
            &value.output,
        ] {
            if path
                .components()
                .any(|part| matches!(part, std::path::Component::ParentDir))
                || path
                    .symlink_metadata()
                    .is_ok_and(|meta| meta.file_type().is_symlink())
            {
                return Err(error("Symlink or parent fixture path rejected"));
            }
        }
        Ok(value)
    }
}

/// A test-only pin verifier for the actual guarded resources and same-manager
/// opaque handles. This is intentionally distinct from PackagedProof and absent
/// in production: source Python and the fixed live-feed navigation are untested.
struct BrowserFixtureProof {
    owner: Arc<Mutex<DesktopSupervisor>>,
    resources: PathBuf,
    profile: PathBuf,
    entries: Vec<Entry>,
}
impl BrowserFixtureProof {
    fn valid(&self) -> bool {
        Inventory::new(self.entries.clone()).is_ok_and(|inventory| {
            crate::verified_artifacts::verify_bundle(&self.resources, &inventory, &|| false).is_ok()
        }) && hash(
            &self
                .resources
                .join("native/playwright/package/lib/coreBundle.js"),
        )
        .ok()
        .as_deref()
            == Some(CORE_SHA256)
    }
}
impl NativeProof for BrowserFixtureProof {
    fn inference_ready(&self) -> bool {
        false
    }
    fn authorize_inference(&self, _: &mut PreparedInference, _: &dyn Fn() -> bool) -> bool {
        false
    }
    fn browser_ready(&self) -> bool {
        self.valid()
    }
    fn authorize_browser(
        &self,
        browser: &Path,
        driver: &Path,
        entry: &Path,
        profile: &Path,
        cancelled: &dyn Fn() -> bool,
    ) -> bool {
        !cancelled()
            && browser == self.resources.join("native/browser/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing")
            && driver == self.resources.join("native/playwright/node")
            && entry == self.resources.join("native/threadrelay/owned-driver.cjs")
            && profile == self.profile
            && self.valid()
    }
    fn driver_contained(&self, handle: &Handle) -> bool {
        self.owner.try_lock().ok().is_some_and(|mut owner| {
            owner.processes.is_live(handle).unwrap_or(false)
                && owner.processes.native_test_groups().is_ok_and(|groups| {
                    groups
                        .iter()
                        .filter(|(kind, _)| *kind == ProcessKind::BrowserDriver)
                        .count()
                        == 1
                })
        })
    }
    fn endpoint_owned(&self, _: &Handle, _: u16) -> bool {
        false
    }
    fn within_resource_limits(&self, _: &Handle) -> bool {
        false
    }
    fn inference_peer(&self, _: &Handle, _: &Handle, _: u16, _: u16) -> bool {
        false
    }
}

struct RetainedOwner {
    owner: Arc<Mutex<DesktopSupervisor>>,
    broker: Option<Arc<NativeBroker>>,
    settled: bool,
    cleanup_evidence: PathBuf,
}
impl Drop for RetainedOwner {
    fn drop(&mut self) {
        if self.settled {
            return;
        }
        if let Some(broker) = &self.broker {
            broker.revoke();
            broker.cleanup();
        }
        let report = self
            .owner
            .lock()
            .ok()
            .map(|mut owner| owner.close_generation(PROPOSED_SHUTDOWN_MS));
        let stopped = report.as_ref().is_some_and(|report| report.stopped);
        let record = json!({"purpose":"native-fixture-owner-cleanup","stopped":stopped,
            "report":report,"native_acceptance":"not_minted"});
        let _ = fs::write(
            &self.cleanup_evidence,
            serde_json::to_vec_pretty(&record).unwrap(),
        );
        if !stopped {
            std::mem::forget(self.owner.clone());
        }
    }
}
fn read_events(reader: Box<dyn Read + Send + Sync>) -> mpsc::Receiver<Value> {
    let (sender, receiver) = mpsc::sync_channel(16);
    std::thread::spawn(move || {
        for line in BufReader::new(reader).lines() {
            let Ok(line) = line else {
                break;
            };
            if line.len() > 65536 {
                break;
            }
            let Ok(value) = serde_json::from_str(&line) else {
                break;
            };
            if sender.try_send(value).is_err() {
                break;
            }
        }
    });
    receiver
}
fn event(receiver: &mpsc::Receiver<Value>, expected: &str, seconds: u64) -> io::Result<Value> {
    let value = receiver
        .recv_timeout(Duration::from_secs(seconds))
        .map_err(|_| error(format!("Fixture worker event unavailable: {expected}")))?;
    if value["event"] != expected {
        return Err(error(format!("Unexpected fixture event: {value}")));
    }
    Ok(value)
}
fn send(writer: &mut dyn Write, value: Value) -> io::Result<()> {
    serde_json::to_writer(&mut *writer, &value)?;
    writer.write_all(b"\n")?;
    writer.flush()
}
fn note(fixture: &Fixture, started: Instant, stage: &str, details: Value) -> io::Result<()> {
    let path = fixture.output.with_extension("progress.jsonl");
    let mut output = OpenOptions::new().create(true).append(true).open(path)?;
    serde_json::to_writer(
        &mut output,
        &json!({"stage":stage,"elapsed_ms":started.elapsed().as_millis(),"details":details}),
    )?;
    output.write_all(b"\n")
}
fn rendered_events(
    receiver: &mpsc::Receiver<Value>,
    fixture: &Fixture,
    started: Instant,
) -> io::Result<Value> {
    loop {
        let value = receiver
            .recv_timeout(Duration::from_secs(20))
            .map_err(|_| error("Bounded fixture operation result unavailable"))?;
        let name = value["event"].as_str().unwrap_or("").to_owned();
        note(fixture, started, &name, value.clone())?;
        match name.as_str() {
            "operation_timing" | "fixture_observed" => (),
            "fixtures_observed" => return Ok(value),
            _ => return Err(error(format!("Unexpected rendered fixture event: {value}"))),
        }
    }
}

fn groups(owner: &Arc<Mutex<DesktopSupervisor>>) -> io::Result<Vec<(ProcessKind, i32)>> {
    owner
        .lock()
        .map_err(|_| error("Owner lock unavailable"))?
        .processes
        .native_test_groups()
}
fn wait(
    owner: &Arc<Mutex<DesktopSupervisor>>,
    condition: impl Fn(&mut DesktopSupervisor) -> bool,
) -> io::Result<()> {
    let deadline = Instant::now() + Duration::from_secs(8);
    while Instant::now() < deadline {
        let satisfied = {
            let mut current = owner.lock().map_err(|_| error("Owner lock unavailable"))?;
            condition(&mut current)
        };
        if satisfied {
            return Ok(());
        }
        std::thread::sleep(Duration::from_millis(25));
    }
    Err(error("Bounded native observation did not settle"))
}

fn scoped_processes(root: &Path, owned: &[(ProcessKind, i32)]) -> io::Result<Value> {
    let output = Command::new("/bin/ps")
        .args(["-axo", "pid=,ppid=,pgid=,command="])
        .env_clear()
        .output()?;
    if !output.status.success() || output.stdout.len() > 4 * 1024 * 1024 {
        return Err(error("Bounded native process readback unavailable"));
    }
    let root = root
        .to_str()
        .ok_or_else(|| error("Invalid snapshot path"))?;
    let mut rows = Vec::new();
    for line in String::from_utf8_lossy(&output.stdout)
        .lines()
        .filter(|line| line.contains(root))
    {
        let mut words = line.split_whitespace();
        let pid = words
            .next()
            .and_then(|v| v.parse::<i32>().ok())
            .ok_or_else(|| error("PID readback unavailable"))?;
        let parent = words
            .next()
            .and_then(|v| v.parse::<i32>().ok())
            .ok_or_else(|| error("Parent readback unavailable"))?;
        let group = words
            .next()
            .and_then(|v| v.parse::<i32>().ok())
            .ok_or_else(|| error("Group readback unavailable"))?;
        if !owned.iter().any(|(_, leader)| *leader == group) {
            return Err(error(format!(
                "Pinned runtime process escaped registered groups: {pid}; owned={owned:?}; row={}",
                line.trim()
            )));
        }
        for forbidden in [
            "--no-sandbox",
            "--disable-web-security",
            "--ignore-certificate-errors",
            "--remote-debugging-port",
            "--headless",
        ] {
            if line.contains(forbidden) {
                return Err(error("Disallowed runtime launch option observed"));
            }
        }
        rows.push(json!({"pid":pid,"parent":parent,"group":group,"command":line.trim()}));
    }
    for kind in [ProcessKind::Browser, ProcessKind::BrowserDriver] {
        let leader = owned
            .iter()
            .find(|(entry, _)| *entry == kind)
            .ok_or_else(|| error("Registered browser/driver root missing"))?
            .1;
        if !rows.iter().any(|row| row["pid"] == leader) {
            return Err(error("Pinned executable root not observed"));
        }
    }
    rows.sort_by_key(|row| row["pid"].as_i64());
    Ok(Value::Array(rows))
}
fn observe_processes(root: &Path, owner: &Arc<Mutex<DesktopSupervisor>>) -> io::Result<Value> {
    let deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < deadline {
        let before = groups(owner)?;
        let first = scoped_processes(root, &before)?;
        std::thread::sleep(Duration::from_millis(50));
        let second = scoped_processes(root, &before)?;
        if before == groups(owner)? && first == second {
            return Ok(first);
        }
    }
    Err(error("Stable scoped process observation unavailable"))
}
fn mutability_denied(resources: &Path) -> io::Result<Value> {
    let executable = resources.join(
        "native/browser/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
    );
    let write_denied = OpenOptions::new().write(true).open(&executable).is_err();
    let rename_denied = fs::rename(
        &executable,
        executable.with_file_name("unexpected-mutation-probe"),
    )
    .is_err();
    if !write_denied || !rename_denied {
        return Err(error("Real snapshot permitted write/rename"));
    }
    Ok(json!({"write_open_denied":write_denied,"rename_denied":rename_denied}))
}

fn observed_private_socket(fixture: &Fixture) -> io::Result<Value> {
    use std::os::unix::fs::{FileTypeExt, MetadataExt};
    let profile = fixture.run_root.join("browser-profiles/threads");
    let socket = fs::read_link(profile.join("SingletonSocket"))?;
    let temporary = fixture.run_root.join("native-runtime/browser/tmp");
    let parent = socket
        .parent()
        .ok_or_else(|| error("Singleton socket parent missing"))?;
    if !socket.is_absolute()
        || !socket.starts_with(&temporary)
        || parent.parent() != Some(temporary.as_path())
        || socket.file_name().and_then(|name| name.to_str()) != Some("SingletonSocket")
        || socket
            .components()
            .any(|part| matches!(part, std::path::Component::ParentDir))
        || std::os::unix::ffi::OsStrExt::as_bytes(socket.as_os_str()).len() > 253
    {
        return Err(error(
            "Browser singleton socket escaped fixed project temporary directory",
        ));
    }
    let directory = fs::symlink_metadata(parent)?;
    let metadata = fs::symlink_metadata(&socket)?;
    if !directory.is_dir()
        || directory.file_type().is_symlink()
        || directory.mode() & 0o077 != 0
        || directory.uid() != unsafe { libc::geteuid() }
        || !metadata.file_type().is_socket()
        || metadata.uid() != unsafe { libc::geteuid() }
    {
        return Err(error(
            "Browser singleton socket ownership/privacy unavailable",
        ));
    }
    Ok(
        json!({"path":socket,"private_project_temp":true,"socket_inode":metadata.ino(),
        "socket_device":metadata.dev(),"owner_uid":metadata.uid(),"directory_inode":directory.ino()}),
    )
}

fn execute(fixture: &Fixture) -> io::Result<Value> {
    let started = Instant::now();
    note(
        fixture,
        started,
        "begin",
        json!({"scenario":fixture.scenario}),
    )?;
    private_dir(&fixture.run_root)?;
    let owner = Arc::new(Mutex::new(DesktopSupervisor::acquire(&fixture.run_root)?));
    let mut retained = RetainedOwner {
        owner: owner.clone(),
        broker: None,
        settled: false,
        cleanup_evidence: fixture.output.with_extension("owner-cleanup.json"),
    };
    let entries: Vec<Entry> = serde_json::from_slice(&fs::read(&fixture.inventory)?)?;
    let mut inventory = Inventory::new(entries.clone())?;
    if fixture.scenario == "identity_drift" {
        let entry = inventory
            .entries
            .get_mut("native/playwright/package/lib/coreBundle.js")
            .ok_or_else(|| error("Complete core pin missing"))?;
        let Entry::File { sha256, .. } = entry else {
            return Err(error("Invalid core pin"));
        };
        *sha256 = "0".repeat(64);
        let denied = crate::verified_artifacts::native_test_resources(
            &fixture.resources,
            &fixture.run_root,
            &inventory,
            &|| false,
        )
        .is_err();
        if !denied || !groups(&owner)?.is_empty() {
            return Err(error("Artifact drift was not denied before execution"));
        }
        retained.settled = owner
            .lock()
            .unwrap()
            .close_generation(PROPOSED_SHUTDOWN_MS)
            .stopped;
        if !retained.settled {
            return Err(error("Drift denial cleanup unconfirmed"));
        }
        return Ok(
            json!({"scenario":"identity_drift","result":"observed","drift_denied_before_execution":true,"native_browser_launched":false}),
        );
    }
    let (guarded, guard) = crate::verified_artifacts::native_test_resources(
        &fixture.resources,
        &fixture.run_root,
        &inventory,
        &|| false,
    )?;
    note(fixture, started, "real_native_guard_prepared", json!({}))?;
    let resources = guarded.root().to_owned();
    owner.lock().unwrap().retain_artifacts(guard);
    let mutations = mutability_denied(&resources)?;
    let mut spec = SpawnSpec::packaged_worker(&fixture.python);
    spec.args = vec![
        "-I".into(),
        "-S".into(),
        "-B".into(),
        fixture.worker_script.as_os_str().into(),
        fixture.project.join("worker/src").into_os_string(),
        fixture.dependencies.as_os_str().into(),
        fixture.project.as_os_str().into(),
    ];
    let runtime = crate::runtime_environment::PrivateRuntime::prepare(
        &fixture.run_root,
        crate::runtime_environment::RuntimeKind::Worker,
    )?;
    let spec = spec.with_private_runtime(&runtime);
    let mut worker = owner
        .lock()
        .unwrap()
        .processes
        .spawn(ProcessKind::Worker, &spec)?;
    let mut pipes = worker
        .take_pipes()
        .ok_or_else(|| error("Private worker lanes unavailable"))?;
    let worker = Arc::new(worker);
    let lane = DriverLane::from_socket(
        pipes
            .driver
            .take()
            .ok_or_else(|| error("Private FD5 lane unavailable"))?,
    )?;
    let receiver = read_events(pipes.stdout);
    let stderr_path = fixture.output.with_extension("worker-stderr.txt");
    let mut errors = pipes.stderr;
    let mut error_output = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(stderr_path)?;
    std::thread::spawn(move || {
        let _ = io::copy(&mut errors.by_ref().take(65536), &mut error_output);
    });
    let ready = event(&receiver, "fixture_worker_ready", 10)?;
    note(fixture, started, "worker_ready", ready.clone())?;
    let worker_leader = groups(&owner)?
        .into_iter()
        .find(|(kind, _)| *kind == ProcessKind::Worker)
        .ok_or_else(|| error("Retained worker identity unavailable"))?
        .1;
    if ready["pid"] != worker_leader || ready["python"] != "3.12.16" {
        return Err(error("Worker native identity/version mismatch"));
    }
    let proof = Arc::new(BrowserFixtureProof {
        owner: owner.clone(),
        resources: resources.clone(),
        profile: fixture.run_root.join("browser-profiles/threads"),
        entries,
    });
    let broker = Arc::new(NativeBroker::new(
        owner.clone(),
        worker.clone(),
        &fixture.run_root,
        &resources,
        proof,
        Some(lane),
    )?);
    retained.broker = Some(broker.clone());
    if fixture.scenario == "partial_spawn" {
        let obstruction = SpawnSpec::browser_pipe_root(
            resources.join("native/playwright/node"),
            ["-e", "setInterval(()=>{},1000)"],
        );
        let obstruction = owner.lock().unwrap().processes.spawn_dependency(
            &worker,
            ProcessKind::Browser,
            &obstruction,
        )?;
        let denied = broker.open_browser().is_err();
        if !denied {
            return Err(error("Partial spawn unexpectedly activated"));
        }
        wait(&owner, |owner| {
            owner.processes.native_test_groups().is_ok_and(|groups| {
                !groups
                    .iter()
                    .any(|(kind, _)| *kind == ProcessKind::BrowserDriver)
            })
        })?;
        let stopped = owner.lock().unwrap().close_generation(PROPOSED_SHUTDOWN_MS);
        retained.settled = stopped.stopped;
        drop(obstruction);
        if !stopped.stopped {
            return Err(error("Partial spawn cleanup unconfirmed"));
        }
        return Ok(
            json!({"scenario":"partial_spawn","result":"observed","partial_spawn_denied":denied,
            "obstruction":"test-only benign owned Node root occupies browser slot","cleanup":stopped,"full_browser_launched":false}),
        );
    }
    let opened = broker.open_browser()?;
    note(fixture, started, "native_browser_opened", json!({}))?;
    let lease = opened["lease"]
        .as_str()
        .ok_or_else(|| error("Opaque browser lease unavailable"))?
        .to_owned();
    if opened["source_session_id"] != lease || broker.poll_browser("unrelated-or-expired-lease") {
        return Err(error("Browser lease/source binding mismatch"));
    }
    if fixture.scenario == "malformed_frame" {
        send(&mut pipes.stdin, json!({"command":"malformed"}))?;
        let rejected = event(&receiver, "malformed_rejected", 12)?;
        if rejected["driver_eof"] != true {
            return Err(error("Malformed driver frame stayed connected"));
        }
        wait(&owner, |owner| {
            owner
                .processes
                .native_test_groups()
                .is_ok_and(|groups| groups.is_empty())
        })?;
        let stopped = owner.lock().unwrap().close_generation(PROPOSED_SHUTDOWN_MS);
        retained.settled = stopped.stopped;
        if !stopped.stopped {
            return Err(error("Malformed frame cleanup unconfirmed"));
        }
        return Ok(
            json!({"scenario":"malformed_frame","result":"observed","rejected":rejected,"cleanup":stopped,"snapshot_mutation":mutations}),
        );
    }
    send(&mut pipes.stdin, json!({"command":"start"}))?;
    let browser_observed = event(&receiver, "browser_observed", 25)?;
    note(
        fixture,
        started,
        "browser_observed",
        browser_observed.clone(),
    )?;
    wait(&owner, |owner| {
        owner.processes.native_test_browser_pipe().unwrap_or(false)
    })?;
    let private_socket = observed_private_socket(fixture)?;
    note(
        fixture,
        started,
        "private_socket_observed",
        private_socket.clone(),
    )?;
    let processes = observe_processes(&resources, &owner)?;
    let initial_groups = groups(&owner)?;
    if initial_groups.len() != 3 || broker.open_browser().is_ok() || !broker.poll_browser(&lease) {
        return Err(error("Exclusive same-manager browser ownership failed"));
    }
    note(
        fixture,
        started,
        "host_proof_settled_before_render",
        json!({"owned_processes":processes}),
    )?;
    send(&mut pipes.stdin, json!({"command":"render"}))?;
    let observations = rendered_events(&receiver, fixture, started)?;
    let screenshot = if fixture.scenario == "normal_close" {
        let path = fixture.output.with_extension("png");
        send(
            &mut pipes.stdin,
            json!({"command":"screenshot","path":path}),
        )?;
        Some(event(&receiver, "screenshot", 20)?)
    } else {
        None
    };
    let start = Instant::now();
    let action = match fixture.scenario.as_str() {
        "normal_close" => {
            let report = broker.close_browser(&lease)?;
            if !report.stopped || !report.remaining.is_empty() {
                return Err(error("Normal browser close unconfirmed"));
            }
            json!({"action":"broker.close_browser","report":report})
        }
        "browser_loss" | "driver_loss" | "worker_loss" => {
            let kind = match fixture.scenario.as_str() {
                "browser_loss" => ProcessKind::Browser,
                "driver_loss" => ProcessKind::BrowserDriver,
                _ => ProcessKind::Worker,
            };
            owner.lock().unwrap().processes.native_test_kill(kind)?;
            json!({"action":"kill retained owned kind","kind":format!("{kind:?}")})
        }
        _ => return Err(error("Unexpected native scenario")),
    };
    wait(&owner, |owner| {
        owner.processes.native_test_groups().is_ok_and(|groups| {
            !groups
                .iter()
                .any(|(kind, _)| matches!(kind, ProcessKind::Browser | ProcessKind::BrowserDriver))
        })
    })?;
    let dependency_cleanup_ms = start.elapsed().as_millis();
    if dependency_cleanup_ms > u128::from(PROPOSED_SHUTDOWN_MS) {
        return Err(error(
            "Owned browser dependency cleanup exceeded proposed shutdown bound",
        ));
    }
    let disconnected = if fixture.scenario != "worker_loss" {
        send(&mut pipes.stdin, json!({"command":"probe"}))?;
        let probe = event(&receiver, "probe", 8)?;
        if probe["connected"] != false {
            return Err(error("Lost browser/driver remained usable"));
        }
        send(&mut pipes.stdin, json!({"command":"exit"}))?;
        let _ = event(&receiver, "fixture_worker_exiting", 5)?;
        Some(probe)
    } else {
        None
    };
    if broker.poll_browser(&lease) || broker.open_browser().is_ok() {
        return Err(error("Stale generation restarted/adopted browser"));
    }
    let stopped = owner.lock().unwrap().close_generation(PROPOSED_SHUTDOWN_MS);
    retained.settled = stopped.stopped;
    if !stopped.stopped || !groups(&owner)?.is_empty() {
        return Err(error("Owned generation cleanup unconfirmed"));
    }
    Ok(
        json!({"scenario":fixture.scenario,"result":"observed","worker":ready,"browser":browser_observed,"fixtures":observations,
        "owned_processes":processes,"native_fd3_fd4_no_tcp_listener":true,"private_socket":private_socket,"snapshot_mutation":mutations,
        "exclusive_lease":true,"stale_lease_denied":true,"action":action,"disconnected":disconnected,
        "cleanup":stopped,"dependency_cleanup_elapsed_ms":dependency_cleanup_ms,"cleanup_elapsed_ms":start.elapsed().as_millis(),"screenshot":screenshot,
        "scope_limitations":["test-only source worker; frozen worker/package acceptance not exercised",
            "production fixed live-feed navigation not exercised","no accounts, credentials, model, or send",
            "test-only mock Keychain/basic password store flags if configured by broker",
            "official ad-hoc bundle signature warning retained; distribution review not performed"]}),
    )
}

#[test]
#[ignore = "Explicit coordinated visible native browser fixture only; no production capability"]
fn visible_owned_browser_pipeline() {
    let fixture = Fixture::load().expect("Read authorized local native fixture");
    let result = execute(&fixture);
    let record = match &result {
        Ok(value) => {
            json!({"schema_version":1,"purpose":"browser-development-integration-observation",
            "result":"observed","native_acceptance":"not_minted","observation":value})
        }
        Err(problem) => {
            json!({"schema_version":1,"purpose":"browser-development-integration-observation",
            "result":"failed","native_acceptance":"not_minted","scenario":fixture.scenario,"error":problem.to_string()})
        }
    };
    fs::write(&fixture.output, serde_json::to_vec_pretty(&record).unwrap())
        .expect("Retain private native observation");
    if let Err(problem) = result {
        panic!("Native browser fixture failed: {problem}");
    }
    println!(
        "Native browser source fixture observed: {}",
        fixture.scenario
    );
}
