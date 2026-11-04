//! Native-only power callbacks. No renderer can mint these events.
use crate::worker_bridge::WorkerHost;
use std::sync::Arc;

#[cfg(windows)]
pub fn register(host: Arc<WorkerHost>) -> std::io::Result<()> {
    use windows_sys::Win32::System::Power::{
        PowerRegisterSuspendResumeNotification, DEVICE_NOTIFY_SUBSCRIBE_PARAMETERS,
    };
    use windows_sys::Win32::UI::WindowsAndMessaging::DEVICE_NOTIFY_CALLBACK;
    unsafe extern "system" fn callback(
        context: *const std::ffi::c_void,
        event: u32,
        _: *const std::ffi::c_void,
    ) -> u32 {
        use windows_sys::Win32::UI::WindowsAndMessaging::{
            PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND, PBT_APMSUSPEND,
        };
        let host = &*(context as *const Arc<WorkerHost>);
        match event {
            PBT_APMSUSPEND => host.power_event(true),
            PBT_APMRESUMEAUTOMATIC | PBT_APMRESUMESUSPEND => host.power_event(false),
            _ => (),
        }
        0
    }
    // Process-lifetime registration and host reference: callbacks cannot race
    // a freed context during shutdown. The host is explicitly shut down on exit.
    let context = Box::into_raw(Box::new(host));
    let params = Box::into_raw(Box::new(DEVICE_NOTIFY_SUBSCRIBE_PARAMETERS {
        Callback: Some(callback),
        Context: context.cast(),
    }));
    let mut registration = std::ptr::null_mut();
    let status = unsafe {
        PowerRegisterSuspendResumeNotification(
            DEVICE_NOTIFY_CALLBACK,
            params.cast(),
            &mut registration,
        )
    };
    if status != 0 {
        unsafe {
            drop(Box::from_raw(params));
            drop(Box::from_raw(context));
        }
        return Err(std::io::Error::from_raw_os_error(status as i32));
    }
    Ok(())
}

#[cfg(target_os = "macos")]
pub fn register(host: Arc<WorkerHost>) -> std::io::Result<()> {
    unsafe extern "C" {
        fn threadrelay_register_power(
            context: *mut std::ffi::c_void,
            callback: extern "C" fn(*mut std::ffi::c_void, i32),
        ) -> i32;
    }
    extern "C" fn callback(context: *mut std::ffi::c_void, sleeping: i32) {
        let host = unsafe { &*(context as *const Arc<WorkerHost>) };
        host.power_event(sleeping != 0);
    }
    let context = Box::into_raw(Box::new(host));
    if unsafe { threadrelay_register_power(context.cast(), callback) } == 0 {
        unsafe {
            drop(Box::from_raw(context));
        }
        return Err(std::io::Error::other("Power notifications unavailable"));
    }
    Ok(())
}

#[cfg(not(any(windows, target_os = "macos")))]
pub fn register(_: Arc<WorkerHost>) -> std::io::Result<()> {
    // Unsupported targets have no native activation capability; keep local
    // SQLite development available without pretending to observe power.
    Ok(())
}
