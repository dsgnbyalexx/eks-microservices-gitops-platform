"""Service-to-service HTTP calls with a timeout on every request.

Without a timeout, one slow service makes its callers hang too, and threads
pile up until the caller also stops responding (a cascading failure).
The request id and the caller's token are forwarded, so one request can be
traced across services in the logs.
"""

import requests
from flask import g, has_request_context

DEFAULT_TIMEOUT = (1.0, 3.0)  # (connect, read) seconds


class DownstreamError(Exception):
    """The other service could not be reached or answered with a 5xx error."""


def call(method: str, url: str, *, json=None, headers=None, timeout=DEFAULT_TIMEOUT):
    outgoing = dict(headers or {})
    if has_request_context():
        outgoing.setdefault("X-Request-ID", g.get("request_id", ""))
        if g.get("token"):
            outgoing.setdefault("Authorization", f"Bearer {g.token}")
    try:
        response = requests.request(method, url, json=json, headers=outgoing, timeout=timeout)
    except requests.RequestException as err:
        raise DownstreamError(f"{method} {url} failed: {type(err).__name__}") from err
    if response.status_code >= 500:
        raise DownstreamError(f"{method} {url} returned {response.status_code}")
    return response
