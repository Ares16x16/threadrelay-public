"""Unknown audience remains unknown; exact score-route consent permits links only."""
import pytest
from test_routing import env as routing_env,route,post,running
from threadrelay.features import LocalFeatures
from threadrelay.routing.automatic import AutomaticPolicy
from threadrelay.storage.posts import PostStore

env=routing_env


@pytest.mark.parametrize('allowed',[False,True])
def test_unknown_source_needs_exact_current_score_route_consent(env,allowed):
    from test_routing import destination
    from threadrelay.storage.saved_destinations import SavedDestinations
    SavedDestinations(env.db).save_verified(destination(),1,'group')
    policy=AutomaticPolicy(env.db,env.classifier)
    policy.save({'confirmed':True,'method':'topic_score'})
    selected=route(mode='auto',version=2,allow_unverified_source=allowed)
    features=LocalFeatures(env.db,router=env.router,automatic=policy)
    features.save_route(selected,expected_auto_receipt=features.preview_auto_route(selected)['receipt'])
    env.router.auto_send_authorized=policy.authorized
    env.router.auto_route_authorized=policy.route_authorized
    env.router.auto_source_authorized=policy.source_authorized
    source=post(visibility='unknown',quality_flags=[{'code':'visibility_unverified','detail':'Observed audience unavailable'}])
    PostStore(env.db).upsert(source)
    proposal=env.router.plan(source,env.classifier.classify_bound(source,env.config.topics()),session_mode='auto')[0]
    assert proposal['post']['visibility']=='unknown'
    assert proposal['approval_required'] is not allowed
    if allowed:
        identifier=env.router.stage(proposal)
        from threadrelay.storage.outbox import Outbox
        delivery=Outbox(env.db).get(identifier)
        assert env.router.authorize(delivery,running('auto'))['allowed'] is True
        env.config.save_route({**selected,'version':3,'allow_unverified_source':False})
        assert env.router.authorize(delivery,running('auto'))['allowed'] is False


@pytest.mark.parametrize('visibility,flags',[
    ('restricted',[]),('unknown',[{'code':'truncated','detail':'Incomplete text'}]),
    ('unknown',[{'code':'visibility_unverified','detail':'Unknown audience'},{'code':'missing_media_context','detail':'Media unavailable'}]),
])
def test_unverified_audience_consent_never_allows_other_source_problems(env,visibility,flags):
    policy=AutomaticPolicy(env.db,env.classifier);policy.save({'confirmed':True,'method':'topic_score'})
    selected=route(mode='auto',version=2,allow_unverified_source=True)
    env.config.save_route(selected)
    env.router.auto_send_authorized=lambda:True;env.router.auto_route_authorized=lambda route:True
    env.router.auto_source_authorized=policy.source_authorized
    source=post(visibility=visibility,quality_flags=flags);PostStore(env.db).upsert(source)
    proposal=env.router.plan(source,env.classifier.classify_bound(source,env.config.topics()),session_mode='auto')[0]
    assert proposal['approval_required'] is True
