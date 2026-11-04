"""Fixed packaged-worker entrypoint; fd 3 is a host-owned bootstrap channel.

The executable receives no paths or secrets in argv/environment. This entrypoint
is not a general CLI and never interprets renderer data as bootstrap.
"""
import os
import sys
from pathlib import Path
from threading import Event, Thread
from typing import BinaryIO

from .application import Application
from .bootstrap import read_startup
from .main import dispatch, serve_one, encode_frame
from .session import ParentProcess, ParentWatchdog
from .secure_channel import serve_private


def serve_local(root: Path, source: BinaryIO, destination: BinaryIO, *, monitor: bool = False,
                generation: int = 1, secure_stream=None, secure_shutdown=None, broker=None) -> None:
    with Application(root, generation=generation, broker=broker) as app:
        done = Event()
        thread = None
        private_thread = None
        parent = None
        try:
            if secure_stream is not None:
                if secure_shutdown is None:
                    raise ValueError('Private channel shutdown unavailable')
                private_thread = Thread(target=serve_private,
                    args=(secure_stream, app.secure_channel, app.features.account_features),
                    name='private-auth-channel', daemon=True)
                private_thread.start()
            if monitor:
                parent = ParentProcess()
                watchdog = ParentWatchdog(app.session, parent_alive=parent.alive, shutdown=lambda: os._exit(2))

                def tick() -> None:
                    while not done.is_set():
                        if not watchdog.poll():
                            return
                        app.coordinator.tick()
                        done.wait(0.25)

                thread = Thread(target=tick, name='private-parent-and-deadline', daemon=True)
                thread.start()
            stop_requested = False

            def handle(command):
                nonlocal stop_requested
                result = dispatch(command, controller=app.session, features=app.features, coordinator=app.coordinator, readiness=app.readiness)
                stop_requested = command['type'] == 'Stop'
                return result

            while serve_one(source, destination, handle):
                if stop_requested:
                    break
        finally:
            done.set()
            app.secure_channel.close()
            if private_thread is not None:
                try:
                    secure_shutdown()
                except OSError:
                    pass
                private_thread.join(timeout=1)
                if private_thread.is_alive():
                    raise RuntimeError('Private channel closure unconfirmed')
            if thread is not None:
                thread.join(timeout=1)
            if parent is not None:
                parent.close()



def serve_evaluation(startup, destination, broker):
    """Evaluation-only packaged path: no Application, collector or Telegram."""
    from .evaluation.owned import OwnedEvaluation, run_owned
    owner=OwnedEvaluation(Path(startup['data_root']),broker)
    arguments=['--dataset',startup['dataset'],'--output',startup['output'],
               '--quantization',startup['quantization']]
    try:
        code=run_owned(arguments,owner)
        if code not in (0,2) or not owner.retry_cleanup(): code=2
    except Exception:
        code=2
    destination.write(encode_frame({'type':'EvaluationFinished','generation':startup['generation'],'exit_code':code,'report_digest':owner.report_digest if code == 0 else None}))
    destination.flush()


def _run_startup(startup, private_stream, private_shutdown, control, destination):
    if startup['type']=='LocalWorkerBootstrap':
        serve_local(Path(startup['data_root']), sys.stdin.buffer, destination, monitor=True,
            generation=startup['generation'], secure_stream=private_stream, secure_shutdown=private_shutdown, broker=control)
    else:
        done=Event();parent=ParentProcess()
        def monitor():
            while not done.wait(.25):
                if not parent.alive(): os._exit(2)
        thread=Thread(target=monitor,name='evaluation-parent-watchdog',daemon=True);thread.start()
        try: serve_evaluation(startup,destination,control)
        finally: done.set();thread.join(timeout=1);parent.close()

class _PipeDuplex:
    """A fixed pair of Windows anonymous pipe handles, not a socket or path."""
    def __init__(self, reader, writer):
        import msvcrt
        self.reader = os.fdopen(msvcrt.open_osfhandle(reader, os.O_RDONLY | os.O_BINARY), 'rb', buffering=0)
        try:
            self.writer = os.fdopen(msvcrt.open_osfhandle(writer, os.O_WRONLY | os.O_BINARY), 'wb', buffering=0)
        except BaseException:
            self.reader.close()
            raise
    def read(self, n): return self.reader.read(n)
    def write(self, value): return self.writer.write(value)
    def flush(self): return self.writer.flush()
    def close(self):
        self.writer.close()
        self.reader.close()


def main() -> int:
    try:
        from .broker_channel import HostControl
        if os.name == 'nt':
            from .bootstrap import read_windows_descriptor
            expected_generation, handles = read_windows_descriptor(sys.stdin.buffer)
            private_stream = _PipeDuplex(*handles[:2])
            try:
                control_stream = _PipeDuplex(*handles[2:4])
                try:
                    driver_stream = _PipeDuplex(*handles[4:6])
                    startup = read_startup(private_stream)
                    root, generation = Path(startup['data_root']), startup['generation']
                    if generation != expected_generation:
                        raise ValueError('Private generation mismatch')
                    observer = HostControl(None, generation=generation, stream=driver_stream) if startup['type']=='LocalEvaluationBootstrap' else None
                    control = HostControl(None, generation=generation, stream=control_stream, root=root,
                        driver=None if observer is not None else driver_stream, observer=observer)
                    try:
                        _run_startup(startup, private_stream, private_stream.close, control, sys.stdout.buffer)
                    finally:
                        control.close()
                finally:
                    control_stream.close()
            finally:
                private_stream.close()
        else:
            import socket
            with socket.socket(fileno=3) as private_socket:
                with private_socket.makefile('rwb', buffering=0) as private_stream:
                    startup = read_startup(private_stream)
                    root, generation = Path(startup['data_root']), startup['generation']
                    lane=socket.socket(fileno=5)
                    observer=HostControl(lane,generation=generation) if startup['type']=='LocalEvaluationBootstrap' else None
                    control = HostControl(socket.socket(fileno=4), generation=generation, root=root,
                        driver=None if observer is not None else lane, observer=observer)
                    try:
                        _run_startup(startup, private_stream, lambda: private_socket.shutdown(socket.SHUT_RDWR), control, sys.stdout.buffer)
                    finally:
                        control.close()
    except Exception:
        print('Private worker unavailable.', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
