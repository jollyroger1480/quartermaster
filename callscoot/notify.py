"""Call and SMS alerts via Telegram sendMessage.

Token is TELEGRAM_BOT_TOKEN. Destination is alerts.telegram_chat, or
TELEGRAM_HOME_CHANNEL when that setting is empty. "chat_id:topic_id" posts
into a forum topic. A failed send is logged and never kills the call; the
transcript is already on disk.

The control listener (start_control_listener) long-polls getUpdates and turns
inline-button presses plus plain text messages from the home chat into
commands on the shared CallControls bus — record, AI take over/drop, hang up,
and text-to-caller.
"""
import json
import os
import re
import subprocess
import threading
import urllib.parse
import urllib.request

from .config import dig


def _token():
    return os.environ.get("TELEGRAM_BOT_TOKEN", "")


def _home_chat(cfg):
    chat = (dig(cfg, "alerts.telegram_chat", "")
            or os.environ.get("TELEGRAM_HOME_CHANNEL", ""))
    return chat.partition(":")[0]  # bare chat id; threads only matter for sending


# Alerts go out with no parse_mode (plain text), so any Telegram markup a
# model writes would show as literal symbols. Strip only paired emphasis and
# heading/bullet markers; single *, _ or ' inside names and addresses stay.
_MD_PAIRS = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_MD_LINE_LEAD = re.compile(r"^[ \t]*(#{1,6}[ \t]+|[*\-\u2022][ \t]+)", re.M)
_SUMMARY_TITLE = re.compile(r"^[^\n:]{0,40}\bsummary[^\n]{0,3}:\s*\n", re.I)


def plain_text(text):
    """Model text made safe for a plain-text Telegram message."""
    t = _MD_PAIRS.sub(r"\2", text or "")
    t = _MD_LINE_LEAD.sub("", t)
    t = re.sub(r"[ \t]+$", "", t, flags=re.M)  # markdown hard-break spaces
    return _SUMMARY_TITLE.sub("", t.strip(), count=1)


def telegram_send(cfg, text, reply_markup=None):
    token = _token()
    chat = (dig(cfg, "alerts.telegram_chat", "")
            or os.environ.get("TELEGRAM_HOME_CHANNEL", ""))
    if not token or not chat:
        return False, "telegram not configured"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # composite "chat_id:thread_id" targets a forum topic inside a group
    chat_id, _, thread = chat.partition(":")
    payload = {"chat_id": chat_id, "text": text[:3500]}
    if thread:
        payload["message_thread_id"] = int(thread)
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    data = urllib.parse.urlencode(payload).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10) as r:
            return r.status == 200, f"telegram http {r.status}"
    except Exception as e:
        from . import errors
        errors.record("telegram", e, cfg)
        return False, f"telegram send failed: {e}"


CALL_KEYBOARD = {
    "inline_keyboard": [[
        {"text": "⏺ Record", "callback_data": "rec_toggle"},
        {"text": "🤖 AI on/off", "callback_data": "ai_toggle"},
        {"text": "⏹ Hang up", "callback_data": "hangup"},
    ]],
}


def alert_label(cfg, flagged, reasons=None):
    """Header tag for a flagged alert. A caller who only asked for the owner
    gets a calm "asked for <owner>"; any other reason (or a flag with no
    reason given) is an escalation, and escalation wins when both apply."""
    if not flagged:
        return ""
    reasons = set(reasons or ())
    if reasons == {"asked_for_person"}:
        return f" — asked for {dig(cfg, 'persona.owner', 'the owner')}"
    return " ⚠️ ESCALATION"


def send_call_alert(cfg, number, transcript, flagged, transcript_path, log=print,
                    reply_markup=None, summary="", reasons=None):
    if not dig(cfg, "alerts.telegram", True):
        return
    if not transcript_path:
        transcript_path = "(none)"
    caller_lines = [t for w, t in transcript if w == "caller"]
    gist = plain_text(summary) or (
        " | ".join(caller_lines)[:600] or "(no speech captured)")
    owner = dig(cfg, "persona.owner", "the owner")
    text = (
        f"☎️ CALL{alert_label(cfg, flagged, reasons)} — {number}\n"
        f"{gist}\n"
        f"— taken by the shop assistant for {owner}\n"
        f"transcript: {transcript_path}"
    )
    ok, note = telegram_send(cfg, text, reply_markup=reply_markup)
    log(f"{'alert sent' if ok else 'alert skipped'}: {note}")


def lead_card_text(rec, digest_path=""):
    """Part-request card for the owner: vehicle, parts, callback, lead file."""
    parts = ", ".join(
        (p.get("name") or "") + (f" (PN {p['part_number']})" if p.get("part_number") else "")
        for p in rec.get("parts") or [])
    veh_bits = []
    for v in rec.get("vehicles") or []:
        veh_bits.append(" ".join(str(v.get(k) or "") for k in
                                 ("year", "make", "model", "engine")).strip())
    veh = " / ".join(b for b in veh_bits if b) or "vehicle not captured"
    who = rec.get("caller_name") or "name not captured"
    lines = [f"🧲 NEW PART REQUEST — {rec.get('number', '?')}",
             f"{who} — {veh} — {parts}"]
    if rec.get("notes"):
        lines.append(f"notes: {rec['notes'][:200]}")
    if rec.get("callback"):
        lines.append(f"callback: {rec['callback']}")
    lines.append(f"lead file: {digest_path}")
    return "\n".join(lines)[:3400]


def send_lead_alert(cfg, rec, digest_path="", log=print):
    """New part request → Telegram + desktop pop-up."""
    text = lead_card_text(rec, digest_path)
    ok, note = telegram_send(cfg, text)
    log(f"{'lead alert sent' if ok else 'lead alert skipped'}: {note}")
    try:
        head = text.splitlines()[0] if text else "new part request"
        body = text.split("\n", 1)[1][:180] if "\n" in text else ""
        subprocess.Popen(["notify-send", "-a", "callscoot", "-t", "0", head, body])
    except OSError:
        pass
    return ok


def send_live_call_card(cfg, number, log=print):
    """Short live-call card carrying the control buttons (screening/mid-call)."""
    text = f"☎️ LIVE CALL — {number}\ncontrols:"
    ok, note = telegram_send(cfg, text, reply_markup=CALL_KEYBOARD)
    log(f"{'control card sent' if ok else 'control card skipped'}: {note}")
    return ok


def _api(token, method, payload, timeout=40):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(payload).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=timeout) as r:
        return json.loads(r.read())


def _handle_update(cfg, upd, controls, log):
    """Route one getUpdates update into the controls bus."""
    msg = upd.get("message") or upd.get("edited_message") or {}
    cbq = upd.get("callback_query")
    home = _home_chat(cfg)
    if cbq:
        chat = str((cbq.get("message") or {}).get("chat", {}).get("id", ""))
        if chat != home:
            return
        data = cbq.get("data", "")
        if data == "rec_toggle":
            note = controls.toggle_record()
        elif data == "ai_toggle":
            note = controls.toggle_ai()
        elif data == "hangup":
            controls.post("hangup")
            note = "hanging up"
        else:
            return
        try:
            _api(_token(), "answerCallbackQuery",
                 {"callback_query_id": cbq.get("id"), "text": note}, timeout=8)
        except Exception:
            pass
        log(f"control: {note}")
        controls.note_event(note)
        return
    chat = str(msg.get("chat", {}).get("id", ""))
    text = (msg.get("text") or "").strip()
    if chat != home or not text:
        return
    if text.startswith("/"):
        cmd = text.split()[0].lstrip("/").lower()
        if cmd in ("record", "rec"):
            note = controls.toggle_record()
        elif cmd in ("ai", "takeover"):
            note = controls.toggle_ai()
        elif cmd in ("hangup", "end"):
            controls.post("hangup")
            note = "hanging up"
        else:
            return
        log(f"control: {note}")
        controls.note_event(note)
        return
    # plain text from the owner during a call → spoken to the caller verbatim
    if controls.snapshot()["on_call"]:
        controls.post("say_to_caller", text)
        controls.note_event(f"text→caller: {text[:60]}")
        log(f"control: say to caller: {text[:60]}")


def start_control_listener(cfg, controls, log=print):
    """Background long-poll thread. Silently does nothing when Telegram is
    unconfigured; errors are recorded and retried — a dead poller never
    affects a live call."""
    if not _token() or not _home_chat(cfg):
        log("telegram control listener off (unconfigured)")
        return None

    def _loop():
        offset = 0
        while True:
            try:
                res = _api(_token(), "getUpdates",
                           {"offset": offset, "timeout": 25, "allowed_updates":
                            json.dumps(["message", "callback_query", "edited_message"])},
                           timeout=40)
                for upd in res.get("result", []):
                    offset = upd["update_id"] + 1
                    try:
                        _handle_update(cfg, upd, controls, log)
                    except Exception as e:
                        from . import errors
                        errors.record("control-update", e, cfg)
            except Exception as e:
                from . import errors
                errors.record("control-listener", e, cfg)
                import time as _t
                _t.sleep(5)

    t = threading.Thread(target=_loop, name="tg-control", daemon=True)
    t.start()
    log("telegram control listener on (buttons + text-to-caller)")
    return t
