"""Snapshot live order data into plain-text digests the vault RAG can search.

Sources (all best-effort, missing = skip, never fatal):
- eBay orders via the local-exec tool ~/ebay/agent/feedback_orders.py (read-only)
- Square seen-orders JSON if/when sync_square_orders.py has produced it
Digests land in $CALLSCOOT_HOME/knowledge/ (add that dir to vault.extra_dirs).
"""
import datetime
import json
import os
import subprocess
import sys

from .config import app_home, dig

TOKEN_FAIL = "No EBAY_USER_ACCESS_TOKEN"


def knowledge_dir(cfg):
    d = os.path.expanduser(dig(cfg, "orders.knowledge_dir", os.path.join(app_home(), "knowledge")))
    os.makedirs(d, exist_ok=True)
    return d


def _write(cfg, name, body):
    if not body or not body.strip():
        return False
    path = os.path.join(knowledge_dir(cfg), name)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {name.replace('_', ' ').replace('.md', '')} — snapshot {stamp}\n\n")
        f.write(body.strip() + "\n")
    return True


def _ebay_orders(cfg):
    """Two read-only fetches in one subprocess: completed orders + unshipped."""
    agent_dir = os.path.expanduser(dig(cfg, "orders.ebay_agent_dir", "~/ebay/agent"))
    if not os.path.isdir(agent_dir):
        return None, None, f"no agent dir {agent_dir}"
    days = dig(cfg, "orders.ebay_days", 30)
    count = dig(cfg, "orders.ebay_count", 25)
    code = (
        "import sys; sys.path.insert(0, {d!r}); "
        "from feedback_orders import tool_check_orders; "
        "print('===COMPLETED==='); "
        "print(tool_check_orders(days={days}, count={count}, brief=False)); "
        "print('===UNSHIPPED==='); "
        "print(tool_check_orders(days={days}, count=15, status='unshipped', brief=False))"
    ).format(d=agent_dir, days=days, count=count)
    try:
        p = subprocess.run([sys.executable, "-c", code], cwd=agent_dir,
                           capture_output=True, text=True, timeout=240)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, None, f"eBay orders fetch failed: {e}"
    out = (p.stdout or "").strip()
    if not out or TOKEN_FAIL in out:
        return None, None, "eBay token not available (PC gate fail) — skipped"
    if "Traceback" in (p.stderr or "") and len(out) < 400:
        return None, None, "eBay orders tool errored — skipped"
    completed, unshipped, buf, section = None, None, [], None
    for line in out.splitlines() + ["===END==="]:
        if line.strip() in ("===COMPLETED===", "===UNSHIPPED===", "===END==="):
            body = "\n".join(buf).strip()
            if section == "c" and body:
                completed = body[:20000]
            elif section == "u" and body:
                unshipped = body[:12000]
            buf = []
            section = {"===COMPLETED===": "c", "===UNSHIPPED===": "u"}.get(line.strip())
            continue
        buf.append(line)
    return completed, unshipped, "eBay orders fetched"


def _square_orders(cfg):
    path = os.path.expanduser(dig(
        cfg, "orders.square_seen_json",
        os.path.join(app_home(), "knowledge", "square_seen_orders.json")))
    if not os.path.isfile(path):
        return None, f"no Square orders file yet ({path})"
    try:
        data = json.load(open(path, encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return None, f"Square orders file unreadable: {e}"
    lines = []
    pairs = data.items() if isinstance(data, dict) else enumerate(data)
    for key, rec in pairs:
        if isinstance(rec, dict):
            bits = [f"order {rec.get(k2, key)}" for k2 in ("id", "order_id", "number") if rec.get(k2)]
            head = bits[0] if bits else f"order {key}"
            flat = " | ".join(f"{k2}={v2}" for k2, v2 in rec.items()
                              if isinstance(v2, (str, int, float)) and v2 != "")
            lines.append(f"{head}: {flat}")
        else:
            lines.append(f"{key}: {rec}")
    if not lines:
        return None, "Square orders file empty"
    return "\n".join(lines)[:20000], f"{len(lines)} Square orders"


def refresh(cfg, log=print):
    """Fetch every configured source; returns list of what was written/skipped."""
    summary = []
    completed, unshipped, note = _ebay_orders(cfg)
    summary.append(note)
    key_completed = (
        "Key: OrderID | buyer username | total | ship-to | items.\n"
        "Every order below is PAID. An order that also appears in ebay_unshipped.md "
        "has NOT shipped yet; one absent from it has already shipped.\n\n"
    )
    if completed and _write(cfg, "ebay_orders.md", key_completed + completed):
        summary.append("wrote ebay_orders.md")
    key_unshipped = (
        "These orders are PAID but have NOT shipped yet (snapshot time above). "
        "Anything not listed here has already shipped.\n\n"
    )
    if unshipped and _write(cfg, "ebay_unshipped.md", key_unshipped + unshipped):
        summary.append("wrote ebay_unshipped.md")
    body, note = _square_orders(cfg)
    summary.append(note)
    if body:
        summary.append("wrote square_orders.md" if _write(cfg, "square_orders.md", body) else "square orders empty")
    for line in summary:
        log(f"[callscoot] index: {line}")
    return summary
