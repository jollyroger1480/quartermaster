"""Recording notice, retention, capture-only leads, parts rule, log masking, latency paths."""
import datetime
import json
import os
import tempfile
import time
import unittest
import wave
from unittest import mock

from callscoot import agent, audio, errors, leads, retention, stt


def _wav(path, rate=16000, frames=1600):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * frames)
    return path


PERSONA = {"persona": {"owner": "Anne", "shop_hours": "By appointment only",
                       "business_address": "Yourtown, ST"}}


class NoticeTests(unittest.TestCase):
    def test_notice_added_once(self):
        self.assertIn("may be recorded", agent.with_notice({}, "Hi there."))
        g = "Thanks. This call may be recorded. How can I help?"
        self.assertEqual(agent.with_notice({}, g), g)

    def test_every_session_says_notice_before_caller_audio(self):
        """Joined-silent call: the notice is spoken before the first utterance is saved."""
        order = []
        cfg = {"logs": {"dir": tempfile.mkdtemp()}, "call": {"thinking_cue": False}}
        with mock.patch.object(agent.phone, "call_state", side_effect=[{"state": 2}] * 3 + [{"state": 0}] * 5), \
             mock.patch.object(agent.phone, "hangup"), \
             mock.patch.object(agent.audio, "wait_for_hfp"), \
             mock.patch.object(agent.audio, "SessionRecorder") as rec, \
             mock.patch.object(agent.audio, "record_utterance",
                               side_effect=lambda *a, **k: order.append("caller-audio") or None), \
             mock.patch.object(agent.tts, "synth_cached",
                               side_effect=lambda c, t: order.append(t) or _wav(tempfile.mktemp(suffix=".wav"))), \
             mock.patch.object(agent, "_archive"), \
             mock.patch.object(agent, "summarize", return_value="s"), \
             mock.patch("callscoot.notify.send_call_alert"), \
             mock.patch.object(agent.audio, "play_to_sink"):
            rec.return_value.armed = False
            rec.return_value.finalize.return_value = None
            agent.run_session(cfg, number="5551234567", join_live=True, speak=False)
        self.assertIn("may be recorded", order[0])


class PartsRuleTests(unittest.TestCase):
    def setUp(self):
        self.p = agent.system_prompt(PERSONA, "")

    def test_listed_online_answer(self):
        self.assertIn("sold ONLY online", self.p)
        self.assertIn("everything in stock is listed there", self.p)

    def test_not_listed_takes_request_without_stock_promise(self):
        self.assertIn("we don't have it right now", self.p)
        self.assertIn("year, make, model, and part", self.p)
        self.assertIn("callback number", self.p)
        self.assertIn("at most ONE clarifying question", self.p)
        self.assertIn("get back to them if we find one", self.p)
        self.assertIn("Never promise to check inventory", self.p)
        for bad in ("let me check our inventory", "check the back", "we probably have"):
            self.assertNotIn(bad, self.p.lower())

    def test_no_zip_or_street_ever(self):
        cfg = {**PERSONA, "leads": {"ship_zip": "99999"}}
        p = agent.system_prompt(cfg, "")
        self.assertNotIn("99999", p)
        self.assertIn("Never give a street, ZIP", p)


class LeadCaptureTests(unittest.TestCase):
    def test_capture_only_no_sourcing_owner_list(self):
        d = tempfile.mkdtemp()
        kdir = os.path.join(d, "knowledge")
        cfg = {"leads": {"dir": os.path.join(d, "leads")}, "orders": {"knowledge_dir": kdir}}
        lead = {"caller_name": "Sam", "callback": "", "notes": "",
                "vehicles": [{"year": "2008", "make": "Chevy", "model": "Silverado"}],
                "parts": [{"name": "starter", "part_number": "12345"}]}
        with mock.patch.object(leads, "extract_lead", return_value=lead), \
             mock.patch("callscoot.notify.send_lead_alert") as alert:
            self.assertTrue(leads.process(cfg, "5551234567", [("caller", "starter")], log=lambda m: None))
        alert.assert_called_once()
        body = open(leads.digest_path(cfg)).read()
        self.assertIn("starter (PN 12345)", body)
        self.assertIn("5551234567", body)  # owner-facing list carries the callback
        self.assertFalse(os.path.exists(os.path.join(kdir, "leads.md")))  # receptionist never sees it


class RetentionTests(unittest.TestCase):
    def test_prunes_only_old_call_data_and_done_or_stale_leads(self):
        d = tempfile.mkdtemp()
        logs, ld = os.path.join(d, "logs"), os.path.join(d, "leads")
        os.makedirs(logs)
        os.makedirs(ld)
        old = (datetime.datetime.now() - datetime.timedelta(days=9)).strftime("%Y%m%d_%H%M%S")
        new = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        for stamp in (old, new):
            os.makedirs(os.path.join(logs, f"call_{stamp}_5551234567"))
            _wav(os.path.join(logs, f"call_{stamp}_5551234567", "caller_00.wav"))
        for keep in ("watch.log", "errors.log", "rings"):
            open(os.path.join(logs, keep), "w").close()
        e3 = os.path.join(logs, "errors.log.3")
        open(e3, "w").close()
        os.utime(e3, (time.time() - 10 * 86400,) * 2)
        stale = (datetime.datetime.now() - datetime.timedelta(days=31)).strftime("%Y%m%d_%H%M%S")
        for name, status in ((f"{new}_1", "new"), (f"{new}_2", "done"), (f"{stale}_3", "new")):
            json.dump({"status": status}, open(os.path.join(ld, name + ".json"), "w"))
        cfg = {"logs": {"dir": logs}, "leads": {"dir": ld}}
        dry = retention.prune(cfg, dry_run=True, log=lambda m: None)
        self.assertEqual(len(dry), 4)
        self.assertTrue(os.path.isdir(os.path.join(logs, f"call_{old}_5551234567")))  # dry run kept it
        with mock.patch.object(leads, "write_digest"):
            retention.prune(cfg, log=lambda m: None)
        self.assertEqual(sorted(os.listdir(logs)),
                         sorted([f"call_{new}_5551234567", "watch.log", "errors.log", "rings"]))
        self.assertEqual(os.listdir(ld), [f"{new}_1.json"])


class LogMaskTests(unittest.TestCase):
    def test_numbers_masked(self):
        self.assertEqual(errors.mask_numbers("RINGING 5705551234"), "RINGING ***1234")
        self.assertEqual(errors.mask_numbers("from +1 (570) 555-1234"), "from ***1234")
        self.assertIn("2026-11-18 01:13:05", errors.mask_numbers("2026-11-18 01:13:05 ok"))


class LatencyPathTests(unittest.TestCase):
    def test_sco_rate_wav_skips_ffmpeg(self):
        p = _wav(tempfile.mktemp(suffix=".wav"))
        cfg = {"audio": {"backend": "bluealsa", "sco_rate": 16000}}
        with mock.patch.object(audio, "_run", return_value=mock.Mock(returncode=0)) as run:
            audio.play_to_sink(cfg, p, sink="bluealsa:x")
        self.assertEqual([c.args[0][0] for c in run.call_args_list], ["aplay"])
        os.unlink(p)

    def test_grok_stt_falls_back_to_whisper(self):
        cfg = {"stt": {"backend": "grok", "grok_token_file": "/nonexistent"}}
        with mock.patch.object(stt, "whisper", return_value="hello") as w, \
             mock.patch("callscoot.errors.record"):
            self.assertEqual(stt.transcribe(cfg, "/x.wav"), "hello")
        w.assert_called_once()


if __name__ == "__main__":
    unittest.main()
