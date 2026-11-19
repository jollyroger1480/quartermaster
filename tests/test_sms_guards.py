from callscoot import sms

NOW = 2_000_000
DAY = 86400


def _m(addr, body, age):
    return {"_id": "1", "address": addr, "body": body, "date": str(int((NOW - age) * 1000))}


def test_text_older_than_a_week_is_too_old():
    assert sms._too_old(_m("+15705550100", "hi", 8 * DAY), NOW)
    assert not sms._too_old(_m("+15705550100", "hi", 6 * DAY), NOW)


def test_owner_text_in_the_week_blocks_replies_but_secretary_texts_do_not():
    bot = {"5705550100": [[NOW - 60, "Anne will follow up."]]}
    own = [_m("(570) 555-0100", "Anne  will follow up.", 60)]
    assert not sms._owner_texted(own, "+15705550100", bot, NOW)
    assert sms._owner_texted(own + [_m("+15705550100", "I'll be there at 3", 2 * DAY)], "+15705550100", bot, NOW)
    assert not sms._owner_texted([_m("+15705550100", "old chat", 9 * DAY)], "+15705550100", bot, NOW)
    assert not sms._owner_texted([_m("+15705550199", "someone else", 60)], "+15705550100", bot, NOW)
