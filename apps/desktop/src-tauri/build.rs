fn main() {
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("macos") {
        cc::Build::new()
            .file("native/macos_observer.c")
            .file("native/macos_artifacts.c")
            .compile("threadrelay_macos_observer");
        println!("cargo:rerun-if-changed=native/macos_observer.c");
        println!("cargo:rerun-if-changed=native/macos_artifacts.c");
        if std::env::var_os("CARGO_FEATURE_DESKTOP").is_some() {
            cc::Build::new()
                .file("native/macos_power.m")
                .file("native/macos_embedded.m")
                .compile("threadrelay_macos_power");
            println!("cargo:rustc-link-lib=framework=AppKit");
            println!("cargo:rerun-if-changed=native/macos_power.m");
            println!("cargo:rerun-if-changed=native/macos_embedded.m");
        }
    }
    #[cfg(feature = "desktop")]
    tauri_build::build();
}
