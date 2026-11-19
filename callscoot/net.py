"""Keep-alive HTTPS: one requests.Session per thread, so the call loop reuses
its TLS connection to api.x.ai for STT, LLM and TTS instead of a fresh
handshake on every turn (~0.1-0.3 s each). Threads (leads, SMS) get their own."""
import threading

import requests

_local = threading.local()


class NetError(OSError):
    """Any transport or HTTP-status failure. Subclasses OSError so callers'
    existing `except OSError` fallbacks keep working."""


def session():
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        _local.s = s
    return s


def post(url, key="", timeout=20, **kw):
    """POST and return the Response; raises NetError on transport or HTTP error."""
    headers = dict(kw.pop("headers", None) or {})
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        r = session().post(url, headers=headers, timeout=timeout, **kw)
    except requests.RequestException as e:
        _local.s = None  # a broken pooled connection must not poison the next turn
        raise NetError(str(e)) from e
    if r.status_code >= 400:
        raise NetError(f"HTTP {r.status_code}: {r.text[:200]}")
    return r
