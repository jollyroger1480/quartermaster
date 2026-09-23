"""callscoot CLI."""
import argparse
import os
import shlex
import subprocess
import sys
import time

from . import agent, audio, llm, orders_index, phone, stt, tts, vault_rag, watch
from .config import app_home, dig, find_config, load


def load_env_files():
    """Load provider keys into the environment. Reads $CALLSCOOT_HOME/.env
    (recommended spot for your keys) and ~/.hermes/.env when present.
    Never overrides variables already set in the environment."""
    paths = [os.path.join(app_home(), ".env"), os.path.expanduser("~/.hermes/.env")]
    for path in paths:
        if not os.path.isfile(path):
            continue
        for line in open(path, encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            if k and k not in os.environ:
                os.environ[k] = v


def cmd_doctor(cfg):
    results = []

    def check(label, fn):
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, str(e)
        results.append((ok, label, detail))

    def c_adb():
        out, _, rc = phone.adb(["get-state"])
        if rc == 0 and out.strip() == "device":
            return True, "device attached"
        return False, "no device — run: callscoot adb-connect"

    def c_bt_adapter():
        out = subprocess.run(["bluetoothctl", "show"], capture_output=True, text=True).stdout
        if "Powered: yes" in out:
            return True, "adapter powered"
        return False, "adapter off — run: bluetoothctl power on"

    def c_bt_devices():
        out = subprocess.run(["bluetoothctl", "devices"], capture_output=True, text=True).stdout
        lines = [l for l in out.strip().splitlines() if l.strip()]
        if not lines:
            return False, "nothing paired — pair the phone in Bluetooth settings"
        return True, "; ".join(l.strip() for l in lines[:6])

    def c_hfp():
        if audio.backend(cfg) == "bluealsa":
            import shutil
            if not shutil.which("bluealsactl") and not shutil.which("bluealsa-cli"):
                return False, "bluealsa CLI missing — did the installer run?"
            if not audio.bluealsa_ready(cfg):
                return False, ("bluealsa running but no HFP PCM for the phone — "
                               "reconnect phone BT, check 'Phone audio' is on")
            return True, "bluealsa HFP PCM present"
        card = audio.bt_card()
        if not card:
            return False, "no bluez_card — the phone must be connected with call audio allowed"
        prof = audio.set_hfp_profile(cfg, card)
        if not prof:
            return False, f"card {card} present but no HFP profile would apply"
        sink, source = audio.bt_sink(cfg), audio.bt_source(cfg)
        return bool(sink and source), f"{card} profile={prof} sink={sink} source={source}"

    def c_pw():
        import shutil
        missing = [b for b in ("pactl", "pw-record", "pw-play") if not shutil.which(b)]
        return not missing, "ok" if not missing else f"missing {missing}"

    def c_piper():
        voice = dig(cfg, "tts.voice", "en_US-amy-medium")
        onnx = os.path.expanduser(f"~/.local/share/piper/{voice}.onnx")
        if not os.path.isfile(onnx):
            return False, f"voice missing: {onnx}"
        return True, voice

    def c_stt():
        import faster_whisper  # noqa: F401
        return True, f"faster-whisper ok (model '{dig(cfg, 'stt.model', 'base.en')}' downloads on first use)"

    def c_llm():
        try:
            reply, provider = llm.chat(cfg, [{"role": "user", "content": "Reply with exactly: OK"}])
            return True, f"provider '{provider}' replied: {reply[:60]}"
        except llm.LLMError as e:
            return False, str(e)[:200]

    def c_vault():
        root = os.path.expanduser(dig(cfg, "vault.root", ""))
        if not os.path.isdir(root):
            return False, f"vault root missing: {root}"
        files = vault_rag._vault_files(cfg)
        return len(files) > 0, f"{len(files)} searchable files"

    def c_graphify():
        gdir = os.path.expanduser(dig(cfg, "vault.graphify_dir", "") or "")
        if not gdir or not os.path.isdir(gdir):
            return True, "disabled"
        import shutil
        if not shutil.which("graphify"):
            return False, "graphify not on PATH"
        return True, gdir

    check("config file", lambda: (True, find_config() or "NO CONFIG — using defaults"))
    check("LLM (free cloud)", c_llm)
    check("vault + extra dirs", c_vault)
    check("graphify", c_graphify)
    check("STT (faster-whisper)", c_stt)
    check("TTS (piper)", c_piper)
    check("audio tools", c_pw)
    check("adb device", c_adb)
    check("bluetooth adapter", c_bt_adapter)
    check("paired devices", c_bt_devices)
    check("HFP audio path", c_hfp)

    fails = 0
    for ok, label, detail in results:
        mark = "✓" if ok else "✗"
        if not ok:
            fails += 1
        print(f"  {mark} {label:24s} {detail}")

    adb_ok = dict((l, o) for o, l, _ in results).get("adb device")
    hfp_ok = dict((l, o) for o, l, _ in results).get("HFP audio path")
    print("\nnext steps:")
    if not hfp_ok:
        print("  1. Pair the phone (Settings → Bluetooth → pair PC), make a test call, allow 'call audio'.")
    if not adb_ok:
        print("  2. Wireless debugging → pair:  adb pair IP:PORT   then:  callscoot adb-connect")
    if hfp_ok and adb_ok:
        print("  - manual drills:  callscoot status | call NUM | answer | hangup | listen")
        print("  - receptionist:   callscoot ask \"where is order 12345\"   then:  callscoot watch")
    return 1 if fails else 0


def cmd_status(cfg):
    st = phone.call_state()
    print(f"call: state={st['state']} (0 idle, 1 ringing, 2 offhook) number={st['number']}")
    print(f"bt card: {audio.bt_card()}")
    print(f"bt sink:   {audio.bt_sink(cfg)}")
    print(f"bt source: {audio.bt_source(cfg)}")


def cmd_adb_connect(cfg, target=None):
    if target:
        out, err, rc = phone.adb(["connect", target], timeout=12)
        print(out or err)
        return rc
    ip = dig(cfg, "phone.adb_ip", "")
    port = dig(cfg, "phone.adb_port", 5555)
    if ip:
        out, err, rc = phone.adb(["connect", f"{ip}:{port}"], timeout=12)
        print(out or err)
    else:
        print("phone.adb_ip not set in callscoot.toml — trying mDNS discovery…")
    out, _, _ = phone.adb(["mdns", "services"], timeout=10)
    for line in out.splitlines():
        if "_adb-tls-connect" in line or "_adb-tcp-connect" in line:
            addr = line.rsplit(None, 1)[-1].strip(";")
            print(f"discovered {addr}; connecting…")
            out2, err2, _ = phone.adb(["connect", addr], timeout=12)
            print(out2 or err2)
            break
    out, _, rc = phone.adb(["get-state"])
    print(f"state: {out or 'not connected'}")
    return rc


def cmd_call(cfg, number, with_agent):
    phone.ensure_connected(cfg)
    phone.dial(cfg, number)
    print(f"dialing {number}…")
    if not with_agent:
        return 0
    for _ in range(45):
        time.sleep(1)
        if phone.call_state()["state"] == 2:
            break
    else:
        print("call never connected")
        return 1
    agent.run_session(cfg, number=number)
    return 0


def cmd_listen(cfg):
    audio.wait_for_hfp(cfg)
    print("listening — say something…")
    rec = audio.record_utterance(cfg)
    if not rec:
        print("(nothing heard)")
        return 1
    print(f"captured {rec['seconds']:.1f}s; transcribing…")
    text = stt.transcribe(cfg, rec["path"])
    os.unlink(rec["path"])
    print(f"heard: {text!r}")
    return 0


def cmd_talk(cfg):
    """Human-talk mode: your mic → the call, caller → your speakers. Use headphones."""
    audio.wait_for_hfp(cfg)
    mic = subprocess.run(["pactl", "get-default-source"], capture_output=True, text=True).stdout.strip()
    speakers = subprocess.run(["pactl", "get-default-sink"], capture_output=True, text=True).stdout.strip()
    bts, btsrc = audio.bt_sink(cfg), audio.bt_source(cfg)
    print(f"looping: {mic} → {bts}   and   {btsrc} → {speakers}")
    print("WEAR HEADPHONES — speakers + phone mic will echo. Ctrl+C to stop.")
    mods = []
    try:
        for src, sink in ((mic, bts), (btsrc, speakers)):
            p = subprocess.run(["pactl", "load-module", "module-loopback",
                                f"source={src}", f"sink={sink}"], capture_output=True, text=True)
            if p.returncode == 0 and p.stdout.strip().isdigit():
                mods.append(p.stdout.strip())
        print(f"live ({len(mods)} loopbacks). Talking is two-way now.")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for m in mods:
            subprocess.run(["pactl", "unload-module", m], capture_output=True)
        print("loopbacks removed.")


def cmd_ask(cfg, question):
    context, sources = vault_rag.build_context(cfg, question)
    if sources:
        print(f"[context: {len(sources)} files]")
        for s in sources:
            print(f"  - {s}")
    else:
        print("[context: nothing found in vault]")
    reply, provider = llm.chat(cfg, [
        {"role": "system", "content": agent.system_prompt(cfg, context)},
        {"role": "user", "content": question},
    ])
    print(f"[provider: {provider}]")
    print(reply)
    return 0


def main(argv=None):
    load_env_files()
    ap = argparse.ArgumentParser(prog="callscoot",
                                 description="Quartermaster — Bluetooth HFP phone & SMS receptionist (voice via bluez-alsa/PipeWire, control via ADB)")
    ap.add_argument("--config", help="path to callscoot.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="validate every stage, print next steps")
    sub.add_parser("status", help="call state + BT audio nodes")
    p = sub.add_parser("adb-connect", help="connect wireless ADB (config ip or mDNS)")
    p.add_argument("target", nargs="?", help="optional IP:PORT override")
    p = sub.add_parser("call"); p.add_argument("number"); p.add_argument("--agent", action="store_true")
    sub.add_parser("answer")
    sub.add_parser("hangup")
    sub.add_parser("watch", help="auto-answer + receptionist loop")
    sub.add_parser("index", help="snapshot eBay/Square order data into knowledge/")
    sub.add_parser("agent", help="take over the current ringing/active call with the receptionist")
    sub.add_parser("talk", help="human-talk loopbacks (mic→call, caller→speakers)")
    sub.add_parser("listen", help="record one utterance from the phone and transcribe it")
    p = sub.add_parser("ask"); p.add_argument("question", nargs="+")
    p = sub.add_parser("gaming"); p.add_argument("mode", nargs="?", choices=["on", "off", "toggle", "status"],
                                                help="kokoro off for gaming (piper voice), on to restore")
    sub.add_parser("unlock", help="wake + unlock the phone with the saved PIN")
    args = ap.parse_args(argv)
    cfg = load(args.config)

    if args.cmd == "unlock":
        phone.ensure_connected(cfg)
        print("unlocked" if phone.unlock(cfg) else "keyguard state unknown")
        return 0

    if args.cmd == "gaming":
        flag = tts.GAMING_FLAG
        cur = os.path.exists(flag)
        want = args.mode or "toggle"
        if want == "status":
            pass
        else:
            new = (not cur) if want == "toggle" else (want == "on")
            if new and not cur:
                open(flag, "w").close()
                subprocess.Popen(["notify-send", "-a", "callscoot",
                                  "Callscoot gaming mode ON",
                                  "Kokoro unloaded — piper voice, light footprint"])
                cur = True
            elif (not new) and cur:
                os.unlink(flag)
                subprocess.Popen(["notify-send", "-a", "callscoot",
                                  "Callscoot gaming mode OFF",
                                  "Kokoro natural voice restored"])
                cur = False
        state = "piper voice (gaming)" if cur else "kokoro voice (natural)"
        print(f"voice: {state}")
        return 0

    if args.cmd == "doctor":
        return cmd_doctor(cfg)
    if args.cmd == "status":
        cmd_status(cfg)
        return 0
    if args.cmd == "adb-connect":
        return cmd_adb_connect(cfg, target=args.target)
    if args.cmd == "call":
        return cmd_call(cfg, args.number, args.agent)
    if args.cmd == "answer":
        phone.ensure_connected(cfg)
        print(phone.answer(cfg))
        return 0
    if args.cmd == "hangup":
        phone.ensure_connected(cfg)
        phone.hangup()
        print("hung up")
        return 0
    if args.cmd == "watch":
        watch.watch(cfg)
        return 0
    if args.cmd == "index":
        orders_index.refresh(cfg)
        return 0
    if args.cmd == "agent":
        phone.ensure_connected(cfg)
        st = phone.call_state()
        if st["state"] == 0:
            print("no incoming call — place one, or use: callscoot call NUM --agent")
            return 1
        agent.run_session(cfg, number=st["number"] or "unknown")
        return 0
    if args.cmd == "talk":
        cmd_talk(cfg)
        return 0
    if args.cmd == "listen":
        return cmd_listen(cfg)
    if args.cmd == "ask":
        return cmd_ask(cfg, " ".join(args.question))
    return 1


if __name__ == "__main__":
    sys.exit(main())
