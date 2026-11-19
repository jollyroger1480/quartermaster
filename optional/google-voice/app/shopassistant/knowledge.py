"""Keyword search over the business's own notes (data\\knowledge\\*.md / *.txt).
Strictly local files. The assistant is only allowed to state business facts
that this search hands it, so what's written there is what callers hear."""
import os
import re

from .config import dig, knowledge_dir

STOP = set("""the a an is are was were do does did i you my your me we to of and or in on for
with at by it this that be have has had what when where who whom how why can could would will
shall should may might must please hi hello hey thanks thank about from up so if there their
they them he she his her us our got get go going just very really okay ok yes no not don't
i'm you're it's that's but""".split())

NUM_RE = re.compile(r"\b\d{4,}\b")
TERM_RE = re.compile(r"[a-z0-9#]{2,}")
HEADING_RE = re.compile(r"^#{1,6}\s")

TEMPLATE = """# {shop} - facts for the phone assistant

Write this the way you'd explain your business to a new employee on their first day.
Plain sentences work best. The assistant can only tell callers what is written in this
folder, so anything missing here becomes "I'll have {owner} get back to you".
Replace every [FILL IN] and delete what doesn't apply.

## What we do

[FILL IN: what the business does, who it serves, and what it does not do.]

## Hours and how to reach us

[FILL IN: days and hours, time zone, whether walk-ins are welcome or it's by appointment,
and the best way to reach {owner}.]

## Services and prices

[FILL IN: one short paragraph per service. Say what's included, the starting price,
how long it takes, and what the customer has to bring, send or tell you.
If a price depends on the job, say "quoted per job" so the assistant doesn't guess.]

## How to order or book

[FILL IN: website, phone, in person? What payment do you take? Anything you never do,
like taking card numbers over the phone?]

## Policies

[FILL IN: refunds, returns, warranty, deposits, cancellations. If {owner} decides these
personally, say so.]

## Common questions

[FILL IN: write the questions customers ask most, each followed by your usual answer.
This section does more for the assistant than anything else.]
"""


def ensure_template(cfg):
    """First run: drop a fill-in-the-blanks file so the folder is never empty."""
    d = knowledge_dir(cfg)
    path = os.path.join(d, "business.md")
    shop = dig(cfg, "persona.shop", "") or "Our business"
    owner = dig(cfg, "persona.owner", "") or "the owner"
    existing = [f for f in os.listdir(d) if f.lower().endswith((".md", ".txt"))]
    if existing:
        # still the untouched placeholder from before the business was named? refresh it
        blank = TEMPLATE.format(shop="Our business", owner="the owner")
        if existing != ["business.md"] or text_of(path).replace("\r\n", "\n") != blank:
            return None
    with open(path, "w", encoding="utf-8") as f:
        f.write(TEMPLATE.format(shop=shop, owner=owner))
    return path


def files(cfg):
    out = []
    for dirpath, dirnames, filenames in os.walk(knowledge_dir(cfg)):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for f in sorted(filenames):
            if f.lower().endswith((".md", ".txt")):
                out.append(os.path.join(dirpath, f))
    return out


def text_of(path):
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def unfinished(cfg):
    """True while the knowledge is still the untouched template (or empty)."""
    body = "\n".join(text_of(p) for p in files(cfg))
    return len(body.strip()) < 200 or "[FILL IN" in body


def _terms(s):
    return [w for w in TERM_RE.findall((s or "").lower()) if w not in STOP]


def _para_chunks(text, max_chars):
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


def _chunks(text, max_chars):
    """Every chunk carries its section heading, so a heading is never split
    from its body and heading words count toward the body's score."""
    out, heading, buf = [], "", []

    def flush():
        body = "\n".join(buf).strip()
        if body:
            for c in _para_chunks(body, max_chars):
                out.append(f"{heading}\n{c}" if heading else c)

    for ln in text.splitlines():
        if HEADING_RE.match(ln):
            flush()
            heading, buf = ln.strip(), []
        else:
            buf.append(ln)
    flush()
    return out


def _stem(t):
    for suf in ("ing", "es", "ed", "s"):
        if len(t) - len(suf) >= 4 and t.endswith(suf):
            return t[: -len(suf)]
    return t


def _score(chunk, q_terms, q_numbers):
    low = chunk.lower()
    score = sum(2 for t in q_terms if _stem(t) in low)
    score += sum(1 for t in q_terms for w in low.split() if t == w)
    for n in q_numbers:
        if n in chunk:
            score += 25
    return score


def retrieve(cfg, query, k=None, focus=None):
    """focus = the newest message; its words weigh 3x the earlier turns, so a
    follow-up question finds its own section instead of the previous topic's."""
    k = k or int(dig(cfg, "knowledge.top_k", 6))
    q_terms = list(dict.fromkeys(_terms(query)))
    q_numbers = NUM_RE.findall(query or "")
    f_terms = list(dict.fromkeys(_terms(focus))) if focus else []
    f_numbers = NUM_RE.findall(focus) if focus else []
    if not q_terms and not q_numbers and not f_terms:
        return []
    hits = []
    max_chars = int(dig(cfg, "knowledge.chunk_chars", 700))
    for path in files(cfg):
        for chunk in _chunks(text_of(path), max_chars):
            if "[FILL IN" in chunk:
                continue                      # never read template placeholders to a caller
            s = _score(chunk, q_terms, q_numbers)
            if f_terms or f_numbers:
                s += 3 * _score(chunk, f_terms, f_numbers)
            if s > 0:
                hits.append((s, path, chunk))
    hits.sort(key=lambda h: -h[0])
    cap = k if len({h[1] for h in hits}) <= 1 else max(2, k // 2)
    picked, per_file = [], {}
    for s, path, chunk in hits:
        if per_file.get(path, 0) >= cap:
            continue
        per_file[path] = per_file.get(path, 0) + 1
        picked.append((s, path, chunk))
        if len(picked) >= k:
            break
    return picked


def build_context(cfg, query, focus=None):
    hits = retrieve(cfg, query, focus=focus)
    return "\n\n".join(chunk for _s, _p, chunk in hits), [h[1] for h in hits]
