#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
fn main() {
    // Compile-time introspection only: exits before AppKit, worker, vault or
    // profile initialization; no runtime argument grants native authority.
    #[cfg(all(debug_assertions, feature = "development-worker"))]
    if std::env::args_os().len() == 2
        && std::env::args_os().nth(1).as_deref() == Some(std::ffi::OsStr::new("--print-local-mode"))
    {
        println!(
            "{}",
            if cfg!(feature = "connected-worker") {
                "connected"
            } else {
                "inert"
            }
        );
        return;
    }

    let code = match threadrelay_desktop::run() {
        Ok(code) => code,
        Err(message) => {
            eprintln!("ThreadRelay could not start: {message}");
            1
        }
    };
    std::process::exit(code);
}
