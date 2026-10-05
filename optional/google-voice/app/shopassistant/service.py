"""Always-on supervisor.

It starts the call watcher as a child process, restarts it whenever it exits,
hangs or stops sending heartbeats, backs off if it keeps crashing, and writes
everything to data\\logs\\service\\<date>.log. Only one supervisor can run at
a time, so the scheduled task's 5-minute repeat is a watchdog: if the
supervisor was killed it comes back; if it's running nothing happens.
"""
import datetime
import glob
import os
import subprocess
import sys
import tempfile
import time

from . import brand
from .config import app_dir, dig, load, logs_dir, root_dir

TASK_NAME = brand.APP_ID
MUTEX = "Local\\" + brand.APP_ID + "Supervisor"

HEARTBEAT_STALE_S = 900        # longer than the longest call (max_duration_s = 600)


def _lock_path():
    """Per-install lock (not /tmp: another user's lock file there would block this one)."""
    return os.path.join(logs_dir(), ".supervisor.lock")


def _logs(cfg):
    return logs_dir(cfg)


def heartbeat_path(cfg):
    return os.path.join(_logs(cfg), ".heartbeat")


def stop_path(cfg):
    return os.path.join(_logs(cfg), ".service.stop")


def beat(cfg, _last=[0.0]):
    """Called from the watch loop; cheap, at most every 30 s."""
    now = time.time()
    if now - _last[0] < 30:
        return
    _last[0] = now
    try:
        os.makedirs(_logs(cfg), exist_ok=True)
        with open(heartbeat_path(cfg), "w") as f:
            f.write(str(int(now)))
    except OSError:
        pass


# ------------------------------------------------------------ single instance
class _SingleInstance:
    def __init__(self, cfg):
        self.handle = None
        self.fh = None
        if sys.platform == "win32":
            import ctypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            ctypes.set_last_error(0)
            self.handle = k32.CreateMutexW(None, False, MUTEX)
            self.ok = bool(self.handle) and ctypes.get_last_error() != 183   # ERROR_ALREADY_EXISTS
        else:
            import fcntl
            self.fh = open(_lock_path(), "w")
            try:
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.ok = True
            except OSError:
                self.ok = False


# --------------------------------------------------------- windows job object
def _kill_with_parent_job():
    """Children die if the supervisor dies (no orphaned watchers fighting over
    the Voice window). Edge is allowed to break away so calls survive restarts."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.windll.kernel32
    job = k32.CreateJobObjectW(None, None)
    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = 0x2000 | 0x0800   # KILL_ON_JOB_CLOSE | BREAKAWAY_OK
    k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
    return job


def _die_with_parent():
    """Linux: the watcher gets SIGTERM if the supervisor dies, so two watchers never fight over the window."""
    try:
        import ctypes
        import signal
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)    # PR_SET_PDEATHSIG
    except Exception:
        pass


def _assign(job, proc):
    if job is None:
        return
    import ctypes
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1F0FFF, False, proc.pid)
    k32.AssignProcessToJobObject(job, h)
    k32.CloseHandle(h)


# ------------------------------------------------------------------ logging
class _Log:
    def __init__(self, cfg):
        self.dir = os.path.join(_logs(cfg), "service")
        os.makedirs(self.dir, exist_ok=True)
        self.keep_days = int(dig(cfg, "service.keep_log_days", 14))
        self.day = None
        self.fh = None

    def file(self):
        today = datetime.date.today().isoformat()
        if today != self.day:
            if self.fh:
                self.fh.close()
            self.day = today
            self.fh = open(os.path.join(self.dir, f"{today}.log"), "a", encoding="utf-8", buffering=1)
            for old in sorted(glob.glob(os.path.join(self.dir, "*.log")))[:-self.keep_days]:
                try:
                    os.unlink(old)
                except OSError:
                    pass
        return self.fh

    def __call__(self, line):
        line = line.rstrip("\n")
        if not line.startswith("["):
            line = f"[{time.strftime('%H:%M:%S')}] {line}"
        self.file().write(line + "\n")
        if sys.stdout and not sys.executable.lower().endswith("pythonw.exe"):
            try:
                print(line, flush=True)
            except Exception:
                pass


# --------------------------------------------------------------- supervisor
def _child_cmd(cfg):
    override = dig(cfg, "service.command", None)          # tests / custom setups
    if override:
        return list(override)
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe"):
        exe = exe[:-5] + ".exe"                           # child needs a real stdout pipe
    return [exe, "-u", "-m", "shopassistant", "watch"]


def _child_env():
    env = dict(os.environ)
    env["SHOPASSISTANT_HOME"] = root_dir()
    env["PYTHONPATH"] = app_dir()
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def run(cfg):
    lock = _SingleInstance(cfg)
    if not lock.ok:
        print("supervisor already running — nothing to do")
        return 0
    log = _Log(cfg)
    try:
        os.unlink(stop_path(cfg))
    except OSError:
        pass
    job = _kill_with_parent_job()
    stale = float(dig(cfg, "service.heartbeat_stale_s", HEARTBEAT_STALE_S))
    daily = (dig(cfg, "service.restart_daily_at", "") or "").strip()     # e.g. "03:30"
    backoff = 5.0
    log(f"supervisor up (pid {os.getpid()})")
    while True:
        if os.path.exists(stop_path(cfg)):
            log("stop requested — supervisor exiting")
            return 0
        started = time.time()
        try:
            os.unlink(heartbeat_path(cfg))
        except OSError:
            pass
        extra = {"creationflags": 0x08000000} if sys.platform == "win32" else {"preexec_fn": _die_with_parent}
        proc = subprocess.Popen(_child_cmd(cfg), cwd=app_dir(), env=_child_env(),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                text=True, encoding="utf-8", errors="replace", bufsize=1, **extra)
        _assign(job, proc)
        log(f"started the call watcher (pid {proc.pid})")
        reason = _pump(cfg, proc, log, started, stale, daily)
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(15)
            except subprocess.TimeoutExpired:
                proc.kill()
        ran = time.time() - started
        log(f"watch ended ({reason}, exit {proc.returncode}, ran {ran/60:.1f} min)")
        if reason == "stop":
            return 0
        backoff = 10.0 if ran > 300 else min(backoff * 2, 300.0)
        log(f"restarting in {backoff:.0f} s")
        t_end = time.time() + backoff
        while time.time() < t_end:
            if os.path.exists(stop_path(cfg)):
                log("stop requested — supervisor exiting")
                return 0
            time.sleep(1)


def _pump(cfg, proc, log, started, stale, daily):
    """Copy child output to the log; decide when to restart it."""
    import queue
    import threading
    q = queue.Queue()

    def reader():
        for line in proc.stdout:
            q.put(line)
        q.put(None)

    threading.Thread(target=reader, daemon=True).start()
    done_today = None
    checked = 0.0
    while True:
        try:
            line = q.get(timeout=2)
            if line is None:
                return "exited"
            log(line)
        except queue.Empty:
            if proc.poll() is not None:
                return "exited"
        # run the checks on a clock, not only when the watcher is quiet: a watcher that
        # logs an error every few seconds must still be stoppable
        if time.time() - checked < 2:
            continue
        checked = time.time()
        if os.path.exists(stop_path(cfg)):
            return "stop"
        hb = heartbeat_path(cfg)
        last = os.path.getmtime(hb) if os.path.exists(hb) else started
        if time.time() - last > stale and time.time() - started > stale:
            return f"no heartbeat for {int(time.time() - last)} s"
        if daily:
            now = datetime.datetime.now()
            if now.strftime("%H:%M") == daily and done_today != now.date() and time.time() - started > 120:
                done_today = now.date()
                return f"daily restart {daily}"


# ----------------------------------------------------------- task scheduler
def _task_xml(pyw, args, home, user):
    esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    start = datetime.datetime.now().replace(microsecond=0).isoformat()
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{esc(brand.APP_NAME)} - keeps the phone assistant running</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{esc(user)}</UserId>
      <Delay>PT45S</Delay>
    </LogonTrigger>
    <TimeTrigger>
      <Enabled>true</Enabled>
      <StartBoundary>{start}</StartBoundary>
      <Repetition>
        <Interval>PT5M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{esc(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>4</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{esc(pyw)}</Command>
      <Arguments>{esc(args)}</Arguments>
      <WorkingDirectory>{esc(home)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _pythonw():
    exe = sys.executable
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return cand if os.path.isfile(cand) else exe


def _autostart_file():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "autostart", brand.APP_ID.lower() + ".desktop")


def _install_linux(cfg):
    launcher = os.path.join(root_dir(), "shop-assistant.sh")
    quoted = '"' + launcher.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`") + '"'
    path = _autostart_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("[Desktop Entry]\nType=Application\n"
                f"Name={brand.APP_NAME}\nComment=Keeps the phone assistant running\n"
                f"Exec={quoted} service run\nTerminal=false\nX-GNOME-Autostart-enabled=true\n"
                "X-GNOME-Autostart-Delay=20\n")
    start(cfg)
    print(f"{brand.APP_NAME} will start when you log in ({path}).")
    return 0


def install(cfg):
    if sys.platform != "win32":
        return _install_linux(cfg)
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\")
    xml = _task_xml(_pythonw(), "-m shopassistant service run", app_dir(), user)
    fd, path = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    with open(path, "w", encoding="utf-16") as f:
        f.write(xml)
    try:
        p = subprocess.run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", path, "/F"],
                           capture_output=True, text=True)
    finally:
        os.unlink(path)
    print((p.stdout or p.stderr).strip())
    if p.returncode != 0:
        return p.returncode
    start(cfg)
    print(f"task '{TASK_NAME}' installed for {user} and started.\n"
          f"  starts at logon (45 s delay), re-checks every 5 min, restarts the bot on any crash.\n"
          f"  logs: {os.path.join(_logs(cfg), 'service')}")
    return 0


def uninstall(cfg):
    stop(cfg, wait=True)
    if sys.platform != "win32":
        try:
            os.unlink(_autostart_file())
        except OSError:
            pass
        print("removed from login startup")
        return 0
    p = subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True, text=True)
    print((p.stdout or p.stderr).strip())
    return p.returncode


def stop(cfg, wait=True):
    os.makedirs(_logs(cfg), exist_ok=True)
    open(stop_path(cfg), "w").close()
    print("stop requested - the assistant exits within a few seconds.")
    print("It stays stopped until you start it again.")
    if wait:
        time.sleep(8)
    return 0


def running():
    """Is a supervisor alive right now?"""
    if sys.platform == "win32":
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenMutexW(0x00100000, False, MUTEX)      # SYNCHRONIZE
        if h:
            k32.CloseHandle(h)
            return True
        return False
    import fcntl
    try:
        with open(_lock_path(), "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh, fcntl.LOCK_UN)
        return False
    except OSError:
        return True


def task_installed():
    if sys.platform != "win32":
        return os.path.isfile(_autostart_file())
    p = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME], capture_output=True, text=True)
    return p.returncode == 0


def spawn():
    """Start the supervisor in the background, detached from whoever asked."""
    kw = {"cwd": app_dir(), "env": _child_env(), "stdin": subprocess.DEVNULL,
          "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    cmd = [_pythonw(), "-m", "shopassistant", "service", "run"]
    if sys.platform != "win32":
        return subprocess.Popen(cmd, start_new_session=True, **kw)
    try:      # DETACHED | NEW_GROUP | BREAKAWAY_FROM_JOB
        return subprocess.Popen(cmd, creationflags=0x00000008 | 0x00000200 | 0x01000000, **kw)
    except OSError:   # launched from inside a job that forbids breakaway
        return subprocess.Popen(cmd, creationflags=0x00000008 | 0x00000200, **kw)


def start(cfg):
    try:
        os.unlink(stop_path(cfg))
    except OSError:
        pass
    if running():
        return 0
    spawn()
    return 0


def status(cfg):
    hb = heartbeat_path(cfg)
    if os.path.exists(hb):
        age = time.time() - os.path.getmtime(hb)
        print(f"last heartbeat: {int(age)} s ago ({'healthy' if age < 120 else 'STALE — check the log'})")
    else:
        print("no heartbeat - the assistant is not running")
    print("stop flag set (service stopped on purpose)" if os.path.exists(stop_path(cfg)) else "stop flag: not set")
    if sys.platform == "win32":
        p = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST"], capture_output=True, text=True)
        print((p.stdout or p.stderr).strip())
    logs = sorted(glob.glob(os.path.join(_logs(cfg), "service", "*.log")))
    if logs:
        print(f"log: {logs[-1]}")
    return 0


def main(action, cfg):
    return {"run": run, "install": install, "uninstall": uninstall,
            "stop": stop, "start": start, "status": status}[action](cfg)
