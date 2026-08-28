# Tests for the Python port of the tokenizer (elixir/lib.py tokenize()).
# It replaced a perl one-liner in script.sh (tokenize-file); these
# tests pin down the properties its callers rely on:
#   - tokens alternate separator, word, separator, word, ...
#   - separators contain no identifier characters (for the requested
#     family), words only those
#   - round trip: separators and words concatenated give the input back
#
# This file is part of Elixir, a source code cross-referencer.
#
# Elixir is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

import pathlib
import sys
import unittest

sys.path.append(str(pathlib.Path(__file__).resolve().parents[1]))

from elixir.lib import tokenize


def tokenize_pipeline(data, family):
    # tokenizeFile() feeds tokenize() with newlines turned into \001,
    # like the perl tokenizer did
    return list(tokenize(data.replace(b'\n', b'\001'), family))


class TokenizeTest(unittest.TestCase):
    def assert_alternates(self, data, family):
        tokens = tokenize_pipeline(data, family)
        self.assertEqual(b''.join(tokens), data.replace(b'\n', b'\001'))
        # a trailing empty word is produced when the data ends with a
        # separator; consumers of the stream ignore empty words
        if tokens and tokens[-1] == b'':
            tokens = tokens[:-1]
        word = rb'^[\w-]+$' if family == 'D' else rb'^\w+$'
        for i, tok in enumerate(tokens):
            if i % 2:  # words
                self.assertRegex(tok, word, (i, tok))
            elif tok:  # separators
                self.assertNotRegex(tok, word, (i, tok))
        return tokens

    def test_roundtrip_and_alternation(self):
        cases = [
            b'int foo;\n',
            b'#include <stdio.h>\nint main(void) { return 0; }\n',
            b'/* c1 */ /* c2 */ x\n',
            b'/* multi\nline */ x\n',
            b'// line comment\ncode();\n',
            b'char *s = "string with spaces";\n',
            b"char c = 'x';\n",
            b'#  include  <sys/types.h>\n',
            b'#include<asm/page.h>\n',
            b'a\rb\nc\n',
            b'\n\n\n\n',
            b'   leading whitespace\n',
            b'trailing word',
            b'"unterminated string\nrest\n',
            b"it's quoted\n",
            b'#define X "s" \\\n\tY\n',
        ]
        for data in cases:
            self.assert_alternates(data, 'C')

    def test_leading_word_run_glued_to_first_separator(self):
        # A file starting with a word has no separator in front of it;
        # the token stream passes it through verbatim, glued to the
        # first separator (same as the perl tokenizer did).
        tokens = self.assert_alternates(b'word1 word2\n', 'C')
        self.assertEqual(tokens[0], b'word1 ')
        self.assertEqual(tokens[1], b'word2')

    def test_family_d_dash_is_word_char(self):
        # In devicetree files, '-' is part of identifiers
        tokens = tokenize_pipeline(b' a-b c\n', 'D')
        self.assertEqual(tokens[0], b' ')
        self.assertEqual(tokens[1], b'a-b')
        self.assertEqual(tokens[3], b'c')

        # In other families it is a separator
        tokens = tokenize_pipeline(b' a-b c\n', 'C')
        self.assertEqual(tokens[1], b'a')
        self.assertEqual(tokens[3], b'b')

    def test_comments_and_includes_are_separators(self):
        tokens = tokenize_pipeline(b' x /* one two */ y\n', 'C')
        # the words inside the comment must not become tokens
        words = tokens[1::2]
        self.assertNotIn(b'one', words)
        self.assertNotIn(b'two', words)
        self.assertIn(b'x', words)
        self.assertIn(b'y', words)

        tokens = tokenize_pipeline(b'a #include <stdio.h>\nb\n', 'C')
        self.assertNotIn(b'stdio', tokens[1::2])
        self.assertIn(b'#include <stdio.h>', b''.join(tokens[0::2]))

    def test_quotes_keep_words_out(self):
        tokens = tokenize_pipeline(b' a = "two words";\n', 'C')
        self.assertNotIn(b'two', tokens[1::2])
        self.assertIn(b'"two words"', b''.join(tokens[0::2]))
    def test_tree_files_roundtrip(self):
        tree = pathlib.Path(__file__).parent / 'tree'
        for path in tree.rglob('*'):
            if not path.is_file():
                continue
            data = path.read_bytes()
            for family in ('C', 'D'):
                tokens = tokenize_pipeline(data, family)
                self.assertEqual(b''.join(tokens),
                                 data.replace(b'\n', b'\001'), (family, str(path)))


if __name__ == '__main__':
    unittest.main()
