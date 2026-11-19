"""call.record_all: every answered call records in full from pickup to hangup,
the manual on/off still works, a recorder failure never drops the call, and the
recording goes with its call folder at retention time. Nothing here reaches a
phone, a mic or ffmpeg."""
import tempfile
import time
from unittest import mock

from callscoot import agent, audio, retention

NUM = "5705550123"


class FakeRecorder:
    def __init__(self, events, fail=False):
        self.events, self.fail, self.armed = events, fail, False

    def arm(self, log=print):
        self.events.append("arm")
        if self.fail:
            raise OSError("no capture device")
        self.armed = True

    def disarm(self, log=print):
        self.events.append("disarm")
        self.armed = False

    def add_caller(self, raw):
        pass

    def add_bot(self, wav):
        pass

    def finalize(self, session_dir, log=print):
        self.events.append("finalize")
        return None


def _run(cfg_call=None, fail=False):
    """One offline call: greeting, caller says goodbye, bot says farewell."""
    cfg = {"logs": {"dir": tempfile.mkdtemp()},
           "call": {"thinking_cue": False, "farewell": "FAREWELL", "voicemail_reply": "VM",
                    **(cfg_call or {})},
           "persona": {"owner": "Anne"}}
    events, logs = [], []
    utter = iter(["Have a good day."])

    def say(c, t):
        events.append(f"say:{t}")
        return tempfile.mkstemp(suffix=".wav")[1]

    with mock.patch.object(agent.phone, "call_state", return_value={"state": 2}), \
         mock.patch.object(agent.phone, "hangup") as hangup, \
         mock.patch.object(agent.audio, "wait_for_hfp"), \
         mock.patch.object(agent.audio, "record_utterance", return_value={"path": "x"}), \
         mock.patch.object(agent, "_transcribe_saved", side_effect=lambda *a: next(utter, "")), \
         mock.patch.object(agent.vault_rag, "build_context", return_value=("", [])), \
         mock.patch.object(agent.llm, "chat", return_value=("ok", "fake")), \
         mock.patch.object(agent, "earlier_contact", return_value=""), \
         mock.patch.object(agent, "summarize", return_value="s"), \
         mock.patch("callscoot.notify.send_call_alert"), \
         mock.patch.object(agent.audio, "SessionRecorder",
                           side_effect=lambda c, t0: FakeRecorder(events, fail)), \
         mock.patch.object(agent.tts, "synth", side_effect=say), \
         mock.patch.object(agent.tts, "synth_cached", side_effect=say), \
         mock.patch.object(agent, "_archive"), \
         mock.patch.object(agent.audio, "play_to_sink"):
        agent.run_session(cfg, number=NUM, speak=True, log=logs.append)
    return events, logs, hangup


def test_every_call_records_from_pickup_before_the_greeting():
    events, _, _ = _run()
    assert events[0] == "arm"
    assert events[-1] == "finalize"
    assert any(e.startswith("say:") for e in events[1:])


def test_record_all_off_leaves_recording_on_demand():
    events, _, _ = _run({"record_all": False})
    assert "arm" not in events
    assert events[-1] == "finalize"


def test_recorder_that_will_not_start_never_drops_the_call():
    events, logs, hangup = _run(fail=True)
    assert any("could not start" in m for m in logs)
    said = [e for e in events if e.startswith("say:")]
    assert said and said[-1] == "say:FAREWELL"  # the call ran to its normal end
    assert hangup.called and events[-1] == "finalize"


def test_pickup_helper_sets_the_panel_flag_and_survives_a_broken_one():
    class Controls:
        def __init__(self, broken=False):
            self.state, self.broken = {}, broken

        def _set(self, **kw):
            if self.broken:
                raise RuntimeError("panel gone")
            self.state.update(kw)

    rec, ctl, transcript = FakeRecorder([]), Controls(), []
    assert agent._record_from_pickup({}, rec, ctl, transcript, log=lambda m: None)
    assert rec.armed and ctl.state["recording"] is True and transcript
    rec2 = FakeRecorder([])
    assert agent._record_from_pickup({}, rec2, Controls(broken=True), [], log=lambda m: None)
    assert rec2.armed


class FakeMic:
    made = 0

    def __init__(self, cfg, rate=16000):
        FakeMic.made += 1
        self.running, self._chunks = False, []

    def start(self, t0):
        self.running = True
        return True

    def stop(self):
        self.running = False

    def chunks(self):
        return list(self._chunks)


def test_manual_off_and_on_mid_call_keeps_what_was_already_captured(monkeypatch):
    monkeypatch.setattr(audio, "MicRecorder", FakeMic)
    FakeMic.made = 0
    r = audio.SessionRecorder({}, time.monotonic())
    log = lambda m: None
    r.arm(log)
    r.mic._chunks.append((0.1, b"\x01\x00"))
    r.add_caller(b"\x02\x00")
    r.disarm(log)
    assert not r.armed and not r.mic.running
    r.add_caller(b"\x03\x00")  # off: not captured
    r.arm(log)
    assert r.armed and r.mic.running
    assert FakeMic.made == 1 and r.mic.chunks() == [(0.1, b"\x01\x00")]
    assert [raw for _, raw in r.caller] == [b"\x02\x00"]
    r.disarm(log)


def test_full_recording_is_pruned_with_its_call_folder(tmp_path):
    cfg = {"logs": {"dir": str(tmp_path), "retention_days": 7},
           "leads": {"dir": str(tmp_path / "leads")}}
    old = tmp_path / "call_20200101_120000_5705550123"
    new = tmp_path / time.strftime("call_%Y%m%d_%H%M%S_5705550123")
    for d in (old, new):
        d.mkdir()
        (d / "call_full.ogg").write_bytes(b"x" * 100)
    paths = [p for p, *_ in retention.candidates(cfg)]
    assert str(old) in paths and str(new) not in paths
