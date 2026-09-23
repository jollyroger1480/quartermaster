"""Error log for the shop secretary.

The watch loop still prints its one-line status to the journal. This records
the traceback the first time an error shows up, and writes
``<logs.dir>/errors.log``. The same error is not stacked again for 10 minutes.
``CALLSCOOT_LOG=DEBUG`` is not a firehose: this file is errors only.
"""
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler

from .config import app_home, dig

_log = None
_repeat = {}
_QUIET_S = 600


def setup(cfg=None):
    """Attach the error file once. Safe to call again."""
    global _log
    if _log is not None:
        return _log
    log_dir = os.path.expanduser(dig(cfg or {}, "logs.dir", os.path.join(app_home(), "logs")))
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, "errors.log")
    log = logging.getLogger("callscoot.errors")
    log.setLevel(logging.INFO)
    log.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    file_handler = RotatingFileHandler(path, maxBytes=200_000, backupCount=3, encoding="utf-8")
    file_handler.setLevel(logging.WARNING)
    file_handler.setFormatter(fmt)
    err_handler = logging.StreamHandler(sys.stderr)
    err_handler.setLevel(logging.WARNING)
    err_handler.setFormatter(fmt)
    log.addHandler(file_handler)
    log.addHandler(err_handler)
    log.warning("error log on: %s", path)
    _log = log
    return log


def record(where, err, cfg=None):
    """Write one traceback for this error, then stay quiet while it repeats."""
    log = setup(cfg)
    key = (where, type(err).__name__, str(err))
    now = time.monotonic()
    state = _repeat.get(key)
    if state is not None:
        state["n"] += 1
        if now - state["noted"] < _QUIET_S:
            return
        log.warning(
            "%s: same error %d more times (%s: %s)",
            where, state["n"], type(err).__name__, err,
        )
        state["n"] = 0
        state["noted"] = now
        return
    _repeat[key] = {"n": 0, "noted": now}
    log.error("%s: %s: %s", where, type(err).__name__, err, exc_info=(type(err), err, err.__traceback__))
