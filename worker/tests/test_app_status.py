"""App readiness and destination reads do not activate model or account services."""
from threadrelay.application import Application
from threadrelay.main import dispatch
from threadrelay.inference.runtime import DecisionRuntime


def test_app_status_is_read_only_and_distinguishes_unloaded_runtime(tmp_path):
    with Application(tmp_path) as app:
        result=dispatch({'version':1,'id':'status','type':'QueryAppStatus','payload':{}},features=app.features)
        assert result['type']=='AppStatus' and result['payload']['service']=='ready'
        assert result['payload']['model']['state']=='unavailable'
        assert result['payload']['session']['state']=='stopped'
        assert app.telegram._client is None and app.runtime._lease is None
        assert not (tmp_path/'telegram').exists()


def test_destination_query_without_login_returns_actionable_blocker_without_network(tmp_path):
    with Application(tmp_path) as app:
        result=dispatch({'version':1,'id':'chats','type':'QueryTelegramDestinations','payload':{}},features=app.features)
        assert result['type']=='TelegramDestinations'
        assert result['payload']['items']==[] and result['payload']['blocked_reason']=='login_required'
        assert app.telegram._client is None


def test_app_status_reports_retained_sample_cleanup_until_confirmed_closed(tmp_path):
    class Lease:
        report = {'stopped':False,'remaining':['inference']}

        def close(self):
            return self.report

    with Application(tmp_path) as app:
        sample = DecisionRuntime(model_file=tmp_path/'missing')
        lease = Lease()
        sample._lease = lease
        assert sample.stop() == {'stopped':False,'remaining':['inference']}
        app._sample_runtime = sample
        app._sample_active = False
        app._sample_cleanup_pending = True
        try:
            result = dispatch({'version':1,'id':'pending','type':'QueryAppStatus','payload':{}},features=app.features)
            assert result['payload']['model']['state'] == 'stopping'
            assert result['payload']['activity'] == 'sample'
            assert app.runtime._lease is None and app.telegram._client is None
        finally:
            lease.report = {'stopped':True,'remaining':[]}
            assert sample.stop() == lease.report
            app._sample_runtime = None
            app._sample_cleanup_pending = False
        assert app.app_status()['model']['state'] == 'unavailable'
        assert app.app_status()['activity'] is None
