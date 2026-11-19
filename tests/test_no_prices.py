"""Pricing is the owner's call. On a call or a text, a price question gets the
fixed hand-off line (no model call), a model reply that names a price is
replaced before it goes out, retrieved context carries no money amounts, and
the owner alert is tagged so he knows a price is waiting on him. Years, part
numbers, phone numbers, hours and addresses are not prices and pass."""
import tempfile
from unittest import mock

import pytest

from callscoot import agent, notify, pricing, sms, vault_rag
from tests.test_call_flow import PERSONA, _run

CFG = {"persona": {"owner": "Anne"}}
CALL_LINE = pricing.handoff_line(CFG)
SMS_LINE = pricing.handoff_line(CFG, spoken=False)

PRICE_ASKS = [
    "How much is the alternator?",
    "what do you want for the distributor cap",
    "do you charge for scrap pickup?",
    "would you take 40 for it",
    "how much to ship it to Ohio",
    "any discount if I buy two?",
    "what's your best price",
    "can you give me a quote on junk removal",
    "is shipping free?",
    "what is it worth to you",
    "cuánto cuesta",
]

NOT_PRICE_ASKS = [
    "has my order shipped yet?",
    "I need a lower control arm for a 2008 Grand Cherokee",
    "how much longer will it take",
    "is Anne there",
    "do you have part number WIX 51952",
    "I have a car to scrap in Yourtown",
]

LEAKS = [
    "It is $24.99.",
    "That one is forty-nine dollars.",
    "We can do 20 bucks.",
    "I can give you a discount on two.",
    "Shipping is free on that one.",
    "The price is 45.",
    "It would run you about fifty.",
    "It is listed at forty nine ninety nine.",
    "We pay 200 for a junk car.",
    "Drop-off is cheaper than pickup.",
    "Take 10% off today.",
    "Cuesta veinte dólares.",
    "Just a few bucks.",
]

CLEAN = [
    "I have a 2008 Ford F-150 5.4, part number WIX 51952.",
    "Carlson H5758Q fits 2004 to 2012 Cobalt per the listing.",
    "We are in Yourtown, ST, by appointment only.",
    "It is listed for 1994 to 1997 trucks.",
    "Is that a two thousand eight Grand Cherokee?",
    "Shipping is three to five business days.",
    "Anne will call you back at the number ending 0123.",
    "Hours are by appointment only.",
    "Send the part number to this same number, 570-555-0123.",
    CALL_LINE,
    SMS_LINE,
]


@pytest.mark.parametrize("text", PRICE_ASKS)
def test_price_questions_are_seen(text):
    assert pricing.asks_price(text)


@pytest.mark.parametrize("text", NOT_PRICE_ASKS)
def test_other_questions_are_not_price_questions(text):
    assert not pricing.asks_price(text)


@pytest.mark.parametrize("text", LEAKS)
def test_a_reply_naming_a_price_is_caught(text):
    assert pricing.price_leak(text)


@pytest.mark.parametrize("text", CLEAN)
def test_years_part_numbers_phones_hours_and_addresses_pass(text):
    assert pricing.price_leak(text) == ""


def test_context_reaches_the_model_with_no_money_in_it():
    raw = "OrderID=1 | Total=$24.94 | ships free\nScrap pickup: we pay 40 dollars a ton. Call 570-555-0123."
    out = vault_rag.redact(raw)
    assert "$" not in out and "40 dollars" not in out
    assert "570-555-0123" in out and "OrderID=1" in out
    assert pricing.price_leak(pricing.scrub("$1,200.00 or 12 bucks")) == ""


def test_mask_never_shows_the_whole_number():
    assert pricing.mask("+15705550123") == "***0123"
    assert "5705550123" not in pricing.mask("5705550123")


def test_hand_off_line_is_said_word_for_word_and_can_be_set():
    assert CALL_LINE in agent.fixed_lines(CFG)
    cfg = {**CFG, "call": {"price_reply": "Ask Anne."}, "sms": {"price_reply": "Text Anne."}}
    assert pricing.handoff_line(cfg) == "Ask Anne."
    assert pricing.handoff_line(cfg, spoken=False) == "Text Anne."


# --- calls ------------------------------------------------------------------

def test_price_question_on_a_call_gets_the_hand_off_line_with_no_model_call():
    said, calls = _run(["How much do you want for the alternator?"], [])
    assert calls == []
    assert pricing.handoff_line({**PERSONA}) in said


def test_model_reply_with_a_price_never_reaches_the_caller():
    said, calls = _run(["I need a starter for a 2008 Ford Ranger"],
                       ["We have one listed at forty nine dollars."])
    assert len(calls) == 1
    assert not any("forty nine dollars" in s for s in said)
    assert pricing.handoff_line({**PERSONA}) in said


def test_clean_model_reply_still_goes_out():
    said, _ = _run(["I need a starter for a 2008 Ford Ranger"],
                   ["What is the part number on the old one?"])
    assert "What is the part number on the old one?" in said


def _call_alert(utterances, reply):
    cfg = {"logs": {"dir": tempfile.mkdtemp()},
           "call": {"thinking_cue": False, "farewell": "F", "voicemail_reply": "VM"}, **PERSONA}
    utter = iter(utterances)
    sent = []
    with mock.patch.object(agent.phone, "call_state", return_value={"state": 2}), \
         mock.patch.object(agent.phone, "hangup"), \
         mock.patch.object(agent.audio, "wait_for_hfp"), \
         mock.patch.object(agent.audio, "record_utterance", return_value={"path": "x"}), \
         mock.patch.object(agent, "_transcribe_saved", side_effect=lambda *a: next(utter, "")), \
         mock.patch.object(agent.vault_rag, "build_context", return_value=("", [])), \
         mock.patch.object(agent.llm, "chat", return_value=(reply, "f")), \
         mock.patch.object(agent, "earlier_contact", return_value=""), \
         mock.patch.object(agent, "summarize", return_value="s"), \
         mock.patch.object(notify, "telegram_send",
                           side_effect=lambda c, t, reply_markup=None: sent.append(t) or (False, "dry")), \
         mock.patch.object(agent.audio, "SessionRecorder") as rec, \
         mock.patch.object(agent.tts, "synth", return_value=tempfile.mkstemp()[1]), \
         mock.patch.object(agent.tts, "synth_cached", return_value=tempfile.mkstemp()[1]), \
         mock.patch.object(agent, "_archive"), \
         mock.patch.object(agent.audio, "play_to_sink"):
        rec.return_value.armed = False
        rec.return_value.finalize.return_value = None
        agent.run_session(cfg, number="5705550123", speak=True, join_live=True)
    return sent[0].splitlines()[0]


def test_owner_alert_says_a_price_is_waiting_on_him():
    assert "PRICE REQUEST" in _call_alert(["how much is the alternator"], "unused")
    assert "PRICE REQUEST" in _call_alert(["got a starter for a 2008 Ranger"], "It is $40.")
    assert "PRICE REQUEST" not in _call_alert(["got a starter for a 2008 Ranger"], "What year?")


def test_alert_label_keeps_its_old_meaning_beside_a_price_request():
    p = pricing.PRICE_REQUEST
    assert notify.alert_label(CFG, False, {p}).startswith(" — 💲 PRICE REQUEST")
    assert notify.alert_label(CFG, True, {"asked_for_person", p}).startswith(" — asked for Anne")
    assert notify.alert_label(CFG, True, {"escalation", p}).startswith(" ⚠️ ESCALATION")
    assert notify.alert_label(CFG, False, set()) == ""


# --- texts ------------------------------------------------------------------

@pytest.fixture
def texts(tmp_path, monkeypatch):
    def no_phone(*a, **kw):
        raise AssertionError("test tried to reach the phone")

    monkeypatch.setattr(sms.subprocess, "run", no_phone)
    monkeypatch.setattr(sms, "STATE_FILE", str(tmp_path / "state.json"))
    inbox, sent, brain, alerts = [], [], [], []
    reply = {"text": "We have it listed."}
    monkeypatch.setattr(sms, "_inbox", lambda: list(inbox))
    monkeypatch.setattr(sms, "_sent", lambda: [])
    monkeypatch.setattr(sms, "_is_contact", lambda addr: False)
    monkeypatch.setattr(sms.vault_rag, "build_context", lambda cfg, body, **kw: ("", []))
    monkeypatch.setattr(sms, "llm_chat", lambda cfg, m: brain.append(m) or (reply["text"], "fake"))
    monkeypatch.setattr(sms.notify, "send_call_alert",
                        lambda *a, **kw: alerts.append(kw.get("reasons") or set()))
    monkeypatch.setattr(sms, "_send_sms", lambda cfg, addr, text: sent.append(text) or True)
    stock = tmp_path / "inv.csv"
    stock.write_text("item_id,title,price,qty_available\n1,Ford Ranger Starter Motor,49.99,1\n")
    cfg = {"persona": {"owner": "Anne"},
           "sms": {"stock_file": str(stock), "max_replies_per_hour": 9, "max_replies_per_day": 50}}

    def poll(body):
        inbox.append({"_id": str(len(inbox) + 1), "address": "+15705550123", "body": body})
        sms.poll(cfg, log=lambda m: None)

    poll("hello")  # the first run only records the high-water mark
    sent.clear()
    return {"poll": poll, "sent": sent, "brain": brain, "alerts": alerts, "reply": reply}


def test_price_question_by_text_gets_the_hand_off_line_with_no_model_call(texts):
    texts["poll"]("how much for the ranger starter motor?")
    assert texts["sent"] == [SMS_LINE]
    assert texts["brain"] == []
    assert pricing.PRICE_REQUEST in texts["alerts"][-1]


def test_model_text_with_a_price_is_blocked(texts):
    texts["reply"]["text"] = "We have the Ford Ranger Starter Motor listed at $49.99."
    texts["poll"]("got a starter motor for a ford ranger?")
    assert texts["sent"] == [SMS_LINE]
    assert pricing.PRICE_REQUEST in texts["alerts"][-1]


def test_store_listings_reach_the_model_without_prices(texts):
    texts["poll"]("got a starter motor for a ford ranger?")
    system = texts["brain"][-1][0]["content"]
    assert "Ford Ranger Starter Motor" in system
    assert "49.99" not in system
    assert texts["sent"] == ["We have it listed."]
