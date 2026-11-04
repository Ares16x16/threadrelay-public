"""Real SQLite activity truth; synthetic records only, with no native transport."""
from datetime import timedelta
import json
import sqlite3
import time

import pytest

from test_routing import NOW, post, topic
from test_storage import attempt, delivery, fixture, snapshot
from threadrelay.storage.archive import ArchiveStore
from threadrelay.storage.database import Database, encode
from threadrelay.storage.outbox import AccountStore, Outbox
from threadrelay.storage.posts import PostStore


def store(db):
    from threadrelay.storage.activity import ActivityStore
    return ActivityStore(db)


def classification(source, *, match=False, abstained=False):
    return {'post_id':source['post_id'], 'model_version':'synthetic', 'abstained':abstained,
            'topic_results':[{'topic_id':'science', 'match':match, 'abstained':abstained,
                              'score':None if abstained else 0.95 if match else 0.01}]}


def test_empty_journal_and_committed_session_events_survive_reopen(tmp_path):
    path=tmp_path/'app.db'
    with Database(path,clock=lambda:NOW) as db:
        value=store(db).query()
        assert value=={'entries':[], 'cursor':'0', 'has_more':False, 'older_cursor':None, 'as_of':NOW.isoformat()}
        store(db).append('session_running',detail='visible_feed')
    with Database(path,clock=lambda:NOW) as db:
        entry=store(db).query()['entries'][0]
        assert entry=={'sequence':'1', 'at':NOW.isoformat(), 'category':'session', 'kind':'session_running',
                      'revision_id':None, 'run_id':None, 'delivery_id':None, 'job_id':None,
                      'author':None, 'text_preview':None, 'permalink':None, 'destination':None,
                      'detail':'visible_feed', 'result':None, 'due_at':None}


def test_live_batches_are_contiguous_and_older_pages_do_not_move_forward(tmp_path):
    with Database(tmp_path/'app.db',clock=lambda:NOW) as db:
        activity=store(db)
        for index in range(7):activity.append('reading',detail=f'batch_{index}')
        latest=activity.query(limit=2)
        assert [item['sequence'] for item in latest['entries']]==['6','7']
        assert latest['cursor']=='7' and not latest['has_more'] and latest['older_cursor']=='6'
        first=activity.query(after='0',limit=2)
        assert [item['sequence'] for item in first['entries']]==['1','2']
        assert first['cursor']=='2' and first['has_more']
        second=activity.query(after=first['cursor'],limit=2)
        assert [item['sequence'] for item in second['entries']]==['3','4']
        older=activity.query(before=latest['older_cursor'],limit=2)
        assert [item['sequence'] for item in older['entries']]==['4','5']
        assert not older['has_more'] and older['older_cursor']=='4'
        oldest=activity.query(before='4',limit=100)
        assert [item['sequence'] for item in oldest['entries']]==['1','2','3']
        assert oldest['older_cursor'] is None and not oldest['has_more']
        empty=activity.query(after='7')
        assert empty['entries']==[] and empty['cursor']=='7' and not empty['has_more']


@pytest.mark.parametrize('page',[{'after':'01'}, {'after':'-1'}, {'before':'1.0'}, {'after':True},
    {'after':'9223372036854775808'}, {'after':'9'*20}, {'after':'0','before':'2'}, {'limit':0},
    {'limit':101}, {'limit':True}])
def test_activity_pages_reject_noncanonical_or_unsafe_sequences(tmp_path,page):
    with Database(tmp_path/'app.db',clock=lambda:NOW) as db:
        with pytest.raises(ValueError):store(db).query(**page)


def test_existing_writer_append_and_archive_trigger_roll_back_together(tmp_path):
    with Database(tmp_path/'app.db',clock=lambda:NOW) as db:
        with pytest.raises(RuntimeError):
            with db.transaction() as con:
                store(db).append('session_starting',connection=con)
                con.execute('INSERT INTO archive_revisions(id,post_id,payload,provenance,created_at) VALUES (?,?,?,?,?)',
                            ('revision','post',encode(post()),'captured',db.now()))
                raise RuntimeError('rollback')
        assert store(db).query()['entries']==[]
        with db.connection() as con:assert con.execute('SELECT COUNT(*) FROM archive_revisions').fetchone()[0]==0


def test_nonblocking_append_fails_immediately_under_real_writer_contention(tmp_path):
    with Database(tmp_path/'app.db',clock=lambda:NOW) as db:
        with db.transaction():
            started=time.monotonic()
            with pytest.raises(sqlite3.OperationalError,match='locked'):
                store(db).append('session_stopped',nonblocking=True)
            assert time.monotonic()-started<0.25
        assert store(db).query()['entries']==[]
        store(db).append('session_stopped',nonblocking=True)
        assert [entry['sequence'] for entry in store(db).query()['entries']]==['1']


def test_nonblocking_append_reuses_provided_writer_without_acquiring_another(tmp_path):
    with Database(tmp_path/'app.db',clock=lambda:NOW) as db:
        with db.transaction() as con:
            store(db).append('session_stopped',connection=con,nonblocking=True)
        assert [entry['kind'] for entry in store(db).query()['entries']]==['session_stopped']


@pytest.mark.parametrize('kind,values',[('native_auth',{}),('reading',{'detail':'x'*201}),
    ('reading',{'revision_id':'x'*129}),('reading',{'run_id':3}),('reading',{'job_id':''})])
def test_append_rejects_unbounded_or_unknown_metadata(tmp_path,kind,values):
    with Database(tmp_path/'app.db',clock=lambda:NOW) as db:
        with pytest.raises(ValueError):store(db).append(kind,**values)
        assert store(db).query()['entries']==[]


@pytest.mark.parametrize('match,abstained,result',[(True,False,'match'),(False,False,'no_match'),(False,True,'abstained')])
def test_archive_model_events_keep_exact_revision_and_result(tmp_path,match,abstained,result):
    with Database(tmp_path/'app.db',clock=lambda:NOW) as db:
        archive=ArchiveStore(db);original=post(text='Original 愛情')
        revision=archive.observe(original)
        assert archive.observe(original)==revision
        run=archive.start_run('collection',revision,{'post':original},[topic()],{})
        archive.observe(post(text='Later unrelated edit'))
        archive.finish_run(run,'succeeded',classification=classification(original,match=match,abstained=abstained))
        entries=store(db).query()['entries']
        assert [item['kind'] for item in entries]==['history_saved','classification_started','history_saved','classification_succeeded']
        assert entries[1]['text_preview']==entries[3]['text_preview']=='Original 愛情'
        assert entries[1]['result'] is None and entries[3]['result']==result
        assert entries[3]['author']=='synthetic' and entries[3]['permalink']==original['permalink']
        assert entries[3]['revision_id']==revision and entries[3]['run_id']==run


def test_reopen_records_interrupted_model_cancelled_once_without_success(tmp_path):
    path=tmp_path/'app.db'
    with Database(path,clock=lambda:NOW) as db:
        run=ArchiveStore(db).start_run('sample',None,{'text':'Sample input'},[],{})
    for _ in range(2):
        with Database(path,clock=lambda:NOW+timedelta(seconds=5)) as db:
            entries=store(db).query()['entries']
            assert [item['kind'] for item in entries]==['classification_started','classification_cancelled']
            assert entries[1]['run_id']==run and entries[1]['detail']=='application_interrupted'
            assert entries[1]['result'] is None


def test_upgrade_keeps_old_archive_without_fabricating_live_events(tmp_path):
    from importlib.resources import files
    names=('001_initial.sql','002_routing.sql','003_local_policy.sql','004_collection.sql',
           '005_archive.sql','006_delivery_workflow.sql')
    migrations=[(index,files('threadrelay.storage').joinpath('migrations/'+name))
                for index,name in enumerate(names,1)]
    path=tmp_path/'old.db'
    with Database(path,clock=lambda:NOW,migrations=migrations) as db:
        archive=ArchiveStore(db);revision=archive.observe(post())
        run=archive.start_run('collection',revision,{'post':post()},[],{})
        archive.finish_run(run,'succeeded',classification=classification(post()))
    with Database(path,clock=lambda:NOW+timedelta(days=1)) as db:
        assert ArchiveStore(db).get_revision(revision)==post()
        assert store(db).query()['entries']==[]


def test_failed_model_event_keeps_only_bounded_exact_sample_preview(tmp_path):
    with Database(tmp_path/'app.db',clock=lambda:NOW) as db:
        archive=ArchiveStore(db)
        run=archive.start_run('sample',None,{'text':'愛'*1000},[],{})
        archive.finish_run(run,'failed',error_code='model_unavailable')
        entries=store(db).query()['entries']
        assert [item['kind'] for item in entries]==['classification_started','classification_failed']
        assert entries[1]['detail']=='model_unavailable' and entries[1]['result'] is None
        assert entries[1]['text_preview']=='愛'*500
        assert entries[1]['revision_id'] is None and entries[1]['author'] is None and entries[1]['permalink'] is None


def test_unavailable_revision_reference_does_not_fall_back_to_another_post(tmp_path):
    with Database(tmp_path/'app.db',clock=lambda:NOW) as db:
        ArchiveStore(db).observe(post(text='Must not be guessed'))
        store(db).append('reading',revision_id='unavailable')
        entry=store(db).query()['entries'][-1]
        assert entry['revision_id']=='unavailable'
        assert entry['text_preview'] is None and entry['author'] is None and entry['permalink'] is None


def test_overlong_permalink_is_omitted_instead_of_inventing_a_truncated_link(tmp_path):
    with Database(tmp_path/'app.db',clock=lambda:NOW) as db:
        ArchiveStore(db).observe(post(permalink='https://www.threads.net/@synthetic/post/'+'a'*5000))
        assert store(db).query()['entries'][0]['permalink'] is None


def test_large_unicode_batches_remain_bounded_without_skipping_live_entries(tmp_path):
    with Database(tmp_path/'app.db',clock=lambda:NOW) as db:
        archive=ArchiveStore(db)
        for index in range(100):
            archive.observe(post(post_id=f'threads:{index}',author='愛'*500,text='情'*500,
                permalink='https://www.threads.net/@synthetic/post/'+'a'*4000))
        latest=store(db).query(limit=100)
        assert len(json.dumps(latest).encode())<805000
        assert len(latest['entries'])<100 and latest['entries'][-1]['sequence']=='100'
        first=store(db).query(after='0',limit=100)
        assert first['has_more'] and len(json.dumps(first).encode())<805000
        second=store(db).query(after=first['cursor'],limit=100)
        assert [entry['sequence'] for entry in first['entries']+second['entries']]==[str(index) for index in range(1,101)]
        assert second['cursor']=='100' and not second['has_more']


def enqueue(db):
    AccountStore(db).activate(snapshot());PostStore(db).upsert(fixture('ObservedPost'))
    outbox=Outbox(db);outbox.save_approval(fixture('Approval'));outbox.enqueue(delivery())
    return outbox


def test_delivery_activity_uses_only_committed_correlated_truth(tmp_path):
    with Database(tmp_path/'app.db',clock=lambda:NOW) as db:
        # A different archived revision must not be guessed from this post ID.
        ArchiveStore(db).observe(fixture('ObservedPost'))
        outbox=enqueue(db);claimed=outbox.claim_next('synthetic')
        entries=[item for item in store(db).query()['entries'] if item['delivery_id']]
        assert [item['kind'] for item in entries]==['delivery_pending','delivery_preparing']
        assert all(item['text_preview'] is None and item['revision_id'] is None for item in entries)
        wrong=attempt(claimed,'sent',request_extra='foreign',final_message_id='42')
        assert not outbox.transition(claimed['id'],'sending','sent',wrong)
        sending=attempt(claimed,'sending',temporary_message_id='-42')
        assert outbox.transition(claimed['id'],'sending','sending',sending)
        assert outbox.transition(claimed['id'],'sending','sending',sending)
        sent={**sending,'state':'sent','final_message_id':'42'}
        assert outbox.transition(claimed['id'],'sending','sent',sent)
        assert not outbox.transition(claimed['id'],'sending','sent',sent)
        entries=[item for item in store(db).query()['entries'] if item['delivery_id']]
        assert [item['kind'] for item in entries]==['delivery_pending','delivery_preparing','delivery_sending','delivery_sent']
        assert 'request_extra' not in json.dumps(entries) and 'attempt_id' not in json.dumps(entries)


def test_recovery_marks_incomplete_delivery_unknown_without_new_attempt(tmp_path):
    path=tmp_path/'app.db'
    with Database(path,clock=lambda:NOW) as db:
        claimed=enqueue(db).claim_next('synthetic')
    with Database(path,clock=lambda:NOW+timedelta(seconds=5)) as db:
        entries=store(db).query()['entries']
        assert [item['kind'] for item in entries]==['delivery_pending','delivery_preparing','delivery_unknown']
        assert Outbox(db).get(claimed['id'])['state']=='unknown'
        assert Outbox(db).claim_next('no_retry') is None
        assert len(Outbox(db).attempts(claimed['id']))==1


def test_workflow_activity_deduplicates_status_but_preserves_schedule_changes(tmp_path):
    with Database(tmp_path/'app.db',clock=lambda:NOW) as db:
        archive=ArchiveStore(db);revision=archive.observe(post(text='Exact scheduled source'))
        due=NOW.timestamp()+60
        with db.transaction() as con:
            con.execute('INSERT INTO workflow_drafts VALUES (?,?,?,NULL,NULL,?,?)',('draft',1,'{}',db.now(),db.now()))
            con.execute('''INSERT INTO workflow_jobs(id,version,draft_id,draft_version,kind,revision_id,post_id,text,due_at,status,reasons,approval_hash,requires_approval,context,created_at,updated_at)
                VALUES ('job',1,'draft',1,'job',?,'post','private template',?,'waiting','[]','approved',0,?,?,?)''',
                (revision,due,encode({'spec':{'target':{'destination_id':'-42'}}}),db.now(),db.now()))
            con.execute("UPDATE workflow_jobs SET version=2,context=json_set(context,'$.hidden','private context') WHERE id='job'")
            con.execute("UPDATE workflow_jobs SET due_at=?,version=3 WHERE id='job'",(due+60,))
            con.execute("UPDATE workflow_jobs SET status='blocked',reasons='[\"connection_required\"]',version=4 WHERE id='job'")
            con.execute("UPDATE workflow_jobs SET status='sending',version=5 WHERE id='job'")
            con.execute("UPDATE workflow_jobs SET status='sent',version=6 WHERE id='job'")
        entries=[item for item in store(db).query()['entries'] if item['job_id']]
        assert [item['kind'] for item in entries]==['job_waiting','job_waiting','job_blocked']
        assert [item['due_at'] for item in entries[:2]]==[(NOW+timedelta(seconds=60)).isoformat(),(NOW+timedelta(seconds=120)).isoformat()]
        assert all(item['text_preview']=='Exact scheduled source' and item['destination']=='-42' for item in entries)
        assert entries[-1]['detail']=='connection_required'
        assert 'private template' not in json.dumps(entries) and 'private context' not in json.dumps(entries)


def test_workflow_delivery_projects_only_its_exact_revision(tmp_path):
    with Database(tmp_path/'app.db',clock=lambda:NOW) as db:
        archive=ArchiveStore(db);revision=archive.observe(post(text='Exact job source'))
        with db.transaction() as con:
            con.execute('INSERT INTO workflow_drafts VALUES (?,?,?,NULL,NULL,?,?)',('draft',1,'{}',db.now(),db.now()))
            con.execute('''INSERT INTO workflow_jobs(id,version,draft_id,draft_version,kind,revision_id,due_at,status,reasons,requires_approval,context,created_at,updated_at)
                VALUES ('job',1,'draft',1,'job',?,NULL,'waiting','[]',0,'{}',?,?)''',(revision,db.now(),db.now()))
            value=delivery(workflow_job_id='job',route_refs=[])
            con.execute('INSERT INTO archive_deliveries VALUES (?,?,?,?)',(value['id'],encode(value),db.now(),db.now()))
        archive.observe(post(text='Later edited source'))
        entry=next(item for item in store(db).query()['entries'] if item['delivery_id'])
        assert entry['revision_id']==revision and entry['job_id']=='job' and entry['text_preview']=='Exact job source'
