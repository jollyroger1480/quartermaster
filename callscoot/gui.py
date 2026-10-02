"""Local web panel for the live call — buttons mirroring the Telegram controls.

Served by the watch process on 127.0.0.1 only (gui.port, default 8795):
  GET  /          the button page (auto-refreshing state)
  POST /act       one action: rec | ai | hangup | say | reconnect | status | listen
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

_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Quartermaster — call controls</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 body{font-family:system-ui;background:#14181f;color:#e8eaed;display:grid;
      place-items:center;min-height:96vh;margin:0}
 .panel{background:#1d232d;border-radius:14px;padding:28px 34px;min-width:340px}
 h1{font-size:18px;margin:0 0 4px}
 .st{font-size:13px;color:#9aa4b2;margin-bottom:18px;white-space:pre-line}
 button{display:block;width:100%;margin:8px 0;padding:14px;font-size:16px;
        border:0;border-radius:10px;cursor:pointer;font-weight:600}
 .rec{background:#7f1d1d;color:#fff}.rec.on{background:#16a34a}
 .ai{background:#1d4ed8;color:#fff}.ai.on{background:#7c3aed}
 .end{background:#374151;color:#fff}
 .link{background:#0f3d3e;color:#fff}.link.on{background:#0f766e}
 h2{font-size:12px;letter-spacing:.04em;color:#6b7280;margin:18px 0 6px;font-weight:600}
 input{width:100%;box-sizing:border-box;padding:12px;font-size:15px;border-radius:10px;
       border:1px solid #374151;background:#11151b;color:#e8eaed;margin:6px 0}
 .note{font-size:12px;color:#6b7280;margin-top:14px}
</style></head><body><div class="panel">
<h1>⚓ Quartermaster</h1><div class="st" id="st">loading…</div>
<button class="rec" id="b-rec" onclick="act('rec')">⏺ RECORD (no AI)</button>
<button class="ai" id="b-ai" onclick="act('ai')">🤖 TURN AI ON</button>
<button class="end" onclick="act('hangup')">⏹ HANG UP</button>
<input id="say" placeholder="say to caller…"
       onkeydown="if(event.key==='Enter')act('say')">
<button style="background:#374151" onclick="act('say')">🗣 Speak to caller</button>
<h2>PHONE LINK</h2>
<button class="link" onclick="act('reconnect')">↻ RECONNECT (no AI)</button>
<button class="link" onclick="act('status')">☰ STATUS</button>
<button class="link" id="b-listen" onclick="act('listen')">🎧 LISTEN LIVE</button>
<div class="note">Reconnect brings Bluetooth and wireless debugging back. It does not start the bot. Listen plays the caller and the bot on this PC. Use headphones so the phone does not hear the room. Your mic stays off the call.</div>
</div><script>
async function st(){try{const r=await fetch('/state');const j=await r.json();
 const rec=j.recording, pend=j.pending_record, cop=j.copilot, on=j.on_call, join=j.pending_join;
 document.getElementById('b-rec').textContent=(rec||pend)?'⏹ STOP RECORDING':'⏺ RECORD (no AI)';
 document.getElementById('b-rec').className='rec'+((rec||pend)?' on':'');
 document.getElementById('b-ai').className='ai'+((on&&!cop)||join?' on':'');
 document.getElementById('b-ai').textContent=on?(cop?'🤖 TURN AI ON':'🤖 TURN AI OFF')
   :(join?'🤖 CANCEL AI':'🤖 TURN AI ON');
 const ear=document.getElementById('b-listen');
 ear.textContent=j.listening?'🎧 STOP LISTEN':'🎧 LISTEN LIVE';
 ear.className='link'+(j.listening?' on':'');
 var line=on?('LIVE CALL — '+j.number+(cop?' (you have it, AI off)':' (AI is talking)'))
   :'no active call';
 if(!on&&pend) line+='\\nRecord armed. The bot will not talk.';
 if(!on&&join) line+='\\nAI armed. It will talk when the call is up.';
 if(j.listening) line+='\\nListen on — caller and bot play here.';
 if(j.link) line+='\\n'+j.link;
 if(j.last_event && line.indexOf(j.last_event)<0) line+='\\n'+j.last_event;
 document.getElementById('st').textContent=line;
}catch(e){}}
async function act(k){const s=document.getElementById('say').value;
 const f=new FormData();f.append('op',k);if(s)f.append('text',s);
 if(k==='say'&&!s)return;await fetch('/act',{method:'POST',body:f});
 if(k==='say')document.getElementById('say').value='';st();setTimeout(st,800);}
st();setInterval(st,3000);
</script></body></html>"""


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
    ip = dig(cfg, "phone.adb_ip", "")
    port = dig(cfg, "phone.adb_port", 0)
    if ip and port:
        out, err, _ = phone.adb(["connect", f"{ip}:{port}"], timeout=12)
        parts.append((out or err or "adb connect").strip().splitlines()[-1])
    else:
        parts.append("adb target missing")
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
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if not self.path.startswith("/act"):
                self.send_response(404)
                self.end_headers()
                return
            n = int(self.headers.get("Content-Length", 0) or 0)
            form = parse_qs(self.rfile.read(n).decode())
            op = (form.get("op") or [""])[0]
            text = (form.get("text") or [""])[0].strip()
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
