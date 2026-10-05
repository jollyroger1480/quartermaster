"""Caller-side audio: an always-on stream from the bridge, cut into utterances
by an adaptive energy VAD. Runs on the event-loop thread (fed by the CDP
binding), so it only does cheap numpy work per 20 ms frame.

Full duplex: while the bot is talking the VAD keeps listening. Loud, sustained
caller speech (barge-in) fires `barge` so the call loop can stop playback, and
the interrupting speech becomes the next utterance instead of being lost.
"""
import asyncio
import collections
import math
import time
import wave

import numpy as np

from .config import dig

RATE = 16000
FRAME = 320  # 20 ms


class CallerAudio:
    def __init__(self, cfg):
        self.silence_s = float(dig(cfg, "audio.record_silence_s", 1.1))
        self.max_s = float(dig(cfg, "audio.record_max_s", 15))
        self.min_s = float(dig(cfg, "audio.record_min_s", 0.35))
        self.min_rms = float(dig(cfg, "audio.vad_min_rms", 150))
        self.barge_enabled = bool(dig(cfg, "audio.barge_in", True))
        self.barge_factor = float(dig(cfg, "audio.barge_factor", 1.8))
        self.barge_frames = max(1, int(float(dig(cfg, "audio.barge_ms", 500)) / 20))
        self.start_frames = 3
        self.pre_frames = 15      # 300 ms pre-roll
        self.floor = None
        self.calib = []
        self.leftover = np.zeros(0, dtype=np.int16)
        self.pre = collections.deque(maxlen=self.pre_frames + self.barge_frames + 25)
        self.cur = []
        self.voiced = 0
        self.hot = 0
        self.silent = 0
        self.in_speech = False
        self.bot_speaking = False
        self.barge = asyncio.Event()
        self.utterances = asyncio.Queue()
        self.recording = []
        self.frames_in = 0
        self.last_frame_at = 0.0

    @property
    def thresh(self):
        base = (self.floor or 0.0) * 2.5 + 80
        return max(base, self.min_rms)

    def feed(self, pcm):
        self.recording.append(pcm)
        self.last_frame_at = time.monotonic()
        buf = np.concatenate([self.leftover, pcm]) if self.leftover.size else pcm
        n = buf.size // FRAME
        for i in range(n):
            self._frame(buf[i * FRAME:(i + 1) * FRAME])
        self.leftover = buf[n * FRAME:].copy()

    def _frame(self, fr):
        self.frames_in += 1
        rms = math.sqrt(float(np.mean(fr.astype(np.float32) ** 2)))
        if self.floor is None:
            self.calib.append(rms)
            if len(self.calib) >= 20:  # 400 ms of line noise
                self.floor = float(np.percentile(self.calib, 20))  # prompt/speech at pickup shouldn't set the floor
            return
        th = self.thresh
        if not self.in_speech:
            self.pre.append(fr)
            if self.bot_speaking:
                if not self.barge_enabled:
                    return
                self.hot = self.hot + 1 if rms > th * self.barge_factor else 0
                if self.hot >= self.barge_frames:
                    self._begin()
                    self.barge.set()
                return
            self.hot = self.hot + 1 if rms > th else 0
            if self.hot >= self.start_frames:
                self._begin()
            elif rms < th:
                self.floor = 0.97 * self.floor + 0.03 * rms
            return
        self.cur.append(fr)
        if rms > th * 0.8:
            self.voiced += 1
            self.silent = 0
        else:
            self.silent += 1
        dur = len(self.cur) * FRAME / RATE
        if self.silent * FRAME / RATE >= self.silence_s or dur >= self.max_s:
            self._finish()

    def _begin(self):
        self.in_speech = True
        self.cur = list(self.pre)
        self.pre.clear()
        self.voiced = self.hot
        self.silent = 0
        self.hot = 0

    def _finish(self):
        self.in_speech = False
        pcm = np.concatenate(self.cur) if self.cur else np.zeros(0, dtype=np.int16)
        self.cur = []
        if self.voiced * FRAME / RATE >= self.min_s:
            self.utterances.put_nowait(pcm)
        self.voiced = self.silent = 0

    def reset_pending(self):
        """Drop anything queued (e.g. line noise during the greeting)."""
        while not self.utterances.empty():
            self.utterances.get_nowait()

    def full_recording(self):
        return np.concatenate(self.recording) if self.recording else np.zeros(0, dtype=np.int16)


def blip(rate=16000):
    """Soft rising two-tone 'thinking' cue."""
    parts = []
    for freq, dur in ((660, 0.09), (880, 0.12)):
        n = int(rate * dur)
        t = np.arange(n) / rate
        env = np.sin(np.pi * np.arange(n) / n)
        parts.append(np.sin(2 * np.pi * freq * t) * 0.22 * env)
    return (np.concatenate(parts) * 32767).astype(np.int16)


def resample(pcm, src, dst):
    if src == dst or pcm.size == 0:
        return pcm
    n = int(round(pcm.size * dst / src))
    x = np.linspace(0, pcm.size - 1, n)
    return np.interp(x, np.arange(pcm.size), pcm.astype(np.float32)).astype(np.int16)


def write_wav(path, pcm, rate):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.ascontiguousarray(pcm, dtype=np.int16).tobytes())


def read_wav(path):
    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        ch = w.getnchannels()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1).astype(np.int16)
    return data, rate
