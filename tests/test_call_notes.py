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


if __name__ == "__main__":
    unittest.main()
