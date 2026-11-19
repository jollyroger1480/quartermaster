"""Watch mode: poll telephony state, auto-answer allowed callers, hand the call
to the agent session. Also hosts the control surfaces (Telegram listener, local
web GUI) and honors AI-join requests for calls the owner took personally."""
import datetime
import os
import tempfile
import threading
import time
import wave

from . import (agent, audio, errors, gui, leads, notify, orders_index, phone, retention, robocalls,
               sms, stt, tts)
from .config import app_home, dig
from .controls import CallControls, clamp_rings, rings_path, secretary_flag_path


def _stamp():
    return datetime.datetime.now().strftime("%H:%M:%S")


def _log():
    return lambda m: print(f"[{_stamp()}] {m}")


def _maybe_source_lead(cfg, number, transcript):
    """Parts call? Save the part request for the owner (extract → save → alert)
    in the background so the watch loop keeps answering."""
    if not transcript or not dig(cfg, "leads.enabled", True):
        return
    if not leads.looks_like_parts_call(transcript):
        return
    path = ""
    for who, t in transcript:
        if who == "note" and t.startswith("transcript: "):
            path = t[len("transcript: "):]
    print(f"[{_stamp()}] parts call — saving the request in the background")
    threading.Thread(target=leads.process, daemon=True,
                     args=(cfg, number or "unknown", transcript, path, _log())).start()


def _maybe_log_robocall(cfg, number, transcript):
    """Spam-flagged call? Confirm robocall/telemarketer and save an evidence
    record in the background (record only — nothing is filed or sent)."""
    if not transcript or not robocalls.suspected(transcript):
        return
    threading.Thread(target=robocalls.handle, daemon=True,
                     args=(cfg, number or "unknown", transcript, _log())).start()


def calls_today(cfg, today=None):
    """Bot sessions started today, counted from the per-call log folders.
    None = the count could not be read (the caller must treat that as capped)."""
    logdir = os.path.expanduser(dig(cfg, "logs.dir", os.path.join(app_home(), "logs")))
    today = today or datetime.datetime.now().strftime("%Y%m%d")
    try:
        return sum(1 for n in os.listdir(logdir) if n.startswith(f"call_{today}_"))
    except FileNotFoundError:
        return 0  # no log folder yet = no calls logged yet
    except OSError:
        return None


def over_daily_cap(cfg):
    """Spend/loop guard: past call.max_calls_per_day the bot lets calls ring through.
    Fails closed: an unreadable counter counts as over the cap."""
    cap = int(dig(cfg, "call.max_calls_per_day", 40) or 0)
    if cap <= 0:
        return False
    n = calls_today(cfg)
    return n is None or n >= cap


def _retention(cfg, last_day=None):
    """Prune old recordings/transcripts/leads at most once per calendar day.
    Returns the day it ran (or last_day when it did not need to)."""
    today = datetime.date.today().isoformat()
    if today == last_day:
        return last_day
    try:
        retention.prune(cfg, log=lambda m: (print(f"[{_stamp()}] {m}"), errors.info(m, cfg)))
    except Exception as e:
        errors.record("retention", e, cfg)
    return today


def _warmup(cfg):
    """Load TTS + STT models before the first call so the first turn is fast."""
    try:
        wav = tts.synth(cfg, "Ready.")
        os.unlink(wav)
        for line in agent.fixed_lines(cfg):  # pre-render greeting + notice and stock lines
            os.unlink(tts.synth_cached(cfg, line))
        fd, p = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        w = wave.open(p, "wb")
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 3200)
        w.close()
        stt.whisper(cfg, p)  # keep the local fallback loaded even when Grok STT leads
        os.unlink(p)
        print(f"[{_stamp()}] warmed up — TTS and STT models loaded")
        errors.info("warmed up — TTS and STT models loaded", cfg)
    except Exception as e:
        print(f"[{_stamp()}] warmup skipped: {e}")
        errors.record("warmup", e, cfg)


HEADSET_RETRY = 300.0
_headset = {"next": 0.0, "busy": False, "down": False}


def _headset_ready(cfg):
    if audio.backend(cfg) == "bluealsa":
        return audio.bluealsa_ready(cfg)
    return bool(audio.bt_source(cfg))


def headset_ok(cfg):
    """False only when we can tell the call would have no sound."""
    try:
        return bool(_headset_ready(cfg))
    except Exception:
        return True


def _headset_reconnect(cfg):
    try:
        ok = phone.bt_connect(cfg)
        print(f"[{_stamp()}] Bluetooth reconnect {'worked' if ok else 'failed, will retry'}")
        if ok:
            errors.info("Bluetooth to the phone is back", cfg)
    finally:
        _headset["busy"] = False


def keep_headset(cfg, now=None, wait=False):
    """Calls have no sound without Bluetooth. When it drops (phone left and came
    back), reconnect from this PC instead of waiting for someone to do it."""
    now = time.monotonic() if now is None else now
    if _headset["busy"] or (now < _headset["next"] and not wait):
        return
    _headset["next"] = now + HEADSET_RETRY
    try:
        if _headset_ready(cfg):
            _headset["down"] = False
            return
    except Exception:
        return
    if not _headset["down"]:
        print(f"[{_stamp()}] Bluetooth to the phone is down; reconnecting")
        _headset["down"] = True
    _headset["busy"] = True
    if wait:
        _headset_reconnect(cfg)
    else:
        threading.Thread(target=_headset_reconnect, args=(cfg,), daemon=True).start()


def watch(cfg):
    errors.setup(cfg)
    answer_unknown = bool(dig(cfg, "phone.answer_unknown", True))
    refresh_s = float(dig(cfg, "orders.refresh_minutes", 15)) * 60
    controls = CallControls()
    controls.ring_seconds = max(0.5, float(dig(cfg, "call.ring_delay_s", 3.0)))
    controls.load_secretary(secretary_flag_path(cfg))
    controls.load_rings(rings_path(cfg), clamp_rings(dig(cfg, "call.rings", 1)))
    if dig(cfg, "alerts.telegram_control", False):
        # opt-in: callscoot must NOT long-poll a bot the Quartermaster
        # secretary is already polling — one getUpdates poller per bot, or 409s
        notify.start_control_listener(cfg, controls, log=lambda m: print(f"[{_stamp()}] {m}"))
    else:
        print(f"[{_stamp()}] telegram control listener off (shared with secretary)")
    gui.start_server(cfg, controls, log=lambda m: print(f"[{_stamp()}] {m}"))
    try:
        phone.ensure_connected(cfg)
    except phone.PhoneError as e:
        print(f"[{_stamp()}] [ phone link down ({e}); reconnecting…")
        errors.record("adb", e, cfg)
    who = "everyone" if answer_unknown else "allowlist only"
    sec = "on" if controls.secretary else "off"
    print(f"[{_stamp()}] watching — secretary {sec}, auto-answer {who}. Ctrl+C to stop.")
    errors.info(f"watching — secretary {sec}, auto-answer {who}", cfg)
    _warmup(cfg)
    pruned_day = _retention(cfg)
    prev = 0
    last_index = 0.0
    sms_poll = type("S", (), {"tick": None})()
    while True:
        try:
            st = phone.call_state()
        except phone.PhoneError as e:
            print(f"[{_stamp()}] [ phone link down ({e}); reconnecting…")
            errors.record("adb", e, cfg)
            try:
                phone.ensure_connected(cfg)
            except Exception as reconnect_err:
                errors.record("adb-reconnect", reconnect_err, cfg)
            prev = 0
            time.sleep(3)
            continue
        s, num = st["state"], st["number"]
        if s == 0:
            keep_headset(cfg)
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
            transcript = None
            try:
                transcript, _flagged = agent.run_session(cfg, number=num or "unknown", controls=controls,
                                  join_live=True, speak=speak)
            except Exception as e:
                print(f"[{_stamp()}] [ join-session error: {e}")
                errors.record("join-session", e, cfg)
            _maybe_source_lead(cfg, num or "unknown", transcript)
            _maybe_log_robocall(cfg, num or "unknown", transcript)
            prev = phone.call_state()["state"]
            print("[callscoot] idle again")
            continue
        if s == 1 and prev != 1:
            label = errors.mask_numbers(num) if num else "unknown number"
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
                # caller ID often lands a poll or two after the ring; cap the wait
                # so a phone that never sends it still gets handled as an unknown caller
                id_deadline = time.monotonic() + 6.0
                stopped = False
                while time.monotonic() < id_deadline:
                    time.sleep(1)
                    st_now = phone.call_state()
                    if st_now["state"] != 1:
                        print("[callscoot]   caller hung up before answer")
                        prev = st_now["state"]
                        stopped = True
                        break
                    if st_now["number"]:
                        num = st_now["number"]
                        break
                if stopped:
                    continue
            if phone.last10(num) in blacklist:
                print(f"[{_stamp()}] blacklisted — not answering")
                prev = s
                time.sleep(1)
                continue
            if over_daily_cap(cfg):
                print(f"[{_stamp()}] daily call cap reached — not answering")
                errors.info("daily call cap reached — not answering", cfg)
                prev = s
                time.sleep(1)
                continue
            allowed = phone.allowlisted(cfg, num) or answer_unknown
            if not allowed:
                print("[callscoot]   not allowlisted — ignoring")
                prev = s
                time.sleep(1)
                continue
            keep_headset(cfg, wait=True)  # no Bluetooth means a silent call
            if not headset_ok(cfg):
                print(f"[{_stamp()}] no Bluetooth to the phone. Not answering; the call goes to voicemail.")
                errors.info(f"MISSED {label}: no Bluetooth to the phone, left for voicemail", cfg)
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
            transcript = None
            try:
                transcript, _flagged = agent.run_session(cfg, number=num or "unknown",
                                                         controls=controls)
            except Exception as e:
                print(f"[{_stamp()}] [ session error: {e}")
                errors.record("call-session", e, cfg)
                try:
                    phone.hangup()
                except Exception as hangup_err:
                    print(f"[{_stamp()}] error hanging up: {hangup_err}")
                    errors.record("hangup", hangup_err, cfg)
            _maybe_source_lead(cfg, num or "unknown", transcript)
            _maybe_log_robocall(cfg, num or "unknown", transcript)
            prev = phone.call_state()["state"]
            print("[callscoot] idle again")
            continue
        prev = s
        if s == 0:
            pruned_day = _retention(cfg, pruned_day)
        sms_tick = (int(time.monotonic() * 1000) // max(1, int(dig(cfg, "sms.poll_seconds", 5) * 1000)))
        if (dig(cfg, "sms.enabled", True) and sms_tick != getattr(sms_poll, "tick", None)
                and s == 0 and phone.adb_up()):
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
