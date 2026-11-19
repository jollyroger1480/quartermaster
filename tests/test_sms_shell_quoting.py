"""Phone-shell commands must treat sender numbers and reply text as data."""
from callscoot import sms


def test_adb_shell_quotes_every_argument(monkeypatch):
    seen = {}

    class P:
        stdout = ""

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return P()

    monkeypatch.setattr(sms.subprocess, "run", fake_run)
    sms._adb_shell(["input", "text", "ok; reboot $(id) `x` 'q'"])
    assert seen["argv"][:2] == ["adb", "shell"] and len(seen["argv"]) == 3
    import shlex
    assert shlex.split(seen["argv"][2]) == ["input", "text", "ok; reboot $(id) `x` 'q'"]


def test_only_plain_plus_digit_numbers_are_engaged():
    assert sms._is_mobile_number("+15705550123")
    for bad in ["+15705550123;id", "+1 570 555 0123", "5705550123", "+1570", "AMAZON", "", "+15705550123/../x"]:
        assert not sms._is_mobile_number(bad)
