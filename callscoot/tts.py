"""Text-to-speech: Grok Voice (xAI /v1/tts) first when tts.backend = "grok",
then kokoro (local, natural), then piper (local, fast). Any failure falls
through to the next engine so a call never goes silent."""
import os
import re
import shutil
import subprocess
import tempfile

from .config import app_home, dig


class TTSError(RuntimeError):
    pass


def speakable(text):
    """Strip anything that reads badly aloud."""
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text or "")
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"[*_#`>|]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


_kokoro_pipe = None

GAMING_FLAG = os.path.join(app_home(), "kokoro.off")


def gaming_on():
    """True when the kokoro.off switch exists (gaming mode — use piper)."""
    return os.path.exists(GAMING_FLAG)


def unload_kokoro():
    """Drop the resident kokoro pipeline so its RAM goes back to the system."""
    global _kokoro_pipe
    if _kokoro_pipe is not None:
        _kokoro_pipe = None
        import gc
        gc.collect()
        return True
    return False


def _kokoro_synth(cfg, text):
    global _kokoro_pipe
    import wave
    import numpy as np
    from kokoro import KPipeline
    if _kokoro_pipe is None:
        _kokoro_pipe = KPipeline(lang_code=dig(cfg, "tts.kokoro_lang", "a"))
    voice = dig(cfg, "tts.kokoro_voice", "af_heart")
    rate = int(dig(cfg, "tts.kokoro_rate", 24000))
    chunks = []
    for _gs, _ps, audio in _kokoro_pipe(text, voice=voice):
        a = audio.numpy() if hasattr(audio, "numpy") else audio
        chunks.append((np.asarray(a) * 32767).astype("int16"))
    if not chunks:
        raise TTSError("kokoro produced no audio")
    audio_all = np.concatenate(chunks)
    fd, out = tempfile.mkstemp(prefix="tts_", suffix=".wav")
    os.close(fd)
    w = wave.open(out, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(rate)
    w.writeframes(audio_all.tobytes())
    w.close()
    return out


GROK_TTS_URL = "https://api.x.ai/v1/tts"


def _grok_synth(cfg, text):
    """xAI Grok Voice (token from tts.grok_token_file or XAI_API_KEY).
    Asks for raw 16 kHz s16le mono PCM and writes the WAV header here: the
    service's own WAV carries a streaming placeholder length (~18 hours)."""
    from . import net
    from .llm import provider_key
    import wave
    rate = int(dig(cfg, "tts.grok_rate", 16000))
    key = provider_key({"api_key_file": dig(cfg, "tts.grok_token_file", ""),
                        "api_key_env": "XAI_API_KEY"})
    if not key:
        raise TTSError("grok token file missing or empty")
    body = {
        "text": text[:4000],
        "voice_id": dig(cfg, "tts.grok_voice", "eve"),
        "language": dig(cfg, "tts.grok_language", "en"),
        "output_format": {"codec": "pcm", "sample_rate": rate},
    }
    audio = net.post(GROK_TTS_URL, key, json=body,
                     timeout=float(dig(cfg, "tts.grok_timeout_s", 8))).content
    if len(audio) < 3200 or audio[:4] in (b"RIFF", b"{\"er", b"<htm"):
        raise TTSError("grok tts returned no pcm audio")
    fd, out = tempfile.mkstemp(prefix="tts_", suffix=".wav")
    os.close(fd)
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(audio[: len(audio) - len(audio) % 2])
    return out


def _piper_synth(cfg, text):
    voice = dig(cfg, "tts.voice", "en_US-amy-medium")
    onnx = os.path.expanduser(f"~/.local/share/piper/{voice}.onnx")
    if not os.path.isfile(onnx):
        raise TTSError(f"piper voice not found: {onnx}")
    binary = shutil.which("piper") or os.path.expanduser("~/.local/bin/piper")
    fd, out = tempfile.mkstemp(prefix="tts_", suffix=".wav")
    os.close(fd)
    cmd = [binary, "-m", onnx, "-f", out,
           "--length-scale", str(dig(cfg, "tts.length_scale", 1.0))]
    sil = dig(cfg, "tts.sentence_silence", 0.35)
    if sil:
        cmd += ["--sentence-silence", str(sil)]
    subprocess.run(cmd, input=text.encode(), check=True, capture_output=True, timeout=90)
    return out


def cache_dir():
    d = os.path.join(app_home(), "cache", "tts")
    os.makedirs(d, exist_ok=True)
    return d


def synth_cached(cfg, text):
    """Fixed lines (greeting + notice, nudges, goodbyes) are rendered once and
    reused: zero TTS wait on those turns. Returns a temp COPY the caller may
    delete. Keyed by backend + voice + text, so a voice change re-renders."""
    import hashlib
    clean = speakable(text)
    # Every setting that changes the rendered audio is in the key.
    backend = dig(cfg, "tts.backend", "kokoro")
    key = "|".join(str(v) for v in [
        backend, dig(cfg, "tts.grok_voice", "eve"), dig(cfg, "tts.grok_rate", 16000),
        dig(cfg, "tts.grok_language", "en"), dig(cfg, "tts.kokoro_voice", "af_heart"),
        dig(cfg, "tts.kokoro_rate", 24000), dig(cfg, "tts.kokoro_lang", "a"),
        dig(cfg, "tts.voice", "en_US-amy-medium"), dig(cfg, "tts.length_scale", 1.0),
        dig(cfg, "tts.sentence_silence", 0.35), clean])
    path = os.path.join(cache_dir(), hashlib.sha256(key.encode()).hexdigest()[:20] + ".wav")
    if not os.path.isfile(path):
        wav = synth(cfg, text)
        expected = {"grok": dig(cfg, "tts.grok_rate", 16000),
                    "kokoro": dig(cfg, "tts.kokoro_rate", 24000)}.get(backend)
        if expected is not None and not _rendered_at(wav, expected):
            return wav  # a fallback voice rendered it: use it once, do not pin it
        shutil.copyfile(wav, path + ".part")
        os.replace(path + ".part", path)
        return wav
    fd, out = tempfile.mkstemp(prefix="tts_", suffix=".wav")
    os.close(fd)
    shutil.copyfile(path, out)
    return out


def _rendered_at(wav, rate):
    """True when the wav has the configured backend's sample rate (not a fallback's)."""
    import wave
    try:
        with wave.open(wav) as w:
            return w.getframerate() == int(rate)
    except Exception:
        return False


def synth(cfg, text):
    text = speakable(text)
    if not text:
        raise TTSError("nothing to synthesize")
    backend = dig(cfg, "tts.backend", "kokoro")
    if backend == "grok":
        try:
            return _grok_synth(cfg, text)
        except Exception as e:
            from . import errors
            errors.record("tts-grok", e, cfg)  # local voices keep the call alive
    if backend in ("kokoro", "grok") and not gaming_on():
        try:
            return _kokoro_synth(cfg, text)
        except Exception as e:
            from . import errors
            errors.record("tts-kokoro", e, cfg)  # piper fallback keeps the call alive
    return _piper_synth(cfg, text)
