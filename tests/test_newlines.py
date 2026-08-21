"""Newlines arrive as CRLF and must not reach the corpus.

Multipart form encoding normalises newlines to CRLF on the wire, so every text
field the page posts — the problem, the scratchpad, the code — arrived with
carriage returns in it. They ended up in turns.jsonl, which is the fine-tuning
corpus, and in the problem text the renderer parses.
"""

import unittest

from app.main import Session, lf


class Lf(unittest.TestCase):
    def test_crlf(self):
        self.assertEqual(lf("a\r\nb"), "a\nb")

    def test_bare_cr(self):
        # Not what multipart sends, but a lone CR would be worse: it renders
        # as no break at all.
        self.assertEqual(lf("a\rb"), "a\nb")

    def test_leaves_lf_alone(self):
        self.assertEqual(lf("a\nb\n"), "a\nb\n")

    def test_none_passes_through(self):
        self.assertIsNone(lf(None))

    def test_empty(self):
        self.assertEqual(lf(""), "")


class SessionState(unittest.TestCase):
    def setUp(self):
        self.session = Session(id="test0000", problem="p", minutes=30)

    def tearDown(self):
        for f in self.session.dir.glob("*"):
            f.unlink()
        self.session.dir.rmdir()

    def test_code_is_stored_as_lf(self):
        self.session.note_code("def f():\r\n    return 1\r\n")
        self.assertEqual(self.session.code, "def f():\n    return 1\n")

    def test_scratchpad_is_stored_as_lf(self):
        self.session.note_scratchpad("Inputs: a\r\nOutputs: b")
        self.assertEqual(self.session.scratchpad, "Inputs: a\nOutputs: b")

    def test_the_same_code_twice_is_not_logged_twice(self):
        # The page autosaves every few seconds; normalising must not make an
        # unchanged buffer look changed.
        self.session.note_code("x = 1\r\n")
        before = (self.session.dir / "turns.jsonl").read_text().count("\n")
        self.session.note_code("x = 1\r\n")
        after = (self.session.dir / "turns.jsonl").read_text().count("\n")
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
