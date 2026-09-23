"""Audio plumbing: capture the caller and play TTS into the call.

Two backends:
- "bluealsa" (primary): bluez-alsa owns HFP; capture/play via arecord/aplay on
  the bluealsa ALSA PCM. SCO is narrowband (8k CVSD / 16k mSBC) so TTS wav is
  resampled with ffmpeg before playback and recordings are upsampled for STT.
- "pipewire" (legacy fallback): HFP nodes via pw-record/pw-play. Not usable for
  phone-HF on pipewire 1.6.x (see vault pc/2026-09-22 note) — kept for BT
  headphones and future pipewire fixes.
"""
import array
import math
import os
import shutil
import subprocess
import tempfile
import time
import wave

from .config import app_home, dig


class AudioError(RuntimeError):
    pass


HFP_PROFILES = [
    "headset-head-unit", "headset-head-unit-msbc",
    "headset_audio_gateway", "hfp-hf", "hfp-ag", "headset",
]



def _run(cmd, timeout=15):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def backend(cfg):
    return dig(cfg, "audio.backend", "pipewire")


def bluealsa_pcm(cfg):
    return dig(cfg, "audio.bluealsa_pcm", "")


def _sco_rate(cfg):
    return int(dig(cfg, "audio.sco_rate", 16000))


_bluealsa_ok_cache = None  # None = unchecked; True/False cached after first success


def bluealsa_ready(cfg):
    """True if the bluealsa HFP capture PCM for the phone exists."""
    global _bluealsa_ok_cache
    pcm = bluealsa_pcm(cfg)
    if not pcm:
        return False
    mac = pcm.split("DEV=")[-1].split(",")[0]          # your phone MAC (see callscoot.example.toml)
    want = "dev_" + mac.replace(":", "_").lower()      # BlueZ object path form
    for cli in ("bluealsactl", "bluealsa-cli"):
        if shutil.which(cli):
            p = _run([cli, "list-pcms"], timeout=8)
            if p.returncode == 0:
                _bluealsa_ok_cache = want in p.stdout.lower() and "hfp" in p.stdout.lower()
                return _bluealsa_ok_cache
    if _bluealsa_ok_cache is not None:
        return _bluealsa_ok_cache  # D-Bus hiccup — trust the last known state
    return False


def bt_card():
    p = _run(["pactl", "list", "short", "cards"])
    for line in p.stdout.splitlines():
        parts = line.split("\t")
        if parts and parts[0].startswith("bluez_card."):
            return parts[0]
    return None


def set_hfp_profile(card=None):
    """Pipewire-backend only (bluealsa handles its own HFP)."""
    if backend({}) == "bluealsa":
        return "bluealsa"
    card = card or bt_card()
    if not card:
        return None
    for prof in HFP_PROFILES:
        if _run(["pactl", "set-card-profile", card, prof]).returncode == 0:
            return prof
    return None


def _nodes(kind):
    p = _run(["pactl", "list", "short", kind])
    return [l.split("\t")[1] for l in p.stdout.splitlines() if "\t" in l]


def bt_sink(cfg=None):
    if backend(cfg or {}) == "bluealsa":
        return bluealsa_pcm(cfg)
    override = dig(cfg or {}, "audio.bt_sink", "")
    if override:
        return override
    for n in _nodes("sinks"):
        if n.startswith("bluez_sink."):
            return n
    return None


def bt_source(cfg=None):
    if backend(cfg or {}) == "bluealsa":
        return bluealsa_pcm(cfg)
    override = dig(cfg or {}, "audio.bt_source", "")
    if override:
        return override
    for n in _nodes("sources"):
        if n.startswith("bluez_source."):
            return n
    return None


def wait_for_hfp(cfg, timeout=15):
    """Make sure the HFP audio path exists; returns (sink, source)."""
    if backend(cfg) == "bluealsa":
        set_hfp_profile()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if bluealsa_ready(cfg):
                pcm = bluealsa_pcm(cfg)
                return pcm, pcm
            time.sleep(1.0)
        raise AudioError(
            "bluealsa HFP PCM not found — is the phone connected with Phone "
            "audio enabled? Check: bluealsactl list-pcms"
        )
    set_hfp_profile()
    deadline = time.monotonic() + timeout
    sink = source = None
    while time.monotonic() < deadline:
        sink = bt_sink(cfg)
        source = bt_source(cfg)
        if sink and source:
            return sink, source
        set_hfp_profile()
        time.sleep(1.0)
    raise AudioError(
        "Bluetooth HFP nodes missing "
        f"(sink={sink!r} source={source!r}). Make a test call and force "
        "'Headset Head Unit (HFP)' in pavucontrol → Configuration."
    )


def _record_cmd(cfg, source, rate):
    if backend(cfg) == "bluealsa":
        return ["arecord", "-D", source, "-t", "raw", "-f", "S16_LE",
                "-r", str(rate), "-c", "1", "-q"]
    return ["pw-record", "--raw", "--format", "s16", "--rate", str(rate),
            "--channels", "1", "--target", source, "-"]


def record_utterance(cfg, source=None):
    """Record until the caller stops talking. Returns {'path','seconds'} or None."""
    rate = dig(cfg, "audio.sample_rate", 16000)
    max_s = dig(cfg, "audio.record_max_s", 14)
    silence_s = dig(cfg, "audio.record_silence_s", 1.35)
    min_s = dig(cfg, "audio.record_min_s", 0.45)
    source = source or bt_source(cfg)
    if not source:
        raise AudioError("No Bluetooth source node — is the phone on HFP right now?")

    if backend(cfg) != "bluealsa" and not shutil.which("pw-record"):
        cmd = ["parecord", "--raw", "--format=s16le", f"--rate={rate}",
               "--channels=1", f"--device={source}", "-"]
    else:
        cmd = _record_cmd(cfg, source, rate)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    chunk_bytes = (rate // 20) * 2  # 50 ms of s16 mono
    pcm = array.array("h")
    thresh = None
    floor, early = [], []
    speech_start = None
    last_voice = None
    t0 = time.monotonic()
    try:
        while time.monotonic() - t0 < max_s + 2:
            raw = proc.stdout.read(chunk_bytes)
            if not raw:
                break
            if len(raw) < chunk_bytes:
                continue
            elapsed = time.monotonic() - t0
            a = array.array("h")
            a.frombytes(raw)
            pcm.extend(a)
            rms = math.sqrt(sum(x * x for x in a) / len(a))
            if thresh is None:
                floor.append(rms)
                if elapsed >= 0.35 and len(floor) >= 5:
                    med = sorted(floor)[len(floor) // 2]
                    thresh = max(med * 2.5 + 80, dig(cfg, "audio.vad_min_rms", 240))
                    for te, tr in early:  # replay calibration window in case speech started
                        if tr > thresh:
                            speech_start, last_voice = te, te
                            break
                else:
                    early.append((elapsed, rms))
                continue
            if rms > thresh:
                if speech_start is None:
                    speech_start = elapsed
                last_voice = elapsed
            elif speech_start is not None:
                if elapsed - last_voice > silence_s and last_voice - speech_start >= min_s:
                    break
                if elapsed - last_voice > max(2 * silence_s, 2.5):
                    break
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except Exception:
            proc.kill()

    if speech_start is None:
        return None
    start_idx = max(0, int((speech_start - 0.3) * rate))
    trimmed = pcm[start_idx:]
    if len(trimmed) < rate * 0.2:
        return None
    fd, path = tempfile.mkstemp(prefix="utt_", suffix=".wav")
    os.close(fd)
    w = wave.open(path, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(rate)
    w.writeframes(trimmed.tobytes())
    w.close()
    path = _to_16k_if_needed(cfg, path, rate)
    return {"path": path, "seconds": len(trimmed) / rate}


def _to_16k_if_needed(cfg, path, rate):
    """Whisper wants 16 kHz; SCO CVSD is 8 kHz. Resample only when needed."""
    if rate >= 16000 or not shutil.which("ffmpeg"):
        return path
    up = path.replace(".wav", "_16k.wav")
    p = _run(["ffmpeg", "-y", "-i", path, "-ar", "16000", "-ac", "1", up], timeout=30)
    if p.returncode == 0 and os.path.isfile(up):
        os.unlink(path)
        return up
    return path


def thinking_wav(cfg):
    """A soft two-tone 'thinking' blip, generated once and cached."""
    import math
    import wave as _wave
    path = os.path.join(app_home(), "knowledge", "thinking.wav")
    if os.path.isfile(path):
        return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rate = _sco_rate(cfg)
    tones = [(660, 0.09), (880, 0.12)]  # gentle rising two-tone
    frames = []
    for freq, dur in tones:
        n = int(rate * dur)
        for i in range(n):
            env = math.sin(math.pi * i / n)  # fade in/out, no clicks
            v = math.sin(2 * math.pi * freq * i / rate) * 0.22 * env
            frames.append(int(v * 32767))
    w = _wave.open(path, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(rate)
    w.writeframes(b"".join(int(v).to_bytes(2, "little", signed=True) for v in frames))
    w.close()
    return path


def play_to_sink(cfg, wav_path, sink=None):
    sink = sink or bt_sink(cfg)
    if not sink:
        raise AudioError("No Bluetooth sink node — HFP not active.")
    try:
        if backend(cfg) == "bluealsa":
            rate = _sco_rate(cfg)
            p = _run(["aplay", "-D", sink, "-q", wav_path], timeout=30)
            if p.returncode != 0 and shutil.which("ffmpeg"):
                # sample-rate mismatch (SCO is 8k CVSD / 16k mSBC) — convert and retry
                conv = wav_path.replace(".wav", f"_{rate}.wav")
                c = _run(["ffmpeg", "-y", "-i", wav_path, "-ar", str(rate),
                          "-ac", "1", conv], timeout=60)
                if c.returncode == 0:
                    p = _run(["aplay", "-D", sink, "-q", conv], timeout=30)
                    try:
                        os.unlink(conv)
                    except OSError:
                        pass
            if p.returncode != 0:
                raise AudioError(f"bluealsa playback failed: {p.stderr.strip()[:200]}")
            return
        p = _run(["pw-play", "--target", sink, wav_path], timeout=30)
        if p.returncode != 0:
            p = _run(["paplay", "--device=" + sink, wav_path], timeout=30)
        if p.returncode != 0:
            raise AudioError(f"playback failed: {p.stderr.strip()[:200]}")
    except subprocess.TimeoutExpired:
        raise AudioError("playback timed out — call audio link probably dropped")
