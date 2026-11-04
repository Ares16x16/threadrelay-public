//! Trusted, compiled bundle inventory. Not an untrusted archive extractor.
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::io;

// Bounded for complete frozen Python + driver + framework bundles. Package
// measurement reports actual count/bytes; exceeding either is a build failure.
pub const MAX_ENTRIES: usize = 100_000;
pub const MAX_BYTES: u64 = 64 * 1024 * 1024 * 1024;
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum Entry {
    File {
        path: String,
        sha256: String,
        size: u64,
        executable: bool,
    },
    Directory {
        path: String,
    },
    Symlink {
        path: String,
        target: String,
    },
}
impl Entry {
    pub fn path(&self) -> &str {
        match self {
            Self::File { path, .. } | Self::Directory { path } | Self::Symlink { path, .. } => path,
        }
    }
}
pub struct Inventory {
    pub entries: BTreeMap<String, Entry>,
}
fn invalid() -> io::Error {
    io::Error::other("Invalid trusted bundle inventory")
}
pub fn normal_name(name: &str) -> bool {
    // Narrow portable names avoid case/Unicode filesystem aliases. Unicode
    // bundle paths require a separately reviewed normalization extension.
    !name.is_empty()
        && name.len() <= 1024
        && name.is_ascii()
        && name.split('/').all(|p| {
            !p.is_empty()
                && p != "."
                && p != ".."
                && !p.ends_with(['.', ' '])
                && p.bytes()
                    .all(|b| b >= 32 && b != 127 && !b"\\:*?\"<>|".contains(&b))
                && !matches!(
                    p.split('.')
                        .next()
                        .unwrap_or("")
                        .to_ascii_uppercase()
                        .as_str(),
                    "CON"
                        | "PRN"
                        | "AUX"
                        | "NUL"
                        | "COM1"
                        | "COM2"
                        | "COM3"
                        | "COM4"
                        | "COM5"
                        | "COM6"
                        | "COM7"
                        | "COM8"
                        | "COM9"
                        | "LPT1"
                        | "LPT2"
                        | "LPT3"
                        | "LPT4"
                        | "LPT5"
                        | "LPT6"
                        | "LPT7"
                        | "LPT8"
                        | "LPT9"
                )
        })
}
impl Inventory {
    pub fn new(values: Vec<Entry>) -> io::Result<Self> {
        if values.is_empty() || values.len() > MAX_ENTRIES {
            return Err(invalid());
        }
        let mut entries = BTreeMap::new();
        let mut bytes = 0u64;
        for item in values {
            if !normal_name(item.path()) {
                return Err(invalid());
            }
            match &item {
                Entry::File { sha256, size, .. } => {
                    if sha256.len() != 64
                        || !sha256
                            .bytes()
                            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
                    {
                        return Err(invalid());
                    }
                    bytes = bytes.checked_add(*size).ok_or_else(invalid)?;
                    if bytes > MAX_BYTES {
                        return Err(invalid());
                    }
                }
                Entry::Symlink { target, .. } => {
                    if target.is_empty()
                        || target.len() > 1024
                        || !target.is_ascii()
                        || target.starts_with('/')
                        || target
                            .bytes()
                            .any(|b| b < 32 || b == 127 || b == b'\\' || b == b':')
                    {
                        return Err(invalid());
                    }
                }
                _ => (),
            }
            if entries.insert(item.path().to_owned(), item).is_some() {
                return Err(invalid());
            }
        }
        for name in entries.keys().cloned().collect::<Vec<_>>() {
            let mut parent = name.as_str();
            while let Some((p, _)) = parent.rsplit_once('/') {
                match entries.get(p) {
                    None => {
                        entries.insert(p.to_owned(), Entry::Directory { path: p.into() });
                    }
                    Some(Entry::Directory { .. }) => (),
                    _ => return Err(invalid()),
                }
                parent = p;
            }
        }
        if entries.len() > MAX_ENTRIES {
            return Err(invalid());
        }
        let mut aliases = BTreeSet::new();
        if entries
            .keys()
            .any(|p| !aliases.insert(p.to_ascii_lowercase()))
        {
            return Err(invalid());
        }
        let graph = Self { entries };
        for entry in graph.entries.values() {
            if let Entry::Symlink { path, .. } = entry {
                graph.resolve(path)?;
            }
        }
        Ok(graph)
    }
    pub fn resolve(&self, name: &str) -> io::Result<String> {
        let mut todo: VecDeque<String> = name.split('/').map(str::to_owned).collect();
        let mut done: Vec<String> = Vec::new();
        let mut links = 0;
        while let Some(part) = todo.pop_front() {
            if part == "." || part.is_empty() {
                continue;
            }
            if part == ".." {
                if done.pop().is_none() {
                    return Err(invalid());
                }
                continue;
            }
            let candidate = done
                .iter()
                .map(String::as_str)
                .chain(std::iter::once(part.as_str()))
                .collect::<Vec<_>>()
                .join("/");
            match self.entries.get(&candidate).ok_or_else(invalid)? {
                Entry::Symlink { target, .. } => {
                    links += 1;
                    if links > 64 {
                        return Err(invalid());
                    }
                    for component in target.split('/').rev() {
                        todo.push_front(component.to_owned());
                    }
                }
                Entry::File { .. } if !todo.is_empty() => return Err(invalid()),
                _ => done.push(part),
            }
        }
        Ok(done.join("/"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn file(path: &str) -> Entry {
        Entry::File {
            path: path.into(),
            sha256: "a".repeat(64),
            size: 8,
            executable: false,
        }
    }
    fn link(path: &str, target: &str) -> Entry {
        Entry::Symlink {
            path: path.into(),
            target: target.into(),
        }
    }
    #[test]
    fn framework_links_resolve_in_manifest_order() {
        let graph = Inventory::new(vec![
            file("Framework/Versions/A/binary"),
            link("Framework/Versions/Current", "A"),
            link("Framework/binary", "Versions/Current/binary"),
        ])
        .unwrap();
        assert_eq!(
            graph.resolve("Framework/binary").unwrap(),
            "Framework/Versions/A/binary"
        );
        // A preceding link is resolved BEFORE .., not lexically normalized.
        assert!(Inventory::new(vec![file("x"), link("a", "."), link("escape", "a/../x")]).is_err());
    }
    #[test]
    fn rejects_escaping_cyclic_dangling_absolute_and_alias_entries() {
        for target in ["../x", "/x", "missing", "loop", "C:\\x"] {
            assert!(
                Inventory::new(vec![file("x"), link("loop", target)]).is_err(),
                "{target}"
            );
        }
        assert!(Inventory::new(vec![file("A"), file("a")]).is_err());
        assert!(Inventory::new(vec![file("x"), file("x/child")]).is_err());
        assert!(Inventory::new(vec![file("./x")]).is_err());
        assert!(Inventory::new(vec![]).is_err());
    }
}
