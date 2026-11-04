//! Target-process-scoped anonymous pipes. No pipe name or inheritable handle.
//! This source requires native Windows handle/job tests before activation.
use std::{
    fs::File,
    io::{self, Read, Write},
    os::windows::io::{AsRawHandle, FromRawHandle},
    process::Child,
    ptr::null_mut,
};
use windows_sys::Win32::{
    Foundation::{
        CloseHandle, DuplicateHandle, DUPLICATE_CLOSE_SOURCE, DUPLICATE_SAME_ACCESS, HANDLE,
    },
    System::{Pipes::CreatePipe, Threading::GetCurrentProcess},
};

#[derive(Debug)]
pub struct Duplex {
    pub reader: File,
    pub writer: File,
}
impl Read for Duplex {
    fn read(&mut self, b: &mut [u8]) -> io::Result<usize> {
        self.reader.read(b)
    }
}
impl Write for Duplex {
    fn write(&mut self, b: &[u8]) -> io::Result<usize> {
        self.writer.write(b)
    }
    fn flush(&mut self) -> io::Result<()> {
        self.writer.flush()
    }
}
struct Local(HANDLE);
impl Drop for Local {
    fn drop(&mut self) {
        unsafe {
            CloseHandle(self.0);
        }
    }
}
fn pipe() -> io::Result<(Local, Local)> {
    let mut reader = null_mut();
    let mut writer = null_mut();
    if unsafe { CreatePipe(&mut reader, &mut writer, std::ptr::null(), 0) } == 0 {
        return Err(io::Error::last_os_error());
    }
    Ok((Local(reader), Local(writer)))
}
fn duplicate(process: HANDLE, source: HANDLE) -> io::Result<HANDLE> {
    let mut target = null_mut();
    if unsafe {
        DuplicateHandle(
            GetCurrentProcess(),
            source,
            process,
            &mut target,
            0,
            0,
            DUPLICATE_SAME_ACCESS,
        )
    } == 0
    {
        Err(io::Error::last_os_error())
    } else {
        Ok(target)
    }
}
fn close_remote(process: HANDLE, handle: HANDLE) {
    let mut local = null_mut();
    if unsafe {
        DuplicateHandle(
            process,
            handle,
            GetCurrentProcess(),
            &mut local,
            0,
            0,
            DUPLICATE_CLOSE_SOURCE | DUPLICATE_SAME_ACCESS,
        )
    } != 0
        && !local.is_null()
    {
        unsafe {
            CloseHandle(local);
        }
    }
}
fn into_file(handle: Local) -> File {
    let raw = handle.0;
    std::mem::forget(handle);
    unsafe { File::from_raw_handle(raw) }
}
/// Child is already in its kill-on-close Job and remains suspended. On any
/// error all obtained remote duplicates are closed before caller kills/reaps.
pub fn prepare(child: &mut Child, generation: u64) -> io::Result<(Duplex, Duplex, Duplex)> {
    if generation == 0 || generation > 9_007_199_254_740_991 {
        return Err(io::Error::other("Invalid generation"));
    }
    let process = child.as_raw_handle();
    let (br, bw) = pipe()?;
    let (cr, cw) = pipe()?;
    let (hr, hw) = pipe()?;
    let (ir, iw) = pipe()?;
    let (dr, dw) = pipe()?;
    let (er, ew) = pipe()?;
    // child reads br/hr; child writes cw/iw. The other ends stay in host.
    crate::private_staging::stage_handles(
        &[br.0, cw.0, hr.0, iw.0, dr.0, ew.0],
        |source| duplicate(process, source),
        |remote| {
            let mut descriptor = Vec::with_capacity(64);
            descriptor.extend_from_slice(b"TRHP");
            descriptor.extend_from_slice(&[1, 6, 0, 0]);
            descriptor.extend_from_slice(&generation.to_le_bytes());
            for handle in remote {
                descriptor.extend_from_slice(&(*handle as usize as u64).to_le_bytes());
            }
            child
                .stdin
                .as_mut()
                .ok_or_else(|| io::Error::other("Worker stdin unavailable"))?
                .write_all(&descriptor)
        },
        |handle| close_remote(process, handle),
    )?;
    Ok((
        Duplex {
            reader: into_file(cr),
            writer: into_file(bw),
        },
        Duplex {
            reader: into_file(ir),
            writer: into_file(hw),
        },
        Duplex {
            reader: into_file(er),
            writer: into_file(dw),
        },
    ))
}
