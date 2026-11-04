"""Single source of the exact outbound message and review preview."""
from threadrelay.collector.canonicalize import canonicalize_observed_link
from threadrelay.contracts import validate_contract


def format_outgoing(post):
    validate_contract('ObservedPost', post)
    if canonicalize_observed_link(post['permalink']) != {key: post[key] for key in ('post_id', 'permalink')}:
        raise ValueError('Canonical post identity unavailable')
    # No configured label exists in the current route schema. Never copy prose.
    return post['permalink']
