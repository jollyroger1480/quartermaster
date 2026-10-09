"""Call control over the Bluetooth headset link.

bluealsad is already the Hands-Free unit. The phone exposes answer, hangup,
and caller ID on that RFCOMM channel, so the secretary does not need wireless
debugging to pick up. Texts still use ADB.
"""
import os
import re
import select
import subprocess
import threading
import time

_lock = threading.Lock()
_fd = None
_path = None
_names = None

CLCC_RE = re.compile(
    r'\+CLCC:\s*(\d+),(\d+),(\d+),(\d+),(\d+),(?:"([^"]*)")?,?(\d+)?'
)


class HfpError(RuntimeError):
    pass


def parse_cind_map(text):
    return re.findall(r'"([A-Za-z0-9_]+)"', text or "")


def parse_cind_values(text):
    for line in _lines(text):
        if line.startswith("+CIND:"):
            raw = line.split(":", 1)[1]
            if "(" in raw:
                continue
            return [int(p) for p in re.findall(r"-?\d+", raw)]
    return []


def parse_clcc(text):
    calls = []
    for line in _lines(text):
        m = CLCC_RE.search(line)
        if not m:
            continue
        number = (m.group(6) or "").strip() or None
        calls.append({
            "idx": int(m.group(1)),
            "direction": int(m.group(2)),
            "stat": int(m.group(3)),
            "number": number,
        })
    return calls


def interpret(names, values, calls):
    """Map HFP indicators + CLCC onto callscoot states: 0 idle, 1 ringing, 2 offhook."""
    incoming = [c for c in calls if c["stat"] in (4, 5)]
    if incoming:
        return {"state": 1, "number": incoming[0]["number"]}
    active = [c for c in calls if c["stat"] in (0, 1, 2, 3)]
    if active:
        return {"state": 2, "number": active[0]["number"]}
    ind = {}
    for i, name in enumerate(names):
        if i < len(values):
            ind[name.lower()] = values[i]
    setup = ind.get("callsetup", 0)
    call = ind.get("call", 0)
    if setup == 1:
        return {"state": 1, "number": None}
    if call == 1 or setup in (2, 3):
        return {"state": 2, "number": None}
    return {"state": 0, "number": None}


def call_state():
    with _lock:
        _ensure()
        if _names is None:
            _refresh_names()
        cind = _exchange("AT+CIND?")
        clcc = _exchange("AT+CLCC")
        if "ERROR" in _lines(clcc) and "+CLCC:" not in clcc:
            calls = []
        else:
            calls = parse_clcc(clcc)
        return interpret(_names or [], parse_cind_values(cind), calls)


def answer():
    """Pick up the ringing call. Sends ATA. Do not call this when idle."""
    with _lock:
        _ensure()
        text = _exchange("ATA")
    if _failed(text):
        raise HfpError(f"ATA refused: {_compact(text)}")


def hangup():
    with _lock:
        _ensure()
        text = _exchange("AT+CHUP")
        if _failed(text):
            # Already idle is not a failure for the watcher.
            follow = _exchange("AT+CIND?")
            if "ERROR" in _lines(follow):
                raise HfpError(f"CHUP refused: {_compact(text)}")
            vals = parse_cind_values(follow)
            names = _names or []
            if interpret(names, vals, [])["state"] == 2:
                raise HfpError(f"still offhook after CHUP: {_compact(text)}")


def ping():
    call_state()


def _norm(line):
    # This AG terminates with ":OK" instead of a bare "OK".
    return line.lstrip(":;> ").strip()


def _has_ok(lines):
    return any(_norm(ln) == "OK" for ln in lines)


def _failed(text):
    lines = _lines(text)
    return "ERROR" in lines and not _has_ok(lines)


def _compact(text):
    return " ".join(_lines(text))[:200]


def _lines(text):
    return [ln.strip() for ln in (text or "").replace("\r", "\n").split("\n") if ln.strip()]


def _refresh_names():
    global _names
    text = _exchange("AT+CIND=?")
    names = parse_cind_map(text)
    if "call" not in [n.lower() for n in names]:
        raise HfpError(f"phone did not list call indicators: {_compact(text)}")
    _names = names


def _ensure():
    global _fd, _path
    if _fd is not None:
        return
    path = _rfcomm_path()
    try:
        import dbus
    except ImportError as e:
        raise HfpError("python3-dbus is required for headset call control") from e
    bus = dbus.SystemBus()
    obj = bus.get_object("org.bluealsa", path)
    unix_fd = obj.Open(dbus_interface="org.bluealsa.RFCOMM1")
    fd = unix_fd.take()
    os.set_blocking(fd, False)
    _fd = fd
    _path = path


def _rfcomm_path():
    try:
        out = subprocess.run(
            ["busctl", "tree", "org.bluealsa"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        raise HfpError(f"bluealsa is not on the bus: {e}") from e
    paths = []
    for line in out.splitlines():
        tok = line.split()[-1] if line.split() else ""
        if tok.startswith("/org/bluealsa/") and tok.endswith("/rfcomm"):
            paths.append(tok)
    if not paths:
        raise HfpError("phone headset link is down — reconnect Bluetooth")
    if len(paths) == 1:
        return paths[0]
    # Prefer the HFP-HF transport when more than one RFCOMM shows up.
    for path in paths:
        try:
            prop = subprocess.run(
                ["busctl", "get-property", "org.bluealsa", path,
                 "org.bluealsa.RFCOMM1", "Transport"],
                capture_output=True, text=True, timeout=3,
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            continue
        if "HFP-HF" in prop:
            return path
    return paths[0]


def _exchange(cmd, timeout=2.5):
    global _fd, _names
    fd = _fd
    try:
        _drain(fd)
        os.write(fd, (cmd + "\r").encode("ascii"))
        return _read_until(fd, timeout)
    except OSError as e:
        _close()
        raise HfpError(f"headset link dropped during {cmd}: {e}") from e


def _drain(fd):
    while True:
        ready, _, _ = select.select([fd], [], [], 0)
        if not ready:
            return
        chunk = os.read(fd, 4096)
        if not chunk:
            raise OSError("rfcomm closed")


def _read_until(fd, timeout):
    end = time.monotonic() + timeout
    buf = b""
    while time.monotonic() < end:
        wait = max(0.0, end - time.monotonic())
        ready, _, _ = select.select([fd], [], [], wait)
        if not ready:
            break
        chunk = os.read(fd, 4096)
        if not chunk:
            raise OSError("rfcomm closed")
        buf += chunk
        lines = _lines(buf.decode("utf-8", "replace"))
        if _has_ok(lines) or "ERROR" in lines:
            return buf.decode("utf-8", "replace")
    raise HfpError("headset did not answer " + (buf.decode("utf-8", "replace")[:80] or "(silence)"))


def _close():
    global _fd, _path, _names
    if _fd is not None:
        try:
            os.close(_fd)
        except OSError:
            pass
    _fd = None
    _path = None
    _names = None
