"""The receptionist loop: keep the Voice window attached, watch for rings,
answer after the ring delay, run the call, recover from anything."""
import asyncio
import datetime
import re
import time

import numpy as np


from .call import run_call
from .config import dig, load, set_state
from .gv import GoogleVoice
from .service import beat
from .speech import STT, TTS
from .texts import TextBot


def stamp():
    return datetime.datetime.now().strftime("%H:%M:%S")


def last10(number):
    digits = re.sub(r"\D", "", number or "")
    return digits[-10:] if len(digits) >= 10 else digits


class Faults:
    """Log system problems (and show them in the control panel), at most once per hour each."""

    def __init__(self, cfg, log):
        self.cfg, self.log, self.sent = cfg, log, {}

    def raise_(self, key, text):
        now = time.monotonic()
        if now - self.sent.get(key, -1e9) < 3600:
            return
        self.sent[key] = now
        self.log(f"FAULT: {text}")
        set_state(fault=text, fault_at=time.time())

    def clear(self, key):
        if self.sent.pop(key, None) is not None:
            set_state(fault="", fault_at=0)


def should_answer(cfg, number, label):
    black = {last10(b) for b in (dig(cfg, "spam.blacklist", []) or []) if b}
    allow = {last10(a) for a in (dig(cfg, "phone.allowlist", []) or []) if a}
    if number and last10(number) in black:
        return False, "blacklisted"
    if number and last10(number) in allow:
        return True, "allowlisted"
    if not number and label and re.search(r"[A-Za-z]{2,}", label) and not dig(cfg, "phone.answer_contacts", True):
        return False, "saved contact (answer_contacts = false)"
    if not dig(cfg, "phone.answer_unknown", True):
        return False, "not on allowlist"
    return True, "receptionist mode"


async def watch(cfg, answer=True, log=None, reload=False):
    log = log or (lambda m: print(f"[{stamp()}] {m}", flush=True))
    loop = asyncio.get_running_loop()
    faults = Faults(cfg, log)
    set_state(fault="", fault_at=0)                      # a fresh start has no known problems yet
    stt, tts = STT(cfg), TTS(cfg, log=log)
    ring_delay = float(dig(cfg, "call.ring_delay_s", 8))

    log("loading speech models…")
    try:
        await loop.run_in_executor(None, tts.warm)
        await loop.run_in_executor(None, stt.transcribe, np.zeros(16000, dtype=np.int16))
        log("models ready")
    except Exception as e:
        log(f"warmup problem (will retry on first call): {e}")

    gv = None
    texts = None
    ring_seen = None
    ring_skip = None
    signed_in_noted = False
    ring_last = 0.0
    ring_gap = float(dig(cfg, "call.ring_gap_s", 2.0))  # Voice re-renders the ring card; ignore brief gaps
    while True:
        beat(cfg)
        try:
            if gv is None or not gv.alive():
                if gv is not None:
                    try:
                        await gv.close()
                    except Exception:
                        pass
                gv = await GoogleVoice(cfg, log=log).start()
                texts = TextBot(cfg, gv, log=log)
                faults.clear("attach")
                set_state(voice_attached_at=time.time())
                log(f"attached to Google Voice — {'answering after ' + str(ring_delay) + 's' if answer else 'WATCH ONLY (not answering)'}")
            if not await gv.signed_in():
                faults.raise_("signin", "the Google Voice window is signed out - use Sign in to Google Voice in the control panel")
                await asyncio.sleep(30)
                continue
            faults.clear("signin")
            if not signed_in_noted:
                signed_in_noted = True
                set_state(voice_signed_in_at=time.time())
            if await gv.click_use_here():
                log("clicked 'Use here' (Voice was open elsewhere)")
            st = await gv.state()
            if not st["ringing"] and ring_seen is not None and time.monotonic() - ring_last < ring_gap:
                await asyncio.sleep(0.3)
                continue
            if not st["ringing"]:
                if ring_seen is not None:
                    log("ringing stopped")
                ring_seen = ring_skip = None
                if texts is not None and not st["active"]:
                    try:
                        await texts.poll()
                    except Exception as e:
                        log(f"sms: poll error {type(e).__name__}: {e}")
                await asyncio.sleep(0.5)
                continue
            ring_last = time.monotonic()
            num = st["number"] or "unknown"
            if ring_seen is None:
                ring_seen = time.monotonic()
                if reload:                                    # pick up changes saved in the control panel
                    cfg = load()
                    ring_delay = float(dig(cfg, "call.ring_delay_s", 8))
                ok, why = should_answer(cfg, st["number"], st["label"])
                ring_skip = None if ok else why
                log(f"RINGING {num} — {st['label'][:90]!r} — {'will answer' if ok and answer else 'not answering: ' + (why if not ok else 'watch only')}")
            if ring_skip or not answer or time.monotonic() - ring_seen < ring_delay:
                await asyncio.sleep(0.3)
                continue
            ok, why = should_answer(cfg, st["number"], st["label"])  # number often renders late
            if not ok:
                log(f"not answering {num}: {why}")
                ring_skip = why
                continue
            if not await gv.answer():
                log("clicked answer but the call never connected")
                ring_seen = None
                continue
            log(f"answered {num}")
            await run_call(cfg, gv, stt, tts, num, log=log)
            ring_seen = None
            log("idle")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"error: {type(e).__name__}: {e}")
            if gv is None:                                    # could not open or attach to the window at all
                faults.raise_("attach", str(e))
                await asyncio.sleep(20)
                continue
            if not gv.alive():
                faults.raise_("attach", f"lost the Google Voice window ({e}); reconnecting")
            await asyncio.sleep(3)
