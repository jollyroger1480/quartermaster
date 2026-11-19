"""LLM client: any OpenAI-compatible chat-completions endpoint. Providers are
tried top to bottom; the first one that answers wins. Keys come from the
environment (filled from data\\secrets.json), never from settings."""
import json
import os
import urllib.error
import urllib.request

from . import brand
from .config import dig


class LLMError(RuntimeError):
    pass


def _post(url, key, payload, timeout=60):
    # Groq (Cloudflare) rejects the default "Python-urllib" user agent
    headers = {"Content-Type": "application/json", "User-Agent": brand.USER_AGENT}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _content(d):
    try:
        msg = d["choices"][0]["message"]
    except (KeyError, IndexError):
        return None
    if msg.get("content") and str(msg["content"]).strip():
        return str(msg["content"]).strip()
    return None


def providers(cfg):
    return dig(cfg, "llm.providers", None) or []


def chat(cfg, messages):
    errors = []
    max_tokens = dig(cfg, "llm.max_tokens", 1024)
    for p in providers(cfg):
        url = p["base_url"].rstrip("/") + "/chat/completions"
        key = os.environ.get(p.get("api_key_env") or "", "")
        if p.get("api_key_env") and not key:
            errors.append(f"{p.get('name')}: no API key saved")
            continue
        payload = {"model": p.get("model", "auto"), "messages": messages, "max_tokens": max_tokens,
                   "temperature": dig(cfg, "llm.temperature", 0.3)}
        payload.update(p.get("extra_body") or {})
        failed = None
        for attempt in range(2):
            try:
                text = _content(_post(url, key, payload))
                if text:
                    return text, p.get("name", p["base_url"])
                if attempt == 0:   # a reasoning model spent its budget thinking: retry with more room
                    payload["max_tokens"] = max_tokens * 3
                    continue
            except urllib.error.HTTPError as e:
                try:
                    detail = json.loads(e.read().decode("utf-8", "replace"))["error"]["message"]
                except Exception:
                    detail = ""
                failed = f"{p.get('name')}: HTTP {e.code} {detail}".strip()
                break
            except (urllib.error.URLError, TimeoutError, OSError, KeyError, json.JSONDecodeError) as e:
                failed = f"{p.get('name')}: {e}"
                break
        errors.append(failed or f"{p.get('name')}: empty reply")
    raise LLMError("the AI service did not answer - " + "; ".join(errors or ["no provider configured"]))


def list_models(cfg):
    """{provider: [model ids] or 'error: …'} - shown when a model name is rejected."""
    out = {}
    for p in providers(cfg):
        key = os.environ.get(p.get("api_key_env") or "", "")
        headers = {"User-Agent": brand.USER_AGENT}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        try:
            req = urllib.request.Request(p["base_url"].rstrip("/") + "/models", headers=headers)
            with urllib.request.urlopen(req, timeout=15) as r:
                out[p.get("name")] = sorted(m["id"] for m in json.load(r).get("data", []))
        except Exception as e:
            out[p.get("name")] = f"error: {e}"
    return out
