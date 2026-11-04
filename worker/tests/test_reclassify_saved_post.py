"""Explicit stored-post runs never enroll or send historical content."""
import pytest

from test_routing import ScoresRuntime,post,topic
import threadrelay.application as module
from threadrelay.storage.archive import ArchiveStore


def runtime(monkeypatch,*,during=lambda app:None):
    class Runtime(ScoresRuntime):
        created=0
        def __init__(self,**kwargs):Runtime.created+=1
        def start(self,manifest,*,cancellation=None):pass
        def stop(self):return {'stopped':True,'remaining':[]}
        def revoke(self):pass
        def cancel_request(self):pass
        def status(self):return {'state':'not_loaded','model_id':'synthetic'}
        def decide(self,state,questions):
            during(Runtime.app)
            return super().decide(state,questions)
    monkeypatch.setattr(module,'DecisionRuntime',Runtime)
    return Runtime


def test_saved_post_reclassification_uses_current_topics_and_keeps_source_without_routing(tmp_path,monkeypatch):
    Runtime=runtime(monkeypatch)
    with module.Application(tmp_path) as app:
        Runtime.app=app;source=post();app.config.save_topic(topic());app.posts.upsert(source)
        archive=ArchiveStore(app.db);revision=archive.observe(source)
        app.config.save_topic(topic(version=2,description='New personal romantic longing definition'))
        assert app.features.collection()[0]['classification'] is None
        result=app.test_sample({'text':source['text'],'post_id':source['post_id']})
        item=app.features.collection()[0]
        assert item['classification']==result['classification']
        assert item['classification']['topic_results'][0]['match'] is True
        assert item['topics'][0]['label']=='New personal romantic longing definition'
        run=archive.get_run(archive.list_runs()['items'][0]['run_id'])
        assert run['kind']=='collection' and run['revision_id']==revision
        assert run['input_snapshot']=={'post':source} and run['topics'][0]['version']==2
        assert archive.get_revision(revision)==source and app.posts.get(source['post_id'])==source
        assert result['sending_possible'] is False and app.telegram._client is None
        with app.db.connection() as con:
            for table in ('routing_plans','approvals','deliveries','workflow_jobs'):
                assert con.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]==0


@pytest.mark.parametrize('change',['missing','text'])
def test_unavailable_or_substituted_saved_post_is_rejected_before_starting_model(tmp_path,monkeypatch,change):
    Runtime=runtime(monkeypatch)
    with module.Application(tmp_path) as app:
        Runtime.app=app;source=post();app.config.save_topic(topic())
        if change!='missing':app.posts.upsert(source)
        before=Runtime.created
        with pytest.raises(ValueError):app.test_sample({'text':source['text'] if change=='missing' else 'Replacement text','post_id':source['post_id']})
        assert Runtime.created==before and not app._sample_active


def test_topic_changes_during_saved_post_run_do_not_publish_stale_match(tmp_path,monkeypatch):
    Runtime=runtime(monkeypatch,during=lambda app:app.config.save_topic(topic(version=2,description='Changed during inference')))
    with module.Application(tmp_path) as app:
        Runtime.app=app;source=post();app.posts.upsert(source);app.config.save_topic(topic())
        with pytest.raises(ValueError):app.test_sample({'text':source['text'],'post_id':source['post_id']})
        assert app.features.collection()[0]['classification'] is None
        assert app.telegram._client is None
