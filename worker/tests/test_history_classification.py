"""History inference eligibility never supplies source/send authority; local doubles."""
from threading import Event

import pytest

from test_delivery_workflow import workflow_env as _workflow_env, configure, make_ready
from test_pipeline import NoSend
from test_routing import env as _routing_env, post, route, running

from threadrelay.pipeline import Pipeline
from threadrelay.storage.archive import ArchiveStore
from threadrelay.storage.posts import PostStore


@pytest.fixture
def history_env(tmp_path):
    yield from _routing_env.__wrapped__(tmp_path)


@pytest.fixture
def history_workflow_env(tmp_path):
    yield from _workflow_env.__wrapped__(tmp_path)


def unknown_audience_post():
    return post(visibility="unknown", quality_flags=[{"code": "visibility_unverified", "detail": "visibility unverified"}])


def test_complete_unknown_audience_gets_history_match_without_automatic_delivery(history_env):
    env = history_env
    env.config.save_route(route(version=2, mode="auto"))
    source = unknown_audience_post()
    session = running("auto")
    Pipeline(session, Event(), PostStore(env.db), env.config, env.classifier, env.router, NoSend()).process(source)
    archive = ArchiveStore(env.db)
    run = archive.get_run(archive.list_runs()["items"][0]["run_id"])
    assert run["status"] == "succeeded" and run["classification"]["abstained"] is False
    assert run["classification"]["topic_results"][0]["match"] is True
    assert run["input_snapshot"] == {"post": source}
    assert archive.get_revision(run["revision_id"])["quality_flags"] == source["quality_flags"]
    assert run["binding"]["cache_version"] == "threadrelay-classification-v4"
    assert session.snapshot["counters"]["matched"] == 1 and session.snapshot["counters"]["queued"] == 0
    with env.db.connection() as con:
        assert con.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0
    proposal = env.router.plan(source, env.classifier.classify_bound(source, env.config.topics()))[0]
    assert proposal["approval_required"] is True
    with pytest.raises(ValueError, match="approval"):
        env.router.stage(proposal)


def test_history_match_on_unknown_audience_never_qualifies_delivery_source(history_workflow_env):
    env = history_workflow_env
    make_ready(env)  # Synthetic binding; no account/network process.
    source = unknown_audience_post()
    classifier = env.sender.router.classifier
    topics = env.config.topics()
    revision = env.archive.observe(source)
    run = env.archive.start_run("collection", revision, {"post": source}, topics, classifier.metadata)
    classification = classifier.classify(source, topics)
    assert classification["abstained"] is False and classification["topic_results"][0]["match"] is True
    env.archive.finish_run(run, "succeeded", classification=classification)
    draft = configure(env, sources=[revision])
    preview = env.workflow.preview(draft["id"], draft["version"])
    assert not preview["approvable"] and "source_ineligible" in preview["blocked_reasons"]
    with pytest.raises(ValueError):
        env.workflow.approve(draft["id"], draft["version"], preview["preview_hash"])
    env.workflow.tick()
    assert not any(request["@type"] == "sendMessage" for request in env.binding.requests)


@pytest.mark.parametrize('unavailable', [True, False])
def test_archive_distinguishes_runtime_failure_from_valid_uncertain_decision(history_env, unavailable):
    from test_routing import ScoresRuntime
    from threadrelay.inference.decision import Classifier
    from threadrelay.inference.runtime import RuntimeUnavailable
    class Runtime(ScoresRuntime):
        def decide(self, state, questions):
            if unavailable:
                raise RuntimeUnavailable('synthetic failure')
            return {key: 0.5 for key in questions}
    env=history_env;classifier=Classifier(Runtime());env.router.classifier=classifier
    sender=NoSend();session=running('review');arrivals=[]
    Pipeline(session, Event(), PostStore(env.db), env.config, classifier, env.router, sender,
        on_history=lambda *args: arrivals.append(args)).process(env.post)
    archive=ArchiveStore(env.db)
    run=archive.get_run(archive.list_runs()['items'][0]['run_id'])
    assert run['status']==('failed' if unavailable else 'succeeded')
    assert run['error_code']==('model_unavailable' if unavailable else None)
    if unavailable:
        assert run['classification'] is None and arrivals==[] and sender.calls==0
    else:
        assert run['classification']['abstained'] is True
