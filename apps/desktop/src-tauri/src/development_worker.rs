//! Explicit debug-only source worker. It grants local SQLite access only.
//! No installed inventory, native acceptance, account or vault authority is implied.
use crate::{
    credentials::{CredentialError, CredentialVault, SecretBytes},
    native_broker::{NativeProof, PreparedInference},
    supervisor::{Handle, SpawnSpec},
};
use std::{
    io,
    path::{Path, PathBuf},
};

#[derive(Clone)]
pub(crate) struct DevelopmentWorker {
    connected: bool,
    executable: PathBuf,
    data: PathBuf,
    source: PathBuf,
    dependencies: PathBuf,
    resources: PathBuf,
    #[cfg(unix)]
    runtime: crate::runtime_environment::PrivateRuntime,
}

fn unavailable() -> io::Error {
    io::Error::other("Project-local Python 3.12.16 worker environment unavailable")
}

fn project_root() -> io::Result<PathBuf> {
    let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let root = manifest.ancestors().nth(3).ok_or_else(unavailable)?;
    if !root.is_absolute() || root.canonicalize()? != root {
        return Err(unavailable());
    }
    Ok(root.to_owned())
}

/// Development paths are fixed by this checkout, never argv, environment or IPC.
/// Refuse links and writable ancestors between the checkout and each selected path.
fn checked_path(project: &Path, path: &Path, directory: bool) -> io::Result<()> {
    let relative = path.strip_prefix(project).map_err(|_| unavailable())?;
    let mut current = project.to_owned();
    for component in relative.components() {
        if !matches!(component, std::path::Component::Normal(_)) {
            return Err(unavailable());
        }
        current.push(component);
        let meta = std::fs::symlink_metadata(&current)?;
        if meta.file_type().is_symlink() {
            return Err(unavailable());
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            if meta.uid() != unsafe { libc::geteuid() } || meta.mode() & 0o022 != 0 {
                return Err(unavailable());
            }
        }
        if current != path && !meta.is_dir() {
            return Err(unavailable());
        }
        if current == path
            && (if directory {
                !meta.is_dir()
            } else {
                !meta.is_file()
            })
        {
            return Err(unavailable());
        }
    }
    Ok(())
}

impl DevelopmentWorker {
    pub(crate) fn discover(data: &Path) -> io::Result<Self> {
        let project = project_root()?;
        let allowed = project.join(".cache/threadrelay-dev");
        if !data.is_absolute() || !data.starts_with(&allowed) || data == allowed {
            return Err(io::Error::other("Invalid local development data root"));
        }
        let value = Self {
            connected: false,
            executable: project.join(".cache/build-inputs/install/python-3.12.16/bin/python3.12"),
            data: data.to_owned(),
            source: project.join("worker/src"),
            dependencies: project.join(".cache/venvs/worker-3.12.16/lib/python3.12/site-packages"),
            resources: project.join(".cache/threadrelay-dev/resources"),
            #[cfg(unix)]
            runtime: crate::runtime_environment::PrivateRuntime::prepare(
                data,
                crate::runtime_environment::RuntimeKind::Worker,
            )?,
        };
        value.validate()?;
        Ok(value)
    }
    pub(crate) fn connected(data: &Path) -> io::Result<Self> {
        if !cfg!(all(target_os = "macos", target_arch = "aarch64")) {
            return Err(unavailable());
        }
        let mut worker = Self::discover(data)?;
        worker.connected = true;
        Ok(worker)
    }
    pub(crate) fn is_connected(&self) -> bool {
        self.connected
    }

    pub(crate) fn validate(&self) -> io::Result<()> {
        let project = project_root()?;
        checked_path(&project, &self.data, true)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            if std::fs::symlink_metadata(&self.data)?.mode() & 0o077 != 0 {
                return Err(io::Error::other(
                    "Private local development data root required",
                ));
            }
        }
        checked_path(&project, &self.executable, false)?;
        checked_path(
            &project,
            &self.source.join("threadrelay/packaged_worker.py"),
            false,
        )?;
        checked_path(
            &project,
            &self.dependencies.join("jsonschema/__init__.py"),
            false,
        )?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            if std::fs::metadata(&self.executable)?.mode() & 0o111 == 0 {
                return Err(unavailable());
            }
        }
        Ok(())
    }
    pub(crate) fn executable(&self) -> &Path {
        &self.executable
    }
    pub(crate) fn resources(&self) -> &Path {
        &self.resources
    }
    pub(crate) fn spec(&self, generation: u64) -> SpawnSpec {
        let spec = SpawnSpec::development_worker(
            &self.executable,
            &self.source,
            &self.dependencies,
            generation,
        );
        #[cfg(unix)]
        let spec = spec.with_private_runtime(&self.runtime);
        spec
    }
}

/// Deliberately narrow: future protocol commands are denied until reviewed.
/// Saving an enabled route, approving work or activating a session is excluded.
pub(crate) fn local_command(command: &serde_json::Value) -> bool {
    match command["type"].as_str().unwrap_or("") {
        "InitializeLocalWorkspace"
        | "QueryState"
        | "CheckTelegramLogin"
        | "QueryAppStatus"
        | "QueryActivity"
        | "QueryTelegramTest"
        | "QueryManualHistorySend"
        | "QueryTelegramDestinations"
        | "QuerySavedTelegramDestinations"
        | "Pause"
        | "SuspendCollection"
        | "StopCollection"
        | "Stop"
        | "QueryTopics"
        | "SaveTopic"
        | "QueryRouteQueue"
        | "SetRouteQueuePaused"
        | "ClearRouteQueue"
        | "CancelRouteQueueItem"
        | "QueryRoutes"
        | "QueryHistory"
        | "QueryReview"
        | "QueryCollection"
        | "QueryArchivePosts"
        | "QueryArchiveRuns"
        | "QueryArchiveDeliveries"
        | "ReadArchiveDetail"
        | "QueryDeliveryWorkspace"
        | "SaveMessageTemplate"
        | "ConfigureDeliveryDraft"
        | "GetDeliveryDraft"
        | "PreviewDeliveryDraft"
        | "QueryDeliveryJobs"
        | "PauseDeliveryJob"
        | "CancelDeliveryJob"
        | "DismissReview"
        | "BulkDismissReview"
        | "QueryAccount"
        | "QueryReadiness"
        | "QueryDestination"
        | "QueryRetention"
        | "SaveRetention"
        | "QueryAutoStatus" => true,
        "SaveRoute" => command["payload"]["enabled"] == false,
        _ => false,
    }
}

pub(crate) fn connected_command(command: &serde_json::Value) -> bool {
    local_command(command)
        || match command["type"].as_str().unwrap_or("") {
            "BeginTelegramRecovery"
            | "QueryTelegramRecovery"
            | "RestoreTelegramSession"
            | "BeginTelegramLogin"
            | "CancelTelegramLogin"
            | "LogoutTelegram"
            | "RequestTelegramQr"
            | "ResendTelegramCode"
            | "VerifySavedMessages"
            | "VerifyDestination"
            | "RefreshTelegramDestinations"
            | "SaveTelegramDestination"
            | "SendTelegramTest"
            | "PreviewManualHistorySend"
            | "SendManualHistoryOnce"
            | "SaveAutoPolicy"
            | "PreviewAutoRoute"
            | "ApproveDeliveryDraft"
            | "ResumeDeliveryJob"
            | "TestSample" => true,
            "OptInAutoRoute" => {
                command["payload"]["route"]["mode"] == "auto"
                    && command["payload"]["confirmed"] == true
                    && command["payload"]["route"]["enabled"] == true
                    && command["payload"]["route"]["sender"]["kind"] == "user"
                    && command["payload"]["expected_receipt"]
                        .as_str()
                        .is_some_and(|value| {
                            value.len() == 64
                                && value
                                    .bytes()
                                    .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
                        })
            }
            "SaveRoute" => {
                command["payload"]["enabled"] == true
                    && matches!(
                        command["payload"]["mode"].as_str(),
                        Some("review" | "dry_run" | "auto")
                    )
            }
            "Start" => matches!(
                command["payload"]["mode"].as_str(),
                Some("dry_run" | "review" | "auto")
            ),
            _ => false,
        }
}

pub(crate) struct NoNativeCapabilities;
impl NativeProof for NoNativeCapabilities {
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
    fn driver_contained(&self, _: &Handle) -> bool {
        false
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

pub(crate) struct NoCredentials;
impl CredentialVault for NoCredentials {
    fn put(&self, _: &str, _: &[u8]) -> Result<(), CredentialError> {
        Err(CredentialError::Unavailable)
    }
    fn get(&self, _: &str) -> Result<SecretBytes, CredentialError> {
        Err(CredentialError::Unavailable)
    }
    fn delete(&self, _: &str) -> Result<(), CredentialError> {
        Err(CredentialError::Unavailable)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn local_commands_never_include_activation_or_unknown_commands() {
        for kind in [
            "Start",
            "OpenThreadsLogin",
            "RestoreTelegramSession",
            "BeginTelegramLogin",
            "CancelTelegramLogin",
            "LogoutTelegram",
            "RequestTelegramQr",
            "ResendTelegramCode",
            "VerifyDestination",
            "SelectSender",
            "TestSample",
            "Approve",
            "ApproveReview",
            "BulkApproveReview",
            "SaveAutoPolicy",
            "OptInAutoRoute",
            "PreviewAutoRoute",
            "FutureCommand",
        ] {
            assert!(
                !local_command(&json!({"type":kind,"payload":{}})),
                "allowed {kind}"
            );
        }
        assert!(!local_command(
            &json!({"type":"SaveRoute","payload":{"enabled":true}})
        ));
        assert!(local_command(
            &json!({"type":"SaveRoute","payload":{"enabled":false}})
        ));
        for kind in [
            "InitializeLocalWorkspace",
            "QueryTopics",
            "SaveTopic",
            "QueryRoutes",
            "QueryAccount",
            "QueryReadiness",
            "QueryRetention",
            "SaveRetention",
            "Stop",
        ] {
            assert!(
                local_command(&json!({"type":kind,"payload":{}})),
                "denied {kind}"
            );
        }
    }

    #[test]
    fn connected_commands_admit_normal_auto_route_configuration_and_preserve_legacy_consent() {
        for kind in [
            "RestoreTelegramSession",
            "BeginTelegramLogin",
            "CancelTelegramLogin",
            "LogoutTelegram",
            "RequestTelegramQr",
            "ResendTelegramCode",
            "VerifySavedMessages",
            "VerifyDestination",
            "RefreshTelegramDestinations",
            "TestSample",
            "SaveAutoPolicy",
            "PreviewManualHistorySend",
            "SendManualHistoryOnce",
            "QueryManualHistorySend",
            "PreviewAutoRoute",
        ] {
            assert!(connected_command(&json!({"type":kind,"payload":{}})));
        }
        for mode in ["dry_run", "review", "auto"] {
            assert!(connected_command(
                &json!({"type":"Start","payload":{"mode":mode}})
            ));
        }
        let fixtures: serde_json::Value =
            serde_json::from_str(include_str!("../../../../contracts/fixtures.json")).unwrap();
        let payload = fixtures["valid"]
            .as_array()
            .unwrap()
            .iter()
            .find(|case| case["name"] == "Auto exact consent")
            .unwrap()["value"]
            .clone();
        let command = json!({"type":"OptInAutoRoute","payload":payload});
        assert!(connected_command(&command));
        for key in ["confirmed", "expected_receipt"] {
            let mut changed = command.clone();
            changed["payload"].as_object_mut().unwrap().remove(key);
            assert!(!connected_command(&changed));
        }
        let mut channel = command.clone();
        channel["payload"]["route"]["sender"]["kind"] = json!("chat");
        assert!(!connected_command(&channel));
        for kind in [
            "SelectSender",
            "ApproveReview",
            "BulkApproveReview",
            "OptInAutoRoute",
            "FutureCommand",
        ] {
            assert!(!connected_command(&json!({"type":kind,"payload":{}})));
        }
        for mode in ["review", "dry_run", "auto"] {
            assert!(connected_command(
                &json!({"type":"SaveRoute","payload":{"enabled":true,"mode":mode}})
            ));
        }
        assert!(!connected_command(
            &json!({"type":"SaveRoute","payload":{"enabled":true,"mode":"future"}})
        ));
        assert!(!local_command(
            &json!({"type":"SaveRoute","payload":{"enabled":true,"mode":"review"}})
        ));
    }
    #[test]
    fn development_native_proof_and_credentials_always_deny() {
        let proof = NoNativeCapabilities;
        assert!(!proof.tdlib_ready());
        assert!(!proof.tdlib_auth_ready());
        assert!(!proof.tdlib_connection_only());
        assert!(proof.prepare_tdlib_auth(&|| false).is_none());
        assert!(proof.prepare_tdlib(&|| false).is_none());
        assert!(!proof.browser_ready());
        assert!(!proof.inference_ready());
        assert!(!proof.authorize_browser(
            Path::new("/browser"),
            Path::new("/driver"),
            Path::new("/cli"),
            Path::new("/profile"),
            &|| false
        ));
        assert_eq!(
            NoCredentials.put("vault:account", b"value"),
            Err(CredentialError::Unavailable)
        );
        assert!(matches!(
            NoCredentials.get("vault:account"),
            Err(CredentialError::Unavailable)
        ));
        assert_eq!(
            NoCredentials.delete("vault:account"),
            Err(CredentialError::Unavailable)
        );
    }
}
