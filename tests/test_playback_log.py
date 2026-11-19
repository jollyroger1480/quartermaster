"""Every bluealsa playback attempt leaves start / rc / stderr lines in errors.log.

Born 2026-11-18: first A26 call answered to silence; aplay -q printed nothing,
the reply blocked 30 s, and errors.log had no playback line at all.
"""
import os
import subprocess
import tempfile
import unittest
import wave
from unittest import mock

from callscoot import audio


def _wav(rate=16000, frames=8000):
    path = tempfile.mktemp(suffix=".wav")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * frames)
    return path


CFG = {"audio": {"backend": "bluealsa", "sco_rate": 16000}}
SINK = "bluealsa:DEV=00:11:22:33:44:55,PROFILE=sco"


class PlaybackLogTests(unittest.TestCase):
    def setUp(self):
        self.wav = _wav()
        self.lines = []
        p = mock.patch.object(audio.errors, "info", side_effect=lambda m, cfg=None: self.lines.append(m))
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        if os.path.exists(self.wav):
            os.unlink(self.wav)

    def test_success_logs_device_rate_rc(self):
        ok = subprocess.CompletedProcess(["aplay"], 0, "", "")
        with mock.patch.object(audio, "_run", return_value=ok):
            audio.play_to_sink(CFG, self.wav, sink=SINK)
        text = "\n".join(self.lines)
        self.assertIn(f"playback start: dev={SINK}", text)
        self.assertIn("wav=16000Hz/1ch/2B/0.50s", text)
        self.assertIn("sco_rate=16000", text)
        self.assertIn("playback end: rc=0", text)

    def test_failure_logs_stderr_then_raises(self):
        bad = subprocess.CompletedProcess(["aplay"], 1, "", "aplay: main:850: audio open error: Device or resource busy")
        with mock.patch.object(audio, "_run", return_value=bad), \
             mock.patch.object(audio.shutil, "which", return_value=None):
            with self.assertRaises(audio.AudioError):
                audio.play_to_sink(CFG, self.wav, sink=SINK)
        self.assertTrue(any("rc=1" in l and "Device or resource busy" in l for l in self.lines), self.lines)

    def test_timeout_is_logged_not_swallowed(self):
        def hang(cmd, timeout=15):
            raise subprocess.TimeoutExpired(cmd, timeout)
        with mock.patch.object(audio, "_run", side_effect=hang):
            with self.assertRaises(audio.AudioError):
                audio.play_to_sink(CFG, self.wav, sink=SINK)
        self.assertTrue(any("playback TIMEOUT" in l and SINK in l for l in self.lines), self.lines)

    def test_real_fake_aplay_binary(self):
        """End to end through subprocess with a fake aplay on PATH."""
        d = tempfile.mkdtemp()
        fake = os.path.join(d, "aplay")
        with open(fake, "w") as f:
            f.write("#!/bin/sh\necho 'aplay: set_params: Rate 16000Hz not available' >&2\nexit 1\n")
        os.chmod(fake, 0o755)
        env = dict(os.environ, PATH=d + os.pathsep + os.environ.get("PATH", ""))
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(audio.shutil, "which", return_value=None):
            with self.assertRaises(audio.AudioError) as ctx:
                audio.play_to_sink(CFG, self.wav, sink=SINK)
        self.assertIn("Rate 16000Hz not available", str(ctx.exception))
        self.assertTrue(any("rc=1" in l and "not available" in l for l in self.lines), self.lines)
        os.unlink(fake)
        os.rmdir(d)


if __name__ == "__main__":
    unittest.main()
