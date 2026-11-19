"""LLM client: any OpenAI-compatible chat-completions endpoint. Providers are
tried top to bottom from callscoot.toml [[llm.providers]]; the first one that
answers wins. Keys are read from the environment (never stored in files that
leave your machine). A provider may instead name api_key_file: a file holding
only the bearer token, re-read on every call, so a token that rotates is picked
up without a restart. Default fallback: Groq (free tier) then local Ollama."""
import json
import os
import urllib.error
import urllib.request

from .config import dig


class LLMError(RuntimeError):
    pass


def _post(url, key, payload, timeout=60):
    from . import net  # keep-alive session: no TLS handshake per turn
    return net.post(url, key, timeout=timeout, json=payload).json()


def provider_key(p):
    """Bearer token for a provider: api_key_file (read fresh) wins over api_key_env."""
    path = p.get("api_key_file") or ""
    if path:
        try:
            with open(os.path.expanduser(path), encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return ""
    return os.environ.get(p.get("api_key_env") or "", "")


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
    max_tokens = dig(cfg, "llm.max_tokens", 120)
    for p in providers(cfg):
        url = p["base_url"].rstrip("/") + "/chat/completions"
        key = provider_key(p)
        # a live caller is waiting: a slow provider must fail over, not hang the call
        timeout = float(p.get("timeout_s") or dig(cfg, "llm.timeout_s", 20))
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
                text = _content(_post(url, key, payload, timeout=timeout))
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
