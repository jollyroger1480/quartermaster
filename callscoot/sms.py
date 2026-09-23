"""SMS bot: poll the phone's inbox over ADB and reply with the same
vault-grounded brain, sent through Google Messages (unlock -> compose ->
type -> tap send). Only engages real phone numbers; shortcodes/services and
spam scripts are ignored. Cooldowns prevent runaway loops."""
import datetime
import json
import os
import re
import subprocess
import time

from . import notify, phone, vault_rag
from .agent import SPAM_RE
from .config import app_home, dig
from .llm import chat as llm_chat

STATE_FILE = os.path.join(app_home(), ".sms_state")
# Default tap points for the moto g play (720x1600) in Google Messages —
# override in callscoot.toml: [sms] field_xy = [x, y], send_xy = [x, y]
DEFAULT_FIELD_XY = (295, 1425)
DEFAULT_SEND_XY = (657, 944)


def _field_xy(cfg):
    xy = dig(cfg, "sms.field_xy", None) or DEFAULT_FIELD_XY
    return (int(xy[0]), int(xy[1]))


def _send_xy(cfg):
    xy = dig(cfg, "sms.send_xy", None) or DEFAULT_SEND_XY
    return (int(xy[0]), int(xy[1]))


def _adb_shell(args, timeout=30):
    p = subprocess.run(["adb", "shell", *args], capture_output=True, text=True, timeout=timeout)
    return p.stdout


def _load_state():
    try:
        return json.load(open(STATE_FILE))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def _inbox():
    out = _adb_shell(["content", "query", "--uri", "content://sms/inbox",
                      "--projection", "_id,address,body,date"])
    msgs = []
    for line in out.splitlines():
        if not line.startswith("Row:"):
            continue
        row = {}
        for part in line.split(", "):
            if "=" in part:
                k, _, v = part.partition("=")
                row[k.strip()] = v.strip()
        if row.get("_id") and row.get("address") is not None:
            msgs.append(row)
    msgs.sort(key=lambda m: int(m["_id"]))
    return msgs


def _is_mobile_number(addr):
    return addr.startswith("+") and len(re.sub(r"\D", "", addr)) >= 10


def _is_contact(addr):
    """Saved in the phone's contacts = personal relationship, not a customer."""
    out = _adb_shell(["content", "query",
                      "--uri", f"content://com.android.contacts/phone_lookup/{addr}",
                      "--projection", "display_name"], timeout=15)
    return "display_name=" in out


def _system_prompt(cfg, context):
    shop = dig(cfg, "persona.shop", "the shop")
    owner = dig(cfg, "persona.owner", "the owner")
    return f"""You answer SMS texts for {shop} (owned by {owner}).

RULES:
- Plain text, 1-2 short sentences, max 300 characters. No markdown, no emojis, no URLs.
- Answer ONLY from CONTEXT. If it is not in CONTEXT, say {owner} will follow up. Never invent order info, prices, or promises.
- Never share {owner}'s personal info, address, schedule, or legal matters.
- Never book or confirm pickup times yourself — say {owner} will call/text to confirm.
- Junk removal, scrap pickup, e-waste are shop services (see CONTEXT).
- If the text looks like spam/scam, reply exactly: Not interested.
- Default English; reply in simple Spanish if the text is in Spanish.

CONTEXT:
{context or "(nothing on file)"}"""


def _send_sms(cfg, addr, text):
    """Unlock the phone and send via Google Messages UI. Returns True on success."""
    fx, fy = _field_xy(cfg)
    sx, sy = _send_xy(cfg)
    text = re.sub(r"[^\w\s.,!?'-]", "", text).replace("\n", " ").strip()[:300]
    if not text:
        return False
    phone.unlock(cfg)
    _adb_shell(["am", "start", "-a", "android.intent.action.SENDTO",
                "-d", f"smsto:{addr}", "--es", "sms_body", text])
    time.sleep(4)
    _adb_shell(["input", "tap", str(fx), str(fy)])
    time.sleep(1)
    _adb_shell(["input", "text", text.replace(" ", "%s")])
    time.sleep(1)
    _adb_shell(["input", "tap", str(sx), str(sy)])
    time.sleep(3)
    _adb_shell(["input", "keyevent", "4"])  # back out of the thread
    time.sleep(1)
    sent = _adb_shell(["content", "query", "--uri", "content://sms/sent",
                       "--projection", "body"])
    return text[:40] in sent


def poll(cfg, log=print):
    """Check for new inbound texts and reply. Call periodically from the watch loop."""
    if not dig(cfg, "sms.enabled", True):
        return
    state = _load_state()
    msgs = _inbox()
    if not msgs:
        return
    last_id = int(state.get("last_id", 0))
    if "last_id" not in state:  # first run: start from now, don't replay history
        state["last_id"] = int(msgs[-1]["_id"])
        _save_state(state)
        return
    fresh = [m for m in msgs if int(m["_id"]) > last_id]
    if not fresh:
        return
    state["last_id"] = int(msgs[-1]["_id"])
    now = time.time()
    replies = state.get("replies", {})
    for m in fresh:
        addr, body = m.get("address", ""), (m.get("body") or "").strip()
        if not _is_mobile_number(addr) or not body:
            continue
        if not dig(cfg, "sms.reply_to_contacts", False) and _is_contact(addr):
            log(f"sms: {addr} is a saved contact — personal, not replying")
            continue
        history = [t for t in replies.get(addr, []) if now - t < 3600]
        replies[addr] = history
        if len(history) >= dig(cfg, "sms.max_replies_per_hour", 4):
            log(f"sms: reply limit reached for {addr}")
            continue
        try:
            context, _ = vault_rag.build_context(cfg, body)
            messages = [{"role": "system", "content": _system_prompt(cfg, context)},
                        {"role": "user", "content": body}]
            reply, provider = llm_chat(cfg, messages)
        except Exception as e:
            log(f"sms: brain error for {addr}: {e}")
            continue
        if SPAM_RE.search(body):
            reply = "Not interested — please remove this number."
        sent_ok = _send_sms(cfg, addr, reply)
        history.append(now)
        replies[addr] = history
        state["replies"] = replies
        _save_state(state)
        log(f"sms {addr}: {'replied' if sent_ok else 'SEND FAILED'} [{provider}] {reply[:80]}")
        notify.send_call_alert(
            cfg, f"SMS {addr}", [("caller", body), ("agent", reply)], False, "", log=log)
    _save_state(state)
