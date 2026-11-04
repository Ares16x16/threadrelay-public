"""Canonicalize only an observed, absolute, allowlisted Threads post URL.

The deliberately narrow /@handle/post/shortcode grammar is a supported input
contract, not a claim that all Threads link variants/layouts were live verified.
No redirect probing, ID decoding, shortcode synthesis, or host migration.
"""
import re
from typing import TypedDict
from urllib.parse import urlsplit, urlunsplit

from threadrelay.contracts import validate_contract


class PostIdentity(TypedDict):
    post_id: str
    permalink: str


HOSTS = frozenset({"threads.com", "www.threads.com", "threads.net", "www.threads.net"})
_POST_PATH = re.compile(r"/@([A-Za-z0-9_.]{1,30})/post/([A-Za-z0-9_-]{1,100})/?", re.ASCII)


def canonicalize_observed_link(url: str) -> PostIdentity | None:
    if not isinstance(url, str) or len(url) > 4096 or re.search(r"[\s\\\x00-\x1f\x7f]", url):
        return None
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in HOSTS
                or parsed.username is not None or parsed.password is not None
                or parsed.port is not None or "%" in url):
            return None
    except ValueError:
        return None
    match = _POST_PATH.fullmatch(parsed.path)
    if not match:
        return None
    username, shortcode = match.groups()
    identity: PostIdentity = {
        "post_id": f"threads:{shortcode}",
        "permalink": urlunsplit(("https", parsed.hostname, f"/@{username.lower()}/post/{shortcode}", "", "")),
    }
    validate_contract("PostIdentity", identity)
    return identity
