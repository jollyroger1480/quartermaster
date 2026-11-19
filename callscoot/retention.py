"""Retention: delete call recordings and transcripts after logs.retention_days
(default 7), and part-request leads once done or after leads.retention_days
(default 30).

Scope is deliberately narrow. Only these are ever touched:
- in logs.dir: call_* folders (caller audio, bot TTS audio, transcript, full
  recording), loose call_*.md / *.ogg / *.wav files, and rotated errors.log.N
- in leads.dir: <id>.json lead files
knowledge/, config, .env, watch.log, rings and anything else are left alone.
"""
import datetime
import json
import os
import re
import shutil
import time

from .config import app_home, dig

_STAMP_RE = re.compile(r"call_(\d{8})_(\d{6})")
_AUDIO_EXT = (".ogg", ".wav", ".md")


def _logs_dir(cfg):
    return os.path.expanduser(dig(cfg, "logs.dir", os.path.join(app_home(), "logs")))


def _age_stamp(path, name):
    """When this item was made: the call stamp in its name, else its mtime."""
    m = _STAMP_RE.match(name)
    if m:
        try:
            return datetime.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").timestamp()
        except ValueError:
            pass
    return os.path.getmtime(path)


def _size(path):
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def candidates(cfg, now=None):
    """[(path, when, size, kind)] that retention would delete right now."""
    now = now or time.time()
    out = []
    days = float(dig(cfg, "logs.retention_days", 7))
    logs = _logs_dir(cfg)
    if days > 0 and os.path.isdir(logs):
        cutoff = now - days * 86400
        for name in os.listdir(logs):
            path = os.path.join(logs, name)
            if os.path.islink(path):
                continue
            is_call = name.startswith("call_") and (
                os.path.isdir(path) or name.endswith(_AUDIO_EXT))
            is_loose_audio = os.path.isfile(path) and name.endswith((".ogg", ".wav"))
            is_old_errlog = re.fullmatch(r"errors\.log\.\d+", name) is not None
            if not (is_call or is_loose_audio or is_old_errlog):
                continue
            when = os.path.getmtime(path) if is_old_errlog else _age_stamp(path, name)
            if when < cutoff:
                out.append((path, when, _size(path), "call" if is_call else
                            "audio" if is_loose_audio else "errors.log"))
    lead_days = float(dig(cfg, "leads.retention_days", 30))
    ldir = os.path.expanduser(dig(cfg, "leads.dir", os.path.join(app_home(), "leads")))
    if os.path.isdir(ldir):
        cutoff = now - lead_days * 86400 if lead_days > 0 else None
        for name in os.listdir(ldir):
            if not name.endswith(".json"):
                continue
            path = os.path.join(ldir, name)
            try:
                rec = json.load(open(path, encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            when = _age_stamp(path, "call_" + name)  # lead ids start with the call stamp
            if (isinstance(rec, dict) and rec.get("status") == "done") or (
                    cutoff is not None and when < cutoff):
                out.append((path, when, _size(path), "lead"))
    return sorted(out, key=lambda c: c[1])


def summary(items):
    if not items:
        return "nothing to remove"
    total = sum(c[2] for c in items)
    fmt = lambda t: datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d")
    kinds = {}
    for c in items:
        kinds[c[3]] = kinds.get(c[3], 0) + 1
    by = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items()))
    return (f"{len(items)} items ({by}), {total / 1024 / 1024:.1f} MB, "
            f"oldest {fmt(items[0][1])}, newest {fmt(items[-1][1])}")


def prune(cfg, dry_run=False, log=print):
    """Delete what candidates() lists. Returns the list (deleted or would-be)."""
    items = candidates(cfg)
    log(f"[retention] {'would remove' if dry_run else 'removing'}: {summary(items)}")
    if dry_run:
        return items
    removed_lead = False
    for path, _when, _size_b, kind in items:
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.unlink(path)
            removed_lead |= kind == "lead"
        except OSError as e:
            log(f"[retention] could not remove {os.path.basename(path)}: {e}")
    if removed_lead:
        from . import leads
        leads.write_digest(cfg, log)
    return items
