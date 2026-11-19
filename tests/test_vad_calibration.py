"""VAD must still hear a caller who is already talking when capture goes live."""
import sys
import textwrap
import unittest
from unittest import mock

from callscoot import audio

# Paced fake capture: real-time 50 ms chunks of loud tone, then digital silence.
_FAKE = textwrap.dedent("""
    import math, struct, sys, time
    rate = 16000; n = rate // 20
    tone = struct.pack("<%dh" % n, *(int(6000 * math.sin(2 * math.pi * 300 * i / rate)) for i in range(n)))
    zero = bytes(2 * n)
    out = sys.stdout.buffer
    for k in range(int(sys.argv[1])):
        out.write(tone); out.flush(); time.sleep(0.05)
    for k in range(60):
        out.write(zero); out.flush(); time.sleep(0.05)
""")


def _cfg():
    return {"audio": {"backend": "bluealsa", "sample_rate": 16000, "record_max_s": 4,
                      "record_silence_s": 0.3, "record_min_s": 0.45, "vad_min_rms": 240}}


class VadCalibration(unittest.TestCase):
    def _record(self, speech_chunks):
        cmd = [sys.executable, "-c", _FAKE, str(speech_chunks)]
        with mock.patch.object(audio, "_record_cmd", return_value=cmd):
            return audio.record_utterance(_cfg(), source="fake")

    def test_speech_already_running_at_capture_start_is_heard(self):
        # Speech covers the whole calibration window, so the "noise floor" is speech.
        rec = self._record(speech_chunks=24)
        self.assertIsNotNone(rec, "caller talking from the first chunk was read as silence")
        self.assertGreater(rec["seconds"], 0.8)

    def test_silence_still_returns_none(self):
        self.assertIsNone(self._record(speech_chunks=0))


if __name__ == "__main__":
    unittest.main()
