"""Observed-only metadata source tests; sanitized DOM, no accounts or network."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest
from test_collector import raw_snapshot
from test_public_collector import FIXTURE, snapshot

from threadrelay.collector.embedded import validate_embedded_snapshot
from threadrelay.collector.extractor import post_from_snapshot

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def convert(raw):
    return post_from_snapshot(raw, source_session_id="observed-fixture", observed_at=NOW)


def test_public_metadata_preserves_rendered_labels_without_guessing_counts():
    raw = snapshot()
    metadata = convert(raw)["observed_metadata"]
    assert metadata["published_label"] == "1h"
    assert "display_name" not in metadata and "published_at" not in metadata
    assert metadata["engagement"] == [
        {"kind": "likes", "label": "Like 87", "count": 87},
        {"kind": "replies", "label": "Comment 12", "count": 12},
        {"kind": "reposts", "label": "Repost"},
        {"kind": "shares", "label": "Share"},
    ]
    html = FIXTURE.read_text().replace("Like 87", "Like 1.2K")
    assert convert(snapshot(html))["observed_metadata"]["engagement"][0] == {"kind": "likes", "label": "Like 1.2K"}


def test_public_metadata_preserves_observed_time_and_explicit_visible_display_name():
    html = FIXTURE.read_text().replace('<time>1h</time>', '<time datetime="2026-10-31T12:30:00+00:00">1h</time>', 1)
    html = html.replace('<div data-fixture="header"><div>', '<div data-fixture="header"><div><span itemprop="name">Alice Display</span>', 1)
    metadata = convert(snapshot(html))["observed_metadata"]
    assert metadata["display_name"] == "Alice Display"
    assert metadata["published_at"] == "2026-10-31T12:30:00+00:00"
    hidden = html.replace('itemprop="name"', 'itemprop="name" hidden')
    assert "display_name" not in convert(snapshot(hidden))["observed_metadata"]


def test_media_metadata_excludes_avatar_hidden_and_signed_or_private_urls():
    media = '<img src="https://images.example.com/photo.jpg" alt="Visible caption"><img src="https://images.example.com/photo.jpg?signature=private"><img src="https://localhost/photo.jpg"><img hidden src="https://images.example.com/hidden.jpg"><video src="blob:https://www.threads.com/private"></video><a href="https://example.com/article">Article</a>'
    html = FIXTURE.read_text().replace('<div data-fixture="translation">', media + '<div data-fixture="translation">', 1)
    metadata = convert(snapshot(html))["observed_metadata"]
    assert metadata["media"] == [
        {"kind": "image", "url": "https://images.example.com/photo.jpg", "alt": "Visible caption"},
        {"kind": "image"}, {"kind": "image"}, {"kind": "video"},
        {"kind": "link", "url": "https://example.com/article"},
    ]
    assert all(word not in str(metadata) for word in ["signature", "localhost", "hidden.jpg", "blob:", "avatar"])


@pytest.mark.parametrize("metadata", [
    {"invented": True}, {"published_at": "yesterday"},
    {"engagement": [{"kind": "likes", "label": "2", "count": True}]},
    {"engagement": [{"kind": "likes", "label": "2", "count": -1}]},
    {"media": [{"kind": "image", "url": "https://example.com/image?token=private"}]},
    {"media": [{"kind": "image", "url": "https://name:secret@example.com/image"}]},
    {"media": [{"kind": "image", "url": "https://127.0.0.1/image"}]},
    {"media": [{"kind": "image", "url": "https://localhost./image"}]},
    {"media": [{"kind": "image", "url": "https://printer.home.arpa/image"}]},
    {"media": [{"kind": "image", "url": "https://example.com/image#private"}]},
    {"media": [{"kind": "image"}] * 21}, {"display_name": "x" * 501},
])
def test_metadata_boundary_rejects_unknown_unbounded_or_sensitive_reference_values(metadata):
    raw = raw_snapshot(observed_metadata=metadata)
    assert convert(raw)["code"] == "unknown_layout"
    with pytest.raises(ValueError, match="invalid_embedded_snapshot"):
        validate_embedded_snapshot({"schema_version": 1, "state": "ready", "cards": [raw], "scroll_y": 0, "viewport_height": 900})


def test_metadata_is_optional_copied_and_does_not_invalidate_semantic_hash():
    plain = convert(raw_snapshot())
    metadata = {"published_label": "1h", "engagement": [{"kind": "shares", "label": "Share 7", "count": 7}]}
    raw = raw_snapshot(observed_metadata=deepcopy(metadata))
    post = convert(raw)
    assert "observed_metadata" not in plain
    assert post["observed_metadata"] == metadata
    assert post["content_hash"] == plain["content_hash"]
    raw["observed_metadata"]["published_label"] = "2h"
    assert post["observed_metadata"]["published_label"] == "1h"


def test_metadata_caps_and_ambiguous_or_hidden_evidence_are_not_invented():
    html = FIXTURE.read_text().replace('data-fixture="header"><div>', 'data-fixture="header"><div><span itemprop="name">One</span><span itemprop="name">Two</span>', 1)
    html = html.replace('Like 87', 'Like 9,007,199,254,740,992')
    html = html.replace('<div data-fixture="translation">', ''.join(f'<img src="https://images.example.com/{i}.jpg">' for i in range(25)) + '<div data-fixture="translation">', 1)
    metadata = convert(snapshot(html))["observed_metadata"]
    assert "display_name" not in metadata
    assert "count" not in metadata["engagement"][0]
    assert len(metadata["media"]) == 20
    html = FIXTURE.read_text().replace('Like 87', 'Like 1,234')
    assert convert(snapshot(html))["observed_metadata"]["engagement"][0]["count"] == 1234


def test_semantic_adapter_preserves_metadata_without_adding_untyped_quote_fields():
    from test_collector import dom_snapshot
    raw = dom_snapshot()
    metadata = convert(raw)["observed_metadata"]
    assert metadata["published_label"]
    assert validate_embedded_snapshot({"schema_version": 1, "state": "ready", "cards": [raw], "scroll_y": 0, "viewport_height": 900})["cards"][0]["observed_metadata"] == metadata


def test_embedded_wire_roundtrip_preserves_metadata_and_no_services_or_url_fetch():
    from test_embedded_collector import snapshot as host_snapshot
    # Covered through the independent strict raw-card boundary as well as DTO.
    raw = snapshot()
    wire = validate_embedded_snapshot(host_snapshot(cards=[raw]))
    assert convert(wire["cards"][0])["observed_metadata"] == raw["observed_metadata"]


def test_rendered_reference_filter_omits_local_dns_aliases_as_well_as_ips():
    for host in ["localhost.", "printer.home.arpa", "127.0.0.1"]:
        html = FIXTURE.read_text().replace('<div data-fixture="translation">', f'<img src="https://{host}/private.jpg"><div data-fixture="translation">', 1)
        assert convert(snapshot(html))["observed_metadata"]["media"] == [{"kind": "image"}]
