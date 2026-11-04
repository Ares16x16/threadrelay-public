"""Attach Playwright only to a pre-launched browser over host-owned CDP pipes.

The host broker must establish every native process group and exact endpoint
identity before returning a capability. This adapter cannot create acceptance
proof; no default broker or production bypass is supplied.
"""
import os
from pathlib import Path
from threading import get_ident

from .browser import BrowserUnavailable, _feed_url


_UNSAFE_ENV = {'DEBUGP', 'DEBUG', 'INTERNAL_PW_GREENLET_DEBUG', 'PWDEBUG', 'PLAYWRIGHT_NODEJS_PATH', 'PLAYWRIGHT_BROWSERS_PATH',
               'NODE_OPTIONS', 'NODE_PATH', 'LD_PRELOAD', 'LD_LIBRARY_PATH', 'PYTHONPATH', 'PYTHONHOME'}


class _OwnedBrowser:
    def __init__(self, native, profile, clear):
        self._native, self.profile_dir, self._clear = native, profile, clear
        self._owner = get_ident()
        self._driver = self._browser = None
        self.context = self.page = None
        self.source_session_id = None
        self._native_closed = False

    def _same_thread(self):
        if get_ident() != self._owner:
            raise BrowserUnavailable('browser_owner_thread_mismatch')

    def poll(self):
        self._same_thread()
        try:
            return 'active' if self._native.poll() == 'active' and self._native.pipe_owned() else 'ownership_lost'
        except Exception:
            return 'ownership_lost'

    def close(self):
        self._same_thread()
        failed = False
        if self._browser is not None:
            try:
                self._browser.close()
                self._browser = None
            except Exception:
                failed = True
        if self._driver is not None:
            try:
                self._driver.stop()
                self._driver = None
            except Exception:
                failed = True
        if not self._native_closed:
            try:
                report = self._native.close()
                self._native_closed = report == {'stopped': True, 'remaining': []}
            except Exception:
                self._native_closed = False
        if not self._native_closed or failed:
            return {'stopped': False, 'remaining': ['browser']}
        self._clear(self)
        return {'stopped': True, 'remaining': []}


class NativeBrowserLauncher:
    def __init__(self, app_data_root: Path, broker, *, playwright_factory=None):
        root = Path(app_data_root)
        if not root.is_absolute():
            raise ValueError('Trusted app root required')
        self._profile = root.resolve() / 'browser-profiles' / 'threads'
        self._broker, self._playwright_factory = broker, playwright_factory
        self._lease = None

    def _clear(self, lease):
        if self._lease is lease:
            self._lease = None

    def close_pending(self):
        if self._lease is None:
            return {'stopped': True, 'remaining': []}
        return self._lease.close()

    def open(self, profile_dir: Path, *, feed_url: str):
        url = _feed_url(feed_url)
        if (not profile_dir.is_absolute() or profile_dir.resolve() != self._profile
                or any(p.is_symlink() for p in (profile_dir, profile_dir.parent))):
            raise ValueError('dedicated_profile_required')
        if self._lease is not None:
            raise BrowserUnavailable('browser_cleanup_pending')
        if any(key in os.environ for key in _UNSAFE_ENV) or any(key.startswith(('DYLD_', 'PLAYWRIGHT_', 'PYTHON', 'NODE_')) or key.upper().endswith('_PROXY') for key in os.environ):
            raise BrowserUnavailable('unsafe_driver_environment')
        if self._playwright_factory is None and not self._broker.owned_driver_transport_available():
            raise BrowserUnavailable('owned_driver_transport_unavailable')
        if not self._broker.driver_containment_verified():
            raise BrowserUnavailable('driver_containment_unverified')
        native = self._broker.launch_browser()
        lease = _OwnedBrowser(native, self._profile, self._clear)
        self._lease = lease  # retain ownership before validation or attachment
        try:
            lease.source_session_id = native.source_session_id
            if (lease.poll() != 'active'
                    or not isinstance(lease.source_session_id, str) or not 1 <= len(lease.source_session_id) <= 128):
                raise BrowserUnavailable('browser_endpoint_unverified')
            if self._playwright_factory is None:
                from .owned_playwright import OwnedPlaywright
                lease._driver = OwnedPlaywright(native.driver).start()
            else:
                lease._driver = self._playwright_factory().start()
            lease._browser = lease._driver.browser
            if lease.poll() != 'active' or len(lease._browser.contexts) != 1:
                raise BrowserUnavailable('browser_endpoint_unverified')
            lease.context = lease._browser.contexts[0]
            if len(lease.context.pages) != 1:
                raise BrowserUnavailable('browser_context_unavailable')
            lease.page = lease.context.pages[0]
            lease.page.goto(url, wait_until='domcontentloaded', timeout=15000)
            if lease.poll() != 'active':
                raise BrowserUnavailable('browser_endpoint_unverified')
            return lease
        except Exception:
            lease.close()
            raise BrowserUnavailable('owned_browser_unavailable') from None
