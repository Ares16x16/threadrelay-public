//! Concrete streaming SHA256 boundary for bounded artifact reads.
//!
//! Keeping Digest's generic caller in this small optimized crate makes debug
//! desktop verification fast without changing its lifecycle/debug settings.
#![forbid(unsafe_code)]
use sha2::Digest;

pub struct Sha256(sha2::Sha256);

impl Default for Sha256 {
    fn default() -> Self {
        Self::new()
    }
}
impl Sha256 {
    #[inline(never)]
    pub fn new() -> Self {
        Self(sha2::Sha256::new())
    }

    #[inline(never)]
    pub fn update(&mut self, bytes: &[u8]) {
        self.0.update(bytes);
    }

    #[inline(never)]
    pub fn finish_hex(self) -> String {
        format!("{:x}", self.0.finalize())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_and_incremental_known_vectors_are_exact() {
        assert_eq!(
            Sha256::new().finish_hex(),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
        let mut hash = Sha256::new();
        for bytes in [b"a".as_slice(), b"", b"bc"] {
            hash.update(bytes);
        }
        assert_eq!(
            hash.finish_hex(),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }
}
