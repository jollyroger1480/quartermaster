"""The control panel: a small web app served on this computer only
(127.0.0.1) and opened in a browser app window. It edits settings and
knowledge, starts and stops the assistant, and shows the calls it took.

Only this computer can reach it, and every request must carry the token
that was handed to the page, so other web pages can't drive it.
"""
import datetime
import glob
import json
import os
import re
import secrets as pysecrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, brand, config, knowledge, service
from .config import dig

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "panel")
TOKEN = pysecrets.token_urlsafe(24)
CALL_ID = re.compile(r"^call_\d{8}_\d{6}_[0-9a-z]+$")
FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,60}\.(md|txt)$")
JOBS = {}
EDITABLE = {"persona", "call", "audio", "stt", "tts", "sms", "phone", "spam"}


class Bad(Exception):
    pass


# ------------------------------------------------------------------ helpers
def _python(windowed=False):
    exe = sys.executable
    low = exe.lower()
    if windowed:
        cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
        return cand if os.path.isfile(cand) else exe
    if low.endswith("pythonw.exe"):
        return exe[:-5] + ".exe"
    return exe


def _run_cli(args, timeout=120):
    env = dict(os.environ)
    env["SHOPASSISTANT_HOME"] = config.root_dir()
    env["PYTHONPATH"] = config.app_dir()
    env["PYTHONUTF8"] = "1"
    flags = 0x08000000 if sys.platform == "win32" else 0
    p = subprocess.run([_python(), "-m", "shopassistant", *args], cwd=config.app_dir(), env=env,
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, creationflags=flags)
    return p.returncode, (p.stdout or ""), (p.stderr or "")


def _modules_ok():
    import importlib.util
    return {m: importlib.util.find_spec(m) is not None for m in ("faster_whisper", "kokoro", "playwright", "numpy")}


def _call_dirs(cfg):
    dirs = [d for d in glob.glob(os.path.join(config.logs_dir(cfg), "call_*")) if os.path.isdir(d)]
    return sorted(dirs, reverse=True)


def _parse_call(path, full=False):
    name = os.path.basename(path)
    m = re.match(r"call_(\d{4})(\d\d)(\d\d)_(\d\d)(\d\d)(\d\d)_(.+)$", name)
    when = f"{m.group(1)}-{m.group(2)}-{m.group(3)} {m.group(4)}:{m.group(5)}" if m else ""
    digits = m.group(7) if m else ""
    number = f"({digits[:3]}) {digits[3:6]}-{digits[6:]}" if len(digits) == 10 else digits
    text = knowledge.text_of(os.path.join(path, "transcript.md"))
    summary, turns, section = [], [], ""
    for ln in text.splitlines():
        if ln.startswith("## "):
            section = ln[3:].strip()
            continue
        if section == "summary" and ln.strip():
            summary.append(ln.strip())
        elif section == "transcript":
            mm = re.match(r"\*\*(\w+):\*\*\s*(.*)$", ln.strip())
            if mm:
                turns.append({"who": mm.group(1), "text": mm.group(2)})
    said = sum(1 for t in turns if t["who"] == "caller")
    out = {"id": name, "when": when, "number": number, "summary": summary,
           "escalation": "escalation: YES" in text, "caller_turns": said}
    if full:
        out["turns"] = turns
        out["audio"] = sorted(f for f in os.listdir(path) if f.lower().endswith((".ogg", ".wav")))
    return out


def _log_tail(cfg, n):
    files = sorted(glob.glob(os.path.join(config.logs_dir(cfg), "service", "*.log")))
    if not files:
        return []
    try:
        with open(files[-1], encoding="utf-8", errors="replace") as f:
            return [ln.rstrip("\n") for ln in f.readlines()[-n:]]
    except OSError:
        return []


def _state(cfg):
    st = config.state()
    hb = service.heartbeat_path(cfg)
    age = time.time() - os.path.getmtime(hb) if os.path.exists(hb) else None
    running = service.running()
    mods = _modules_ok()
    has_key = any(os.environ.get(p.get("api_key_env") or "") for p in dig(cfg, "llm.providers", []))
    calls = _call_dirs(cfg)
    steps = [
        {"id": "business", "title": "Tell it about your business", "page": "business",
         "done": bool(dig(cfg, "persona.shop", "") and dig(cfg, "persona.owner", "")),
         "hint": "Business name, your name, hours and how you get back to people."},
        {"id": "ai", "title": "Connect the AI service", "page": "settings",
         "done": bool(has_key and st.get("llm_ok")),
         "hint": "A free Groq key. Takes two minutes."},
        {"id": "knowledge", "title": "Write down what callers ask about", "page": "knowledge",
         "done": not knowledge.unfinished(cfg),
         "hint": "Services, prices and common questions. The assistant only knows what you write here."},
        {"id": "voice", "title": "Sign in to Google Voice", "page": "voice",
         "done": bool(st.get("voice_signed_in_at")),
         "hint": "Calls to your Google Voice number are what the assistant answers."},
        {"id": "test", "title": "Make a test call", "page": "home",
         "done": bool(calls),
         "hint": "Turn the assistant on and call your Google Voice number from another phone."},
    ]
    return {
        "brand": {"name": brand.APP_NAME, "tagline": brand.APP_TAGLINE, "maker": brand.MAKER,
                  "maker_url": brand.MAKER_URL, "accent": brand.ACCENT, "version": __version__,
                  "logo": os.path.isfile(os.path.join(HERE, "logo.png"))},
        "running": running,
        "wanted_off": os.path.exists(service.stop_path(cfg)),
        "heartbeat_age": age,
        "listening": bool(running and age is not None and age < 120),
        "autostart": service.task_installed(),
        "fault": st.get("fault") or "",
        "engine_ok": all(mods.values()),
        "engine": mods,
        "steps": steps,
        "calls": len(calls),
        "shop": dig(cfg, "persona.shop", ""),
        "can_autostart": True,
    }


def _settings_view(cfg):
    out = {k: cfg.get(k, {}) for k in EDITABLE}
    prov = (dig(cfg, "llm.providers", []) or [{}])
    out["ai"] = {"base_url": prov[0].get("base_url", ""), "model": prov[0].get("model", ""),
                 "backup_model": prov[1].get("model", "") if len(prov) > 1 else "",
                 "has_key": bool(os.environ.get(prov[0].get("api_key_env") or "")),
                 "tested_ok": bool(config.state().get("llm_ok"))}
    out["defaults"] = {"greeting": config.line(config._merge(cfg, {"call": {"greeting": ""}}), "greeting"),
                       "assistant_name": config.assistant_name(config._merge(cfg, {"persona": {"name": ""}}))}
    return out


def _test_llm(cfg):
    from . import llm
    try:
        reply, provider = llm.chat(cfg, [{"role": "user", "content": "Reply with exactly: OK"}])
        config.set_state(llm_ok=True)
        return {"ok": True, "detail": f"Connected ({provider})."}
    except llm.LLMError as e:
        config.set_state(llm_ok=False)
        msg = str(e)
        if "401" in msg:
            msg = "The AI service rejected that key. Copy it again from the Groq console and paste the whole thing."
        return {"ok": False, "detail": msg}


def _start_job(args, timeout):
    jid = pysecrets.token_hex(6)
    JOBS[jid] = {"done": False, "output": "", "ok": None}

    def work():
        try:
            code, out, err = _run_cli(args, timeout=timeout)
            lines = [ln for ln in (out + ("\n" + err if code else "")).splitlines()
                     if "Warning" not in ln and "warn" not in ln]
            JOBS[jid].update(done=True, ok=code == 0, output="\n".join(lines).strip())
        except Exception as e:
            JOBS[jid].update(done=True, ok=False, output=f"{type(e).__name__}: {e}")

    threading.Thread(target=work, daemon=True).start()
    return jid


# --------------------------------------------------------------------- API
def api(method, path, q, body):
    cfg = config.load()
    if path == "/api/state":
        return _state(cfg)
    if path == "/api/settings" and method == "GET":
        return _settings_view(cfg)
    if path == "/api/settings" and method == "POST":
        changes = {k: v for k, v in (body.get("changes") or {}).items() if k in EDITABLE and isinstance(v, dict)}
        ai = body.get("ai") or {}
        if ai:
            prov = [dict(p) for p in dig(cfg, "llm.providers", [])]
            if ai.get("base_url"):
                for p in prov:
                    p["base_url"] = ai["base_url"].strip().rstrip("/")
            if ai.get("model") and prov:
                prov[0]["model"] = ai["model"].strip()
            if ai.get("backup_model") and len(prov) > 1:
                prov[1]["model"] = ai["backup_model"].strip()
            changes["llm"] = {"providers": prov}
        cfg = config.save(changes)
        if "persona" in changes:
            knowledge.ensure_template(cfg)
        return _settings_view(cfg)
    if path == "/api/key" and method == "POST":
        env = (dig(cfg, "llm.providers", [{}])[0].get("api_key_env") or "GROQ_API_KEY")
        config.save_secret(env, (body.get("key") or "").strip())
        return _test_llm(config.load())
    if path == "/api/key/test" and method == "POST":
        return _test_llm(cfg)

    if path == "/api/knowledge" and method == "GET":
        knowledge.ensure_template(cfg)
        root = config.knowledge_dir(cfg)
        return {"files": [{"name": os.path.relpath(p, root).replace("\\", "/"), "size": os.path.getsize(p)}
                          for p in knowledge.files(cfg)],
                "unfinished": knowledge.unfinished(cfg), "folder": root}
    if path == "/api/knowledge/file":
        name = (q.get("name") or body.get("name") or "").strip()
        if not FILE_NAME.match(name):
            raise Bad("File names can use letters, numbers, spaces, dashes and must end in .md or .txt")
        full = os.path.join(config.knowledge_dir(cfg), name)
        if method == "GET":
            return {"name": name, "text": knowledge.text_of(full)}
        if body.get("delete"):
            if os.path.isfile(full):
                os.unlink(full)
            return {"ok": True}
        with open(full, "w", encoding="utf-8", newline="\n") as f:
            f.write(body.get("text") or "")
        return {"ok": True, "unfinished": knowledge.unfinished(cfg)}

    if path == "/api/calls":
        return {"calls": [_parse_call(d) for d in _call_dirs(cfg)[:300]]}
    if path == "/api/calls/item":
        cid = q.get("id", "")
        if not CALL_ID.match(cid):
            raise Bad("unknown call")
        d = os.path.join(config.logs_dir(cfg), cid)
        if not os.path.isdir(d):
            raise Bad("unknown call")
        return _parse_call(d, full=True)
    if path == "/api/log":
        return {"lines": _log_tail(cfg, min(int(q.get("n", 200)), 1000))}

    if path == "/api/bot" and method == "POST":
        action = body.get("action")
        if action in ("stop", "restart"):
            os.makedirs(config.logs_dir(cfg), exist_ok=True)
            open(service.stop_path(cfg), "w").close()
            deadline = time.time() + 25
            while service.running() and time.time() < deadline:
                time.sleep(0.5)
        if action in ("start", "restart"):
            service.start(cfg)
            time.sleep(1.5)
        return _state(cfg)
    if path == "/api/autostart" and method == "POST":
        code, out, err = _run_cli(["service", "install" if body.get("on") else "uninstall"], timeout=60)
        return {"ok": code == 0, "detail": (out or err).strip()[-400:], "autostart": service.task_installed()}
    if path == "/api/voice" and method == "POST":
        from . import edge
        action = body.get("action")
        try:
            if action == "signin":
                if edge.cdp_alive(cfg):
                    return {"ok": False, "detail": "The Google Voice window is already open with the assistant "
                                                   "attached. Turn the assistant off and close that window first."}
                edge.launch_signin(cfg)
                return {"ok": True, "detail": "A browser window opened. Sign in to Google, open Voice, then close "
                                              "that window and come back here."}
            if action == "open":
                edge.ensure_running(cfg, log=lambda m: None)
                return {"ok": True, "detail": "The Google Voice window is open."}
        except Exception as e:
            return {"ok": False, "detail": str(e)}
        raise Bad("unknown action")

    if path == "/api/chat" and method == "POST":
        from . import brain, llm
        hist = [m for m in (body.get("history") or []) if m.get("role") in ("user", "assistant")][-12:]
        text = (body.get("text") or "").strip()
        if not text:
            raise Bad("Type something first.")
        t = time.perf_counter()
        try:
            fn = brain.sms_reply if body.get("mode") == "sms" else brain.reply
            reply, provider, esc = fn(cfg, hist, text)
        except llm.LLMError as e:
            raise Bad(str(e))
        ctx, _ = knowledge.build_context(cfg, text, focus=text)
        return {"reply": reply, "provider": provider, "seconds": round(time.perf_counter() - t, 1),
                "used_knowledge": bool(ctx)}
    if path == "/api/checks" and method == "POST":
        code, out, err = _run_cli(["doctor", "--json"], timeout=90)
        try:
            return {"rows": json.loads(out.strip().splitlines()[-1])}
        except Exception:
            raise Bad("The checks could not run: " + (err or out)[-300:])
    if path == "/api/job" and method == "POST":
        kind = body.get("kind")
        if kind == "bench":
            return {"id": _start_job(["bench"], 900)}
        raise Bad("unknown job")
    if path == "/api/job" and method == "GET":
        j = JOBS.get(q.get("id", ""))
        if not j:
            raise Bad("unknown job")
        return j
    raise Bad("not found")


# ------------------------------------------------------------------ server
class Handler(BaseHTTPRequestHandler):
    server_version = "panel"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _local(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def _authed(self, q):
        return pysecrets.compare_digest(self.headers.get("X-Token") or q.get("t", ""), TOKEN)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def _handle(self, method):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if not self._local():
            return self._send(403, {"error": "local only"})
        try:
            if u.path == "/api/ping":
                return self._send(200, {"app": brand.APP_ID})
            if u.path.startswith("/api/"):
                if not self._authed(q):
                    return self._send(403, {"error": "Reload this page."})
                if u.path == "/api/audio":
                    return self._audio(q)
                if u.path == "/api/say" and method == "POST":
                    return self._say(self._body())
                return self._send(200, api(method, u.path, q, self._body() if method == "POST" else {}))
            return self._static(u.path)
        except Bad as e:
            return self._send(400, {"error": str(e)})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 2_000_000:
            raise Bad("too large")
        raw = self.rfile.read(n) if n else b"{}"
        try:
            d = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            raise Bad("bad request")
        return d if isinstance(d, dict) else {}

    def _static(self, path):
        name = {"/": "index.html"}.get(path, path.lstrip("/"))
        if not re.match(r"^[a-z0-9_.-]+\.(html|css|js|png|svg|ico)$", name):
            return self._send(404, "not found", "text/plain")
        full = os.path.join(HERE, name)
        if not os.path.isfile(full):
            return self._send(404, "not found", "text/plain")
        ctype = {"html": "text/html; charset=utf-8", "css": "text/css", "js": "text/javascript",
                 "png": "image/png", "svg": "image/svg+xml", "ico": "image/x-icon"}[name.rsplit(".", 1)[1]]
        with open(full, "rb") as f:
            data = f.read()
        if name == "index.html":
            data = (data.decode("utf-8").replace("__TOKEN__", TOKEN).replace("__APP__", brand.APP_NAME)
                    .replace("__ACCENT__", brand.ACCENT)).encode("utf-8")
        self._send(200, data, ctype)

    def _audio(self, q):
        cid, name = q.get("id", ""), q.get("file", "")
        if not CALL_ID.match(cid) or not re.match(r"^(caller|bot)\.(ogg|wav)$", name):
            raise Bad("unknown recording")
        full = os.path.join(config.logs_dir(), cid, name)
        if not os.path.isfile(full):
            raise Bad("unknown recording")
        with open(full, "rb") as f:
            data = f.read()
        self._send(200, data, "audio/ogg" if name.endswith(".ogg") else "audio/wav")

    def _say(self, body):
        text = (body.get("text") or "").strip()[:600]
        if not text:
            raise Bad("Nothing to say.")
        fd, out = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            code, _o, err = _run_cli(["say", text, "--out", out], timeout=180)
            if code != 0 or not os.path.getsize(out):
                raise Bad("The voice could not start: " + err.strip()[-300:])
            with open(out, "rb") as f:
                data = f.read()
        finally:
            try:
                os.unlink(out)
            except OSError:
                pass
        self._send(200, data, "audio/wav")


class Server(ThreadingHTTPServer):
    allow_reuse_address = sys.platform != "win32"   # on Windows this flag would let two panels share a port
    daemon_threads = True


def _open_window(url):
    from . import edge
    try:
        exe = edge.edge_exe(config.load())
        kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if sys.platform != "win32":
            kw["start_new_session"] = True
        subprocess.Popen([exe, f"--app={url}", "--window-size=1180,860"], **kw)
        return
    except Exception:
        pass
    import webbrowser
    webbrowser.open(url)


def _already_running(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1.5) as r:
            return json.load(r).get("app") == brand.APP_ID
    except Exception:
        return False


def serve(open_browser=True, port=None):
    cfg = config.load()
    knowledge.ensure_template(cfg)
    port = int(port or dig(cfg, "panel.port", 8765))
    for candidate in range(port, port + 20):
        if _already_running(candidate):
            if open_browser:
                _open_window(f"http://127.0.0.1:{candidate}/")
            return 0
        try:
            httpd = Server(("127.0.0.1", candidate), Handler)
        except OSError:
            continue
        break
    else:
        print("no free port for the control panel")
        return 1
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"{brand.APP_NAME} control panel: {url}", flush=True)
    if open_browser:
        threading.Timer(0.6, _open_window, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
