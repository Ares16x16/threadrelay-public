"""Launcher source contracts with synthetic brokers, never native proof."""
from pathlib import Path
from threading import Event
import pytest
from threadrelay.inference.provision import load_catalog
from threadrelay.inference.runtime import RuntimeLaunchSpec, RuntimeUnavailable


def clear_driver_overrides(monkeypatch):
    import os
    for key in list(os.environ):
        if key.upper().endswith('_PROXY') or key.startswith(('DYLD_', 'PLAYWRIGHT_', 'PYTHON', 'NODE_')) or key in {'DEBUG','DEBUGP','PWDEBUG','LD_PRELOAD','LD_LIBRARY_PATH'}:
            monkeypatch.delenv(key, raising=False)


def test_inference_launcher_allows_only_fixed_catalog_and_internal_broker(tmp_path):
    from threadrelay.inference.native_launcher import NativeInferenceLauncher
    catalog = load_catalog(); record = catalog['models'][0]; manifest = record['manifest']
    calls = []
    class Broker:
        def launch_inference(self, **kwargs): calls.append(kwargs); return object()
    launcher = NativeInferenceLauncher(tmp_path, Broker())
    spec = RuntimeLaunchSpec(tmp_path/'models'/record['filename'],manifest['id'],manifest['sha256'],
        catalog['runtime']['commit'],4096,4,'x'*32,Event())
    assert launcher.launch(spec) is not None
    assert calls[0]['model_id'] == manifest['id'] and 'path' not in calls[0] and 'argv' not in calls[0]
    from dataclasses import replace
    with pytest.raises(RuntimeUnavailable): launcher.launch(replace(spec, model_file=Path('/tmp/other')))
    with pytest.raises(RuntimeUnavailable): launcher.launch(replace(spec, host='localhost'))
    with pytest.raises(RuntimeUnavailable): launcher.launch(replace(spec, offline=False))
    assert len(calls) == 1


def test_browser_adapter_uses_only_broker_owned_prelaunched_browser_and_closes(tmp_path, monkeypatch):
    from threadrelay.collector.native_launcher import NativeBrowserLauncher
    clear_driver_overrides(monkeypatch)
    profile = tmp_path/'browser-profiles'/'threads'; calls = []
    class NativeLease:
        source_session_id = 'synthetic-generation'
        def poll(self): return 'active'
        def pipe_owned(self): return True
        def close(self): calls.append('native-close'); return {'stopped':True,'remaining':[]}
    class Broker:
        def driver_containment_verified(self): return True
        def launch_browser(self): calls.append('broker-launch'); return NativeLease()
    class Page:
        def __init__(self, context): self.context = context
        def goto(self, url, **kwargs): calls.append(('goto',url))
    class Context:
        def __init__(self): self.pages = [Page(self)]
    class Browser:
        def __init__(self): self.contexts = [Context()]
        def close(self): calls.append('browser-close')
    class Playwright:
        browser = Browser()
        def stop(self): calls.append('driver-stop')
    class Factory:
        def start(self): return Playwright()
    launcher = NativeBrowserLauncher(tmp_path, Broker(), playwright_factory=lambda:Factory())
    lease = launcher.open(profile, feed_url='https://www.threads.com/')
    assert ('goto','https://www.threads.com/') in calls
    assert lease.close() == {'stopped':True,'remaining':[]}
    assert 'native-close' in calls and 'driver-stop' in calls
    with pytest.raises(ValueError): launcher.open(tmp_path/'elsewhere', feed_url='https://www.threads.com/')


def test_bad_broker_lease_is_retained_before_validation_and_closed(tmp_path, monkeypatch):
    from threadrelay.collector.native_launcher import NativeBrowserLauncher
    from threadrelay.collector.browser import BrowserUnavailable
    clear_driver_overrides(monkeypatch)
    closed = []
    class Lease:
        @property
        def source_session_id(self): raise RuntimeError('private')
        def close(self): closed.append(True); return {'stopped':True,'remaining':[]}
    class Broker:
        def driver_containment_verified(self): return True
        def launch_browser(self): return Lease()
    launcher = NativeBrowserLauncher(tmp_path, Broker(), playwright_factory=lambda: None)
    with pytest.raises(BrowserUnavailable): launcher.open(tmp_path/'browser-profiles'/'threads', feed_url='https://www.threads.com/')
    assert closed == [True]


def test_browser_driver_rejects_inherited_proxy_before_native_launch(tmp_path, monkeypatch):
    from threadrelay.collector.native_launcher import NativeBrowserLauncher
    from threadrelay.collector.browser import BrowserUnavailable
    clear_driver_overrides(monkeypatch)
    monkeypatch.setenv('HTTP_PROXY','http://synthetic.invalid')
    class Broker:
        def driver_containment_verified(self): return True
        def launch_browser(self): pytest.fail('unsafe environment reached launcher')
    with pytest.raises(BrowserUnavailable):
        NativeBrowserLauncher(tmp_path, Broker()).open(tmp_path/'browser-profiles'/'threads', feed_url='https://www.threads.com/')


def test_browser_never_uses_stock_unowned_playwright_driver(tmp_path, monkeypatch):
    from threadrelay.collector.native_launcher import NativeBrowserLauncher
    from threadrelay.collector.browser import BrowserUnavailable
    clear_driver_overrides(monkeypatch)
    class Broker:
        def owned_driver_transport_available(self): return False
        def driver_containment_verified(self): return True
        def launch_browser(self): pytest.fail('no owned driver transport available')
    with pytest.raises(BrowserUnavailable):
        NativeBrowserLauncher(tmp_path, Broker()).open(tmp_path/'browser-profiles'/'threads', feed_url='https://www.threads.com/')
