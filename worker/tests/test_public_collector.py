"""Sanitized observed-layout DOM/geometry tests; not live or native acceptance."""

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

import pytest
from test_collector import browser as _semantic_browser, page as _semantic_page

from threadrelay.collector.extractor import RENDERED_SNAPSHOT_JS, post_from_snapshot

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures/threads/public-div.html"
SELECTOR = 'div[data-pressable-container="true"][data-interactive-id=""]'
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def snapshot(html=None, *, selector=SELECTOR, page_script=False, script=None):
    payload = {
        "source": script or RENDERED_SNAPSHOT_JS,
        "html": FIXTURE.read_text() if html is None else html,
        "selector": selector,
        "mode": "page" if page_script else "card",
    }
    result = subprocess.run(
        ["node", str(Path(__file__).with_name("collector_dom_double.mjs"))],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    return json.loads(result.stdout)


def post(raw):
    return post_from_snapshot(
        raw, source_session_id="sanitized-public-layout", observed_at=NOW
    )


def test_public_div_identity_text_controls_avatar_and_guest_visibility():
    raw = snapshot()
    value = post(raw)
    assert value["post_id"] == "threads:Public_EN_1"
    assert value["author"] == "@alice"
    assert (
        value["text"]
        == "My boyfriend and I are planning our anniversary.\nWe want a quiet dinner."
    )
    assert value["visibility"] == "public" and value["quality_flags"] == []
    assert raw["audience"] == [] and raw["guest_public_context"] is True
    assert raw["missing_media_context"] is False
    assert all(
        word not in value["text"] for word in ["Translate", "Like", "Comment", "Hidden"]
    )


def test_public_alias_identity_and_cantonese_text():
    raw = snapshot(selector="[data-fixture=cantonese-card]")
    value = post(raw)
    assert (
        value["post_id"] == "threads:Public_YUE_1" and value["author"] == "@hk_friend"
    )
    assert value["text"] == "成日話唔好沉船，點知一收到佢訊息就好開心。"
    assert value["visibility"] == "public"


@pytest.mark.parametrize("transform", ["matrix(1,0,0,1,0,0)", "matrix(1, 0, 0, 1, -24.5, 100)"])
def test_finite_translated_identity_keeps_visible_author_and_post_bounds(transform):
    html = FIXTURE.read_text().replace('data-fixture="header"', f'data-fixture="header" style="transform:{transform}"', 1)
    value = post(snapshot(html))
    assert value["post_id"] == "threads:Public_EN_1"
    assert value["author"] == "@alice" and value["visibility"] == "public"


@pytest.mark.parametrize("transform", ["matrix(2,0,0,1,0,0)", "matrix(0,1,-1,0,0,0)", "matrix(1,.5,0,1,0,0)", "matrix3d(1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1)", "matrix(1,0,0,1,Infinity,0)"])
def test_nontranslation_transforms_cannot_establish_author_identity(transform):
    html = FIXTURE.read_text().replace('data-fixture="header"', f'data-fixture="header" style="transform:{transform}"', 1)
    assert snapshot(html)["issue"] == "unknown_layout"


@pytest.mark.parametrize(
    "replace",
    [
        (
            '<header><a role="link" data-fixture="guest-login" href="https://www.threads.com/login">Log in</a></header>',
            "",
        ),
        ('data-fixture="guest-login"', 'hidden data-fixture="guest-login"'),
        ("https://www.threads.com/login", "https://evil.example/login"),
    ],
)
def test_public_without_visible_platform_login_evidence_stays_unknown(replace):
    value = post(snapshot(FIXTURE.read_text().replace(*replace)))
    assert value["visibility"] == "unknown"
    assert "visibility_unverified" in {flag["code"] for flag in value["quality_flags"]}


def test_login_link_inside_post_cannot_spoof_public_access():
    html = FIXTURE.read_text().replace(
        '<header><a role="link" data-fixture="guest-login" href="https://www.threads.com/login">Log in</a></header>',
        "",
    )
    html = html.replace(
        "We want a quiet dinner.",
        'We want a quiet dinner.<a href="https://www.threads.com/login">Log in</a>',
    )
    value = post(snapshot(html))
    assert value["visibility"] == "unknown"


@pytest.mark.parametrize(
    "attribute", ["hidden", 'style="display:none"', 'style="opacity:0"']
)
def test_hidden_public_card_is_not_visible(attribute):
    html = FIXTURE.read_text().replace(
        'data-fixture="english-card"', attribute + ' data-fixture="english-card"'
    )
    assert snapshot(html)["issue"] == "not_visible"


def test_public_ambiguous_or_mismatched_header_identity_fails_closed():
    html = FIXTURE.read_text().replace(
        '<span dir="auto">alice</span>', '<span dir="auto">someone_else</span>', 1
    )
    assert snapshot(html)["issue"] == "unknown_layout"
    html = FIXTURE.read_text().replace(
        '<span dir="auto">alice</span>',
        '<span dir="auto">alice</span></a><a role="link" href="https://www.threads.com/@eve">eve',
        1,
    )
    assert snapshot(html)["issue"] == "unknown_layout"


def test_public_truncation_and_media_remain_explicit_quality_warnings():
    html = FIXTURE.read_text().replace(
        "We want a quiet dinner.", "We want a quiet dinner…"
    )
    value = post(snapshot(html))
    assert "truncated" in {f["code"] for f in value["quality_flags"]}
    html = FIXTURE.read_text().replace(
        '<div data-fixture="translation">',
        '<img width="250" height="180" alt="not read"><div data-fixture="translation">',
        1,
    )
    raw = snapshot(html)
    assert raw["missing_media_context"] is True
    assert "missing_media_context" in {f["code"] for f in post(raw)["quality_flags"]}


@pytest.mark.parametrize("control", [
    '<button aria-expanded="false" aria-haspopup="menu">More options</button>',
    '<div role="button" aria-expanded="false"><video width="250" height="180"></video>Open video</div>',
    '<button aria-expanded="false">Comments</button>',
])
def test_collapsed_non_text_controls_do_not_mark_complete_public_caption_truncated(control):
    html = FIXTURE.read_text().replace('<div data-fixture="translation">', control + '<div data-fixture="translation">', 1)
    source = (ROOT / "apps/desktop/src-tauri/native/embedded-snapshot.js").read_text()
    for raw in [snapshot(html), snapshot(html, page_script=True, script=source)["cards"][0]]:
        assert raw["truncated"] is False
        value = post(raw)
        assert "truncated" not in {flag["code"] for flag in value["quality_flags"]}
        assert value["text"] == "My boyfriend and I are planning our anniversary.\nWe want a quiet dinner."


@pytest.mark.parametrize("label", ["Read more", "Show more", "查看更多"])
def test_collapsed_text_expansion_keeps_public_caption_truncated_without_reading_hidden_text(label):
    control = f'<button aria-expanded="false">{label}</button>'
    html = FIXTURE.read_text().replace('<div data-fixture="translation">', control + '<div data-fixture="translation">', 1)
    source = (ROOT / "apps/desktop/src-tauri/native/embedded-snapshot.js").read_text()
    for raw in [snapshot(html), snapshot(html, page_script=True, script=source)["cards"][0]]:
        assert raw["truncated"] is True
        value = post(raw)
        assert "truncated" in {flag["code"] for flag in value["quality_flags"]}
        assert "Hidden full text" not in value["text"]


@pytest.mark.parametrize("control,truncated", [
    ('<button aria-expanded="false" aria-haspopup="menu">More options</button>', False),
    ('<button aria-expanded="false">Read more</button>', True),
    ('<button aria-expanded="false" hidden>Read more</button>', False),
])
def test_semantic_collapsed_control_truncation_uses_visible_text_expansion(control, truncated):
    from test_collector import dom_snapshot

    raw = dom_snapshot(extra=control)
    assert raw["truncated"] is truncated
    assert raw["text"] == "A bounded visible post."


@pytest.mark.parametrize("control,truncated", [
    ('<button aria-expanded="false" aria-label="Read more"><svg width="10" height="10"></svg></button>', True),
    ('<button aria-expanded="false" aria-label="More options" aria-haspopup="menu"><svg width="10" height="10"></svg></button>', False),
    ('<button aria-expanded="false" aria-label="Read more" aria-haspopup="menu"><svg width="10" height="10"></svg></button>', False),
])
def test_icon_only_expanders_use_own_accessible_label_without_treating_menus_as_text(control, truncated):
    from test_collector import dom_snapshot

    html = FIXTURE.read_text().replace('<div data-fixture="translation">', control + '<div data-fixture="translation">', 1)
    source = (ROOT / "apps/desktop/src-tauri/native/embedded-snapshot.js").read_text()
    raws = [dom_snapshot(extra=control), snapshot(html), snapshot(html, page_script=True, script=source)["cards"][0]]
    for raw in raws:
        assert raw["truncated"] is truncated
        assert "Read more" not in raw["text"] and "More options" not in raw["text"]


@pytest.mark.parametrize("kind", ["img", "video"])
def test_media_viewer_keeps_visible_caption_and_media_metadata_without_analyzing(kind):
    close = '</video>' if kind == 'video' else ''
    media = f'<div role="button"><{kind} width="250" height="180" src="https://images.example.com/attachment" alt="Not a model description">{close}<span dir="auto">Open attachment</span></div>'
    html = FIXTURE.read_text().replace('<div data-fixture="translation">', media + '<div data-fixture="translation">', 1)
    for page_script in [False, True]:
        source = (ROOT / "apps/desktop/src-tauri/native/embedded-snapshot.js").read_text() if page_script else None
        raw = snapshot(html, page_script=page_script, script=source)
        if page_script:
            assert raw["state"] == "ready"
            raw = raw["cards"][0]
        value = post(raw)
        assert value["author"] == "@alice" and value["post_id"] == "threads:Public_EN_1"
        assert value["text"] == "My boyfriend and I are planning our anniversary.\nWe want a quiet dinner."
        assert raw["missing_media_context"] is True
        assert "missing_media_context" in {flag["code"] for flag in value["quality_flags"]}
        assert value["observed_metadata"]["media"][0]["kind"] == ("image" if kind == "img" else "video")
        assert "Open attachment" not in value["text"] and "Not a model description" not in value["text"]


@pytest.mark.parametrize("kind", ["img", "video"])
def test_captionless_media_preserves_identity_with_empty_unanalyzed_text(kind):
    html = FIXTURE.read_text()
    start = html.index('<div data-fixture="text-group">')
    end = html.index('<div data-fixture="translation">', start)
    html = html[:start] + f'<div role="button"><{kind} width="250" height="180" src="data:image/png;base64,UNTRUSTED" alt="Never invent this description"></{kind}></div>' + html[end:]
    for page_script in [False, True]:
        source = (ROOT / "apps/desktop/src-tauri/native/embedded-snapshot.js").read_text() if page_script else None
        raw = snapshot(html, page_script=page_script, script=source)
        if page_script:
            raw = raw["cards"][0]
        value = post(raw)
        assert value["post_id"] == "threads:Public_EN_1"
        assert value["text"] == "" and raw["missing_media_context"] is True
        assert "url" not in value["observed_metadata"]["media"][0]


@pytest.mark.parametrize("kind", ["img", "video"])
def test_semantic_media_viewer_does_not_hide_unanalyzed_media_flag(kind):
    from test_collector import dom_snapshot

    close = '</video>' if kind == 'video' else ''
    raw = dom_snapshot(extra=f'<div role="button"><{kind} width="250" height="180" src="https://images.example.com/attachment">{close}</div>')
    value = post(raw)
    assert value["text"] == "A bounded visible post."
    assert raw["missing_media_context"] is True
    assert "missing_media_context" in {flag["code"] for flag in value["quality_flags"]}
    assert value["observed_metadata"]["media"][0]["kind"] == ("image" if kind == "img" else "video")


def test_public_unsupported_nested_post_or_quote_is_not_flattened():
    nested = '<div data-pressable-container="true" data-interactive-id=""><a role="link" href="https://www.threads.com/@other/post/Nested"><time>now</time></a><span dir="auto">Other author claim.</span></div>'
    html = FIXTURE.read_text().replace(
        "We want a quiet dinner.", "We want a quiet dinner." + nested
    )
    assert snapshot(html)["issue"] == "unknown_layout"
    html = FIXTURE.read_text().replace(
        "We want a quiet dinner.",
        "We want a quiet dinner.<blockquote>Other claim.</blockquote>",
    )
    assert snapshot(html)["issue"] == "unknown_layout"


@pytest.mark.parametrize(
    "style", ["clip-path:inset(0)", "overflow-y:hidden", "transform:translateX(1px)"]
)
def test_public_unsupported_text_geometry_fails_closed(style):
    html = FIXTURE.read_text().replace(
        'data-fixture="text"',
        f'style="{style}" data-test-scrollheight="100" data-test-clientheight="20" data-fixture="text"',
        1,
    )
    assert snapshot(html)["issue"] in {"text_clipped", "unsupported_text_layout"}


def test_guest_evidence_cannot_override_restricted_or_ambiguous_audience():
    raw = snapshot()
    for audience in [
        ["Followers only"],
        ["Public", "Followers only"],
        ["Unsupported audience"],
    ]:
        assert post({**raw, "audience": audience})["code"] == "unknown_layout"
    assert post({**raw, "guest_public_context": "true"})["code"] == "unknown_layout"


def test_native_page_snapshot_gates_visible_modal_before_post_read():
    script = (ROOT / "apps/desktop/src-tauri/native/embedded-snapshot.js").read_text()
    html = FIXTURE.read_text() + '<div role="dialog">Log in or sign up</div>'
    value = snapshot(html, page_script=True, script=script)
    assert value["state"] == "manual_login_or_challenge" and value["cards"] == []


def test_public_empty_or_multiple_unrecognized_text_regions_fail_closed():
    html = FIXTURE.read_text().replace(
        'dir="auto" data-fixture="text"', 'data-fixture="text"', 1
    )
    assert snapshot(html)["issue"] == "unknown_layout"
    html = FIXTURE.read_text().replace(
        '<div data-fixture="translation">',
        '<span dir="auto">Unrelated extra region</span><div data-fixture="translation">',
        1,
    )
    assert snapshot(html)["issue"] == "unknown_layout"


def test_public_hidden_identity_and_unmarked_quote_permalink_fail_closed():
    html = FIXTURE.read_text().replace(
        '<span dir="auto">alice</span>', '<span hidden dir="auto">alice</span>', 1
    )
    assert snapshot(html)["issue"] == "unknown_layout"
    html = FIXTURE.read_text().replace(
        "We want a quiet dinner.",
        'We want a quiet dinner.<a href="https://www.threads.com/@other/post/Quote">Quoted post</a>',
    )
    assert snapshot(html)["issue"] == "unknown_layout"


def test_native_public_snapshot_and_card_limit_use_fixed_closed_schema():
    script = (ROOT / "apps/desktop/src-tauri/native/embedded-snapshot.js").read_text()
    value = snapshot(page_script=True, script=script)
    assert set(value) == {
        "schema_version",
        "state",
        "cards",
        "scroll_y",
        "viewport_height",
    }
    assert value["state"] == "ready" and len(value["cards"]) == 2
    assert all(card["guest_public_context"] is True for card in value["cards"])
    html = FIXTURE.read_text() + "".join(
        '<div data-pressable-container="true" data-interactive-id=""></div>'
        for _ in range(100)
    )
    value = snapshot(html, page_script=True, script=script)
    assert value["state"] == "snapshot_limit" and value["cards"] == []


def test_native_public_snapshot_byte_limit_does_not_return_oversized_text():
    script = (ROOT / "apps/desktop/src-tauri/native/embedded-snapshot.js").read_text()
    html = FIXTURE.read_text().replace(
        "My boyfriend and I are planning our anniversary.", "文" * 90000
    )
    html = html.replace("成日話唔好沉船，點知一收到佢訊息就好開心。", "文" * 90000)
    value = snapshot(html + html, page_script=True, script=script)
    assert value["state"] == "snapshot_limit" and value["cards"] == []


def test_embedded_wire_validator_requires_strict_boolean_guest_context():
    from threadrelay.collector.embedded import validate_embedded_snapshot

    raw = snapshot()
    envelope = {
        "schema_version": 1,
        "state": "ready",
        "cards": [raw],
        "scroll_y": 0,
        "viewport_height": 900,
    }
    assert (
        validate_embedded_snapshot(envelope)["cards"][0]["guest_public_context"] is True
    )
    for bad in ["true", 1, None]:
        with pytest.raises(ValueError):
            validate_embedded_snapshot(
                {**envelope, "cards": [{**raw, "guest_public_context": bad}]}
            )


# Test-only rendering of sanitized local bytes, with every network request
# intercepted. This is neither a live Threads selector run nor WebKit evidence.


@pytest.fixture(scope="module")
def public_browser():
    yield from _semantic_browser.__wrapped__()


@pytest.fixture
def public_page(public_browser):
    yield from _semantic_page.__wrapped__(public_browser)


def load_rendered(page, html):
    page.route(
        "https://www.threads.com/",
        lambda route: route.fulfill(content_type="text/html", body=html),
    )
    page.goto("https://www.threads.com/", wait_until="domcontentloaded")


def test_rendered_sanitized_public_div_layout(public_page):
    page = public_page
    load_rendered(page, FIXTURE.read_text())
    from threadrelay.collector.extractor import CARD_SELECTOR, extract_rendered_post

    cards = page.locator(CARD_SELECTOR)
    assert cards.count() == 2
    values = [
        extract_rendered_post(
            cards.nth(index),
            source_session_id="sanitized-rendered-public",
            observed_at=NOW,
        )
        for index in range(2)
    ]
    assert [value["post_id"] for value in values] == [
        "threads:Public_EN_1",
        "threads:Public_YUE_1",
    ]
    assert all(
        value["visibility"] == "public" and value["quality_flags"] == []
        for value in values
    )
    assert "Translate" not in values[0]["text"] and "Like" not in values[0]["text"]


def test_rendered_public_dialog_blocks_snapshot(public_page):
    page = public_page
    load_rendered(
        page, FIXTURE.read_text() + '<div role="dialog">Log in or sign up</div>'
    )
    script = (ROOT / "apps/desktop/src-tauri/native/embedded-snapshot.js").read_text()
    value = page.evaluate(script)
    assert value["state"] == "manual_login_or_challenge" and value["cards"] == []


def test_rendered_translation_preserves_complete_bounds_and_rejects_offscreen(public_page):
    from threadrelay.collector.extractor import extract_rendered_post
    page = public_page
    html = FIXTURE.read_text().replace('data-fixture="header"', 'data-fixture="header" style="transform:translate(4px,2px)"', 1)
    load_rendered(page, html)
    value = extract_rendered_post(page.locator(SELECTOR).first, source_session_id="sanitized-translation", observed_at=NOW)
    assert value["post_id"] == "threads:Public_EN_1"
    page.locator('[data-fixture="header"]').first.evaluate("el=>el.style.transform='translate(-2000px,0)'")
    assert extract_rendered_post(page.locator(SELECTOR).first, source_session_id="sanitized-translation", observed_at=NOW)["code"] == "unknown_layout"


def test_rendered_native_metadata_retains_only_observed_clean_references(public_page):
    from threadrelay.collector.embedded import validate_embedded_snapshot
    html = FIXTURE.read_text().replace('<time>1h</time>', '<time datetime="2026-10-31T12:30:00Z">1h</time>', 1)
    html = html.replace('data-fixture="header"><div>', 'data-fixture="header"><div><span itemprop="name">Rendered Name</span>', 1)
    html = html.replace('Like 87', 'Like 1.2K')
    html = html.replace('<div data-fixture="translation">', '<img width="24" height="24" alt="Rendered image" src="https://images.example.com/public.jpg"><img width="24" height="24" src="https://images.example.com/private.jpg?signature=secret"><div data-fixture="translation">', 1)
    load_rendered(public_page, html)
    source = (ROOT / "apps/desktop/src-tauri/native/embedded-snapshot.js").read_text()
    wire = validate_embedded_snapshot(public_page.evaluate(source))
    metadata = post(wire["cards"][0])["observed_metadata"]
    assert metadata["display_name"] == "Rendered Name"
    assert metadata["published_at"] == "2026-10-31T12:30:00Z"
    assert metadata["engagement"][0] == {"kind": "likes", "label": "Like 1.2K"}
    assert metadata["media"] == [{"kind": "image", "url": "https://images.example.com/public.jpg", "alt": "Rendered image"}, {"kind": "image"}]
    assert "signature" not in str(wire) and "secret" not in str(wire)


def username_login_html():
    return FIXTURE.read_text().replace('https://www.threads.com/login">Log in</a>', 'https://www.threads.com/login?show_choice_screen=false">Log in with username instead</a>')


def test_observed_desktop_guest_login_literal_establishes_context_without_public_badge():
    raw = snapshot(username_login_html())
    assert raw["guest_public_context"] is True and raw["audience"] == []
    assert post(raw)["visibility"] == "public"
    assert post({**raw, "audience": ["Followers only"]})["code"] == "unknown_layout"


@pytest.mark.parametrize("change", ["inside", "external", "hidden", "different_label"])
def test_desktop_guest_login_literal_cannot_be_spoofed_or_inferred(change):
    html = username_login_html()
    if change == "inside":
        link = '<a role="link" data-fixture="guest-login" href="https://www.threads.com/login?show_choice_screen=false">Log in with username instead</a>'
        html = html.replace(link, "").replace("We want a quiet dinner.", "We want a quiet dinner." + link)
    elif change == "external":
        html = html.replace("www.threads.com/login?show_choice_screen=false", "evil.example/login?show_choice_screen=false")
    elif change == "hidden":
        html = html.replace('data-fixture="guest-login"', 'hidden data-fixture="guest-login"')
    else:
        html = html.replace("Log in with username instead", "Log in with a different account")
    assert post(snapshot(html))["visibility"] == "unknown"


def test_rendered_desktop_guest_login_literal_and_modal_guard(public_page):
    load_rendered(public_page, username_login_html())
    value = public_page.locator(SELECTOR).first.evaluate(RENDERED_SNAPSHOT_JS)
    assert post(value)["visibility"] == "public"
    load_rendered(public_page, username_login_html() + '<div role="dialog">Promotional login</div>')
    script = (ROOT / "apps/desktop/src-tauri/native/embedded-snapshot.js").read_text()
    assert public_page.evaluate(script)["state"] == "manual_login_or_challenge"


def fractional_time_html(*, outer_height="18.4", inner_height="18.4", border="0", overflow="hidden"):
    needle = '<span dir="auto"><time>1h</time></span>'
    replacement = (f'<span dir="auto" data-fixture="fractional-time-wrapper" style="display:inline-block;vertical-align:top;overflow:{overflow};width:24px;height:{outer_height}px;border:{border}px solid transparent">'
                   f'<time style="display:block;width:24px;height:{inner_height}px;line-height:18.4px">1h</time></span>')
    return FIXTURE.read_text().replace(needle, replacement, 1)


@pytest.mark.parametrize("border", ["0", "1", "0.5"])
def test_rendered_fractional_time_fits_real_padding_clip_box(public_page, border):
    load_rendered(public_page, fractional_time_html(border=border))
    geometry = public_page.locator('[data-fixture="fractional-time-wrapper"]').evaluate('''el=>{const r=el.getBoundingClientRect(),t=el.querySelector('time').getBoundingClientRect(),s=getComputedStyle(el),top=Number.parseFloat(s.borderTopWidth),bottom=Number.parseFloat(s.borderBottomWidth);return {client:el.clientHeight,offset:el.offsetHeight,borders:top+bottom,fits:t.bottom<=r.bottom-bottom&&t.top>=r.top+top,fractional:r.height!==el.clientHeight}}''')
    assert geometry["fits"] and geometry["fractional"] and geometry["client"] > 0 and geometry["offset"] - geometry["client"] == geometry["borders"]
    value = post(public_page.locator(SELECTOR).first.evaluate(RENDERED_SNAPSHOT_JS))
    assert value["post_id"] == "threads:Public_EN_1"
    assert value["observed_metadata"]["published_label"] == "1h"


@pytest.mark.parametrize("outer,inner,border", [("18.4", "18.6", "0"), ("18.4", "18.6", "1")])
def test_rendered_fractional_spill_with_or_without_border_stays_closed(public_page, outer, inner, border):
    load_rendered(public_page, fractional_time_html(outer_height=outer, inner_height=inner, border=border))
    # Force a measured subpixel spill of actual text Range bounds, rather
    # than empty line-box leading, which the header fallback may safely ignore.
    public_page.locator('[data-fixture="fractional-time-wrapper"]').evaluate("""el=>{const range=document.createRange();range.selectNodeContents(el.querySelector('time'));const bottom=Math.max(...[...range.getClientRects()].map(r=>r.bottom));const box=el.getBoundingClientRect(),border=Number.parseFloat(getComputedStyle(el).borderTopWidth);el.style.height=(bottom-box.top-border-.25)+'px'}""")
    raw = public_page.locator(SELECTOR).first.evaluate(RENDERED_SNAPSHOT_JS)
    assert raw["issue"] == "missing_permalink"


def test_rendered_reported_gutter_keeps_integer_fallback(public_page):
    # Explicit geometry override tests the offset/client inequality branch. This
    # is not a claim that headless macOS allocates non-overlay scrollbars.
    load_rendered(public_page, fractional_time_html())
    public_page.locator('[data-fixture="fractional-time-wrapper"]').evaluate("el=>{const height=el.clientHeight;Object.defineProperty(el,'clientHeight',{get:()=>height-2});Object.defineProperty(el,'offsetHeight',{get:()=>height+10})}")
    assert public_page.locator(SELECTOR).first.evaluate(RENDERED_SNAPSHOT_JS)["issue"] == "missing_permalink"


def header_text_range_html(*, height="21", font_size="16"):
    html = FIXTURE.read_text().replace('data-fixture="header"><div>', f'data-fixture="header"><div data-fixture="range-header-row" style="display:flex;align-items:center;height:{height}px;overflow:hidden;font-size:{font_size}px;line-height:22px">', 1)
    return html.replace('<time>1h</time>', '<time datetime="2026-11-01T12:00:00Z" style="display:inline-block;vertical-align:top;line-height:22px">1h</time>', 1)


def test_rendered_header_linebox_clip_accepts_only_complete_text_ranges(public_page):
    load_rendered(public_page, header_text_range_html())
    geometry = public_page.locator('[data-fixture="range-header-row"]').evaluate('''row=>{const box=row.getBoundingClientRect(),links=[...row.querySelectorAll('a')];let spill=false,fit=true,count=0;const ranges=[];for(const a of links){const r=a.getBoundingClientRect();spill ||= r.top<box.top||r.bottom>box.bottom;const w=document.createTreeWalker(a,NodeFilter.SHOW_TEXT);for(let n=w.nextNode();n;n=w.nextNode()){if(!n.textContent.trim())continue;const range=document.createRange();range.selectNodeContents(n);for(const t of range.getClientRects()){count++;ranges.push([t.top-box.top,t.bottom-box.top,box.height]);fit &&=t.top>=box.top&&t.bottom<=box.bottom;}}}return {spill,fit,count,ranges}}''')
    assert geometry["spill"] and geometry["fit"] and geometry["count"] >= 2, geometry
    value = post(public_page.locator(SELECTOR).first.evaluate(RENDERED_SNAPSHOT_JS))
    assert value["post_id"] == "threads:Public_EN_1" and value["author"] == "@alice"
    assert value["observed_metadata"]["published_label"] == "1h"
    assert value["observed_metadata"]["published_at"] == "2026-11-01T12:00:00Z"


@pytest.mark.parametrize("change", ["glyph_spill", "mask", "rotation", "no_ranges", "empty_ranges", "hidden"])
def test_rendered_header_range_fallback_keeps_real_text_and_style_failures_closed(public_page, change):
    html = header_text_range_html(height="16" if change == "glyph_spill" else "21")
    if change == "mask":
        html = html.replace('data-fixture="range-header-row"', 'data-fixture="range-header-row" data-range-mask="true"') + '<style>[data-range-mask]{mask-image:linear-gradient(transparent,transparent)}</style>'
    elif change == "rotation":
        html = html.replace('data-fixture="range-header-row"', 'data-fixture="range-header-row" data-range-rotation="true"') + '<style>[data-range-rotation]{transform:rotate(1deg)}</style>'
    if change == "hidden":
        html = html.replace('data-fixture="range-header-row"', 'data-fixture="range-header-row" hidden')
    load_rendered(public_page, html)
    if change == "empty_ranges":
        public_page.evaluate('Range.prototype.getClientRects=()=>[]')
    if change == "no_ranges":
        public_page.evaluate('Range.prototype.getClientRects=undefined')
    raw = public_page.locator(SELECTOR).first.evaluate(RENDERED_SNAPSHOT_JS)
    assert "issue" in raw and "author" not in raw and "text" not in raw


def test_rendered_document_scrollport_uses_viewport_not_scrolled_root_box(public_page):
    html = FIXTURE.read_text() + '<style>html{overflow-y:scroll}body{padding-top:1000px;padding-bottom:1000px}</style>'
    load_rendered(public_page, html)
    public_page.evaluate('window.scrollTo(0, 400)')
    geometry = public_page.locator(SELECTOR).first.evaluate('''el=>{const r=el.getBoundingClientRect(),root=document.documentElement,b=root.getBoundingClientRect();return {visible:r.top>=0&&r.bottom<=innerHeight,scrolled:scrollY>0,rootMoved:b.top<0,rootClientViewport:root.clientHeight===innerHeight,oldClipBottom:b.top+root.clientHeight,postTop:r.top}}''')
    assert geometry["visible"] and geometry["scrolled"] and geometry["rootMoved"] and geometry["rootClientViewport"], geometry
    assert geometry["postTop"] > geometry["oldClipBottom"], geometry
    value = post(public_page.locator(SELECTOR).first.evaluate(RENDERED_SNAPSHOT_JS))
    assert value["post_id"] == "threads:Public_EN_1"


def test_rendered_custom_scrollport_still_rejects_clipped_header_text(public_page):
    html = FIXTURE.read_text().replace('<main>', '<main style="height:40px;overflow-y:scroll">', 1)
    load_rendered(public_page, html)
    raw = public_page.locator(SELECTOR).first.evaluate(RENDERED_SNAPSHOT_JS)
    assert "issue" in raw and "author" not in raw


def test_rendered_header_range_fallback_does_not_broaden_body_text(public_page):
    html = FIXTURE.read_text()
    start = html.index('<div data-fixture="text-group">')
    end = html.index('<div data-fixture="translation">', start)
    replacement = '<div data-fixture="text-group"><span dir="auto" data-fixture="body-range-box" style="display:flex;align-items:center;height:21px;overflow:hidden;font-size:16px;line-height:22px"><span style="display:inline-block;line-height:22px">Readable body</span></span></div>'
    load_rendered(public_page, html[:start] + replacement + html[end:])
    raw = public_page.locator(SELECTOR).first.evaluate(RENDERED_SNAPSHOT_JS)
    assert raw["issue"] == "unsupported_text_layout" and "text" not in raw
