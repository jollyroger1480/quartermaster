"""LLM client: any OpenAI-compatible chat-completions endpoint. Providers are
tried top to bottom from callscoot.toml [[llm.providers]]; the first one that
answers wins. Keys are read from the environment (never stored in files that
leave your machine). Default fallback: Groq (free tier) then local Ollama."""
import json
import os
import urllib.error
import urllib.request

from .config import dig


class LLMError(RuntimeError):
    pass


def _post(url, key, payload, timeout=60):
    headers = {"Content-Type": "application/json"}
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
    return dig(cfg, "llm.providers", None) or [
        {"name": "groq", "base_url": "https://api.groq.com/openai/v1",
         "model": "openai/gpt-oss-20b", "api_key_env": "GROQ_API_KEY"},
        {"name": "ollama", "base_url": "http://127.0.0.1:11434/v1",
         "model": "llama3.1", "api_key_env": ""},
    ]


def chat(cfg, messages):
    errors = []
    max_tokens = dig(cfg, "llm.max_tokens", 250)
    for p in providers(cfg):
        url = p["base_url"].rstrip("/") + "/chat/completions"
        key = os.environ.get(p.get("api_key_env") or "", "")
        payload = {
            "model": p.get("model", "auto"),
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": dig(cfg, "llm.temperature", 0.3),
        }
        payload.update(p.get("extra_body") or {})
        failed = None
        for attempt in range(2):
            try:
                text = _content(_post(url, key, payload))
                if text:
                    return text, p.get("name", p["base_url"])
                # reasoning model burned its budget thinking — retry bigger, thinking off
                if attempt == 0:
                    payload["max_tokens"] = max_tokens * 3
                    payload.setdefault("chat_template_kwargs", {"thinking": False})
                    continue
            except (urllib.error.URLError, TimeoutError, OSError, KeyError, json.JSONDecodeError) as e:
                failed = f"{p.get('name')}: {e}"
                break
        errors.append(failed or f"{p.get('name')}: empty content")
    raise LLMError("all providers failed — " + "; ".join(errors))
