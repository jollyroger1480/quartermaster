"""Text-to-speech via piper (local, fast)."""
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


def synth(cfg, text):
    text = speakable(text)
    if not text:
        raise TTSError("nothing to synthesize")
    if dig(cfg, "tts.backend", "kokoro") == "kokoro" and not gaming_on():
        try:
            return _kokoro_synth(cfg, text)
        except Exception as e:
            from . import errors
            errors.record("tts-kokoro", e, cfg)  # piper fallback keeps the call alive
    return _piper_synth(cfg, text)
