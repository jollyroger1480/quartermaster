"""Local web panel for the live call — buttons mirroring the Telegram controls.

Served by the watch process on 127.0.0.1 only (gui.port, default 8795):
  GET  /          the button page (auto-refreshing state)
  POST /act       one action: rec | ai | hangup | say  (form field `text` for say)
  GET  /state     JSON snapshot
Stdlib http.server only; no LAN exposure, no auth — it never leaves localhost.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

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
 input{width:100%;box-sizing:border-box;padding:12px;font-size:15px;border-radius:10px;
       border:1px solid #374151;background:#11151b;color:#e8eaem;margin:6px 0}
 .note{font-size:12px;color:#6b7280;margin-top:14px}
</style></head><body><div class="panel">
<h1>⚓ Quartermaster</h1><div class="st" id="st">loading…</div>
<button class="rec" id="b-rec" onclick="act('rec')">⏺ RECORD CALL</button>
<button class="ai" id="b-ai" onclick="act('ai')">🤖 AI TAKE OVER</button>
<button class="end" onclick="act('hangup')">⏹ HANG UP</button>
<input id="say" placeholder="say to caller…"
       onkeydown="if(event.key==='Enter')act('say')">
<button style="background:#374151" onclick="act('say')">🗣 Speak to caller</button>
<div class="note">localhost only — this panel controls the live phone call.</div>
</div><script>
async function st(){try{const r=await fetch('/state');const j=await r.json();
 const rec=j.recording, cop=j.copilot, on=j.on_call;
 document.getElementById('b-rec').textContent=rec?'⏹ STOP RECORDING':'⏺ RECORD CALL';
 document.getElementById('b-rec').className='rec'+(rec?' on':'');
 document.getElementById('b-ai').className='ai'+(cop&&!on?' on':'');
 document.getElementById('b-ai').textContent=on?(cop?'🤖 AI TAKE OVER':'🤖 AI STEP BACK')
   :'🤖 AI JOIN CALL';
 document.getElementById('st').textContent=
  (on?('LIVE CALL — '+j.number+(cop?' (copilot: you have the call)':' (bot has the call)'))
     :'no active call')+(j.last_event?('\\n'+j.last_event):'');
}catch(e){}} 
async function act(k){const s=document.getElementById('say').value;
 const f=new FormData();f.append('op',k);if(s)f.append('text',s);
 if(k==='say'&&!s)return;await fetch('/act',{method:'POST',body:f});
 if(k==='say')document.getElementById('say').value='';st();setTimeout(st,800);}
st();setInterval(st,3000);
</script></body></html>"""


def start_server(cfg, controls, log=print):
    if not dig(cfg, "gui.enabled", True):
        return None
    port = int(dig(cfg, "gui.port", 8795))

    class _H(BaseHTTPRequestHandler):
        def _cors(self):
            self.send_header("Cache-Control", "no-store")

        def do_GET(self):
            if self.path.startswith("/state"):
                body = json.dumps(controls.snapshot()).encode()
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
            else:
                note = None
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
