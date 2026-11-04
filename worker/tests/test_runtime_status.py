"""Read-only lifecycle status is separate from starting or using the model."""
from threading import Event, Thread

import pytest

from test_inference import setup as runtime_setup, model, response
from threadrelay.inference.runtime import DecisionRuntime


@pytest.fixture
def runtime_env(tmp_path):
    return runtime_setup.__wrapped__(tmp_path)


def test_idle_status_never_starts_or_probes_dependencies(runtime_env, tmp_path):
    runtime, launcher, _, client, _, _ = runtime_env
    assert runtime.status() == {'state':'not_loaded','model_id':'h2o-lightning-4b-q4-k-m'}
    assert launcher.specs == [] and client.calls == []
    assert DecisionRuntime(model_file=tmp_path/'missing').status()['state'] == 'unavailable'


def test_status_observes_start_classification_and_cleanup_without_authorizing(runtime_env):
    runtime, launcher, lease, client, catalog, _ = runtime_env
    entered, release = Event(), Event()
    launch = launcher.launch
    def delayed_launch(spec):
        entered.set()
        assert release.wait(2)
        return launch(spec)
    launcher.launch = delayed_launch
    thread=Thread(target=lambda: runtime.start(model(catalog)))
    thread.start()
    assert entered.wait(2)
    assert runtime.status()['state'] == 'starting'
    release.set();thread.join(2)
    assert not thread.is_alive() and runtime.status()['state'] == 'ready'
    observed=[]
    client.result=lambda: (observed.append(runtime.status()['state']) or response({'topic-a':.95}))
    runtime.decide({'post_text':'Synthetic text','quote':None},{'topic-a':{'type':'noul','instructions':'A synthetic proposition'}})
    assert observed == ['classifying'] and runtime.status()['state'] == 'ready'
    lease.close_result={'stopped':False,'remaining':['inference']}
    runtime.stop()
    assert runtime.status()['state'] == 'stopping'
    lease.close_result={'stopped':True,'remaining':[]}
    runtime.stop()
    assert runtime.status()['state'] == 'not_loaded'


def test_persistent_request_cancellation_keeps_owned_model_ready_for_next_request(runtime_env):
    runtime,launcher,lease,client,catalog,_=runtime_env
    runtime._persistent=True
    runtime.start(model(catalog))
    entered,release=Event(),Event();failures=[]
    def pending():
        entered.set();assert release.wait(2)
        return response({'topic-a':.9})
    client.result=pending
    def classify():
        try:runtime.decide({'post_text':'First','quote':None},{'topic-a':{'type':'noul','instructions':'A proposition'}})
        except Exception as error:failures.append(error)
    thread=Thread(target=classify);thread.start();assert entered.wait(2)
    runtime.cancel_request();release.set();thread.join(2)
    assert failures and not thread.is_alive()
    assert runtime.status()['state']=='ready' and lease.closed==0 and len(launcher.specs)==1
    client.result=response({'topic-a':.8})
    assert runtime.decide({'post_text':'Second','quote':None},{'topic-a':{'type':'noul','instructions':'A proposition'}})=={'topic-a':.8}
    assert lease.closed==0 and len(launcher.specs)==1
    runtime.stop();assert lease.closed==1


def test_pause_cancels_decision_admission_without_session_runtime_lock_inversion(runtime_env):
    from threading import RLock
    runtime,launcher,lease,client,catalog,_=runtime_env
    runtime._persistent=True;runtime.start(model(catalog))
    session_lock=RLock();entered=Event();proceed=Event();pause_done=Event();failures=[]
    def can_infer():
        entered.set();assert proceed.wait(2)
        if not session_lock.acquire(timeout=1):raise RuntimeError('lifecycle lock inversion')
        session_lock.release();return True
    runtime._can_infer=can_infer
    def pause():
        with session_lock:
            assert entered.wait(2);proceed.set();runtime.cancel_request()
        pause_done.set()
    def decide():
        try:runtime.decide({'post_text':'Cancelled before dispatch','quote':None},{'topic-a':{'type':'noul','instructions':'A proposition'}})
        except Exception as error:failures.append(error)
    pauser=Thread(target=pause);decision=Thread(target=decide)
    pauser.start();decision.start()
    prompt=pause_done.wait(.3)
    pauser.join(2);decision.join(2)
    assert prompt and not pauser.is_alive() and not decision.is_alive()
    assert failures and lease.closed==0 and runtime.status()['state']=='ready'
    assert all(path!='/v1/systemone' or '__canary' in payload['questions'] for _,path,payload in client.calls)
    runtime.stop()


def test_persistent_model_lost_ownership_is_closed_and_no_longer_reports_ready(runtime_env):
    from threadrelay.inference.runtime import RuntimeUnavailable
    runtime,_,lease,_,catalog,_=runtime_env
    runtime._persistent=True;runtime.start(model(catalog));lease.status='exited'
    with pytest.raises(RuntimeUnavailable):
        runtime.decide({'post_text':'Lost service','quote':None},{'topic-a':{'type':'noul','instructions':'A proposition'}})
    assert runtime.status()['state']=='not_loaded' and lease.closed==1
