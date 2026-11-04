//! Dedicated bounded private observer lane; never a PID/query from the CLI.
use std::{
    io::{Read, Write},
    sync::Arc,
};
pub(crate) fn start(
    lane: crate::native_broker::DriverLane,
    broker: Arc<crate::native_broker::NativeBroker>,
    generation: u64,
) {
    std::thread::spawn(move || {
        let (mut reader, mut writer) = (lane.reader, lane.writer);
        loop {
            let mut header = [0u8; 4];
            if reader.read_exact(&mut header).is_err() {
                break;
            }
            let size = u32::from_be_bytes(header) as usize;
            if !(1..=8192).contains(&size) {
                break;
            }
            let mut frame = vec![0; size + 4];
            frame[..4].copy_from_slice(&header);
            if reader.read_exact(&mut frame[4..]).is_err() {
                break;
            }
            let request = match crate::ipc::read_frame(&mut std::io::Cursor::new(frame)) {
                Ok(Some(v)) => v,
                _ => break,
            };
            if request.as_object().is_none_or(|o| o.len() != 3)
                || request["type"] != "SampleEvaluation"
                || request["generation"].as_u64() != Some(generation)
            {
                break;
            }
            let id = request["reservation"].as_str().unwrap_or("");
            let response = match broker.sample_evaluation(id) {
                Some((worker, runtime)) => {
                    serde_json::json!({"type":"EvaluationMemory","generation":generation,"reservation":id,
                    "worker_rss_bytes":worker,"owned_runtime_rss_bytes":runtime,"accelerator_bytes":null})
                }
                None => {
                    serde_json::json!({"type":"ObservationUnavailable","generation":generation,"reservation":id})
                }
            };
            let frame = match crate::ipc::encode_frame(&response) {
                Ok(v) => v,
                Err(_) => break,
            };
            if writer
                .write_all(&frame)
                .and_then(|_| writer.flush())
                .is_err()
            {
                break;
            }
        }
    });
}
