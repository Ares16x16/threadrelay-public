"""Owned driver wire tests; no native browser or runtime proof."""
import asyncio
import socket
import struct
import threading
import pytest


def test_driver_transport_handles_bounded_little_endian_frames_without_spawning():
    from threadrelay.collector.owned_playwright import OwnedDriverTransport
    one, two = socket.socketpair(); one.settimeout(1); two.settimeout(1)
    class Driver:
        def __init__(self): self.closed = False
        def read(self, count): return one.recv(count)
        def write(self, data): one.sendall(data)
        def revoke(self):
            self.closed = True
            try: one.shutdown(socket.SHUT_RDWR)
            except OSError: pass
    async def exercise():
        driver = Driver(); transport = OwnedDriverTransport(asyncio.get_running_loop(), driver)
        received = asyncio.Event(); messages = []
        transport.on_message = lambda message: (messages.append(message), received.set())
        await transport.connect(); task = asyncio.create_task(transport.run())
        transport.send({'id':1})
        header = await asyncio.to_thread(two.recv, 4)
        body = await asyncio.to_thread(two.recv, struct.unpack('<I', header)[0])
        assert b'"id"' in body
        response = b'{"id":1,"result":{}}'
        two.sendall(struct.pack('<I', len(response)) + response)
        await asyncio.wait_for(received.wait(), 1)
        assert messages == [{'id':1,'result':{}}]
        transport.request_stop(); await asyncio.wait_for(task, 1)
        await asyncio.wait_for(transport.wait_until_stopped(), 1)
        assert driver.closed
    try: asyncio.run(exercise())
    finally: one.close(); two.close()


def test_driver_transport_rejects_oversized_frame_and_revokes():
    from threadrelay.collector.owned_playwright import OwnedDriverTransport, MAX_DRIVER_FRAME
    one, two = socket.socketpair(); one.settimeout(1); two.settimeout(1)
    class Driver:
        closed = False
        def read(self, count): return one.recv(count)
        def write(self, data): one.sendall(data)
        def revoke(self):
            self.closed = True
            try: one.shutdown(socket.SHUT_RDWR)
            except OSError: pass
    async def exercise():
        driver = Driver(); transport = OwnedDriverTransport(asyncio.get_running_loop(), driver)
        await transport.connect(); task = asyncio.create_task(transport.run())
        two.sendall(struct.pack('<I', MAX_DRIVER_FRAME+1))
        await asyncio.wait_for(task, 1)
        assert driver.closed and transport.on_error_future.done()
        transport.on_error_future.exception()  # consume expected redacted failure
    try: asyncio.run(exercise())
    finally: one.close(); two.close()


def test_pinned_adapter_rejects_library_drift_before_driver_use(monkeypatch):
    from threadrelay.collector import owned_playwright
    class Driver:
        def __getattr__(self, name): pytest.fail(f'driver touched after pin drift: {name}')
    monkeypatch.setattr(owned_playwright, 'PINNED_VERSION', '0.invalid')
    with pytest.raises(RuntimeError): owned_playwright.OwnedPlaywright(Driver()).start()


def test_owned_context_initialization_eof_is_bounded_and_revoked():
    from threadrelay.collector.owned_playwright import OwnedPlaywright
    class Driver:
        def __init__(self): self.closed = False
        def read(self, count): return b''
        def write(self, data): pass
        def revoke(self): self.closed = True
    driver = Driver()
    with pytest.raises(Exception): OwnedPlaywright(driver, timeout=.25).start()
    assert driver.closed


def test_context_never_migrates_to_another_thread():
    from threadrelay.collector.owned_playwright import OwnedPlaywright
    class Driver:
        def revoke(self): pass
    owner = OwnedPlaywright(Driver()); failures = []
    def run():
        try: owner.close()
        except Exception as error: failures.append(str(error))
    thread = threading.Thread(target=run); thread.start(); thread.join(1)
    assert failures == ['Owned driver thread mismatch']
    owner.close()


def test_driver_rejects_duplicate_object_fields():
    from threadrelay.collector.owned_playwright import OwnedDriverTransport
    one, two = socket.socketpair(); one.settimeout(1); two.settimeout(1)
    class Driver:
        closed = False
        def read(self, count): return one.recv(count)
        def write(self, data): one.sendall(data)
        def revoke(self):
            self.closed = True
            try: one.shutdown(socket.SHUT_RDWR)
            except OSError: pass
    async def exercise():
        driver = Driver(); transport = OwnedDriverTransport(asyncio.get_running_loop(), driver)
        await transport.connect(); task = asyncio.create_task(transport.run())
        payload = b'{"id":1,"id":2}'
        two.sendall(struct.pack('<I', len(payload)) + payload)
        await asyncio.wait_for(task, 1)
        assert driver.closed and transport.on_error_future.done()
        transport.on_error_future.exception()
    try: asyncio.run(exercise())
    finally: one.close(); two.close()
