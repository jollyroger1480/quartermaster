"""Robocall evidence log: gate, classifier fail-safe, protected numbers, records, CLI.
No phone, no network: the LLM and contacts lookup are faked."""
import contextlib
import csv
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from callscoot import cli, llm, robocalls

CALLER = "+1 555 010 0123"


def _call(logdir, stamp, number=CALLER, caller_lines=("press 1 to speak about your vehicle warranty",),
          spam=True):
    folder = os.path.join(logdir, f"call_{stamp}_{robocalls._last10(number)}")
    os.makedirs(folder, exist_ok=True)
    tpath = os.path.join(folder, "transcript.md")
    with open(tpath, "w", encoding="utf-8") as f:
        f.write("# call\n\n**caller:** " + " ".join(caller_lines) + "\n")
    with open(os.path.join(folder, "caller_00.ogg"), "wb") as f:
        f.write(b"x")
    tr = [("meta", f"call from {number}")]
    tr += [("caller", t) for t in caller_lines]
    if spam:
        tr.append(("note", "SPAM/telemarketer script detected — declined and hung up"))
    tr.append(("note", f"transcript: {tpath}"))
    return tr


def _reply(**kw):
    d = {"verdict": "robocall", "kind": "prerecorded message", "company": "Acme Auto Shield",
         "callback": "acme-shield.example", "summary": "Recorded warranty pitch.\nAsked to press 1."}
    d.update(kw)
    return lambda cfg, messages: (json.dumps(d), "fake")


class RobocallTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = {"logs": {"dir": self.dir}, "phone": {"allowlist": []}}
        self.not_contact = lambda digits: False

    def handle(self, tr, reply=None, number=CALLER, is_contact=None):
        with mock.patch.object(llm, "chat", side_effect=reply or _reply()):
            return robocalls.handle(self.cfg, number, tr, log=lambda m: None,
                                    is_contact=is_contact or self.not_contact)

    def test_flagged_robocall_writes_csv_row_and_record(self):
        tr = _call(self.dir, "20261101_101500")
        path = self.handle(tr)
        self.assertTrue(path and os.path.isfile(path))
        self.assertTrue(path.endswith("robocalls/call_20261101_101500_5550100123.md"))
        rows = robocalls.read_rows(self.cfg)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["date"], r["number"], r["kind"]),
                         ("2026-11-01", "5550100123", "prerecorded message"))
        self.assertTrue(r["time"].startswith("10:15:00"))
        self.assertEqual(r["company"], "Acme Auto Shield")
        self.assertEqual(r["callback"], "acme-shield.example")
        self.assertEqual(r["prior_12mo"], "0")
        text = open(path, encoding="utf-8").read()
        for want in ("Calling number: 5550100123", "Call length:", "Recorded warranty pitch.",
                     "Asked to press 1.", "caller_00.ogg", "Transcript copy", "Nothing has been filed"):
            self.assertIn(want, text)

    def test_unflagged_customer_call_never_reaches_classifier(self):
        tr = _call(self.dir, "20261101_101500", spam=False,
                   caller_lines=("hi, do you have a starter for a 2008 silverado",
                                 "can you hear me? ok, thanks"))
        chat = mock.Mock()
        with mock.patch.object(llm, "chat", chat):
            self.assertIsNone(robocalls.handle(self.cfg, CALLER, tr, log=lambda m: None,
                                               is_contact=self.not_contact))
        chat.assert_not_called()
        self.assertEqual(robocalls.read_rows(self.cfg), [])

    def test_unflagged_recording_still_gets_the_robocall_check(self):
        # the live check let it through (no SPAM note): the scored evidence over
        # all caller turns still sends it to the classifier and the record
        tr = _call(self.dir, "20261101_101500", spam=False,
                   caller_lines=("and the voice service.",
                                 "customers cannot find your business, call 877 555 0100 to opt out"))
        self.assertTrue(robocalls.suspected(tr))
        self.assertTrue(self.handle(tr, reply=_reply()))
        self.assertEqual(len(robocalls.read_rows(self.cfg)), 1)

    def test_classifier_errors_and_not_fail_to_no_record(self):
        tr = _call(self.dir, "20261101_101500")
        for bad in (llm.LLMError("down"), lambda c, m: ("not json at all", "fake"),
                    _reply(verdict="not"), _reply(verdict="maybe"),
                    lambda c, m: ('{"verdict": ', "fake")):
            self.assertIsNone(self.handle(tr, reply=bad))
        self.assertEqual(robocalls.read_rows(self.cfg), [])

    def test_customer_asking_for_part_is_not_recorded(self):
        # SPAM_RE can fire on ordinary speech ("first"); the classifier decides
        tr = _call(self.dir, "20261101_101500",
                   caller_lines=("do you have a first gear synchro for a 2004 ford ranger",))
        self.assertIsNone(self.handle(tr, reply=_reply(verdict="not", kind="unknown")))
        self.assertEqual(robocalls.read_rows(self.cfg), [])

    def test_allowlist_and_saved_contacts_are_never_recorded(self):
        tr = _call(self.dir, "20261101_101500")
        self.cfg["phone"]["allowlist"] = [CALLER]
        self.assertIsNone(self.handle(tr))
        self.cfg["phone"]["allowlist"] = []
        self.assertIsNone(self.handle(tr, is_contact=lambda d: True))

        def unreadable(d):
            raise RuntimeError("adb down")
        self.assertIsNone(self.handle(tr, is_contact=unreadable))
        self.assertEqual(robocalls.read_rows(self.cfg), [])

    def test_prior_calls_counted_within_twelve_months_same_number(self):
        for stamp in ("20251001_090000", "20260301_090000", "20261001_090000"):
            self.handle(_call(self.dir, stamp))
        other = "+1 555 010 0999"
        self.handle(_call(self.dir, "20261005_090000", number=other), number=other)
        path = self.handle(_call(self.dir, "20261101_090000"))
        rows = {r["call_folder"].rsplit("/", 1)[-1]: r for r in robocalls.read_rows(self.cfg)}
        self.assertEqual(rows["call_20261101_090000_5550100123"]["prior_12mo"], "2")
        self.assertIn("last 12 months: 2", open(path, encoding="utf-8").read())

    def test_same_call_is_recorded_once(self):
        tr = _call(self.dir, "20261101_101500")
        self.handle(tr)
        self.handle(tr)
        self.assertEqual(len(robocalls.read_rows(self.cfg)), 1)

    def test_bad_kind_falls_back_from_verdict(self):
        self.handle(_call(self.dir, "20261101_101500"),
                    reply=_reply(verdict="telemarketer", kind="robot?"))
        self.assertEqual(robocalls.read_rows(self.cfg)[0]["kind"], "live telemarketer")

    def test_cli_lists_newest_first_one_line_each(self):
        self.handle(_call(self.dir, "20261001_090000"))
        self.handle(_call(self.dir, "20261101_090000"))
        toml = os.path.join(self.dir, "c.toml")
        with open(toml, "w") as f:
            f.write(f'[logs]\ndir = "{self.dir}"\n')
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(["--config", toml, "robocalls"]), 0)
        lines = buf.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("2026-11-01"))
        self.assertTrue(lines[1].startswith("2026-10-01"))

    def test_cli_empty_list(self):
        toml = os.path.join(self.dir, "c.toml")
        with open(toml, "w") as f:
            f.write(f'[logs]\ndir = "{self.dir}"\n')
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(["--config", toml, "robocalls"]), 0)
        self.assertIn("no robocalls recorded", buf.getvalue())

    def test_retention_leaves_robocall_records_alone(self):
        from callscoot import retention
        self.handle(_call(self.dir, "20250101_090000"))
        rdir = os.path.join(self.dir, "robocalls")
        kept = os.path.join(rdir, "call_20250101_090000_5550100123", "caller_00.ogg")
        self.assertTrue(os.path.isfile(kept))
        old = 1_000_000_000  # 2001: far past any retention window
        for root, dirs, files in os.walk(rdir):
            for n in dirs + files:
                os.utime(os.path.join(root, n), (old, old))
        os.utime(robocalls.csv_path(self.cfg), (old, old))
        paths = [p for p, *_ in retention.candidates(self.cfg)]
        removed = [os.path.basename(p) for p in paths]
        self.assertNotIn("robocalls.csv", removed)
        self.assertNotIn("robocalls", removed)
        self.assertFalse([p for p in paths if p.startswith(rdir)])
        self.assertIn("call_20250101_090000_5550100123", removed)
        retention.prune(self.cfg, log=lambda m: None)
        self.assertTrue(os.path.isfile(kept))
        self.assertEqual(len(robocalls.read_rows(self.cfg)), 1)

    def test_flagged_call_audio_is_kept_and_listed(self):
        tr = _call(self.dir, "20261101_101500")
        folder = robocalls.call_folder(tr)
        for n in ("call_full.ogg", "bot_00.ogg", "_tr_caller.wav"):
            with open(os.path.join(folder, n), "wb") as f:
                f.write(b"y")
        path = self.handle(tr)
        kept_dir = os.path.join(self.dir, "robocalls", os.path.basename(folder))
        self.assertEqual(sorted(os.listdir(kept_dir)),
                         ["bot_00.ogg", "call_full.ogg", "caller_00.ogg"])
        text = open(path, encoding="utf-8").read()
        for n in ("bot_00.ogg", "call_full.ogg", "caller_00.ogg", kept_dir):
            self.assertIn(n, text)

    def test_ordinary_calls_keep_no_audio_copy(self):
        tr = _call(self.dir, "20261101_101500", spam=False,
                   caller_lines=("do you have a radiator for a 2005 civic",))
        self.handle(tr)
        tr2 = _call(self.dir, "20261102_101500")
        self.handle(tr2, reply=_reply(verdict="not"))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "robocalls")))

    def test_dnc_requested_and_company_answer(self):
        tr = _call(self.dir, "20261101_101500")
        self.handle(tr)
        tr2 = _call(self.dir, "20261102_101500")
        tr2 += [("caller", "uh we're calling from Shieldline Motor Club"),
                ("note", "company answer: uh we're calling from Shieldline Motor Club"),
                ("note", "do-not-call requested")]
        seen = []

        def chat(c, messages):
            seen.append(messages[-1]["content"])
            return _reply(company="")(c, messages)
        path = self.handle(tr2, reply=chat)
        self.assertIn("COMPANY ANSWER: uh we're calling from Shieldline Motor Club", seen[0])
        rows = sorted(robocalls.read_rows(self.cfg), key=lambda r: r["date"])
        self.assertEqual([r["dnc_requested"] for r in rows], ["no", "yes"])
        self.assertEqual(rows[1]["company"], "Shieldline Motor Club")
        self.assertIn("Do-not-call requested on the call: yes", open(path, encoding="utf-8").read())

    def test_company_from_answer_needs_a_name(self):
        self.assertEqual(robocalls.company_from_answer("this is Acme Solar."), "Acme Solar")
        for a in ("what?", "", "hello hello", "I don't know"):
            self.assertEqual(robocalls.company_from_answer(a), "")

    def test_old_csv_header_is_upgraded_without_losing_rows(self):
        old_fields = [f for f in robocalls.FIELDS if f != "dnc_requested"]
        with open(robocalls.csv_path(self.cfg), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=old_fields)
            w.writeheader()
            w.writerow({k: "" for k in old_fields} | {
                "date": "2026-10-01", "time": "09:00:00 EDT", "number": "5550100123",
                "summary": "older, with a comma / and slash", "call_folder": "/x/call_old"})
        self.handle(_call(self.dir, "20261101_101500"))
        with open(robocalls.csv_path(self.cfg), newline="", encoding="utf-8") as f:
            r = csv.DictReader(f)
            self.assertEqual(r.fieldnames, robocalls.FIELDS)
            rows = list(r)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["summary"], "older, with a comma / and slash")
        self.assertEqual(rows[0]["dnc_requested"], "")
        self.assertEqual(rows[1]["dnc_requested"], "no")
        self.assertEqual(rows[1]["prior_12mo"], "1")


class DncLineTests(unittest.TestCase):
    """The live spam guard: one company question + do-not-call, then hang up."""

    def run_call(self, utterances):
        from callscoot import agent
        cfg = {"logs": {"dir": tempfile.mkdtemp()},
               "call": {"thinking_cue": False, "farewell": "FAREWELL", "voicemail_reply": "VM",
                        "dnc_answer_wait_s": 0.1}}
        said = []
        utter = iter(utterances)
        tok = lambda *a: said.append(a[1]) or tempfile.mkstemp(suffix=".wav")[1]
        with mock.patch.object(agent.phone, "call_state", return_value={"state": 2}), \
             mock.patch.object(agent.phone, "hangup") as hang, \
             mock.patch.object(agent.audio, "wait_for_hfp"), \
             mock.patch.object(agent.audio, "record_utterance", return_value={"path": "x"}) as rec, \
             mock.patch.object(agent, "_transcribe_saved", side_effect=lambda *a: next(utter, "")), \
             mock.patch.object(agent.llm, "chat") as chat, \
             mock.patch.object(agent, "earlier_contact", return_value=""), \
             mock.patch.object(agent, "summarize", return_value="s"), \
             mock.patch("callscoot.notify.send_call_alert"), \
             mock.patch.object(agent.audio, "SessionRecorder") as sr, \
             mock.patch.object(agent.tts, "synth", side_effect=tok), \
             mock.patch.object(agent.tts, "synth_cached", side_effect=tok), \
             mock.patch.object(agent, "_archive"), \
             mock.patch.object(agent.audio, "play_to_sink"):
            sr.return_value.armed = False
            sr.return_value.finalize.return_value = None
            tr, _flagged = agent.run_session(cfg, number=CALLER, speak=True, join_live=True)
        return said, tr, rec.call_count, chat.call_count, hang.called

    def test_exact_line_and_one_listen_for_live_caller(self):
        from callscoot import agent
        said, tr, recs, chats, hung = self.run_call(
            ["hi I'm calling about your vehicle's extended warranty", "we're calling from Acme"])
        self.assertEqual(said[-1], "What company are you calling from? Please put this number "
                                   "on your do-not-call list. Goodbye.")
        self.assertEqual(said[-1], agent.DNC_REPLY)
        self.assertEqual(recs, 2)          # the spam line + one answer listen
        self.assertEqual(chats, 0)         # no LLM turn
        self.assertTrue(hung)
        self.assertIn(("caller", "we're calling from Acme"), tr)
        self.assertIn(("note", "company answer: we're calling from Acme"), tr)
        self.assertIn(("note", "do-not-call requested"), tr)
        self.assertTrue(robocalls.dnc_requested(tr) and robocalls.flagged_as_spam(tr))

    def test_recording_gets_no_listen(self):
        said, tr, recs, chats, hung = self.run_call(
            ["this is a recorded message about your vehicle warranty press 1 now"])
        self.assertEqual(recs, 1)
        self.assertTrue(hung)
        self.assertFalse(robocalls.company_answer(tr))

    def test_silence_after_question_just_hangs_up(self):
        said, tr, recs, chats, hung = self.run_call(["about your extended warranty", ""])
        self.assertEqual(recs, 2)
        self.assertTrue(hung)
        self.assertFalse(robocalls.company_answer(tr))
        self.assertEqual([w for w, _t in tr].count("caller"), 1)

    def test_line_has_no_threats(self):
        from callscoot import agent
        for word in ("fine", "lawsuit", "sue", "court", "tcpa", "attorney", "legal"):
            self.assertNotIn(word, agent.DNC_REPLY.lower())
        self.assertIn(agent.DNC_REPLY, agent.fixed_lines({}))


if __name__ == "__main__":
    unittest.main()


def test_spam_pattern_does_not_fire_inside_ordinary_words():
    from callscoot.agent import SPAM_RE
    for line in ("I need the first gear synchro", "it is theirs, not mine", "the stairs side panel"):
        assert not SPAM_RE.search(line)
    assert SPAM_RE.search("this is the IRS calling about your taxes")
