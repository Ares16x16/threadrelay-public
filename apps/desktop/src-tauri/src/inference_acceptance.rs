//! Opt-in source-only real model integration, absent from production builds.
//! No accounts, held-out quality claims, catalog updates, or capability minting.
use crate::{
    bundle_inventory::{Entry, Inventory},
    native_broker::{NativeBroker, NativeProof, PreparedInference},
    supervisor::{DesktopSupervisor, Handle, ProcessKind, SpawnSpec, PROPOSED_SHUTDOWN_MS},
};
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::{self, BufRead, BufReader, Read, Write},
    path::{Path, PathBuf},
    sync::{mpsc, Arc, Mutex},
    time::{Duration, Instant},
};
const MODEL_SHA: &str = "03b90d043897e0de78983e36702f182d19fb6b2321586707841533ea7d8c407d";
const MODEL_SIZE: u64 = 2708808736;
const MODEL_NAME: &str = "h2o-lightning-4b-Q4_K_M.gguf";
const COMMIT: &str = "8ca4060b7e30e76baaa5670ee1ec1b760b7f8552";
fn error(s: impl Into<String>) -> io::Error {
    io::Error::other(s.into())
}
fn hash(path: &Path) -> io::Result<String> {
    let mut f = fs::File::open(path)?;
    let mut d = Sha256::new();
    let mut b = [0; 65536];
    loop {
        let n = f.read(&mut b)?;
        if n == 0 {
            break;
        }
        d.update(&b[..n]);
    }
    Ok(format!("{:x}", d.finalize()))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Fixture {
    project: PathBuf,
    resources: PathBuf,
    inventory: PathBuf,
    inventory_sha256: String,
    python: PathBuf,
    dependencies: PathBuf,
    worker_script: PathBuf,
    run_root: PathBuf,
    output: PathBuf,
    scenario: String,
}
impl Fixture {
    fn load() -> io::Result<Self> {
        let path = std::env::var_os("THREADRELAY_INFERENCE_NATIVE_FIXTURE")
            .ok_or_else(|| error("Explicit fixture required"))?;
        let v: Self = serde_json::from_slice(&fs::read(path)?)?;
        let project = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../..")
            .canonicalize()?;
        if v.project != project
            || v.resources != project.join(".cache/llama-native/resources")
            || v.inventory
                != project.join(".cache/llama-native/evidence/native-resource-inventory.json")
            || v.python != project.join(".cache/build-inputs/install/python-3.12.16/bin/python3.12")
            || v.dependencies
                != project.join(".cache/venvs/worker-3.12.16/lib/python3.12/site-packages")
            || v.worker_script != project.join("scripts/verify-inference-worker.py")
            || !v
                .run_root
                .starts_with(project.join(".cache/llama-native/runs"))
            || !v
                .output
                .starts_with(project.join(".cache/llama-native/evidence"))
            || !matches!(
                v.scenario.as_str(),
                "normal_close" | "runtime_loss" | "worker_loss" | "host_loss"
            )
            || hash(&v.inventory)? != v.inventory_sha256
        {
            return Err(error("Fixed fixture identity mismatch"));
        }
        Ok(v)
    }
}
struct Proof {
    owner: Arc<Mutex<DesktopSupervisor>>,
    worker: Arc<Handle>,
    resources: PathBuf,
    root: PathBuf,
    entries: Vec<Entry>,
    rss: Mutex<Vec<Value>>,
    peers: Mutex<u64>,
    snapshot: Mutex<Value>,
}
impl NativeProof for Proof {
    fn inference_ready(&self) -> bool {
        true
    }
    fn authorize_inference(&self, p: &mut PreparedInference, c: &dyn Fn() -> bool) -> bool {
        eprintln!("inference fixture: authorize begin");
        if c()
            || p.model_sha256 != MODEL_SHA
            || p.model_size != MODEL_SIZE
            || p.runtime_commit != COMMIT
            || p.model_path != self.root.join("models").join(MODEL_NAME)
        {
            return false;
        }
        let Ok(inventory) = Inventory::new(self.entries.clone()) else {
            return false;
        };
        if crate::verified_artifacts::verify_bundle(&self.resources, &inventory, c).is_err() {
            return false;
        }
        let Ok(owner) = self.owner.lock() else {
            return false;
        };
        let known = owner.artifact_roots();
        drop(owner);
        let mut guard = match crate::verified_artifacts::prepare_model(
            &p.model_path,
            &self.root,
            MODEL_SHA,
            MODEL_SIZE,
            c,
            &known,
        ) {
            Ok(guard) => guard,
            Err(e) => {
                eprintln!("inference fixture: model guard denied: {e}");
                return false;
            }
        };
        eprintln!("inference fixture: model guard ready");
        let path = guard.root().join(MODEL_NAME);
        let Ok(mut owner) = self.owner.lock() else {
            return false;
        };
        if c() || !owner.processes.is_live(&self.worker).unwrap_or(false) {
            let _ = guard.cleanup();
            return false;
        }
        owner.retain_artifacts(guard);
        let write_denied = fs::OpenOptions::new().write(true).open(&path).is_err();
        let rename_denied = fs::rename(&path, path.with_file_name("mutation-probe.gguf")).is_err();
        *self.snapshot.lock().unwrap() = json!({"loaded_model_path":path,"write_open_denied":write_denied,"rename_denied":rename_denied});
        if !write_denied || !rename_denied {
            return false;
        }
        p.model_path = path;
        eprintln!("inference fixture: authorize complete");
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
    fn driver_contained(&self, _: &Handle) -> bool {
        false
    }
    fn endpoint_owned(&self, h: &Handle, p: u16) -> bool {
        let result = self
            .owner
            .lock()
            .ok()
            .and_then(|mut o| o.processes.inference_listener(h, p).ok());
        eprintln!("inference fixture: listener {result:?}");
        result == Some(true)
    }
    fn within_resource_limits(&self, h: &Handle) -> bool {
        let observation = self
            .owner
            .lock()
            .ok()
            .and_then(|mut o| o.processes.evaluation_memory(&self.worker, Some(h)).ok());
        if let Some((worker, runtime)) = observation {
            self.rss
                .lock()
                .unwrap()
                .push(json!({"worker_bytes":worker,"runtime_bytes":runtime}));
            runtime <= 8 * 1024 * 1024 * 1024
        } else {
            false
        }
    }
    fn inference_peer(&self, w: &Handle, r: &Handle, c: u16, s: u16) -> bool {
        let ok = self.owner.lock().ok().is_some_and(|mut o| {
            o.processes
                .verify_inference_peer(w, r, c, s)
                .unwrap_or(false)
        });
        if ok {
            *self.peers.lock().unwrap() += 1;
        }
        ok
    }
}
struct DenyVault;
impl crate::credentials::CredentialVault for DenyVault {
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
struct Retained {
    owner: Arc<Mutex<DesktopSupervisor>>,
    broker: Option<Arc<NativeBroker>>,
    settled: bool,
    output: PathBuf,
}
impl Drop for Retained {
    fn drop(&mut self) {
        if self.settled {
            return;
        }
        if let Some(b) = &self.broker {
            b.revoke();
            b.cleanup();
        }
        let report = self
            .owner
            .lock()
            .ok()
            .map(|mut o| o.close_generation(PROPOSED_SHUTDOWN_MS));
        let stopped = report.as_ref().is_some_and(|r| r.stopped);
        let _ = fs::write(
            self.output.with_extension("cleanup.json"),
            serde_json::to_vec_pretty(&json!({"stopped":stopped,"report":report})).unwrap(),
        );
        if !stopped {
            std::mem::forget(self.owner.clone());
        }
    }
}
fn send(w: &mut dyn Write, v: Value) -> io::Result<()> {
    serde_json::to_writer(&mut *w, &v)?;
    w.write_all(b"\n")?;
    w.flush()
}
fn event(r: &mpsc::Receiver<Value>, expected: &str) -> io::Result<Value> {
    let v = r
        .recv_timeout(Duration::from_secs(35))
        .map_err(|_| error(format!("Missing event {expected}")))?;
    if v["event"] != expected {
        return Err(error(format!("Unexpected event {v}")));
    }
    Ok(v)
}
fn native_identity(pid: i32) -> io::Result<Value> {
    let mut info = std::mem::MaybeUninit::<libc::proc_bsdinfo>::uninit();
    let size = std::mem::size_of::<libc::proc_bsdinfo>();
    let observed = unsafe {
        libc::proc_pidinfo(
            pid,
            libc::PROC_PIDTBSDINFO,
            0,
            info.as_mut_ptr().cast(),
            size as i32,
        )
    };
    if observed != size as i32 {
        return Err(error("Exact native PID identity unavailable"));
    }
    let info = unsafe { info.assume_init() };
    if info.pbi_pid != pid as u32 || info.pbi_uid != unsafe { libc::geteuid() } {
        return Err(error("Native PID identity ownership mismatch"));
    }
    Ok(json!({"pid":pid,"ppid":info.pbi_ppid,"pgid":info.pbi_pgid,
        "start_sec":info.pbi_start_tvsec,"start_usec":info.pbi_start_tvusec}))
}
fn native_group_identities(groups: &[(ProcessKind, i32)]) -> io::Result<Vec<Value>> {
    let mut result = Vec::new();
    for (kind, leader) in groups {
        let mut pids = [0i32; 1024];
        let n = unsafe {
            libc::proc_listpids(
                2,
                *leader as u32,
                pids.as_mut_ptr().cast(),
                std::mem::size_of_val(&pids) as i32,
            )
        };
        if n <= 0 || n as usize >= std::mem::size_of_val(&pids) || n % 4 != 0 {
            return Err(error("Complete retained native group unavailable"));
        }
        let mut members = Vec::new();
        for pid in pids[..n as usize / 4].iter().filter(|pid| **pid > 0) {
            let identity = native_identity(*pid)?;
            if identity["pgid"] != *leader {
                return Err(error("Native group identity changed"));
            }
            members.push(identity);
        }
        members.sort_by_key(|value| value["pid"].as_i64());
        if !members.iter().any(|value| value["pid"] == *leader) {
            return Err(error("Retained group leader missing"));
        }
        result.push(json!({"kind":format!("{kind:?}"),"leader":leader,"members":members}));
    }
    Ok(result)
}
fn execute(f: &Fixture) -> io::Result<Value> {
    let start = Instant::now();
    let owner = Arc::new(Mutex::new(DesktopSupervisor::acquire(&f.run_root)?));
    let mut retained = Retained {
        owner: owner.clone(),
        broker: None,
        settled: false,
        output: f.output.clone(),
    };
    let entries: Vec<Entry> = serde_json::from_slice(&fs::read(&f.inventory)?)?;
    let inv = Inventory::new(entries.clone())?;
    let (guarded, guard) =
        crate::verified_artifacts::native_test_resources(&f.resources, &f.run_root, &inv, &|| {
            false
        })?;
    let resources = guarded.root().to_owned();
    owner.lock().unwrap().retain_artifacts(guard);
    let mut spec = SpawnSpec::packaged_worker(&f.python);
    spec.args = vec![
        "-I".into(),
        "-S".into(),
        "-B".into(),
        f.worker_script.as_os_str().into(),
        f.project.join("worker/src").into_os_string(),
        f.dependencies.as_os_str().into(),
        f.project.as_os_str().into(),
        f.run_root.as_os_str().into(),
    ];
    let env = crate::runtime_environment::PrivateRuntime::prepare(
        &f.run_root,
        crate::runtime_environment::RuntimeKind::Worker,
    )?;
    let spec = spec.with_private_runtime(&env);
    let mut worker = owner
        .lock()
        .unwrap()
        .processes
        .spawn(ProcessKind::Worker, &spec)?;
    let mut pipes = worker
        .take_pipes()
        .ok_or_else(|| error("Worker pipes unavailable"))?;
    let worker = Arc::new(worker);
    let (tx, rx) = mpsc::sync_channel(16);
    std::thread::spawn(move || {
        for line in BufReader::new(pipes.stdout).lines() {
            let Ok(line) = line else { break };
            if line.len() > 65536 {
                break;
            }
            let Ok(v) = serde_json::from_str(&line) else {
                break;
            };
            if tx.try_send(v).is_err() {
                break;
            }
        }
    });
    let mut stderr = pipes.stderr;
    let output = f.output.with_extension("worker-stderr.txt");
    std::thread::spawn(move || {
        if let Ok(mut file) = fs::File::create(output) {
            let _ = io::copy(&mut stderr.by_ref().take(65536), &mut file);
        }
    });
    let ready = event(&rx, "worker_ready")?;
    let proof = Arc::new(Proof {
        owner: owner.clone(),
        worker: worker.clone(),
        resources: resources.clone(),
        root: f.run_root.clone(),
        entries,
        rss: Mutex::new(vec![]),
        peers: Mutex::new(0),
        snapshot: Mutex::new(Value::Null),
    });
    let broker = Arc::new(NativeBroker::new(
        owner.clone(),
        worker.clone(),
        &f.run_root,
        &resources,
        proof.clone(),
        None,
    )?);
    retained.broker = Some(broker.clone());
    let control = crate::host_control::start(
        pipes
            .control
            .take()
            .ok_or_else(|| error("Host control missing"))?,
        1,
        Arc::new(DenyVault),
        Some(broker.clone()),
    )?;
    send(&mut pipes.stdin, json!({"command":"start"}))?;
    let runtime = event(&rx, "runtime_ready")?;
    send(&mut pipes.stdin, json!({"command":"classify"}))?;
    let classified = event(&rx, "classified")?;
    let groups = owner.lock().unwrap().processes.native_test_groups()?;
    if groups
        .iter()
        .filter(|(k, _)| *k == ProcessKind::Worker)
        .count()
        != 1
        || groups
            .iter()
            .filter(|(k, _)| *k == ProcessKind::Inference)
            .count()
            != 1
    {
        return Err(error("Exclusive native groups unavailable"));
    }
    if f.scenario == "host_loss" {
        let before = native_group_identities(&groups)?;
        std::thread::sleep(Duration::from_millis(50));
        let after = native_group_identities(&groups)?;
        if before != after {
            return Err(error("Parent-loss native identities did not settle"));
        }
        let worker_leader = groups
            .iter()
            .find(|(kind, _)| *kind == ProcessKind::Worker)
            .unwrap()
            .1;
        if ready["pid"] != worker_leader
            || runtime["owned_port"]
                .as_u64()
                .is_none_or(|port| port == 0 || port > 65535)
        {
            return Err(error("Parent-loss worker/listener binding unavailable"));
        }
        let record = json!({"scenario":"host_loss","phase":"ready_for_owned_parent_sigkill",
            "host":native_identity(std::process::id() as i32)?,"groups":before,
            "runtime":runtime,"classification":classified,"guarded_resources":resources,
            "model_snapshot":*proof.snapshot.lock().unwrap(),"connected_peers_verified":*proof.peers.lock().unwrap(),
            "rss_observations":*proof.rss.lock().unwrap(),"native_acceptance":"not_minted",
            "artifact_cleanup":"must_remain_pending_if_host_is_killed"});
        let temporary = f.output.with_extension("host-loss-ready.new");
        fs::write(&temporary, serde_json::to_vec_pretty(&record)?)?;
        fs::rename(temporary, f.output.with_extension("host-loss-ready.json"))?;
        // The outer runner kills only its retained Popen host. If it does not,
        // normal unwinding still owns and cleans the registered generation.
        std::thread::sleep(Duration::from_secs(60));
        return Err(error(
            "Outer parent-loss runner did not terminate the retained host",
        ));
    }
    let cleanup = Instant::now();
    let loss = if f.scenario == "normal_close" {
        send(&mut pipes.stdin, json!({"command":"close"}))?;
        let v = event(&rx, "runtime_closed")?;
        if v["report"] != json!({"stopped":true,"remaining":[]}) {
            return Err(error("Runtime close unconfirmed"));
        }
        Some(v)
    } else {
        owner
            .lock()
            .unwrap()
            .processes
            .native_test_kill(if f.scenario == "runtime_loss" {
                ProcessKind::Inference
            } else {
                ProcessKind::Worker
            })?;
        if f.scenario == "runtime_loss" {
            send(&mut pipes.stdin, json!({"command":"probe_loss"}))?;
            let v = event(&rx, "loss_observed")?;
            if v["abstained"] != true {
                return Err(error("Runtime loss did not abstain"));
            }
            Some(v)
        } else {
            None
        }
    };
    if f.scenario != "worker_loss" {
        send(&mut pipes.stdin, json!({"command":"exit"}))?;
        let _ = event(&rx, "worker_exiting")?;
    }
    if f.scenario == "worker_loss" {
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            let alive = owner
                .lock()
                .unwrap()
                .processes
                .native_test_groups()?
                .iter()
                .any(|(k, _)| *k == ProcessKind::Inference);
            if !alive {
                break;
            }
            if Instant::now() >= deadline {
                return Err(error("Worker death did not revoke inference within bound"));
            }
            std::thread::sleep(Duration::from_millis(25));
        }
    }
    broker.revoke();
    control.revoke();
    let report = owner.lock().unwrap().close_generation(PROPOSED_SHUTDOWN_MS);
    retained.settled = report.stopped;
    if !report.stopped
        || !owner
            .lock()
            .unwrap()
            .processes
            .native_test_groups()?
            .is_empty()
    {
        return Err(error("Owned cleanup unconfirmed"));
    }
    if *proof.peers.lock().unwrap() == 0 {
        return Err(error("No connected peer was verified"));
    }
    Ok(
        json!({"scenario":f.scenario,"worker":ready,"runtime":runtime,"classification":classified,"owned_groups":groups.iter().map(|(kind,pid)|json!({"kind":format!("{kind:?}"),"pid":pid})).collect::<Vec<_>>(),"connected_peers_verified":*proof.peers.lock().unwrap(),"rss_observations":*proof.rss.lock().unwrap(),"loss_or_close":loss,"cleanup":report,"cleanup_elapsed_ms":cleanup.elapsed().as_millis(),"elapsed_ms":start.elapsed().as_millis(),"guarded_resources":resources,"model_snapshot":*proof.snapshot.lock().unwrap(),"scope":"source no-account native integration; no held-out quality or production acceptance"}),
    )
}
#[test]
#[ignore = "Explicit no-account pinned real model fixture; no production capability"]
fn owned_local_decisions() {
    let fixture = Fixture::load().expect("Read fixed native inference fixture");
    let result = execute(&fixture);
    let record = match &result {
        Ok(v) => json!({"result":"observed","native_acceptance":"not_minted","observation":v}),
        Err(e) => {
            json!({"result":"failed","native_acceptance":"not_minted","scenario":fixture.scenario,"error":e.to_string()})
        }
    };
    fs::write(&fixture.output, serde_json::to_vec_pretty(&record).unwrap())
        .expect("Retain native inference observation");
    if let Err(e) = result {
        panic!("Native inference failed: {e}")
    }
}
