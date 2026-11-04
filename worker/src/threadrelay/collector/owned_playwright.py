"""Pinned Playwright protocol transport over a supervisor-owned driver lease.

No subprocess or path is accepted or opened here. The host must already own the
Node driver and relay only its private pipe bytes. The lease's revoke operation
must immediately close/unblock its read and write endpoints; the supervisor,
not this transport, confirms process shutdown. This transport cannot provide a
native acceptance receipt.
"""
import asyncio
import json
import queue
import struct
import threading

from playwright._impl._transport import Transport

MAX_DRIVER_FRAME = 4 * 1024 * 1024
_QUEUE = 16


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError('duplicate field')
        result[key] = value
    return result


def _constant(_):
    raise ValueError('invalid number')


class OwnedDriverTransport(Transport):
    """Little-endian Playwright wire, bounded and separate from application IPC."""
    def __init__(self, loop, driver):
        super().__init__(loop)
        self._driver = driver
        self._stopped = False
        self._stopped_future = loop.create_future()  # exists even if connect fails
        self._incoming = asyncio.Queue(maxsize=_QUEUE)
        self._slots = threading.BoundedSemaphore(_QUEUE)
        self._outgoing = queue.Queue(maxsize=_QUEUE)
        self._reader = self._writer = None

    def _error(self):
        if not self.on_error_future.done():
            self.on_error_future.set_exception(RuntimeError('Owned driver connection unavailable'))

    def _deliver(self, value):
        try:
            if not self._stopped:
                self._incoming.put_nowait(value)
        except asyncio.QueueFull:
            self._error(); self.request_stop()
        finally:
            self._slots.release()

    def _failed(self):
        if not self._stopped:
            self._error(); self.request_stop()

    def _read_exact(self, size):
        chunks = bytearray()
        while len(chunks) < size and not self._stopped:
            data = self._driver.read(size-len(chunks))
            if not data: raise EOFError
            chunks.extend(data)
        if len(chunks) != size: raise EOFError
        return bytes(chunks)

    def _read(self):
        try:
            while not self._stopped:
                size = struct.unpack('<I', self._read_exact(4))[0]
                if not 1 <= size <= MAX_DRIVER_FRAME: raise ValueError
                value = json.loads(self._read_exact(size).decode('utf-8'), object_pairs_hook=_unique,
                                   parse_constant=_constant)
                if not isinstance(value, dict): raise ValueError
                if not self._slots.acquire(timeout=1): raise ValueError
                try:
                    self._loop.call_soon_threadsafe(self._deliver, value)
                except Exception:
                    self._slots.release(); raise
        except Exception:
            if not self._stopped:
                try: self._loop.call_soon_threadsafe(self._failed)
                except RuntimeError: pass

    def _write(self):
        try:
            while not self._stopped:
                try: frame = self._outgoing.get(timeout=.1)
                except queue.Empty: continue
                if frame is None: break
                self._driver.write(frame)
        except Exception:
            if not self._stopped:
                try: self._loop.call_soon_threadsafe(self._failed)
                except RuntimeError: pass

    async def connect(self):
        try:
            if self._stopped: raise ValueError
            # The supplied stream is already registered by the host. Never
            # invoke Playwright's PipeTransport or subprocess launcher here.
            self._reader = threading.Thread(target=self._read, daemon=True, name='owned-driver-read')
            self._writer = threading.Thread(target=self._write, daemon=True, name='owned-driver-write')
            self._reader.start(); self._writer.start()
        except Exception:
            self._error(); self.request_stop()
            if not self._stopped_future.done(): self._stopped_future.set_result(None)
            raise RuntimeError('Owned driver connection unavailable') from None

    async def run(self):
        try:
            while not self._stopped:
                value = await self._incoming.get()
                if value is None: break
                self.on_message(value)  # only the synchronous owner loop dispatches
        finally:
            self.request_stop()
            if not self._stopped_future.done(): self._stopped_future.set_result(None)

    def send(self, message):
        if self._stopped: raise RuntimeError('Owned driver connection unavailable')
        try:
            data = json.dumps(message, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
            if not 1 <= len(data) <= MAX_DRIVER_FRAME: raise ValueError
            self._outgoing.put_nowait(struct.pack('<I', len(data)) + data)
        except Exception:
            self._error(); self.request_stop()
            raise RuntimeError('Owned driver connection unavailable') from None

    def request_stop(self):
        if self._stopped: return
        self._stopped = True
        try: self._driver.revoke()
        except Exception: pass
        try: self._outgoing.put_nowait(None)
        except queue.Full: pass
        try: self._incoming.put_nowait(None)
        except asyncio.QueueFull: pass
        if self._reader is None and not self._stopped_future.done():
            self._stopped_future.set_result(None)

    async def wait_until_stopped(self):
        await self._stopped_future

    def dispose(self):
        self.request_stop()


# Exact source fingerprints for the 1.64.1 context/connection contract adapted
# below. Packaging must retain these pinned source files; drift fails closed.
PINNED_VERSION = '1.64.1'
PINNED_FILES = {
    'sync_api/_context_manager.py':'08a125ef906a626e08b662bbad66a46046846312ef2ab5854aba6ba8850aba0e',
    '_impl/_connection.py':'b0b51be0b29970d4ee586f1e9d1f0ff7762dee2d72b8ce138c06f43fbfbae54b',
    '_impl/_transport.py':'70ce983fc5e20dee3b656690b88c5cf557a5ceacb3d9e327c152b315d40a0e94',
    '_impl/_greenlets.py':'5752a799d45ebb9107d4988798037ae89e345579c66ceb3ad94f86638a9a0d29',
    '_impl/_object_factory.py':'577db2ad75ed58873b078c3a85bb8c9c6e347137140232e1c2d79b416347e513',
    'sync_api/_generated.py':'9749fe525f50e2585d1d3a591181c4393f9b179f9d22818ce754a3ec72fff03d',
}


def _verify_playwright_pin():
    import hashlib
    import importlib.metadata
    import os
    from pathlib import Path
    import playwright
    if os.environ.get('INTERNAL_PW_GREENLET_DEBUG') or 'DEBUGP' in os.environ:
        raise RuntimeError('Unsafe driver environment')
    if importlib.metadata.version('playwright') != PINNED_VERSION:
        raise RuntimeError('Playwright version mismatch')
    root = Path(playwright.__file__).resolve().parent
    for relative, expected in PINNED_FILES.items():
        file = root / relative
        if file.is_symlink() or hashlib.sha256(file.read_bytes()).hexdigest() != expected:
            raise RuntimeError('Playwright source mismatch')


class OwnedPlaywright:
    """Version-pinned sync context, adapted from Playwright's Apache-2.0 context manager.

    The dispatcher and every Playwright object remain on the creating thread.
    Only the owned driver's bytes are pumped by background threads.
    """
    def __init__(self, driver, *, timeout=10):
        if type(timeout) not in (int, float) or not 0 < timeout <= 30:
            raise ValueError('Invalid driver timeout')
        self._driver, self._timeout = driver, timeout
        self._closed = False
        self._owner = threading.get_ident()
        self._loop = self._connection = None

    def _same_thread(self):
        if threading.get_ident() != self._owner:
            raise RuntimeError('Owned driver thread mismatch')

    def start(self):
        self._same_thread()
        _verify_playwright_pin()
        from greenlet import GreenletExit, greenlet
        from playwright._impl._connection import Connection, from_channel
        from playwright._impl._greenlets import MainGreenlet
        from playwright._impl._object_factory import create_remote_object
        from playwright.sync_api._generated import Browser as SyncBrowser
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError('Sync Playwright requires its own loop')
        self._loop = asyncio.new_event_loop()
        caller = greenlet.getcurrent()
        def dispatcher():
            task = self._loop.create_task(self._connection.run_as_sync())
            def ended(_):
                future = self._connection.playwright_future
                if not future.done(): future.set_exception(RuntimeError('Owned driver initialization unavailable'))
            task.add_done_callback(ended)
            while not task.done():
                try:
                    self._loop.run_until_complete(task)
                except GreenletExit:
                    raise
                except BaseException as exc:
                    if task.done(): raise
                    caller.throw(exc)
                    asyncio._set_running_loop(None)
        fiber = MainGreenlet(dispatcher)
        transport = OwnedDriverTransport(self._loop, self._driver)
        self._connection = Connection(fiber, create_remote_object, transport, self._loop)
        future = self._connection.playwright_future
        def expire():
            if not future.done(): future.set_exception(RuntimeError('Owned driver initialization timeout'))
        timeout = self._loop.call_later(self._timeout, expire)
        future.add_done_callback(lambda _: caller.switch())
        try:
            fiber.switch()
            timeout.cancel()
            playwright = future.result()
            channel = playwright._initializer.get('preLaunchedBrowser')
            if channel is None: raise RuntimeError('Owned browser unavailable')
            browser = from_channel(channel)
            browser._connect_to_browser_type(playwright.chromium, None)
            self.browser = SyncBrowser(browser)
            return self
        except BaseException:
            timeout.cancel()
            self.close()
            raise

    def stop(self):
        self.close()

    def close(self):
        self._same_thread()
        if self._closed: return
        self._closed = True
        try:
            if self._connection is not None:
                self._connection.stop_sync()
                error = self._connection._transport.on_error_future
                if error.done() and not error.cancelled(): error.exception()
        finally:
            if self._loop is not None:
                for task in asyncio.all_tasks(self._loop):
                    if not task.done(): task.cancel()
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
                self._loop.close()
