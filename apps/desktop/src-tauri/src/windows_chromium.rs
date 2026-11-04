//! Chromium pipe root: explicit STARTUPINFOEX handle whitelist, suspended Job
//! assignment, no ambient inheritance. Requires native target acceptance.
use crate::supervisor::{BrowserPipes, NativeIdentity, OwnedChild, OwnedPipes, SpawnSpec};
use std::{
    ffi::OsStr,
    fs::File,
    io,
    mem::{size_of, zeroed},
    os::windows::{ffi::OsStrExt, io::FromRawHandle},
    path::Path,
    ptr::{null, null_mut},
};
use windows_sys::Win32::{
    Foundation::{
        CloseHandle, SetHandleInformation, HANDLE, HANDLE_FLAG_INHERIT, WAIT_OBJECT_0, WAIT_TIMEOUT,
    },
    Security::SECURITY_ATTRIBUTES,
    System::{
        JobObjects::{
            AssignProcessToJobObject, CreateJobObjectW, JobObjectBasicAccountingInformation,
            JobObjectExtendedLimitInformation, QueryInformationJobObject, SetInformationJobObject,
            TerminateJobObject, JOBOBJECT_BASIC_ACCOUNTING_INFORMATION,
            JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        },
        Pipes::CreatePipe,
        Threading::{
            CreateProcessW, DeleteProcThreadAttributeList, InitializeProcThreadAttributeList,
            ResumeThread, TerminateProcess, UpdateProcThreadAttribute, WaitForSingleObject,
            CREATE_SUSPENDED, CREATE_UNICODE_ENVIRONMENT, EXTENDED_STARTUPINFO_PRESENT,
            PROCESS_INFORMATION, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, STARTF_USESTDHANDLES,
            STARTUPINFOEXW,
        },
    },
};
struct Handle(HANDLE);
unsafe impl Send for Handle {}
unsafe impl Sync for Handle {}
impl Drop for Handle {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe {
                CloseHandle(self.0);
            }
        }
    }
}
impl Handle {
    fn into_raw(mut self) -> HANDLE {
        let raw = self.0;
        self.0 = null_mut();
        raw
    }
}
fn pipe() -> io::Result<(Handle, Handle)> {
    let mut reader = null_mut();
    let mut writer = null_mut();
    if unsafe { CreatePipe(&mut reader, &mut writer, null::<SECURITY_ATTRIBUTES>(), 0) } == 0 {
        return Err(io::Error::last_os_error());
    }
    Ok((Handle(reader), Handle(writer)))
}
fn inherit(handle: HANDLE) -> io::Result<()> {
    if unsafe { SetHandleInformation(handle, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT) } == 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}
fn quote(value: &OsStr) -> Vec<u16> {
    let value: Vec<u16> = value.encode_wide().collect();
    crate::private_staging::quote_windows_argument(&value)
}
struct Attributes {
    storage: Vec<usize>,
    list: *mut std::ffi::c_void,
}
impl Drop for Attributes {
    fn drop(&mut self) {
        if !self.list.is_null() {
            unsafe {
                DeleteProcThreadAttributeList(self.list);
            }
        }
    }
}
fn attributes(handles: &[HANDLE]) -> io::Result<Attributes> {
    let mut bytes = 0usize;
    unsafe {
        InitializeProcThreadAttributeList(null_mut(), 1, 0, &mut bytes);
    }
    if bytes == 0 || bytes > 1024 * 1024 {
        return Err(io::Error::other("Handle list unavailable"));
    }
    let mut storage = vec![0usize; (bytes + size_of::<usize>() - 1) / size_of::<usize>()];
    let list = storage.as_mut_ptr().cast();
    if unsafe { InitializeProcThreadAttributeList(list, 1, 0, &mut bytes) } == 0 {
        return Err(io::Error::last_os_error());
    }
    let result = Attributes { storage, list };
    if unsafe {
        UpdateProcThreadAttribute(
            result.list,
            0,
            PROC_THREAD_ATTRIBUTE_HANDLE_LIST as usize,
            handles.as_ptr().cast(),
            std::mem::size_of_val(handles),
            null_mut(),
            null(),
        )
    } == 0
    {
        return Err(io::Error::last_os_error());
    }
    Ok(result)
}
fn job(process: HANDLE) -> io::Result<Handle> {
    let raw = unsafe { CreateJobObjectW(null(), null()) };
    if raw.is_null() {
        return Err(io::Error::last_os_error());
    }
    let handle = Handle(raw);
    let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { zeroed() };
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    if unsafe {
        SetInformationJobObject(
            raw,
            JobObjectExtendedLimitInformation,
            (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
            size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        )
    } == 0
        || unsafe { AssignProcessToJobObject(raw, process) } == 0
    {
        return Err(io::Error::last_os_error());
    }
    Ok(handle)
}
struct BrowserChild {
    process: Handle,
    _thread: Handle,
    job: Option<Handle>,
    pid: u32,
    pipes: Option<OwnedPipes>,
    failed: bool,
}
impl OwnedChild for BrowserChild {
    fn launch_error(&self) -> Option<io::Error> {
        self.failed
            .then(|| io::Error::other("Browser setup failed"))
    }
    fn native_identity(&self) -> Option<NativeIdentity> {
        Some(NativeIdentity {
            pid: self.pid,
            process: self.process.0,
            job: self.job.as_ref()?.0,
        })
    }
    fn take_pipes(&mut self) -> Option<OwnedPipes> {
        self.pipes.take()
    }
    fn exited(&mut self) -> io::Result<bool> {
        match unsafe { WaitForSingleObject(self.process.0, 0) } {
            WAIT_TIMEOUT => return Ok(false),
            WAIT_OBJECT_0 => (),
            _ => return Err(io::Error::last_os_error()),
        }
        if let Some(job) = &self.job {
            let mut info: JOBOBJECT_BASIC_ACCOUNTING_INFORMATION = unsafe { zeroed() };
            if unsafe {
                QueryInformationJobObject(
                    job.0,
                    JobObjectBasicAccountingInformation,
                    (&mut info as *mut JOBOBJECT_BASIC_ACCOUNTING_INFORMATION).cast(),
                    size_of::<JOBOBJECT_BASIC_ACCOUNTING_INFORMATION>() as u32,
                    null_mut(),
                )
            } == 0
            {
                return Err(io::Error::last_os_error());
            }
            if info.ActiveProcesses != 0 {
                let _ = unsafe { TerminateJobObject(job.0, 1) };
                return Ok(false);
            }
        }
        Ok(true)
    }
    fn terminate(&mut self) -> io::Result<()> {
        self.kill()
    }
    fn kill(&mut self) -> io::Result<()> {
        let ok = unsafe {
            if let Some(job) = &self.job {
                TerminateJobObject(job.0, 1)
            } else {
                TerminateProcess(self.process.0, 1)
            }
        };
        if ok == 0 {
            Err(io::Error::last_os_error())
        } else {
            Ok(())
        }
    }
}
impl Drop for BrowserChild {
    fn drop(&mut self) {
        let _ = self.kill();
    }
}
/// Only fixed host arguments are accepted through SpawnSpec; renderer cannot
/// select a binary, flags, pipe or handle. Every inherited handle is listed.
pub(crate) fn spawn(spec: &SpawnSpec) -> io::Result<Box<dyn OwnedChild>> {
    if !Path::new(&spec.executable).is_absolute() {
        return Err(io::Error::other("Invalid browser path"));
    }
    let (child_stdin, host_stdin) = pipe()?;
    let (host_stdout, child_stdout) = pipe()?;
    let (host_stderr, child_stderr) = pipe()?;
    let (chrome_read, host_cdp_write) = pipe()?;
    let (host_cdp_read, chrome_write) = pipe()?;
    let inherited = [
        child_stdin.0,
        child_stdout.0,
        child_stderr.0,
        chrome_read.0,
        chrome_write.0,
    ];
    for h in inherited {
        inherit(h)?;
    }
    let attrs = attributes(&inherited)?;
    let mut startup: STARTUPINFOEXW = unsafe { zeroed() };
    startup.StartupInfo.cb = size_of::<STARTUPINFOEXW>() as u32;
    startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    startup.StartupInfo.hStdInput = child_stdin.0;
    startup.StartupInfo.hStdOutput = child_stdout.0;
    startup.StartupInfo.hStdError = child_stderr.0;
    startup.lpAttributeList = attrs.list;
    let mut command = quote(spec.executable.as_os_str());
    for arg in &spec.args {
        command.push(b' ' as u16);
        command.extend(quote(arg));
    }
    command.push(b' ' as u16);
    command.extend(quote(OsStr::new(&format!(
        "--remote-debugging-io-pipes={},{}",
        chrome_read.0 as usize, chrome_write.0 as usize
    ))));
    command.push(0);
    let executable: Vec<u16> = spec
        .executable
        .as_os_str()
        .encode_wide()
        .chain(Some(0))
        .collect();
    let system = std::env::var_os("SystemRoot")
        .ok_or_else(|| io::Error::other("System root unavailable"))?;
    let mut environment: Vec<u16> = OsStr::new("SystemRoot=")
        .encode_wide()
        .chain(system.encode_wide())
        .chain([0, 0])
        .collect();
    let mut info: PROCESS_INFORMATION = unsafe { zeroed() };
    let ok = unsafe {
        CreateProcessW(
            executable.as_ptr(),
            command.as_mut_ptr(),
            null(),
            null(),
            1,
            CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | EXTENDED_STARTUPINFO_PRESENT,
            environment.as_mut_ptr().cast(),
            null(),
            &startup.StartupInfo,
            &mut info,
        )
    };
    let _keep_attributes = &attrs.storage;
    if ok == 0 {
        return Err(io::Error::last_os_error());
    }
    let process = Handle(info.hProcess);
    let thread = Handle(info.hThread);
    let assigned = job(process.0);
    let failed = assigned.is_err();
    let job = assigned.ok();
    let failed = failed || unsafe { ResumeThread(thread.0) } == u32::MAX;
    let pipes = OwnedPipes {
        browser_cdp: Some(BrowserPipes {
            reader: Box::new(unsafe { File::from_raw_handle(host_cdp_read.into_raw()) }),
            writer: Box::new(unsafe { File::from_raw_handle(host_cdp_write.into_raw()) }),
        }),
        stdin: Box::new(unsafe { File::from_raw_handle(host_stdin.into_raw()) }),
        stdout: Box::new(unsafe { File::from_raw_handle(host_stdout.into_raw()) }),
        stderr: Box::new(unsafe { File::from_raw_handle(host_stderr.into_raw()) }),
        bootstrap: None,
        control: None,
        driver: None,
    };
    Ok(Box::new(BrowserChild {
        process,
        _thread: thread,
        job,
        pid: info.dwProcessId,
        pipes: Some(pipes),
        failed,
    }))
}
