use threadrelay_desktop::credentials::{CredentialError, CredentialVault, PlatformVault};

#[test]
fn rejects_untrusted_references_and_empty_secrets_before_platform_access() {
    let vault = PlatformVault::new();
    assert_eq!(
        vault.put("../../arbitrary", b"value"),
        Err(CredentialError::InvalidInput)
    );
    assert_eq!(
        vault.put("vault:valid", b""),
        Err(CredentialError::InvalidInput)
    );
    assert!(matches!(
        vault.get("vault:../escape"),
        Err(CredentialError::InvalidInput)
    ));
}

#[test]
#[cfg(not(any(target_os = "windows", target_os = "macos")))]
fn unsupported_platform_fails_closed_without_filesystem_fallback() {
    let vault = PlatformVault::new();
    assert_eq!(
        vault.put("vault:test", b"value"),
        Err(CredentialError::Unavailable)
    );
    assert!(matches!(
        vault.get("vault:test"),
        Err(CredentialError::Unavailable)
    ));
    assert_eq!(
        vault.delete("vault:test"),
        Err(CredentialError::Unavailable)
    );
}
