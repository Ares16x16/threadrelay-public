"""The already-composed consent must never bind unseen current backend data."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
import test_routing
from test_routing import route, destination
from test_evaluation import native_runtime, record, topic as evaluation_topic
from threadrelay.contracts import validate_contract
from threadrelay.evaluation.runner import evaluate
from threadrelay.features import LocalFeatures
from threadrelay.inference.decision import Classifier
from threadrelay.main import dispatch
from threadrelay.routing.automatic import AutomaticPolicy

env = test_routing.env


@pytest.fixture
def auto(env):
    env.config.save_topic({**evaluation_topic('science'), 'version': 2})
    topics = env.config.topics()
    data = {'records': [record('fixture'+language, expected=('science',), language=language)
                        for language in ('en', 'zh', 'mixed', 'other')],
            'split': 'test', 'synthetic': False, 'topics': topics}
    runtime = native_runtime([])
    runtime.decide = lambda state, questions: {key: 1 for key in questions}
    classifier = Classifier(runtime)
    evidence = [evaluate(data, classifier)]
    automatic = AutomaticPolicy(env.db, classifier, lambda: evidence[0])
    policy = {'confirmed': True, 'min_precision': 1, 'min_recall': 1,
              'min_positive': 1, 'min_predicted': 1, 'languages': ['en', 'zh', 'mixed', 'other']}
    automatic.save(policy)
    return SimpleNamespace(features=LocalFeatures(env.db, router=env.router, automatic=automatic),
                           policy=policy, automatic=automatic, evidence=evidence,
                           runtime=runtime, selected=route(mode='auto', version=2))


def preview(auto):
    request = {'version': 1, 'id': 'preview-auto', 'type': 'PreviewAutoRoute', 'payload': deepcopy(auto.selected)}
    validate_contract('Command', request)
    result = dispatch(request, features=auto.features)
    assert result['type'] == 'AutoRoutePreview'
    validate_contract('Event', result)
    return result['payload']


def command(auto):
    disclosure = preview(auto)
    value = {'version': 1, 'id': 'stale-auto', 'type': 'OptInAutoRoute',
             'payload': {'route': deepcopy(auto.selected), 'confirmed': True, 'expected_receipt': disclosure['receipt']}}
    validate_contract('Command', value)
    return value


@pytest.mark.parametrize('change', ['audience', 'destination_version', 'policy', 'policy_reaffirmed', 'policy_reverted', 'evidence', 'account', 'classifier', 'topics', 'route_base'])
def test_composed_auto_consent_rejects_changed_basis_without_mutation(env, auto, change):
    consent = command(auto)
    if change == 'audience':
        env.config.save_destination(destination(audience='public'), account_epoch=auto.selected['account_epoch'])
    elif change == 'destination_version':
        env.config.save_destination(destination(title='Changed'), account_epoch=auto.selected['account_epoch'])
        env.config.save_destination(destination(), account_epoch=auto.selected['account_epoch'])
    elif change == 'policy_reaffirmed':
        auto.automatic.save(auto.policy)
    elif change == 'policy_reverted':
        auto.automatic.save({**auto.policy, 'min_precision': .9})
        auto.automatic.save(auto.policy)
    elif change == 'account':
        from threadrelay.storage.outbox import AccountStore
        AccountStore(env.db).invalidate_epoch(auto.selected['account_id'])
    elif change == 'classifier':
        auto.runtime._metadata['runtime_commit'] = 'replacement'
        auto.evidence[0] = deepcopy(auto.evidence[0])
        auto.evidence[0]['versions']['runtime_commit'] = 'replacement'
    elif change == 'topics':
        from threadrelay.evaluation.dataset import digest
        env.config.save_topic({**env.config.topics()[0], 'version': 3})
        auto.evidence[0] = deepcopy(auto.evidence[0])
        auto.evidence[0]['versions']['topics_sha256'] = digest(env.config.topics())
    elif change == 'route_base':
        env.config.save_route(route(version=2, mode='auto'))
    elif change == 'policy':
        auto.automatic.save({**auto.policy, 'min_precision': .9})
    else:
        auto.evidence[0] = deepcopy(auto.evidence[0])
        auto.evidence[0]['versions']['fixture_revision'] = 'replacement'
    if change != 'account':
        # Changed values remain eligible: rejection must be the old preview, not a closed evidence gate.
        assert auto.automatic.authorized()
        assert preview(auto)['receipt'] != consent['payload']['expected_receipt']
    before = env.config.routes()
    result = dispatch(consent, features=auto.features)
    assert result['type'] == 'Error'
    assert env.config.routes() == before
    assert not auto.automatic.route_authorized(auto.selected)
    with env.db.connection() as con:
        assert con.execute("SELECT payload FROM local_policy WHERE name LIKE 'auto-route:%'").fetchall() == []


def test_fresh_preview_discloses_exact_basis_and_authorizes_only_on_opt_in(env, auto):
    before = env.config.routes()
    disclosure = preview(auto)
    assert disclosure['route'] == auto.selected
    assert disclosure['destination']['audience'] == 'private'
    assert disclosure['destination_version'] == 1
    assert disclosure['policy'] == auto.policy
    assert disclosure['topics'] == env.config.topics()
    assert disclosure['versions'] == auto.evidence[0]['versions']
    assert env.config.routes() == before
    assert not auto.automatic.route_authorized(auto.selected)
    consent = command(auto)
    result = dispatch(consent, features=auto.features)
    assert result['type'] == 'Acknowledged'
    assert env.config.routes() == [auto.selected]
    assert auto.automatic.route_authorized(auto.selected)


@pytest.mark.parametrize('field,value', [('id', 'other'), ('version', 3), ('account_epoch', 2), ('destination_id', '123')])
def test_preview_cannot_authorize_a_modified_route(env, auto, field, value):
    consent = command(auto)
    consent['payload']['route'][field] = value
    validate_contract('Command', consent)
    before = env.config.routes()
    assert dispatch(consent, features=auto.features)['type'] == 'Error'
    assert env.config.routes() == before


def test_fabricated_receipt_and_boolean_only_do_not_authorize(env, auto):
    consent = command(auto)
    consent['payload']['expected_receipt'] = '0' * 64
    validate_contract('Command', consent)
    before = env.config.routes()
    assert dispatch(consent, features=auto.features)['type'] == 'Error'
    with pytest.raises(ValueError):
        auto.features.save_route(auto.selected)
    assert env.config.routes() == before
    assert not auto.automatic.route_authorized(auto.selected)


def test_stale_replacement_leaves_existing_route_and_receipt_untouched(env, auto):
    assert dispatch(command(auto), features=auto.features)['type'] == 'Acknowledged'
    auto.selected = {**auto.selected, 'version': 3}
    consent = command(auto)
    auto.evidence[0] = deepcopy(auto.evidence[0])
    auto.evidence[0]['versions']['fixture_revision'] = 'replacement'
    with env.db.connection() as con:
        before = [tuple(row) for row in con.execute("SELECT * FROM local_policy WHERE name LIKE 'auto-route:%'")]
    routes = env.config.routes()
    assert dispatch(consent, features=auto.features)['type'] == 'Error'
    with env.db.connection() as con:
        assert [tuple(row) for row in con.execute("SELECT * FROM local_policy WHERE name LIKE 'auto-route:%'")] == before
    assert env.config.routes() == routes
    assert not auto.automatic.route_authorized(routes[0])


def test_preview_requires_trusted_real_evidence(env, auto):
    auto.evidence[0] = None
    with pytest.raises(ValueError):
        auto.features.preview_auto_route(auto.selected)
    assert not auto.automatic.route_authorized(auto.selected)


def test_receipt_comparison_holds_writer_and_route_write_failure_rolls_back_consent(env, auto):
    import sqlite3
    consent = command(auto)
    report = auto.evidence[0]
    checked = []

    def evidence_under_writer():
        with sqlite3.connect(env.db.path, timeout=0) as other:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                other.execute('BEGIN IMMEDIATE')
        checked.append(True)
        return report

    auto.automatic.evidence = evidence_under_writer
    with env.db.transaction() as con:
        con.execute("CREATE TRIGGER reject_route BEFORE UPDATE ON routing_routes BEGIN SELECT RAISE(ABORT, 'fixture'); END")
    before = env.config.routes()
    assert dispatch(consent, features=auto.features)['type'] == 'Error'
    assert checked == [True]  # one trusted evidence read inside the writer
    assert env.config.routes() == before
    with env.db.connection() as con:
        assert con.execute("SELECT payload FROM local_policy WHERE name LIKE 'auto-route:%'").fetchall() == []


def test_actual_application_saves_route_without_policy_or_receipt_but_keeps_native_gates_closed(tmp_path, auto):
    from threadrelay.application import Application
    from threadrelay.storage.outbox import AccountStore
    from test_routing import ACCOUNT
    with Application(tmp_path / 'composition') as app:
        app.classifier._runtime = auto.runtime
        AccountStore(app.db).activate({'account_id': ACCOUNT, 'epoch': 1, 'auth_state': 'ready',
            'display_name': 'Synthetic', 'network_state': 'offline', 'revocation_pending': False})
        app.config.save_topic({**evaluation_topic('science'), 'version': 2})
        app.config.save_destination(destination(), account_epoch=1)
        from threadrelay.storage.saved_destinations import SavedDestinations
        SavedDestinations(app.db).save_verified(destination(),1,'group')
        assert app.automatic.policy() is None
        status = app.automatic.status()
        assert status['eligible'] and status['reason'] == 'route_selection_required'
        selected = {**auto.selected, 'message_template':'{{author}}: {{url}}',
                    'queue':{'mode':'interval','interval_seconds':60}}
        saved = dispatch({'version':1,'id':'save','type':'SaveRoute','payload':selected}, features=app.features)
        assert saved['type'] == 'Acknowledged'
        assert app.config.routes() == [selected]
        assert app.automatic.route_authorized(selected)
        assert app.readiness({'feed_url':'https://www.threads.com/','mode':'auto'})['reason'] == 'select_one_auto_route'
        assert not app.features.account_features.native_gate()
        assert app.telegram._client is None


def test_restart_invalidates_outstanding_preview_not_saved_route_consent(env, auto):
    pending = command(auto)
    auto.features.automatic = AutomaticPolicy(env.db, auto.automatic.classifier, lambda: auto.evidence[0])
    assert dispatch(pending, features=auto.features)['type'] == 'Error'
    assert dispatch(command(auto), features=auto.features)['type'] == 'Acknowledged'
    reopened = AutomaticPolicy(env.db, auto.automatic.classifier, lambda: auto.evidence[0])
    assert reopened.route_authorized(auto.selected)
