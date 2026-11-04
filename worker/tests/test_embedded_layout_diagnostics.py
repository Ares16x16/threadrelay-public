"""Fixed metadata-only native debug script over sanitized DOM, never live proof."""
from pathlib import Path

from test_public_collector import FIXTURE, snapshot

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "apps/desktop/src-tauri/native/embedded-layout-diagnostics.js"


def diagnose(html=None):
    return snapshot(html, page_script=True, script=SCRIPT.read_text())


def test_diagnostics_return_only_closed_bounded_metadata_not_post_content():
    result = diagnose()
    assert set(result) == {"schema_version", "candidate_count", "candidate_limit", "sampled_count", "samples"}
    assert result["schema_version"] == 1 and result["candidate_count"] == 2
    assert result["sampled_count"] == 2 and result["candidate_limit"] is False
    for sample in result["samples"]:
        assert set(sample) == {"reason", "recognized", "column_count", "header_anchor_count", "header_time_count", "content_span_count", "nested_boundary_count", "author_checks"}
        assert sample["reason"] == "recognized" and sample["recognized"] is True
        assert all(type(value) is int and 0 <= value <= 101 for key, value in sample.items() if key not in {"reason", "recognized", "author_checks"})
    serialized = str(result)
    assert all(secret not in serialized for secret in ["alice", "boyfriend", "threads.com", "沉船", "Public_EN_1"])


def test_diagnostics_report_exact_rejection_branch_without_weakening_parser():
    html = FIXTURE.read_text().replace('data-fixture="columns"', 'data-fixture="columns"><div></div', 1)
    result = diagnose(html)
    assert result["samples"][0]["reason"] == "public_columns"
    assert result["samples"][0]["recognized"] is False
    html = FIXTURE.read_text().replace('<span dir="auto" data-fixture="text">', '<span data-fixture="text">', 1)
    assert diagnose(html)["samples"][0]["reason"] == "public_body_count"


def test_diagnostics_bound_visible_samples_and_candidate_inspection():
    html = FIXTURE.read_text()
    result = diagnose(html * 60)
    assert result["candidate_count"] == 101 and result["candidate_limit"] is True
    assert result["sampled_count"] == 4 and len(result["samples"]) == 4
    hidden = html.replace('data-fixture="english-card"', 'data-fixture="english-card" hidden').replace('data-fixture="cantonese-card"', 'data-fixture="cantonese-card" data-test-rect="10,1000,100,20"')
    assert diagnose(hidden)["samples"] == []


def test_diagnostics_mirror_current_parser_with_only_static_issue_labels():
    import re
    from threadrelay.collector.extractor import RENDERED_SNAPSHOT_JS

    source = SCRIPT.read_text()
    mirrored = source.split('const snapshotCard = ', 1)[1].split(';\n    // END EXACT MIRROR', 1)[0]
    restored = re.sub(r"return \{issue:'([^']+)',diagnosticReason:'[a-z_]+'\}", r"return issue('\1')", mirrored)
    restored = restored.replace('        recordAuthorMetadata(header, headerLinks, authors, platformUrl, boundary, visible, displayed, unsupportedTextLayout, nonDefault, clips);\n', '')
    restored = restored.replace('        recordBodyMetadata(content,bodies,text);\n', '')
    assert restored == RENDERED_SNAPSHOT_JS
    assert source.count('output.samples.length>=4') == 1


def test_diagnostics_distinguish_profile_shape_visibility_and_text_layout():
    checks = diagnose()["samples"][0]["author_checks"]
    assert checks["profile_path_count"] == checks["profile_exact_count"] == checks["profile_visible_count"] == checks["profile_layout_supported_count"] == checks["filtered_author_count"] == 1
    html = FIXTURE.read_text().replace('href="https://www.threads.com/@alice"', 'href="https://www.threads.com/@alice?private_query_must_not_escape=1"')
    sample = diagnose(html)["samples"][0]
    assert sample["reason"] == "public_author_count"
    assert sample["author_checks"]["profile_path_count"] == 1
    assert sample["author_checks"]["profile_exact_count"] == sample["author_checks"]["filtered_author_count"] == 0
    assert "private_query" not in str(sample)
    html = FIXTURE.read_text().replace('data-fixture="header"', 'data-fixture="header" style="transform:translateY(0px)"', 1)
    checks = diagnose(html)["samples"][0]["author_checks"]
    assert checks["profile_transform"] is True and checks["profile_visible_count"] == 0
    html = FIXTURE.read_text().replace('<span dir="auto">alice</span>', '<span dir="auto" style="transform:translateY(0px)">alice</span>', 1)
    checks = diagnose(html)["samples"][0]["author_checks"]
    assert checks["profile_visible_count"] == 1 and checks["profile_layout_supported_count"] == 0


def test_diagnostics_distinguish_multiple_supported_author_links():
    anchor = '<a role="link" href="https://www.threads.com/@alice"><span dir="auto">alice</span></a>'
    sample = diagnose(FIXTURE.read_text().replace(anchor, anchor + anchor, 1))["samples"][0]
    assert sample["reason"] == "public_author_count"
    assert sample["author_checks"]["profile_exact_count"] == sample["author_checks"]["filtered_author_count"] == 2


def test_diagnostics_prove_only_finite_identity_or_pure_2d_translation():
    for transform, nontranslation in [
        ("none", False),
        ("matrix(1, 0, 0, 1, 0, 0)", False),
        ("matrix(1, 0, 0, 1, -24.5, 1e2)", False),
        ("matrix(2, 0, 0, 1, 0, 0)", True),
        ("matrix(0, 1, -1, 0, 0, 0)", True),
        ("matrix(1, .5, 0, 1, 0, 0)", True),
        ("matrix3d(1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1)", True),
        ("translateY(0px)", True),
        ("matrix(1, 0, 0, 1, Infinity, 0)", True),
        ("matrix(1, 0, 0, 1, , 0)", True),
    ]:
        html = FIXTURE.read_text().replace('data-fixture="header"', f'data-fixture="header" style="transform:{transform}"', 1)
        checks = diagnose(html)["samples"][0]["author_checks"]
        assert checks["profile_transform_nontranslation"] is nontranslation, transform


def test_diagnostics_distinguish_time_link_and_node_evidence_without_values():
    checks = diagnose()["samples"][0]["author_checks"]
    assert checks["time_link_count"] == checks["time_link_visible_count"] == checks["time_node_visible_count"] == 1
    assert checks["time_display_contents_count"] == 0
    assert all(type(value) in (int, bool) for key, value in checks.items() if key not in {'clip_geometry', 'body_geometry'})
    assert len(checks['clip_geometry']) <= 3
    for row in checks['clip_geometry']:
        assert len(row) == 19 and all(value is None or isinstance(value, (int, float)) and abs(value) <= 10000 for value in row)
    assert checks['body_candidate_count'] == 1
    assert len(checks['body_geometry']) == 1
    assert all(len(row) == 13 and all(isinstance(value, (int, float)) and -1 <= value <= 100001 for value in row) for row in checks['body_geometry'])
