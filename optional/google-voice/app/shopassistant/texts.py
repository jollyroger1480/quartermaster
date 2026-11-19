"""SMS replies through the Google Voice web Messages view.

Built from a probe of the live page (Sept 2026):
- thread rows: gv-thread-list-item .container — class "read" absent = unread
- last message text: gv-annotation[aria-label] inside the row
- opening a row sets ?itemId=t.%2B1XXXXXXXXXX (the other party's number)
- compose: gv-message-entry textarea, button[aria-label="Send message"]

Safety rails: only unread threads from plain 10-digit numbers (no contacts,
short codes or Voice-flagged spam), a hands-off delay so the owner can answer
first, a per-number hourly cap, and never while a call is ringing or active.
"""
import datetime
import json
import os
import re
import time
import urllib.parse

from . import brain
from .config import dig, logs_dir

ROWS_JS = r"""() => {
  const out = [];
  const rows = document.querySelectorAll('gv-thread-list-item .container, gv-message-thread-list-item .container');
  rows.forEach((el, i) => {
    const lines = (el.innerText || '').split('\n').map((s) => s.replace(/[\u200e\u200f\u202a-\u202e]/g, '').trim()).filter(Boolean);
    const ann = el.querySelector('gv-annotation[aria-label]');
    out.push({ i, unread: !el.classList.contains('read'), active: el.classList.contains('active'),
               lines: lines.slice(0, 8), preview: ann ? ann.getAttribute('aria-label') : '' });
  });
  return out;
}"""

ICONS = {"report", "person", "group", "people", "block"}
NUM_RE = re.compile(r"^\+?1?\s*\(?(\d{3})\)?[\s.-]?(\d{3})[\s.-]?(\d{4})$")


def _row_identity(row):
    lines = [l for l in row["lines"] if l not in (".",)]
    spam = bool(lines) and lines[0] == "report"
    lines = [l for l in lines if l not in ICONS]
    head = lines[0] if lines else ""
    m = NUM_RE.match(head)
    number = "".join(m.groups()) if m else None
    time_s = next((l for l in lines[1:4] if re.search(r"\d:\d\d|\b[A-Z][a-z]{2} \d{1,2}\b|yesterday", l, re.I)), "")
    return {"key": head, "number": number, "contact": number is None and bool(re.search(r"[A-Za-z]", head)),
            "spam": spam, "time": time_s}


def _minutes_ago(time_s):
    """Voice shows today's messages as '9:46 AM'; anything else is older."""
    m = re.match(r"^(\d{1,2}):(\d{2})\s*([AP]M)$", (time_s or "").replace("\u202f", " ").strip(), re.I)
    if not m:
        return 1e9
    h, mi, ap = int(m.group(1)) % 12, int(m.group(2)), m.group(3).upper()
    h += 12 if ap == "PM" else 0
    now = datetime.datetime.now()
    t = now.replace(hour=h, minute=mi, second=0, microsecond=0)
    return (now - t).total_seconds() / 60 if t <= now else 1e9


class TextBot:
    def __init__(self, cfg, gv, log=print):
        self.cfg, self.gv, self.log = cfg, gv, log
        self.state_path = os.path.join(logs_dir(cfg), ".sms_state.json")
        self.state = self._load()
        self.first_seen = {}          # key -> monotonic time we first saw it unread
        # key -> wall-clock time of the bot's last reply in that thread (persisted,
        # so a restart doesn't forget conversations the bot is in the middle of)
        self.handling = self.state.setdefault("handling", {})
        self.next_poll = 0.0

    # ------------------------------------------------------------ state
    def _load(self):
        try:
            return json.load(open(self.state_path, encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self):
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f)
        os.replace(tmp, self.state_path)

    def _log_file(self, number, incoming, reply, note=""):
        d = os.path.join(logs_dir(self.cfg), "sms")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{datetime.date.today():%Y-%m-%d}.md"), "a", encoding="utf-8") as f:
            f.write(f"### {datetime.datetime.now():%H:%M:%S} — {number}\n\n**them:** {incoming}\n\n")
            f.write(f"**bot:** {reply}\n\n" if reply else f"_{note}_\n\n")

    # ------------------------------------------------------------ page
    async def _ensure_messages_view(self):
        """Voice switches to the Calls view for every call and stays there, so
        come back to Messages before each poll. The nav link's name changes with
        the unread badge ("Messages, 1 unread"), so match loosely, then fall
        back to the href, then to loading the Messages URL (only ever called idle)."""
        page = self.gv.page
        if "/messages" in (page.url or ""):
            return True
        tries = (
            lambda: page.get_by_role("link", name=re.compile(r"^\s*messages\b", re.I)).first.click(timeout=2500),
            lambda: page.locator('a[href*="/messages"]').first.click(timeout=2500),
        )
        for t in tries:
            try:
                await t()
                await page.wait_for_timeout(1200)
            except Exception:
                continue
            if "/messages" in (page.url or ""):
                return True
        m = re.match(r"(https?://[^/]+/u/\d+/)", page.url or "")
        url = m.group(1) + "messages" if m else dig(self.cfg, "browser.url", "https://voice.google.com/u/0/messages")
        try:
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(2500)
            await self.gv.configure_bridge()
        except Exception as e:
            self.log(f"sms: can't open the Messages view ({type(e).__name__}: {e})")
            return False
        ok = "/messages" in (page.url or "")
        if not ok:
            self.log(f"sms: can't open the Messages view (page is {page.url})")
        return ok

    async def _rows(self):
        return await self.gv.page.evaluate(ROWS_JS)

    async def _open_row(self, i):
        page = self.gv.page
        rows = page.locator("gv-thread-list-item .container, gv-message-thread-list-item .container")
        await rows.nth(i).click(timeout=4000)
        await page.wait_for_timeout(1500)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(page.url).query)
        item = urllib.parse.unquote((q.get("itemId") or [""])[0])
        m = re.match(r"t\.\+?1?(\d{10})$", item)
        return m.group(1) if m else None

    async def _close_thread(self):
        """Leave the conversation open-view: Voice marks texts read the moment they
        land in an open thread, which would hide the customer's next message."""
        page = self.gv.page
        if "itemId=" not in (page.url or ""):
            return
        try:
            await page.evaluate("""() => { const u = location.pathname;
                history.pushState({}, '', u); window.dispatchEvent(new PopStateEvent('popstate', {state: {}})); }""")
            await page.wait_for_timeout(800)
        except Exception:
            pass
        try:
            await page.get_by_role("link", name=re.compile(r"^\s*messages\s*$", re.I)).first.click(timeout=2000)
            await page.wait_for_timeout(600)
        except Exception:
            pass
        if "itemId=" in (page.url or ""):
            await page.goto(page.url.split("?")[0], wait_until="domcontentloaded")
            await page.wait_for_timeout(2500)
            await self.gv.configure_bridge()

    async def _send(self, text):
        page = self.gv.page
        box = page.locator("gv-message-entry textarea, gv-message-entry [contenteditable=true], "
                           "gv-message-entry input[type=text]").first
        await box.click(timeout=4000)
        await box.fill(text)
        await page.wait_for_timeout(300)
        await page.get_by_role("button", name=re.compile(r"^\s*send message\s*$", re.I)).first.click(timeout=4000)
        for _ in range(16):   # confirm: the open thread's preview flips to "You: ..."
            await page.wait_for_timeout(500)
            for r in await self._rows():
                if r["active"] and (r["preview"] or "").lower().startswith("you:"):
                    return True
        return False

    # ------------------------------------------------------------ poll
    async def poll(self):
        """One pass. Call only while no call is ringing or active."""
        if not dig(self.cfg, "sms.enabled", True):
            return
        now = time.monotonic()
        if now < self.next_poll:
            return
        self.next_poll = now + float(dig(self.cfg, "sms.poll_seconds", 15))
        if not await self._ensure_messages_view():
            return
        rows = await self._rows()
        seen = self.state.setdefault("seen", {})
        if not self.state.get("initialized"):
            # never answer history on first run — except unread texts from the
            # last few minutes (e.g. a test sent while the bot was starting)
            fresh = 0
            for r in rows:
                ident = _row_identity(r)
                if r["unread"] and _minutes_ago(ident["time"]) <= float(dig(self.cfg, "sms.startup_grace_min", 10)):
                    fresh += 1
                    continue
                seen[ident["key"]] = f"{ident['time']}|{r['preview']}"
            self.state["initialized"] = True
            self._save()
            self.log(f"sms: watching {len(rows)} threads ({len(rows) - fresh} existing marked as seen, {fresh} recent unread will be answered)")
            if not fresh:
                return
            rows = await self._rows()
        delay = float(dig(self.cfg, "sms.reply_delay_s", 60))
        allow = set(dig(self.cfg, "sms.allow_contacts", []) or [])
        for r in rows:
            ident = _row_identity(r)
            sig = f"{ident['time']}|{r['preview']}"
            key = ident["key"]
            # In a conversation the bot is already having, Voice often marks the
            # customer's next text read on arrival (the thread is on screen), so
            # read/unread can't be trusted there — any new non-"You:" text counts.
            ours = time.time() - self.handling.get(key, 0) < float(dig(self.cfg, "sms.conversation_window_s", 1800))
            if seen.get(key) == sig or not (r["unread"] or ours):
                self.first_seen.pop(key, None)
                continue
            preview = (r["preview"] or "").strip()
            if preview.lower().startswith("you:"):
                mine = self.state.get("last_reply", {}).get(key, "")
                said = preview[4:].strip()
                if ours and mine and not (mine.startswith(said[:40]) or said.startswith(mine[:40])):
                    self.handling.pop(key, None)            # the owner replied himself — bot steps out
                    self.state.setdefault("owner", {})[key] = time.time()
                    self.log(f"sms: {key}: you replied yourself — bot stays out of this conversation")
                seen[key] = sig
                self._save()
                continue
            if preview.lower().startswith("draft") or not preview:
                seen[key] = sig
                continue
            why = None
            if time.time() - self.state.get("owner", {}).get(key, 0) < float(dig(self.cfg, "sms.owner_hold_s", 7200)):
                why = "you're handling this conversation"
            elif ident["spam"]:
                why = "Voice flagged spam"
            elif ident["contact"] and key not in allow and not dig(self.cfg, "sms.reply_to_contacts", False):
                why = "saved contact"
            elif not ident["number"] and not ident["contact"]:
                why = "short code / non-phone sender"
            elif brain.SPAM_RE.search(preview):
                why = "spam script"
            if why:
                self.log(f"sms: {key}: not replying ({why}) — {preview[:60]!r}")
                self._log_file(key, preview, None, f"not replied: {why}")
                seen[key] = sig
                self._save()
                continue
            t0 = self.first_seen.setdefault(key, now)
            wait = float(dig(self.cfg, "sms.followup_delay_s", 15)) if ours else delay
            if now - t0 < wait:
                continue                                    # give the owner first crack at it
            try:
                reply = await self._handle(r, ident, preview)
                if reply:
                    self.handling[key] = time.time()
                    self.state.setdefault("last_reply", {})[key] = reply
            except Exception as e:                          # never retry-loop on a broken page
                self.log(f"sms: {key}: reply failed ({type(e).__name__}: {e}) — leaving it for the owner")
                self._log_file(key, preview, None, f"reply failed: {e}")
            finally:
                await self._close_thread()
            seen[key] = sig
            self.first_seen.pop(key, None)
            self._save()
            return                                          # one reply per pass keeps the loop responsive

    async def _handle(self, row, ident, preview):
        key = ident["key"]
        number = await self._open_row(row["i"])
        number = number or ident["number"] or key
        sent = [t for t in self.state.setdefault("sent", {}).get(number, []) if time.time() - t < 3600]
        if len(sent) >= int(dig(self.cfg, "sms.max_replies_per_hour", 4)):
            self.log(f"sms: {number}: hourly reply cap reached — leaving it for the owner")
            self._log_file(number, preview, None, "not replied: hourly cap")
            return None
        hist = self.state.setdefault("hist", {}).get(number, [])
        try:
            import asyncio
            reply, provider, esc = await asyncio.get_running_loop().run_in_executor(
                None, brain.sms_reply, self.cfg, hist, preview)
        except Exception as e:
            self.log(f"sms: {number}: brain error {e}")
            return None
        if dig(self.cfg, "sms.dry_run", False):
            self.log(f"sms DRY RUN {number}: {preview!r} -> {reply!r}")
            self._log_file(number, preview, reply, "dry run — not sent")
            return reply
        ok = await self._send(reply)
        self.log(f"sms {number}: {preview[:60]!r} -> {'SENT' if ok else 'SEND UNCONFIRMED'} [{provider}] {reply[:80]!r}"
                 + (" ⚠️ ESCALATION" if esc else ""))
        self._log_file(number, preview, reply, "" if ok else "send unconfirmed")
        self.state["sent"][number] = sent + [time.time()]
        self.state["hist"][number] = (hist + [{"role": "user", "content": preview},
                                              {"role": "assistant", "content": reply}])[-12:]
        return reply if ok else None
