"""Per-topic relevance policy over synthetic owned inference; no live model."""
from copy import deepcopy

import pytest

from test_inference import setup as runtime_setup, active, post, response, topic
from threadrelay.inference.decision import Classifier, InvalidTopicConfiguration, StaleClassification


@pytest.fixture
def runtime_env(tmp_path):
    return runtime_setup.__wrapped__(tmp_path)


def test_topic_minimum_changes_match_without_changing_the_model_score(runtime_env):
    runtime, client = active(runtime_env)
    client.result = response({'topic-a': .9, 'topic-b': .9})
    result = Classifier(runtime).classify(post(), [topic(minimum_score=.8), topic('topic-b', minimum_score=.95)])
    assert result['topic_results'] == [
        {'topic_id':'topic-a', 'match':True, 'abstained':False, 'score':.9},
        {'topic_id':'topic-b', 'match':False, 'abstained':True, 'score':None},
    ]


@pytest.mark.parametrize('score,match,abstained', [(.1,False,False), (.1001,False,True), (.7999,False,True), (.8,True,False), (1.,True,False)])
def test_topic_cutoff_and_existing_negative_boundary_are_inclusive(runtime_env, score, match, abstained):
    runtime, client = active(runtime_env)
    client.result = response({'topic-a':score})
    result = Classifier(runtime).classify(post(), [topic(minimum_score=.8)])['topic_results'][0]
    assert (result['match'], result['abstained']) == (match, abstained)
    assert result['score'] == (None if abstained else score)


def test_legacy_topic_keeps_the_classifiers_existing_default_policy(runtime_env):
    runtime, client = active(runtime_env)
    client.result = response({'topic-a':.85})
    classifier = Classifier(runtime, positive_threshold=.82, negative_threshold=.35)
    assert classifier.classify(post(), [topic()])['topic_results'][0]['match'] is True
    assert classifier.classify(post(), [topic(minimum_score=.9)])['topic_results'][0]['abstained'] is True


@pytest.mark.parametrize('minimum', [None, True, '0.9', .1, .109, 1.01, float('nan'), float('inf')])
def test_invalid_minimum_fails_before_model_inference(runtime_env, minimum):
    runtime, client = active(runtime_env)
    with pytest.raises(InvalidTopicConfiguration):
        Classifier(runtime).classify(post(), [topic(minimum_score=minimum)])
    assert client.calls == []


def test_minimum_cannot_overlap_a_custom_negative_policy(runtime_env):
    runtime, client = active(runtime_env)
    with pytest.raises(InvalidTopicConfiguration):
        Classifier(runtime, positive_threshold=.82, negative_threshold=.35).classify(post(), [topic(minimum_score=.2)])
    assert client.calls == []


def test_changing_only_the_minimum_invalidates_a_bound_classification(runtime_env):
    runtime, client = active(runtime_env)
    client.result = response({'topic-a':.85})
    classifier = Classifier(runtime)
    topics = [topic(minimum_score=.8)]
    decision = classifier.classify_bound(post(), topics)
    assert decision.for_snapshot(post(), topics, classifier)['topic_results'][0]['match'] is True
    changed = deepcopy(topics)
    changed[0]['minimum_score'] = .9
    with pytest.raises(StaleClassification):
        decision.for_snapshot(post(), changed, classifier)


def test_threshold_edits_refresh_current_results_but_preserve_all_history_after_retention(tmp_path):
    from datetime import timedelta

    from test_collection import collect
    from test_routing import NOW, topic as saved_topic
    from threadrelay.features import LocalFeatures
    from threadrelay.storage.archive import ArchiveStore
    from threadrelay.storage.database import Database
    from threadrelay.storage.retention import Retention
    from threadrelay.storage.routing import ConfigStore

    path = tmp_path / 'app.db'
    with Database(path, clock=lambda: NOW) as db:
        config = ConfigStore(db)
        config.save_topic(saved_topic(minimum_score=.9))
        collect(db, 'dry_run')
        assert LocalFeatures(db).collection()[0]['classification']['topic_results'][0]['match'] is True
        config.save_topic(saved_topic(version=2, minimum_score=1))
        assert LocalFeatures(db).collection()[0]['classification'] is None
        collect(db, 'dry_run')
        assert LocalFeatures(db).collection()[0]['classification']['topic_results'][0]['abstained'] is True
        Retention(db).prune(NOW + timedelta(days=90))
        assert LocalFeatures(db).collection() == []
    with Database(path, clock=lambda: NOW) as db:
        archive = ArchiveStore(db)
        runs = [archive.get_run(row['run_id']) for row in archive.list_runs()['items']]
        assert len(runs) == 2
        assert {run['topics'][0]['minimum_score'] for run in runs} == {.9, 1}
        assert {run['classification']['topic_results'][0]['match'] for run in runs} == {True, False}
        assert all(run['status'] == 'succeeded' and archive.get_revision(run['revision_id']) for run in runs)
