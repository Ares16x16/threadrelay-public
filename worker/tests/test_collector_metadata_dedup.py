"""Both source adapters retain metadata revisions independently of model identity."""
from copy import deepcopy
from datetime import timedelta

import pytest

from test_collector import FixturePage, NOW, raw_snapshot, session
from test_embedded_collector import opened, snapshot
from test_routing import ScoresRuntime, topic

from threadrelay.contracts import validate_contract
from threadrelay.inference.decision import Classifier
from threadrelay.storage.archive import ArchiveStore
from threadrelay.storage.database import Database


def collector(tmp_path, kind):
    if kind == "embedded":
        browser, broker, reasons = opened(tmp_path)
        def replace(raw):
            broker.lease.frames = [snapshot(cards=[deepcopy(raw)])]
    else:
        page = FixturePage()
        browser, _, reasons = session(tmp_path, page)
        def replace(raw):
            page.snapshots = [deepcopy(raw)]
    stamp = [NOW]
    browser._now = lambda: stamp[0]
    return browser, replace, stamp, reasons


@pytest.mark.parametrize("kind", ["embedded", "legacy"])
@pytest.mark.parametrize("metadata", [
    {"engagement": [{"kind": "likes", "label": "Like 8", "count": 8}]},
    {"published_label": "2h"},
    {"display_name": "Observed new name", "published_label": "2h"},
    {"media": [{"kind": "image", "url": "https://images.example.com/observed.jpg"}]},
])
def test_metadata_revision_is_admitted_archived_without_changing_model_identity(tmp_path, kind, metadata):
    browser, replace, stamp, reasons = collector(tmp_path, kind)
    first_raw = raw_snapshot(observed_metadata={"published_label": "1h"})
    try:
        replace(first_raw)
        first = browser.observe()[0]
        validate_contract("ObservedPost", first)
        stamp[0] += timedelta(seconds=1)
        replace(raw_snapshot(observed_metadata=metadata))
        changed = browser.observe()
        assert len(changed) == 1
        changed = changed[0]
        validate_contract("ObservedPost", changed)
        assert changed["observed_metadata"] == metadata
        assert first["content_hash"] == changed["content_hash"]
        classifier = Classifier(ScoresRuntime())
        assert classifier.cache_key(first, [topic()]) == classifier.cache_key(changed, [topic()])
        with Database(tmp_path / "archive.db") as db:
            archive = ArchiveStore(db)
            initial_id = archive.observe(first)
            changed_id = archive.observe(changed)
            assert initial_id != changed_id
            assert archive.get_revision(initial_id)["observed_metadata"] == {"published_label": "1h"}
            assert archive.get_revision(changed_id)["observed_metadata"] == metadata
        stamp[0] += timedelta(seconds=1)
        replace(raw_snapshot(observed_metadata=dict(reversed(list(metadata.items())))))
        assert browser.observe() == []  # Timestamp/key order alone is no revision.
        assert reasons == []
    finally:
        browser.close()


@pytest.mark.parametrize("kind", ["embedded", "legacy"])
def test_metadata_reversion_discards_buffered_revision_and_does_not_poll_duplicate(tmp_path, kind):
    browser, replace, stamp, _ = collector(tmp_path, kind)
    original = raw_snapshot(observed_metadata={"published_label": "1h"})
    try:
        replace(original)
        assert browser.observe()[0]["observed_metadata"] == {"published_label": "1h"}
        stamp[0] += timedelta(seconds=1)
        replace(raw_snapshot(observed_metadata={"published_label": "2h"}))
        browser.scroll_next()  # B buffered before virtualization/host scroll.
        stamp[0] += timedelta(seconds=1)
        replace(original)
        assert browser.observe() == []  # Latest A retracts pending B.
        stamp[0] += timedelta(seconds=1)
        replace(raw_snapshot(observed_metadata={"published_label": "3h"}))
        assert browser.observe()[0]["observed_metadata"] == {"published_label": "3h"}
        stamp[0] += timedelta(seconds=1)
        assert browser.observe() == []
    finally:
        browser.close()


@pytest.mark.parametrize("kind", ["embedded", "legacy"])
def test_missing_metadata_becoming_observed_is_a_revision(tmp_path, kind):
    browser, replace, stamp, _ = collector(tmp_path, kind)
    try:
        replace(raw_snapshot())
        initial = browser.observe()[0]
        assert "observed_metadata" not in initial
        stamp[0] += timedelta(seconds=1)
        replace(raw_snapshot(observed_metadata={"published_label": "1h"}))
        changed = browser.observe()
        assert len(changed) == 1 and changed[0]["observed_metadata"] == {"published_label": "1h"}
        assert changed[0]["content_hash"] == initial["content_hash"]
    finally:
        browser.close()
