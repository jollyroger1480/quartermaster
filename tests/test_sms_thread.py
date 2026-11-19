from callscoot import sms

NOW = 2_000_000


def _m(i, addr, body, age=60):
    return {"_id": str(i), "address": addr, "body": body, "date": str(int((NOW - age) * 1000))}


def test_thread_is_one_number_in_order_and_ends_on_the_text_being_answered():
    inbox = [_m(1, "+15705550100", "I have a car to scrap"), _m(3, "5705550100", "Sam, Dunmore, no title"),
             _m(4, "+15705550199", "other person"), _m(5, "+15705550100", "later text")]
    sent = [_m(2, "(570) 555-0100", "What's your name and town?"), _m(6, "+15705550100", "later reply")]
    assert sms._thread_messages(inbox, sent, "+15705550100", 3, now=NOW) == [
        {"role": "user", "content": "I have a car to scrap"},
        {"role": "assistant", "content": "What's your name and town?"},
        {"role": "user", "content": "Sam, Dunmore, no title"},
    ]


def test_old_conversation_is_left_out_but_the_new_text_is_kept():
    inbox = [_m(1, "+15705550100", "last month", age=86400 * 30), _m(2, "+15705550100", "today")]
    assert sms._thread_messages(inbox, [], "+15705550100", 2, now=NOW) == [{"role": "user", "content": "today"}]
