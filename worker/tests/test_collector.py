"""Synthetic rendered DOM only; these semantics are NOT verified Threads selectors."""
from datetime import datetime, timezone
from asyncio import CancelledError
from pathlib import Path
import os

import pytest
from playwright.sync_api import sync_playwright

from threadrelay.collector.canonicalize import canonicalize_observed_link
from threadrelay.collector.extractor import CARD_SELECTOR, extract_rendered_post
from threadrelay.contracts import validate_contract

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def browser():
    if os.environ.get("THREADRELAY_RUN_BROWSER_TESTS") != "1":
        pytest.skip("rendered Chromium tests require explicit THREADRELAY_RUN_BROWSER_TESTS=1; current sandbox blocks fresh Chromium socket and escalation")
    # Test-only launch, never a production ownership adapter. No live URLs/data.
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=os.environ.get("THREADRELAY_TEST_CHROMIUM"))
        yield browser
        browser.close()


@pytest.fixture
def page(browser):
    context = browser.new_context(viewport={"width": 900, "height": 900})
    # Fixture-only deny network; the product collector does not intercept traffic.
    context.route("**/*", lambda route: route.abort())
    page = context.new_page()
    yield page
    context.close()


def fixture(page, name):
    page.set_content((ROOT / "fixtures" / "threads" / f"{name}.html").read_text())
    return page.locator("main [role=feed] > article").first


def extract(card, **kwargs):
    return extract_rendered_post(card, source_session_id="synthetic-session", observed_at=NOW, **kwargs)


@pytest.mark.parametrize("url", [
    "", "https://example.com/post/123", "http://www.threads.com/@a/post/A",
    "https://www.threads.com.evil.test/@a/post/A", "https://evil@www.threads.com/@a/post/A",
    "https://www.threads.com:444/@a/post/A", "https://www.threads.com/@a",
    "https://www.threads.com/@a/post/A/other", "https://www.threads.com/@a/post/%41",
    "https://www.threads.com/@a/post/A\\evil", "https://www.threads.com/t/A",
    "//www.threads.com/@a/post/A", " https://www.threads.com/@a/post/A",
    "https://www.threads.com/@a/post/A\n", "https://www.threads.com/@a/post/A?bad=%0A",
])
def test_missing_or_non_observed_permalink_is_rejected(url):
    assert canonicalize_observed_link(url) is None


def test_aliases_and_tracking_keep_post_identity_without_inventing_a_host():
    first = canonicalize_observed_link("https://www.threads.com/@alice/post/Code_1?x=1#fragment")
    second = canonicalize_observed_link("https://threads.net/@ALICE/post/Code_1/")
    assert first == {"post_id": "threads:Code_1", "permalink": "https://www.threads.com/@alice/post/Code_1"}
    assert second == {"post_id": "threads:Code_1", "permalink": "https://threads.net/@alice/post/Code_1"}
    validate_contract("PostIdentity", first)


def test_only_scoped_visible_text_is_extracted_and_visibility_is_not_assumed(page):
    post = extract(fixture(page, "basic"))
    validate_contract("ObservedPost", post)
    assert post["post_id"] == "threads:Code_1"
    assert post["author"] == "@alice"
    assert post["text"] == "Visible first line.\nSecond line. a visible link"
    assert post["visibility"] == "unknown"
    assert post["quote"] is None
    assert {f["code"] for f in post["quality_flags"]} == {"visibility_unverified"}
    assert page.evaluate("window.actions") == []
    assert page.url == "about:blank"


def test_hash_binds_content_and_attribution_not_observation_time_or_session(page):
    card = fixture(page, "quotes")
    first = extract(card)
    repeated = extract_rendered_post(card, source_session_id="another-session", observed_at=datetime(2026, 2, 1, tzinfo=timezone.utc))
    assert first["post_id"] == repeated["post_id"]
    assert first["content_hash"] == repeated["content_hash"]
    page.locator('blockquote section').evaluate("el => el.textContent='An edited quoted claim.'")
    assert extract(card)["content_hash"] != first["content_hash"]


def test_nested_quote_has_explicit_attribution_not_outer_author_text(page):
    post = extract(fixture(page, "quotes"))
    assert post["text"] == "I disagree with the quoted claim."
    assert post["quote"] == {"author": "@quoted", "text": "The quoted claim.", "permalink": "https://www.threads.com/@quoted/post/Quoted_1"}
    assert post["visibility"] == "public"


def test_visible_truncation_warns_without_expansion_and_keeps_restricted_visibility(page):
    post = extract(fixture(page, "truncated"))
    assert post["text"] == "Only the visible excerpt…"
    assert post["visibility"] == "restricted"
    assert "truncated" in {f["code"] for f in post["quality_flags"]}
    assert page.evaluate("window.actions") == []


def test_unknown_layout_returns_issue_not_broad_text(page):
    issue = extract(fixture(page, "unknown-layout"))
    validate_contract("ExtractionIssue", issue)
    assert issue["code"] == "unknown_layout"
    assert "Do not dump" not in issue["detail"]


def test_permalink_must_be_the_visible_outer_header_timestamp_link(page):
    card = fixture(page, "quotes")
    page.locator("article > header > a:has(time)").evaluate("el => el.remove()")
    assert extract(card)["code"] == "missing_permalink"
    # Nested quote permalink is not promoted to the outer post identity.


def test_hidden_permalink_or_author_conflict_cannot_supply_outer_identity(page):
    card = fixture(page, "basic")
    page.locator("article > header > a:has(time)").first.evaluate("el => el.hidden=true")
    assert extract(card)["code"] == "missing_permalink"
    fixture(page, "basic")
    page.locator("article > header > a[rel=author]").first.evaluate("el => el.href='https://www.threads.com/@someone_else'")
    assert extract(page.locator("article").first)["code"] == "author_mismatch"


def test_css_clipped_text_is_rejected_not_read_from_hidden_overflow(page):
    card = fixture(page, "basic")
    page.locator('section').first.evaluate("el => {el.style.height='5px'; el.style.overflow='hidden'}")
    assert extract(card)["code"] == "text_clipped"


def test_ambiguous_or_unknown_audience_remains_unknown(page):
    card = fixture(page, "quotes")
    page.locator('article > header [aria-label=Audience]').evaluate("el => el.textContent='Everyone maybe'")
    assert extract(card)["visibility"] == "unknown"


def test_quote_unknown_layout_is_deliberately_omitted_with_warning(page):
    card = fixture(page, "quotes")
    page.locator("blockquote header").evaluate("el => el.remove()")
    post = extract(card)
    assert post["quote"] is None
    assert "quote_omitted" in {f["code"] for f in post["quality_flags"]}
    assert post["text"] == "I disagree with the quoted claim."


def raw_snapshot(**changes):
    return {"author": "@alice", "author_url": "https://www.threads.com/@alice", "permalink": "https://www.threads.com/@alice/post/Code_1", "text": "A bounded visible post.", "audience": [], "truncated": False, "quote": None, "quote_count": 0, "nested_quote": False, **changes}


def test_snapshot_conversion_validates_provenance_and_hash_without_browser():
    from threadrelay.collector.extractor import post_from_snapshot
    first = post_from_snapshot(raw_snapshot(), source_session_id="session-a", observed_at=NOW)
    later = post_from_snapshot(raw_snapshot(), source_session_id="session-b", observed_at=datetime(2026, 2, 1, tzinfo=timezone.utc))
    validate_contract("ObservedPost", first)
    assert first["visibility"] == "unknown"
    assert first["content_hash"] == later["content_hash"]
    edited = post_from_snapshot(raw_snapshot(text="Edited."), source_session_id="session-a", observed_at=NOW)
    assert edited["content_hash"] != first["content_hash"]
    assert post_from_snapshot(raw_snapshot(permalink="https://example.com/post/1"), source_session_id="s", observed_at=NOW)["code"] == "missing_permalink"
    assert post_from_snapshot(raw_snapshot(author="@impostor"), source_session_id="s", observed_at=NOW)["code"] == "author_mismatch"
    with pytest.raises(ValueError):
        post_from_snapshot(raw_snapshot(), source_session_id="s", observed_at=datetime(2026, 1, 1))


def test_snapshot_quote_and_unsafe_audience_do_not_become_outer_author_claim():
    from threadrelay.collector.extractor import post_from_snapshot
    quoted = raw_snapshot(author="@quoted", author_url="https://www.threads.net/@quoted", permalink="https://www.threads.net/@quoted/post/Q1", text="A claim", truncated=True)
    post = post_from_snapshot(raw_snapshot(quote=quoted, quote_count=1, audience=["Public", "Followers only"], nested_quote=True), source_session_id="s", observed_at=NOW)
    assert post["text"] == "A bounded visible post."
    assert post["visibility"] == "unknown"
    assert post["quote"] == {"author": "@quoted", "text": "A claim", "permalink": "https://www.threads.net/@quoted/post/Q1"}
    assert {f["code"] for f in post["quality_flags"]} == {"visibility_unverified", "quote_truncated", "nested_quote_omitted"}


class FixturePage:
    """Only external browser effects are doubled; collector logic stays real."""
    def __init__(self, snapshots=None):
        self.url = "https://www.threads.com/"
        self.context = object()
        self.snapshots = snapshots if snapshots is not None else [raw_snapshot()]
        self.challenge = False
        self.scrolls = 0
        self.closed = False

    def is_closed(self):
        return self.closed

    def evaluate(self, expression):
        if "scrollBy" in expression:
            self.scrolls += 1
            self.snapshots = [raw_snapshot(permalink="https://www.threads.com/@alice/post/Second", text="After virtualization.")]
            return None
        return self.challenge

    def locator(self, selector):
        assert selector == CARD_SELECTOR
        return self

    def evaluate_all(self, expression):
        return [dict(s) for s in self.snapshots]


class FixtureLease:
    def __init__(self, page, profile_dir):
        self.page = page
        self.context = page.context
        self.profile_dir = profile_dir
        self.source_session_id = "fixture-owned-session"
        self.status = "active"
        self.close_result = {"stopped": True, "remaining": []}
        self.closed = False

    def poll(self):
        return self.status

    def close(self):
        self.closed = True
        return self.close_result


class FixtureLauncher:
    def __init__(self, page):
        self.page = page
        self.leases = []

    def open(self, profile_dir, *, feed_url):
        assert feed_url == "https://www.threads.com/"
        lease = FixtureLease(self.page, profile_dir)
        self.leases.append(lease)
        return lease


def session(tmp_path, page=None, can_intake=lambda: True):
    from threadrelay.collector.browser import BrowserSession
    launcher = FixtureLauncher(page or FixturePage())
    reasons = []
    root = tmp_path / "private-app-data"
    browser = BrowserSession(app_data_dir=root, launcher=launcher, can_intake=can_intake, on_attention=reasons.append, now=lambda: NOW)
    browser.open(root / "browser-profiles" / "threads")
    return browser, launcher, reasons


def test_production_default_does_not_launch_and_profile_cannot_be_arbitrary(tmp_path):
    from threadrelay.collector.browser import BrowserSession, BrowserUnavailable
    with pytest.raises(BrowserUnavailable, match="native_owned_browser_launcher_unavailable"):
        BrowserSession(app_data_dir=tmp_path).open(tmp_path / "browser-profiles" / "threads")
    launcher = FixtureLauncher(FixturePage())
    with pytest.raises(ValueError):
        BrowserSession(app_data_dir=tmp_path, launcher=launcher).open(tmp_path / "Chrome" / "User Data")
    assert launcher.leases == []


def test_non_running_session_never_reads_or_scrolls(tmp_path):
    page = FixturePage()
    browser, launcher, reasons = session(tmp_path, page, can_intake=lambda: False)
    assert browser.observe() == []
    browser.scroll_next()
    assert page.scrolls == 0
    assert reasons == []
    browser.close()
    assert launcher.leases[0].closed


def test_virtualized_snapshot_is_captured_before_scroll_and_duplicates_are_stable(tmp_path):
    browser, launcher, _ = session(tmp_path)
    browser.scroll_next()  # virtualizes first card before caller observes again
    posts = browser.observe()
    assert [(p["post_id"], p["text"]) for p in posts] == [("threads:Code_1", "A bounded visible post."), ("threads:Second", "After virtualization.")]
    assert all(p["source_session_id"] == "fixture-owned-session" for p in posts)
    assert browser.observe() == []
    launcher.page.snapshots[0]["text"] = "An edited snapshot."
    assert browser.observe()[0]["post_id"] == "threads:Second"
    browser.close()


@pytest.mark.parametrize("cause,reason", [("challenge", "manual_login_or_challenge"), ("disconnected", "browser_lost"), ("ownership_lost", "browser_ownership_lost"), ("unknown_layout", "unknown_layout"), ("external_url", "unexpected_navigation")])
def test_challenge_browser_loss_or_unknown_layout_pauses_without_relaunch(tmp_path, cause, reason):
    browser, launcher, reasons = session(tmp_path)
    if cause == "challenge":
        launcher.page.challenge = True
    elif cause in {"disconnected", "ownership_lost"}:
        launcher.leases[0].status = cause
    elif cause == "unknown_layout":
        launcher.page.snapshots = [{"issue": "unknown_layout"}]
    else:
        launcher.page.url = "https://example.com/embedded-link"
    assert browser.observe() == []
    browser.scroll_next()
    assert browser.observe() == []
    assert reasons == [reason]
    assert launcher.page.scrolls == 0
    assert len(launcher.leases) == 1
    browser.close()


def test_unconfirmed_close_keeps_owned_shutdown_obligation(tmp_path):
    from threadrelay.collector.browser import BrowserShutdownUnconfirmed
    browser, launcher, reasons = session(tmp_path)
    launcher.leases[0].close_result = {"stopped": False, "remaining": ["browser"]}
    with pytest.raises(BrowserShutdownUnconfirmed):
        browser.close()
    assert browser.observe() == []
    assert reasons == ["browser_shutdown_unconfirmed"]
    launcher.leases[0].close_result = {"stopped": True, "remaining": []}
    browser.close()


def test_lease_identity_cannot_point_to_another_profile_or_context(tmp_path):
    from threadrelay.collector.browser import BrowserSession, BrowserUnavailable
    class WrongProfile(FixtureLauncher):
        def open(self, profile_dir, *, feed_url):
            lease = super().open(profile_dir, feed_url=feed_url)
            lease.profile_dir = tmp_path / "unrelated"
            return lease
    launcher = WrongProfile(FixturePage())
    with pytest.raises(BrowserUnavailable, match="owned_browser_identity_mismatch"):
        BrowserSession(app_data_dir=tmp_path, launcher=launcher).open(tmp_path / "browser-profiles" / "threads")
    assert launcher.leases[0].closed


@pytest.mark.parametrize("url", ["https://example.com/", "http://www.threads.com/", "https://www.threads.com/@a/post/A", "https://www.threads.com/accounts/delete", "https://www.threads.com/?next=https://evil.test", "https://www.threads.com:443/"])
def test_feed_navigation_is_allowlisted_before_activation(tmp_path, url):
    from threadrelay.collector.browser import BrowserSession
    launcher = FixtureLauncher(FixturePage())
    with pytest.raises(ValueError, match="unsupported_feed_url"):
        BrowserSession(app_data_dir=tmp_path, launcher=launcher, feed_url=url)
    assert launcher.leases == []


def test_ownership_probe_failure_pauses_instead_of_leaving_pipeline_running(tmp_path):
    browser, launcher, reasons = session(tmp_path)
    def unavailable():
        raise OSError("synthetic identity service unavailable")
    launcher.leases[0].poll = unavailable
    assert browser.observe() == []
    assert reasons == ["browser_ownership_lost"]
    assert launcher.page.scrolls == 0
    browser.close()


def test_unknown_layout_closes_actual_session_controller_gate(tmp_path):
    from threadrelay.collector.browser import BrowserSession
    from threadrelay.session import SessionController
    controller = SessionController(setup_check=lambda _: {"allowed": True, "reason": None})
    assert controller.start({"feed_url": "https://www.threads.com/"})["state"] == "running"
    launcher = FixtureLauncher(FixturePage([{"issue": "unknown_layout"}]))
    browser = BrowserSession(app_data_dir=tmp_path, launcher=launcher, can_intake=controller.can_intake, on_attention=controller.pause, now=lambda: NOW)
    browser.open(tmp_path / "browser-profiles" / "threads")
    assert browser.observe() == []
    assert controller.snapshot["state"] == "paused"
    assert controller.snapshot["attention_reason"] == "unknown_layout"
    assert not controller.can_transmit(NOW)
    browser.close()


def test_snapshot_count_and_buffer_are_bounded_without_broad_extraction(tmp_path):
    browser, launcher, reasons = session(tmp_path, FixturePage([raw_snapshot() for _ in range(101)]))
    assert browser.observe() == []
    assert reasons == ["snapshot_limit"]
    assert browser.issues == [{"code": "snapshot_limit", "detail": "snapshot limit"}]
    browser.close()


def test_stop_during_snapshot_cannot_emit_or_scroll(tmp_path):
    gate = [True]
    page = FixturePage()
    original = page.evaluate_all
    def close_gate(expression):
        result = original(expression)
        gate[0] = False
        return result
    page.evaluate_all = close_gate
    browser, _, _ = session(tmp_path, page, can_intake=lambda: gate[0])
    browser.scroll_next()
    assert browser.observe() == []
    assert page.scrolls == 0
    browser.close()


def test_login_navigation_pauses_without_interaction(tmp_path):
    browser, launcher, reasons = session(tmp_path)
    launcher.page.url = "https://www.threads.com/accounts/login/"
    assert browser.observe() == []
    assert reasons == ["manual_login_or_challenge"]
    browser.close()


def test_offscreen_or_css_masked_descendant_is_not_read_as_visible_text(page):
    card = fixture(page, "basic")
    page.locator('section').first.evaluate("el => el.insertAdjacentHTML('beforeend', '<span style=\"position:absolute;left:-5000px\">offscreen secret</span>')")
    assert extract(card)["code"] == "unsupported_text_layout"


def test_visible_post_is_snapshotted_before_real_dom_virtualization(page):
    from threadrelay.collector.extractor import CARD_SELECTOR, RENDERED_SNAPSHOT_JS, post_from_snapshot
    fixture(page, "virtualized")
    snapshots = page.locator(CARD_SELECTOR).evaluate_all(f"cards => cards.map({RENDERED_SNAPSHOT_JS})")
    page.evaluate("window.scrollBy(0, 500)")
    page.wait_for_function("document.querySelector('[role=feed]').dataset.changed === 'yes'")
    assert post_from_snapshot(snapshots[0], source_session_id="s", observed_at=NOW)["post_id"] == "threads:First_1"
    assert extract(page.locator(CARD_SELECTOR).first)["post_id"] == "threads:Second_1"


def test_extractor_requires_real_source_session_provenance_before_any_dom_read():
    class UnreadCard:
        def evaluate(self, expression):
            raise AssertionError("missing provenance must not read DOM")
    result = extract_rendered_post(UnreadCard())
    assert result == {"code": "source_session_required", "detail": "source session required"}
    validate_contract("ExtractionIssue", result)


# Actual JavaScript execution with DOM/geometry doubles, NOT rendered acceptance.
def dom_snapshot(*, author_style="", link_style="", time_style="", audience_style="", header_style="", header_attrs="", header_rect="10,10,100,20", extra="", body_style="", body_html="A bounded visible post."):
    import json
    import subprocess
    from threadrelay.collector.extractor import RENDERED_SNAPSHOT_JS
    html = f'''<main><div role="feed"><article>
<header style="{header_style}" {header_attrs} data-test-rect="{header_rect}">
<a rel="author" href="https://www.threads.com/@alice" {author_style}>@alice</a>
<a href="https://www.threads.com/@alice/post/Code_1" {link_style}><time {time_style}>now</time></a>
<span role="note" aria-label="Audience" {audience_style}>Public</span></header>
<section aria-label="Post text" style="{body_style}">{body_html}</section>{extra}
</article></div></main>'''
    result = subprocess.run(["node", str(Path(__file__).with_name("collector_dom_double.mjs"))], input=json.dumps({"source": RENDERED_SNAPSHOT_JS, "html": html}), text=True, capture_output=True, check=True, timeout=10)
    return json.loads(result.stdout)


def test_real_snapshot_js_preserves_supported_zero_width_line_break():
    raw = dom_snapshot(body_html='First<br data-test-rect="20,10,0,20">Second')
    assert raw["text"] == "First\nSecond"


@pytest.mark.parametrize("attributes,expected", [
    ('hidden data-test-rect="20,10,0,20"', {"text": "FirstSecond"}),
    ('style="display:none" data-test-rect="20,10,0,20"', {"text": "FirstSecond"}),
    ('style="visibility:hidden" data-test-rect="20,10,0,20"', {"text": "FirstSecond"}),
    ('style="mask-image:linear-gradient(transparent,transparent)" data-test-rect="20,10,0,20"', {"issue": "unsupported_text_layout"}),
    ('data-test-rect="1000,10,0,20"', {"issue": "unsupported_text_layout"}),
    ('data-test-rect="900,10,0,20"', {"issue": "unsupported_text_layout"}),
    ('data-test-rect="20,10,0,0"', {"text": "FirstSecond"}),
])
def test_real_snapshot_js_does_not_read_unsupported_zero_width_break(attributes, expected):
    raw = dom_snapshot(body_html=f'First<br {attributes}>Second')
    assert {key: raw[key] for key in expected} == expected


def test_real_snapshot_js_rejects_zero_width_break_outside_ancestor_clip():
    raw = dom_snapshot(body_html=(
        '<span style="overflow-x:hidden" data-test-rect="10,10,100,20">'
        'First<br data-test-rect="110,10,0,20">Second</span>'
    ))
    assert raw["issue"] == "unsupported_text_layout"


def test_header_ancestor_overflow_rejects_identity_even_if_identity_rect_is_inside():
    raw = dom_snapshot(
        header_style="overflow-x:hidden",
        header_attrs='data-test-scrollwidth="300" data-test-clientwidth="100"',
        audience_style='data-test-rect="200,10,100,20"',
    )
    assert raw["issue"] == "unknown_layout"


def test_header_clips_audience_without_overflowing_identity_or_ancestor():
    from threadrelay.collector.extractor import post_from_snapshot
    raw = dom_snapshot(
        header_style="overflow-x:hidden", header_rect="100,10,100,20",
        author_style='data-test-rect="110,10,30,20"',
        link_style='data-test-rect="140,10,30,20"',
        time_style='data-test-rect="140,10,30,20"',
        audience_style='data-test-rect="60,10,30,20"',
    )
    post = post_from_snapshot(raw, source_session_id="dom-double", observed_at=NOW)
    assert post["post_id"] == "threads:Code_1"
    assert post["visibility"] == "unknown"
    assert "visibility_unverified" in {flag["code"] for flag in post["quality_flags"]}


@pytest.mark.parametrize("audience_style,header_style", [
    ('data-test-rect="1000,10,100,20"', ""),
    ('data-test-rect="200,10,100,20"', "overflow-x:hidden"),
    ('style="clip-path:inset(100%)"', ""),
    ('style="mask-image:linear-gradient(transparent,transparent)"', ""),
    ('style="content-visibility:auto"', ""),
])
def test_real_snapshot_js_cannot_promote_unsupported_audience_evidence(audience_style, header_style):
    from threadrelay.collector.extractor import post_from_snapshot
    raw = dom_snapshot(audience_style=audience_style, header_style=header_style)
    assert "issue" not in raw
    post = post_from_snapshot(raw, source_session_id="dom-double", observed_at=NOW)
    assert post["visibility"] == "unknown"
    assert "visibility_unverified" in {flag["code"] for flag in post["quality_flags"]}


@pytest.mark.parametrize("field,attributes", [
    ("author_style", 'data-test-rect="1000,10,100,20"'),
    ("link_style", 'data-test-rect="200,10,100,20"'),
    ("time_style", 'data-test-rect="1000,10,100,20"'),
    ("author_style", 'style="mask-image:linear-gradient(transparent,transparent)"'),
    ("link_style", 'style="content-visibility:auto"'),
])
def test_real_snapshot_js_rejects_unsupported_author_permalink_or_timestamp(field, attributes):
    raw = dom_snapshot(**{field: attributes}, header_style="overflow-x:hidden")
    assert "issue" in raw
    assert "permalink" not in raw


@pytest.mark.parametrize("style", ["mask-image:linear-gradient(transparent,transparent)", "content-visibility:auto"])
def test_real_snapshot_js_rejects_unsupported_body_css(style):
    assert "issue" in dom_snapshot(body_style=style)


def test_real_snapshot_js_retains_supported_visible_identity_and_public_evidence():
    from threadrelay.collector.extractor import post_from_snapshot
    post = post_from_snapshot(dom_snapshot(), source_session_id="dom-double", observed_at=NOW)
    assert post["post_id"] == "threads:Code_1"
    assert post["visibility"] == "public"


@pytest.mark.parametrize("failure", [RuntimeError("native bridge failed"), ValueError("malformed native reply")])
def test_all_ordinary_native_probe_failures_close_actual_transmission_gate(tmp_path, failure):
    from threadrelay.collector.browser import BrowserSession
    from threadrelay.session import SessionController
    controller = SessionController(setup_check=lambda _: {"allowed": True, "reason": None})
    assert controller.start({"feed_url": "https://www.threads.com/"})["state"] == "running"
    launcher = FixtureLauncher(FixturePage())
    browser = BrowserSession(app_data_dir=tmp_path, launcher=launcher, can_intake=controller.can_intake, on_attention=controller.pause, now=lambda: NOW)
    browser.open(tmp_path / "browser-profiles" / "threads")
    def failed_probe():
        raise failure
    launcher.leases[0].poll = failed_probe
    try:
        assert browser.observe() == []
        assert controller.snapshot["state"] == "paused"
        assert controller.snapshot["attention_reason"] == "browser_ownership_lost"
        assert not controller.can_transmit(NOW)
        browser.scroll_next()
        assert launcher.page.scrolls == 0
    finally:
        browser.close()


@pytest.mark.parametrize("signal", [SystemExit("stop"), KeyboardInterrupt(), CancelledError()])
def test_ownership_probe_does_not_swallow_process_control_signals(tmp_path, signal):
    browser, launcher, _ = session(tmp_path)
    def interrupted():
        raise signal
    launcher.leases[0].poll = interrupted
    try:
        with pytest.raises(type(signal)):
            browser.observe()
    finally:
        browser.close()


QUOTE_WITH_MEDIA = '''<blockquote><header><a rel="author" href="https://www.threads.com/@quoted">@quoted</a><a href="https://www.threads.com/@quoted/post/Q1"><time>then</time></a></header><section aria-label="Post text">A quoted caption.</section><img alt="Uninterpreted image"></blockquote>'''


@pytest.mark.parametrize("extra,expected", [
    ('<img alt="Uninterpreted image">', {"missing_media_context"}),
    ('<video></video>', {"missing_media_context"}),
    ('<iframe title="Unsupported attachment"></iframe>', {"missing_media_context"}),
    ('<a download>Document attachment</a>', {"missing_media_context"}),
    (QUOTE_WITH_MEDIA, {"quote_missing_media_context"}),
    ('<video></video>' + QUOTE_WITH_MEDIA, {"missing_media_context", "quote_missing_media_context"}),
])
def test_real_snapshot_js_records_missing_media_per_author_quote_boundary(extra, expected):
    from threadrelay.collector.extractor import post_from_snapshot
    raw = dom_snapshot(extra=extra)
    post = post_from_snapshot(raw, source_session_id="dom-double", observed_at=NOW)
    validate_contract("ObservedPost", post)
    assert {flag["code"] for flag in post["quality_flags"]} == expected
    assert post["text"] == "A bounded visible post."
    plain = post_from_snapshot(dom_snapshot(), source_session_id="dom-double", observed_at=NOW)
    assert post["content_hash"] != plain["content_hash"]
    if "quote_missing_media_context" in expected:
        assert post["quote"]["author"] == "@quoted"
        assert post["quote"]["text"] == "A quoted caption."


def test_real_snapshot_js_hidden_media_and_control_icons_do_not_change_snapshot_hash():
    from threadrelay.collector.extractor import post_from_snapshot
    extra = '<img hidden><video style="display:none"></video><footer><img alt="UI icon"></footer><button><img alt="Like icon"></button>'
    post = post_from_snapshot(dom_snapshot(extra=extra), source_session_id="dom-double", observed_at=NOW)
    plain = post_from_snapshot(dom_snapshot(), source_session_id="dom-double", observed_at=NOW)
    assert post["quality_flags"] == []
    assert post["content_hash"] == plain["content_hash"]


@pytest.mark.parametrize("style", ["position:absolute;left:1200px", "clip-path:inset(100%)", "mask-image:linear-gradient(transparent,transparent)", "content-visibility:auto"])
def test_rendered_unsupported_public_evidence_remains_unknown(page, style):
    card = fixture(page, "evidence-media")
    page.locator('article > header [aria-label=Audience]').evaluate("(el, style) => el.style.cssText=style", style)
    assert extract(card)["visibility"] == "unknown"


def test_rendered_ancestor_clipped_public_evidence_remains_unknown(page):
    card = fixture(page, "evidence-media")
    assert extract(card)["visibility"] == "public"
    page.locator('article > header').evaluate("""el => {
        el.style.cssText='position:relative;margin-left:150px;width:200px;height:80px;line-height:40px;overflow:hidden';
    }""")
    page.locator('article > header [aria-label=Audience]').evaluate("el => {el.style.position='absolute';el.style.left='-100px';el.style.top='0'}")
    # Prove the fixture clips only audience, not either identity element.
    geometry = page.locator('article > header').evaluate("""h => {
        const box=h.getBoundingClientRect();
        const inside=el => { const rects=[...el.getClientRects()]; return rects.length>0 && rects.every(r => r.width>0 && r.height>0 &&
            r.left>=box.left+h.clientLeft && r.right<=box.left+h.clientLeft+h.clientWidth &&
            r.top>=box.top+h.clientTop && r.bottom<=box.top+h.clientTop+h.clientHeight); };
        const audience=h.querySelector('[aria-label=Audience]').getBoundingClientRect();
        return {author:inside(h.querySelector('a[rel=author]')),
                link:inside(h.querySelector('a:has(time)')), time:inside(h.querySelector('time')),
                audienceOutside:audience.right<=box.left+h.clientLeft,
                audienceInViewport:audience.left>=0 && audience.right<=innerWidth,
                noHeaderOverflow:h.scrollWidth<=h.clientWidth+1 && h.scrollHeight<=h.clientHeight+1,
                clipping:getComputedStyle(h).overflowX==='hidden'};
    }""")
    assert all(geometry.values()), geometry
    post = extract(card)
    assert post["post_id"] == "threads:Media_1"
    assert post["visibility"] == "unknown"
    assert "visibility_unverified" in {flag["code"] for flag in post["quality_flags"]}


@pytest.mark.parametrize("selector", ['article > header a[rel=author]', 'article > header a:has(time)', 'article > header time'])
def test_rendered_offscreen_identity_evidence_is_rejected(page, selector):
    card = fixture(page, "evidence-media")
    page.locator(selector).evaluate("el => {el.style.position='absolute';el.style.left='1200px'}")
    assert "code" in extract(card)


def test_rendered_outer_and_quote_media_have_separate_quality_flags(page):
    post = extract(fixture(page, "evidence-media"))
    assert {flag["code"] for flag in post["quality_flags"]} == {"missing_media_context", "quote_missing_media_context"}
    assert post["text"] == "A caption with uncollected media context."
    assert post["quote"]["text"] == "A quoted caption."


def test_real_snapshot_js_unsupported_quote_is_omitted_with_media_uncertainty_not_silently_lost():
    from threadrelay.collector.extractor import post_from_snapshot
    quote = QUOTE_WITH_MEDIA.replace('<blockquote>', '<blockquote style="mask-image:linear-gradient(transparent,transparent)">')
    post = post_from_snapshot(dom_snapshot(extra=quote), source_session_id="dom-double", observed_at=NOW)
    assert post["quote"] is None
    assert {flag["code"] for flag in post["quality_flags"]} == {"quote_omitted", "quote_missing_media_context"}


@pytest.mark.parametrize("attributes,present", [
    ("", True),
    ('data-test-rect="10,890,100,20"', True),
    ('style="mask-image:linear-gradient(transparent,transparent)"', True),
    ("hidden", False),
    ('style="display:none"', False),
])
def test_real_snapshot_js_nested_quote_presence_preserves_omission_uncertainty(attributes, present):
    from threadrelay.collector.extractor import post_from_snapshot
    base = post_from_snapshot(dom_snapshot(extra=QUOTE_WITH_MEDIA), source_session_id="dom-double", observed_at=NOW)
    nested = f'<blockquote {attributes}><section aria-label="Post text">Nested text must not be attributed to either author.</section></blockquote>'
    prefix, suffix = QUOTE_WITH_MEDIA.rsplit('</blockquote>', 1)
    raw = dom_snapshot(extra=prefix + nested + '</blockquote>' + suffix)
    assert raw["nested_quote"] is present
    post = post_from_snapshot(raw, source_session_id="dom-double", observed_at=NOW)
    assert post["quote"] == base["quote"]
    flags = {flag["code"] for flag in post["quality_flags"]}
    assert ("nested_quote_omitted" in flags) is present
    assert (post["content_hash"] != base["content_hash"]) is present


@pytest.mark.parametrize('transient', [
    {'text': 'B transient edited text'}, {'audience': ['Followers only']}, {'truncated': True},
])
def test_latest_reversion_discards_buffered_edit_before_store(tmp_path, transient):
    from datetime import timedelta
    from threadrelay.storage.database import Database
    from threadrelay.storage.posts import PostStore
    page = FixturePage([raw_snapshot(text='A original current text', audience=['Public'])])
    browser, _, _ = session(tmp_path, page)
    stamp = [NOW]
    browser._now = lambda: stamp[0]
    try:
        with Database(tmp_path / 'posts.db') as db:
            posts = PostStore(db)
            original = browser.observe()[0]
            posts.upsert(original)
            stamp[0] += timedelta(seconds=1)
            page.snapshots = [raw_snapshot(text='A original current text', audience=['Public']) | transient]
            browser.scroll_next()  # buffers B before the page virtualizes
            stamp[0] += timedelta(seconds=1)
            page.snapshots = [raw_snapshot(text='A original current text', audience=['Public'])]
            emitted = browser.observe()
            for post in emitted:
                posts.upsert(post)
            assert posts.get('threads:Code_1')['text'] == 'A original current text'
            assert posts.get('threads:Code_1')['visibility'] == 'public'
            assert posts.get('threads:Code_1')['quality_flags'] == []
            assert emitted == []  # latest A is already emitted, not a reason to emit older B
            page.snapshots[0]['text'] = 'C newest edit'
            assert browser.observe()[0]['text'] == 'C newest edit'
    finally:
        browser.close()


def test_legacy_mixed_unsupported_cards_retain_issues_and_admit_valid_posts(tmp_path):
    browser, launcher, reasons = session(tmp_path, FixturePage([{"issue": "unknown_layout"}, raw_snapshot()]))
    assert [post["post_id"] for post in browser.observe()] == ["threads:Code_1"]
    assert browser.issues == [{"code": "unknown_layout", "detail": "unknown layout"}]
    assert reasons == []
    browser.close()
