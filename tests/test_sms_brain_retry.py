"""A text whose brain call errors is retried next pass, in order, and gives up
after sms.BRAIN_MAX_TRIES so it cannot block the queue forever. Nothing here may
reach the phone: subprocess.run is a tripwire; the brain and the send are fakes."""
import pytest

from callscoot import sms


@pytest.fixture
def bot(tmp_path, monkeypatch):
    def no_phone(*a, **kw):
        raise AssertionError("test tried to reach the phone")

    monkeypatch.setattr(sms.subprocess, "run", no_phone)
    monkeypatch.setattr(sms, "STATE_FILE", str(tmp_path / "state.json"))
    inbox, sent, broken, logs = [], [], set(), []
    monkeypatch.setattr(sms, "_inbox", lambda: list(inbox))
    monkeypatch.setattr(sms, "_sent", lambda: [])
    monkeypatch.setattr(sms, "_is_contact", lambda addr: False)
    monkeypatch.setattr(sms.vault_rag, "build_context", lambda cfg, body, **kw: ("", []))

    def brain(cfg, messages):
        body = messages[-1]["content"]
        if body in broken:
            raise RuntimeError("provider down")
        return f"re: {body}", "fake"

    monkeypatch.setattr(sms, "llm_chat", brain)
    monkeypatch.setattr(sms.notify, "send_call_alert", lambda *a, **kw: None)
    monkeypatch.setattr(sms, "_send_sms",
                        lambda cfg, addr, text: sent.append(text) or True)
    sms._save_state({"last_id": 0})
    cfg = {"sms": {"enabled": True, "max_replies_per_hour": 100, "max_replies_per_day": 100}}

    def poll():
        sms.poll(cfg, log=logs.append)

    return {"inbox": inbox, "sent": sent, "broken": broken, "logs": logs, "poll": poll}


def _text(i, body, addr=None):
    return {"_id": str(i), "address": addr or f"+157055501{i:02d}", "body": body}


def test_errored_text_is_retried_next_pass(bot):
    bot["inbox"].append(_text(1, "got a starter"))
    bot["broken"].add("got a starter")
    bot["poll"]()
    assert bot["sent"] == []
    assert sms._load_state()["last_id"] == 0
    bot["broken"].clear()
    bot["poll"]()
    assert bot["sent"] == ["re: got a starter"]
    assert sms._load_state()["last_id"] == 1


def test_later_text_not_answered_ahead_of_errored_one(bot):
    bot["inbox"].extend([_text(1, "got a starter"), _text(2, "second")])
    bot["broken"].add("got a starter")
    bot["poll"]()
    assert bot["sent"] == []  # the pass stopped at the errored text
    bot["broken"].clear()
    bot["poll"]()
    assert bot["sent"] == ["re: got a starter", "re: second"]


def test_gives_up_after_max_tries_and_queue_moves_on(bot):
    bot["inbox"].extend([_text(1, "cursed"), _text(2, "second")])
    bot["broken"].add("cursed")
    for _ in range(sms.BRAIN_MAX_TRIES - 1):
        bot["poll"]()
        assert bot["sent"] == []
    bot["poll"]()  # final try: marked handled, the next text is answered
    assert bot["sent"] == ["re: second"]
    state = sms._load_state()
    assert state["last_id"] == 2 and "brain_fail" not in state
    assert any("giving up" in line for line in bot["logs"])
    bot["poll"]()
    assert bot["sent"] == ["re: second"]  # never answered later


def test_success_resets_the_counter(bot):
    bot["inbox"].append(_text(1, "flaky"))
    bot["broken"].add("flaky")
    bot["poll"]()
    assert sms._load_state()["brain_fail"] == {"id": "1", "count": 1}
    bot["broken"].clear()
    bot["poll"]()
    assert "brain_fail" not in sms._load_state()
    # a new text that errors starts again at 1, not where the old one left off
    bot["inbox"].append(_text(2, "flaky2"))
    bot["broken"].add("flaky2")
    bot["poll"]()
    assert sms._load_state()["brain_fail"] == {"id": "2", "count": 1}
