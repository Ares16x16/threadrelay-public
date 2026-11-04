#!/usr/bin/env python3
"""Test-only worker for the real supervisor/Node/CDP native browser fixture.

The worker never launches a subprocess/browser, accepts a CDP address, loads an
account, navigates to a service, or reads a personal profile. FD5 is the exact
private driver lane inherited from the real supervisor.
"""
from __future__ import annotations

import hashlib
import ctypes
import json
import os
from pathlib import Path
import socket
import struct
import sys
import traceback
import time


def send(value: dict) -> None:
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def command() -> dict:
    line = sys.stdin.buffer.readline(65537)
    if not line or len(line) > 65536:
        raise EOFError("Fixture command unavailable")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError("Fixture command shape")
    return value


def main() -> None:
    if len(sys.argv) != 4 or sys.version_info[:3] != (3, 12, 16):
        raise ValueError("Pinned fixture worker input required")
    source, dependencies, project = (Path(value) for value in sys.argv[1:])
    if any(not value.is_absolute() or value.is_symlink() for value in [source, dependencies, project]):
        raise ValueError("Fixed project-local fixture paths required")
    if source != project / "worker/src" or not dependencies.is_relative_to(project / ".cache"):
        raise ValueError("Fixture source/dependency path mismatch")
    os.umask(0o077)
    temporary = Path(os.environ.get("TMPDIR", ""))
    runtime_home = Path(os.environ.get("HOME", ""))
    if (not temporary.is_absolute() or not temporary.is_relative_to(project / ".cache/native-browser/runs")
            or temporary.parts[-3:] != ("native-runtime", "worker", "tmp")
            or runtime_home != temporary.parent / "home"):
        raise ValueError("Host-owned private worker environment required")
    # Observe Foundation's actual process home, which does not follow HOME on
    # macOS. Read one returned string; this never accesses that directory.
    foundation = ctypes.CDLL("/System/Library/Frameworks/Foundation.framework/Foundation")
    core = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    foundation.NSHomeDirectory.argtypes = []
    foundation.NSHomeDirectory.restype = ctypes.c_void_p
    core.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    core.CFStringGetCString.restype = ctypes.c_bool
    native_home = ctypes.create_string_buffer(4096)
    if not core.CFStringGetCString(foundation.NSHomeDirectory(), native_home, len(native_home), 0x08000100):
        raise ValueError("Bounded Foundation home readback unavailable")
    foundation_home = native_home.value.decode("utf-8")
    if foundation_home != str(runtime_home):
        raise ValueError("Foundation home escaped retained private worker home")
    sys.path[:0] = [str(source), str(dependencies)]
    from datetime import datetime, timezone
    from threadrelay.broker_channel import _DriverWire
    from threadrelay.collector.extractor import CARD_SELECTOR, extract_rendered_post
    from threadrelay.collector.owned_playwright import OwnedPlaywright
    from threadrelay.contracts import validate_contract

    stream = socket.socket(fileno=5)
    driver = _DriverWire(stream)
    owned = None
    send({"event": "fixture_worker_ready", "pid": os.getpid(), "python": sys.version.split()[0],
          "private_tmpdir": str(temporary), "private_home": str(runtime_home),
          "foundation_home": foundation_home})
    try:
        request = command()
        if request == {"command": "malformed"}:
            driver.write(struct.pack("<I", 4 * 1024 * 1024 + 1))
            stream.settimeout(8)
            ended = stream.recv(1) == b""
            send({"event": "malformed_rejected", "driver_eof": ended})
            return
        if request != {"command": "start"}:
            raise ValueError("Fixture start required")
        owned = OwnedPlaywright(driver, timeout=15).start()
        browser = owned.browser
        if browser.version != "156.0.8078.4" or len(browser.contexts) != 1:
            raise ValueError("Pinned browser identity/context mismatch")
        context = browser.contexts[0]
        if len(context.pages) != 1:
            raise ValueError("Exclusive initial browser page required")
        # This is fixture-only control. It precedes all content loading and does
        # not turn the product collector into a traffic interceptor.
        blocked = []
        context.route("**/*", lambda route: (blocked.append(route.request.resource_type), route.abort()))
        context.set_offline(True)
        page = context.pages[0]
        page.set_default_timeout(15000)
        if page.url != "about:blank":
            raise ValueError("Unexpected initial navigation")
        session = browser.new_browser_cdp_session()
        version = session.send("Browser.getVersion")
        if version["product"] != "Chrome/156.0.8078.4" or "HeadlessChrome" in version["userAgent"]:
            raise ValueError("Visible full browser required")
        targets = session.send("Target.getTargets")["targetInfos"]
        page_targets = [target for target in targets if target["type"] == "page"]
        if len(page_targets) != 1 or page_targets[0]["url"] != "about:blank":
            raise ValueError("Exclusive blank target required")
        window = session.send("Browser.getWindowForTarget", {"targetId": page_targets[0]["targetId"]})
        bounds = window["bounds"]
        if bounds.get("width", 0) <= 0 or bounds.get("height", 0) <= 0 or bounds.get("windowState") == "minimized":
            raise ValueError("Visible browser window unavailable")
        send({"event": "browser_observed", "browser_version": browser.version,
              "product": version["product"], "headed_user_agent": True, "window_bounds": bounds,
              "contexts": len(browser.contexts), "pages": len(context.pages), "page_url": page.url})
        if command() != {"command": "render"}:
            raise ValueError("Host proof must settle before fixture rendering")
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        observations = []
        expected = {
            "basic": "threads:Code_1", "quotes": "threads:Outer_1",
            "truncated": "threads:Short_1", "evidence-media": "threads:Media_1",
        }
        for name, expected_id in expected.items():
            fixture = project / "fixtures/threads" / (name + ".html")
            content = fixture.read_text()
            def timed(operation, call):
                started = time.monotonic()
                succeeded = False
                try:
                    result = call()
                    succeeded = True
                    return result
                finally:
                    send({"event": "operation_timing", "fixture": name, "operation": operation,
                          "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                          "succeeded": succeeded})

            timed("set_content", lambda: page.set_content(content, wait_until="domcontentloaded", timeout=15000))
            baseline = timed("page_evaluate", lambda: page.evaluate("() => document.readyState"))
            if baseline not in {"interactive", "complete"}:
                raise ValueError("Fixture document baseline unavailable")
            card = page.locator(CARD_SELECTOR).first
            tag = timed("locator_evaluate", lambda: card.evaluate("el => el.tagName"))
            if tag != "ARTICLE":
                raise ValueError("Fixture locator baseline unavailable")
            post = timed("production_extractor", lambda: extract_rendered_post(
                card, source_session_id="native-local-fixture", observed_at=now))
            validate_contract("ObservedPost", post)
            if post["post_id"] != expected_id:
                raise ValueError("Rendered fixture identity mismatch: " + name)
            if name == "basic":
                if post["text"] != "Visible first line.\nSecond line. a visible link" or page.evaluate("window.actions") != []:
                    raise ValueError("Visible text/action isolation mismatch")
            if name == "quotes" and post["quote"]["author"] != "@quoted":
                raise ValueError("Quote attribution mismatch")
            if name == "truncated" and "Never read" in post["text"]:
                raise ValueError("Hidden truncation leaked")
            flags = sorted(value["code"] for value in post["quality_flags"])
            if name == "evidence-media" and flags != ["missing_media_context", "quote_missing_media_context"]:
                raise ValueError("Media uncertainty mismatch")
            observations.append({"fixture": name, "fixture_sha256": hashlib.sha256(content.encode()).hexdigest(),
                                 "post_id": post["post_id"], "quality_flags": flags,
                                 "content_hash": post["content_hash"]})
            send({"event": "fixture_observed", "fixture": name, "post_id": post["post_id"], "quality_flags": flags})
            if page.url != "about:blank":
                raise ValueError("Fixture performed navigation")
        # Local screenshot is requested separately by the host and saved only
        # under its dedicated project evidence directory.
        send({"event": "fixtures_observed", "browser_version": browser.version,
              "product": version["product"], "headed_user_agent": True, "window_bounds": bounds, "page_url": page.url,
              "contexts": len(browser.contexts), "pages": len(context.pages),
              "observations": observations, "blocked_resource_count": len(blocked),
              "production_feed_navigation": "not_exercised"})
        while True:
            request = command()
            kind = request.get("command")
            if kind == "probe":
                try:
                    title = page.title()
                    send({"event": "probe", "connected": True, "title_sha256": hashlib.sha256(title.encode()).hexdigest()})
                except Exception as error:
                    send({"event": "probe", "connected": False, "error_type": type(error).__name__})
            elif kind == "screenshot":
                output = Path(request["path"])
                if not output.is_absolute() or not output.is_relative_to(project / ".cache/native-browser/evidence"):
                    raise ValueError("Screenshot must remain in project browser evidence")
                if output.is_symlink():
                    raise ValueError("Symlink screenshot rejected")
                page.screenshot(path=str(output), full_page=True, timeout=15000)
                send({"event": "screenshot", "sha256": hashlib.sha256(output.read_bytes()).hexdigest()})
            elif kind == "close_driver":
                owned.close()
                owned = None
                send({"event": "driver_closed"})
            elif kind == "exit":
                send({"event": "fixture_worker_exiting"})
                return
            else:
                raise ValueError("Unknown fixture command")
    finally:
        if owned is not None:
            owned.close()
        driver.revoke()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc(file=sys.stderr)
        send({"event": "fixture_error"})
        sys.exit(2)
