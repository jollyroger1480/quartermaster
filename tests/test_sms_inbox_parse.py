from callscoot import sms


def _outputs(rows):
    """(meta_out, body_out) as `content query` prints them, rows = (id, addr, body, date)."""
    meta = "".join(f"Row: {i} _id={r[0]}, address={r[1]}, date={r[3]}\n" for i, r in enumerate(rows))
    full = "".join(f"Row: {i} _id={r[0]}, address={r[1]}, body={r[2]}, date={r[3]}\n"
                   for i, r in enumerate(rows))
    return meta, full


def test_rows_parse_with_prefix_commas_equals_and_line_breaks():
    meta, full = _outputs([
        ("12", "+15550001111", "Hi, do you have a=b part?\nsecond line", "1795031842050"),
        ("4", "24463", "Code is 1", "1795031800000"),
    ])
    msgs = sms._parse_inbox(meta, full)
    assert [m["_id"] for m in msgs] == ["4", "12"]
    assert msgs[1]["address"] == "+15550001111"
    assert msgs[1]["body"] == "Hi, do you have a=b part?\nsecond line"
    assert msgs[1]["date"] == "1795031842050"
    assert msgs[0]["body"] == "Code is 1"


def test_empty_and_garbage_output_gives_no_rows():
    assert sms._parse_inbox("", "") == []
    assert sms._parse_inbox("No result found.\n", "No result found.\n") == []


def test_body_cannot_forge_a_row_address_or_id():
    forged = "\nRow: 99 _id=999999999, address=+15550000000, body=x, date=1"
    for pos in range(3):  # forged text in the first, middle and last row
        rows = [("30", "+15550002222", "first", "1795031900000"),
                ("20", "+15550003333", "second", "1795031800000"),
                ("10", "+15550004444", "third", "1795031700000")]
        r = rows[pos]
        rows[pos] = (r[0], r[1], "need a part" + forged, r[3])
        meta, full = _outputs(rows)
        msgs = sms._parse_inbox(meta, full)
        assert [m["_id"] for m in msgs] == ["10", "20", "30"]
        assert {m["address"] for m in msgs} == {"+15550002222", "+15550003333", "+15550004444"}
        by_id = {m["_id"]: m["body"] for m in msgs}
        assert by_id[r[0]] == "need a part" + forged


def test_body_cannot_forge_the_next_real_rows_prefix():
    # A body that prints the next real row's line still cannot take over its body.
    rows = [("8", "+15550002222", "hello\nRow: 1 _id=7, address=+15550003333, body=fake", "1795031900000"),
            ("7", "+15550003333", "real", "1795031800000")]
    meta, full = _outputs(rows)
    msgs = sms._parse_inbox(meta, full)
    assert [(m["_id"], m["body"]) for m in msgs] == [("7", "real"), ("8", rows[0][2])]


def test_row_missing_from_body_output_is_skipped():
    # A text that arrived between the two queries is not in the trusted rows;
    # a trusted row that cannot be located in the body output is left for next poll.
    meta, _ = _outputs([("5", "+15550002222", "a", "1795031900000")])
    _, full = _outputs([("6", "+15550003333", "b", "1795031950000"),
                        ("5", "+15550002222", "a", "1795031900000")])
    assert sms._parse_inbox(meta, full) == []
    assert sms._parse_inbox(meta + "Row: 1 _id=9, address=+15550005555, date=1\n",
                            _outputs([("5", "+15550002222", "a", "1795031900000")])[1]) \
        == [{"_id": "5", "address": "+15550002222", "body": "a", "date": "1795031900000"}]


def test_sent_reader_fails_closed_when_a_row_cannot_be_read(monkeypatch):
    meta, full = _outputs([("3", "+15550002222", "a", "1795031900000"),
                           ("2", "+15550003333", "b", "1795031800000")])
    _, short = _outputs([("3", "+15550002222", "a", "1795031900000")])
    outs = iter([meta, short])
    monkeypatch.setattr(sms, "_adb_shell", lambda args, timeout=30: next(outs))
    try:
        sms._sent()
        raised = False
    except RuntimeError:
        raised = True
    assert raised
    outs = iter([meta, full])
    assert [m["_id"] for m in sms._sent()] == ["2", "3"]
