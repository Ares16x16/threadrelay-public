"""Generation-scoped host control; no renderer process/path/URL or vault reads.

The host owns process capabilities and independently verifies native evidence.
Channel connectivity, wire IDs, or synthetic responses are not native proof.
Python immutable secret copies cannot be reliably zeroized.
"""
import json
import queue
from pathlib import Path
import re
import socket
import struct
from threading import Lock, Thread

from .delivery.telegram import NativeUnavailable
from .inference.provision import load_catalog
from .inference.runtime import RuntimeUnavailable
from .main import _read_exact, _reject_constant, _unique_object, encode_frame

_CAPABILITY_READ_TIMEOUT = 1.0


class HostControl:
    def __init__(self, sock, *, generation, stream=None, root=None, driver=None, observer=None):
        if type(generation) is not int or generation < 1:
            raise ValueError('Invalid host control generation')
        if root is not None and not Path(root).is_absolute(): raise ValueError('Invalid trusted root')
        self._root = None if root is None else Path(root).resolve()
        self._observer = observer
        self._driver = None if driver is None else _DriverWire(driver)
        self._driver_used = False
        self._browser_capability = False
        self._embedded_lease = None
        self._socket, self._generation = sock, generation
        if stream is None:
            self._socket.settimeout(5)
            self._stream = sock.makefile('rwb', buffering=0)
        else:
            self._stream = stream
        self._lock = Lock()
        self._closed = False
        self._resolved = False
        self._requests = queue.Queue(maxsize=1)
        Thread(target=self._io_owner, name='host-control-io', daemon=True).start()

    def _io_owner(self):
        while not self._closed:
            try: item=self._requests.get(timeout=.1)
            except queue.Empty: continue
            if item is None: return
            request, reply, timeout, max_response_bytes=item
            try:
                if self._socket is not None:
                    self._socket.settimeout(timeout)
                self._stream.write(encode_frame(request))
                request = None
                self._stream.flush()
                size=struct.unpack('>I',_read_exact(self._stream,4))[0]
                if not 1<=size<=max_response_bytes: raise ValueError
                value=json.loads(_read_exact(self._stream,size).decode('utf-8'),
                    object_pairs_hook=_unique_object,parse_constant=_reject_constant)
                if (not isinstance(value,dict) or type(value.get('generation')) is not int
                        or value['generation']!=self._generation): raise ValueError
                if not self._closed: reply.put_nowait((True,value))
            except Exception:
                if not self._closed:
                    try: reply.put_nowait((False,None))
                    except queue.Full: pass
                self.close()
                return

    def _exchange_locked(self, kind, values=None, *, timeout=5, max_response_bytes=8192):
        if self._closed: raise NativeUnavailable('Host control unavailable')
        reply=queue.Queue(maxsize=1)
        try:
            self._requests.put_nowait(({'type':kind,'generation':self._generation,**(values or {})},reply,timeout,max_response_bytes))
            success,value=reply.get(timeout=timeout)
            if not success or self._closed: raise ValueError
            return value
        except Exception:
            self.close()
            raise NativeUnavailable('Host control unavailable') from None

    def _exchange(self, kind, values=None, *, timeout=5, max_response_bytes=8192):
        with self._lock:
            return self._exchange_locked(kind, values, timeout=timeout, max_response_bytes=max_response_bytes)

    def load_tdlib(self):
        # Explicit authentication only. Neither capability queries nor mounting
        # construct a native client. The host holds paths for this generation.
        from .delivery.native_loader import _from_host
        from .delivery.telegram import NativeJsonBinding
        value = self._exchange('PreparePinnedTdlib')
        if (set(value) != {'type', 'generation', 'library', 'purpose'} or value['type'] != 'TdlibPrepared'
                or value['purpose'] not in {'connection_only','delivery'}
                or not isinstance(value['library'], str) or not 1 <= len(value['library']) <= 4096):
            raise NativeUnavailable('Native TDLib unavailable')
        capability = _from_host(Path(value['library']), generation=self._generation,
                                is_live=lambda: not self._closed, purpose=value['purpose'])
        return NativeJsonBinding.load_verified(capability)

    def capabilities(self):
        # Never wait behind a slow launch or native callback while the worker
        # command thread holds lifecycle authority. Missing proof stays closed.
        if not self._lock.acquire(blocking=False): return {}
        try:
            value = self._exchange_locked('Capabilities', timeout=_CAPABILITY_READ_TIMEOUT)
            names = ('browser','inference','telegram','telegram_auth','automatic')
            if (set(value) != {'type','generation',*names} or value['type'] != 'Capabilities'
                    or any(type(value[name]) is not bool for name in names)):
                raise ValueError
            self._browser_capability = value['browser']
            return {name:value[name] for name in names}
        except Exception:
            return {}
        finally:
            self._lock.release()

    def embedded_threads_available(self):
        if not self._lock.acquire(blocking=False):
            return False
        try:
            value = self._exchange_locked('EmbeddedThreadsAvailable', timeout=.2)
            return (set(value) == {'type','generation','available'}
                and value['type'] == 'EmbeddedThreadsAvailability'
                and value['generation'] == self._generation
                and type(value['available']) is bool and value['available'])
        except Exception:
            return False
        finally:
            self._lock.release()

    def attach_embedded_threads(self):
        from .collector.browser import BrowserUnavailable
        if self._closed or self._embedded_lease is not None:
            raise BrowserUnavailable('embedded_cleanup_pending')
        value = self._exchange('AttachEmbeddedThreads')
        if (set(value) != {'type','generation','webview_generation','source_session_id'}
                or value['type'] != 'EmbeddedThreadsAttached' or value['generation'] != self._generation
                or type(value['webview_generation']) is not int or not 1 <= value['webview_generation'] <= 2**64 - 1
                or not isinstance(value['source_session_id'], str)
                or re.fullmatch(r'[0-9a-f]{64}', value['source_session_id']) is None):
            self.close()
            raise BrowserUnavailable('embedded_identity_unavailable')
        lease = _EmbeddedThreadsLease(self, value['webview_generation'], value['source_session_id'])
        self._embedded_lease = lease
        return lease

    def owned_driver_transport_available(self):
        if not self._browser_capability and not self._closed and not self._driver_used:
            self.capabilities()
        return bool(not self._closed and self._driver is not None and not self._driver_used and self._browser_capability)

    def driver_containment_verified(self):
        return self.owned_driver_transport_available()

    def launch_browser(self):
        if not self.owned_driver_transport_available(): raise NativeUnavailable('Browser unavailable')
        # One private driver lane belongs to exactly one activation in this
        # worker generation. Never reconnect it to a different process.
        self._driver_used = True
        try:
            value=self._exchange('OpenDedicatedBrowser',timeout=20)
            if (set(value)!={'type','generation','lease','source_session_id'} or value['type']!='BrowserOpened'
                    or not isinstance(value['lease'],str) or re.fullmatch(r'[0-9a-f]{64}',value['lease']) is None
                    or value['source_session_id']!=value['lease']): raise ValueError
            return _BrowserLease(self,value,self._driver)
        except Exception:
            self._driver.revoke()
            raise NativeUnavailable('Browser unavailable') from None

    def automatic_evidence(self):
        import hashlib
        value = self._exchange('AutomaticEvidence')
        if set(value) != {'type','generation','digest'} or value['type'] != 'AutomaticEvidence':
            raise NativeUnavailable('Evaluation authority unavailable')
        digest = value['digest']
        if not isinstance(digest, str) or re.fullmatch(r'[0-9a-f]{64}', digest) is None or self._root is None:
            return None
        path = self._root / 'automatic-evaluation.json'
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_048_576:
            return None
        with path.open('rb') as stream:
            data = stream.read(1_048_577)
        if len(data) > 1_048_576 or hashlib.sha256(data).hexdigest() != digest:
            return None
        return json.loads(data, object_pairs_hook=_unique_object, parse_constant=_reject_constant)

    def remove_account(self):
        value = self._exchange('RemoveLocalAccount')
        if value != {'type':'AccountRemoved','generation':self._generation,'removed':True}:
            raise NativeUnavailable('Account cleanup unconfirmed')

    def begin_account(self):
        with self._lock:
            if self._closed: raise NativeUnavailable('Secure credentials unavailable')
            self._resolved = False
            return 'vault:threadrelay_account'

    def quarantine_telegram_profile(self,expected_profile_id,expected_marker_sha256):
        value=self._exchange('QuarantineTelegramProfile',{'expected_profile_id':expected_profile_id,
            'expected_marker_sha256':expected_marker_sha256})
        from .storage.isolation import valid_receipt
        if (set(value)!={'type','generation','receipt'} or value['type']!='TelegramProfileQuarantined'
                or not valid_receipt(value['receipt'])):raise NativeUnavailable('Telegram quarantine unavailable')
        return value['receipt']

    def verify_telegram_quarantine(self,recovery_id):
        value=self._exchange('VerifyTelegramQuarantine',{'recovery_id':recovery_id})
        from .storage.isolation import valid_receipt
        if (set(value)!={'type','generation','receipt'} or value['type']!='TelegramQuarantineVerified'
                or not valid_receipt(value['receipt']) or value['receipt']['recovery_id']!=recovery_id):
            raise NativeUnavailable('Telegram quarantine verification unavailable')
        return value['receipt']

    def resolve(self, ref):
        if ref != 'vault:threadrelay_account': raise NativeUnavailable('Secure credentials unavailable')
        with self._lock:
            if self._closed or self._resolved: raise NativeUnavailable('Secure credentials unavailable')
            self._resolved = True
            try:
                value = self._exchange_locked('ResolveAccount')
            except Exception:
                self.close()
                raise NativeUnavailable('Secure credentials unavailable') from None
            if value == {'type':'AccountUnavailable','generation':self._generation}:
                raise NativeUnavailable('Secure credentials unavailable')
            try:
                if (set(value) != {'type','generation','api_id','api_hash','database_key'}
                        or value['type'] != 'AccountCredentials' or type(value['api_id']) is not int
                        or not 1 <= value['api_id'] <= 2**31-1
                        or not isinstance(value['api_hash'], str) or re.fullmatch(r'[0-9a-fA-F]{32}', value['api_hash']) is None
                        or not isinstance(value['database_key'], str) or re.fullmatch(r'[0-9a-f]{64}', value['database_key']) is None):
                    raise ValueError
                return {'api_id':value['api_id'], 'api_hash':value['api_hash'], 'database_key':bytes.fromhex(value['database_key'])}
            except Exception:
                self.close()
                raise NativeUnavailable('Secure credentials unavailable') from None

    def launch_inference(self, *, model_id, context_tokens, cpu_threads, auth_token, cancelled):
        if self._root is None or cancelled.is_set(): raise RuntimeUnavailable('inference_unavailable')
        catalog=load_catalog()
        record=next((r for r in catalog['models'] if r['manifest']['id']==model_id),None)
        if record is None: raise RuntimeUnavailable('inference_unavailable')
        value=self._exchange('StartPinnedInference', {'model_id':model_id,'context_tokens':context_tokens,
            'cpu_threads':cpu_threads,'auth_token':auth_token}, timeout=20)
        keys={'type','generation','lease','port','model_id','model_sha256','runtime_commit','platform','archive_sha256','loaded_model_path'}
        artifacts=catalog['runtime']['artifacts']
        loaded = value.get('loaded_model_path')
        source = self._root / 'models' / record['filename']
        valid_loaded = False
        if isinstance(loaded, str) and 1 <= len(loaded) <= 4096 and '\0' not in loaded:
            path = Path(loaded)
            if path.is_absolute() and str(path) == loaded:
                if value.get('platform') == 'macos-arm64':
                    valid_loaded = (path.parent.parent == self._root / 'execution-staging'
                        and re.fullmatch(r'[0-9a-f]{48}', path.parent.name) is not None
                        and path.name == record['filename'])
                elif value.get('platform') == 'windows-x64':
                    valid_loaded = path == source
        if (not valid_loaded or set(value)!=keys or value['type']!='InferenceStarted' or value['model_id']!=model_id
                or value['model_sha256']!=record['manifest']['sha256'] or value['runtime_commit']!=catalog['runtime']['commit']
                or type(value['port']) is not int or not 1<=value['port']<=65535
                or not isinstance(value['lease'],str) or re.fullmatch(r'[0-9a-f]{64}',value['lease']) is None
                or not any(a['platform']==value['platform'] and a['sha256']==value['archive_sha256'] for a in artifacts)):
            self.close(); raise RuntimeUnavailable('inference_unavailable')
        return _InferenceLease(self, value, self._root/'models'/record['filename'])

    def reserve_evaluation(self):
        if self._root is None: raise RuntimeUnavailable('evaluation_unavailable')
        value=self._exchange('ReserveEvaluation')
        if (set(value)!={'type','generation','reservation'} or value['type']!='EvaluationReserved'
                or not isinstance(value['reservation'],str) or re.fullmatch(r'[0-9a-f]{64}',value['reservation']) is None):
            raise RuntimeUnavailable('evaluation_unavailable')
        return _EvaluationReservation(self,value['reservation'])

    def evaluation_hardware_provider(self,reservation):
        from .evaluation.hardware import observe_hardware
        def capture():
            observed=observe_hardware()
            try:
                value=self._exchange('EvaluationHardware',{'reservation':reservation._id},timeout=1)
                expected={'acceleration_mode':'cpu_only','accelerator_model':'not_applicable_cpu_only',
                    'accelerator_memory_bytes':'not_applicable_cpu_only','acceleration_backend':'none_cpu_only','gpu_offload_layers':'0'}
                if (set(value)=={'type','generation','reservation','hardware'} and value['type']=='EvaluationHardware'
                        and value['reservation']==reservation._id and value['hardware']==expected):observed.update(expected)
            except Exception:pass
            return observed
        return capture

    def evaluation_memory_factory(self,reservation):
        if self._observer is None or not isinstance(reservation,_EvaluationReservation) or reservation._owner is not self:
            return None
        from .evaluation.memory import MemoryObservation,MemoryScope,ScopedMemorySampler
        def observe():
            value=self._observer._exchange('SampleEvaluation',{'reservation':reservation._id},timeout=.2)
            if (set(value)!={'type','generation','reservation','worker_rss_bytes','owned_runtime_rss_bytes','accelerator_bytes'}
                    or value['type']!='EvaluationMemory' or value['reservation']!=reservation._id
                    or type(value['worker_rss_bytes']) is not int or value['worker_rss_bytes']<0
                    or type(value['owned_runtime_rss_bytes']) is not int or value['owned_runtime_rss_bytes']<0
                    or value['accelerator_bytes'] is not None): raise ValueError('Scoped observation unavailable')
            return MemoryObservation(value['worker_rss_bytes'],value['owned_runtime_rss_bytes'],None)
        try:observe()  # scope only exists after a real exact-generation sample
        except Exception:return None
        return lambda:ScopedMemorySampler(observe,scope=MemoryScope(True,True,'unavailable'),
            window='cold_preparation_classification_cleanup')

    def close(self):
        if not self._closed:
            self._closed = True
            if self._driver is not None: self._driver.revoke()
            if self._observer is not None: self._observer.close()
            if self._socket is not None:
                try: self._socket.shutdown(socket.SHUT_RDWR)
                except OSError: pass
            try: self._requests.put_nowait(None)
            except queue.Full: pass
            # Closing a platform pipe may block behind an in-flight native read.
            # Revoke immediately and let a daemon closer unblock it; no late
            # response can regain authority or start another request.
            def finish():
                try: self._stream.close()
                except OSError: pass
                if self._socket is not None:
                    try: self._socket.close()
                    except OSError: pass
            Thread(target=finish,name='host-control-close',daemon=True).start()


class _EmbeddedThreadsLease:
    """Private host attachment to one existing view; never a URL/JS/page API."""
    def __init__(self, owner, generation, source):
        self._owner = owner
        self.webview_generation, self.source_session_id = generation, source
        self._closed = self._lost = False

    def _exchange(self, kind, response_type, fields=(), **limits):
        from .collector.browser import BrowserUnavailable
        if self._closed or (self._lost and kind != 'ReleaseEmbeddedThreads'):
            raise BrowserUnavailable('embedded_ownership_lost')
        value = self._owner._exchange(kind, {'webview_generation':self.webview_generation}, **limits)
        if (set(value) != {'type','generation','webview_generation',*fields}
                or value['type'] != response_type or value['generation'] != self._owner._generation
                or type(value['webview_generation']) is not int or value['webview_generation'] != self.webview_generation):
            self._lost = True
            raise BrowserUnavailable('embedded_ownership_lost')
        return value

    def snapshot(self):
        from .collector.embedded import validate_embedded_snapshot
        try:
            value = self._exchange('SnapshotEmbeddedThreads', 'EmbeddedThreadsSnapshot', ('payload',),
                max_response_bytes=1048576)
            return validate_embedded_snapshot(value['payload'])
        except Exception:
            self._lost = True
            raise

    def scroll(self):
        self._exchange('ScrollEmbeddedThreads', 'EmbeddedThreadsScrolled')

    def poll(self):
        if self._closed:
            return 'disconnected'
        if self._lost:
            return 'ownership_lost'
        try:
            value = self._exchange('PollEmbeddedThreads', 'EmbeddedThreadsStatus', ('active',), timeout=.5)
            if type(value['active']) is not bool:
                raise ValueError
            return 'active' if value['active'] else 'disconnected'
        except Exception:
            self._lost = True
            return 'ownership_lost'

    def close(self):
        from .contracts import validate_contract
        if self._closed:
            return {'stopped':True,'remaining':[]}
        try:
            value = self._exchange('ReleaseEmbeddedThreads', 'EmbeddedThreadsReleased', ('report',), timeout=6)
            report = value['report']
            validate_contract('ShutdownReport', report)
            if report == {'stopped':True,'remaining':[]}:
                self._closed = True
                if self._owner._embedded_lease is self:
                    self._owner._embedded_lease = None
            return report
        except Exception:
            return {'stopped':False,'remaining':['embedded_threads']}


class _InferenceLease:
    def __init__(self, owner, value, model_path):
        self._owner, self._id = owner, value['lease']
        self.port, self.model_path = value['port'], model_path
        self.loaded_model_path = Path(value['loaded_model_path'])
        self.model_sha256, self.runtime_commit = value['model_sha256'], value['runtime_commit']
        self.platform, self.archive_sha256 = value['platform'], value['archive_sha256']
        self._closed = False

    def poll(self):
        if self._closed: return 'disconnected'
        try:
            value=self._owner._exchange('PollInference', {'lease':self._id})
            if (set(value)!={'type','generation','lease','active'} or value['type']!='InferenceStatus'
                    or value['lease']!=self._id or type(value['active']) is not bool): raise ValueError
            return 'active' if value['active'] else 'ownership_lost'
        except Exception: return 'ownership_lost'

    def verify_connection(self, raw):
        if self._closed:return False
        try:
            local=raw.getsockname();remote=raw.getpeername()
            if (not isinstance(local,tuple) or len(local)!=2 or local[0]!='127.0.0.1'
                    or type(local[1]) is not int or not 1<=local[1]<=65535
                    or remote!=('127.0.0.1',self.port)):return False
            value=self._owner._exchange('VerifyInferencePeer',{'lease':self._id,
                'client_port':local[1],'server_port':self.port},timeout=1)
            return (set(value)=={'type','generation','lease','verified'} and value['type']=='InferencePeer'
                and value['lease']==self._id and value['verified'] is True)
        except Exception:return False

    def within_resource_limits(self):
        return self.poll() == 'active'

    def close(self):
        if self._closed: return {'stopped':True,'remaining':[]}
        try:
            value=self._owner._exchange('CloseInference', {'lease':self._id},timeout=6)
            report=value.get('report')
            if (set(value)!={'type','generation','lease','report'} or value['type']!='InferenceClosed'
                    or value['lease']!=self._id or not isinstance(report,dict) or set(report)!={'stopped','remaining'}
                    or type(report['stopped']) is not bool or not isinstance(report['remaining'],list)
                    or any(not isinstance(v,str) for v in report['remaining'])
                    or (report['stopped'] and report['remaining'])): raise ValueError
            if report['stopped']: self._closed=True
            return report
        except Exception:
            return {'stopped':False,'remaining':['inference']}


class _DriverWire:
    def __init__(self, stream):
        self._stream=stream
        self._revoked=False
    def read(self, count):
        if self._revoked: return b''
        return self._stream.recv(count) if isinstance(self._stream,socket.socket) else self._stream.read(count)
    def write(self, data):
        if self._revoked: raise OSError('Driver revoked')
        if isinstance(self._stream,socket.socket): self._stream.sendall(data)
        else:
            view=memoryview(data)
            while view:
                count=self._stream.write(view)
                if not count: raise OSError('Driver unavailable')
                view=view[count:]
            self._stream.flush()
    def revoke(self):
        if self._revoked: return
        self._revoked=True
        def close():
            if isinstance(self._stream,socket.socket):
                try:self._stream.shutdown(socket.SHUT_RDWR)
                except OSError:pass
            try:self._stream.close()
            except OSError:pass
        Thread(target=close,name='driver-lane-close',daemon=True).start()


class _BrowserLease:
    def __init__(self, owner, value, driver):
        self._owner,self._id,self.driver=owner,value['lease'],driver
        self.source_session_id=value['source_session_id']
        self._closed=False
    def poll(self):
        if self._closed:return 'disconnected'
        try:
            value=self._owner._exchange('PollBrowser',{'lease':self._id})
            if (set(value)!={'type','generation','lease','active'} or value['type']!='BrowserStatus'
                    or value['lease']!=self._id or type(value['active']) is not bool):raise ValueError
            return 'active' if value['active'] else 'ownership_lost'
        except Exception:return 'ownership_lost'
    def pipe_owned(self):
        return self.poll()=='active'
    def close(self):
        self.driver.revoke()
        if self._closed:return {'stopped':True,'remaining':[]}
        try:
            value=self._owner._exchange('CloseBrowser',{'lease':self._id},timeout=6)
            report=value.get('report')
            if (set(value)!={'type','generation','lease','report'} or value['type']!='BrowserClosed'
                    or value['lease']!=self._id or not isinstance(report,dict) or set(report)!={'stopped','remaining'}
                    or type(report['stopped']) is not bool or not isinstance(report['remaining'],list)
                    or any(not isinstance(v,str) for v in report['remaining'])
                    or (report['stopped'] and report['remaining'])):raise ValueError
            if report['stopped']:self._closed=True
            return report
        except Exception:return {'stopped':False,'remaining':['browser','browser_driver']}


class _EvaluationReservation:
    purpose='evaluation'
    def __init__(self,owner,reservation):
        from .inference.native_launcher import NativeInferenceLauncher
        self._owner,self._id,self._released=owner,reservation,False
        self.launcher=NativeInferenceLauncher(owner._root,owner)
    def active(self):
        if self._released:return False
        try:
            value=self._owner._exchange('PollEvaluation',{'reservation':self._id})
            return (set(value)=={'type','generation','reservation','active'} and value['type']=='EvaluationStatus'
                and value['reservation']==self._id and value['active'] is True)
        except Exception:return False
    def release(self):
        if self._released:return True
        try:
            value=self._owner._exchange('ReleaseEvaluation',{'reservation':self._id})
            ok=(set(value)=={'type','generation','reservation','released'} and value['type']=='EvaluationReleased'
                and value['reservation']==self._id and value['released'] is True)
            if ok:self._released=True
            return ok
        except Exception:return False
