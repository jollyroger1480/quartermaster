"""Command line (the control panel runs these for you).

  panel                 open the control panel
  watch [--no-answer]   run the phone assistant in this window
  service run|start|stop|status|install|uninstall
  signin                one-time Google sign-in in the dedicated browser profile
  voice                 open the Google Voice app window (with the debug port)
  doctor [--json]       check every layer
  probe [-s 180]        record what the Voice page shows during a test call
  chat [--sms]          talk to the assistant from the console (nothing is sent)
  say "text" [--out f]  hear / save the assistant's voice
  bench [--models a,b]  time speech recognition, voice and the AI service
"""
import argparse
import asyncio
import json
import os
import sys
import time

from . import __version__, brand
from .config import dig, load, logs_dir


def _log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def _quiet_console():
    """pythonw has no console: send stray prints and tracebacks to a file."""
    if sys.stdout is None or sys.stderr is None:
        f = open(os.path.join(logs_dir(), "background.log"), "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or f
        sys.stderr = sys.stderr or f


# ------------------------------------------------------------------ doctor
def checks(cfg):
    """[(ok, label, detail)] - used by `doctor` and by the control panel."""
    from . import edge, knowledge, llm
    rows = []

    def check(label, fn):
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {e}"
        rows.append((bool(ok), label, detail))

    def py():
        import struct
        v = sys.version_info
        bits = 8 * struct.calcsize("P")
        return (v.major, v.minor) >= (3, 11) and bits == 64, f"{v.major}.{v.minor}.{v.micro} {bits}-bit"

    def mod(name):
        import importlib.util
        return (importlib.util.find_spec(name) is not None,
                "installed" if importlib.util.find_spec(name) else "missing - run Install again")

    def cdp():
        b = edge.cdp_alive(cfg)
        return bool(b), b or "the Google Voice window is not open"

    def page():
        if not edge.cdp_alive(cfg):
            return False, "skipped (the Google Voice window is not open)"
        return asyncio.run(_page_check(cfg))

    def llm_ping():
        try:
            reply, provider = llm.chat(cfg, [{"role": "user", "content": "Reply with exactly: OK"}])
            return True, f"{provider}: {reply[:40]}"
        except llm.LLMError as e:
            msg = str(e)
            if "404" in msg:
                for name, models in llm.list_models(cfg).items():
                    shown = ", ".join(models[:12]) if isinstance(models, list) else models
                    msg += f" | models this key can use on {name}: {shown}"
            return False, msg

    def know():
        n = len(knowledge.files(cfg))
        if not n:
            return False, "no knowledge files yet"
        if knowledge.unfinished(cfg):
            return False, f"{n} file(s), but the template still has [FILL IN] parts"
        return True, f"{n} file(s)"

    def business():
        ok = bool(dig(cfg, "persona.shop", "") and dig(cfg, "persona.owner", ""))
        return ok, "set" if ok else "business name and owner are not filled in"

    def ffmpeg():
        from shutil import which
        return True, "found (small .ogg recordings)" if which("ffmpeg") else "not installed - recordings are saved as .wav (fine)"

    check("Python", py)
    check("Speech recognition", lambda: mod("faster_whisper"))
    check("Voice", lambda: mod("kokoro"))
    check("Browser control", lambda: mod("playwright"))
    check("Browser", lambda: (True, edge.edge_exe(cfg)))
    check("Business details", business)
    check("Knowledge", know)
    check("AI service", llm_ping)
    check("Google Voice window", cdp)
    check("Google Voice page", page)
    check("ffmpeg (optional)", ffmpeg)
    return rows


async def _page_check(cfg):
    from playwright.async_api import async_playwright
    from . import edge
    async with async_playwright() as pw:
        b = await pw.chromium.connect_over_cdp(edge.cdp_url(cfg))
        match = dig(cfg, "browser.match", "voice.google.com")
        pages = [p for c in b.contexts for p in c.pages if match in (p.url or "")]
        if not pages:
            return False, "the window is open but not on Google Voice - sign in again"
        has = await pages[0].evaluate("() => !!window.__qm")
        return True, "signed in" + (", audio bridge loaded" if has else "")


def cmd_doctor(cfg, as_json):
    rows = checks(cfg)
    if as_json:
        print(json.dumps([{"ok": ok, "label": label, "detail": detail} for ok, label, detail in rows]))
        return 0
    bad = 0
    for ok, label, detail in rows:
        bad += not ok
        print(f"  {'OK ' if ok else 'XX '} {label:<22} {detail}")
    print("\nall good" if not bad else f"\n{bad} problem(s) above.")
    return 1 if bad else 0


# ------------------------------------------------------------------- probe
async def _probe(cfg, seconds):
    from .gv import GoogleVoice
    out = os.path.join(logs_dir(cfg), f"probe_{time.strftime('%Y%m%d_%H%M%S')}.jsonl")
    gv = await GoogleVoice(cfg, log=_log).start()
    frames = [0]
    gv.audio_sink = lambda pcm: frames.__setitem__(0, frames[0] + pcm.size)
    _log(f"probing for {seconds}s - call the Google Voice number from another phone, answer it IN THE "
         f"VOICE WINDOW, say a few words, hang up. Writing {out}")
    last = None
    t_end = time.monotonic() + seconds
    with open(out, "w", encoding="utf-8") as f:
        while time.monotonic() < t_end:
            snap = await gv.snapshot()
            st = await gv.state()
            snap["state"] = st
            snap["caller_samples"] = frames[0]
            f.write(json.dumps(snap) + "\n")
            f.flush()
            key = json.dumps([[b.get("aria") or b.get("text") for b in fr.get("buttons", [])] for fr in snap["frames"]])
            if key != last:
                last = key
                labels = sorted({(b.get("aria") or b.get("text")) for fr in snap["frames"]
                                 for b in fr.get("buttons", []) if (b.get("aria") or b.get("text"))})
                _log(f"state={st} caller_audio={frames[0] / 16000:.1f}s")
                _log("  buttons: " + " | ".join(labels[:60]))
            while gv.events:
                _log(f"  bridge: {gv.events.popleft()}")
            await asyncio.sleep(1)
    await gv.close()
    _log(f"done - saved {out}")


# --------------------------------------------------------------- chat / say
def cmd_chat(cfg, sms):
    from . import brain
    hist = []
    print(("Type texts" if sms else "Type what a caller would say") + ". Empty line or Ctrl+C to quit.")
    while True:
        try:
            msg = input("them> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not msg:
            return 0
        t = time.perf_counter()
        reply, provider, esc = (brain.sms_reply if sms else brain.reply)(cfg, hist, msg)
        print(f"bot > {reply}\n      [{provider} {time.perf_counter() - t:.1f}s{' ESCALATION' if esc else ''}]")
        hist = (hist + [{"role": "user", "content": msg}, {"role": "assistant", "content": reply}])[-12:]


def cmd_say(cfg, text, out):
    import tempfile
    from .audio import write_wav
    from .speech import TTS
    pcm, rate = TTS(cfg, log=_log).synth(text)
    if out:
        write_wav(out, pcm, rate)
        return 0
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    write_wav(path, pcm, rate)
    if sys.platform == "win32":
        import winsound
        winsound.PlaySound(path, winsound.SND_FILENAME)
        os.unlink(path)
    else:
        print(f"wrote {path}")
    return 0


# ------------------------------------------------------------------- bench
BENCH_LINE = "Hi, I have a question about your prices, and I'd like to leave a message for the owner if that's okay."


def cmd_bench(cfg, models):
    import copy
    import platform
    import struct

    import numpy as np

    from .audio import resample
    from .speech import STT, TTS
    cpu = platform.processor() or "?"
    if sys.platform == "win32":
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            cpu = winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
        except OSError:
            pass
    print(f"CPU: {cpu}  logical cores: {os.cpu_count()}  python {sys.version.split()[0]} {8 * struct.calcsize('P')}-bit")

    tts = TTS(cfg, log=_log)
    t = time.perf_counter()
    tts.warm()
    print(f"  voice load         {time.perf_counter() - t:5.1f}s  ({dig(cfg, 'tts.backend', 'kokoro')})")
    t = time.perf_counter()
    first, chunks = None, []
    for pcm, rate in tts.stream(BENCH_LINE):
        if first is None:
            first = time.perf_counter() - t
        chunks.append(resample(pcm, rate, 16000))
    total = time.perf_counter() - t
    audio = np.concatenate(chunks)
    secs = audio.size / 16000
    note = "OK" if first < 1.5 else "SLOW - a faster computer will sound more natural"
    print(f"  voice              first audio {first:4.1f}s, {total:4.1f}s for {secs:.1f}s of speech ({note})")

    speed = {}
    for m in models:
        c = copy.deepcopy(cfg)
        c.setdefault("stt", {})["model"] = m
        stt = STT(c)
        t = time.perf_counter()
        stt.transcribe(np.zeros(16000, dtype=np.int16))
        load_s = time.perf_counter() - t
        t = time.perf_counter()
        text = stt.transcribe(audio)
        run = time.perf_counter() - t
        speed[m] = run / secs
        print(f"  listening {m:<10} load {load_s:4.1f}s, {run:4.1f}s for {secs:.1f}s of audio "
              f"(x{run / secs:.2f} real time) -> {text[:60]!r}")
    try:
        from . import brain
        t = time.perf_counter()
        reply, provider, _ = brain.reply(cfg, [], "what are your hours")
        print(f"  AI {provider:<15} {time.perf_counter() - t:4.1f}s -> {reply[:60]!r}")
    except Exception as e:
        print(f"  AI                 FAILED {e}")
    print()
    ok = [m for m, v in speed.items() if v <= 0.35]
    if ok:
        print(f"Recommended listening model: {ok[0]}")
    else:
        print("Every model tested is slower than x0.35 real time on this computer; "
              "callers will notice the pauses. Use tiny.en or a faster computer.")
    return 0


# -------------------------------------------------------------------- main
def main(argv=None):
    _quiet_console()
    ap = argparse.ArgumentParser(prog="shopassistant", description=f"{brand.APP_NAME} {__version__}\n{__doc__}",
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("panel")
    p.add_argument("--no-open", action="store_true")
    p = sub.add_parser("watch")
    p.add_argument("--no-answer", action="store_true")
    p = sub.add_parser("service")
    p.add_argument("action", choices=["run", "install", "uninstall", "stop", "start", "status"])
    sub.add_parser("signin")
    sub.add_parser("voice")
    p = sub.add_parser("doctor")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("probe")
    p.add_argument("-s", "--seconds", type=int, default=180)
    p = sub.add_parser("chat")
    p.add_argument("--sms", action="store_true")
    p = sub.add_parser("say")
    p.add_argument("text", nargs="+")
    p.add_argument("--out", default="")
    p = sub.add_parser("bench")
    p.add_argument("--models", default="base.en,small.en,tiny.en")
    sub.add_parser("version")
    a = ap.parse_args(argv)
    cfg = load()
    cmd = a.cmd or "panel"

    if cmd == "version":
        print(f"{brand.APP_NAME} {__version__}")
        return 0
    if cmd == "panel":
        from . import panel
        return panel.serve(open_browser=not getattr(a, "no_open", False))
    from . import edge
    if cmd == "signin":
        edge.launch_signin(cfg)
        print("Sign in to Google in the window that opened, open Voice, then CLOSE that window.")
        return 0
    if cmd == "voice":
        edge.ensure_running(cfg, log=_log)
        return 0
    if cmd == "doctor":
        return cmd_doctor(cfg, a.json)
    if cmd == "probe":
        asyncio.run(_probe(cfg, a.seconds))
        return 0
    if cmd == "chat":
        return cmd_chat(cfg, a.sms)
    if cmd == "say":
        return cmd_say(cfg, " ".join(a.text), a.out)
    if cmd == "bench":
        return cmd_bench(cfg, [m.strip() for m in a.models.split(",") if m.strip()])
    if cmd == "service":
        from . import service
        return service.main(a.action, cfg)
    if cmd == "watch":
        from .watch import watch
        try:
            asyncio.run(watch(cfg, answer=not a.no_answer, log=_log, reload=True))
        except KeyboardInterrupt:
            _log("stopped")
        return 0
    return 1
