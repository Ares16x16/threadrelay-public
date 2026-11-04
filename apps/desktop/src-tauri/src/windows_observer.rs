//! Observe a connected loopback 4-tuple and pin the server process in its Job.
//! This is source support, not target acceptance or artifact verification.
use crate::supervisor::NativeIdentity;
use std::{
    io,
    mem::size_of,
    ptr::{addr_of, null_mut},
};
use windows_sys::Win32::{
    Foundation::{CloseHandle, ERROR_INSUFFICIENT_BUFFER, HANDLE},
    NetworkManagement::IpHelper::{
        GetExtendedTcpTable, MIB_TCPROW_OWNER_PID, MIB_TCPTABLE_OWNER_PID, MIB_TCP_STATE_ESTAB,
        TCP_TABLE_OWNER_PID_ALL,
    },
    Networking::WinSock::AF_INET,
    System::{
        JobObjects::{
            IsProcessInJob, JobObjectBasicProcessIdList, QueryInformationJobObject,
            JOBOBJECT_BASIC_PROCESS_ID_LIST,
        },
        ProcessStatus::{K32GetProcessMemoryInfo, PROCESS_MEMORY_COUNTERS},
        Threading::{OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION},
    },
};

fn rows() -> io::Result<Vec<MIB_TCPROW_OWNER_PID>> {
    let mut bytes = 0u32;
    let status = unsafe {
        GetExtendedTcpTable(
            null_mut(),
            &mut bytes,
            0,
            AF_INET as u32,
            TCP_TABLE_OWNER_PID_ALL,
            0,
        )
    };
    if status != ERROR_INSUFFICIENT_BUFFER || bytes < 4 || bytes > 16 * 1024 * 1024 {
        return Err(io::Error::other("TCP observation unavailable"));
    }
    for _ in 0..3 {
        let mut storage = vec![0u64; (bytes as usize + 7) / 8];
        let mut size = bytes;
        let status = unsafe {
            GetExtendedTcpTable(
                storage.as_mut_ptr().cast(),
                &mut size,
                0,
                AF_INET as u32,
                TCP_TABLE_OWNER_PID_ALL,
                0,
            )
        };
        if status == ERROR_INSUFFICIENT_BUFFER && size <= 16 * 1024 * 1024 {
            bytes = size;
            continue;
        }
        if status != 0 || size < 4 || size as usize > storage.len() * 8 {
            return Err(io::Error::other("TCP observation unavailable"));
        }
        let table = storage.as_ptr().cast::<MIB_TCPTABLE_OWNER_PID>();
        let count = unsafe { (*table).dwNumEntries as usize };
        if count > (size as usize - 4) / size_of::<MIB_TCPROW_OWNER_PID>() {
            return Err(io::Error::other("TCP observation unavailable"));
        }
        let ptr = unsafe { addr_of!((*table).table).cast::<MIB_TCPROW_OWNER_PID>() };
        return Ok(unsafe { std::slice::from_raw_parts(ptr, count) }.to_vec());
    }
    Err(io::Error::other("TCP observation unavailable"))
}
fn tuple(row: &MIB_TCPROW_OWNER_PID, local: u16, remote: u16) -> bool {
    row.dwState == MIB_TCP_STATE_ESTAB as u32
        && row.dwLocalAddr == u32::from_ne_bytes([127, 0, 0, 1])
        && row.dwRemoteAddr == u32::from_ne_bytes([127, 0, 0, 1])
        && u16::from_be(row.dwLocalPort as u16) == local
        && u16::from_be(row.dwRemotePort as u16) == remote
}
struct Process(HANDLE);
impl Drop for Process {
    fn drop(&mut self) {
        unsafe {
            CloseHandle(self.0);
        }
    }
}
pub(crate) fn verify_connected_peer(
    worker: NativeIdentity,
    runtime: NativeIdentity,
    client: u16,
    server: u16,
) -> io::Result<bool> {
    if client == 0 || server == 0 {
        return Ok(false);
    }
    let before = rows()?;
    let clients: Vec<_> = before.iter().filter(|r| tuple(r, client, server)).collect();
    let servers: Vec<_> = before.iter().filter(|r| tuple(r, server, client)).collect();
    if clients.len() != 1 || servers.len() != 1 || clients[0].dwOwningPid != worker.pid {
        return Ok(false);
    }
    let server_pid = servers[0].dwOwningPid;
    let raw = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, server_pid) };
    if raw.is_null() {
        return Ok(false);
    }
    let pinned = Process(raw);
    let mut member = 0;
    if unsafe { IsProcessInJob(pinned.0, runtime.job, &mut member) } == 0 || member == 0 {
        return Ok(false);
    }
    // Retaining the exact process handle prevents PID reuse while the second
    // snapshot confirms the same established pair; no HTTP bytes have been sent.
    let after = rows()?;
    let client_count = after
        .iter()
        .filter(|r| tuple(r, client, server) && r.dwOwningPid == worker.pid)
        .count();
    let server_count = after
        .iter()
        .filter(|r| tuple(r, server, client) && r.dwOwningPid == server_pid)
        .count();
    Ok(client_count == 1
        && server_count == 1
        && !worker.process.is_null()
        && !runtime.process.is_null())
}

pub(crate) fn listener(runtime: NativeIdentity, port: u16) -> io::Result<bool> {
    if port == 0 {
        return Ok(false);
    }
    let table = rows()?;
    let found: Vec<_> = table
        .iter()
        .filter(|row| {
            row.dwState
                == windows_sys::Win32::NetworkManagement::IpHelper::MIB_TCP_STATE_LISTEN as u32
                && row.dwLocalAddr == u32::from_ne_bytes([127, 0, 0, 1])
                && u16::from_be(row.dwLocalPort as u16) == port
        })
        .collect();
    if found.len() != 1 {
        return Ok(false);
    }
    let raw = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, found[0].dwOwningPid) };
    if raw.is_null() {
        return Ok(false);
    }
    let pinned = Process(raw);
    let mut member = 0;
    if unsafe { IsProcessInJob(pinned.0, runtime.job, &mut member) } == 0 || member == 0 {
        return Ok(false);
    }
    let after = rows()?;
    let matches: Vec<_> = after
        .iter()
        .filter(|row| {
            row.dwState
                == windows_sys::Win32::NetworkManagement::IpHelper::MIB_TCP_STATE_LISTEN as u32
                && row.dwLocalAddr == u32::from_ne_bytes([127, 0, 0, 1])
                && u16::from_be(row.dwLocalPort as u16) == port
        })
        .collect();
    Ok(matches.len() == 1 && matches[0].dwOwningPid == found[0].dwOwningPid)
}
fn job_pids(job: HANDLE) -> io::Result<Vec<u32>> {
    const MAX: usize = 1024;
    let bytes = 8 + MAX * size_of::<usize>();
    let mut storage = vec![0usize; (bytes + size_of::<usize>() - 1) / size_of::<usize>()];
    let mut written = 0u32;
    if unsafe {
        QueryInformationJobObject(
            job,
            JobObjectBasicProcessIdList,
            storage.as_mut_ptr().cast(),
            bytes as u32,
            &mut written,
        )
    } == 0
    {
        return Err(io::Error::other("Job scope unavailable"));
    }
    let list = storage.as_ptr().cast::<JOBOBJECT_BASIC_PROCESS_ID_LIST>();
    let assigned = unsafe { (*list).NumberOfAssignedProcesses as usize };
    let count = unsafe { (*list).NumberOfProcessIdsInList as usize };
    if count != assigned || count > MAX || 8 + count * size_of::<usize>() > bytes {
        return Err(io::Error::other("Job scope unavailable"));
    }
    let ptr = unsafe { addr_of!((*list).ProcessIdList).cast::<usize>() };
    let mut pids = unsafe { std::slice::from_raw_parts(ptr, count) }
        .iter()
        .map(|v| u32::try_from(*v).map_err(|_| io::Error::other("Job scope unavailable")))
        .collect::<io::Result<Vec<_>>>()?;
    pids.sort_unstable();
    pids.dedup();
    if pids.len() != count {
        return Err(io::Error::other("Job scope unavailable"));
    }
    Ok(pids)
}
fn rss(handle: HANDLE) -> io::Result<u64> {
    let mut counters: PROCESS_MEMORY_COUNTERS = unsafe { std::mem::zeroed() };
    counters.cb = size_of::<PROCESS_MEMORY_COUNTERS>() as u32;
    if unsafe { K32GetProcessMemoryInfo(handle, &mut counters, counters.cb) } == 0 {
        return Err(io::Error::other("RSS unavailable"));
    }
    Ok(counters.WorkingSetSize as u64)
}
/// Current RSS over exact live worker root and every process in the runtime Job.
/// Churn, denied access, incomplete membership, or scope uncertainty fail closed.
pub(crate) fn evaluation_memory(
    worker: NativeIdentity,
    runtime: Option<NativeIdentity>,
) -> io::Result<(u64, u64)> {
    if job_pids(worker.job)? != vec![worker.pid] {
        return Err(io::Error::other("Worker scope unavailable"));
    }
    let worker_rss = rss(worker.process)?;
    let runtime_rss = if let Some(runtime) = runtime {
        let before = job_pids(runtime.job)?;
        if before.is_empty() {
            return Err(io::Error::other("Runtime scope unavailable"));
        }
        let mut pinned = Vec::with_capacity(before.len());
        for pid in &before {
            let raw = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, *pid) };
            if raw.is_null() {
                return Err(io::Error::other("Runtime scope unavailable"));
            }
            let process = Process(raw);
            let mut member = 0;
            if unsafe { IsProcessInJob(process.0, runtime.job, &mut member) } == 0 || member == 0 {
                return Err(io::Error::other("Runtime scope unavailable"));
            }
            pinned.push(process);
        }
        let mut total = 0u64;
        for process in &pinned {
            total = total
                .checked_add(rss(process.0)?)
                .ok_or_else(|| io::Error::other("RSS unavailable"))?;
        }
        if job_pids(runtime.job)? != before {
            return Err(io::Error::other("Runtime scope changed"));
        }
        total
    } else {
        0
    };
    if job_pids(worker.job)? != vec![worker.pid] {
        return Err(io::Error::other("Worker scope changed"));
    }
    Ok((worker_rss, runtime_rss))
}
