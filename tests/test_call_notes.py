"""Half-sentence listen, reply cleanup, and note search. No phone required."""
import os
import tempfile
import unittest

from callscoot.agent import looks_unfinished, tidy_reply
from callscoot.vault_rag import retrieve


class ListenTests(unittest.TestCase):
    def test_short_fragment_and_trailing_number_are_unfinished(self):
        self.assertTrue(looks_unfinished("2011"))
        self.assertTrue(looks_unfinished("Chevy Colorado and"))
        self.assertFalse(looks_unfinished("I need a water pump for a 2011 Chevy"))

    def test_tidy_reply_keeps_one_question(self):
        raw = "We ship that.We ship that. What year is it? And what engine?"
        out = tidy_reply(raw)
        self.assertEqual(out.count("?"), 1)
        self.assertIn("And what engine?", out)
        self.assertNotIn("What year", out)
        self.assertNotIn("We ship that. We ship that", out)


class NoteSearchTests(unittest.TestCase):
    def test_heading_stem_and_latest_sentence(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "notes.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(
                "## Scrap\n\nFree local scrap haul in town.\n\n"
                "## Shipping\n\nWe ship parts across the US.\n\n"
                "## Blank\n\n[FILL IN: do not read this]\n"
            )
        cfg = {"vault": {"root": d, "top_k": 4, "chunk_chars": 700,
                         "lanes": [], "extra_dirs": []}}
        ship = retrieve(cfg, "shipping a radiator")
        self.assertTrue(ship)
        self.assertIn("## Shipping", ship[0][2])
        follow = retrieve(cfg, "scrap haul where do you ship", focus="where do you ship")
        self.assertIn("## Shipping", follow[0][2])
        self.assertFalse(any("[FILL IN" in hit[2] for hit in follow))


class SummaryTests(unittest.TestCase):
    """Owner slip: the caller's own name fills a model 'unknown', and the
    Telegram alert (sent with no parse_mode) carries no markdown symbols."""
    CFG = {"persona": {"owner": "Anne", "shop": "Buccaneer Salvage"}}

    def _summary(self, transcript, model_text):
        from unittest import mock
        from callscoot import agent
        with mock.patch.object(agent.llm, "chat", return_value=(model_text, "f")):
            return agent.summarize(self.CFG, "5705550123", transcript)

    def test_self_given_name_replaces_unknown(self):
        for said, name in (("I'm out in Simpson. Name's Dave.", "Dave"),
                           ("my name is Mary-Jo O'Neil and I have a dryer", "Mary-Jo O'Neil"),
                           ("this is Mike calling back about the radiator", "Mike")):
            out = self._summary([("caller", said)],
                                "Caller: unknown - 5705550123\nAbout: scrap")
            self.assertTrue(out.startswith(f"Caller: {name} - 5705550123"), out)

    def test_owner_shop_and_nonname_words_are_not_the_callers_name(self):
        from callscoot import agent
        for said in ("is this Anne's shop?", "this is Buccaneer Salvage right?",
                     "this is Monday, right?", "I'm out in Simpson", "this is ridiculous"):
            self.assertEqual(agent.caller_name(self.CFG, [("caller", said)]), "", said)
        # the bot's own words never name the caller
        self.assertEqual(agent.caller_name(self.CFG, [("agent", "This is Dave's line")]), "")

    def test_name_the_model_already_has_is_kept(self):
        out = self._summary([("caller", "name's Dave")], "Caller: David R - 5705550123")
        self.assertEqual(out, "Caller: David R - 5705550123")

    def test_call_alert_is_plain_text_and_keeps_special_characters(self):
        from unittest import mock
        from callscoot import notify
        summary = ("**Phone call summary:**\n\nCaller: D'Arcy *Jr* O_Brien - 5705550123  \n"
                   "About: **scrap** pickup\n- Wants: __two__ washers & a 3*4 sheet <metal>\n"
                   "## Action: call back")
        with mock.patch.object(notify, "telegram_send", return_value=(False, "dry")) as send:
            notify.send_call_alert({"persona": {"owner": "Anne"}}, "5705550123",
                                   [("caller", "hi")], False, "/t.md", log=lambda *_: None,
                                   summary=summary)
        text = send.call_args[0][1]
        self.assertNotIn("**", text)
        self.assertNotIn("__", text)
        self.assertNotIn("##", text)
        self.assertNotIn("summary:", text.lower())
        for keep in ("Caller: D'Arcy *Jr* O_Brien - 5705550123\n", "About: scrap pickup",
                     "Wants: two washers & a 3*4 sheet <metal>", "Action: call back"):
            self.assertIn(keep, text)

    def test_summary_prompt_asks_for_plain_text(self):
        from callscoot import agent
        self.assertIn("Plain text only: no markdown", agent.SUMMARY_PROMPT)


if __name__ == "__main__":
    unittest.main()
