"""One phone call, full duplex: greet, listen, think, speak - with barge-in,
streamed speech, and a saved transcript, summary and recording."""
import asyncio
import datetime
import os
import re
import tempfile
import threading
import time

import numpy as np

from . import brain, llm
from .audio import CallerAudio, blip, resample, write_wav
from .config import dig, line, logs_dir, owner_name, set_state
from .gv import CallEnded


def _archive(wav_path, dest_no_ext):
    """Keep recordings small: opus if ffmpeg is installed, otherwise the plain wav."""
    import shutil
    import subprocess
    if shutil.which("ffmpeg"):
        dest = dest_no_ext + ".ogg"
        try:
            p = subprocess.run(["ffmpeg", "-y", "-i", wav_path, "-c:a", "libopus", "-b:a", "24k", "-ac", "1", dest],
                               capture_output=True, timeout=60)
            if p.returncode == 0 and os.path.isfile(dest):
                return dest
        except (OSError, subprocess.TimeoutExpired):
            pass
    dest = dest_no_ext + ".wav"
    shutil.copyfile(wav_path, dest)
    return dest

# Google Voice call screening, played to whoever answers: "Call from X. To accept, press 1."
SCREEN_RE = re.compile(
    r"accept,?\s*press\s*(1|one)|press\s*(1|one)\s*to accept"
    r"|press\s*(1|one)\b.{0,60}(voice\s*mail|press\s*(2|two))", re.I)


CLOSING_WORDS = {"bye", "goodbye", "good", "okay", "ok", "alright", "all", "right", "thanks", "thank", "you",
                 "have", "a", "nice", "great", "day", "one", "night", "see", "ya", "later", "talk", "to", "that's",
                 "thats", "it", "sounds", "cool", "appreciate", "yep", "yeah", "yes", "sir", "ma'am", "take", "care", "bye-bye"}


def is_goodbye(text):
    """ "Okay, thanks, bye" ends the call; "Bye, email." (misheard "by email") does not."""
    words = re.findall(r"[a-z'-]+", (text or "").lower())
    return bool(words) and any(w in ("bye", "goodbye", "bye-bye") for w in words) and all(w in CLOSING_WORDS for w in words)


TRAILING_FILLER = {"and", "uh", "um", "the", "a", "an", "my", "it's", "its", "is", "for", "to", "of",
                   "with", "so", "but", "or", "on", "in", "i", "it", "that", "like", "need", "want", "got"}


def looks_unfinished(text):
    """Callers read things out in pieces ("2011... Chevy... Colorado... 2.9") and
    pause mid-sentence. Short fragments, trailing numbers or filler words mean
    they probably aren't done yet."""
    t = (text or "").strip()
    if not t:
        return True
    words = re.findall(r"[\w'.-]+", t)
    last = re.sub(r"[^\w']", "", words[-1].lower()) if words else ""
    return (len(words) <= 3 or bool(re.search(r"\d[\s.,-]*$", t)) or last in TRAILING_FILLER
            or t.endswith((",", "-", "—", "...")))


def _session_dir(cfg, number):
    root = logs_dir(cfg)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    digits = re.sub(r"\D", "", number or "")[-10:] or "unknown"
    d = os.path.join(root, f"call_{stamp}_{digits}")
    os.makedirs(d, exist_ok=True)
    return d


def _save(session_dir, transcript, flagged, summary, caller_pcm, bot_pcm):
    path = os.path.join(session_dir, "transcript.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {os.path.basename(session_dir)}\n\nescalation: {'YES' if flagged else 'no'}\n\n")
        f.write(f"## summary\n\n{summary}\n\n## transcript\n\n")
        for who, text in transcript:
            f.write(f"**{who}:** {text}\n\n")
    for name, pcm in (("caller", caller_pcm), ("bot", bot_pcm)):
        if pcm.size:
            fd, tmp = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            write_wav(tmp, pcm, 16000)
            try:
                _archive(tmp, os.path.join(session_dir, name))
            finally:
                os.unlink(tmp)
    return path


async def run_call(cfg, gv, stt, tts, number, log=print):
    loop = asyncio.get_running_loop()
    owner = owner_name(cfg)
    greeting, farewell = line(cfg, "greeting"), line(cfg, "farewell")
    voicemail, spam_reply, no_audio = line(cfg, "voicemail_reply"), line(cfg, "spam_reply"), line(cfg, "no_audio_reply")
    max_dur = float(dig(cfg, "call.max_duration_s", 600))
    max_turns = int(dig(cfg, "call.max_turns", 30))
    nudge_s = float(dig(cfg, "call.nudge_after_s", 7))

    caller = CallerAudio(cfg)
    gv.audio_sink = caller.feed
    session_dir = _session_dir(cfg, number)
    transcript = [("meta", f"call from {number} at {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")]
    history, bot_audio = [], []
    flagged = False
    t0 = time.monotonic()

    async def over():
        return await gv.call_over()

    async def say(text, interruptible=True):
        """Stream speech into the call. True = finished, False = caller barged in."""
        caller.barge.clear()
        q = asyncio.Queue()
        stop = threading.Event()

        def produce():
            try:
                for chunk in tts.stream(text):
                    if stop.is_set():
                        break
                    loop.call_soon_threadsafe(q.put_nowait, chunk)
            except Exception as e:
                loop.call_soon_threadsafe(q.put_nowait, e)
            finally:
                loop.call_soon_threadsafe(q.put_nowait, None)

        loop.run_in_executor(None, produce)
        caller.bot_speaking = True
        try:
            while True:
                get = asyncio.ensure_future(q.get())
                waits = {get}
                barge = None
                if interruptible:
                    barge = asyncio.ensure_future(caller.barge.wait())
                    waits.add(barge)
                done, _ = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
                if barge is not None and barge in done:
                    get.cancel()
                    stop.set()
                    await gv.flush()
                    return False
                if barge is not None:
                    barge.cancel()
                item = get.result()
                if item is None:
                    break
                if isinstance(item, Exception):
                    log(f"tts error: {item}")
                    break
                pcm, rate = item
                bot_audio.append(resample(pcm, rate, 16000))
                await gv.play(pcm, rate)
            while True:  # let the queued audio drain
                left = await gv.queued()
                if left <= 0.02:
                    break
                if interruptible and caller.barge.is_set():
                    await gv.flush()
                    return False
                if await over():
                    raise CallEnded()
                await asyncio.sleep(min(0.15, left))
            await asyncio.sleep(0.25)  # far-end echo tail
            return True
        finally:
            stop.set()
            caller.bot_speaking = False

    async def listen(limit):
        start = time.monotonic()
        while True:
            try:
                return await asyncio.wait_for(caller.utterances.get(), 0.5)
            except asyncio.TimeoutError:
                pass
            if await over():
                raise CallEnded()
            if time.monotonic() - start > limit and not caller.in_speech:
                return None

    grace_short = float(dig(cfg, "call.turn_grace_s", 0.5))
    grace_long = float(dig(cfg, "call.turn_grace_long_s", 1.4))

    async def resumed(grace):
        """Wait up to `grace` s for the caller to start talking again."""
        end = time.monotonic() + grace
        while time.monotonic() < end:
            if caller.in_speech or not caller.utterances.empty():
                return True
            if await over():
                raise CallEnded()
            await asyncio.sleep(0.05)
        return caller.in_speech or not caller.utterances.empty()

    async def gather_turn(utt):
        """Transcribe, then keep the turn open while the caller is mid-thought,
        merging the pieces into one message. Returns (text, audio_s, stt_s)."""
        parts, audio_s, stt_s = [], 0.0, 0.0
        for _ in range(6):
            audio_s += utt.size / 16000
            t = time.monotonic()
            piece = await loop.run_in_executor(None, stt.transcribe, utt)
            stt_s += time.monotonic() - t
            if piece:
                parts.append(piece)
            joined = " ".join(parts)
            if not await resumed(grace_long if looks_unfinished(joined) else grace_short):
                break
            nxt = await listen(15)
            if nxt is None:
                break
            utt = nxt
        return " ".join(parts), audio_s, stt_s

    async def speak_line(text, **kw):
        transcript.append(("agent", text))
        log(f"agent: {text}")
        return await say(text, **kw)

    async def accept_screening(text):
        how = await gv.dtmf("1")
        log(f"Voice call screening — pressed 1 ({how or 'FAILED'})")
        transcript.append(("note", f"Voice screening prompt — pressed 1 via {how}"))
        await asyncio.sleep(1.2)
        caller.reset_pending()
        return how is not None

    try:
        await asyncio.sleep(float(dig(cfg, "call.answer_delay_ms", 700)) / 1000)
        # Voice may play its screening prompt the moment we pick up — listen
        # briefly before greeting so we can press 1 instead of talking over it.
        t_listen = time.monotonic()
        while time.monotonic() - t_listen < float(dig(cfg, "call.pre_greet_listen_s", 2.0)):
            if caller.in_speech or not caller.utterances.empty():
                utt = await listen(10)
                if utt is not None:
                    first = await loop.run_in_executor(None, stt.transcribe, utt)
                    if SCREEN_RE.search(first or ""):
                        await accept_screening(first)
                    elif first:
                        log(f"caller (before greeting): {first}")
                break
            if await over():
                raise CallEnded()
            await asyncio.sleep(0.1)
        await speak_line(greeting)
        if caller.frames_in == 0:
            await asyncio.sleep(1.5)
        if caller.frames_in == 0:
            log("NO CALLER AUDIO reached the bot — the bridge did not see Voice's audio (use Probe in the control panel)")
            transcript.append(("note", "no caller audio reached the bot"))
            await speak_line(no_audio, interruptible=False)
            return transcript, flagged
        empty = turns = 0
        slow_warned = False
        while time.monotonic() - t0 < max_dur and turns < max_turns:
            utt = await listen(nudge_s)
            text = ""
            audio_s = stt_s = 0.0
            if utt is not None:
                text, audio_s, stt_s = await gather_turn(utt)
                if stt_s > 3 and stt_s > 0.6 * audio_s and not slow_warned:
                    slow_warned = True
                    log(f"  SLOW SPEECH RECOGNITION: {stt_s:.1f}s for {audio_s:.1f}s of audio - run the speed test in the control panel")
            if not text:
                empty += 1
                if empty == 1:
                    transcript.append(("note", "no speech — nudged"))
                    await speak_line("Are you still there?")
                    continue
                await speak_line(voicemail, interruptible=False)
                break
            if SCREEN_RE.search(text):
                await accept_screening(text)
                await speak_line(greeting)
                continue
            empty = 0
            turns += 1

            if dig(cfg, "spam.hangup_on_scam", True) and brain.SPAM_RE.search(text):
                transcript.append(("caller", text))
                transcript.append(("note", "SPAM script detected — declined"))
                await speak_line(spam_reply, interruptible=False)
                break
            if is_goodbye(text):
                transcript.append(("caller", text))
                await speak_line(farewell, interruptible=False)
                break
            if dig(cfg, "call.thinking_cue", True):
                await gv.play(blip(), 16000)
            # Think; if the caller keeps talking while we think, fold that in and think again
            # instead of answering the half-sentence.
            for _ in range(3):
                t_llm = time.monotonic()
                try:
                    reply, provider, esc = await loop.run_in_executor(None, brain.reply, cfg, history, text)
                except llm.LLMError as e:
                    log(f"llm error: {e}")
                    reply, provider, esc = (f"Sorry, I didn't catch that on my end. "
                                            f"I'll make sure {owner} gets your message."), "none", False
                if not (caller.in_speech or not caller.utterances.empty()):
                    break
                more_utt = await listen(15)
                if more_utt is None:
                    break
                more, a2, s2 = await gather_turn(more_utt)
                audio_s, stt_s = audio_s + a2, stt_s + s2
                if not more:
                    break
                text = f"{text} {more}"
                log(f"  (caller kept talking — re-thinking: {more!r})")
            transcript.append(("caller", text))
            log(f"caller: {text}   ({audio_s:.1f}s audio, stt {stt_s:.1f}s)")
            flagged |= esc
            history += [{"role": "user", "content": text}, {"role": "assistant", "content": reply}]
            log(f"  [{provider} {time.monotonic() - t_llm:.1f}s]")
            finished = await speak_line(reply)
            if not finished:
                transcript.append(("note", "caller interrupted"))
                log("  (caller interrupted — listening)")
    except CallEnded:
        transcript.append(("note", "caller hung up"))
        log("caller hung up")
    finally:
        gv.audio_sink = None
        try:
            if not await gv.call_over():
                await gv.hangup()
                log("hung up")
        except Exception as e:
            log(f"hangup problem: {e}")
        summary = await loop.run_in_executor(None, brain.summarize, cfg, number, transcript)
        bot_pcm = np.concatenate(bot_audio) if bot_audio else np.zeros(0, dtype=np.int16)
        path = await loop.run_in_executor(None, _save, session_dir, transcript, flagged, summary,
                                          caller.full_recording(), bot_pcm)
        log(f"transcript: {path}")
        set_state(last_call_at=time.time())
    return transcript, flagged
