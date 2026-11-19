"""Launch the browser (Edge on Windows; Chrome, Edge or Chromium on Linux) as a
dedicated Google Voice app window.

A separate --user-data-dir keeps this profile (and its Google sign-in) apart
from your everyday Edge, and is required for --remote-debugging-port on
current Chromium builds. The bot attaches over CDP on 127.0.0.1 only; Edge
keeps running if the bot restarts, so a crash never drops a live call window.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

from . import brand
from .config import data_dir, dig

DEFAULT_URL = "https://voice.google.com/u/0/messages"

EDGE_CANDIDATES = [
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe",
]


class EdgeError(RuntimeError):
    pass


def edge_exe(cfg):
    explicit = dig(cfg, "browser.edge_path", "")
    cands = ([explicit] if explicit else []) + EDGE_CANDIDATES
    for c in cands:
        p = os.path.expandvars(c)
        if os.path.isfile(p):
            return p
    from shutil import which
    for name in ("google-chrome-stable", "google-chrome", "microsoft-edge-stable", "microsoft-edge", "msedge",
                 "chromium", "chromium-browser", "brave-browser"):
        w = which(name)
        if w:
            return w
    if sys.platform == "win32":
        raise EdgeError("Microsoft Edge was not found on this computer")
    raise EdgeError("No supported browser found. Install Google Chrome, Microsoft Edge or Chromium.")


def profile_dir(cfg):
    custom = dig(cfg, "browser.profile_dir", "")
    if custom:
        return os.path.expanduser(os.path.expandvars(custom))
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, brand.APP_ID, "edge-profile")
    # Linux: keep it in the app's own data folder (snap-packaged Chromium can't use hidden folders)
    return os.path.join(data_dir(), "browser-profile")


def cdp_port(cfg):
    return int(dig(cfg, "browser.cdp_port", 9222))


def cdp_url(cfg):
    return f"http://127.0.0.1:{cdp_port(cfg)}"


def cdp_alive(cfg, timeout=1.5):
    try:
        with urllib.request.urlopen(cdp_url(cfg) + "/json/version", timeout=timeout) as r:
            return json.load(r).get("Browser", "?")
    except Exception:
        return None


def _args(cfg, debug=True):
    url = dig(cfg, "browser.url", DEFAULT_URL)
    args = [
        f"--user-data-dir={profile_dir(cfg)}",
        "--no-first-run",
        "--no-default-browser-check",
        f"--app={url}",
        "--window-size=1100,850",
    ]
    if debug:
        args += [
            f"--remote-debugging-port={cdp_port(cfg)}",
            "--remote-debugging-address=127.0.0.1",
            # audio must start without a click, and must keep running when the
            # window is minimized or covered
            "--autoplay-policy=no-user-gesture-required",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
            "--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling",
            "--use-fake-ui-for-media-stream",
        ]
    if sys.platform != "win32":
        args.append("--password-store=basic")   # no desktop-keyring prompt blocking an unattended start
    args += list(dig(cfg, "browser.extra_args", []) or [])
    return args


def _spawn(cmd):
    kw = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "stdin": subprocess.DEVNULL}
    if sys.platform != "win32":
        return subprocess.Popen(cmd, start_new_session=True, **kw)
    try:      # DETACHED | NEW_GROUP | BREAKAWAY_FROM_JOB (Edge outlives assistant restarts)
        return subprocess.Popen(cmd, creationflags=0x00000008 | 0x00000200 | 0x01000000, **kw)
    except OSError:
        return subprocess.Popen(cmd, creationflags=0x00000008 | 0x00000200, **kw)


def ensure_running(cfg, timeout=25, log=print):
    """Start the dedicated Edge app window with CDP if it isn't already up."""
    if cdp_alive(cfg):
        return
    os.makedirs(profile_dir(cfg), exist_ok=True)
    exe = edge_exe(cfg)
    log(f"opening the Google Voice window ({os.path.basename(exe)})")
    _spawn([exe, *_args(cfg, debug=True)])
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cdp_alive(cfg):
            return
        time.sleep(0.5)
    raise EdgeError(
        f"The browser started but its control port {cdp_port(cfg)} never answered. If a Google Voice "
        "window from the sign-in step is still open, close it and try again."
    )


def launch_signin(cfg):
    """Plain window (no debugging) for the one-time Google sign-in; Google is
    stricter about sign-ins from debuggable browsers."""
    if cdp_alive(cfg):
        raise EdgeError("the assistant's Google Voice window is open - close it first, then sign in")
    os.makedirs(profile_dir(cfg), exist_ok=True)
    return _spawn([edge_exe(cfg), *_args(cfg, debug=False)])
