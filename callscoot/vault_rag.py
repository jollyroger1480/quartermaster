"""Retrieval over the shared ai-memory vault (orders, policies, whatever is on
file). Keyword scoring with a boost for order-like numbers, plus an optional
graphify knowledge-graph query. Strictly local files — no internet."""
import os
import re
import subprocess

from .config import app_home, dig

STOP = set("""the a an is are was were do does did i you my your me we to of and or in on for
with at by it this that be have has had what when where who whom how why can could would will
shall should may might must please hi hello hey thanks thank about from up so if there their
they them he she his her us our got get go going just very really okay ok yes no not do don't
i'm you're it's that's on the and but for with""".split())

NUM_RE = re.compile(r"\b\d{4,}\b")
TERM_RE = re.compile(r"[a-z0-9#]{2,}")


def _terms(s):
    return [w for w in TERM_RE.findall(s.lower()) if w not in STOP]


def _vault_files(cfg):
    root = os.path.expanduser(dig(cfg, "vault.root",
                                  os.path.join(app_home(), "knowledge")))
    lanes = dig(cfg, "vault.lanes", []) or []
    bases = [root]
    for d in dig(cfg, "vault.extra_dirs", []) or []:
        d = os.path.expanduser(d)
        if os.path.isdir(d):
            bases.append(d)
    files = []
    for base in bases:
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            if base == root and lanes and dirpath == root:
                dirnames[:] = [d for d in dirnames if d in lanes]
            for f in filenames:
                if f.lower().endswith((".md", ".txt")):
                    files.append(os.path.join(dirpath, f))
    return files


def _chunks(text, max_chars):
    out, cur = [], ""
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(cur) + len(para) + 2 <= max_chars:
            cur = f"{cur}\n\n{para}".strip()
        else:
            if cur:
                out.append(cur)
            while len(para) > max_chars * 1.8:
                out.append(para[:max_chars])
                para = para[max_chars:]
            cur = para
    if cur:
        out.append(cur)
    return out


def _score(chunk, q_terms, q_numbers):
    low = chunk.lower()
    score = sum(2 for t in q_terms if t in low)
    score += sum(1 for t in q_terms for w in low.split() if t == w)  # exact word bonus
    for n in q_numbers:
        if n in chunk:
            score += 25
    return score


def retrieve(cfg, query, k=None):
    k = k or dig(cfg, "vault.top_k", 6)
    q_terms = _terms(query)
    q_numbers = NUM_RE.findall(query)
    if not q_terms and not q_numbers:
        return []
    hits = []
    max_chars = dig(cfg, "vault.chunk_chars", 700)
    for path in _vault_files(cfg):
        # live operational digests outrank static notes on ties
        fresh = 6 if os.sep + "knowledge" + os.sep in path else 0
        for chunk in _chunks(text_of(path), max_chars):
            s = _score(chunk, q_terms, q_numbers)
            if s <= 0:
                continue
            hits.append((s + fresh, path, chunk))
    hits.sort(key=lambda h: -h[0])
    # cap chunks per file so one dense note can't fill the whole context
    picked, per_file = [], {}
    for s, path, chunk in hits:
        if per_file.get(path, 0) >= 2:
            continue
        per_file[path] = per_file.get(path, 0) + 1
        picked.append((s, path, chunk))
        if len(picked) >= k:
            break
    return picked


def text_of(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def graphify_query(cfg, query):
    gdir = os.path.expanduser(dig(cfg, "vault.graphify_dir", "") or "")
    if not gdir or not os.path.isdir(gdir):
        return ""
    query = query.lstrip("-").strip()[:500]  # no flag injection into the CLI
    try:
        p = subprocess.run(["graphify", "query", query], cwd=gdir,
                           capture_output=True, text=True, timeout=25)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    out = (p.stdout or "").strip()
    return out[:1800]


EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
URL_RE = re.compile(r"https?://\S+")


def redact(text):
    """Contact-harvest bait never reaches the phone side."""
    text = URL_RE.sub("[link]", text)
    return EMAIL_RE.sub("[email]", text)


def build_context(cfg, query):
    parts = []
    g = graphify_query(cfg, query)
    if g:
        parts.append("[knowledge graph]\n" + redact(g))
    hits = retrieve(cfg, query)
    if hits:
        root = os.path.expanduser(dig(cfg, "vault.root", ""))
        blocks = []
        for _s, path, chunk in hits:
            try:
                label = os.path.relpath(path, root) if root else path
            except ValueError:
                label = path
            blocks.append(f"[source: {label}]\n{redact(chunk)}")
        parts.append("\n\n".join(blocks))
    return "\n\n---\n\n".join(parts), [h[1] for h in hits]
