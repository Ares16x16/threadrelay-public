"""Account-free collection results over real SQLite and the actual pipeline."""
from copy import deepcopy
from datetime import timedelta
from threading import Event

import pytest

from test_routing import NOW, ScoresRuntime, post, running, topic
from threadrelay.features import LocalFeatures
from threadrelay.inference.decision import Classifier
from threadrelay.main import dispatch
from threadrelay.pipeline import Pipeline
from threadrelay.routing.policy import Router
from threadrelay.storage.database import Database
from threadrelay.storage.posts import PostStore
from threadrelay.storage.retention import Retention
from threadrelay.storage.routing import ConfigStore


class NoSend:
    def start_next(self, _):
        return None  # The real connection-only delivery owner denies transmission.


def collect(db, mode="review", value=None):
    config = ConfigStore(db)
    if not config.topics():
        config.save_topic(topic())
    classifier = Classifier(ScoresRuntime())
    router = Router(db, classifier=classifier)
    Pipeline(running(mode), Event(), PostStore(db), config, classifier, router, NoSend()).process(value or post())


@pytest.mark.parametrize("mode", ["review", "dry_run"])
def test_guest_pipeline_exposes_classification_without_account_route_or_delivery(tmp_path, mode):
    with Database(tmp_path / "app.db", clock=lambda: NOW) as db:
        collect(db, mode)
        result = LocalFeatures(db).collection()
        assert len(result) == 1
        assert result[0]["post"]["text"] == "Synthetic research"
        assert result[0]["classification"]["topic_results"] == [
            {"topic_id": "science", "match": True, "abstained": False, "score": .95}
        ]
        assert result[0]["topics"] == [{"id": "science", "label": "Synthetic research"}]
        for table in ("accounts", "routing_routes", "routing_plans", "deliveries"):
            with db.connection() as con:
                assert con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        event = dispatch({"version": 1, "id": "read", "type": "QueryCollection", "payload": {}}, features=LocalFeatures(db))
        assert event["type"] == "Collection" and event["payload"] == result


def test_changed_topics_or_post_hide_stale_classification_and_text_expiry_removes_results(tmp_path):
    with Database(tmp_path / "app.db", clock=lambda: NOW) as db:
        collect(db)
        ConfigStore(db).save_topic(topic(version=2, description="Different topic"))
        changed = LocalFeatures(db).collection()[0]
        assert changed["classification"] is None and changed["topics"] == []
        collect(db)
        assert LocalFeatures(db).collection()[0]["classification"] is not None
        PostStore(db).upsert(post(text="Changed observed source"))
        assert LocalFeatures(db).collection()[0]["classification"] is None
        Retention(db).prune(NOW + timedelta(days=8))
        assert LocalFeatures(db).collection() == []
        with db.connection() as con:
            assert con.execute("SELECT COUNT(*) FROM collection_classifications").fetchone()[0] == 0


def test_collection_is_bounded_and_survives_reopening_without_runtime(tmp_path):
    path = tmp_path / "app.db"
    with Database(path, clock=lambda: NOW) as db:
        for index in range(105):
            value = post(post_id=f"threads:P{index}", permalink=f"https://www.threads.net/@synthetic/post/P{index}",
                         observed_at=(NOW + timedelta(seconds=index)).isoformat())
            PostStore(db).upsert(value)
        collect(db, value=post(observed_at=(NOW + timedelta(seconds=200)).isoformat()))
    with Database(path, clock=lambda: NOW) as db:
        result = LocalFeatures(db).collection()
        assert len(result) == 100
        assert result[0]["post"]["post_id"] == "threads:Abc123"
        assert result[0]["classification"]["topic_results"][0]["match"] is True
        assert result[-1]["post"]["post_id"] == "threads:P6"


def test_late_cancelled_result_does_not_publish_a_classification(tmp_path):
    with Database(tmp_path / "app.db", clock=lambda: NOW) as db:
        config = ConfigStore(db); config.save_topic(topic())
        session = running("dry_run")
        class CancelRuntime(ScoresRuntime):
            def decide(self, state, questions):
                session.pause()
                return super().decide(state, questions)
        classifier = Classifier(CancelRuntime())
        Pipeline(session, Event(), PostStore(db), config, classifier, Router(db, classifier=classifier), NoSend()).process(deepcopy(post()))
        assert LocalFeatures(db).collection()[0]["classification"] is None
