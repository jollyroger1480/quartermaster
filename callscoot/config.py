"""Config loading (TOML) + dotted getters."""
import os
import tomllib


def app_home():
    """Root dir for config, logs, knowledge, state. Override with CALLSCOOT_HOME."""
    return os.path.expanduser(os.environ.get("CALLSCOOT_HOME", "~/grok-work/callscoot"))


CONFIG_CANDIDATES = [
    os.path.join(os.getcwd(), "callscoot.toml"),
    os.path.join(app_home(), "callscoot.toml"),
]


def find_config(explicit=None):
    cands = ([explicit] if explicit else []) + CONFIG_CANDIDATES
    for c in cands:
        c = os.path.expanduser(c)
        if os.path.isfile(c):
            return c
    return None


def toml_quote(value):
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def llm_side_path(config_path):
    return os.path.join(os.path.dirname(os.path.abspath(config_path)), "callscoot.llm.toml")


def load(explicit=None):
    path = find_config(explicit)
    if not path:
        return {}
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    side_path = llm_side_path(path)
    if not os.path.isfile(side_path):
        return cfg
    with open(side_path, "rb") as f:
        side = tomllib.load(f)
    extra = side.get("providers") or []
    if not extra:
        return cfg
    llm = cfg.setdefault("llm", {})
    llm["providers"] = list(extra) + list(llm.get("providers") or [])
    return cfg


def add_llm_provider(config_path, name, base_url, model, api_key_env, api_key=""):
    """Write a provider the loader tries before callscoot.toml. The key stays in .env."""
    if not str(base_url).startswith(("http://", "https://")):
        raise ValueError("base_url must start with http:// or https://")
    if os.path.basename(config_path) == "callscoot.example.toml":
        raise ValueError("copy callscoot.example.toml to callscoot.toml first")
    side = llm_side_path(config_path)
    block = (
        "[[providers]]\n"
        f"name = {toml_quote(name)}\n"
        f"base_url = {toml_quote(base_url)}\n"
        f"model = {toml_quote(model)}\n"
        f"api_key_env = {toml_quote(api_key_env)}\n"
    )
    if os.path.isfile(side):
        with open(side, "a", encoding="utf-8") as fh:
            fh.write("\n" + block)
    else:
        with open(side, "w", encoding="utf-8") as fh:
            fh.write(
                "# Tried before the providers in callscoot.toml.\n"
                "# The first one that answers is the one the phone uses.\n"
                "# This file stays off git.\n\n"
                + block
            )
        os.chmod(side, 0o600)
    if api_key:
        _upsert_env(os.path.join(os.path.dirname(side), ".env"), api_key_env, api_key)
        home_env = os.path.join(app_home(), ".env")
        if os.path.abspath(home_env) != os.path.abspath(os.path.join(os.path.dirname(side), ".env")):
            _upsert_env(home_env, api_key_env, api_key)
    return side


def _upsert_env(path, key, value):
    lines = []
    if os.path.isfile(path):
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    prefix = key + "="
    kept = [line for line in lines if not line.startswith(prefix) and not line.startswith("export " + prefix)]
    kept.append(f"{key}={value}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(kept) + "\n")
    os.chmod(path, 0o600)


def dig(cfg, dotted, default=None):
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur
