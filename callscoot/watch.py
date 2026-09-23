"""Watch mode: poll telephony state, auto-answer allowed callers, hand the call
to the agent session."""
import datetime
import os
import tempfile
import time
import wave

from . import agent, audio, orders_index, phone, sms, stt, tts
from .config import dig


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
    except Exception as e:
        print(f"[{_stamp()}] warmup skipped: {e}")


def watch(cfg):
    phone.ensure_connected(cfg)
    answer_unknown = bool(dig(cfg, "phone.answer_unknown", True))
    ring_delay = float(dig(cfg, "call.ring_delay_s", 3.0))
    refresh_s = float(dig(cfg, "orders.refresh_minutes", 15)) * 60
    print(f"[{_stamp()}] watching — auto-answer {'everyone' if answer_unknown else 'allowlist only'}. Ctrl+C to stop.")
    _warmup(cfg)
    prev = 0
    last_index = 0.0
    sms_poll = type("S", (), {"tick": None})()
    while True:
        try:
            st = phone.call_state()
        except phone.PhoneError as e:
            print(f"[{_stamp()}] [ ADB lost ({e}); reconnecting…")
            try:
                phone.ensure_connected(cfg)
            except Exception:
                pass
            prev = 0
            time.sleep(3)
            continue
        s, num = st["state"], st["number"]
        if s == 0 and time.monotonic() - last_index > refresh_s:
            last_index = time.monotonic()
            try:
                orders_index.refresh(cfg)
            except Exception as e:
                print(f"[{_stamp()}] [ index error: {e}")
        if s == 1 and prev != 1:
            label = num or "unknown number"
            print(f"[{_stamp()}] RINGING {label}")
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
            time.sleep(ring_delay)
            st2 = phone.call_state()
            if st2["state"] != 1:
                print("[callscoot]   caller hung up before answer")
                prev = st2["state"]
                continue
            try:
                agent.run_session(cfg, number=num or "unknown")
            except Exception as e:
                print(f"[{_stamp()}] [ session error: {e}")
                try:
                    phone.hangup()
                except Exception as e:
                    print(f"[{_stamp()}] error hanging up: {e}")
            prev = phone.call_state()["state"]
            print("[callscoot] idle again")
            continue
        prev = s
        sms_tick = (int(time.monotonic() * 1000) // max(1, int(dig(cfg, "sms.poll_seconds", 5) * 1000)))
        if dig(cfg, "sms.enabled", True) and sms_tick != getattr(sms_poll, "tick", None) and s == 0:
            sms_poll.tick = sms_tick
            try:
                sms.poll(cfg, log=lambda m: print(f"[{_stamp()}] {m}"))
            except Exception as e:
                print(f"[{_stamp()}] sms poll error: {e}")
        if tts.gaming_on():  # gaming mode: drop the resident kokoro model
            if tts.unload_kokoro():
                print(f"[{_stamp()}] gaming mode — kokoro unloaded, voice on piper")
        time.sleep(1.0)
