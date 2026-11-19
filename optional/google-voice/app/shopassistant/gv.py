"""Google Voice web driver: attach to the Edge app window over CDP, inject the
audio bridge, read call state from the page, answer and hang up.

Every UI selector is a regex on the button's accessible name and lives in
the "gv" section of settings. Google changes its markup without notice; when it
does, run the probe during a test call and update the patterns - no code change.
"""
import asyncio
import base64
import collections
import json
import os
import re
import time

import numpy as np

from . import edge
from .config import dig

BRIDGE_JS = open(os.path.join(os.path.dirname(__file__), "bridge.js"), encoding="utf-8").read()

DEFAULTS = {
    "answer_button": r"^\s*(answer|accept)\b",
    "decline_button": r"^\s*decline\b",
    "hangup_button": r"\b(end call|hang up|end the call)\b",
    "use_here_button": r"^\s*use (voice )?here\b",
}
# stable Voice test hooks tried when the accessible name doesn't match
TEST_IDS = {"answer_button": "in-call-pickup-call"}

# Click inside the page in one step. Voice (Angular) re-renders the incoming-call
# card several times a second on some calls, so Playwright's click keeps finding a
# detached element; an in-page el.click() can't lose that race.
CLICK_JS = """([src, testid]) => {
  let re; try { re = new RegExp(src, 'i'); } catch (e) { return null; }
  const vis = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  for (const el of document.querySelectorAll('button,[role=button]')) {
    const name = (el.getAttribute('aria-label') || el.innerText || '').trim();
    if (name && re.test(name) && vis(el) && !el.disabled) { el.click(); return name.slice(0, 40); }
  }
  if (testid) {
    const t = document.querySelector('[gv-test-id="' + testid + '"]');
    if (t && vis(t) && !t.disabled) { t.click(); return 'gv-test-id=' + testid; }
  }
  return null;
}"""

# Same lookup as CLICK_JS but returns the caller text, for when the button is
# re-rendered faster than an element handle can be used.
CALLER_FROM_PAGE_JS = """([src, testid]) => {
  let re; try { re = new RegExp(src, 'i'); } catch (e) { return ''; }
  const vis = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  let hit = null;
  for (const el of document.querySelectorAll('button,[role=button]')) {
    const name = (el.getAttribute('aria-label') || el.innerText || '').trim();
    if (name && re.test(name) && vis(el)) { hit = el; break; }
  }
  if (!hit && testid) hit = document.querySelector('[gv-test-id="' + testid + '"]');
  return hit ? (__CLIMB__)(hit) : '';
}"""

PHONE_RE = re.compile(r"(\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")

# JS run against the answer button: climb to the incoming-call container and
# return its text (caller name / number live there).
CALLER_TEXT_JS = """(e) => {
  // Climb from the Answer button until the container shows exactly one phone
  // number (the caller). Stop before reaching a level that holds several
  // numbers (the call-history list) and fall back to the last smaller level.
  const RE = /\\(?\\d{3}\\)?[\\s.-]?\\d{3}[\\s.-]?\\d{4}/g;
  const textOf = (n) => {
    let t = (n.innerText || '');
    for (const a of n.querySelectorAll('[aria-label],[title]'))
      t += ' ' + (a.getAttribute('aria-label') || '') + ' ' + (a.getAttribute('title') || '');
    return t;
  };
  let n = e, prev = '';
  for (let i = 0; i < 16 && n; i++) {
    const t = textOf(n);
    const nums = new Set((t.match(RE) || []).map((x) => x.replace(/\\D/g, '')));
    if (nums.size === 1) return t.slice(0, 600);
    if (nums.size > 1) return prev.slice(0, 600);
    prev = t;
    n = n.parentElement;
  }
  return prev.slice(0, 600);
}"""

CALLER_FROM_PAGE_JS = CALLER_FROM_PAGE_JS.replace("__CLIMB__", CALLER_TEXT_JS)

SNAPSHOT_JS = """() => {
  const vis = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const out = { href: location.href, buttons: [], dialogs: [], labelled: [], tags: {} };
  for (const el of document.querySelectorAll('button,[role=button],a[href]')) {
    if (!vis(el)) continue;
    out.buttons.push({ tag: el.tagName.toLowerCase(), aria: el.getAttribute('aria-label') || '',
      text: (el.innerText || '').trim().slice(0, 60), cls: (el.className && el.className.baseVal === undefined ? el.className : '').toString().slice(0, 80) });
    if (out.buttons.length > 250) break;
  }
  for (const el of document.querySelectorAll('[role=dialog],[role=alertdialog],[aria-live]')) {
    if (vis(el)) out.dialogs.push({ role: el.getAttribute('role') || 'live', text: (el.innerText || '').trim().slice(0, 300) });
  }
  for (const el of document.querySelectorAll('[aria-label]')) {
    if (!vis(el)) continue;
    out.labelled.push({ tag: el.tagName.toLowerCase(), aria: el.getAttribute('aria-label').slice(0, 80) });
    if (out.labelled.length > 300) break;
  }
  for (const el of document.querySelectorAll('*')) {
    const t = el.tagName.toLowerCase(); if (t.includes('-')) out.tags[t] = (out.tags[t] || 0) + 1;
  }
  // messaging view: compose boxes, conversation rows, message bubbles
  out.inputs = [];
  for (const el of document.querySelectorAll('textarea,input,[contenteditable=true],[role=textbox]')) {
    if (!vis(el)) continue;
    out.inputs.push({ tag: el.tagName.toLowerCase(), aria: el.getAttribute('aria-label') || '',
      ph: el.getAttribute('placeholder') || '', cls: (el.className || '').toString().slice(0, 80) });
  }
  out.items = [];
  const seen = new Set();
  for (const el of document.querySelectorAll('[role=listitem],[role=option],[role=row],[role=article],[role=log] > *,[aria-live] > *')) {
    if (!vis(el) || seen.has(el)) continue;
    seen.add(el);
    out.items.push({ tag: el.tagName.toLowerCase(), role: el.getAttribute('role') || '',
      aria: (el.getAttribute('aria-label') || '').slice(0, 120), cls: (el.className || '').toString().slice(0, 80),
      text: (el.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 160),
      parent: el.parentElement ? el.parentElement.tagName.toLowerCase() + '.' + (el.parentElement.className || '').toString().slice(0, 40) : '' });
    if (out.items.length > 150) break;
  }
  out.qm = window.__qm ? window.__qm.status() : null;
  return out;
}"""


ICON_WORDS = {"pause", "dialpad", "mic_off", "mic", "chat", "call_end", "call", "phone", "person",
              "more_vert", "close", "volume_up", "keyboard", "block", "voicemail"}


_ICON_RE = re.compile("|".join(sorted(ICON_WORDS, key=len, reverse=True)))


def _clean_label(t):
    """Voice buttons carry Material icon ligature text (call_end, mic_off …)
    glued to their labels — strip it so logs read like the screen does."""
    t = re.sub("[\\u200e\\u200f\\u202a-\\u202e]", "", t or "")
    t = re.sub(r"(?:\\b\\d ){6,}\\d\\b", " ", t)          # screen-reader spelled-out digits
    return " ".join(_ICON_RE.sub(" ", t).split())[:160]


class GVError(RuntimeError):
    pass


class CallEnded(Exception):
    pass


class GoogleVoice:
    def __init__(self, cfg, log=print):
        self.cfg = cfg
        self.log = log
        self.pw = None
        self.browser = None
        self.context = None
        self.page = None
        self.audio_frame = None
        self.audio_sink = None          # callable(np.ndarray int16 @16k) during calls
        self.last_event = {}
        self.remote_track_at = 0.0
        self.pc_states = []
        self.events = collections.deque(maxlen=200)
        self.pats = {k: re.compile(dig(cfg, f"gv.{k}", v), re.I) for k, v in DEFAULTS.items()}

    # ---------------------------------------------------------------- connect
    async def start(self):
        try:
            return await self._start()
        except Exception:
            try:
                await self.close()
            except Exception:
                pass
            raise

    async def _start(self):
        from playwright.async_api import async_playwright
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: edge.ensure_running(self.cfg, log=self.log))
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.connect_over_cdp(edge.cdp_url(self.cfg))
        self.context = self.browser.contexts[0] if self.browser.contexts else await self.browser.new_context()
        await self.context.expose_function("qmAudio", self._on_audio)
        await self.context.expose_binding("qmEvent", self._on_event)
        await self.context.add_init_script(BRIDGE_JS)
        try:
            await self.context.grant_permissions(["microphone", "notifications"],
                                                 origin="https://voice.google.com")
        except Exception:
            pass
        self.page = self._find_page()
        if self.page is None:
            self.page = await self.context.new_page()
            await self.page.goto(dig(self.cfg, "browser.url", edge.DEFAULT_URL))
        # the bridge only lands on a fresh document — reload once, never mid-call
        while await self._hangup_visible():
            self.log("a call is in progress in the Voice window — waiting for it to end before attaching")
            await asyncio.sleep(3)
        await self.page.reload(wait_until="domcontentloaded")
        await self.page.wait_for_timeout(2500)
        await self.configure_bridge()
        return self

    def _match(self):
        return dig(self.cfg, "browser.match", "voice.google.com")

    def _find_page(self):
        for p in self.context.pages:
            if self._match() in (p.url or ""):
                return p
        return None

    async def configure_bridge(self):
        monitor = bool(dig(self.cfg, "audio.monitor", False))
        for fr in self.page.frames:
            try:
                await fr.evaluate("o => window.__qm && window.__qm.config(o)", {"muteSpeakers": not monitor})
            except Exception:
                pass

    async def close(self):
        try:
            if self.browser:
                await self.browser.close()   # CDP disconnect only; Edge stays up
        except Exception:
            pass
        finally:
            self.browser = None
            if self.pw:
                pw, self.pw = self.pw, None
                await pw.stop()

    def alive(self):
        return (self.browser is not None and self.browser.is_connected()
                and self.page is not None and not self.page.is_closed())

    # ----------------------------------------------------------------- bridge
    def _on_audio(self, b64):
        sink = self.audio_sink
        if sink is None:
            return
        try:
            sink(np.frombuffer(base64.b64decode(b64), dtype=np.int16))
        except Exception as e:  # never let a sink bug kill the binding
            self.log(f"audio sink error: {e}")

    def _on_event(self, source, payload):
        try:
            ev = json.loads(payload)
        except Exception:
            return
        t = ev.get("type")
        if t in ("remote-track", "gum"):
            self.audio_frame = source.get("frame") if isinstance(source, dict) else None
        if t == "remote-track":
            self.remote_track_at = time.monotonic()
        if t == "pc-state":
            self.pc_states.append(ev.get("state"))
        self.last_event = ev
        self.events.append(ev)

    def _frame(self):
        fr = self.audio_frame
        if fr is not None and not fr.is_detached():
            return fr
        return self.page.main_frame

    async def play(self, pcm, rate):
        """Queue int16 mono PCM into the call. Returns seconds now queued."""
        b64 = base64.b64encode(np.ascontiguousarray(pcm, dtype=np.int16).tobytes()).decode()
        return await self._frame().evaluate("([b, r]) => window.__qm.play(b, r)", [b64, int(rate)])

    async def flush(self):
        try:
            await self._frame().evaluate("() => window.__qm.flush()")
        except Exception:
            pass

    async def queued(self):
        return await self._frame().evaluate("() => window.__qm.queued()")

    async def bridge_status(self):
        out = []
        for fr in self.page.frames:
            try:
                s = await fr.evaluate("() => window.__qm ? window.__qm.status() : null")
                if s:
                    out.append(s)
            except Exception:
                pass
        return out

    # --------------------------------------------------------------------- UI
    def _ui_frames(self):
        m = self._match()
        return [self.page.main_frame] + [f for f in self.page.frames
                                         if f is not self.page.main_frame and m in (f.url or "")]

    async def _button(self, key):
        pat = self.pats[key]
        for fr in self._ui_frames():
            try:
                loc = fr.get_by_role("button", name=pat)
                n = await loc.count()
                for i in range(min(n, 5)):
                    el = loc.nth(i)
                    if await el.is_visible():
                        return el
            except Exception:
                continue
        return None

    async def _hangup_visible(self):
        return (await self._button("hangup_button")) is not None

    async def state(self):
        """{'ringing': bool, 'active': bool, 'number': str|None, 'label': str}"""
        ans = await self._button("answer_button")
        if ans is not None:
            label = ""
            try:
                label = await ans.evaluate(CALLER_TEXT_JS)
            except Exception:
                pass
            if not PHONE_RE.search(label or ""):
                for fr in self._ui_frames():
                    try:
                        alt = await fr.evaluate(CALLER_FROM_PAGE_JS, [self.pats["answer_button"].pattern,
                                                                      TEST_IDS.get("answer_button", "")])
                    except Exception:
                        continue
                    if PHONE_RE.search(alt or ""):
                        label = alt
                        break
            m = PHONE_RE.search(label or "")
            return {"ringing": True, "active": False,
                    "number": m.group(0) if m else None,
                    "label": _clean_label(label)}
        active = await self._hangup_visible()
        return {"ringing": False, "active": active, "number": None, "label": ""}

    async def _press(self, key, timeout_ms=2000):
        """Click a Voice button: Playwright's real (trusted) click first, then an
        in-page click if the element keeps getting re-rendered. None = not found."""
        btn = await self._button(key)
        if btn is not None:
            try:
                await btn.click(timeout=timeout_ms)
                return "click"
            except Exception:
                pass
        for fr in self._ui_frames():
            try:
                if await fr.evaluate(CLICK_JS, [self.pats[key].pattern, TEST_IDS.get(key, "")]):
                    return "js"
            except Exception:
                continue
        return None

    async def answer(self, timeout=10.0):
        """Click Answer (re-clicking if Voice ignores it) until the call connects."""
        self.pc_states.clear()
        self.remote_track_at = 0.0
        deadline = time.monotonic() + timeout
        clicked = False
        while time.monotonic() < deadline:
            if await self._button("answer_button") is not None:
                how = await self._press("answer_button")
                if how:
                    if not clicked:
                        self.log(f"answer clicked ({how})")
                    clicked = True
            elif not clicked:
                return False            # stopped ringing before we got to it
            wait_until = min(deadline, time.monotonic() + 2.5)
            while time.monotonic() < wait_until:
                if self.remote_track_at:
                    return True
                if clicked and await self._button("answer_button") is None and await self._hangup_visible():
                    return True
                await asyncio.sleep(0.2)
        return False

    async def dtmf(self, tones):
        """Send keypad tones into the call. Returns how it was sent."""
        for fr in self.page.frames:
            try:
                if await fr.evaluate("t => window.__qm ? window.__qm.dtmf(t) : 0", tones):
                    return "rtp"
            except Exception:
                pass
        # fallback: open Voice's in-call keypad and click the digits
        for fr in self.page.frames:
            try:
                kp = fr.get_by_role("button", name=re.compile(r"(open )?keypad", re.I))
                if await kp.count():
                    await kp.first.click()
                    await asyncio.sleep(0.4)
                for d in tones:
                    await fr.get_by_role("button", name=re.compile(rf"^\\s*{re.escape(d)}\\b")).first.click(timeout=1500)
                    await asyncio.sleep(0.2)
                return "keypad"
            except Exception:
                continue
        return None

    async def hangup(self):
        await self.flush()
        for _ in range(3):
            if await self._button("hangup_button") is None:
                return True
            await self._press("hangup_button", timeout_ms=1500)
            await asyncio.sleep(0.8)
        return not await self._hangup_visible()

    async def call_over(self):
        """True when Voice says the call is gone (UI or WebRTC)."""
        if self.pc_states and self.pc_states[-1] in ("closed", "failed"):
            return True
        return not await self._hangup_visible()

    async def click_use_here(self):
        if await self._button("use_here_button") is None:
            return False
        return (await self._press("use_here_button", timeout_ms=1500)) is not None

    async def signed_in(self):
        url = self.page.url or ""
        if "accounts.google.com" in url or "ServiceLogin" in url:
            return False
        if self._match() not in url:
            return False
        return True

    async def snapshot(self):
        frames = []
        for fr in self.page.frames:
            try:
                frames.append(await fr.evaluate(SNAPSHOT_JS))
            except Exception as e:
                frames.append({"href": fr.url, "error": str(e)[:120]})
        return {"t": time.strftime("%H:%M:%S"), "frames": frames, "last_event": self.last_event}
