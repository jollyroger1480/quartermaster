"""A phone swap or restored SMS history must not stall the watch loop.

Message ids belong to one phone's SMS database, so after a swap the stored
last_id points into old history and thousands of old texts look new. They are
skipped in one step (no contact lookup each, one log line), the poll stays
short so rings are still answered, and a text that really is new still gets
its normal handling. Nothing here may reach the phone."""
import time

import pytest

from callscoot import sms


@pytest.fixture
def bot(tmp_path, monkeypatch):
    def no_phone(*a, **kw):
        raise AssertionError("test tried to reach the phone")

    monkeypatch.setattr(sms.subprocess, "run", no_phone)
    monkeypatch.setattr(sms, "STATE_FILE", str(tmp_path / "state.json"))
    inbox, sent, lookups, logs = [], [], [], []
    monkeypatch.setattr(sms, "_inbox", lambda: list(inbox))
    monkeypatch.setattr(sms, "_sent", lambda: [])
    monkeypatch.setattr(sms, "_is_contact", lambda addr: lookups.append(addr) or False)
    monkeypatch.setattr(sms.vault_rag, "build_context", lambda cfg, body, **kw: ("", []))
    monkeypatch.setattr(sms, "llm_chat", lambda cfg, messages: ("We will follow up.", "fake"))
    monkeypatch.setattr(sms.notify, "send_call_alert", lambda *a, **kw: None)
    monkeypatch.setattr(sms, "_send_sms", lambda cfg, addr, text: sent.append(addr) or True)
    return {"inbox": inbox, "sent": sent, "lookups": lookups, "logs": logs}


def _text(i, age_s, addr="+15705550100", body="do you have a starter"):
    return {"_id": str(i), "address": addr, "body": body,
            "date": str(int((time.time() - age_s) * 1000))}


def _poll(bot, cfg=None):
    sms.poll(cfg or {}, log=bot["logs"].append)


def test_restored_history_is_skipped_in_one_quiet_step(bot):
    sms._save_state({"last_id": 10, "checked_at": time.time() - 600})
    # new phone: ids far past last_id, all dated days before the last check
    bot["inbox"].extend(_text(i, 86400 * (2 + i % 400), f"+1570555{i % 10000:04d}")
                        for i in range(11, 1500))
    _poll(bot)
    assert bot["sent"] == []
    assert bot["lookups"] == []          # no adb contact lookup per old text
    assert len(bot["logs"]) == 1 and "skipped 1489 older texts" in bot["logs"][0]
    assert sms._load_state()["last_id"] == 1499
    bot["logs"].clear()
    _poll(bot)                            # remembered: nothing is logged again
    assert bot["logs"] == [] and bot["sent"] == []


def test_state_without_checked_at_uses_the_file_time(bot):
    sms._save_state({"last_id": 10})      # written by the old code
    bot["inbox"].extend([_text(11, 86400 * 30), _text(12, 86400 * 3)])
    _poll(bot)
    assert bot["sent"] == [] and bot["lookups"] == []
    assert sms._load_state()["last_id"] == 12


def test_a_text_that_arrives_after_the_last_check_is_still_answered(bot):
    sms._save_state({"last_id": 10, "checked_at": time.time() - 600})
    bot["inbox"].extend([_text(11, 86400 * 3), _text(12, 5, "+15705550123")])
    _poll(bot)
    assert bot["sent"] == ["+15705550123"]
    assert sms._load_state()["last_id"] == 12


def test_old_texts_are_checked_for_age_before_any_contact_lookup(bot):
    sms._save_state({"last_id": 10, "checked_at": time.time() - 86400 * 30})
    bot["inbox"].extend(_text(i, 86400 * 9) for i in range(11, 40))  # after the check, but > 7 days
    _poll(bot)
    assert bot["lookups"] == [] and bot["sent"] == []
    assert sms._load_state()["last_id"] == 39


def test_one_poll_gives_the_watch_loop_back_quickly(bot, monkeypatch):
    sms._save_state({"last_id": 10, "checked_at": time.time() - 600})
    bot["inbox"].extend(_text(i, 5, f"+1570555{i:04d}") for i in range(11, 31))
    monkeypatch.setattr(sms, "_is_contact", lambda addr: time.sleep(0.2) or True)
    monkeypatch.setattr(sms, "POLL_BUDGET", 0.5)
    t = time.monotonic()
    _poll(bot)
    assert time.monotonic() - t < 1.5
    first = sms._load_state()["last_id"]
    assert 11 <= first < 30               # stopped early, progress saved
    for _ in range(20):
        _poll(bot)
    assert sms._load_state()["last_id"] == 30
    assert bot["sent"] == []
