from callscoot import sms

STOCK = "- Sorensen DR-124 Distributor Cap NOS, $14.99\n- Carlson H5864Q clip kit, $9.00"


def test_price_from_a_listing_is_held_back_too():
    # pricing is the owner's call: a listing price is still a price
    assert "price" in sms._unsafe_reply("We have the Sorensen cap listed at $14.99. Search BuccaneerSalvage on eBay.", STOCK)
    assert sms._unsafe_reply("We have the Sorensen DR-124 cap listed. Search BuccaneerSalvage on eBay.", STOCK) == ""


def test_price_not_in_the_listings_is_held_back():
    assert "price" in sms._unsafe_reply("We can give you $300 for it.", STOCK)
    assert "price" in sms._unsafe_reply("It is $14.99 or $12 cash.", STOCK)
    assert "price" in sms._unsafe_reply("That would be $20.", "(searched the store: no matching listing)")


def test_promises_are_held_back():
    for text in ("We'll take it.", "I will buy it tomorrow.", "It will fit your Ranger.",
                 "We can hold it for you.", "We guarantee it.", "You will get a refund."):
        assert "promise" in sms._unsafe_reply(text, STOCK), text
    assert sms._unsafe_reply("Thanks. What is your name and what town is it in? Anne will follow up.", STOCK) == ""


def test_send_is_confirmed_only_by_a_new_row_to_that_number():
    old = {"_id": "10", "address": "+15705550101", "body": "Thanks for the text. Anne will follow up.", "date": "1"}
    other = {"_id": "12", "address": "+15705550199", "body": "Thanks for the text. Anne will follow up.", "date": "2"}
    new = {"_id": "13", "address": "(570) 555-0101", "body": "Thanks for the text. Anne will follow up.", "date": "3"}
    text = "Thanks for the text. Anne will follow up."
    assert not sms._sent_confirmed([old, other], 11, "+15705550101", text)
    assert sms._sent_confirmed([old, other, new], 11, "+15705550101", text)
