"""A verified synthetic model belongs to the app, never to one collection."""
from threading import Event
import time

import pytest

import threadrelay.application as module
from threadrelay.inference.provision import ModelProvisioner
from threadrelay.inference.runtime import DecisionRuntime
from test_inference import Client,Lease,Launcher,response,topic
from test_model_manifest import SYNTHETIC_GGUF,synthetic_catalog


@pytest.fixture
def warm_app(tmp_path,monkeypatch):
    catalog=synthetic_catalog();record=catalog['models'][0]
    path=tmp_path/'models'/record['filename'];path.parent.mkdir();path.write_bytes(SYNTHETIC_GGUF)
    client=Client(path,catalog);lease=Lease(path,catalog);launcher=Launcher(lease)
    client.result=response({'topic-a':.9})
    monkeypatch.setattr(module,'load_catalog',lambda:catalog)
    monkeypatch.setattr(module,'ModelProvisioner',lambda:ModelProvisioner(catalog=catalog))
    monkeypatch.setattr(module,'NativeInferenceLauncher',lambda *_:launcher)
    monkeypatch.setattr(module,'DecisionRuntime',lambda **kwargs:DecisionRuntime(**kwargs,client_factory=lambda *_:client))
    class Broker:
        def capabilities(self):return {'inference':True,'telegram':False,'browser':True,'automatic':False}
        def embedded_threads_available(self):return True
    app=module.Application(tmp_path,broker=Broker())
    app.config.save_topic(topic())
    try:yield app,launcher,lease,client
    finally:
        app.close()
        assert lease.closed==1


def wait_ready(app):
    deadline=time.monotonic()+2
    while app.app_status()['model']['state']!='ready' and time.monotonic()<deadline:time.sleep(.01)
    assert app.app_status()['model']['state']=='ready'


def test_startup_loads_verified_model_and_samples_reuse_it_until_app_quit(warm_app):
    app,launcher,lease,_=warm_app
    wait_ready(app)
    assert app.session.snapshot['state']=='stopped' and app.app_status()['activity'] is None
    assert len(launcher.specs)==1
    for text in ('First synthetic sample','Second synthetic sample'):
        assert app.test_sample({'text':text})['sending_possible'] is False
        assert app.app_status()['model']['state']=='ready'
    assert len(launcher.specs)==1 and lease.closed==0
    app.session.pause();app.session.stop()
    assert app.app_status()['model']['state']=='ready' and lease.closed==0


def test_collection_lease_closes_browser_but_keeps_warm_model(warm_app):
    from threadrelay.pipeline import SessionPreparation
    app,launcher,lease,_=warm_app;wait_ready(app)
    closed=[]
    class Browser:
        def open(self,_):pass
        def close(self):closed.append(True)
    app.session._setup_check=lambda _: {'allowed':True,'reason':None}
    token=app.session.begin_start({'feed_url':'https://www.threads.com/','mode':'dry_run'})
    prepare=SessionPreparation(browser_factory=lambda _:Browser(),runtime=app.runtime,manifest=app.runtime._manifest,
        profile=app.root/'browser',pipeline_factory=lambda _:None,session=app.session,
        prepare_runtime=app._ensure_model,retain_runtime=True)
    owned=prepare({},Event());app.session.complete_start(token)
    app.session.pause()
    assert owned.close()=={'stopped':True,'remaining':[]}
    assert closed==[True] and lease.closed==0 and len(launcher.specs)==1
    assert app.app_status()['model']['state']=='ready'


def test_quit_forwards_application_shutdown_event_to_pending_model_activation(tmp_path,monkeypatch):
    app=module.Application(tmp_path)
    entered=Event();finished=Event();observed=[]
    def pending_start(manifest,*,cancellation):
        observed.append(cancellation);entered.set()
        assert cancellation.wait(2)
        finished.set()
    monkeypatch.setattr(app.runtime,'start',pending_start)
    try:
        app._start_model()
        assert entered.wait(1)
        app.close()
        assert observed==[app._model_shutdown] and finished.is_set()
        assert not app._model_thread.is_alive()
    finally:
        if not app._model_shutdown.is_set():app.close()
