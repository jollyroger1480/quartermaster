"""donotcall.gov complaint builder: mapping, the page's own checks, flow, sidecar.
No network: every POST goes to a fake, and urlopen is patched to fail loudly."""
import contextlib
import csv
import datetime
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from callscoot import dnc_report as d
from callscoot import robocalls

FIELDS = ["date", "time", "number", "duration_s", "kind", "verdict", "company", "callback",
          "summary", "prior_12mo", "call_folder", "record"]
MINE = "5705550142"


def _row(**kw):
    r = {"date": "2026-11-10", "time": "14:05:09 EST", "number": "8005550199",
         "duration_s": "31", "kind": "prerecorded message", "verdict": "robocall",
         "company": "Acme Auto Shield", "callback": "", "summary":
         "Recorded pitch for an extended vehicle warranty. / Asked to press 1.",
         "prior_12mo": "0", "call_folder": "/x/logs/call_20261110_140509_8005550199",
         "record": "/x/logs/robocalls/call_20261110_140509_8005550199.md"}
    r.update(kw)
    return r


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = {"logs": {"dir": self.tmp.name}, "phone": {"own_number": MINE}}
        net = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        net.start()
        self.addCleanup(net.stop)
        self.addCleanup(self.tmp.cleanup)

    def write_rows(self, rows, fields=FIELDS):
        with open(robocalls.csv_path(self.cfg), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in fields})

    def run_main(self, argv, post=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = d.main(argv, cfg=self.cfg, post=post)
        return rc, out.getvalue(), err.getvalue()


class Mapping(Base):
    def test_model_matches_page_fields(self):
        model, _ = d.build_model(_row(), MINE)
        self.assertEqual(list(model), d.MODEL_KEYS)
        self.assertEqual(model["phoneNumber"], MINE)
        self.assertEqual(model["companyPhoneNumber"], "8005550199")
        self.assertEqual(model["dateOfCall"], "11/10/2026")
        self.assertEqual((model["timeOfCall"], model["minuteOfCall"]), ("14", "05"))
        self.assertEqual(model["wasPrerecorded"], "Y")
        self.assertEqual(model["isMobileCall"], "")
        self.assertEqual(model["subjectMatter"], "Warranties  & protection plans")
        for k in ("firstName", "lastName", "streetAddress1", "streetAddress2", "city", "zip",
                  "haveDoneBusiness"):
            self.assertEqual(model[k], "", k)
        self.assertEqual(model["state"], "-")

    def test_kind_maps_to_prerecorded(self):
        self.assertEqual(d.build_model(_row(kind="live telemarketer"), MINE)[0]["wasPrerecorded"], "N")
        self.assertEqual(d.build_model(_row(kind="unknown"), MINE)[0]["wasPrerecorded"], "")

    def test_asked_to_stop_tolerates_missing_column(self):
        self.assertEqual(d.build_model(_row(dnc_requested="yes"), MINE)[0]["askedToStop"], "Y")
        self.assertEqual(d.build_model(_row(dnc_requested="no"), MINE)[0]["askedToStop"], "")
        self.assertEqual(d.build_model(_row(), MINE)[0]["askedToStop"], "")

    def test_subject_keywords_and_fallback(self):
        self.assertEqual(d.pick_subject("Medicare back brace offer"), "5")
        self.assertEqual(d.pick_subject("Lower your credit card interest rate"), "3")
        self.assertEqual(d.pick_subject("Claimed to be from Social Security"), "4")
        self.assertEqual(d.pick_subject("Solar panel savings"), "9")
        self.assertEqual(d.pick_subject("Google business listing verification"), "0")
        self.assertEqual(d.SUBJECTS[d.pick_subject("")], "Unknown")

    def test_company_fields_follow_form_limits(self):
        m = d.build_model(_row(company="X" * 80, number="unknown"), MINE)[0]
        self.assertEqual(len(m["companyName"]), 60)
        self.assertEqual(m["companyPhoneNumber"], "")
        self.assertEqual(d.build_model(_row(number="+1 800 555 0199"), MINE)[0]["companyPhoneNumber"],
                         "8005550199")

    def test_comments_trimmed(self):
        m = d.build_model(_row(summary="  " + "a" * 1500), MINE)[0]
        self.assertEqual(len(m["comments"]), 1000)

    def test_own_number_from_phone_config_only(self):
        self.cfg["phone"] = {"own_number": "+1 (570) 555-0100"}
        self.assertEqual(d.my_number(self.cfg), "5705550100")
        with mock.patch.dict(os.environ, {"CALLSCOOT_DNC_MY_NUMBER": "5705550199"}):
            self.assertEqual(d.my_number({"dnc": {"my_number": "5705550199"}}), "")

    def test_owner_name_is_optional(self):
        self.assertEqual(d.my_name({}), ("", ""))
        cfg = {"phone": {"owner_first_name": " Pat ", "owner_last_name": "Doe"}}
        self.assertEqual(d.my_name(cfg), ("Pat", "Doe"))
        m = d.build_model(_row(), MINE, d.my_name(cfg))[0]
        self.assertEqual((m["firstName"], m["lastName"]), ("Pat", "Doe"))
        self.assertEqual(d.build_model(_row(), MINE)[0]["firstName"], "")

    def test_callback_goes_in_comments(self):
        m = d.build_model(_row(callback="888-555-0100"), MINE)[0]
        self.assertTrue(m["comments"].endswith("Callback the caller stated: 888-555-0100"))
        self.assertEqual(m["companyPhoneNumber"], "8005550199")
        long = d.build_model(_row(summary="a" * 1200, callback="888-555-0100"), MINE)[0]
        self.assertEqual(len(long["comments"]), 1000)


class PageChecks(Base):
    def test_ok_record_has_no_problems(self):
        _m, eps, problems = d.plan(self.cfg, _row(), today=datetime.date(2026, 11, 18))
        self.assertEqual(problems, [])
        self.assertEqual([u for _x, u, _p in eps], [d.SAVE_URL])

    def test_live_daytime_call_checks_step2_first(self):
        _m, eps, _p = d.plan(self.cfg, _row(kind="live telemarketer", verdict="telemarketer"))
        self.assertEqual([u for _x, u, _p in eps], [d.STEP2_URL, d.SAVE_URL])
        self.assertEqual(eps[0][2], {"PhoneNumber": MINE, "DateOfCall": "11/10/2026"})

    def test_live_after_hours_call_skips_step2(self):
        _m, eps, _p = d.plan(self.cfg, _row(kind="live telemarketer", time="21:30:00 EST"))
        self.assertEqual([u for _x, u, _p in eps], [d.SAVE_URL])

    def test_refusals(self):
        cases = [
            (_row(verdict="not"), "not robocall"),
            (_row(date="2003-09-30", kind="live telemarketer"), "between 10/01/2003"),
            (_row(date="2009-08-31"), "09/01/2009"),
            (_row(date="2999-01-01"), "between 10/01/2003"),
            (_row(summary="debt collection agency call"), "debt collection"),
            (_row(summary="a political survey"), "survey"),
        ]
        for row, needle in cases:
            problems = d.plan(self.cfg, row)[2]
            self.assertTrue(any(needle in p for p in problems), (row, problems))

    def test_own_number_required_and_area_code_checked(self):
        self.cfg["phone"] = {}
        self.assertTrue(any("phone.own_number is missing" in p for p in d.plan(self.cfg, _row())[2]))
        post = mock.Mock()
        self.write_rows([_row()])
        rc, _o, err = self.run_main(["call_20261110_140509_8005550199", "--send"], post=post)
        self.assertEqual(rc, 1)
        self.assertIn("phone.own_number is missing", err)
        post.assert_not_called()
        self.cfg["phone"] = {"own_number": "2115550100"}
        self.assertTrue(any("Area code" in p for p in d.plan(self.cfg, _row())[2]))


class Flow(Base):
    def setUp(self):
        super().setUp()
        self.rows = [_row(), _row(date="2026-11-11", call_folder="/x/call_20261111_090000_2025550123",
                                  number="2025550123", kind="live telemarketer",
                                  verdict="telemarketer", time="09:00:00 EST")]
        self.write_rows(self.rows)

    def test_list_and_dry_run_send_nothing(self):
        rc, out, _ = self.run_main([])
        self.assertEqual(rc, 0)
        self.assertIn("call_20261110_140509_8005550199", out)
        post = mock.Mock()
        rc, out, _ = self.run_main(["call_20261110_140509_8005550199"], post=post)
        self.assertEqual(rc, 0)
        self.assertIn("DRY RUN", out)
        self.assertIn(d.SAVE_URL, out)
        post.assert_not_called()

    def test_send_saves_and_marks_once(self):
        calls = []

        def post(url, payload):
            calls.append((url, payload))
            return '{"message":"saved"}'
        rc, out, _ = self.run_main(["call_20261110_140509_8005550199", "--send"], post=post)
        self.assertEqual(rc, 0, out)
        self.assertEqual([u for u, _ in calls], [d.SAVE_URL])
        self.assertEqual(calls[0][1]["validFlag"], "Y")
        done = d.load_reported(self.cfg)
        self.assertIn("call_20261110_140509_8005550199", done)
        self.assertRegex(done["call_20261110_140509_8005550199"]["reported_at"],
                         r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
        rc, _out, err = self.run_main(["call_20261110_140509_8005550199", "--send"], post=post)
        self.assertEqual(rc, 1)
        self.assertIn("already reported", err)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("call_20261110_140509_8005550199", self.run_main([])[1])

    def test_step2_refusal_stops_before_save(self):
        post = mock.Mock(return_value="1")
        rc, _out, err = self.run_main(["call_20261111_090000_2025550123", "--send"], post=post)
        self.assertEqual(rc, 1)
        self.assertIn("not on the Do Not Call Registry", err)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(d.load_reported(self.cfg), {})

    def test_failure_is_not_retried_or_marked(self):
        post = mock.Mock(side_effect=OSError("boom"))
        rc, _o, err = self.run_main(["call_20261110_140509_8005550199", "--send"], post=post)
        self.assertEqual((rc, post.call_count), (1, 1))
        self.assertIn("no retry", err)
        post = mock.Mock(return_value='{"message":"error","supportId":"x"}')
        rc, _o, _e = self.run_main(["call_20261110_140509_8005550199", "--send"], post=post)
        self.assertEqual(rc, 1)
        self.assertEqual(d.load_reported(self.cfg), {})

    def test_refused_record_never_posts(self):
        self.write_rows([_row(verdict="not")])
        post = mock.Mock()
        rc, _o, err = self.run_main(["call_20261110_140509_8005550199", "--send"], post=post)
        self.assertEqual(rc, 1)
        self.assertIn("not robocall", err)
        post.assert_not_called()

    def test_send_without_record_is_refused(self):
        self.assertEqual(self.run_main(["--send"])[0], 2)

    def test_real_post_uses_urllib_with_timeout_and_json(self):
        seen = {}

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(req, timeout):
            seen.update(url=req.full_url, timeout=timeout, body=json.loads(req.data),
                        ctype=req.get_header("Content-type"), ua=req.get_header("User-agent"))
            return Resp(b'{"message":"saved"}')
        reply = d._post(d.SAVE_URL, {"a": 1}, opener=opener)
        self.assertEqual(json.loads(reply), {"message": "saved"})
        self.assertEqual(seen["timeout"], d.TIMEOUT_S)
        self.assertEqual(seen["body"], {"a": 1})
        self.assertTrue(seen["ctype"].startswith("application/json"))
        self.assertEqual(seen["ua"], "Mozilla/5.0")


if __name__ == "__main__":
    unittest.main()
