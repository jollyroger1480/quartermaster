"""Panel actions: reconnect and status do not start the bot, listen only plays locally."""
import os
import tempfile
import unittest
from unittest import mock

from callscoot import audio, errors
from callscoot.controls import CallControls, rings_path, secretary_flag_path
from callscoot.gui import _PAGE, handle_act, link_status, read_form, reconnect_link


class PanelTests(unittest.TestCase):
    def test_page_has_the_three_link_buttons(self):
        self.assertIn("RECONNECT (no AI)", _PAGE)
        self.assertIn("STATUS", _PAGE)
        self.assertIn("LISTEN LIVE", _PAGE)
        self.assertIn("URLSearchParams", _PAGE)
        self.assertNotIn("FormData", _PAGE)
        self.assertIn("TURN SECRETARY OFF", _PAGE)
        self.assertIn("act('secretary')", _PAGE)
        self.assertIn("act('rings_up')", _PAGE)
        self.assertIn("act('rings_down')", _PAGE)
        self.assertIn("FEWER", _PAGE)
        self.assertIn("does not shut it off", _PAGE)

    def test_browser_multipart_click_is_read(self):
        raw = (
            b"--bound\r\n"
            b'Content-Disposition: form-data; name="op"\r\n\r\n'
            b"status\r\n"
            b"--bound\r\n"
            b'Content-Disposition: form-data; name="text"\r\n\r\n'
            b"hello\r\n"
            b"--bound--\r\n"
        )
        op, text = read_form("multipart/form-data; boundary=bound", raw)
        self.assertEqual(op, "status")
        self.assertEqual(text, "hello")
        op2, text2 = read_form(
            "application/x-www-form-urlencoded", b"op=listen&text=hi+there")
        self.assertEqual((op2, text2), ("listen", "hi there"))

    def test_record_does_not_arm_the_bot(self):
        c = CallControls()
        self.assertIn("AI off", c.toggle_record())
        snap = c.snapshot()
        self.assertTrue(snap["pending_record"])
        self.assertFalse(snap["pending_join"])
        self.assertFalse(snap["on_call"])

    def test_reconnect_and_status_leave_ai_off(self):
        c = CallControls()
        cfg = {
            "phone": {"adb_ip": "10.0.0.5", "adb_port": 39927},
            "audio": {"bluealsa_pcm": "bluealsa:DEV=B0:C2:C7:C2:F5:9D,PROFILE=sco"},
        }
        with mock.patch("callscoot.gui.phone.adb", return_value=("device", "", 0)), \
             mock.patch("callscoot.gui.phone.call_state", return_value={"state": 0, "number": None}), \
             mock.patch("callscoot.gui.audio.bluealsa_ready", return_value=True), \
             mock.patch("callscoot.gui.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="Connected: yes\nConnection successful\n", stderr="")
            note = handle_act(cfg, c, "reconnect")
            status = handle_act(cfg, c, "status")
        self.assertIn("AI not started", note)
        self.assertIn("ADB device", status)
        self.assertIn("listen off", status)
        self.assertFalse(c.pending_join)
        self.assertFalse(c.pending_record)
        run.assert_called()

    def test_listen_toggle_does_not_arm_ai(self):
        c = CallControls()
        with mock.patch("callscoot.gui.audio.live.set", side_effect=["listening", "listen off"]) as setter:
            on = handle_act({}, c, "listen")
            off = handle_act({}, c, "listen")
        self.assertEqual(on, "listening")
        self.assertEqual(off, "listen off")
        self.assertEqual(setter.call_args_list[0].args[1], True)
        self.assertFalse(c.pending_join)

    def test_actions_are_logged(self):
        d = tempfile.mkdtemp()
        errors._log = None
        cfg = {"logs": {"dir": d}}
        c = CallControls()
        with mock.patch("callscoot.gui.audio.live.set", return_value="listen off"):
            handle_act(cfg, c, "listen")
        with open(os.path.join(d, "errors.log"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("panel listen: listen off", text)

    def test_live_feed_writes_caller_audio_and_stops_on_a_broken_pipe(self):
        player = audio.LiveListen()
        proc = mock.Mock()
        proc.stdin = mock.Mock()
        player.on = True
        player._proc = proc
        player.feed(b"\x00\x00")
        proc.stdin.write.assert_called_once_with(b"\x00\x00")
        proc.stdin.write.side_effect = BrokenPipeError()
        player.feed(b"\x00\x00")
        self.assertFalse(player.on)
        self.assertIsNone(player._proc)

    def test_bot_wav_is_not_played_locally_when_listen_is_off(self):
        player = audio.LiveListen()
        with mock.patch("callscoot.audio.subprocess.run") as run, \
             mock.patch("callscoot.audio.shutil.which", return_value="paplay"):
            player.play_copy("/tmp/does-not-need-to-exist.wav")
        run.assert_not_called()

    def test_status_helper_reports_a_down_phone(self):
        from callscoot.phone import PhoneError
        c = CallControls()
        with mock.patch("callscoot.gui.phone.adb", return_value=("", "no devices", 1)), \
             mock.patch("callscoot.gui.phone.call_state", side_effect=PhoneError("down")), \
             mock.patch("callscoot.gui.audio.bluealsa_ready", return_value=False), \
             mock.patch("callscoot.gui._phone_mac", return_value=""):
            text = link_status({}, c)
        self.assertIn("ADB down", text)
        self.assertIn("headset missing", text)
        self.assertFalse(c.pending_join)

    def test_secretary_off_blocks_the_bot_and_comes_back(self):
        d = tempfile.mkdtemp()
        c = CallControls()
        cfg = {"logs": {"dir": d}}
        path = secretary_flag_path(cfg)
        off = handle_act(cfg, c, "secretary")
        self.assertIn("New rings are not answered", off)
        self.assertFalse(c.snapshot()["secretary"])
        self.assertFalse(c.pending_join)
        self.assertTrue(os.path.exists(path))
        ai = handle_act(cfg, c, "ai")
        self.assertIn("secretary is off", ai)
        self.assertFalse(c.pending_join)
        handle_act(cfg, c, "rec")
        self.assertTrue(c.pending_record)
        on = handle_act(cfg, c, "secretary")
        self.assertIn("secretary on", on)
        self.assertTrue(c.snapshot()["secretary"])
        self.assertFalse(os.path.exists(path))
        self.assertEqual(c.drain(), [("rec_on", None)])

    def test_ring_count_does_not_arm_the_bot_and_stays_in_range(self):
        d = tempfile.mkdtemp()
        c = CallControls()
        c.ring_seconds = 3
        cfg = {"logs": {"dir": d}}
        path = rings_path(cfg)
        up = handle_act(cfg, c, "rings_up")
        self.assertIn("2 rings", up)
        self.assertEqual(c.rings, 2)
        self.assertTrue(os.path.exists(path))
        self.assertFalse(c.pending_join)
        self.assertFalse(c.pending_record)
        for _ in range(20):
            handle_act(cfg, c, "rings_up")
        self.assertEqual(c.rings, 8)
        for _ in range(20):
            handle_act(cfg, c, "rings_down")
        self.assertEqual(c.rings, 1)
        self.assertIn("1 ring", handle_act(cfg, c, "rings_down"))
        self.assertEqual(c.drain(), [])
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "1")

    def test_secretary_off_quiets_the_bot_without_hangup(self):
        c = CallControls()
        c.on_call = True
        c.copilot = False
        c.pending_join = True
        note = c.toggle_secretary(None)
        self.assertIn("line stays up", note)
        self.assertFalse(c.pending_join)
        self.assertFalse(c.secretary)
        self.assertEqual(c.drain(), [("ai_drop", None)])

    def test_reconnect_helper_does_not_answer(self):
        c = CallControls()
        cfg = {"phone": {"adb_ip": "10.0.0.5", "adb_port": 1},
               "audio": {"bluealsa_pcm": "bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=sco"}}
        with mock.patch("callscoot.gui.subprocess.run") as run, \
             mock.patch("callscoot.gui.phone.adb", return_value=("connected", "", 0)), \
             mock.patch("callscoot.gui.link_status", return_value="ADB device"):
            run.return_value = mock.Mock(returncode=0, stdout="Connection successful\n", stderr="")
            note = reconnect_link(cfg, c)
        self.assertIn("AI not started", note)
        self.assertFalse(c.pending_join)
        self.assertEqual(c.drain(), [])


if __name__ == "__main__":
    unittest.main()
