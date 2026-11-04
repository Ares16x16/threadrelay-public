"""Side-effect-free combination of independently established capabilities.

The production composition supplies unavailable until real host/native proof
exists; callers cannot obtain that capability through protocol data or a flag.
"""
from .collector.browser import _feed_url
from .contracts import validate_contract


def assess(config, capabilities):
    validate_contract('SessionConfig', config)
    _feed_url(config['feed_url'])
    names = ('browser', 'inference', 'telegram', 'automatic')
    if not isinstance(capabilities, dict) or any(type(capabilities.get(name, False)) is not bool for name in names):
        raise ValueError('Invalid internal readiness capability')
    mode = config.get('mode', 'review')
    required = ['browser', 'inference']
    if mode == 'auto': required.append('telegram')
    if mode == 'auto': required.append('automatic')
    missing = next((name for name in required if not capabilities.get(name, False)), None)
    reasons = {'browser':'browser_unavailable', 'inference':'inference_unavailable',
               'telegram':'telegram_unavailable', 'automatic':'automatic_approval_unavailable'}
    result = {'mode': mode, **{name:'ready' if capabilities.get(name, False) else 'unavailable' for name in names},
              'can_start':missing is None, 'reason':reasons[missing] if missing else None}
    validate_contract('Readiness', result)
    return result
