"""Bluetooth to the phone is brought back by the watch loop, not by a person."""
from callscoot import phone, watch


def _reset():
    watch._headset.update(next=0.0, busy=False, down=False)


def test_mac_comes_from_the_headset_pcm():
    cfg = {"audio": {"bluealsa_pcm": "bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=sco"}}
    assert phone.phone_mac(cfg) == "AA:BB:CC:DD:EE:FF"
    assert phone.phone_mac({}) == ""
    assert phone.bt_connect({}) is False


def test_reconnects_when_headset_is_down(monkeypatch):
    _reset()
    calls = []
    monkeypatch.setattr(watch, "_headset_ready", lambda cfg: False)
    monkeypatch.setattr(phone, "bt_connect", lambda cfg, timeout=15: calls.append(1) or True)
    watch.keep_headset({}, now=100.0, wait=True)
    assert calls == [1] and watch._headset["busy"] is False


def test_leaves_a_working_headset_alone_and_rate_limits(monkeypatch):
    _reset()
    calls, checks = [], []
    monkeypatch.setattr(watch, "_headset_ready", lambda cfg: checks.append(1) or True)
    monkeypatch.setattr(phone, "bt_connect", lambda cfg, timeout=15: calls.append(1) or True)
    watch.keep_headset({}, now=100.0)
    watch.keep_headset({}, now=101.0)
    assert calls == [] and checks == [1]
    watch.keep_headset({}, now=100.0 + watch.HEADSET_RETRY + 1)
    assert checks == [1, 1]


def test_a_failed_reconnect_is_retried_later(monkeypatch):
    _reset()
    calls = []
    monkeypatch.setattr(watch, "_headset_ready", lambda cfg: False)
    monkeypatch.setattr(phone, "bt_connect", lambda cfg, timeout=15: calls.append(1) and False)
    watch.keep_headset({}, now=100.0, wait=True)
    watch.keep_headset({}, now=100.0 + watch.HEADSET_RETRY + 1, wait=True)
    assert calls == [1, 1]


def test_headset_ok_reports_a_missing_sound_path(monkeypatch):
    monkeypatch.setattr(watch, "_headset_ready", lambda cfg: False)
    assert watch.headset_ok({}) is False
    monkeypatch.setattr(watch, "_headset_ready", lambda cfg: True)
    assert watch.headset_ok({}) is True


def test_headset_ok_does_not_block_calls_when_the_check_itself_breaks(monkeypatch):
    def boom(cfg):
        raise RuntimeError("check failed")
    monkeypatch.setattr(watch, "_headset_ready", boom)
    assert watch.headset_ok({}) is True
