"""Private, generation-bound one-shot handoff for the trusted host channel.

This is not an ordinary Command/Event transport and is not native proof. Strings
cannot be reliably zeroized by Python; references are discarded promptly.
"""
import re
from threading import RLock


class SecureChannel:
    def __init__(self, *, generation):
        if type(generation) is not int or generation < 1:
            raise ValueError('Invalid private generation')
        self._generation = generation
        self._lock = RLock()
        self._values = {}
        self._seen = set()
        self._closed = False

    @property
    def pending_count(self):
        with self._lock:
            return len(self._values)

    def take(self, ref):
        with self._lock:
            if self._closed or ref not in self._values:
                raise ValueError('Private handoff unavailable')
            return self._values.pop(ref)

    def discard(self, ref):
        with self._lock:
            self._values.pop(ref, None)

    def close(self):
        with self._lock:
            self._closed = True
            self._values.clear()
            self._seen.clear()

    def submit(self, feature, message):
        keys = {'type', 'generation', 'epoch', 'challenge_id', 'kind', 'handle', 'value'}
        if not isinstance(message, dict) or set(message) != keys:
            raise ValueError('Invalid private submission')
        handle = message['handle']
        if not isinstance(handle, str) or re.fullmatch(r'[0-9a-f]{64}', handle) is None:
            raise ValueError('Invalid private submission')
        with self._lock:
            if self._closed or handle in self._seen or len(self._seen) >= 128 or self._values:
                raise ValueError('Private handoff unavailable')
            self._seen.add(handle)
            if (message['type'] != 'SubmitSecret' or type(message['generation']) is not int
                    or message['generation'] != self._generation or type(message['epoch']) is not int
                    or message['epoch'] < 0 or message['kind'] not in {'phone', 'code', 'password'}
                    or not isinstance(message['challenge_id'], str) or not 1 <= len(message['challenge_id']) <= 128
                    or not isinstance(message['value'], str) or not 1 <= len(message['value'].encode('utf-8')) <= 4096):
                raise ValueError('Invalid private submission')
            self._values[handle] = message['value']
        try:
            feature.submit_secure(message['challenge_id'], message['kind'], message['epoch'], handle)
        finally:
            self.discard(handle)

    def take_qr(self, feature, message):
        if (not isinstance(message, dict) or set(message) != {'type', 'generation', 'epoch', 'challenge_id'}
                or message['type'] != 'TakeQr' or type(message['generation']) is not int
                or message['generation'] != self._generation or type(message['epoch']) is not int
                or not isinstance(message['challenge_id'], str)):
            raise ValueError('Invalid private request')
        with self._lock:
            if self._closed:
                raise ValueError('Private handoff unavailable')
        auth = feature.auth
        with auth._lifecycle_lock:
            challenge = auth.challenge()
            if (not feature.native_gate() or auth.snapshot().epoch != message['epoch'] or challenge is None
                    or challenge.kind != 'qr' or challenge.challenge_id != message['challenge_id']):
                raise ValueError('Private QR unavailable')
            qr = auth.take_private_qr()
            if qr is None:
                raise ValueError('Private QR unavailable')
            return qr


def serve_private(stream, channel, feature):
    """Serve only the typed private channel; terminate on framing violations."""
    import json
    import struct
    from .main import _read_exact, _reject_constant, _unique_object, encode_frame

    try:
        while True:
            header = _read_exact(stream, 4, clean_eof=True)
            if header is None:
                break
            size = struct.unpack('>I', header)[0]
            if not 1 <= size <= 8192:
                break
            data = _read_exact(stream, size)
            try:
                message = json.loads(data.decode('utf-8'), object_pairs_hook=_unique_object,
                                     parse_constant=_reject_constant)
                if not isinstance(message, dict):
                    break
                if message.get('type') == 'SubmitSecret':
                    channel.submit(feature, message)
                    response = {'type': 'PrivateResult', 'ok': True}
                elif message.get('type') == 'TakeQr':
                    response = {'type': 'PrivateQr', 'value': channel.take_qr(feature, message)}
                else:
                    break
            except Exception:
                response = {'type': 'PrivateResult', 'ok': False}
            finally:
                # Neither value nor native errors are logged or echoed.
                message = None
                data = None
            stream.write(encode_frame(response))
            stream.flush()
            response = None
    except (OSError, ValueError):
        pass
    finally:
        channel.close()
