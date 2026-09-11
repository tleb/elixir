# Port of t/300-doc-comments.t: doc-comment extraction against the
# files in t/tree, indexed by update.py. The perl suite asserted
# through the utils/query.py CLI; these assert the same facts through
# elixir.query.Query directly (test_100's precedent), except the two
# #186/#188 cases, which ran the perl directly and now call
# elixir.parse.parse_doc_comments (the perl had to exit 0 without
# warnings; warnings were fatalized inside it, so an empty result
# without an exception is the port).
#
# This file is part of Elixir, a source code cross-referencer.
#
# Copyright (c) 2020 Christopher White.
# Copyright (c) 2020 D3Engineering, LLC.
#
# Elixir is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# Elixir is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public
# License along with Elixir.  If not, see <http://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from pathlib import Path

import pytest

from conftest import TAG

from elixir import parse

TREE = Path(__file__).resolve().parent / 'tree'


def docs(query, ident):
    """The "Documented in" section: (path, line) per doc comment"""
    _, _, symbol_docs, _ = query.search_ident(TAG, ident, 'C')
    return [(s.path, s.line) for s in symbol_docs]


# Spot-check some identifiers


def test_doc_comment_query_nonexistent(query):
    assert docs(query, 'SOME_NONEXISTENT_IDENTIFIER_XYZZY_PLUGH') == []


def test_not_documented(query):
    # in drivers/i2c/i2c-core-acpi.c
    assert docs(query, 'gsb_buffer') == []


def test_documented_function(query):
    assert docs(query, 'i2c_acpi_get_i2c_resource') == \
        [('drivers/i2c/i2c-core-acpi.c', '45')]


def test_documented_function_102(query):
    # #102: doc comment associated despite intervening plain comments
    assert docs(query, 'documented_function_XYZZY') == [('issue102.c', '6')]


def test_documented_function_cbus_driver(query):
    # kernel-doc in the real v7.3-rc2 driver (provenance in README.adoc)
    assert docs(query, 'cbus_send_bit') == \
        [('drivers/i2c/busses/i2c-cbus-gpio.c', '45')]


# Non-functions


def test_enum_documented(query):
    assert docs(query, 'memblock_flags') == [('include/linux/memblock.h', '28')]


def test_enum_not_documented(query):
    # uapi/linux/rseq.h:16
    assert docs(query, 'rseq_cpu_id_state') == []


def test_struct_documented(query):
    assert docs(query, 'memblock_region') == \
        [('include/linux/memblock.h', '42')]


def test_struct_not_documented(query):
    # eventpoll.h:77
    assert docs(query, 'epoll_event') == []


def test_macro_documented(query):
    # Multiline macro: the doc search starts back at the #define line
    assert docs(query, 'for_each_mem_range') == \
        [('include/linux/memblock.h', '148')]


def test_macro_not_documented(query):
    # memblock.h:343
    assert docs(query, 'MEMBLOCK_LOW_LIMIT') == []


# Specific cases from #134: nonstandard doc comments


def test_nonstandard_doc_comment_134(query):
    # Like regmap_update_bits_base()
    assert docs(query, 'issue134_function1') == [('issue134.c', '9')]


def test_nonstandard_doc_comment2_134(query):
    # Like wait_for_completion()
    assert docs(query, 'issue134_function2') == [('issue134.c', '25')]


def test_prototype_documented_134(query):
    # Like v4l2_fwnode_endpoint_parse()
    assert docs(query, 'issue134_function3') == [('issue134.c', '38')]


# #186: same identifier as function and as macro. The perl's
# warning-fatalization could have died here; it did not, and neither
# does the port.


def test_no_warnings_186():
    assert parse.parse_doc_comments((TREE / 'issue186.c').read_bytes()) == []


def test_documented_function_186(query):
    assert docs(query, 'i186c_fn1') == [('issue186-counterexamples.c', '5')]


def test_documented_macro_186(query):
    assert docs(query, 'i186c_fn2') == [('issue186-counterexamples.c', '20')]


# #188: indented #define


def test_no_warnings_188():
    assert parse.parse_doc_comments((TREE / 'issue188.c').read_bytes()) == []


# #192: return type on the line before the function name


def test_type_on_preceding_line_192(query):
    assert docs(query, 'issue192a') == [('issue192.c', '5')]


def test_uppercase_type_on_preceding_line_192(query):
    assert docs(query, 'issue192b') == [('issue192.c', '15')]
