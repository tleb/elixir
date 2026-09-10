# Unit tests for the chunked parse layer: one ctags per (chunk,
# family) with per-line attribution by the -x input column, the /**/
# gate, and temp names that stay unique and parseable however odd the
# original basenames are. Byte-identical single-vs-chunk equivalence
# over every t/tree blob sits on top of the corpus A/B runs.
#
# This file is part of Elixir, a source code cross-referencer.
#
# Elixir is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of
# the License, or (at your option) any later version.
#
# Elixir is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public
# License along with Elixir (see the file COPYING); if not, see
# <http://www.gnu.org/licenses/>.

from pathlib import Path

import pytest

from elixir import parse
from elixir.lib import getFileFamily

TREE = Path(__file__).resolve().parent / 'tree'


def tree_files():
    '''Every t/tree file as (bytes blob, bytes basename, family)'''
    out = []
    for p in sorted(TREE.rglob('*')):
        if p.is_file():
            name = p.name.encode()
            out.append((p.read_bytes(), name, getFileFamily(p.name)))
    return out


def test_defs_chunk_matches_single():
    items = tree_files()
    chunked = parse.parse_defs_chunk(
        [(blob, name, family) for blob, name, family in items])
    single = [parse.parse_defs(blob, name, family)
              for blob, name, family in items]
    assert chunked == single


def test_doc_comments_chunk_matches_single():
    blobs = [blob for blob, _, _ in tree_files()]
    chunked = parse.parse_doc_comments_chunk(blobs)
    single = [parse.parse_doc_comments(blob) for blob in blobs]
    assert chunked == single


def test_defs_chunk_same_basenames():
    '''Two blobs under one basename must not share their tags'''
    a = b'int shared_name(void) { return 1; }\nint only_a;\n'
    b = b'int shared_name(void) { return 2; }\nint only_b;\n'
    out = parse.parse_defs_chunk([(a, b'a.c', 'C'), (b, b'a.c', 'C')])
    assert out[0] == parse.parse_defs(a, b'a.c', 'C')
    assert out[1] == parse.parse_defs(b, b'a.c', 'C')
    assert b'only_a variable 2' in out[0]
    assert b'only_b variable 2' in out[1]


def test_defs_chunk_hostile_filenames():
    '''Spaces, non-ASCII, separators and NAME_MAX overflow in the
    basename: the temp name keeps only the extension and the tags
    still attribute'''
    blob = b'int gated(void) { return 0; }\n'
    names = [b'weird name.c', b'na\xefve.c', b'a|b\tc.c',
             b'x' * 300 + b'.c', b'.c']
    out = parse.parse_defs_chunk([(blob, n, 'C') for n in names])
    for lines in out:
        assert lines == [b'gated function 1']


def test_defs_chunk_dts_and_kconfig():
    dts = b'/ {\n\tlbl: node {\n\t};\n};\n'
    kconfig = b'config FOO\n\tbool\n\nconfig BAR\n\tbool\n'
    out = parse.parse_defs_chunk([(dts, b'board.dts', 'D'),
                                  (kconfig, b'Kconfig', 'K'),
                                  (b'readme', b'README', None)])
    assert out[0] == [b'lbl label 2']
    assert out[1] == [b'CONFIG_BAR config 4', b'CONFIG_FOO config 1']
    assert out[2] == []


def test_doc_comments_gate(monkeypatch):
    '''A blob without /** never reaches ctags'''
    def boom(flags, entries):
        raise AssertionError('ctags called for a gated blob')
    monkeypatch.setattr(parse, '_chunk_ctags', boom)
    assert parse.parse_doc_comments_chunk(
        [b'int f(void);\n', b'/* plain comment */\n']) == [[], []]


def test_doc_comments_gate_keeps_ordinary_comments():
    '''/** inside a string is not an opener, but the gate only feeds
    the scan; the scan itself decides (kept byte-identical)'''
    blob = b'char *s = "/**";\nint f(void) { return 0; }\n'
    assert parse.parse_doc_comments(blob) == []
