from callscoot import sms


def _cfg(tmp_path, rows):
    f = tmp_path / "inv.csv"
    f.write_text("item_id,title,price,qty_available\n" + "".join(rows))
    return {"sms": {"stock_file": str(f)}}


def test_matching_titles_come_back_best_first(tmp_path):
    cfg = _cfg(tmp_path, [
        "1,1975-1979 Ford F150 Distributor Cap NOS,19.99,1\n",
        "2,Chevrolet Camaro Brake Hose,12.00,1\n",
        "3,Ford F150 Distributor Cap Sold Out,9.99,0\n",
    ])
    lines, checked = sms._stock_matches(cfg, "do you have a distributor cap for a 78 F150")
    assert checked and lines == ["- 1975-1979 Ford F150 Distributor Cap NOS"]  # no price: owner only


def test_no_match_is_a_real_checked_answer(tmp_path):
    cfg = _cfg(tmp_path, ["2,Chevrolet Camaro Brake Hose,12.00,1\n"])
    assert sms._stock_matches(cfg, "need a radiator for a Volvo 240") == ([], True)


def test_missing_file_is_reported_as_not_checked(tmp_path):
    assert sms._stock_matches({"sms": {"stock_file": str(tmp_path / "gone.csv")}}, "f150 cap") == ([], False)


def test_price_keeps_its_dollar_sign_and_shell_characters_are_dropped():
    assert sms._clean_text("Listed for $22.99; `rm` (x) & more") == "Listed for $22.99 rm x  more"
