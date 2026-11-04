"""UTC scheduling chosen once, with an explicit presentation timezone."""
from datetime import datetime, timezone
import math
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from threadrelay.contracts import validate_contract


def timestamp(value):
    if not isinstance(value, str) or not value.endswith(('Z', '+00:00')):
        raise ValueError('utc_schedule_required')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError
        result = parsed.timestamp()
        if not math.isfinite(result):
            raise ValueError
        return result
    except (ValueError, OverflowError, OSError):
        raise ValueError('invalid_schedule_time') from None


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def validate_timing(timing):
    validate_contract('DeliveryTiming', timing)
    try:
        ZoneInfo(timing['timezone'])
    except (ValueError, ZoneInfoNotFoundError):
        raise ValueError('invalid_schedule_timezone') from None
    if timing['mode'] == 'random' and timing['minimum_seconds'] > timing['maximum_seconds']:
        raise ValueError('invalid_random_bounds')
    if timing['mode'] == 'new_matching' and len(set(timing['topic_ids'])) != len(timing['topic_ids']):
        raise ValueError('duplicate_topic_criteria')


def spacing(timing, randint):
    if timing['mode'] == 'random':
        return randint(timing['minimum_seconds'], timing['maximum_seconds'])
    return timing.get('interval_seconds', 300)


def plan_due(timing, count, now, randint):
    validate_timing(timing)
    if type(count) is not int or not 0 <= count <= 50 or not math.isfinite(now):
        raise ValueError('invalid_schedule')
    mode = timing['mode']
    if mode == 'new_matching':
        return []
    start = now
    if mode == 'at_time':
        start = timestamp(timing['at'])
        if start <= now:
            raise ValueError('schedule_must_be_future')
    elif mode in ('fixed', 'random') and timing['start_at'] is not None:
        start = timestamp(timing['start_at'])
        if start < now:
            raise ValueError('schedule_must_be_future')
    due = []
    for index in range(count):
        if index and mode in ('fixed', 'random'):
            start += spacing(timing, randint)
        stamp(start)  # Bound the actual representable persisted instant.
        due.append(start)
    return due
