"""Owned evaluation factory over synthetic native transports, not a benchmark."""
import pytest
from test_inference import Client, Launcher, Lease
from test_model_manifest import SYNTHETIC_GGUF, model, synthetic_catalog
from threadrelay.inference.provision import ModelProvisioner
from threadrelay.inference.runtime import RuntimeUnavailable


def test_evaluation_factory_uses_pinned_runtime_and_confirms_cleanup(tmp_path):
    from threadrelay.evaluation.owned import OwnedEvaluation
    catalog = synthetic_catalog(); manifest = model(catalog)
    path = tmp_path / 'models' / catalog['models'][0]['filename']
    path.parent.mkdir(); path.write_bytes(SYNTHETIC_GGUF)
    lease, client = Lease(path, catalog), Client(path, catalog)
    launcher = Launcher(lease)
    class Reservation:
        purpose = 'evaluation'
        def __init__(self): self.released = False
        def active(self): return not self.released
        def release(self): self.released = True; return True
    reservation = Reservation(); reservation.launcher = launcher
    class Broker:
        def reserve_evaluation(self): return reservation
    owner = OwnedEvaluation(tmp_path, Broker(), catalog=catalog,
        provisioner=ModelProvisioner(catalog=catalog), client_factory=lambda *_:client)
    with owner.factory(manifest) as classifier:
        assert classifier.metadata['model_sha256'] == manifest['sha256']
        assert not reservation.released and len(launcher.specs) == 1
    assert lease.closed == 1 and reservation.released and client.cancelled


def test_unconfirmed_evaluation_cleanup_retains_reservation_until_explicit_retry(tmp_path):
    from threadrelay.evaluation.owned import OwnedEvaluation
    catalog = synthetic_catalog(); manifest = model(catalog)
    path = tmp_path / 'models' / catalog['models'][0]['filename']; path.parent.mkdir(); path.write_bytes(SYNTHETIC_GGUF)
    lease, client = Lease(path, catalog), Client(path, catalog); lease.close_result = {'stopped':False,'remaining':['inference']}
    class Reservation:
        purpose = 'evaluation'
        launcher = Launcher(lease)
        released = False
        def active(self): return not self.released
        def release(self): self.released = True; return True
    reservation = Reservation()
    class Broker:
        def reserve_evaluation(self): return reservation
    owner = OwnedEvaluation(tmp_path, Broker(), catalog=catalog,
        provisioner=ModelProvisioner(catalog=catalog), client_factory=lambda *_:client)
    with pytest.raises(RuntimeUnavailable):
        with owner.factory(manifest): pass
    assert not reservation.released
    with pytest.raises(RuntimeUnavailable):
        with owner.factory(manifest): pass
    lease.close_result = {'stopped':True,'remaining':[]}
    assert owner.retry_cleanup() and reservation.released


def test_internal_evaluation_entrypoint_invokes_owned_factory_and_closes(tmp_path, monkeypatch):
    from pathlib import Path
    from threadrelay.evaluation import runner
    from threadrelay.evaluation.owned import OwnedEvaluation, run_owned
    catalog = synthetic_catalog(); path = tmp_path / 'models' / catalog['models'][0]['filename']
    path.parent.mkdir(); path.write_bytes(SYNTHETIC_GGUF)
    lease, client = Lease(path, catalog), Client(path, catalog)
    client.result = lambda: None  # supply decision responses from the requests below
    original = client.request
    def request(method, endpoint, payload=None):
        if endpoint == '/v1/systemone' and '__canary' not in payload['questions']:
            from test_inference import response
            return response({key: .5 for key in payload['questions']})
        return original(method, endpoint, payload)
    client.request = request
    class Reservation:
        purpose = 'evaluation'
        launcher = Launcher(lease)
        released = False
        def active(self): return not self.released
        def release(self): self.released = True; return True
    reservation = Reservation()
    class Broker:
        def reserve_evaluation(self): return reservation
    owner = OwnedEvaluation(tmp_path, Broker(), catalog=catalog,
        provisioner=ModelProvisioner(catalog=catalog), client_factory=lambda *_:client)
    monkeypatch.setattr(runner, 'load_catalog', lambda:catalog)
    dataset = Path(__file__).parents[2] / 'fixtures/evaluation/synthetic.jsonl'
    output = tmp_path / 'synthetic-report.json'
    code = run_owned(['--dataset', str(dataset), '--output', str(output)], owner)
    assert code == 0 and output.exists()
    assert lease.closed == 1 and reservation.released
    assert '"synthetic_dataset": "true"' in output.read_text()


def test_packaged_evaluation_bootstrap_is_closed_and_does_not_construct_application(tmp_path, monkeypatch):
    from io import BytesIO
    from threadrelay.bootstrap import read_startup
    from threadrelay.main import encode_frame, read_frame
    from threadrelay import packaged_worker
    dataset=tmp_path/'dataset.jsonl';output=tmp_path/'report.json'
    message={'version':1,'type':'LocalEvaluationBootstrap','data_root':str(tmp_path),'generation':9,
             'dataset':str(dataset),'output':str(output),'quantization':'Q4_K_M'}
    startup=read_startup(BytesIO(encode_frame(message)))
    assert startup['type']=='LocalEvaluationBootstrap'
    class Owner:
        report_digest=None
        def __init__(self,root,broker): assert root==tmp_path and broker is sentinel
        def retry_cleanup(self): return True
    sentinel=object(); calls=[]
    monkeypatch.setattr(packaged_worker,'Application',lambda *_a,**_kw:pytest.fail('evaluation must not construct app'))
    monkeypatch.setattr('threadrelay.evaluation.owned.OwnedEvaluation',Owner)
    monkeypatch.setattr('threadrelay.evaluation.owned.run_owned',lambda argv, owner:(calls.append(argv),0)[1])
    destination=BytesIO()
    packaged_worker.serve_evaluation(startup,destination,sentinel)
    destination.seek(0)
    assert read_frame(destination)=={'type':'EvaluationFinished','generation':9,'exit_code':0,'report_digest':None}
    assert calls==[['--dataset',str(dataset),'--output',str(output),'--quantization','Q4_K_M']]
    with pytest.raises(ValueError): read_startup(BytesIO(encode_frame({**message,'launcher':'/tmp/evil'})))


def test_packaged_evaluation_path_runs_real_owned_factory_and_confirms_cleanup(tmp_path, monkeypatch):
    from io import BytesIO
    from pathlib import Path
    from threadrelay.evaluation import owned, runner
    from threadrelay.main import read_frame
    from threadrelay.packaged_worker import serve_evaluation
    catalog=synthetic_catalog();path=tmp_path/'models'/catalog['models'][0]['filename'];path.parent.mkdir();path.write_bytes(SYNTHETIC_GGUF)
    lease,client=Lease(path,catalog),Client(path,catalog)
    original=client.request
    def request(method, endpoint, payload=None):
        if endpoint=='/v1/systemone' and '__canary' not in payload['questions']:
            from test_inference import response
            return response({key:.5 for key in payload['questions']})
        return original(method,endpoint,payload)
    client.request=request
    class Reservation:
        purpose='evaluation';launcher=Launcher(lease);released=False
        def active(self):return not self.released
        def release(self):self.released=True;return True
    reservation=Reservation()
    class Broker:
        def reserve_evaluation(self):return reservation
    real=owned.OwnedEvaluation(tmp_path,Broker(),catalog=catalog,
        provisioner=ModelProvisioner(catalog=catalog),client_factory=lambda *_:client)
    monkeypatch.setattr(owned,'OwnedEvaluation',lambda _root,_broker:real)
    monkeypatch.setattr(runner,'load_catalog',lambda:catalog)
    dataset=Path(__file__).parents[2]/'fixtures/evaluation/synthetic.jsonl';output=tmp_path/'report.json'
    startup={'data_root':str(tmp_path),'generation':9,'dataset':str(dataset),'output':str(output),'quantization':'Q4_K_M'}
    framed=BytesIO();serve_evaluation(startup,framed,Broker());framed.seek(0)
    assert read_frame(framed)=={'type':'EvaluationFinished','generation':9,'exit_code':0,'report_digest':None}
    assert lease.closed==1 and reservation.released and output.exists()


def test_real_entrypoint_reserves_before_sampling_and_does_not_retry_failed_reservation(tmp_path, monkeypatch):
    from threadrelay.evaluation import owned
    events=[]
    class Reservation:
        purpose='evaluation'
        def active(self):return True
        def release(self):events.append('release');return True
    class Broker:
        def reserve_evaluation(self):events.append('reserve');return Reservation()
        def evaluation_memory_factory(self,reservation):events.append(('memory',reservation.purpose));return None
    owner=owned.OwnedEvaluation(tmp_path,Broker())
    monkeypatch.setattr(owned,'runner_main',lambda argv,**kwargs:(events.append(('runner',kwargs['memory_factory'])),2)[1])
    assert owned.run_owned([],owner)==2
    assert events==['reserve',('memory','evaluation'),('runner',None),'release']


def test_invalid_pre_reservation_is_released_or_remains_pending(tmp_path):
    from threadrelay.evaluation.owned import OwnedEvaluation
    class Reservation:
        purpose='wrong'
        def active(self):return False
        def release(self):return False
    class Broker:
        def reserve_evaluation(self):return Reservation()
    owner=OwnedEvaluation(tmp_path,Broker())
    assert owner.begin_run() is None
    assert not owner.end_run()
    assert owner._pending is not None
