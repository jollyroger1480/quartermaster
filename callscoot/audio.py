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
import select
import shutil
import subprocess
import tempfile
import threading
import time
import wave

from . import errors
from .config import app_home, dig


class AudioError(RuntimeError):
    pass


HFP_PROFILES = [
    "headset-head-unit", "headset-head-unit-msbc",
    "headset_audio_gateway", "hfp-hf", "hfp-ag", "headset",
]

SPEAK = {}


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
    mac = pcm.split("DEV=")[-1].split(",")[0]          # B0:C2:C7:C2:F5:9D
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


def set_hfp_profile(cfg=None, card=None):
    """Switch the PipeWire card to an HFP profile.

    cfg is first: the watch loop passes the config dict, and a dict in the
    card slot makes pactl throw and drops the call. BlueALSA owns its own
    SCO link, so that backend returns without touching the card.
    """
    if backend(cfg or {}) == "bluealsa":
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
        set_hfp_profile(cfg)
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
    set_hfp_profile(cfg)
    deadline = time.monotonic() + timeout
    sink = source = None
    while time.monotonic() < deadline:
        sink = bt_sink(cfg)
        source = bt_source(cfg)
        if sink and source:
            return sink, source
        set_hfp_profile(cfg)
        time.sleep(1.0)
    raise AudioError(
        "Bluetooth HFP nodes missing "
        f"(sink={sink!r} source={source!r}). Make a test call and force "
        "'Headset Head Unit (HFP)' in pavucontrol → Configuration."
    )


class LiveListen:
    """Play the call on this PC. Caller PCM and bot wavs only — the mic stays out.

    The SCO capture is already owned by record_utterance during a call, so
    caller audio is fed in from there. Bot wavs are copied and played beside
    the phone playback. Headphones avoid the phone hearing the room.
    """

    def __init__(self):
        self.on = False
        self.error = ""
        self._proc = None
        self._lock = threading.Lock()

    def set(self, cfg, on):
        with self._lock:
            if on and not self.on:
                self._open(cfg)
                self.on = self._proc is not None
            elif not on:
                self._close()
                self.on = False
        if on and not self.on:
            errors.info(f"listen failed: {self.error or 'no player'}")
            return f"listen failed ({self.error or 'no player'})"
        errors.info("listen on" if self.on else "listen off")
        return "listening — headphones, mic stays off the call" if self.on else "listen off"

    def _open(self, cfg):
        rate = int(dig(cfg, "audio.sample_rate", 16000))
        if not shutil.which("paplay"):
            self.error = "paplay missing"
            self._proc = None
            return
        self._proc = subprocess.Popen(
            ["paplay", "--raw", "--format=s16le", f"--rate={rate}",
             "--channels=1", "--latency-msec=60"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.error = ""

    def _close(self):
        proc = self._proc
        self._proc = None
        if not proc:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
        except Exception:
            pass

    def feed(self, raw):
        """Caller audio from the SCO read loop. Drops the player if the pipe breaks."""
        if not raw:
            return
        with self._lock:
            proc = self._proc if self.on else None
            if not proc or not proc.stdin:
                return
            try:
                proc.stdin.write(raw)
            except Exception:
                self.error = "playback stopped"
                self.on = False
                self._close()

    def play_copy(self, wav_path):
        """Play one bot wav locally without blocking the phone playback."""
        if not self.on or not wav_path or not os.path.isfile(wav_path):
            return
        if not shutil.which("paplay"):
            return
        fd, dest = tempfile.mkstemp(prefix="listen_", suffix=".wav")
        os.close(fd)
        try:
            shutil.copyfile(wav_path, dest)
        except OSError:
            return

        def _run():
            try:
                subprocess.run(["paplay", dest], timeout=30,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
            finally:
                try:
                    os.unlink(dest)
                except OSError:
                    pass

        threading.Thread(target=_run, name="listen-bot", daemon=True).start()


live = LiveListen()


def _record_cmd(cfg, source, rate):
    if backend(cfg) == "bluealsa":
        return ["arecord", "-D", source, "-t", "raw", "-f", "S16_LE",
                "-r", str(rate), "-c", "1", "-q"]
    return ["pw-record", "--raw", "--format", "s16", "--rate", str(rate),
            "--channels", "1", "--target", source, "-"]


def _read_chunk(stream, n, timeout):
    """Bytes from a capture pipe, b'' on EOF, None when nothing arrived.

    stdout.read() waits for a full chunk. A stalled SCO link never delivers
    one, so the utterance clock below never runs and arecord stays open
    after the caller is gone.
    """
    if stream is None:
        return b""
    ready, _, _ = select.select([stream], [], [], max(0.0, timeout))
    if not ready:
        return None
    return os.read(stream.fileno(), n)


def record_utterance(cfg, source=None, session_sink=None):
    """Record until the caller stops talking. Returns {'path','seconds'} or None.

    session_sink: optional callable(raw_bytes) — every real (split) PCM chunk is
    also handed to it while the VAD loop runs, feeding the session-wide
    recorder. The sink stamps its own wall-clock offsets; this function stays
    agnostic.
    """
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
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
    )
    chunk_bytes = (rate // 20) * 2  # 50 ms of s16 mono
    pcm = array.array("h")
    thresh = None
    floor, early = [], []
    speech_start = None
    last_voice = None
    pending = b""
    t0 = time.monotonic()
    limit = max_s + 2
    try:
        while time.monotonic() - t0 < limit:
            remain = limit - (time.monotonic() - t0)
            got = _read_chunk(proc.stdout, chunk_bytes, min(0.25, remain))
            if got is None:
                continue
            if not got:
                break
            pending += got
            if len(pending) < chunk_bytes:
                continue
            raw, pending = pending[:chunk_bytes], pending[chunk_bytes:]
            if session_sink is not None:
                try:
                    session_sink(raw)
                except Exception:
                    pass
            try:
                live.feed(raw)
            except Exception:
                pass
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
    try:
        live.play_copy(wav_path)
    except Exception:
        pass
    sink = sink or bt_sink(cfg)
    if not sink:
        raise AudioError("No Bluetooth sink node — HFP not active.")
    try:
        if backend(cfg) == "bluealsa":
            rate = _sco_rate(cfg)
            if shutil.which("ffmpeg"):
                conv = wav_path.replace(".wav", f"_{rate}.wav")
                c = _run(["ffmpeg", "-y", "-i", wav_path, "-ar", str(rate),
                          "-ac", "1", conv], timeout=60)
                if c.returncode == 0:
                    wav_path, conv = conv, wav_path
            p = _run(["aplay", "-D", sink, "-q", wav_path], timeout=30)
            if p.returncode != 0 and shutil.which("ffmpeg"):
                # sample-rate mismatch (SCO is 8k CVSD / 16k mSBC) — convert and retry
                conv = wav_path.replace(".wav", f"_{rate}.wav")
                c = _run(["ffmpeg", "-y", "-i", wav_path, "-ar", str(rate),
                          "-ac", "1", conv], timeout=60)
                if c.returncode == 0 and os.path.isfile(conv):
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


# ── Session recorder: the whole call, three parties ──────────────────────────
#
# Caller = tee of the SCO downlink (fed from record_utterance's session_sink),
# Bot    = the TTS wavs at the moments play_to_sink ran,
# Julian = the local PC mic, captured ONLY while the recorder is armed
#          (he is on the call via the PC-as-headset; PC mic = SCO uplink).
# At finalize, the three tracks are written at their wall-clock offsets and
# muxed to one 3-channel ogg (vorbis; opus channel mapping tops out at stereo).

class MicRecorder:
    """Local PC-mic capture thread writing (offset, raw) chunks. 16 kHz s16."""

    def __init__(self, cfg, rate=16000):
        self.rate = rate
        self._chunks = []
        self._proc = None
        self._t0 = None
        self._lock = threading.Lock()
        self._thread = None
        self.error = None
        self._cfg = cfg

    def start(self, t0):
        import threading as _t
        self._t0 = t0
        dev = dig(self._cfg, "audio.local_mic", "default")
        cmd = ["arecord", "-D", dev, "-t", "raw", "-f", "S16_LE",
               "-r", str(self.rate), "-c", "1", "-q"]
        if not shutil.which("arecord"):
            cmd = ["parecord", "--raw", "--format=s16le",
                   f"--rate={self.rate}", "--channels=1", f"--device={dev}", "-"]
        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL, bufsize=0)
        except OSError as e:
            self.error = str(e)
            return False

        def _run():
            chunk = (self.rate // 10) * 2  # 100 ms
            while self._proc and self._proc.stdout:
                got = self._proc.stdout.read(chunk)
                if not got:
                    break
                with self._lock:
                    self._chunks.append((time.monotonic() - self._t0, got))

        self._thread = _t.Thread(target=_run, name="mic-rec", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

    def chunks(self):
        with self._lock:
            return list(self._chunks)


class SessionRecorder:
    """Arms the three-party capture for one call. Created per session; armed
    on demand from any control surface (REC_ON), finalized at session end."""

    def __init__(self, cfg, t0):
        self.cfg = cfg
        self.t0 = t0
        self.rate = int(dig(cfg, "audio.sample_rate", 16000))
        self.caller = []          # (offset, raw)
        self.bot = []             # (offset, wav_path-before-delete)
        self.mic = None           # MicRecorder, started on arm
        self.armed = False
        self.finished_path = None
        self._lock = threading.Lock()

    def arm(self, log=print):
        if self.armed:
            return
        self.armed = True
        self.mic = MicRecorder(self.cfg, rate=self.rate)
        if not self.mic.start(self.t0):
            log(f"recorder: local mic unavailable ({self.mic.error}) — recording caller+bot only")
        log("recording ON (caller + bot + local mic)")

    def disarm(self, log=print):
        if not self.armed:
            return
        self.armed = False
        if self.mic:
            self.mic.stop()
        log("recording OFF")

    def add_caller(self, raw):
        if self.armed:
            with self._lock:
                self.caller.append((time.monotonic() - self.t0, raw))

    def add_bot(self, wav_path):
        """Snapshot a bot TTS wav into the recording (copied — caller deletes)."""
        if self.armed:
            snap = wav_path + ".rec.wav"
            try:
                shutil.copyfile(wav_path, snap)
                with self._lock:
                    self.bot.append((time.monotonic() - self.t0, snap))
            except OSError:
                pass

    def _track_wav(self, path, chunks, seconds):
        """Write one mono s16 wav of `seconds` length, chunks at their offsets."""
        total = int(seconds * self.rate) * 2
        import wave as _wave
        w = _wave.open(path, "wb")
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(self.rate)
        buf = bytearray(b"\x00\x00" * int(seconds * self.rate))
        for off, raw in chunks:
            i = int(off * self.rate) * 2
            if i < 0 or i + len(raw) > len(buf):
                continue
            buf[i:i + len(raw)] = raw
        w.writeframes(bytes(buf))
        w.close()
        del buf
        return path

    def _bot_track_wav(self, path, seconds):
        """Bot track: TTS wavs resampled to the SCO rate and placed at offsets."""
        import wave as _wave
        w = _wave.open(path, "wb")
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(self.rate)
        buf = bytearray(b"\x00\x00" * int(seconds * self.rate))
        tmps = []
        for off, wav_path in self.bot:
            conv = wav_path + f".{self.rate}.wav"
            if shutil.which("ffmpeg"):
                p = _run(["ffmpeg", "-y", "-i", wav_path, "-ar", str(self.rate),
                          "-ac", "1", conv], timeout=60)
                if p.returncode != 0:
                    continue
            else:
                conv = wav_path
            tmps.append(conv)
            try:
                r = _wave.open(conv, "rb")
                frames = r.readframes(r.getnframes())
                r.close()
            except Exception:
                continue
            i = int(off * self.rate) * 2
            if i + len(frames) <= len(buf):
                buf[i:i + len(frames)] = frames
        w.writeframes(bytes(buf))
        w.close()
        del buf
        for t in tmps:
            try:
                os.unlink(t)
            except OSError:
                pass
        return path

    def finalize(self, session_dir, log=print):
        """Stop capture and mux caller|bot|Julian into one 3-track ogg."""
        self.disarm(log)
        if not (self.caller or self.bot or (self.mic and self.mic.chunks())):
            log("recorder: nothing captured — no call_full.ogg")
            return None
        if not shutil.which("ffmpeg"):
            log("recorder: ffmpeg missing — cannot mux call_full.ogg")
            return None
        seconds = time.monotonic() - self.t0
        c = self._track_wav(os.path.join(session_dir, "_tr_caller.wav"),
                            self.caller, seconds)
        b = self._bot_track_wav(os.path.join(session_dir, "_tr_bot.wav"), seconds)
        j = self._track_wav(os.path.join(session_dir, "_tr_julian.wav"),
                            self.mic.chunks() if self.mic else [], seconds)
        dest = os.path.join(session_dir, "call_full.ogg")
        p = _run(["ffmpeg", "-y", "-i", c, "-i", b, "-i", j,
                  "-filter_complex", "[0:a][1:a][2:a]amerge=inputs=3[a]",
                  "-map", "[a]", "-c:a", "libvorbis", "-q:a", "3", dest],
                 timeout=120)
        for t in (c, b, j):
            try:
                os.unlink(t)
            except OSError:
                pass
        for _, snap in self.bot:
            try:
                os.unlink(snap)
            except OSError:
                pass
        if p.returncode == 0 and os.path.isfile(dest):
            self.finished_path = dest
            log(f"recording saved: {dest}")
            return dest
        log(f"recorder: mux failed: {p.stderr.strip()[:150]}")
        return None
