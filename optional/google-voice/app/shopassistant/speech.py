"""STT (faster-whisper, in-memory) and streaming TTS.

TTS yields audio chunk-by-chunk so the first sentence plays while the rest is
still synthesizing. Backends, tried in order from [tts] backend:
  kokoro — natural voice, CPU, ~real-time or better
  piper  — light, needs piper.exe + a .onnx voice
  sapi   — built into every Windows install (PowerShell System.Speech); last resort
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

import numpy as np

from .audio import read_wav
from .config import dig

# whisper's favourite things to "hear" in line noise
HALLUCINATIONS = {"", "you", "thank you", "thank you.", "thanks for watching", "thanks for watching!",
                  "bye", ".", "okay", "so", "hmm", "uh", "um"}


class STT:
    def __init__(self, cfg):
        self.cfg = cfg
        self.model = None

    def _load(self):
        if self.model is None:
            from faster_whisper import WhisperModel
            threads = int(dig(self.cfg, "stt.cpu_threads", 0) or 0)
            if threads <= 0:  # physical cores, roughly; ctranslate2's own default is 4
                threads = max(4, min(8, (os.cpu_count() or 8) // 2))
            self.model = WhisperModel(
                dig(self.cfg, "stt.model", "small.en"),
                device=dig(self.cfg, "stt.device", "cpu"),
                compute_type=dig(self.cfg, "stt.compute_type", "int8"),
                cpu_threads=threads,
            )
        return self.model

    def prompt(self):
        p = dig(self.cfg, "stt.initial_prompt", "")
        shop = dig(self.cfg, "persona.shop", "")
        owner = dig(self.cfg, "persona.owner", "")
        if not p:
            p = f"Phone call to {shop}." if shop else ""
        if owner and owner.lower() not in p.lower():
            p = f"{p} Can I talk to {owner}?".strip()   # teaches the recognizer the owner's name
        return p or None

    def transcribe(self, pcm16k):
        model = self._load()
        audio = pcm16k.astype(np.float32) / 32768.0
        segments, _ = model.transcribe(
            audio,
            language=dig(self.cfg, "stt.language", "en"),
            beam_size=int(dig(self.cfg, "stt.beam_size", 3)),
            vad_filter=False,
            condition_on_previous_text=False,
            initial_prompt=self.prompt(),
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        if text.lower().strip(" .!?") in HALLUCINATIONS and pcm16k.size < 16000 * 1.2:
            return ""
        return text


def speakable(text):
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text or "")
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"[*_#`>|~]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


class TTS:
    def __init__(self, cfg, log=print):
        self.cfg = cfg
        self.log = log
        self._kpipe = None

    def backends(self):
        first = dig(self.cfg, "tts.backend", "kokoro")
        order = [first] + [b for b in ("kokoro", "piper", "sapi") if b != first]
        return order

    def stream(self, text):
        """Yield (int16 ndarray, rate). Falls through backends on failure,
        but never after audio has already been yielded."""
        text = speakable(text)
        if not text:
            return
        errors = []
        for b in self.backends():
            gen = getattr(self, f"_{b}", None)
            if gen is None:
                continue
            yielded = False
            try:
                for chunk in gen(text):
                    yielded = True
                    yield chunk
                return
            except Exception as e:
                if yielded:
                    self.log(f"tts {b} failed mid-sentence: {e}")
                    return
                errors.append(f"{b}: {e}")
        raise RuntimeError("all TTS backends failed — " + "; ".join(errors))

    def synth(self, text):
        chunks = list(self.stream(text))
        if not chunks:
            return np.zeros(0, dtype=np.int16), 16000
        rate = chunks[0][1]
        return np.concatenate([c for c, _ in chunks]), rate

    def warm(self):
        self.synth("Ready.")

    # ---- kokoro
    def _kokoro(self, text):
        if self._kpipe is None:
            from kokoro import KPipeline
            self._kpipe = KPipeline(lang_code=dig(self.cfg, "tts.kokoro_lang", "a"))
        voice = dig(self.cfg, "tts.kokoro_voice", "af_heart")
        speed = float(dig(self.cfg, "tts.speed", 1.0))
        for _gs, _ps, audio in self._kpipe(text, voice=voice, speed=speed, split_pattern=r"(?<=[.!?])\s+"):
            a = audio.numpy() if hasattr(audio, "numpy") else np.asarray(audio)
            yield (np.clip(a, -1, 1) * 32767).astype(np.int16), 24000

    # ---- piper
    def _piper(self, text):
        model = os.path.expanduser(os.path.expandvars(dig(self.cfg, "tts.piper_model", "")))
        if not model or not os.path.isfile(model):
            raise RuntimeError(f"piper voice not found: {model or '(tts.piper_model unset)'}")
        exe = dig(self.cfg, "tts.piper_exe", "") or shutil.which("piper") or shutil.which("piper.exe")
        if not exe:
            raise RuntimeError("piper executable not found")
        fd, out = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            subprocess.run([exe, "-m", model, "-f", out, "--length-scale",
                            str(dig(self.cfg, "tts.length_scale", 1.0))],
                           input=text.encode("utf-8"), check=True, capture_output=True, timeout=60)
            pcm, rate = read_wav(out)
        finally:
            _rm(out)
        yield pcm, rate

    # ---- Windows SAPI
    def _sapi(self, text):
        if sys.platform != "win32":
            raise RuntimeError("sapi is Windows-only")
        fd, out = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        fd2, txt = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd2, "w", encoding="utf-8") as f:
            f.write(text)
        ps = ("Add-Type -AssemblyName System.Speech;"
              "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
              f"$s.SetOutputToWaveFile('{out}');"
              f"$s.Speak([IO.File]::ReadAllText('{txt}'));$s.Dispose()")
        try:
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           check=True, capture_output=True, timeout=60)
            pcm, rate = read_wav(out)
        finally:
            _rm(out)
            _rm(txt)
        yield pcm, rate


def _rm(p):
    try:
        os.unlink(p)
    except OSError:
        pass
