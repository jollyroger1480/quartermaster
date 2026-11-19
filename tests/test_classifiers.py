"""Classifier rules for calls and texts, plus the labelled eval sets in tests/eval/.

Rules are tested by kind of phrase, never by one recorded call."""
import json
import os

import pytest

from callscoot import agent, leads, robocalls, sms, spamscore
from tests.test_sms_limit_notice import A, CFG, bot  # noqa: F401  (fixture)

EVAL = os.path.join(os.path.dirname(__file__), "eval")


def _predict(kind, x):
    if kind in ("call_spam", "robocall_gate", "sms_spam"):
        return spamscore.is_spam(x)
    if kind == "call_recorded":
        return bool(agent.RECORDING_RE.search(" ".join(x)))
    if kind == "escalate":
        return bool(agent.ESCALATE_RE.search(x))
    if kind == "end":
        return bool(agent.END_RE.search(x)) and len(x.split()) <= 6
    if kind == "arrange":
        return bool(agent.arrange_re("Anne").search(x)) and not agent.SALVAGE_RE.search(x)
    if kind == "parts_gate":
        return any(leads.PARTS_RE.search(t) for t in x)
    raise AssertionError(kind)


@pytest.mark.parametrize("name", ["tuning", "heldout", "heldout_blind"])
def test_eval_set_scores_every_confident_case(name):
    with open(os.path.join(EVAL, f"{name}.json"), encoding="utf-8") as f:
        cases = json.load(f)["cases"]
    misses = [c["id"] for c in cases if c.get("confident", True)
              and _predict(c["classifier"], c["input"]) != c["label"]]
    assert misses == []


# --- spam / robocall evidence ---------------------------------------------

def test_one_weak_phrase_is_never_spam():
    for line in ("can you hear me?", "I gotta act now", "this is urgent",
                 "I'd like to make a donation", "congratulations on the new shop"):
        assert not spamscore.is_spam(line), line


def test_one_script_phrase_is_spam_when_nothing_says_customer():
    assert spamscore.is_spam("calling about your car's extended warranty")
    assert spamscore.is_spam("press 1 to speak with a representative")


def test_customer_with_one_script_phrase_stays_a_customer():
    # threshold: 2 points alone, 4 points once a customer cue is present
    s = spamscore.score("my extended warranty won't cover it, I need a used alternator")
    assert s["customer"] and s["score"] == 2 and not s["spam"]
    assert spamscore.SPAM_MIN == 2 and spamscore.SPAM_MIN_CUSTOMER == 4


def test_two_script_phrases_outweigh_a_customer_word():
    assert spamscore.is_spam("we help junk removal businesses get more leads")


def test_evidence_adds_up_across_turns_and_counts_each_cue_once():
    turns = ["and the voice service", "customers cannot find your business"]
    assert spamscore.score(turns[:1])["score"] == 0
    assert spamscore.is_spam(turns)
    assert spamscore.score(["press 1", "press 1", "press 1"])["score"] == 2


def test_customer_call_is_not_suspected_and_recording_is():
    customer = [("caller", "do you have a tailgate for a 2003 ram"), ("caller", "can you hear me")]
    robo = [("caller", "your google listing is unverified, press one to verify")]
    assert not robocalls.suspected(customer)
    assert robocalls.suspected(robo)
    assert robocalls.suspected([("caller", "hi"), ("note", "SPAM/telemarketer script detected")])


def test_opt_out_and_toll_free_callback_count_as_a_recording():
    assert agent.RECORDING_RE.search("call 888 555 0100 to opt out")
    assert agent.RECORDING_RE.search("press nine to be removed")
    assert not agent.RECORDING_RE.search("call me back at 570 555 0100")


# --- escalation, goodbye, arrangement -------------------------------------

def test_refund_cancel_and_order_problems_escalate():
    for line in ("I want a refund", "can I get my money back", "my order got cancelled",
                 "the order was refunded", "please cancel that order", "you charged me twice",
                 "my package never came", "the part arrived cracked", "it doesn't fit"):
        assert agent.ESCALATE_RE.search(line), line


def test_damage_to_a_vehicle_is_a_parts_request_not_a_complaint():
    for line in ("my car got damaged in a wreck, I need a fender",
                 "I have an issue finding the part number", "my car doesn't work, needs a pump",
                 "please return my call", "I never got a call back",
                 "I got the truck and the bumper is cracked"):
        assert not agent.ESCALATE_RE.search(line), line


def test_negated_hang_up_and_take_care_of_are_not_goodbyes():
    for line in ("don't hang up", "please do not hang up", "I'll take care of it"):
        assert not agent.END_RE.search(line), line
    for line in ("okay see ya", "see you then", "take care"):
        assert agent.END_RE.search(line), line


def test_owner_presence_is_a_person_request_but_his_home_is_private():
    for line in ("is Anne there?", "is he around", "is Anne available today",
                 "I called yesterday and nobody picked up", "nobody answered the phone"):
        assert not agent.arrange_re("Anne").search(line), line
    for line in ("is Anne home right now", "is he home tonight", "where does he live"):
        assert agent.arrange_re("Anne").search(line), line


def test_visiting_a_web_page_is_not_a_visit_request():
    assert not agent.ARRANGE_RE.search("I visited your ebay store")
    assert agent.ARRANGE_RE.search("could I visit the yard tomorrow")


# --- SMS -----------------------------------------------------------------

def _text(phone, body):
    phone["inbox"].append({"_id": str(len(phone["inbox"]) + 1), "address": A, "body": body})
    sms.poll(CFG, log=lambda m: None)


def test_spam_text_is_declined_without_a_model_call(bot):  # noqa: F811
    _text(bot, "hi")  # first run only records the high-water mark
    _text(bot, "Your vehicle warranty is about to expire. Reply STOP to opt out")
    assert bot["sent"] == [(A, sms.SPAM_SMS_REPLY)]
    assert bot["brain"] == []


def test_short_customer_text_reaches_the_brain(bot):  # noqa: F811
    _text(bot, "hi")
    _text(bot, "can you hear me now lol. do you take car batteries")
    assert len(bot["brain"]) == 1 and bot["sent"][-1][1] != sms.SPAM_SMS_REPLY


def test_text_reply_never_promises_a_refund():
    assert sms._unsafe_reply("We will refund your order today.", "")
