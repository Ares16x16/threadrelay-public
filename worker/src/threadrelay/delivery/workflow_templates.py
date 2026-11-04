"""Plain observed-value substitution. No evaluation, formatting or source fetch."""
import re


PLACEHOLDERS = frozenset(('url', 'author', 'text', 'observed_at', 'published_at',
    'display_name', 'likes', 'replies', 'reposts', 'quotes', 'views', 'shares', 'topic'))
_TOKEN = re.compile(r'\{\{([a-z_]+)\}\}')


def validate_template(body):
    if not isinstance(body, str) or not body or len(body) > 4096 or '\0' in body:
        raise ValueError('invalid_template')
    try:
        if len(body.encode('utf-16-le')) // 2 > 4096:
            raise ValueError('invalid_template')
    except UnicodeError:
        raise ValueError('invalid_template') from None
    names = _TOKEN.findall(body)
    if set(names) - PLACEHOLDERS:
        raise ValueError('unknown_placeholder')
    remainder = _TOKEN.sub('', body)
    if '{{' in remainder or '}}' in remainder:
        raise ValueError('invalid_template_syntax')


def render_template(body, post, *, topic=None):
    validate_template(body)
    metadata = post.get('observed_metadata') or {}
    values = {'url': post['permalink'], 'author': post['author'], 'text': post['text'],
        'observed_at': post['observed_at'], 'published_at': metadata.get('published_at'),
        'display_name': metadata.get('display_name'), 'topic': topic}
    for kind in ('likes', 'replies', 'reposts', 'quotes', 'views', 'shares'):
        labels = {item['label'] for item in metadata.get('engagement', []) if item['kind'] == kind}
        # Preserve the displayed label; a rounded label is not an exact count.
        values[kind] = next(iter(labels)) if len(labels) == 1 else None
    missing = set()
    def substitute(match):
        name = match.group(1)
        value = values[name]
        if value is None:
            missing.add(name)
            return '[missing: ' + name + ']'
        return value
    text = _TOKEN.sub(substitute, body)
    error = None
    try:
        if len(text.encode('utf-16-le')) // 2 > 4096:
            error = 'message_too_long'
        elif not text.strip() or '\0' in text:
            error = 'empty_message'
    except UnicodeError:
        error = 'invalid_message_text'
    return {'text': text, 'missing_fields': sorted(missing), 'error': error}
