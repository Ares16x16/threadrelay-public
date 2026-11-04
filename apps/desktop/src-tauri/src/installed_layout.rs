//! Fixed resource layout shared by desktop and evaluator; never renderer input.
use std::path::{Path, PathBuf};

pub fn resources(executable: &Path, target: &str) -> Option<PathBuf> {
    let dir = executable.parent()?;
    match target {
        "windows" => Some(dir.to_owned()),
        "macos" => Some(dir.parent()?.join("Resources")),
        _ => Some(dir.join("resources")),
    }
}

pub fn worker(resources: &Path, target: &str) -> PathBuf {
    let mut path = resources.join("sidecars/threadrelay-worker");
    if target == "windows" {
        path.set_extension("exe");
    }
    path
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn installed_console_and_desktop_share_target_resources() {
        let windows = resources(Path::new("/install/threadrelay-evaluate.exe"), "windows").unwrap();
        assert_eq!(windows, Path::new("/install"));
        assert_eq!(
            worker(&windows, "windows"),
            Path::new("/install/sidecars/threadrelay-worker.exe")
        );
        let mac = resources(
            Path::new("/ThreadRelay.app/Contents/MacOS/threadrelay-evaluate"),
            "macos",
        )
        .unwrap();
        assert_eq!(mac, Path::new("/ThreadRelay.app/Contents/Resources"));
        assert_eq!(
            worker(&mac, "macos"),
            Path::new("/ThreadRelay.app/Contents/Resources/sidecars/threadrelay-worker")
        );
    }
}
