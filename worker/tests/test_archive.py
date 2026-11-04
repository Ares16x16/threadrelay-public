"""Permanent records must survive operational retention and configuration changes."""
from copy import deepcopy
from datetime import timedelta
import json

import pytest

from test_routing import NOW, post, topic
from threadrelay.storage.database import Database
from threadrelay.storage.posts import PostStore
from threadrelay.storage.retention import Retention


def store(db):
    from threadrelay.storage.archive import ArchiveStore
    return ArchiveStore(db)


def test_immutable_revisions_observations_survive_retention_and_reopen(tmp_path):
    path = tmp_path / 'app.db'
    original = post()
    changed = post(text='Later text', observed_at=(NOW + timedelta(seconds=1)).isoformat())
    with Database(path, clock=lambda: NOW) as db:
        archive = store(db)
        first = archive.observe(original)
        assert archive.observe(deepcopy(original)) == first
        second = archive.observe(changed)
        assert first != second
        PostStore(db).upsert(changed)
        Retention(db).prune(NOW + timedelta(days=90))
        assert archive.get_revision(first) == original
        with db.connection() as con:
            assert con.execute('SELECT COUNT(*) FROM archive_observations').fetchone()[0] == 3
    with Database(path, clock=lambda: NOW) as db:
        assert store(db).get_revision(first) == original
        assert store(db).get_revision(second) == changed
        page = store(db).list_posts(limit=1)
        assert page['items'][0]['revision_id'] == second
        assert page['next_cursor'] is None
        assert store(db).list_posts(search='Later')['items'][0]['revision_id'] == second



def test_post_pages_show_latest_canonical_identity_without_losing_revisions(tmp_path):
    from threadrelay.collector.canonicalize import canonicalize_observed_link
    with Database(tmp_path / 'app.db', clock=lambda: NOW) as db:
        archive = store(db)
        first_link = canonicalize_observed_link('https://www.threads.com/@Alice/post/Code_1?tracking=one')
        variant = canonicalize_observed_link('https://threads.net/@alice/post/Code_1/?tracking=two#fragment')
        first = post(**first_link, text='Old distinct wording')
        old = archive.observe(first)
        old_run = archive.start_run('collection', old, {'post': first}, [], {})
        other = archive.observe(post(post_id='threads:Other', permalink='https://threads.net/@alice/post/Other', text='Other entry'))
        latest_post = post(**variant, text='New unique wording', observed_at=(NOW + timedelta(seconds=1)).isoformat())
        latest = archive.observe(latest_post)
        page = archive.list_posts(limit=1)
        assert [item['revision_id'] for item in page['items']] == [latest]
        second = archive.list_posts(cursor=page['next_cursor'], limit=1)
        assert [item['revision_id'] for item in second['items']] == [other]
        assert second['next_cursor'] is None
        assert archive.list_posts(search='Old distinct')['items'] == []
        assert [item['revision_id'] for item in archive.list_posts(search='New unique')['items']] == [latest]
        assert archive.get_revision(old) == first
        assert archive.list_runs(revision_id=old)['items'][0]['run_id'] == old_run
        assert archive.get_revision(latest) == latest_post


def test_runs_are_bound_to_original_inputs_and_cancelled_result_cannot_change(tmp_path):
    with Database(tmp_path / 'app.db', clock=lambda: NOW) as db:
        archive = store(db); revision = archive.observe(post())
        topics = [topic()]; binding = {'model_sha256': 'a'*64, 'prompt_version': 'v1'}
        run = archive.start_run('collection', revision, {'post': post()}, topics, binding)
        topics[0]['description'] = 'Changed'; binding['prompt_version'] = 'v2'
        archive.finish_run(run, 'cancelled', error_code='session_cancelled')
        with pytest.raises(ValueError):
            archive.finish_run(run, 'succeeded', classification={'post_id': post()['post_id'], 'model_version':'x', 'topic_results':[], 'abstained':True})
        saved = archive.get_run(run)
        assert saved['topics'][0]['description'] == topic()['description']
        assert saved['binding']['prompt_version'] == 'v1'
        assert saved['status'] == 'cancelled' and saved['classification'] is None
        assert archive.list_runs(revision_id=revision)['items'][0]['run_id'] == run
        sample = archive.start_run('sample', None, {'text': 'private sample'}, [], {})
        archive.finish_run(sample, 'failed', error_code='model_unavailable')
        assert archive.get_run(sample)['input_snapshot'] == {'text': 'private sample'}


def test_interrupted_run_is_cancelled_on_reopen_without_inventing_success(tmp_path):
    path = tmp_path / 'app.db'
    with Database(path, clock=lambda: NOW) as db:
        run = store(db).start_run('sample', None, {'text':'sample'}, [], {})
    with Database(path, clock=lambda: NOW) as db:
        value = store(db).get_run(run)
        assert value['status'] == 'cancelled' and value['error_code'] == 'application_interrupted'


def test_legacy_migration_preserves_only_available_records(tmp_path):
    from importlib.resources import files
    path = tmp_path / 'legacy.db'
    migrations = [(i, files('threadrelay.storage').joinpath(f'migrations/{name}')) for i,name in
        ((1,'001_initial.sql'),(2,'002_routing.sql'),(3,'003_local_policy.sql'),(4,'004_collection.sql'))]
    with Database(path, clock=lambda: NOW, migrations=migrations) as db:
        PostStore(db).upsert(post())
        with db.transaction() as con:
            con.execute('INSERT INTO collection_classifications VALUES (?,?,?,?,?)', (post()['post_id'],'oldhash','oldtopics',json.dumps({'post_id':post()['post_id'],'model_version':'old','topic_results':[],'abstained':True}),'[]'))
        with db.transaction() as con:
            con.execute('INSERT INTO routing_plans VALUES (?,?,?)', ('old-plan',post()['post_id'],json.dumps({'snapshot_key':'a'*64,'classification':{'post_id':post()['post_id'],'model_version':'older','topic_results':[],'abstained':True}})))
    with Database(path, clock=lambda: NOW) as db:
        archive = store(db); revision = archive.list_posts()['items'][0]
        assert len(archive.list_runs()['items']) == 2
        assert revision['provenance'] == 'legacy_import'
        assert archive.get_revision(revision['revision_id']) == post()
        run = archive.get_run(archive.list_runs()['items'][0]['run_id'])
        assert run['topics'] == [] and run['provenance'] == 'legacy_import'
        assert run['started_at'] is None and run['input_snapshot'] == {}


def test_bounded_summaries_and_chunks_preserve_long_unicode_input(tmp_path):
    with Database(tmp_path / 'app.db', clock=lambda: NOW) as db:
        archive = store(db)
        for index in range(3):
            archive.observe(post(text='愛'*100000, post_id=f'threads:{index}'))
        page = archive.list_posts(limit=100)
        assert len(json.dumps(page).encode()) < 900000
        run = archive.start_run('sample', None, {'text':'愛'*100000}, [topic(positive_examples=['愛'*100000])], {})
        offset, parts, digest = 0, [], None
        while offset is not None:
            part = archive.read_detail('run', run, offset, digest)
            assert len(json.dumps(part).encode()) < 900000
            parts.append(part['data']); offset = part['next_offset']; digest = part['digest']
        assert json.loads(''.join(parts)) == archive.get_run(run)
        for kwargs in ({'limit':0},{'limit':101},{'cursor':'garbage'}):
            with pytest.raises(ValueError): archive.list_posts(**kwargs)


def test_pipeline_records_every_run_without_topic_or_account_dependency(tmp_path):
    from test_collection import collect
    from threadrelay.storage.routing import ConfigStore
    with Database(tmp_path / 'app.db', clock=lambda: NOW) as db:
        collect(db, 'dry_run')
        ConfigStore(db).save_topic(topic(version=2, description='Changed topic'))
        collect(db, 'dry_run')
        runs = store(db).list_runs()['items']
        assert len(runs) == 2
        assert all(run['status'] == 'succeeded' for run in runs)
        assert store(db).get_run(runs[1]['run_id'])['topics'][0]['description'] == topic()['description']
        assert 'model_sha256' in runs[0]['binding']


def test_pipeline_cancel_records_cancelled_run_and_preserves_captured_source(tmp_path):
    from threading import Event
    from test_collection import NoSend
    from test_routing import ScoresRuntime, running
    from threadrelay.inference.decision import Classifier
    from threadrelay.pipeline import Pipeline
    from threadrelay.routing.policy import Router
    from threadrelay.storage.routing import ConfigStore
    with Database(tmp_path / 'app.db', clock=lambda: NOW) as db:
        session = running('dry_run'); config = ConfigStore(db); config.save_topic(topic())
        class Slow(ScoresRuntime):
            def decide(self, state, questions):
                session.pause()
                return super().decide(state, questions)
        classifier = Classifier(Slow())
        Pipeline(session, Event(), PostStore(db), config, classifier, Router(db, classifier=classifier), NoSend()).process(post())
        run = store(db).list_runs()['items'][0]
        assert run['status'] == 'cancelled' and run['classification'] is None
        assert store(db).get_revision(run['revision_id']) == post()


@pytest.mark.parametrize('outcome', ['success', 'cancel', 'unavailable', 'uncertain'])
def test_explicit_sample_is_permanently_recorded_but_never_an_observed_revision(tmp_path, monkeypatch, outcome):
    from test_routing import ScoresRuntime
    import threadrelay.application as module
    class Runtime(ScoresRuntime):
        def __init__(self, **kwargs): pass
        def start(self, manifest,*,cancellation=None): pass
        def stop(self): return {'stopped':True, 'remaining':[]}
        def revoke(self): pass
        def cancel_request(self):pass
        def status(self):return {'state':'not_loaded','model_id':'synthetic'}
        def decide(self, state, questions):
            if outcome == 'cancel': app._cancel_sample()
            if outcome == 'unavailable':
                from threadrelay.inference.runtime import RuntimeUnavailable
                raise RuntimeUnavailable('synthetic failure')
            if outcome == 'uncertain': return {key: 0.5 for key in questions}
            return super().decide(state, questions)
    monkeypatch.setattr(module, 'DecisionRuntime', Runtime)
    with module.Application(tmp_path) as app:
        app.config.save_topic(topic())
        if outcome == 'cancel':
            with pytest.raises(RuntimeError): app.test_sample({'text':'Sample permanently saved'})
        else:
            app.test_sample({'text':'Sample permanently saved'})
        run = store(app.db).get_run(store(app.db).list_runs()['items'][0]['run_id'])
        assert run['kind'] == 'sample' and run['revision_id'] is None
        assert run['input_snapshot']['text'] == 'Sample permanently saved'
        assert run['status'] == {'cancel':'cancelled','unavailable':'failed'}.get(outcome, 'succeeded')
        assert run['error_code'] == {'cancel':'session_cancelled','unavailable':'model_unavailable'}.get(outcome)
        assert store(app.db).list_posts()['items'] == []


def test_archive_query_dispatch_is_bounded_and_account_independent(tmp_path):
    from threadrelay.main import dispatch
    from threadrelay.features import LocalFeatures
    with Database(tmp_path / 'app.db', clock=lambda: NOW) as db:
        revision = store(db).observe(post())
        features = LocalFeatures(db)
        event = dispatch({'version':1,'id':'archive','type':'QueryArchivePosts','payload':{'limit':1}}, features=features)
        assert event['type'] == 'ArchivePosts'
        assert event['payload']['items'][0]['revision_id'] == revision
        detail = dispatch({'version':1,'id':'detail','type':'ReadArchiveDetail','payload':{'kind':'revision','record_id':revision,'offset':0}}, features=features)
        assert detail['type'] == 'ArchiveDetailChunk'
        assert json.loads(detail['payload']['data']) == post()


def test_observed_metadata_remains_exact_in_archive_and_missing_stays_missing(tmp_path):
    value = post(observed_metadata={'display_name':'Observed name', 'published_label':'2h',
        'engagement':[{'kind':'likes','label':'1.2K'}],
        'media':[{'kind':'image','url':'https://example.invalid/visible.jpg','alt':'visible context'}]})
    with Database(tmp_path / 'app.db', clock=lambda: NOW) as db:
        archive = store(db)
        assert archive.get_revision(archive.observe(value)) == value
        assert 'observed_metadata' not in archive.get_revision(archive.observe(post()))


def test_permanent_delivery_outcome_survives_operational_expiry(tmp_path):
    from test_storage import snapshot, post as delivery_post, delivery, fixture
    from threadrelay.storage.outbox import AccountStore, Outbox
    from threadrelay.features import LocalFeatures
    with Database(tmp_path / 'app.db', clock=lambda: NOW) as db:
        AccountStore(db).activate(snapshot())
        PostStore(db).upsert(delivery_post())
        outbox = Outbox(db); outbox.save_approval(fixture('Approval')); outbox.enqueue(delivery())
        claim = outbox.claim_next('worker')
        assert claim is not None
        # A definitely uninitiated cancellation is an outcome, not deletion.
        with db.transaction() as con:
            from threadrelay.storage.outbox import _set_state
            row = con.execute('SELECT * FROM deliveries').fetchone()
            _set_state(con, row, 'cancelled', db.now())
        Retention(db).prune(NOW + timedelta(days=90))
        assert outbox.get(claim['id']) is None
        history = LocalFeatures(db).history()
        assert history[0]['delivery']['state'] == 'cancelled'
        assert history[0]['attempts'][0]['attempt_id'] == claim['attempt_id']


def test_chunked_run_read_rejects_a_changed_running_snapshot(tmp_path):
    with Database(tmp_path / 'app.db', clock=lambda: NOW) as db:
        archive = store(db)
        run = archive.start_run('sample', None, {'text':'a'*100000}, [topic()], {})
        first = archive.read_detail('run', run)
        assert first['next_offset'] is not None
        archive.finish_run(run, 'cancelled', error_code='session_cancelled')
        with pytest.raises(ValueError):
            archive.read_detail('run', run, first['next_offset'], first['digest'])


def test_local_planner_dispatch_validates_and_returns_closed_response(tmp_path):
    from threadrelay.main import dispatch
    from threadrelay.features import LocalFeatures
    template = {'id':'url','version':1,'name':'Post URL','body':'{{url}}','readonly':True,
                'created_at':NOW.isoformat(),'updated_at':NOW.isoformat()}
    class Planner:
        def save_template(self, payload):
            assert payload['body'] == '{{url}}'
            return template
    with Database(tmp_path / 'app.db', clock=lambda: NOW) as db:
        features = LocalFeatures(db, workflow=Planner())
        result = dispatch({'version':1,'id':'plan','type':'SaveMessageTemplate','payload':{
            'id':None,'expected_version':None,'name':'Post URL','body':'{{url}}'}}, features=features)
        assert result['type'] == 'MessageTemplate' and result['payload'] == {'template':template}


def test_delivery_history_pages_and_details_have_stable_cursor(tmp_path):
    from test_storage import snapshot, post as delivery_post, delivery, fixture
    from threadrelay.storage.outbox import AccountStore, Outbox
    with Database(tmp_path / 'app.db', clock=lambda: NOW) as db:
        AccountStore(db).activate(snapshot()); PostStore(db).upsert(delivery_post())
        outbox = Outbox(db); outbox.save_approval(fixture('Approval')); outbox.enqueue(delivery())
        archive = store(db)
        page = archive.list_deliveries(limit=1)
        assert page['items'][0]['attempt_count'] == 0
        assert page['next_cursor'] is None
        assert archive.get_delivery(delivery()['id'])['delivery'] == delivery()
        assert archive.list_deliveries(search='not-present')['items'] == []


def test_workflow_delivery_contract_keeps_route_authority_separate():
    from test_storage import delivery
    from threadrelay.contracts import validate_contract, ProtocolError
    routed = delivery()
    with pytest.raises(ProtocolError):
        validate_contract('Delivery', {**routed,'route_refs':[]})
    validate_contract('Delivery', {**routed,'route_refs':[], 'workflow_job_id':'job'})
    with pytest.raises(ProtocolError):
        validate_contract('Delivery', {**routed, 'workflow_job_id':'job'})
