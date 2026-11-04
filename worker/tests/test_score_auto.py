"""Explicit relevance-score policy is independent of measured evaluation accuracy."""
import pytest
from test_routing import env as routing_env, route, topic, destination, plan

from threadrelay.routing.automatic import AutomaticPolicy
from threadrelay.features import LocalFeatures
from threadrelay.contracts import validate_contract

env=routing_env


@pytest.fixture(autouse=True)
def saved_group(env):
    from threadrelay.storage.saved_destinations import SavedDestinations
    SavedDestinations(env.db).save_verified(destination(),1,'group')


def test_score_auto_requires_new_policy_and_exact_renewed_route_consent(env):
    automatic=AutomaticPolicy(env.db,env.classifier)
    features=LocalFeatures(env.db,router=env.router,automatic=automatic)
    selected=route(mode='auto',version=2)
    assert not automatic.authorized()
    automatic.save({'confirmed':True,'method':'topic_score'})
    assert automatic.authorized()
    assert automatic.status()['evidence_available'] is False
    assert automatic.status()['reason']=='topic_score_requires_per_route_opt_in'
    assert not automatic.route_authorized(selected)
    preview=features.preview_auto_route(selected)
    validate_contract('AutoRoutePreview',preview)
    assert preview['evidence_sha256'] is None
    assert preview['versions']['decision_method']=='topic_score'
    features.save_route(selected,expected_auto_receipt=preview['receipt'])
    assert automatic.route_authorized(selected)
    automatic.save({'confirmed':True,'method':'topic_score'})
    assert not automatic.route_authorized(selected)
    with pytest.raises(ValueError):features.save_route(selected,expected_auto_receipt=preview['receipt'])


def test_legacy_policy_never_silently_selects_score_auto(env):
    automatic=AutomaticPolicy(env.db,env.classifier)
    automatic.save({'confirmed':True,'min_precision':.95,'min_recall':.8,'min_positive':20,'min_predicted':30,'languages':['en','zh','mixed','other']})
    assert not automatic.authorized()
    assert automatic.status()['policy'].get('method') is None


@pytest.mark.parametrize('change',['topic','model','audience','sender'])
def test_score_route_consent_binds_topics_model_destination_and_personal_sender(env,change):
    automatic=AutomaticPolicy(env.db,env.classifier)
    features=LocalFeatures(env.db,router=env.router,automatic=automatic)
    automatic.save({'confirmed':True,'method':'topic_score'})
    selected=route(mode='auto',version=2)
    preview=features.preview_auto_route(selected)
    features.save_route(selected,expected_auto_receipt=preview['receipt'])
    if change=='topic':env.config.save_topic(topic(version=2,minimum_score=.75))
    if change=='model':env.classifier._runtime.metadata={**env.classifier.metadata,'model_sha256':'d'*64}
    if change=='audience':env.config.save_destination(destination(audience='public'),account_epoch=1)
    if change=='sender':selected={**selected,'sender':{'kind':'chat','id':'-123'}}
    assert not automatic.route_authorized(selected)


def test_score_auto_uses_actual_topic_threshold_and_public_unflagged_source(env):
    automatic=AutomaticPolicy(env.db,env.classifier)
    automatic.save({'confirmed':True,'method':'topic_score'})
    selected=route(mode='auto',version=2)
    features=LocalFeatures(env.db,router=env.router,automatic=automatic)
    features.save_route(selected,expected_auto_receipt=features.preview_auto_route(selected)['receipt'])
    env.router.auto_send_authorized=automatic.authorized
    env.router.auto_route_authorized=automatic.route_authorized
    assert not plan(env)[0]['approval_required']
    assert plan(env,visibility='restricted')[0]['approval_required']
    assert plan(env,quality_flags=[{'code':'synthetic_warning','detail':'Fixture'}])[0]['approval_required']


@pytest.mark.parametrize('visibility,flags,allow,expected',[
    ('unknown',[],False,False),
    ('unknown',[{'code':'visibility_unverified','detail':'Unverified'}],True,True),
    ('restricted',[],True,False),
    ('unknown',[{'code':'truncated','detail':'Incomplete'}],True,False),
    ('unknown',[{'code':'visibility_unverified','detail':'Unverified'},{'code':'media_only','detail':'No text'}],True,False),
])
def test_unverified_link_policy_never_relabels_or_drops_other_quality_guards(env,visibility,flags,allow,expected):
    automatic=AutomaticPolicy(env.db,env.classifier)
    automatic.save({'confirmed':True,'method':'topic_score'})
    selected=route(mode='auto',allow_unverified_source=allow)
    source={**env.post,'visibility':visibility,'quality_flags':flags}
    assert automatic.source_authorized(source,[selected]) is expected
    assert source['visibility']==visibility and source['quality_flags']==flags
    automatic.save({'confirmed':True,'min_precision':1,'min_recall':1,'min_positive':1,'min_predicted':1,'languages':['en','zh','mixed','other']})
    assert not automatic.source_authorized(source,[selected])
