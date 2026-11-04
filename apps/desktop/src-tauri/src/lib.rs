#[cfg(all(feature = "development-worker", not(debug_assertions)))]
compile_error!("The development-worker feature is available only in debug builds");

// Tauri's context generator embeds this metadata for its dev-server mode,
// but skips it when custom-protocol embeds the frontend. Keep equivalent local
// development identity for that standalone debug executable too. Deliberately
// omit CFBundleIdentifier: existing debug WebKit/cache locations stay in use.
#[cfg(all(target_os = "macos", debug_assertions, feature = "custom-protocol"))]
tauri::embed_plist::embed_info_plist_bytes!(br#"<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>ThreadRelay</string>
<key>CFBundleDisplayName</key><string>ThreadRelay</string>
<key>CFBundleShortVersionString</key><string>0.1.0</string>
<key>CFBundleVersion</key><string>0.1.0</string>
</dict></plist>"#);

pub mod credentials;
#[cfg(target_os = "macos")]
pub(crate) mod local_vault;
#[cfg(target_os = "macos")]
pub(crate) mod telegram_quarantine;
pub mod embedded_threads;
#[cfg(any(windows, test))]
mod embedded_windows;

#[cfg(all(
    debug_assertions,
    target_os = "macos",
    target_arch = "aarch64",
    any(feature = "development-worker", test)
))]
mod connected_local_proof;
#[cfg(all(debug_assertions, any(feature = "development-worker", test)))]
mod development_worker;

#[cfg(all(test, target_os = "macos"))]
mod browser_acceptance;
#[cfg(any(all(debug_assertions, feature = "desktop"), test))]
mod dev_paths;
#[cfg(all(test, target_os = "macos"))]
mod inference_acceptance;
pub mod ipc;
#[cfg(unix)]
mod runtime_environment;
pub mod supervisor;
pub mod worker_bridge;

#[cfg(feature = "desktop")]
fn main_webview(view: &tauri::Webview) -> bool {
    view.url().is_ok_and(|url| {
        embedded_threads::trusted_invoker(view.label(), &url, cfg!(debug_assertions))
    })
}

#[cfg(feature = "desktop")]
#[tauri::command]
async fn embedded_threads_open(
    view: tauri::Webview,
    app: tauri::AppHandle,
    host: tauri::State<'_, std::sync::Arc<embedded_threads::EmbeddedThreads>>,
    bounds: embedded_threads::Bounds,
) -> Result<embedded_threads::Status, String> {
    if !main_webview(&view) {
        return Err("Embedded Threads unavailable".into());
    }
    let host = host.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        host.open(&app, bounds)
            .map_err(|_| "Embedded Threads unavailable".to_owned())
    })
    .await
    .map_err(|_| "Embedded Threads unavailable".to_owned())?
}
#[cfg(feature = "desktop")]
#[tauri::command]
async fn embedded_threads_resize(
    view: tauri::Webview,
    host: tauri::State<'_, std::sync::Arc<embedded_threads::EmbeddedThreads>>,
    generation: u64,
    revision: u64,
    bounds: embedded_threads::Bounds,
) -> Result<(), String> {
    if !main_webview(&view) {
        return Err("Embedded Threads unavailable".into());
    }
    let host = host.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        host.resize_visible(generation, revision, bounds)
            .map_err(|_| "Embedded Threads unavailable".to_owned())
    })
    .await
    .map_err(|_| "Embedded Threads unavailable".to_owned())?
}
#[cfg(feature = "desktop")]
#[tauri::command]
async fn embedded_threads_hide(
    view: tauri::Webview,
    host: tauri::State<'_, std::sync::Arc<embedded_threads::EmbeddedThreads>>,
    generation: u64,
    revision: u64,
) -> Result<(), String> {
    if !main_webview(&view) {
        return Err("Embedded Threads unavailable".into());
    }
    let host = host.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        host.hide(generation, revision)
            .map_err(|_| "Embedded Threads hide unconfirmed".to_owned())
    })
    .await
    .map_err(|_| "Embedded Threads hide unconfirmed".to_owned())?
}
#[cfg(feature = "desktop")]
#[tauri::command]
async fn embedded_threads_background(
    view: tauri::Webview,
    host: tauri::State<'_, std::sync::Arc<embedded_threads::EmbeddedThreads>>,
    generation: u64,
    revision: u64,
) -> Result<(), String> {
    if !main_webview(&view) {
        return Err("Embedded Threads unavailable".into());
    }
    let host = host.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        host.background(generation, revision)
            .map_err(|_| "Embedded Threads background unconfirmed".to_owned())
    })
    .await
    .map_err(|_| "Embedded Threads background unconfirmed".to_owned())?
}
#[cfg(feature = "desktop")]
#[tauri::command]
async fn embedded_threads_show(
    view: tauri::Webview,
    host: tauri::State<'_, std::sync::Arc<embedded_threads::EmbeddedThreads>>,
    generation: u64,
    revision: u64,
    bounds: embedded_threads::Bounds,
) -> Result<(), String> {
    if !main_webview(&view) {
        return Err("Embedded Threads unavailable".into());
    }
    let host = host.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        host.show(generation, revision, bounds)
            .map_err(|_| "Embedded Threads show unconfirmed".to_owned())
    })
    .await
    .map_err(|_| "Embedded Threads show unconfirmed".to_owned())?
}

#[cfg(feature = "desktop")]
#[tauri::command]
async fn embedded_threads_close(
    view: tauri::Webview,
    host: tauri::State<'_, std::sync::Arc<embedded_threads::EmbeddedThreads>>,
    generation: u64,
) -> Result<(), String> {
    if !main_webview(&view) {
        return Err("Embedded Threads unavailable".into());
    }
    let host = host.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        host.close(generation)
            .map_err(|_| "Embedded Threads close unconfirmed".to_owned())
    })
    .await
    .map_err(|_| "Embedded Threads close unconfirmed".to_owned())?
}
#[cfg(feature = "desktop")]
#[tauri::command]
async fn embedded_threads_status(
    view: tauri::Webview,
    host: tauri::State<'_, std::sync::Arc<embedded_threads::EmbeddedThreads>>,
) -> Result<embedded_threads::Status, String> {
    if !main_webview(&view) {
        return Err("Embedded Threads unavailable".into());
    }
    Ok(host.status())
}

#[cfg(feature = "desktop")]
#[tauri::command]
async fn worker_command(
    view: tauri::Webview,
    command: serde_json::Value,
    owner: tauri::State<'_, std::sync::Arc<worker_bridge::WorkerHost>>,
) -> Result<serde_json::Value, String> {
    if !main_webview(&view) {
        return Err("Worker connection unavailable".into());
    }
    // Blocking pipe waits never own the supervisor mutex or the UI thread.
    let owner = owner.inner().clone();
    tauri::async_runtime::spawn_blocking(move || owner.request(command).map_err(str::to_owned))
        .await
        .map_err(|_| "Worker connection unavailable".to_owned())?
}

#[cfg(feature = "desktop")]
#[tauri::command]
async fn secure_submit(
    window: tauri::Webview,
    owner: tauri::State<'_, std::sync::Arc<worker_bridge::WorkerHost>>,
    challenge_id: String,
    kind: String,
    epoch: u64,
    mut value: String,
) -> Result<(), String> {
    if !main_webview(&window) {
        secure_auth::wipe_string(&mut value);
        return Err("Private input unavailable".into());
    }
    let owner = owner.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        owner
            .secure_submit(&challenge_id, &kind, epoch, value)
            .map_err(str::to_owned)
    })
    .await
    .map_err(|_| "Private input unavailable".to_owned())?
}

#[cfg(feature = "desktop")]
#[tauri::command]
async fn secure_status(
    window: tauri::Webview,
    owner: tauri::State<'_, std::sync::Arc<worker_bridge::WorkerHost>>,
) -> Result<serde_json::Value, String> {
    if !main_webview(&window) {
        return Err("Secure status unavailable".into());
    }
    let owner = owner.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        owner.secure_status(&*owner.stored_vault())
    })
    .await
    .map_err(|_| "Secure status unavailable".to_owned())
}

#[cfg(feature = "desktop")]
#[tauri::command]
async fn secure_configure(
    window: tauri::Webview,
    owner: tauri::State<'_, std::sync::Arc<worker_bridge::WorkerHost>>,
    api_id: String,
    mut api_hash: String,
) -> Result<(), String> {
    if !main_webview(&window) {
        secure_auth::wipe_string(&mut api_hash);
        return Err("Secure setup unavailable".into());
    }
    let owner = owner.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let result = owner.secure_configure(&*owner.stored_vault(), &api_id, &api_hash);
        secure_auth::wipe_string(&mut api_hash);
        result.map_err(str::to_owned)
    })
    .await
    .map_err(|_| "Secure setup unavailable".to_owned())?
}

#[cfg(feature = "desktop")]
#[tauri::command]
async fn secure_qr(
    window: tauri::Webview,
    owner: tauri::State<'_, std::sync::Arc<worker_bridge::WorkerHost>>,
    challenge_id: String,
    epoch: u64,
) -> Result<String, String> {
    if !main_webview(&window) {
        return Err("Private QR unavailable".into());
    }
    let owner = owner.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        owner.secure_qr(&challenge_id, epoch).map_err(str::to_owned)
    })
    .await
    .map_err(|_| "Private QR unavailable".to_owned())?
}

#[cfg(any(feature = "desktop", test))]
#[derive(Default)]
struct StartupOutcome(std::sync::atomic::AtomicBool);

#[cfg(any(feature = "desktop", test))]
impl StartupOutcome {
    fn complete_setup(&self, result: Result<(), &'static str>, request_exit: impl FnOnce(i32)) {
        if let Err(message) = result {
            self.0.store(true, std::sync::atomic::Ordering::Release);
            eprintln!("ThreadRelay could not start: {message}");
            request_exit(1);
        }
    }

    fn exit_code(&self, runtime_code: i32) -> i32 {
        if self.0.load(std::sync::atomic::Ordering::Acquire) {
            1
        } else {
            runtime_code
        }
    }
}

/// Return only bounded, nonsecret startup messages. Native windows are created
/// after the GUI event loop is ready. Handle setup failures inside the callback:
/// returning an Err from a Tauri setup hook can panic across the native boundary.
#[cfg(feature = "desktop")]
pub fn run() -> Result<i32, &'static str> {
    use std::sync::Arc;
    use tauri::Manager;

    #[cfg(debug_assertions)]
    let owner = {
        let root = dev_paths::app_data().map_err(|_| {
            "Project-local app data is unavailable. Check its permissions and links."
        })?;
        let resources = dev_paths::resources().map_err(|_| {
            "Project-local resources are unavailable. Check their permissions and links."
        })?;
        #[cfg(feature = "development-worker")]
        let owner = {
            // Source execution is available only with an explicit debug feature.
            let _ = resources;
            #[cfg(feature = "connected-worker")]
            let result = worker_bridge::WorkerHost::connected(&root);
            #[cfg(not(feature = "connected-worker"))]
            let result = worker_bridge::WorkerHost::development(&root);
            result.map_err(|_| {
                "Local development worker is unavailable. Run the local preparation check."
            })?
        };
        #[cfg(not(feature = "development-worker"))]
        let owner = {
            let executable = installed_layout::worker(&resources, std::env::consts::OS);
            worker_bridge::WorkerHost::new(&root, &executable).map_err(|_| {
                "Local workspace is unavailable or already open in another instance."
            })?
        };
        Arc::new(owner)
    };

    let outcome = Arc::new(StartupOutcome::default());
    let setup_outcome = outcome.clone();
    let app = tauri::Builder::default()
        .setup(move |app| {
            let result = (|| -> Result<(), &'static str> {
                #[cfg(not(debug_assertions))]
                let owner = {
                    let root = app.path().app_data_dir()
                        .map_err(|_| "Installed app data is unavailable.")?;
                    #[cfg(windows)]
                    embedded_windows::prepare_app_root(&root)
                        .map_err(|_|"Private Windows app data is unavailable. Existing permissions were preserved.")?;
                    let resources = app.path().resource_dir()
                        .map_err(|_| "Installed resources are unavailable.")?;
                    #[cfg(any(windows, target_os = "macos"))]
                    {
                        let executable = std::env::current_exe()
                            .map_err(|_| "Installed executable is unavailable.")?;
                        let installed = installed_layout::resources(&executable, std::env::consts::OS)
                            .ok_or("Installed layout is unavailable.")?;
                        if installed != resources {
                            return Err("Installed resource layout does not match.");
                        }
                    }
                    let executable = installed_layout::worker(&resources, std::env::consts::OS);
                    Arc::new(worker_bridge::WorkerHost::new(&root, &executable)
                        .map_err(|_| "Installed workspace is unavailable or already open in another instance.")?)
                };
                app.manage(owner.clone());
                let embedded=Arc::new(embedded_threads::EmbeddedThreads::new(owner.app_root()).map_err(|_|"Embedded Threads storage unavailable.")?);
                owner.set_embedded(embedded.clone()).map_err(|_|"Embedded Threads host unavailable.")?;
                app.manage(embedded.clone());
                #[cfg(target_os = "macos")]
                app.handle().set_activation_policy(tauri::ActivationPolicy::Regular)
                    .map_err(|_| "Native application activation is unavailable.")?;

                // Config create=false keeps fallible window creation inside our
                // handled setup result, after AppKit is ready.
                let config = app.config().app.windows.iter()
                    .find(|window| window.label == "main")
                    .ok_or("Main window configuration is unavailable.")?;
                let window = tauri::WebviewWindowBuilder::from_config(app, config)
                    .and_then(|builder| builder.build())
                    .map_err(|_| "The main window could not be opened.")?;
                window.show().map_err(|_| "The main window could not be shown.")?;
                // OS foreground restrictions may deny focus while the visible
                // window remains usable; that does not invalidate startup.
                let _ = window.set_focus();
                #[cfg(all(debug_assertions,feature="development-worker",target_os="macos"))]
                if std::env::var("THREADRELAY_EMBEDDED_FIXTURE").ok().as_deref()==Some("1") {
                    let fixture_app=app.handle().clone();
                    std::thread::Builder::new().name("embedded-local-fixture".into()).spawn(move||{
                        let record=embedded.run_local_fixture(&fixture_app).unwrap_or_else(|problem|serde_json::json!({"purpose":"no-account-native-webkit-fixture","result":"failed","native_acceptance":"not_minted","error":problem.to_string()}));
                        let output=std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../.cache/completion-2026-10-31/browser/native-webkit-fixture.json");
                        let _=std::fs::write(output,serde_json::to_vec_pretty(&record).unwrap_or_default());
                    }).map_err(|_|"Embedded fixture scheduling unavailable.")?;
                }
                power_events::register(owner.clone())
                    .map_err(|_| "Native power notifications are unavailable.")?;
                #[cfg(feature="connected-worker")]
                {
                    let workspace=owner.clone();
                    std::thread::Builder::new().name("connected-workspace".into()).spawn(move||{
                        if workspace.request(serde_json::json!({"version":1,"id":"connected-workspace","type":"InitializeLocalWorkspace","payload":{}})).is_err() {
                            eprintln!("Connected workspace initialization unavailable; saved login was not restored");
                        } else {
                            let _ = workspace.request(serde_json::json!({"version":1,"id":"restore-saved-telegram","type":"RestoreTelegramSession","payload":{}}));
                        }
                    }).map_err(|_|"Connected workspace scheduling unavailable.")?;
                }
                Ok(())
            })();
            if result.is_err() {
                if let Some(owner) = app.try_state::<Arc<worker_bridge::WorkerHost>>() {
                    owner.shutdown();
                }
            }
            setup_outcome.complete_setup(result, |code| app.handle().exit(code));
            // Never return a fallible setup result into Tauri's native callback.
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            worker_command,
            secure_submit,
            secure_configure,
            secure_status,
            secure_qr
            ,embedded_threads_open,embedded_threads_resize,embedded_threads_hide,embedded_threads_background,embedded_threads_show,embedded_threads_close,embedded_threads_status
        ])
        .build(tauri::generate_context!())
        .map_err(|_| "The native desktop runtime is unavailable.")?;

    let code = app.run_return(|app, event| {
        if matches!(
            event,
            tauri::RunEvent::ExitRequested { .. } | tauri::RunEvent::Exit
        ) {
            if let Some(owner) = app.try_state::<Arc<worker_bridge::WorkerHost>>() {
                owner.shutdown();
            }
        }
    });
    Ok(outcome.exit_code(code))
}

#[cfg(test)]
mod startup_outcome_tests {
    use super::StartupOutcome;

    #[test]
    fn failed_setup_requests_failure_after_recording_it() {
        let outcome = StartupOutcome::default();
        let mut requested = None;
        outcome.complete_setup(Err("The main window could not be opened."), |code| {
            assert_eq!(outcome.exit_code(0), 1);
            requested = Some(code);
        });
        assert_eq!(requested, Some(1));
        // A normal runtime return must not erase a recorded setup failure.
        assert_eq!(outcome.exit_code(0), 1);
    }

    #[test]
    fn successful_setup_preserves_runtime_exit_and_does_not_request_exit() {
        let outcome = StartupOutcome::default();
        outcome.complete_setup(Ok(()), |_| panic!("successful setup requested exit"));
        assert_eq!(outcome.exit_code(0), 0);
        assert_eq!(outcome.exit_code(7), 7);
    }
}

#[cfg(all(test, feature = "desktop", feature = "custom-protocol"))]
mod startup_tests {
    #[test]
    fn standalone_debug_context_contains_the_built_frontend() {
        let context: tauri::Context<tauri::Wry> = tauri::generate_context!();
        let index = context
            .assets()
            .get(&"index.html".into())
            .expect("standalone desktop must embed its built frontend");
        let html = std::str::from_utf8(&index).unwrap();
        assert!(html.contains("<div id=\"root\"></div>"));
        assert!(html.contains("/assets/"));
    }
}

pub mod secure_auth;

pub mod host_control;

#[cfg(windows)]
pub mod windows_private;

pub mod private_staging;

pub mod native_broker;

pub mod evaluation_host;

#[cfg(windows)]
pub mod windows_observer;

pub mod artifact_verifier;

pub mod driver_mux;

#[cfg(windows)]
pub mod windows_chromium;

pub mod evaluation_observer;

#[cfg(target_os = "macos")]
pub mod macos_observer;

pub mod target_proof;

pub mod installed_layout;

#[cfg(any(feature = "desktop", windows, test))]
pub mod power_events;

pub mod bundle_inventory;

pub mod verified_artifacts;

pub mod automatic_evidence;
