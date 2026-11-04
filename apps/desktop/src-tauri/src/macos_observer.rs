//! SDK-compiled libproc shim; only retained supervisor root identities are used.
use std::io;
unsafe extern "C" {
    fn tr_macos_listener(runtime: i32, port: u16) -> i32;
    fn tr_macos_peer(worker: i32, runtime: i32, client: u16, server: u16) -> i32;
    fn tr_macos_rss(worker: i32, runtime: i32, worker_rss: *mut u64, runtime_rss: *mut u64) -> i32;
    #[cfg(test)]
    fn tr_macos_browser_pipe(browser: i32) -> i32;
}
pub(crate) fn listener(runtime: i32, port: u16) -> bool {
    runtime > 0 && unsafe { tr_macos_listener(runtime, port) } == 1
}
pub(crate) fn peer(worker: i32, runtime: i32, client: u16, server: u16) -> bool {
    worker > 0 && runtime > 0 && unsafe { tr_macos_peer(worker, runtime, client, server) } == 1
}
pub(crate) fn rss(worker: i32, runtime: Option<i32>) -> io::Result<(u64, u64)> {
    let (mut w, mut r) = (0, 0);
    if worker <= 0 || unsafe { tr_macos_rss(worker, runtime.unwrap_or(0), &mut w, &mut r) } != 1 {
        Err(io::Error::other("Scoped RSS unavailable"))
    } else {
        Ok((w, r))
    }
}

/// Readback for the no-account native harness; input is a retained supervisor root.
#[cfg(test)]
pub(crate) fn browser_pipe(browser: i32) -> bool {
    browser > 0 && unsafe { tr_macos_browser_pipe(browser) } == 1
}
