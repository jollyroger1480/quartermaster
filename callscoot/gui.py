"""Local web panel for the live call — buttons mirroring the Telegram controls.

Served by the watch process on 127.0.0.1 only (gui.port, default 8795):
  GET  /          the button page (auto-refreshing state)
  POST /act       one action: rec | ai | hangup | say | reconnect | status | listen | secretary | rings_up | rings_down
  GET  /state     JSON snapshot
Stdlib http.server only; no LAN exposure, no auth — it never leaves localhost.
"""
import json
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from . import audio, errors, phone
from .config import dig
from .controls import rings_path, secretary_flag_path

import os

def _load_page():
    path = os.path.join(os.path.dirname(__file__), "panel.html")
    with open(path, encoding="utf-8") as fh:
        return fh.read()

_PAGE = _load_page()


def read_form(content_type, raw):
    """op and text from a button post. Browsers send multipart; curl sends urlencoded."""
    ctype = content_type or ""
    if "multipart/form-data" in ctype.lower():
        m = re.search(r"boundary=([^;]+)", ctype, re.I)
        if not m:
            return "", ""
        boundary = m.group(1).strip().strip('"').encode()
        op = text = ""
        for part in raw.split(b"--" + boundary):
            if b"Content-Disposition" not in part:
                continue
            head, _, body = part.partition(b"\r\n\r\n")
            name_m = re.search(rb'name="([^"]+)"', head)
            if not name_m:
                continue
            val = body.split(b"\r\n", 1)[0].decode(errors="replace").strip()
            name = name_m.group(1).decode()
            if name == "op":
                op = val
            elif name == "text":
                text = val
        return op, text
    form = parse_qs(raw.decode(errors="replace"))
    return (form.get("op") or [""])[0], (form.get("text") or [""])[0].strip()


def _phone_mac(cfg):
    pcm = dig(cfg, "audio.bluealsa_pcm", "") or ""
    m = re.search(r"DEV=([0-9A-Fa-f:]{17})", pcm)
    return m.group(1) if m else ""


def link_status(cfg, controls):
    """One short status block. Does not start the bot."""
    adb_out, _, rc = phone.adb(["get-state"])
    adb_state = adb_out.strip() if rc == 0 and adb_out.strip() else "down"
    try:
        st = phone.call_state()
        call = {0: "idle", 1: "ringing", 2: "offhook"}.get(st.get("state"), "unknown")
    except phone.PhoneError as e:
        call = f"unread ({e})"
    mac = _phone_mac(cfg)
    bt = "no mac"
    if mac:
        p = subprocess.run(["bluetoothctl", "info", mac], capture_output=True, text=True, timeout=8)
        bt = "connected" if "Connected: yes" in p.stdout else "not connected"
    hfp = "present" if audio.bluealsa_ready(cfg) else "missing"
    listen = "on" if audio.live.on else "off"
    text = f"ADB {adb_state} · call {call} · Bluetooth {bt} · headset {hfp} · listen {listen}"
    controls.link_status = text
    return text


def handle_act(cfg, controls, op, text=""):
    """One panel action. Reconnect and status never arm the bot."""
    note = None
    try:
        if op == "rec":
            note = controls.toggle_record()
        elif op == "ai":
            note = controls.toggle_ai()
        elif op == "hangup":
            controls.post("hangup")
            note = "hanging up"
        elif op == "say" and text:
            if controls.snapshot()["on_call"]:
                controls.post("say_to_caller", text)
                note = f"speaking to caller: {text[:50]}"
            else:
                note = "no active call"
        elif op == "reconnect":
            note = reconnect_link(cfg, controls)
        elif op == "status":
            note = link_status(cfg, controls)
        elif op == "listen":
            note = audio.live.set(cfg, not audio.live.on)
        elif op == "secretary":
            note = controls.toggle_secretary(secretary_flag_path(cfg))
        elif op == "rings_up":
            note = controls.set_rings(controls.rings + 1, rings_path(cfg))
        elif op == "rings_down":
            note = controls.set_rings(controls.rings - 1, rings_path(cfg))
    except Exception as e:
        errors.record(f"panel-{op or 'act'}", e, cfg)
        note = f"{op or 'act'} failed: {e}"
    if note:
        errors.info(f"panel {op}: {note}", cfg)
    return note


def reconnect_link(cfg, controls):
    """Bring the phone link back. Does not arm AI, record, or answer."""
    mac = _phone_mac(cfg)
    parts = []
    if mac:
        p = subprocess.run(["bluetoothctl", "connect", mac], capture_output=True, text=True, timeout=20)
        line = (p.stdout or p.stderr or "").strip().splitlines()
        parts.append("Bluetooth " + (line[-1] if line else ("ok" if p.returncode == 0 else "failed")))
    else:
        parts.append("Bluetooth mac missing")
    try:
        phone.ensure_connected(cfg)
        parts.append("ADB device")
    except phone.PhoneError as e:
        parts.append("ADB " + str(e).splitlines()[0])
    controls.link_status = link_status(cfg, controls)
    return "reconnected, AI not started — " + " | ".join(parts)


def start_server(cfg, controls, log=print):
    if not dig(cfg, "gui.enabled", True):
        return None
    port = int(dig(cfg, "gui.port", 8795))

    class _H(BaseHTTPRequestHandler):
        def _cors(self):
            self.send_header("Cache-Control", "no-store")

        def do_GET(self):
            if self.path.startswith("/state"):
                snap = controls.snapshot()
                snap["listening"] = bool(audio.live.on)
                body = json.dumps(snap).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = _PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if not self.path.startswith("/act"):
                self.send_response(404)
                self.end_headers()
                return
            n = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(n)
            op, text = read_form(self.headers.get("Content-Type", ""), raw)
            note = handle_act(cfg, controls, op, text)
            body = json.dumps({"ok": bool(note), "note": note}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            if note:
                log(f"gui control: {note}")
                controls.note_event(note)

        def log_message(self, fmt, *args):  # silence request noise
            pass

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), _H)
    except OSError as e:
        log(f"gui server not started ({e})")
        return None
    t = threading.Thread(target=srv.serve_forever, name="gui", daemon=True)
    t.start()
    log(f"web panel: http://127.0.0.1:{port}")
    return srv
