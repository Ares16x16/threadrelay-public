//! Bounded private Playwright/CDP pipe multiplexer; no TCP or renderer route.
use std::{
    io::{self, BufRead, BufReader, Read, Write},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc,
    },
};
const MAX: usize = 4 * 1024 * 1024;
fn signal(stopped: &AtomicBool, fail: &Arc<dyn Fn() + Send + Sync>) {
    if !stopped.swap(true, Ordering::AcqRel) {
        fail();
    }
}
fn mux(kind: u8, body: Vec<u8>) -> io::Result<Vec<u8>> {
    if body.is_empty() || body.len() > MAX {
        return Err(io::Error::other("Invalid private frame"));
    }
    let mut result = Vec::with_capacity(body.len() + 5);
    result.push(kind);
    result.extend_from_slice(&(body.len() as u32).to_le_bytes());
    result.extend_from_slice(&body);
    Ok(result)
}
fn framed(reader: &mut dyn Read) -> io::Result<Vec<u8>> {
    let mut header = [0u8; 4];
    reader.read_exact(&mut header)?;
    let size = u32::from_le_bytes(header) as usize;
    if size == 0 || size > MAX {
        return Err(io::Error::other("Invalid private frame"));
    }
    let mut body = vec![0; size];
    reader.read_exact(&mut body)?;
    Ok(body)
}
fn cdp(reader: &mut dyn BufRead) -> io::Result<Vec<u8>> {
    let mut result = Vec::new();
    loop {
        let data = reader.fill_buf()?;
        if data.is_empty() {
            return Err(io::Error::other("CDP closed"));
        }
        let end = data.iter().position(|b| *b == 0);
        let n = end.unwrap_or(data.len());
        if result.len().saturating_add(n) > MAX {
            return Err(io::Error::other("Invalid CDP frame"));
        }
        result.extend_from_slice(&data[..n]);
        reader.consume(n + usize::from(end.is_some()));
        if end.is_some() {
            if result.is_empty() {
                return Err(io::Error::other("Invalid CDP frame"));
            }
            return Ok(result);
        }
    }
}
/// Both roots must already be registered; failure immediately revokes the
/// shared generation and requests same-manager cleanup. Queues are bounded.
pub(crate) fn start(
    lane: crate::native_broker::DriverLane,
    chrome: crate::supervisor::BrowserPipes,
    mut node_reader: Box<dyn Read + Send>,
    mut node_writer: Box<dyn Write + Send>,
    fail: Arc<dyn Fn() + Send + Sync>,
) {
    let stopped = Arc::new(AtomicBool::new(false));
    let (sender, receiver) = mpsc::sync_channel::<Vec<u8>>(16);
    let (mut worker_in, mut worker_out) = (lane.reader, lane.writer);
    let (chrome_in, mut chrome_out) = (chrome.reader, chrome.writer);
    let flag = stopped.clone();
    let failed = fail.clone();
    let to_node = sender.clone();
    std::thread::spawn(move || {
        while !flag.load(Ordering::Acquire) {
            let result = framed(&mut *worker_in).and_then(|v| mux(1, v));
            let ok = match result {
                Ok(frame) => to_node.try_send(frame).is_ok(),
                Err(_) => false,
            };
            if !ok {
                signal(&flag, &failed);
                break;
            }
        }
    });
    let flag = stopped.clone();
    let failed = fail.clone();
    let to_node = sender.clone();
    std::thread::spawn(move || {
        let mut reader = BufReader::new(chrome_in);
        while !flag.load(Ordering::Acquire) {
            let result = cdp(&mut reader).and_then(|v| mux(2, v));
            let ok = match result {
                Ok(frame) => to_node.try_send(frame).is_ok(),
                Err(_) => false,
            };
            if !ok {
                signal(&flag, &failed);
                break;
            }
        }
    });
    let flag = stopped.clone();
    let failed = fail.clone();
    std::thread::spawn(move || {
        while !flag.load(Ordering::Acquire) {
            match receiver.recv_timeout(std::time::Duration::from_millis(100)) {
                Ok(frame) => {
                    if node_writer
                        .write_all(&frame)
                        .and_then(|_| node_writer.flush())
                        .is_err()
                    {
                        signal(&flag, &failed);
                        break;
                    }
                }
                Err(mpsc::RecvTimeoutError::Timeout) => (),
                Err(_) => break,
            }
        }
    });
    let flag = stopped;
    let failed = fail;
    std::thread::spawn(move || {
        while !flag.load(Ordering::Acquire) {
            let result = (|| {
                let mut kind = [0u8; 1];
                node_reader.read_exact(&mut kind)?;
                let body = framed(&mut *node_reader)?;
                match kind[0] {
                    1 => {
                        worker_out.write_all(&(body.len() as u32).to_le_bytes())?;
                        worker_out.write_all(&body)?;
                        worker_out.flush()
                    }
                    2 => {
                        chrome_out.write_all(&body)?;
                        chrome_out.write_all(&[0])?;
                        chrome_out.flush()
                    }
                    _ => Err(io::Error::other("Invalid private channel")),
                }
            })();
            if result.is_err() {
                signal(&flag, &failed);
                break;
            }
        }
    });
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::{
        io::{Read, Write},
        os::unix::net::UnixStream,
        time::Duration,
    };
    #[test]
    fn forwards_both_private_wire_formats_without_crossing_channels() {
        let (worker_host, mut worker) = UnixStream::pair().unwrap();
        let (chrome_host, mut chrome) = UnixStream::pair().unwrap();
        let (node_host, mut node) = UnixStream::pair().unwrap();
        for s in [&worker, &chrome, &node] {
            s.set_read_timeout(Some(Duration::from_secs(1))).unwrap();
        }
        let lane = crate::native_broker::DriverLane::from_socket(worker_host).unwrap();
        let cdp = crate::supervisor::BrowserPipes {
            reader: Box::new(chrome_host.try_clone().unwrap()),
            writer: Box::new(chrome_host),
        };
        start(
            lane,
            cdp,
            Box::new(node_host.try_clone().unwrap()),
            Box::new(node_host),
            std::sync::Arc::new(|| {}),
        );
        worker.write_all(&[2, 0, 0, 0, b'{', b'}']).unwrap();
        let mut frame = [0; 7];
        node.read_exact(&mut frame).unwrap();
        assert_eq!(&frame, &[1, 2, 0, 0, 0, b'{', b'}']);
        node.write_all(&[2, 2, 0, 0, 0, b'{', b'}']).unwrap();
        let mut data = [0; 3];
        chrome.read_exact(&mut data).unwrap();
        assert_eq!(&data, b"{}\0");
        chrome.write_all(b"{}\0").unwrap();
        node.read_exact(&mut frame).unwrap();
        assert_eq!(&frame, &[2, 2, 0, 0, 0, b'{', b'}']);
        node.write_all(&[1, 2, 0, 0, 0, b'{', b'}']).unwrap();
        let mut data = [0; 6];
        worker.read_exact(&mut data).unwrap();
        assert_eq!(&data, &[2, 0, 0, 0, b'{', b'}']);
    }
}
