"""Part requests: call transcript → structured request → saved for the owner.

Capture only: the receptionist writes down what the caller wants (year, make,
model, part, part number if said) and a callback number. After the call this
module saves it to leads/<id>.json and leads/open-leads.md (`callscoot leads`)
and notifies the owner. No sourcing, no texts to the caller, no promises.

Everything is best-effort: a failure is logged, never fatal to the call.
"""
import datetime
import json
import os
import re
import threading

from .config import app_home, dig

# Fast transcript gate before we spend an LLM call on extraction.
PARTS_RE = re.compile(
    r"\bparts?\b|engine|motor|transmission|trans\b|alternat|starter|"
    r"radiator|condenser|compressor|caliper|rotor|brake\b|hub\b|axle|"
    r"differential|rear\s?end\b|drive\s?shaft|transfer\s?case|gearbox|"
    r"catalytic|converter|muffler|exhaust|intake|turbo|carb(uretor)?|"
    r"bumper|fender|hood\b|grille|door\b|mirror|quarter\s?panel|tailgate|"
    r"headlight|tail\s?light|taillight|fog\s?light|turn\s?signal|"
    r"radio\b|speaker|stereo|screen\b|dash\b|cluster\b|computer\b|module\b|"
    r"ecm|ecu|pcm|tcm|harness|sensor\b|coil\b|spark\s?plug|injector|"
    r"fuel\s?pump|tank\b|seat\b|console\b|steering\s?wheel|airbag|"
    r"wheel\b|rim\b|tire\b|hubcap|wiper|window\b|windshield|regulator|"
    r"ac\s?compressor|heater\s?core|battery\s?tray|skid\s?plate|"
    r"gasket|thermostat|water\s?pump|bearing|\bhose\b|\bbelt\b|\bvalves?\b|strut|shocks?\b|"
    r"control\s?arm|tie\s?rod|ball\s?joint|"
    r"(do|does)\s+you\s+(have|carry)|looking\s+for|you\s+guys\s+have",
    re.I,
)

EXTRACT_PROMPT = """You extract structured parts-lead data from a phone call transcript
for an auto-salvage shop.

Return ONLY compact JSON, no prose, no code fences:
{"is_parts_lead": true/false,
 "caller_name": "name or empty",
 "vehicles": [{"year": "", "make": "", "model": "", "engine": ""}],
 "callback": "callback number the caller said, or empty",
 "parts": [{"name": "", "part_number": "", "variant_hint": "", "qty": 1}]}
Rules:
- is_parts_lead is true only when the caller wants a PART for a vehicle.
- year/make/model/engine exactly as spoken (" Chevy Silverado" -> make "Chevy").
  Leave a field "" when not said. variant_hint: amperage/engine size/opt code if said.
- Never invent parts they did not mention. part_number only if the caller said one.
- callback: only a number the caller spoke as their callback; "" if they said caller ID is fine or said none.
- If is_parts_lead is false, all other fields are empty."""


def leads_dir(cfg):
    d = os.path.expanduser(dig(cfg, "leads.dir", os.path.join(app_home(), "leads")))
    os.makedirs(d, exist_ok=True)
    return d


def knowledge_dir(cfg):
    d = os.path.expanduser(dig(cfg, "orders.knowledge_dir",
                               os.path.join(app_home(), "knowledge")))
    os.makedirs(d, exist_ok=True)
    return d


def looks_like_parts_call(transcript):
    return any(w == "caller" and PARTS_RE.search(t) for w, t in transcript)


def extract_json(text):
    """Tolerant JSON from an LLM: strips fences and leading/trailing prose."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def extract_lead(cfg, number, transcript):
    """LLM extraction. Returns the lead dict or None when this was no parts call."""
    if not looks_like_parts_call(transcript):
        return None
    from . import llm
    lines = [f"{who}: {t}" for who, t in transcript if who in ("caller", "agent", "note")]
    try:
        reply, _provider = llm.chat(cfg, [
            {"role": "system", "content": EXTRACT_PROMPT},
            {"role": "user", "content": f"Caller ID: {number}\n\n" + "\n".join(lines)[:8000]},
        ])
    except llm.LLMError:
        return None
    data = extract_json(reply)
    if not data or not data.get("is_parts_lead"):
        return None
    parts = [p for p in (data.get("parts") or [])
             if isinstance(p, dict) and (p.get("name") or "").strip()]
    if not parts:
        return None
    return {
        "caller_name": (data.get("caller_name") or "").strip(),
        "callback": re.sub(r"[^\d+]", "", str(data.get("callback") or ""))[:15],
        "vehicles": [v for v in (data.get("vehicles") or []) if isinstance(v, dict)],
        "parts": parts,
        "notes": (data.get("notes") or "").strip(),
    }


def _vehicle_words(lead):
    v = (lead.get("vehicles") or [{}])[0]
    return {"year": str(v.get("year") or ""),
            "model": " ".join(x for x in (v.get("make") or "", v.get("model") or "") if x),
            "engine": v.get("engine") or ""}


def _lead_id(stamp, number):
    safe = re.sub(r"\D", "", number or "") or "unknown"
    return f"{stamp}_{safe[-10:]}"


def save_lead(cfg, lead, number, transcript_path=""):
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    rec = {
        "id": _lead_id(stamp, number),
        "created": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "number": number, **lead,
        "transcript": transcript_path, "status": "new",
    }
    path = os.path.join(leads_dir(cfg), rec["id"] + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rec, f, indent=2)
    return path, rec


def load_leads(cfg):
    out = []
    d = leads_dir(cfg)
    for name in sorted(os.listdir(d)):
        if not name.endswith(".json"):
            continue
        try:
            rec = json.load(open(os.path.join(d, name), encoding="utf-8"))
            if isinstance(rec, dict) and rec.get("status") != "done":
                out.append(rec)
        except (OSError, json.JSONDecodeError):
            continue
    out.sort(key=lambda r: r.get("created") or "", reverse=True)
    return out


def digest_path(cfg):
    return os.path.join(leads_dir(cfg), "open-leads.md")


def write_digest(cfg, log=print):
    """leads/open-leads.md — the owner's list of open part requests (newest first).
    Owner-facing only: it lives outside knowledge/ so the receptionist never
    reads one caller's request to another."""
    leads = load_leads(cfg)
    lines = ["# Open part requests (newest first)",
             "Set \"status\": \"done\" in the lead's JSON to clear it. "
             "Leads are deleted when done or after leads.retention_days (default 30).", ""]
    if not leads:
        lines.append("(none)")
    for rec in leads[:50]:
        v = _vehicle_words(rec)
        veh = " ".join(x for x in (v["year"], v["model"], v["engine"]) if x) \
            or "vehicle not captured"
        who = rec.get("caller_name") or "name not captured"
        cb = rec.get("callback") or rec.get("number") or "unknown"
        parts = ", ".join(
            (p.get("name") or "") + (f" (PN {p['part_number']})" if p.get("part_number") else "")
            for p in rec.get("parts") or [])
        lines.append(f"- {rec.get('created', '')[:16]} — {who} — callback {cb} — {veh} — {parts}")
    path = digest_path(cfg)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines)[:40000] + "\n")
    # retire the old receptionist-readable digest: callers must never hear other leads
    old = os.path.join(knowledge_dir(cfg), "leads.md")
    if os.path.exists(old):
        try:
            os.unlink(old)
        except OSError:
            pass
    return path


_lock = threading.Lock()


def process(cfg, number, transcript, transcript_path="", log=print):
    """Full post-call pipeline. Safe to call from a thread; single-flight."""
    if not dig(cfg, "leads.enabled", True):
        return False
    if not _lock.acquire(blocking=False):
        log("[leads] one already running — skipped this call")
        return False
    try:
        lead = extract_lead(cfg, number, transcript)
        if not lead:
            return False
        v = _vehicle_words(lead)
        log(f"[leads] parts lead: {v['year']} {v['model']} — "
            + ", ".join((p.get('name') or '') for p in lead['parts']))
        path, rec = save_lead(cfg, lead, number, transcript_path)
        digest = write_digest(cfg, log)
        log(f"[leads] saved {path} — listed in {digest}")
        from . import notify
        notify.send_lead_alert(cfg, rec, digest, log=log)
        return True
    except Exception as e:
        log(f"[leads] pipeline error: {e}")
        from . import errors
        errors.record("leads", e, cfg)
        return False
    finally:
        _lock.release()
