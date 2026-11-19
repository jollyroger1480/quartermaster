"""Hourly reply limit: the first text over the limit gets one fixed text (no model
call, same send path and guards), then silence for that number until the hour
resets. Nothing here reaches the phone: subprocess.run is a tripwire."""
import pytest

from callscoot import sms

FIXED = "Thanks for the text. Anne will follow up."


@pytest.fixture
def bot(tmp_path, monkeypatch):
    def no_phone(*a, **kw):
        raise AssertionError("test tried to reach the phone")

    monkeypatch.setattr(sms.subprocess, "run", no_phone)
    monkeypatch.setattr(sms, "STATE_FILE", str(tmp_path / "state.json"))
    inbox, sent, brain = [], [], []
    clock = {"t": 1_800_000_000.0}
    contacts = set()
    monkeypatch.setattr(sms.time, "time", lambda: clock["t"])
    monkeypatch.setattr(sms, "_inbox", lambda: list(inbox))
    monkeypatch.setattr(sms, "_sent", lambda: [])
    monkeypatch.setattr(sms, "_is_contact", lambda addr: addr in contacts)
    monkeypatch.setattr(sms.vault_rag, "build_context", lambda cfg, body, **kw: ("", []))

    def fake_llm(cfg, messages):
        brain.append(messages)
        return "We have it.", "fake"

    monkeypatch.setattr(sms, "llm_chat", fake_llm)
    monkeypatch.setattr(sms.notify, "send_call_alert", lambda *a, **kw: None)
    result = {"ok": True}

    def fake_send(cfg, addr, text):
        sent.append((addr, text))
        return result["ok"]

    monkeypatch.setattr(sms, "_send_sms", fake_send)
    return {"inbox": inbox, "sent": sent, "brain": brain, "clock": clock,
            "result": result, "contacts": contacts}


CFG = {"persona": {"owner": "Anne"},
       "sms": {"max_replies_per_hour": 2, "max_replies_per_day": 50}}
A, B = "+15705550100", "+15705550101"


def _poll(bot, *texts):
    for addr in texts:
        bot["inbox"].append({"_id": str(len(bot["inbox"]) + 1), "address": addr, "body": "got a starter?"})
    sms.poll(CFG, log=lambda m: None)


def _start(bot):
    _poll(bot, A)  # first run only records the high-water mark
    assert bot["sent"] == []


def test_first_text_over_the_limit_gets_one_fixed_text_then_silence(bot):
    _start(bot)
    _poll(bot, A, A)
    assert len(bot["brain"]) == 2
    _poll(bot, A)
    assert bot["sent"][-1] == (A, FIXED)
    assert len(bot["brain"]) == 2  # no model call for the fixed text
    _poll(bot, A, A, A)
    assert len(bot["sent"]) == 3  # silent for the rest of the hour


def test_limit_text_does_not_stretch_the_window_and_resets_with_it(bot):
    _start(bot)
    _poll(bot, A, A)
    bot["clock"]["t"] += 1800
    _poll(bot, A)  # limit text, half an hour in
    assert bot["sent"][-1] == (A, FIXED)
    bot["clock"]["t"] += 1801  # the two replies are now over an hour old
    _poll(bot, A)
    assert bot["sent"][-1] == (A, "We have it.")  # answered normally again
    assert len(bot["brain"]) == 3


def test_limit_text_uses_the_held_back_wording_and_owner_fallback(bot):
    cfg = {"sms": {}}
    assert sms._follow_up_text(cfg) == "Thanks for the text. The owner will follow up."
    assert sms._follow_up_text(CFG) == FIXED
    assert sms._unsafe_reply(FIXED, "") == ""  # passes the outbound check


def test_limit_text_is_per_number(bot):
    _start(bot)
    _poll(bot, A, A, A, B)
    assert bot["sent"] == [(A, "We have it."), (A, "We have it."), (A, FIXED), (B, "We have it.")]


def test_saved_contact_over_the_limit_still_gets_nothing(bot):
    _start(bot)
    bot["contacts"].add(B)
    _poll(bot, B, B, B, B)
    assert bot["sent"] == []


def test_failed_limit_text_is_retried_once_not_repeated(bot):
    _start(bot)
    _poll(bot, A, A)
    bot["result"]["ok"] = False
    _poll(bot, A)
    assert sms._load_state()["last_id"] == 3  # held for retry
    bot["result"]["ok"] = True
    _poll(bot)
    assert [t for _, t in bot["sent"]].count(FIXED) == 2  # the failed try + the retry
    assert sms._load_state()["last_id"] == 4
    _poll(bot, A)
    assert [t for _, t in bot["sent"]].count(FIXED) == 2


def test_limit_text_is_logged_as_the_bots_own(bot):
    _start(bot)
    _poll(bot, A, A, A)
    state = sms._load_state()
    mine = [t for _, t in state["bot_sent"][sms._digits(A)]]
    assert sms._clean_text(FIXED) in mine  # never mistaken for the owner texting
    assert state["day_count"] == 3
    assert len(state["replies"][A]) == 2  # not counted in the hourly window
