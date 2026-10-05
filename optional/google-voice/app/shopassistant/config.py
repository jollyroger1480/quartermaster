"""Settings, secrets and paths.

Everything the owner can change is in data\\settings.json (written by the
control panel); anything missing falls back to DEFAULTS below. The AI key is
kept apart in data\\secrets.json. Nothing here is specific to one business.
"""
import copy
import json
import os
import threading

from . import brand

_LOCK = threading.Lock()

DEFAULTS = {
    "persona": {
        "shop": "",                    # business name
        "owner": "",                   # owner's first name
        "name": "",                    # how the assistant names itself; blank = "<owner>'s automated assistant"
        "about": "",                   # one or two sentences on what the business does
        "shop_hours": "",              # blank = the assistant never states hours
        "business_address": "",        # blank = the assistant never states an address
        "website": "",
        "website_spoken": "",          # how to say it on a call; blank = worked out from website
        "contact_email": "",
        "contact_email_spoken": "",
        "alt_text_number": "",         # a second number that takes texts
        "alt_text_number_spoken": "",
        "reply_by": "text",            # how the owner gets back to callers: text | call | either
        "owner_takes_calls": False,
        "collect_details": "",         # what to collect before handing off, e.g. "year, make, model and engine"
        "system_extra": "",            # extra rules, added to every prompt
    },
    "call": {
        "ring_delay_s": 8, "answer_delay_ms": 700, "pre_greet_listen_s": 0.3, "nudge_after_s": 7,
        "turn_grace_s": 0.5, "turn_grace_long_s": 1.4, "ring_gap_s": 2.0,
        "max_duration_s": 600, "max_turns": 30, "thinking_cue": True,
        "greeting": "", "farewell": "", "voicemail_reply": "", "spam_reply": "", "no_audio_reply": "",
    },
    "audio": {"record_silence_s": 1.1, "record_max_s": 15, "record_min_s": 0.35, "vad_min_rms": 150,
              "barge_in": True, "barge_factor": 1.8, "barge_ms": 500, "monitor": False},
    "stt": {"model": "base.en", "device": "cpu", "compute_type": "int8", "beam_size": 3,
            "language": "en", "cpu_threads": 0, "initial_prompt": ""},
    "tts": {"backend": "kokoro", "kokoro_voice": "af_heart", "kokoro_lang": "a", "speed": 1.0},
    "llm": {
        "max_tokens": 1024, "temperature": 0.3,
        "providers": [
            {"name": "groq", "base_url": "https://api.groq.com/openai/v1", "model": "openai/gpt-oss-120b",
             "api_key_env": "GROQ_API_KEY", "extra_body": {"reasoning_effort": "low", "include_reasoning": False}},
            {"name": "groq-backup", "base_url": "https://api.groq.com/openai/v1", "model": "openai/gpt-oss-20b",
             "api_key_env": "GROQ_API_KEY", "extra_body": {"reasoning_effort": "low", "include_reasoning": False}},
        ],
    },
    "knowledge": {"top_k": 6, "chunk_chars": 700},
    "phone": {"answer_unknown": True, "answer_contacts": True, "allowlist": []},
    "spam": {"blacklist": [], "hangup_on_scam": True},
    "sms": {"enabled": False, "poll_seconds": 15, "reply_delay_s": 60, "followup_delay_s": 15,
            "max_replies_per_hour": 8, "reply_to_contacts": False, "allow_contacts": [], "dry_run": False,
            "reasoning_effort": "medium"},
    "browser": {"url": "https://voice.google.com/u/0/messages", "match": "voice.google.com",
                "cdp_port": 9222, "edge_path": "", "profile_dir": "", "extra_args": []},
    "gv": {},
    "service": {"keep_log_days": 14, "heartbeat_stale_s": 900, "restart_daily_at": ""},
    "panel": {"port": 8765},
}


# ------------------------------------------------------------------- paths
def app_dir():
    """The folder that holds the shopassistant package (…\\app)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def root_dir():
    return os.environ.get("SHOPASSISTANT_HOME") or os.path.dirname(app_dir())


def data_dir():
    d = os.environ.get("SHOPASSISTANT_DATA") or os.path.join(root_dir(), "data")
    os.makedirs(d, exist_ok=True)
    return d


def logs_dir(cfg=None):
    d = os.path.join(data_dir(), "logs")
    os.makedirs(d, exist_ok=True)
    return d


def knowledge_dir(cfg=None):
    d = os.path.join(data_dir(), "knowledge")
    os.makedirs(d, exist_ok=True)
    return d


def settings_path():
    return os.path.join(data_dir(), "settings.json")


def secrets_path():
    return os.path.join(data_dir(), "secrets.json")


def state_path():
    return os.path.join(data_dir(), "state.json")


# ---------------------------------------------------------------- settings
def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _read_json(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def overrides():
    return _read_json(settings_path())


def load(_explicit=None):
    """DEFAULTS + the owner's saved settings; also puts the AI key in the environment."""
    load_secrets()
    return _merge(DEFAULTS, overrides())


def save(changes):
    """Merge `changes` ({section: {key: value}}) into settings.json."""
    with _LOCK:
        cur = overrides()
        cur = _merge(cur, changes)
        _write_json(settings_path(), cur)
    return load()


def dig(cfg, dotted, default=None):
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return default if cur is None else cur


# ----------------------------------------------------------------- secrets
def secrets():
    return _read_json(secrets_path())


def load_secrets():
    for k, v in secrets().items():
        if isinstance(v, str) and v:
            os.environ[k] = v


def save_secret(name, value):
    with _LOCK:
        s = secrets()
        if value:
            s[name] = value.strip()
        else:
            s.pop(name, None)
            os.environ.pop(name, None)
        _write_json(secrets_path(), s)
    load_secrets()


# ------------------------------------------------------------------- state
def state():
    return _read_json(state_path())


def set_state(**kw):
    with _LOCK:
        s = state()
        s.update(kw)
        try:
            _write_json(state_path(), s)
        except OSError:
            pass
    return s


# ------------------------------------------------------- derived persona text
def assistant_name(cfg):
    owner = dig(cfg, "persona.owner", "")
    return dig(cfg, "persona.name", "") or (f"{owner}'s automated assistant" if owner else "the automated assistant")


def shop_name(cfg):
    return dig(cfg, "persona.shop", "") or "the shop"


def owner_name(cfg):
    return dig(cfg, "persona.owner", "") or "the owner"


def spoken_email(cfg):
    e = dig(cfg, "persona.contact_email", "")
    return dig(cfg, "persona.contact_email_spoken", "") or (e.replace("@", " at ").replace(".", " dot ") if e else "")


def spoken_website(cfg):
    w = dig(cfg, "persona.website", "")
    if dig(cfg, "persona.website_spoken", ""):
        return dig(cfg, "persona.website_spoken", "")
    w = w.replace("https://", "").replace("http://", "").replace("www.", "").strip("/")
    return w.replace(".", " dot ").replace("/", " slash ") if w else ""


_DIGITS = {"0": "oh", "1": "one", "2": "two", "3": "three", "4": "four", "5": "five",
           "6": "six", "7": "seven", "8": "eight", "9": "nine"}


def spoken_number(cfg):
    n = dig(cfg, "persona.alt_text_number", "")
    if dig(cfg, "persona.alt_text_number_spoken", ""):
        return dig(cfg, "persona.alt_text_number_spoken", "")
    d = "".join(c for c in n if c.isdigit())
    if len(d) == 11 and d[0] == "1":
        d = d[1:]
    if len(d) != 10:
        return " ".join(_DIGITS[c] for c in d)
    say = lambda part: " ".join(_DIGITS[c] for c in part)
    return f"{say(d[:3])}, {say(d[3:6])}, {say(d[6:])}"


def line(cfg, key):
    """Spoken lines for a call; blank settings fall back to wording built from the business details."""
    custom = dig(cfg, f"call.{key}", "")
    if custom:
        return custom
    shop, owner, name = shop_name(cfg), owner_name(cfg), assistant_name(cfg)
    reply = {"text": f"{owner} will text you back", "call": f"{owner} will call you back"}.get(
        dig(cfg, "persona.reply_by", "text"), f"{owner} will get back to you")
    return {
        "greeting": f"Thanks for calling {shop}, this is {name}. I can answer questions or take a message. "
                    f"What can I help you with?",
        "farewell": "Thanks for calling, have a good one. Goodbye!",
        "voicemail_reply": f"I'll make sure {owner} gets your message. Goodbye for now.",
        "spam_reply": "Not interested, please remove this number from your list. Goodbye.",
        "no_audio_reply": f"Sorry, I'm having trouble hearing you. Please send a text to this number and {reply}.",
    }[key]
