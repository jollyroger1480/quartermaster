"""Call and SMS alerts via Telegram sendMessage.

Token is TELEGRAM_BOT_TOKEN. Destination is alerts.telegram_chat, or
TELEGRAM_HOME_CHANNEL when that setting is empty. "chat_id:topic_id" posts
into a forum topic. A failed send is logged and never kills the call; the
transcript is already on disk.
"""
import os
import urllib.parse
import urllib.request

from .config import dig


def telegram_send(cfg, text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
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
    data = urllib.parse.urlencode(payload).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10) as r:
            return r.status == 200, f"telegram http {r.status}"
    except Exception as e:
        from . import errors
        errors.record("telegram", e, cfg)
        return False, f"telegram send failed: {e}"


def send_call_alert(cfg, number, transcript, flagged, transcript_path, log=print):
    if not dig(cfg, "alerts.telegram", True):
        return
    if not transcript_path:
        transcript_path = "(none)"
    caller_lines = [t for w, t in transcript if w == "caller"]
    gist = " | ".join(caller_lines)[:600] or "(no speech captured)"
    owner = dig(cfg, "persona.owner", "the owner")
    text = (
        f"☎️ CALL{' ⚠️ ESCALATION' if flagged else ''} — {number}\n"
        f"{gist}\n"
        f"— taken by the shop assistant for {owner}\n"
        f"transcript: {transcript_path}"
    )
    ok, note = telegram_send(cfg, text)
    log(f"{'alert sent' if ok else 'alert skipped'}: {note}")
