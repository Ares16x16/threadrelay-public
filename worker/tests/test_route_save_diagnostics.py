import pytest

from threadrelay.features import LocalFeatures
from threadrelay.main import dispatch
from threadrelay.storage.database import Database


@pytest.mark.parametrize('failure,reason',[
    ('Inactive or stale account binding','account_changed'),
    ('Unavailable account binding','account_unavailable'),
    ('destination_unavailable','destination_unavailable'),
    ('destination_not_saved','destination_not_saved'),
    ('destination_not_zero_paid_sendable','destination_not_sendable'),
    ('Sender mismatch','sender_changed'),
    ('Topic unavailable','topic_unavailable'),
    ('Configuration version must increase','route_changed'),
])
def test_route_save_reports_only_fixed_actionable_validation_reasons(tmp_path,monkeypatch,failure,reason):
    features=LocalFeatures(Database(tmp_path/'app.db'))
    def reject(*args,**kwargs):raise ValueError(failure)
    monkeypatch.setattr(features,'save_route',reject)
    route={'id':'review','version':1,'topic_ids':['t'],'destination_id':'-20','mode':'review',
           'enabled':True,'account_id':'7','account_epoch':2,'sender':{'kind':'user','id':'7'}}
    result=dispatch({'version':1,'id':'save','type':'SaveRoute','payload':route},features=features)
    assert result['type']=='Error'
    assert result['payload']=={'code':'invalid_request','message':'route_save:'+reason}


def test_unknown_route_save_error_never_exposes_storage_or_account_details(tmp_path,monkeypatch):
    features=LocalFeatures(Database(tmp_path/'app.db'))
    def reject(*args,**kwargs):raise ValueError('private fixture account detail')
    monkeypatch.setattr(features,'save_route',reject)
    route={'id':'review','version':1,'topic_ids':['t'],'destination_id':'-20','mode':'review',
           'enabled':True,'account_id':'7','account_epoch':2,'sender':{'kind':'user','id':'7'}}
    result=dispatch({'version':1,'id':'save','type':'SaveRoute','payload':route},features=features)
    assert result['payload']['message']=='The operation could not be completed. Review current details and try again.'
