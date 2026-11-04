//! Console-only packaged evaluation owner. No collection/auth/send authority.
use crate::supervisor::{DesktopSupervisor, ProcessKind, SpawnSpec, PROPOSED_SHUTDOWN_MS};
use std::{
    io::{self, Write},
    path::{Path, PathBuf},
    sync::{mpsc, Arc, Mutex},
    time::Duration,
};

pub struct EvaluationArgs {
    dataset: PathBuf,
    output: PathBuf,
    quantization: String,
}
pub fn parse_args(args: &[String]) -> Result<EvaluationArgs, &'static str> {
    let mut dataset = None;
    let mut output = None;
    let mut quantization = None;
    let mut iter = args.iter();
    while let Some(name) = iter.next() {
        let value = iter.next().ok_or("Invalid evaluation arguments")?;
        match name.as_str() {
            "--dataset" if dataset.is_none() => dataset = Some(PathBuf::from(value)),
            "--output" if output.is_none() => output = Some(PathBuf::from(value)),
            "--quantization"
                if quantization.is_none() && matches!(value.as_str(), "Q4_K_M" | "Q8_0") =>
            {
                quantization = Some(value.clone())
            }
            _ => return Err("Invalid evaluation arguments"),
        }
    }
    let absolute = |path: Option<PathBuf>| -> Result<PathBuf, &'static str> {
        let path = path.ok_or("Invalid evaluation arguments")?;
        let path = if path.is_absolute() {
            path
        } else {
            std::env::current_dir()
                .map_err(|_| "Invalid evaluation arguments")?
                .join(path)
        };
        let text = path.to_str().ok_or("Invalid evaluation arguments")?;
        if text.is_empty() || text.len() > 4096 || text.contains('\0') {
            return Err("Invalid evaluation arguments");
        }
        Ok(path)
    };
    let dataset = absolute(dataset)?;
    let output = absolute(output)?;
    if dataset == output {
        return Err("Invalid evaluation arguments");
    }
    Ok(EvaluationArgs {
        dataset,
        output,
        quantization: quantization.unwrap_or_else(|| "Q4_K_M".into()),
    })
}

// Even an early bootstrap/channel failure must not discard an unconfirmed
// registered child or release the profile lock while this process remains up.
struct GenerationGuard {
    owner: Arc<Mutex<DesktopSupervisor>>,
    settled: bool,
}
impl Drop for GenerationGuard {
    fn drop(&mut self) {
        if self.settled {
            return;
        }
        let stopped = self
            .owner
            .lock()
            .ok()
            .is_some_and(|mut owner| owner.close_generation(PROPOSED_SHUTDOWN_MS).stopped);
        if !stopped {
            std::mem::forget(self.owner.clone());
        }
    }
}

/// Explicit console invocation only. Fixed installed worker and app-data paths
/// come from the host, not from these dataset/output arguments.
#[cfg(any(unix, windows))]
pub fn run_owned(
    root: &Path,
    executable: &Path,
    resources: &Path,
    args: EvaluationArgs,
) -> io::Result<i32> {
    run_with_preparation(root, executable, resources, args, |owner| {
        owner.prepare_artifacts(resources, root, &|| false)
    })
}

// The only production caller above always uses the compiled installed inventory.
// Unit tests inject their own resource tree here without altering that gate.
#[cfg(any(unix, windows))]
fn run_with_preparation(
    root: &Path,
    executable: &Path,
    resources: &Path,
    args: EvaluationArgs,
    prepare: impl FnOnce(
        &mut DesktopSupervisor,
    ) -> io::Result<crate::verified_artifacts::GuardedResources>,
) -> io::Result<i32> {
    if !root.is_absolute() || !executable.is_absolute() || !resources.is_absolute() {
        return Err(io::Error::other("Invalid installed layout"));
    }
    let owner = Arc::new(Mutex::new(DesktopSupervisor::acquire(root)?));
    let mut guard = GenerationGuard {
        owner: owner.clone(),
        settled: false,
    };
    let guarded = {
        let mut locked = owner
            .lock()
            .map_err(|_| io::Error::other("Owner unavailable"))?;
        prepare(&mut locked)?
    };
    let resources = guarded.root();
    #[cfg(any(windows, target_os = "macos"))]
    let guarded_executable = guarded.worker(std::env::consts::OS);
    #[cfg(any(windows, target_os = "macos"))]
    let executable = guarded_executable.as_path();
    let mut worker = {
        let mut locked = owner
            .lock()
            .map_err(|_| io::Error::other("Owner unavailable"))?;
        locked.processes.spawn(
            ProcessKind::Worker,
            &SpawnSpec::packaged_worker_generation(executable, 1),
        )?
    };
    let mut pipes = worker
        .take_pipes()
        .ok_or_else(|| io::Error::other("Worker pipes unavailable"))?;
    let worker = Arc::new(worker);
    let broker = Arc::new(crate::native_broker::NativeBroker::new_evaluation(
        owner.clone(),
        worker.clone(),
        root,
        resources,
        Arc::new(crate::target_proof::PackagedProof::new(
            owner.clone(),
            worker,
            guarded.clone(),
        )),
    )?);
    #[cfg(unix)]
    let control = crate::host_control::start(
        pipes
            .control
            .take()
            .ok_or_else(|| io::Error::other("Private control unavailable"))?,
        1,
        Arc::new(crate::credentials::PlatformVault::new()),
        Some(broker.clone()),
    )?;
    #[cfg(windows)]
    let control = crate::host_control::start_windows(
        pipes
            .control
            .take()
            .ok_or_else(|| io::Error::other("Private control unavailable"))?,
        1,
        Arc::new(crate::credentials::PlatformVault::new()),
        Some(broker.clone()),
    );
    let mut bootstrap = pipes
        .bootstrap
        .take()
        .ok_or_else(|| io::Error::other("Private bootstrap unavailable"))?;
    let root = root
        .to_str()
        .ok_or_else(|| io::Error::other("Invalid data root"))?;
    let dataset = args
        .dataset
        .to_str()
        .ok_or_else(|| io::Error::other("Invalid dataset"))?;
    let output = args
        .output
        .to_str()
        .ok_or_else(|| io::Error::other("Invalid output"))?;
    let frame = crate::ipc::encode_frame(
        &serde_json::json!({"version":1,"type":"LocalEvaluationBootstrap","data_root":root,
        "generation":1,"dataset":dataset,"output":output,"quantization":args.quantization}),
    )
    .map_err(|_| io::Error::other("Evaluation bootstrap unavailable"))?;
    bootstrap.write_all(&frame)?;
    bootstrap.flush()?;
    // The otherwise browser-only lane is dedicated to scoped evaluation
    // observations in this evaluation-only worker; it never carries CDP.
    #[cfg(unix)]
    let observation = crate::native_broker::DriverLane::from_socket(
        pipes
            .driver
            .take()
            .ok_or_else(|| io::Error::other("Observer lane unavailable"))?,
    )?;
    #[cfg(windows)]
    let observation = crate::native_broker::DriverLane::from_pipe(
        pipes
            .driver
            .take()
            .ok_or_else(|| io::Error::other("Observer lane unavailable"))?,
    );
    crate::evaluation_observer::start(observation, broker.clone(), 1);
    drop(bootstrap);
    let mut stdout = pipes.stdout;
    let mut stderr = pipes.stderr;
    let _stdin = pipes.stdin;
    std::thread::spawn(move || {
        let _ = std::io::copy(&mut stderr, &mut std::io::sink());
    });
    let (send, receive) = mpsc::sync_channel(1);
    std::thread::spawn(move || {
        let _ = send.send(crate::ipc::read_frame(&mut stdout));
    });
    let response = receive.recv_timeout(Duration::from_secs(30 * 60));
    broker.revoke();
    control.revoke();
    let report = owner
        .lock()
        .map_err(|_| io::Error::other("Owner unavailable"))?
        .close_generation_with_grace(PROPOSED_SHUTDOWN_MS, 1000);
    if !report.stopped {
        std::mem::forget(owner.clone());
        guard.settled = true;
        return Err(io::Error::other("Evaluation cleanup unconfirmed"));
    }
    guard.settled = true;
    let value = response
        .map_err(|_| io::Error::other("Evaluation deadline exceeded"))?
        .map_err(|_| io::Error::other("Evaluation response invalid"))?
        .ok_or_else(|| io::Error::other("Evaluation response unavailable"))?;
    if value.as_object().is_none_or(|o| o.len() != 4)
        || value["type"] != "EvaluationFinished"
        || value["generation"] != 1
        || !matches!(value["exit_code"].as_i64(), Some(0 | 2))
    {
        return Err(io::Error::other("Evaluation response invalid"));
    }
    if value["exit_code"] == 0 {
        if let Some(digest) = value["report_digest"].as_str() {
            crate::automatic_evidence::install(
                Path::new(root),
                &args.output,
                digest,
                &crate::credentials::PlatformVault::new(),
            )?;
        }
    }
    Ok(value["exit_code"].as_i64().unwrap() as i32)
}

#[cfg(not(any(unix, windows)))]
pub fn run_owned(_: &Path, _: &Path, _: &Path, _: EvaluationArgs) -> io::Result<i32> {
    Err(io::Error::other("Unsupported target"))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[cfg(unix)]
    #[test]
    fn source_evaluation_entrypoint_reports_not_run_without_model_or_account() {
        use std::os::unix::fs::PermissionsExt;
        let root =
            std::env::temp_dir().join(format!("threadrelay-eval-host-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let resources = root.join("resources");
        let worker = resources.join("sidecars/threadrelay-worker");
        std::fs::create_dir_all(worker.parent().unwrap()).unwrap();
        let python =
            PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../worker/.venv/bin/python");
        std::fs::write(
            &worker,
            format!(
                "#!/bin/sh\nexec '{}' -m threadrelay.packaged_worker\n",
                python.display()
            ),
        )
        .unwrap();
        std::fs::set_permissions(&worker, std::fs::Permissions::from_mode(0o700)).unwrap();
        let dataset = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../../fixtures/evaluation/synthetic.jsonl");
        let output = root.join("report.json");
        let args = EvaluationArgs {
            dataset,
            output: output.clone(),
            quantization: "Q4_K_M".into(),
        };
        let profile = root.join("profile");
        assert_eq!(
            run_with_preparation(&profile, &worker, &resources, args, |owner| {
                use crate::bundle_inventory::{Entry, Inventory};
                use sha2::{Digest, Sha256};
                let bytes = std::fs::read(&worker)?;
                let inventory = Inventory::new(vec![
                    Entry::Directory {
                        path: "sidecars".into(),
                    },
                    Entry::File {
                        path: "sidecars/threadrelay-worker".into(),
                        sha256: format!("{:x}", Sha256::digest(&bytes)),
                        size: bytes.len() as u64,
                        executable: true,
                    },
                ])?;
                let (guarded, guard) = crate::verified_artifacts::synthetic_resources(
                    &resources, &profile, &inventory,
                )?;
                owner.retain_artifacts(guard);
                Ok(guarded)
            })
            .unwrap(),
            2
        );
        let report: serde_json::Value =
            serde_json::from_slice(&std::fs::read(output).unwrap()).unwrap();
        assert_eq!(report["metrics"]["status"], "not_run");
        assert!(DesktopSupervisor::acquire(&root.join("profile")).is_ok());
        std::fs::remove_dir_all(root).unwrap();
    }

    #[cfg(any(windows, target_os = "macos"))]
    #[test]
    fn missing_production_inventory_rejects_evaluation_before_spawn() {
        let root = std::env::temp_dir().join(format!(
            "threadrelay-eval-missing-inventory-{}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        let worker = root.join("sidecars/threadrelay-worker");
        let output = root.join("report.json");
        let args = EvaluationArgs {
            dataset: root.join("dataset.jsonl"),
            output: output.clone(),
            quantization: "Q4_K_M".into(),
        };
        let error = run_owned(&root.join("profile"), &worker, &root, args).unwrap_err();
        assert!(error.to_string().contains("inventory"));
        assert!(!output.exists());
        assert!(DesktopSupervisor::acquire(&root.join("profile")).is_ok());
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn cli_accepts_only_explicit_dataset_output_and_pinned_quantization() {
        let args = vec![
            "--dataset".to_owned(),
            "/tmp/input.jsonl".to_owned(),
            "--output".to_owned(),
            "/tmp/output.json".to_owned(),
        ];
        let parsed = parse_args(&args).unwrap();
        assert_eq!(parsed.quantization, "Q4_K_M");
        assert!(parse_args(&["--launcher".to_owned(), "/tmp/bin".to_owned()]).is_err());
        let mut bad = args.clone();
        bad.extend(["--quantization".to_owned(), "untrusted".to_owned()]);
        assert!(parse_args(&bad).is_err());
    }
}
