"""Watch mode: poll telephony state, auto-answer allowed callers, hand the call
to the agent session. Also hosts the control surfaces (Telegram listener, local
web GUI) and honors AI-join requests for calls the Cap'n took himself."""
import datetime
import os
import tempfile
import time
import wave

from . import agent, audio, errors, gui, notify, orders_index, phone, sms, stt, tts
from .config import dig
from .controls import CallControls, clamp_rings, rings_path, secretary_flag_path


def _stamp():
    return datetime.datetime.now().strftime("%H:%M:%S")


def _warmup(cfg):
    """Load TTS + STT models before the first call so the first turn is fast."""
    try:
        wav = tts.synth(cfg, "Ready.")
        os.unlink(wav)
        fd, p = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        w = wave.open(p, "wb")
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 3200)
        w.close()
        stt.transcribe(cfg, p)
        os.unlink(p)
        print(f"[{_stamp()}] warmed up — TTS and STT models loaded")
        errors.info("warmed up — TTS and STT models loaded", cfg)
    except Exception as e:
        print(f"[{_stamp()}] warmup skipped: {e}")
        errors.record("warmup", e, cfg)


def watch(cfg):
    errors.setup(cfg)
    answer_unknown = bool(dig(cfg, "phone.answer_unknown", True))
    refresh_s = float(dig(cfg, "orders.refresh_minutes", 15)) * 60
    controls = CallControls()
    controls.ring_seconds = max(0.5, float(dig(cfg, "call.ring_delay_s", 3.0)))
    controls.load_secretary(secretary_flag_path(cfg))
    controls.load_rings(rings_path(cfg), clamp_rings(dig(cfg, "call.rings", 1)))
    notify.start_control_listener(cfg, controls, log=lambda m: print(f"[{_stamp()}] {m}"))
    gui.start_server(cfg, controls, log=lambda m: print(f"[{_stamp()}] {m}"))
    try:
        phone.ensure_connected(cfg)
    except phone.PhoneError as e:
        print(f"[{_stamp()}] [ ADB lost ({e}); reconnecting…")
        errors.record("adb", e, cfg)
    who = "everyone" if answer_unknown else "allowlist only"
    sec = "on" if controls.secretary else "off"
    print(f"[{_stamp()}] watching — secretary {sec}, auto-answer {who}. Ctrl+C to stop.")
    errors.info(f"watching — secretary {sec}, auto-answer {who}", cfg)
    _warmup(cfg)
    prev = 0
    last_index = 0.0
    sms_poll = type("S", (), {"tick": None})()
    while True:
        try:
            st = phone.call_state()
        except phone.PhoneError as e:
            print(f"[{_stamp()}] [ ADB lost ({e}); reconnecting…")
            errors.record("adb", e, cfg)
            try:
                phone.ensure_connected(cfg)
            except Exception as reconnect_err:
                errors.record("adb-reconnect", reconnect_err, cfg)
            prev = 0
            time.sleep(3)
            continue
        s, num = st["state"], st["number"]
        if s != prev:
            errors.debug(f"phone state {prev} -> {s}", cfg)
        if s == 0 and time.monotonic() - last_index > refresh_s:
            last_index = time.monotonic()
            try:
                orders_index.refresh(cfg)
            except Exception as e:
                print(f"[{_stamp()}] [ index error: {e}")
                errors.record("orders-index", e, cfg)
        # Manual panel: record stays silent; AI is the only path that talks.
        if s == 2 and not controls.on_call and (
            controls.pending_join or controls.pending_record
        ):
            speak = bool(controls.pending_join and controls.secretary)
            if controls.pending_join and not controls.secretary and not controls.pending_record:
                controls.pending_join = False
                errors.info("AI join ignored. Secretary is off.", cfg)
                prev = s
                continue
            controls.pending_join = False
            controls.pending_record = False
            print(f"[{_stamp()}] {'AI' if speak else 'record-only'} join for live call {num or 'unknown'}")
            notify.send_live_call_card(cfg, num or "unknown",
                                       log=lambda m: print(f"[{_stamp()}] {m}"))
            try:
                agent.run_session(cfg, number=num or "unknown", controls=controls,
                                  join_live=True, speak=speak)
            except Exception as e:
                print(f"[{_stamp()}] [ join-session error: {e}")
                errors.record("join-session", e, cfg)
            prev = phone.call_state()["state"]
            print("[callscoot] idle again")
            continue
        if s == 1 and prev != 1:
            label = num or "unknown number"
            if not controls.secretary:
                print(f"[{_stamp()}] RINGING {label}. Secretary is off, not answering.")
                errors.info(f"RINGING {label}. Secretary is off, not answering.", cfg)
                prev = s
                time.sleep(1)
                continue
            print(f"[{_stamp()}] RINGING {label}")
            errors.info(f"RINGING {label}", cfg)
            blacklist = {phone.last10(b) for b in (dig(cfg, "spam.blacklist", []) or []) if b}
            if not num:
                # caller ID often lands a poll or two after the ring
                prev = s
                time.sleep(1)
                continue
            if phone.last10(num) in blacklist:
                print(f"[{_stamp()}] blacklisted — not answering")
                prev = s
                time.sleep(1)
                continue
            allowed = phone.allowlisted(cfg, num) or answer_unknown
            if not allowed:
                print("[callscoot]   not allowlisted — ignoring")
                prev = s
                time.sleep(1)
                continue
            audio.set_hfp_profile(cfg)  # pre-arm the audio path while it rings
            per = max(0.5, float(controls.ring_seconds))
            deadline = time.monotonic() + (controls.rings * per)
            stopped = False
            while time.monotonic() < deadline:
                if not controls.secretary:
                    print(f"[{_stamp()}] secretary turned off while ringing. Not answering.")
                    errors.info("secretary turned off while ringing. Not answering.", cfg)
                    prev = s
                    stopped = True
                    break
                time.sleep(0.4)
                st2 = phone.call_state()
                if st2["state"] != 1:
                    print("[callscoot]   caller hung up before answer")
                    prev = st2["state"]
                    stopped = True
                    break
            if stopped:
                continue
            st2 = phone.call_state()
            if st2["state"] != 1:
                print("[callscoot]   caller hung up before answer")
                prev = st2["state"]
                continue
            notify.send_live_call_card(cfg, num or "unknown",
                                       log=lambda m: print(f"[{_stamp()}] {m}"))
            try:
                agent.run_session(cfg, number=num or "unknown", controls=controls)
            except Exception as e:
                print(f"[{_stamp()}] [ session error: {e}")
                errors.record("call-session", e, cfg)
                try:
                    phone.hangup()
                except Exception as hangup_err:
                    print(f"[{_stamp()}] error hanging up: {hangup_err}")
                    errors.record("hangup", hangup_err, cfg)
            prev = phone.call_state()["state"]
            print("[callscoot] idle again")
            continue
        prev = s
        sms_tick = (int(time.monotonic() * 1000) // max(1, int(dig(cfg, "sms.poll_seconds", 5) * 1000)))
        if dig(cfg, "sms.enabled", True) and sms_tick != getattr(sms_poll, "tick", None) and s == 0:
            sms_poll.tick = sms_tick
            try:
                sms.poll(cfg, log=lambda m: (print(f"[{_stamp()}] {m}"), errors.info(m, cfg)))
            except Exception as e:
                print(f"[{_stamp()}] sms poll error: {e}")
                errors.record("sms-poll", e, cfg)
        if tts.gaming_on():  # gaming mode: drop the resident kokoro model
            if tts.unload_kokoro():
                print(f"[{_stamp()}] gaming mode — kokoro unloaded, voice on piper")
        time.sleep(1.0)
