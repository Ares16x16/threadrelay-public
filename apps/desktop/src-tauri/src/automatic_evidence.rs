//! Host-owned evaluation receipt. An exported JSON file alone is never authority.
//! Only the private, owned real-evaluator completion path installs the digest in
//! the OS vault. No renderer method can import a report or write this receipt.
use crate::credentials::CredentialVault;
use sha2::{Digest, Sha256};
use std::{io, path::Path};
const REFERENCE: &str = "vault:threadrelay_evaluation_digest";
const FILE: &str = "automatic-evaluation.json";
const MAX: u64 = 1_048_576;
fn read(path: &Path) -> io::Result<Vec<u8>> {
    use std::io::Read;
    let meta = std::fs::symlink_metadata(path)?;
    if !meta.is_file() || meta.file_type().is_symlink() || meta.len() > MAX {
        return Err(io::Error::other("Evidence unavailable"));
    }
    let mut data = Vec::new();
    std::fs::File::open(path)?
        .take(MAX + 1)
        .read_to_end(&mut data)?;
    if data.len() as u64 > MAX {
        return Err(io::Error::other("Evidence unavailable"));
    }
    Ok(data)
}
fn valid(data: &[u8]) -> bool {
    let Ok(v) = serde_json::from_slice::<serde_json::Value>(data) else {
        return false;
    };
    v["versions"]["synthetic_dataset"] == "false"
        && v["versions"]["dataset_split"] == "test"
        && v["versions"]["reading_mode"] == "one-way-llama.cpp"
        && v["metrics"]["confirmed_model_inference_count"]
            .as_u64()
            .is_some_and(|n| n > 0)
}
pub(crate) fn install(
    root: &Path,
    output: &Path,
    expected: &str,
    vault: &dyn CredentialVault,
) -> io::Result<()> {
    let data = read(output)?;
    let digest = format!("{:x}", Sha256::digest(&data));
    if digest != expected || !valid(&data) {
        return Err(io::Error::other("Evidence unavailable"));
    }
    let temporary = root.join("automatic-evaluation.pending");
    use std::io::Write;
    let mut options = std::fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options.open(&temporary)?;
    file.write_all(&data)?;
    file.sync_all()?;
    drop(file);
    let target = root.join(FILE);
    // A stale receipt never authorizes new bytes; vault failure stays closed.
    match std::fs::symlink_metadata(&target) {
        Ok(m) if m.is_file() && !m.file_type().is_symlink() => std::fs::remove_file(&target)?,
        Err(e) if e.kind() == io::ErrorKind::NotFound => (),
        _ => return Err(io::Error::other("Evidence unavailable")),
    }
    std::fs::rename(&temporary, &target)?;
    vault
        .put(REFERENCE, digest.as_bytes())
        .map_err(|_| io::Error::other("Evidence receipt unavailable"))
}
pub(crate) fn verified_digest(root: &Path, vault: &dyn CredentialVault) -> Option<String> {
    let saved = vault.get(REFERENCE).ok()?;
    let data = read(&root.join(FILE)).ok()?;
    let digest = format!("{:x}", Sha256::digest(&data));
    (saved.expose() == digest.as_bytes() && valid(&data)).then_some(digest)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::credentials::{CredentialError, SecretBytes};
    use std::sync::Mutex;
    struct Vault(Mutex<Option<Vec<u8>>>);
    impl CredentialVault for Vault {
        fn put(&self, reference: &str, bytes: &[u8]) -> Result<(), CredentialError> {
            assert_eq!(reference, REFERENCE);
            *self.0.lock().unwrap() = Some(bytes.to_vec());
            Ok(())
        }
        fn get(&self, reference: &str) -> Result<SecretBytes, CredentialError> {
            assert_eq!(reference, REFERENCE);
            self.0
                .lock()
                .unwrap()
                .clone()
                .map(SecretBytes::for_test)
                .ok_or(CredentialError::NotFound)
        }
        fn delete(&self, _: &str) -> Result<(), CredentialError> {
            unreachable!()
        }
    }
    #[test]
    fn only_private_owned_completion_digest_can_bind_report_bytes() {
        let root =
            std::env::temp_dir().join(format!("threadrelay-auto-evidence-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        let output = root.join("export.json");
        let vault = Vault(Mutex::new(None));
        // This fixture exercises storage provenance, not a real evaluation.
        let data=br#"{"versions":{"synthetic_dataset":"false","dataset_split":"test","reading_mode":"one-way-llama.cpp"},"metrics":{"confirmed_model_inference_count":1}}"#;
        std::fs::write(&output, data).unwrap();
        let digest = format!("{:x}", Sha256::digest(data));
        assert!(install(&root, &output, &"0".repeat(64), &vault).is_err());
        assert!(verified_digest(&root, &vault).is_none());
        install(&root, &output, &digest, &vault).unwrap();
        assert_eq!(verified_digest(&root, &vault), Some(digest));
        std::fs::write(root.join(FILE), b"changed").unwrap();
        assert!(verified_digest(&root, &vault).is_none());
        let synthetic = String::from_utf8(data.to_vec())
            .unwrap()
            .replace("false", "true");
        std::fs::write(&output, &synthetic).unwrap();
        assert!(install(
            &root,
            &output,
            &format!("{:x}", Sha256::digest(synthetic.as_bytes())),
            &vault
        )
        .is_err());
        std::fs::remove_dir_all(root).unwrap();
    }
}
