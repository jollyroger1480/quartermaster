"""Speech-to-text via faster-whisper (CPU, int8)."""
from .config import dig

_model = None


def transcribe(cfg, wav_path):
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
        vad_filter=True,
    )
    return " ".join(s.text.strip() for s in segments).strip()
