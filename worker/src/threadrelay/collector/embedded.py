"""Bounded collector for the host's existing visible embedded Threads view.

This adapter never opens a URL, evaluates script, creates a webview, or adapts a
webview into a generic Playwright page. Only the private fixed host operations
are available. Host acceptance and the current Run intake gate remain separate.
"""

from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
from time import monotonic as monotonic_clock
from threading import Lock

from .browser import (
    BrowserShutdownUnconfirmed,
    BrowserUnavailable,
    MAX_SNAPSHOT_CARDS,
    _feed_url,
)
from .extractor import captured_snapshot_fingerprint, issue, post_from_snapshot, validate_observed_metadata
from threadrelay.contracts import validate_contract

MAX_SNAPSHOT_BYTES = 1000000
MAX_UNSUPPORTED_SCROLLS = 3
RECOVERY_BACKOFF = (1, 2, 3)
_STATES = {
    "ready",
    "manual_login_or_challenge",
    "unexpected_navigation",
    "unknown_layout",
    "snapshot_limit",
}
_CORE = {"author", "author_url", "permalink", "text", "audience", "truncated"}
_OUTER = _CORE | {"quote", "quote_count", "nested_quote"}
_MEDIA = {"missing_media_context", "quote_missing_media_context", "guest_public_context"}


def _raw_card(raw, *, quoted=False):
    if not isinstance(raw, dict):
        raise ValueError("invalid_embedded_card")
    if set(raw) == {"issue"}:
        if raw["issue"] not in {
            "not_visible",
            "unknown_layout",
            "missing_permalink",
            "text_clipped",
            "unsupported_text_layout",
            "partially_visible",
            "text_too_large",
            "snapshot_limit",
        }:
            raise ValueError("invalid_embedded_card")
        issue(raw["issue"])
        return
    required = _CORE if quoted else _OUTER
    if not required <= set(raw) or set(raw) - required - (set() if quoted else _MEDIA | {"observed_metadata"}):
        raise ValueError("invalid_embedded_card")
    for key, maximum in [
        ("author", 500),
        ("author_url", 4096),
        ("permalink", 4096),
        ("text", 100000),
    ]:
        if not isinstance(raw[key], str) or len(raw[key]) > maximum:
            raise ValueError("invalid_embedded_card")
    if (
        not isinstance(raw["audience"], list)
        or len(raw["audience"]) > 100
        or any(not isinstance(v, str) or len(v) > 2000 for v in raw["audience"])
        or type(raw["truncated"]) is not bool
    ):
        raise ValueError("invalid_embedded_card")
    if not quoted:
        if "observed_metadata" in raw:
            validate_observed_metadata(raw["observed_metadata"])
        if (
            type(raw["quote_count"]) is not int
            or not 0 <= raw["quote_count"] <= 100
            or type(raw["nested_quote"]) is not bool
            or any(type(raw[key]) is not bool for key in _MEDIA & raw.keys())
        ):
            raise ValueError("invalid_embedded_card")
        if raw["quote"] is not None:
            _raw_card(raw["quote"], quoted=True)


def validate_embedded_snapshot(value):
    """Validate untrusted page data independently of host extraction and framing."""
    try:
        if (
            not isinstance(value, dict)
            or set(value)
            != {"schema_version", "state", "cards", "scroll_y", "viewport_height"}
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or value["state"] not in _STATES
            or not isinstance(value["cards"], list)
            or len(value["cards"]) > MAX_SNAPSHOT_CARDS
        ):
            raise ValueError
        for key in ["scroll_y", "viewport_height"]:
            if type(value[key]) not in (int, float) or not math.isfinite(value[key]):
                raise ValueError
        if value["viewport_height"] < 0 or (
            value["state"] == "ready" and value["viewport_height"] <= 0
        ):
            raise ValueError
        if value["state"] != "ready" and value["cards"]:
            raise ValueError
        encoded = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > MAX_SNAPSHOT_BYTES:
            raise ValueError
        for raw in value["cards"]:
            _raw_card(raw)
        return deepcopy(value)
    except Exception:
        raise ValueError("invalid_embedded_snapshot") from None


class EmbeddedBrowserSession:
    def __init__(
        self,
        *,
        app_data_dir: Path,
        broker=None,
        feed_url="https://www.threads.com/",
        can_intake: Callable[[], bool] = lambda: False,
        on_attention: Callable[[str], None] = lambda reason: None,
        now=lambda: datetime.now(timezone.utc),
        monotonic: Callable[[], float] = monotonic_clock,
        launcher=None,
    ):
        root = Path(app_data_dir)
        if not root.is_absolute():
            raise ValueError("app_data_dir_must_be_absolute")
        if _feed_url(feed_url) != "https://www.threads.com/":
            raise ValueError("embedded_fixed_feed_required")
        self._profile = root.resolve() / "browser-profiles" / "threads"
        self._broker, self._can_intake, self._on_attention, self._now = (
            broker,
            can_intake,
            on_attention,
            now,
        )
        self._lease = None
        self._paused_reason = None
        self._pending, self._seen, self._issues = {}, {}, []
        self._unsupported_scrolls = 0
        self._unsupported_viewport = False
        self._monotonic = monotonic
        self._last_recovery_clock = None
        self._retry_at = None
        self._recovery_reason = None
        self._recovery_attempt = 0
        self._recovery_scroll_due = False
        self._recovery_projection_lock = Lock()
        self._recovery_projection = None

    @property
    def issues(self):
        return deepcopy(self._issues)

    def open(self, profile_dir):
        if self._lease is not None:
            raise BrowserUnavailable("embedded_cleanup_pending")
        # Compatibility with the high-level preparation API. This path never
        # selects, opens, or navigates the host-owned embedded website data store.
        if (
            not Path(profile_dir).is_absolute()
            or Path(profile_dir).resolve() != self._profile
        ):
            raise ValueError("dedicated_profile_required")
        if self._broker is None:
            raise BrowserUnavailable("embedded_threads_unavailable")
        lease = self._broker.attach_embedded_threads()
        self._lease = lease  # retain before validation or any later cleanup
        try:
            if (
                type(lease.webview_generation) is not int
                or not 1 <= lease.webview_generation <= 2**64 - 1
                or not isinstance(lease.source_session_id, str)
                or re.fullmatch(r"[0-9a-f]{64}", lease.source_session_id) is None
                or lease.poll() != "active"
            ):
                raise BrowserUnavailable("embedded_threads_identity_mismatch")
        except Exception:
            self.close()
            raise
        self._paused_reason = None
        self._pending.clear()
        self._seen.clear()
        self._issues.clear()
        self._unsupported_scrolls = 0
        self._unsupported_viewport = False
        self._last_recovery_clock = None
        self._retry_at = None
        self._recovery_reason = None
        self._recovery_attempt = 0
        self._recovery_scroll_due = False
        self._publish_recovery(None)

    def _publish_recovery(self, snapshot):
        # This lock protects only one immutable metadata tuple. Never hold it
        # across a clock read, session callback or native/runtime operation.
        with self._recovery_projection_lock:
            self._recovery_projection = snapshot

    def _pause(self, reason):
        if self._paused_reason is None:
            self._paused_reason = reason
            self._pending.clear()
            self._retry_at = None
            self._recovery_reason = None
            self._recovery_scroll_due = False
            self._publish_recovery(None)
            self._on_attention(reason)

    def _recovery_now(self):
        try:
            stamp = self._monotonic()
            if (type(stamp) not in (int, float) or not math.isfinite(stamp)
                    or self._last_recovery_clock is not None and stamp < self._last_recovery_clock):
                raise ValueError
            self._last_recovery_clock = stamp
            return stamp
        except Exception:
            self._pause("clock_invalid")
            return None

    def recovery_delay(self):
        """Owner-thread scheduling hint; never grants or restores intake."""
        if self._paused_reason is not None or self._retry_at is None:
            return 0
        stamp = self._recovery_now()
        if stamp is None:
            return 0
        remaining = max(0, self._retry_at - stamp)
        if remaining == 0:
            self._retry_at = None
            self._unsupported_scrolls = 0
            self._recovery_scroll_due = self._recovery_reason in {"unknown_layout", "empty_feed"}
        return remaining

    def recovery_status(self):
        """Pure cross-thread projection; only the collector advances recovery."""
        with self._recovery_projection_lock:
            snapshot = self._recovery_projection
        if snapshot is None:
            return None
        reason, attempt, retry_at = snapshot
        try:
            stamp = self._monotonic()
            if type(stamp) not in (int, float) or not math.isfinite(stamp):
                raise ValueError
            delay = max(0,min(RECOVERY_BACKOFF[-1],math.ceil(retry_at-stamp)))
        except Exception:
            # A display read does not latch a clock failure or close authority.
            # The next owner-thread scheduling check still enforces that gate.
            delay = 0
        return {"reason":reason,"retry_in_seconds":delay,"attempt":attempt}

    def _recover(self, reason):
        stamp = self._recovery_now()
        if stamp is None:
            return
        self._pending.clear()
        self._recovery_reason = reason
        # Once a transient layout's cooldown has elapsed, observation and the
        # pre-scroll snapshot must not perpetually rearm that same cooldown.
        # Only scroll_next can consume this allowance, after fresh authority
        # and page-state checks. It never grants intake or reopens the browser.
        if self._recovery_scroll_due:
            return
        self._recovery_attempt += 1
        self._retry_at = stamp + RECOVERY_BACKOFF[min(self._recovery_attempt - 1, len(RECOVERY_BACKOFF) - 1)]
        self._publish_recovery((self._recovery_reason,self._recovery_attempt,self._retry_at))

    def _ready(self):
        if (
            self._lease is None
            or self._paused_reason is not None
            or not self._can_intake()
        ):
            return False
        try:
            status = self._lease.poll()
        except Exception:
            status = "ownership_lost"
        if status != "active":
            self._pause(
                "browser_lost" if status == "disconnected" else "browser_ownership_lost"
            )
            return False
        return self._can_intake()

    def _capture(self):
        if self.recovery_delay() > 0 or not self._ready():
            return
        try:
            value = validate_embedded_snapshot(self._lease.snapshot())
            # A late snapshot after Pause/Stop cannot admit post data.
            if not self._ready():
                self._pending.clear()
                return
            if value["state"] != "ready":
                self._issues = (
                    [issue(value["state"])]
                    if value["state"] in {"unknown_layout", "snapshot_limit"}
                    else []
                )
                if value["state"] == "unknown_layout":
                    self._recover("unknown_layout")
                else:
                    self._pause(value["state"])
                return
            if not value["cards"]:
                self._issues = [issue("unknown_layout")]
                self._recover("empty_feed")
                return
            posts = [
                post_from_snapshot(
                    raw,
                    source_session_id=self._lease.source_session_id,
                    observed_at=self._now(),
                )
                for raw in value["cards"]
            ]
            self._issues = [
                p for p in posts if "code" in p and p["code"] != "not_visible"
            ]
            # A recognized nonempty envelope uses the ordinary scroll budget.
            # Transient global layout gets one guarded scroll after cooldown;
            # login/navigation/size still stop before any feed movement.
            # Unsupported cards never become posts: keep their issues and try
            # a few ordinary fixed scrolls to reach a supported visible card.
            fatal = next(
                (
                    p["code"]
                    for p in self._issues
                    if p["code"] == "snapshot_limit"
                ),
                None,
            )
            if fatal:
                self._pause(fatal)
                return
            self._recovery_scroll_due = False
            self._unsupported_viewport = not any("post_id" in post for post in posts)
            if not self._unsupported_viewport:
                self._unsupported_scrolls = 0
                self._recovery_reason = None
                self._recovery_attempt = 0
                self._retry_at = None
                self._publish_recovery(None)
            elif self._unsupported_scrolls >= MAX_UNSUPPORTED_SCROLLS:
                self._recover("unsupported_cards")
                return
            if not self._ready():
                self._pending.clear()
                return
            for post in posts:
                if "post_id" not in post:
                    continue
                key = post["post_id"]
                if self._seen.get(key) == captured_snapshot_fingerprint(post):
                    self._pending.pop(key, None)
                elif (
                    len(self._pending) >= MAX_SNAPSHOT_CARDS
                    and key not in self._pending
                ):
                    self._pause("snapshot_limit")
                    return
                else:
                    self._pending[key] = post
        except Exception:
            self._pause("browser_ownership_lost")

    def observe(self):
        self._capture()
        if not self._ready():
            self._pending.clear()
            return []
        posts = list(self._pending.values())
        self._pending.clear()
        for post in posts:
            self._seen[post["post_id"]] = captured_snapshot_fingerprint(post)
        return posts

    def scroll_next(self):
        if self.recovery_delay() > 0:
            return
        self._capture()  # collect before virtualized DOM cards can disappear
        if self.recovery_delay() > 0 or not self._ready():
            return
        try:
            recovery_reason = self._recovery_reason if self._recovery_scroll_due else None
            self._recovery_scroll_due = False
            self._lease.scroll()
            if not self._ready():
                return
            if recovery_reason is not None:
                # Exactly one fixed host scroll per elapsed layout cooldown.
                # Wait for the new viewport to settle before admitting data.
                self._recover(recovery_reason)
            elif self._unsupported_viewport:
                # observe() and pre-scroll capture can inspect the same DOM.
                # Charge actual scrolls, so duplicate reads cannot exhaust the
                # recovery budget before the viewport has a chance to change.
                self._unsupported_scrolls += 1
        except Exception:
            self._pause("browser_ownership_lost")

    def close(self):
        if self._lease is None:
            return
        self._paused_reason = self._paused_reason or "browser_closed"
        self._pending.clear()
        self._recovery_scroll_due = False
        self._publish_recovery(None)
        try:
            report = self._lease.close()
            validate_contract("ShutdownReport", report)
            if report != {"stopped": True, "remaining": []}:
                raise BrowserShutdownUnconfirmed("browser_shutdown_unconfirmed")
        except Exception as error:
            self._paused_reason = "browser_shutdown_unconfirmed"
            self._on_attention("browser_shutdown_unconfirmed")
            raise BrowserShutdownUnconfirmed("browser_shutdown_unconfirmed") from error
        self._lease = None
