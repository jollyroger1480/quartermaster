"""Text auto-reply bookkeeping: skipped texts stay skipped, and daily caps fail closed.
Nothing here may reach the phone: subprocess.run is replaced with a tripwire."""
import os

import pytest

from callscoot import sms, watch


@pytest.fixture
def bot(tmp_path, monkeypatch):
    def no_phone(*a, **kw):
        raise AssertionError("test tried to reach the phone")

    monkeypatch.setattr(sms.subprocess, "run", no_phone)
    monkeypatch.setattr(sms, "STATE_FILE", str(tmp_path / "state.json"))
    inbox, sent = [], []
    monkeypatch.setattr(sms, "_inbox", lambda: list(inbox))
    monkeypatch.setattr(sms, "_sent", lambda: [])
    monkeypatch.setattr(sms, "_is_contact", lambda addr: False)
    monkeypatch.setattr(sms.vault_rag, "build_context", lambda cfg, body, **kw: ("", []))
    monkeypatch.setattr(sms, "llm_chat", lambda cfg, messages: ("Anne will follow up.", "fake"))
    monkeypatch.setattr(sms.notify, "send_call_alert", lambda *a, **kw: None)
    result = {"ok": True}

    def fake_send(cfg, addr, text):
        sent.append((addr, text))
        return result["ok"]

    monkeypatch.setattr(sms, "_send_sms", fake_send)
    day = {"v": "20261118"}
    real_strftime = sms.time.strftime
    monkeypatch.setattr(sms.time, "strftime",
                        lambda fmt, *a: day["v"] if fmt == "%Y%m%d" else real_strftime(fmt, *a))
    return {"inbox": inbox, "sent": sent, "day": day, "result": result}


def _text(i, addr="+15705550100", body="do you have a starter"):
    return {"_id": str(i), "address": addr, "body": body}


def _start(bot, cfg, first_id=1):
    bot["inbox"].append(_text(first_id))
    sms.poll(cfg, log=lambda m: None)  # first run only records the high-water mark
    assert bot["sent"] == []


def test_text_over_daily_cap_is_never_answered_later(bot):
    cfg = {"sms": {"max_replies_per_day": 1, "max_replies_per_hour": 10}}
    _start(bot, cfg)
    bot["inbox"].extend([_text(2, "+15705550101"), _text(3, "+15705550102")])
    sms.poll(cfg, log=lambda m: None)
    assert [a for a, _ in bot["sent"]] == ["+15705550101"]
    assert sms._load_state()["last_id"] == 3
    bot["day"]["v"] = "20261119"  # cap resets the next day
    sms.poll(cfg, log=lambda m: None)
    assert [a for a, _ in bot["sent"]] == ["+15705550101"]


def test_text_over_hourly_limit_is_never_answered_later(bot, monkeypatch):
    cfg = {"sms": {"max_replies_per_day": 50, "max_replies_per_hour": 1}}
    _start(bot, cfg)
    bot["inbox"].extend([_text(2), _text(3)])
    sms.poll(cfg, log=lambda m: None)
    # text 3 hits the limit: it gets only the one fixed limit text, never a model reply
    assert [t for _, t in bot["sent"]] == ["Anne will follow up.",
                                         "Thanks for the text. The owner will follow up."]
    later = sms.time.time() + 7200
    monkeypatch.setattr(sms.time, "time", lambda: later)
    sms.poll(cfg, log=lambda m: None)
    assert len(bot["sent"]) == 2


def test_ignored_senders_are_marked_handled(bot):
    cfg = {"sms": {}}
    _start(bot, cfg)
    bot["inbox"].extend([_text(2, "AMAZON"), _text(3, "+15705550100", body="  ")])
    sms.poll(cfg, log=lambda m: None)
    assert bot["sent"] == [] and sms._load_state()["last_id"] == 3


def test_failed_send_is_still_retried(bot):
    cfg = {"sms": {}}
    _start(bot, cfg)
    bot["inbox"].append(_text(2))
    bot["result"]["ok"] = False
    sms.poll(cfg, log=lambda m: None)
    assert sms._load_state()["last_id"] == 1
    bot["result"]["ok"] = True
    sms.poll(cfg, log=lambda m: None)
    assert len(bot["sent"]) == 2 and sms._load_state()["last_id"] == 2


def test_unreadable_call_counter_counts_as_over_cap(tmp_path, monkeypatch):
    cfg = {"logs": {"dir": str(tmp_path)}, "call": {"max_calls_per_day": 5}}

    def denied(path):
        raise PermissionError(path)

    monkeypatch.setattr(watch.os, "listdir", denied)
    assert watch.calls_today(cfg) is None
    assert watch.over_daily_cap(cfg)
    cfg["call"]["max_calls_per_day"] = 0  # 0 = no limit, still no limit
    assert not watch.over_daily_cap(cfg)


def test_missing_call_log_folder_means_no_calls_yet(tmp_path):
    cfg = {"logs": {"dir": str(tmp_path / "not-made-yet")}, "call": {"max_calls_per_day": 5}}
    assert watch.calls_today(cfg) == 0
    assert not watch.over_daily_cap(cfg)
    assert not os.path.exists(cfg["logs"]["dir"])
