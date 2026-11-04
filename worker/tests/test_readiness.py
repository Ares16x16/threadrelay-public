"""Readiness combines existing capabilities; no native proof is created here."""
from threadrelay.contracts import validate_contract


def test_dry_run_and_review_classify_guest_feed_without_telegram():
    from threadrelay.readiness import assess
    caps = {'browser':True, 'inference':True, 'telegram':False}
    dry = assess({'feed_url':'https://www.threads.com/', 'mode':'dry_run'}, caps)
    review = assess({'feed_url':'https://www.threads.com/', 'mode':'review'}, caps)
    assert dry['can_start'] is True and dry['reason'] is None
    assert review['can_start'] is True and review['reason'] is None
    auto=assess({'feed_url':'https://www.threads.com/','mode':'auto'},caps)
    assert auto['can_start'] is False and auto['reason']=='telegram_unavailable'
    assert dry['telegram'] == 'unavailable'
    validate_contract('Readiness', dry)


def test_missing_browser_and_inference_are_reported_separately():
    from threadrelay.readiness import assess
    result = assess({'feed_url':'https://www.threads.com/', 'mode':'dry_run'},
                    {'browser':False, 'inference':False, 'telegram':False})
    assert result['browser'] == result['inference'] == 'unavailable'
    assert result['reason'] == 'browser_unavailable'
