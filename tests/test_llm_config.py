"""A side file can add an OpenAI-compatible model without editing Python."""
import os
import tempfile
import unittest

from callscoot.config import add_llm_provider, load


class LlmConfigTests(unittest.TestCase):
    def test_side_file_is_tried_first(self):
        d = tempfile.mkdtemp()
        main = os.path.join(d, "callscoot.toml")
        with open(main, "w", encoding="utf-8") as fh:
            fh.write(
                "[llm]\n"
                "[[llm.providers]]\n"
                'name = "builtin"\n'
                'base_url = "http://builtin/v1"\n'
                'model = "a"\n'
                'api_key_env = "NO"\n'
            )
        with open(os.path.join(d, "callscoot.llm.toml"), "w", encoding="utf-8") as fh:
            fh.write(
                "[[providers]]\n"
                'name = "mine"\n'
                'base_url = "http://mine/v1"\n'
                'model = "b"\n'
                'api_key_env = "MINE"\n'
            )
        names = [p["name"] for p in load(main)["llm"]["providers"]]
        self.assertEqual(names, ["mine", "builtin"])

    def test_llm_add_writes_the_side_file_and_not_the_key_in_toml(self):
        d = tempfile.mkdtemp()
        main = os.path.join(d, "callscoot.toml")
        with open(main, "w", encoding="utf-8") as fh:
            fh.write("[phone]\nanswer_unknown = true\n")
        side = add_llm_provider(
            main, "mine", "https://api.example.com/v1", "my-model",
            "MY_LLM_KEY", "secret-value",
        )
        with open(side, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn('name = "mine"', text)
        self.assertNotIn("secret-value", text)
        with open(os.path.join(d, ".env"), encoding="utf-8") as fh:
            env = fh.read()
        self.assertIn("MY_LLM_KEY=secret-value", env)
        self.assertEqual(load(main)["llm"]["providers"][0]["model"], "my-model")


if __name__ == "__main__":
    unittest.main()
