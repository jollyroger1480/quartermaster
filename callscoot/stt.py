"""Speech-to-text. stt.backend = "grok" uses xAI Grok STT (/v1/stt, same
token file as Grok TTS, keep-alive session) and falls back to local
faster-whisper (CPU, int8) on any failure; "whisper" is local only.

Whisper stays loaded once warm. Its own VAD filter is off by default: the
recorder already trims silence, and the extra pass cost seconds on a busy CPU."""
from .config import dig

GROK_STT_URL = "https://api.x.ai/v1/stt"
_model = None


def _grok(cfg, wav_path):
    from . import net
    from .llm import provider_key
    key = provider_key({"api_key_file": dig(cfg, "stt.grok_token_file", dig(
        cfg, "tts.grok_token_file", "")), "api_key_env": "XAI_API_KEY"})
    if not key:
        raise OSError("grok token file missing or empty")
    with open(wav_path, "rb") as f:
        r = net.post(GROK_STT_URL, key, timeout=float(dig(cfg, "stt.grok_timeout_s", 6)),
                     files={"file": ("utterance.wav", f, "audio/wav")},
                     data={"model": dig(cfg, "stt.grok_model", "grok-voice-transcribe-2.0"),
                           "language": dig(cfg, "stt.language", "en"), "format": "true"})
    return (r.json().get("text") or "").strip()


def whisper(cfg, wav_path):
    global _model
    from faster_whisper import WhisperModel
    if _model is None:
        _model = WhisperModel(
            dig(cfg, "stt.model", "base.en"), device="cpu", compute_type="int8"
        )
    segments, _info = _model.transcribe(
        wav_path,
        language=dig(cfg, "stt.language", "en"),
        beam_size=1,
        vad_filter=bool(dig(cfg, "stt.vad_filter", False)),
        without_timestamps=True,
    )
    return " ".join(s.text.strip() for s in segments).strip()


def transcribe(cfg, wav_path):
    if dig(cfg, "stt.backend", "whisper") == "grok":
        try:
            return _grok(cfg, wav_path)
        except Exception as e:
            from . import errors
            errors.record("stt-grok", e, cfg)  # local whisper keeps the call alive
    return whisper(cfg, wav_path)
