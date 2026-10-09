"""Unit tests for the HFP AT-command parsing (no hardware needed).

The Moto AG terminates responses with ":OK" instead of a bare "OK" — these
tests pin that quirk so call control does not regress.
"""
from callscoot import hfp

CIND_TEST = ('\r\n+CIND: ("call",(0,1)),("callsetup",(0-3)),("service",(0-1)),'
             '("signal",(0-5)),("roam",(0,1)),("battchg",(0-5)),("callheld",(0-2))'
             '\r\n\r\n:OK\r\n')
CIND_READ = '\r\n+CIND: 0,0,1,4,0,4,0\r\n\r\n:OK\r\n'
CLCC_RINGING = '\r\n+CLCC: 1,1,4,0,0,"+15705551234",161\r\n\r\n:OK\r\n'
CLCC_ACTIVE = '\r\n+CLCC: 1,1,0,0,0,"+15705551234",161\r\n\r\n:OK\r\n'
CLCC_IDLE = '\r\n:OK\r\n'


def test_cind_names_from_quirky_terminator():
    names = hfp.parse_cind_map(CIND_TEST)
    assert "call" in names and "callsetup" in names and "callheld" in names


def test_cind_values_ignores_test_response():
    assert hfp.parse_cind_values(CIND_TEST) == []
    assert hfp.parse_cind_values(CIND_READ) == [0, 0, 1, 4, 0, 4, 0]


def test_clcc_ringing_active_idle():
    assert hfp.parse_clcc(CLCC_IDLE) == []
    ring = hfp.parse_clcc(CLCC_RINGING)
    assert ring[0]["stat"] == 4
    assert ring[0]["number"] == "+15705551234"
    assert hfp.parse_clcc(CLCC_ACTIVE)[0]["stat"] == 0


def test_interpret_ringing_by_clcc():
    names = hfp.parse_cind_map(CIND_TEST)
    st = hfp.interpret(names, [0, 0, 1, 4, 0, 4, 0], hfp.parse_clcc(CLCC_RINGING))
    assert st == {"state": 1, "number": "+15705551234"}


def test_interpret_ringing_by_indicator_when_clcc_empty():
    names = hfp.parse_cind_map(CIND_TEST)
    st = hfp.interpret(names, [0, 1, 1, 4, 0, 4, 0], [])  # callsetup=1
    assert st == {"state": 1, "number": None}


def test_interpret_offhook_and_idle():
    names = hfp.parse_cind_map(CIND_TEST)
    assert hfp.interpret(names, [1, 0, 1, 4, 0, 4, 0], [])["state"] == 2
    assert hfp.interpret(names, [0, 0, 1, 4, 0, 4, 0], [])["state"] == 0


def test_failed_tolerates_colon_ok():
    assert not hfp._failed(CIND_TEST)
    assert not hfp._failed(CLCC_IDLE)
    assert hfp._failed("\r\nERROR\r\n")
    assert not hfp._failed("\r\nERROR\r\n\r\n:OK\r\n")
