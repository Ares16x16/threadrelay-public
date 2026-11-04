//! Console entrypoint; no path selects executables, modules, or endpoints.
fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args == ["--help"] {
        println!("Usage: threadrelay-evaluate --dataset LOCAL_JSONL --output NEW_REPORT [--quantization Q4_K_M|Q8_0]");
        return;
    }
    let result = (|| -> Result<i32, Box<dyn std::error::Error>> {
        let args = threadrelay_desktop::evaluation_host::parse_args(&args)?;
        let root = dirs::data_dir()
            .ok_or("App data unavailable")?
            .join("app.threadrelay.desktop");
        let executable = std::env::current_exe()?;
        let resources =
            threadrelay_desktop::installed_layout::resources(&executable, std::env::consts::OS)
                .ok_or("Installed layout unavailable")?;
        let worker =
            threadrelay_desktop::installed_layout::worker(&resources, std::env::consts::OS);
        Ok(threadrelay_desktop::evaluation_host::run_owned(
            &root, &worker, &resources, args,
        )?)
    })();
    match result {
        Ok(code) => std::process::exit(code),
        Err(_) => {
            eprintln!("Evaluation unavailable; no release permission granted.");
            std::process::exit(2)
        }
    }
}
