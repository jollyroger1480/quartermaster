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


def load(explicit=None):
    path = find_config(explicit)
    if not path:
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def dig(cfg, dotted, default=None):
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur
