"""Shop-secretary log.

Debug logging is on unless ``CALLSCOOT_LOG`` is set to INFO, WARNING, or
ERROR. Lines go to ``<logs.dir>/errors.log``. The journal still gets the
one-line status prints. A repeated error keeps one traceback, then stays
quiet for 10 minutes.
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
    level_name = os.environ.get("CALLSCOOT_LOG", "DEBUG").upper()
    level = getattr(logging, level_name, logging.DEBUG)
    log = logging.getLogger("callscoot.errors")
    log.setLevel(level)
    log.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    file_handler = RotatingFileHandler(path, maxBytes=500_000, backupCount=3, encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(fmt)
    err_handler = logging.StreamHandler(sys.stderr)
    err_handler.setLevel(logging.WARNING)
    err_handler.setFormatter(fmt)
    log.addHandler(file_handler)
    log.addHandler(err_handler)
    log.warning("debug logging on: %s level=%s", path, logging.getLevelName(level))
    _log = log
    return log


def debug(msg, cfg=None):
    setup(cfg).debug(msg)


def info(msg, cfg=None):
    setup(cfg).info(msg)


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
