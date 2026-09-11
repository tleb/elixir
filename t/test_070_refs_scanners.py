# Equivalence tests for the refs-phase fast scanners
# (elixir/lexers/fastscan.py) against the simple_lexer-based lexers.
#
# Two layers:
#  - synthetic cases pinning the behaviors that are easy to get
#    subtly wrong (preprocessor directives, number-before-identifier,
#    Kconfig help blocks and catch-all runs, makefile rule order);
#  - real corpora (read-only git access): blobs sampled across size
#    deciles, per family, from the linux v7.1 timing clone and the
#    musl clone, where the scanner's output must equal the lexer's
#    exactly — identifiers with their lines, error count, samples.
#
# This file is part of Elixir, a source code cross-referencer.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from elixir import lib
from elixir.lexers import TokenType
from elixir.lexers.fastscan import get_scanner, scan_c, scan_k, scan_m
from elixir.project_utils import get_lexer

LINUX_TIMING_REPO = Path('/home/tleb/prog/public/elixir-data-timing/linux/repo')
MUSL_REPO = Path('/home/tleb/prog/public/elixir-data/musl/repo')

MAX_SAMPLES = 10


def lex_references(path, project, raw):
    '''The refs worker's inner loop, verbatim: what _refs_lex_chunk
    computes for one blob through the simple_lexer path.'''
    family = lib.getFileFamily(os.path.basename(path))
    lexer = get_lexer(path, project)
    try:
        code = raw.decode()
    except UnicodeDecodeError:
        code = raw.decode('raw_unicode_escape')

    prefix = b''
    if family == 'K':
        prefix = b'CONFIG_'

    idents = {}
    errors = 0
    samples = []
    for token_type, token, _, line in lexer(code).lex():
        if token_type == TokenType.ERROR:
            errors += 1
            if len(samples) < MAX_SAMPLES:
                samples.append((token, path, line))
            continue

        token = prefix + token.encode()

        if token_type != TokenType.IDENTIFIER:
            continue

        config_or_not_makefile = family != 'M' or token.startswith(b'CONFIG_')
        if config_or_not_makefile:
            if token in idents:
                idents[token].append(line)
            else:
                idents[token] = [line]
    return family, idents, errors, samples


def scan_references(path, project, raw):
    '''The same blob through the fast-scanner branch'''
    family = lib.getFileFamily(os.path.basename(path))
    scanner = get_scanner(path, project)
    assert scanner is not None, path
    try:
        code = raw.decode()
    except UnicodeDecodeError:
        code = raw.decode('raw_unicode_escape')
    idents, errors, tokens = scanner(code, MAX_SAMPLES)
    return family, idents, errors, [(t, path, ln) for t, ln in tokens]


def assert_equivalent(path, project, raw):
    expect = lex_references(path, project, raw)
    got = scan_references(path, project, raw)
    assert got == expect, _diff(expect, got, path)


def _diff(expect, got, path):
    lines = ['divergence in %s' % path]
    if expect[0] != got[0]:
        lines.append('family %r vs %r' % (expect[0], got[0]))
    if expect[1] != got[1]:
        ek, gk = set(expect[1]), set(got[1])
        lines.append('identifiers only in lexer: %r'
                     % sorted(ek - gk)[:5])
        lines.append('identifiers only in scanner: %r'
                     % sorted(gk - ek)[:5])
        for k in sorted(ek & gk):
            if expect[1][k] != got[1][k]:
                lines.append('lines for %r: lexer %r vs scanner %r'
                             % (k, expect[1][k], got[1][k]))
                break
    if expect[2] != got[2]:
        lines.append('errors %d vs %d' % (expect[2], got[2]))
    if expect[3] != got[3]:
        lines.append('samples %r vs %r' % (expect[3], got[3]))
    return '\n'.join(lines)


# ---- Synthetic cases ----

def test_c_directives_are_not_identifiers():
    # the directive rule eats only #\s*[a-z]+, so the guarded macro
    # name _H is still an identifier; #IFNDEF is uppercase: the
    # directive rule fails, '#' is punctuation and IFNDEF is indexed
    code = '#ifndef _H\n# include "x.h"\n# 5\n#IFNDEF X\n'
    assert scan_c(code)[0] == {b'_H': [1], b'IFNDEF': [4], b'X': [4]}

def test_c_number_before_identifier():
    # 8xx -> number 8 + identifier xx; 0x1f is all number
    code = 'a = 8xx; b = 0x1f;\n'
    assert scan_c(code)[0] == {b'a': [1], b'xx': [1], b'b': [1]}

def test_c_identifier_with_digits_wins():
    # the identifier rule swallows the whole macro name, digit runs
    # included: CEX4 never splits into CEX + a number
    code = '#ifndef _ZCRYPT_CEX4_H_\nint i;\n'
    assert scan_c(code)[0] == {b'_ZCRYPT_CEX4_H_': [1], b'int': [2], b'i': [2]}

def test_c_include_argument_not_identifier():
    code = '#include <linux/foo.h>\nint main(void) {}\n'
    assert scan_c(code)[0] == {b'int': [2], b'main': [2], b'void': [2]}

def test_c_unterminated_quote_is_an_error_with_sample():
    # the quote starts no string and no other rule: one ERROR token,
    # then lexing continues (a is an identifier)
    code = "char c = 'a;\nint i;\n"
    idents, errors, samples = scan_c(code, MAX_SAMPLES)
    assert idents == {b'char': [1], b'c': [1], b'a': [1], b'int': [2], b'i': [2]}
    assert errors == 1
    assert samples == [("'", 1)]

def test_c_no_trailing_newline():
    code = 'int i'
    assert scan_c(code)[0] == {b'int': [1], b'i': [1]}

def test_c_empty():
    assert scan_c('') == ({}, 0, [])

def test_k_help_block_is_skipped():
    code = ('config FOO\n\tbool "foo"\n\thelp\n'
            '\t  This text mentions BAR which must not be indexed.\n'
            '\t  More BAZ text.\n'
            'config BAZ\n\tbool "baz"\n')
    assert scan_k(code)[0] == {b'CONFIG_FOO': [1], b'CONFIG_BAZ': [6]}

def test_k_help_dashes_keyword():
    code = ('config FOO\n\tbool\n\t---help---\n'
            '\t  BAR must not be indexed.\nconfig BAR\n\tbool\n')
    assert scan_k(code)[0] == {b'CONFIG_FOO': [1], b'CONFIG_BAR': [5]}

def test_k_help_not_a_block_when_junk_follows():
    # help followed by non-whitespace: no block, the rest of the line
    # lexes on and X is an identifier
    code = 'config FOO\n\thelp X\nconfig BAR\n\tbool\n'
    assert scan_k(code)[0] == {b'CONFIG_FOO': [1], b'CONFIG_X': [2],
                               b'CONFIG_BAR': [3]}

def test_k_help_block_at_zero_indent_swallows_the_rest():
    # the lexer's minimum-indent logic keeps width 0, so the block
    # runs to the end of the file: BAR is never indexed
    code = 'config FOO\n\thelp\nX\nconfig BAR\n'
    assert scan_k(code)[0] == {b'CONFIG_FOO': [1]}

def test_k_catchall_swallows_to_end_of_line():
    # ':' starts no Kconfig rule: the catch-all [^\n]+ eats the line,
    # QUX included
    code = 'config FOO\n\t depends : QUX\nconfig BAR\n'
    assert scan_k(code)[0] == {b'CONFIG_FOO': [1], b'CONFIG_BAR': [3]}

def test_k_underscore_is_punctuation_not_identifier_start():
    # kconfig_punctuation runs before the identifier rule, so _FOO
    # splits into punctuation '_' + identifier FOO
    code = 'config X\n\tdef_tristate _FOO\n'
    assert scan_k(code)[0] == {b'CONFIG_X': [1], b'CONFIG_FOO': [2]}

def test_k_minor_swallows_within_token():
    # a/b/FOO is one minor identifier: FOO is not emitted; the bare
    # /GO case has the slash as punctuation, so GO is emitted
    code = 'config X\n\tdefault a/b/FOO /GO\n'
    assert scan_k(code)[0] == {b'CONFIG_X': [1], b'CONFIG_GO': [2]}

def test_k_digit_led_identifier():
    code = 'config X\n\tdefault 5FOO 55\n'
    assert scan_k(code)[0] == {b'CONFIG_X': [1], b'CONFIG_5FOO': [2]}

def test_k_hash_comment_and_strings():
    code = ('# FOO not indexed\n'
            'config FOO\n\tprompt "BAR not indexed"\n')
    assert scan_k(code)[0] == {b'CONFIG_FOO': [2]}

def test_k_never_errors():
    # quotes that start no string fall to the catch-all, not an error
    code = "config X 'oops\n\thelp\n\t  text\n"
    assert scan_k(code)[1:] == (0, [])

def test_m_only_config_identifiers():
    code = 'obj-$(CONFIG_FOO) += foo.o\nCcFLAGS-y = -Werror\n'
    assert scan_m(code)[0] == {b'CONFIG_FOO': [1]}

def test_m_comment_and_escape():
    # an escaped hash is punctuation, so the line after it still
    # lexes normally: CONFIG_ALSO_NOT is indexed
    code = '# CONFIG_HIDDEN not indexed\nx = \\# CONFIG_ALSO_NOT\n'
    assert scan_m(code)[0] == {b'CONFIG_ALSO_NOT': [2]}

def test_m_single_quote_alone_is_a_string():
    # the lexer's single-quote rule is one-or-more quotes, lazy: one
    # quote is a string, not an error
    code = "X = '\nCONFIG_FOO = y\n"
    idents, errors, samples = scan_m(code, MAX_SAMPLES)
    assert idents == {b'CONFIG_FOO': [2]}
    assert (errors, samples) == (0, [])

def test_m_unterminated_double_quote_is_an_error():
    idents, errors, samples = scan_m(b'X = "abc\nCONFIG_FOO = y\n'.decode(),
                                     MAX_SAMPLES)
    assert idents == {b'CONFIG_FOO': [2]}
    assert errors == 1
    assert samples == [('"', 1)]

def test_m_non_ascii_is_an_error():
    idents, errors, samples = scan_m('a = caf\u00e9\n', MAX_SAMPLES)
    assert idents == {}
    assert errors == 1
    assert samples == [('\u00e9', 1)]

def test_m_rule_order_identifier_before_minor():
    # CONFIG_FOO matches both make_identifier and make_minor; the
    # identifier rule comes first, so it is emitted even lowercaseless
    code = 'CONFIG_FOO := y\n'
    assert scan_m(code)[0] == {b'CONFIG_FOO': [1]}


# ---- Dispatch ----

@pytest.mark.parametrize('path,expected', [
    ('drivers/base/node.c', scan_c),
    ('include/linux/foo.h', scan_c),
    ('src/ldso/x86_64/dlsym.s', None),        # gas keeps the lexer
    ('arch/arm/kernel/head.S', None),
    ('dts/foo.dts', None), ('dts/foo.dtsi', None),
    ('drivers/net/Kconfig', scan_k),
    ('drivers/net/Kconfig.netdev', scan_k),
    ('Makefile', scan_m), ('scripts/Makefile.lib', scan_m),
    ('arch/arm/boot/compressed/Makefile', scan_m),
    ('firmware/Makefile', scan_m), ('foo.mk', scan_m),
    ('Documentation/kconfig.rst', None),       # no family at all
])
def test_get_scanner_dispatch(path, expected):
    for project in ('linux', 'u-boot', 'musl'):
        assert get_scanner(path, project) is expected, project

def test_get_scanner_kconfig_mk_resolves_like_the_tables():
    # kconfig.* matches before .*\.mk$ in the tables, so a kconfig.mk
    # gets the Kconfig scanner even though its family is K and its
    # extension is .mk
    assert lib.getFileFamily('kconfig.mk') == 'K'
    assert get_scanner('kconfig.mk', 'linux') is scan_k


# ---- Real corpora ----

def _git(repo, *args, stdin=None):
    return subprocess.run(['git', '-C', str(repo), '-c', 'core.quotePath=false',
                           *args],
                          input=stdin, stdout=subprocess.PIPE,
                          check=True).stdout

def _ls_blobs(repo, tag):
    '''(size, path, hash) of a tag's blobs, in ls-tree order'''
    out = _git(repo, 'ls-tree', '-r', '-l', tag)
    for line in out.decode().splitlines():
        meta, path = line.split('\t', 1)
        mode, typ, h, size = meta.split()
        if typ == 'blob':
            yield int(size), path, h

def _cat_blobs(repo, hashes):
    '''hash -> blob bytes through one batch pipe'''
    blobs = {}
    revs = '\n'.join(hashes) + '\n'
    buf = _git(repo, 'cat-file', '--batch', stdin=revs.encode())
    pos = 0
    while pos < len(buf):
        nl = buf.index(b'\n', pos)
        header = buf[pos:nl]  # <hash> blob <size>
        h = header.split()[0].decode()
        size = int(header.rsplit(b' ', 1)[1])
        start = nl + 1
        blobs[h] = buf[start:start + size]
        pos = start + size + 1  # skip the blob and its trailing newline
    return blobs

def _sample_deciles(entries, per_decile):
    '''A stratified sample over size deciles of sorted entries'''
    entries = sorted(entries)
    n = len(entries)
    out = []
    for d in range(10):
        bucket = entries[d * n // 10:(d + 1) * n // 10 or None]
        if not bucket:
            continue
        step = -(-len(bucket) // per_decile)
        out.extend(bucket[::step][:per_decile])
    return out

def _corpus_blobs(repo, tag, families, per_decile, seen):
    by_family = {f: [] for f in families}
    for size, path, h in _ls_blobs(repo, tag):
        if h in seen:
            continue
        seen.add(h)
        family = lib.getFileFamily(os.path.basename(path))
        if family == 'C' and path.lower().endswith('.s'):
            continue # gas keeps the lexer path
        if family in by_family:
            by_family[family].append((size, path, h))
    return {f: _sample_deciles(v, per_decile) for f, v in by_family.items()}

@pytest.mark.skipif(not LINUX_TIMING_REPO.exists(), reason='no linux timing clone')
def test_linux_v71_corpus_equivalence():
    blobs = _corpus_blobs(LINUX_TIMING_REPO, 'v7.1', ('C', 'K', 'M'),
                          12, set())
    raw = _cat_blobs(LINUX_TIMING_REPO, [h for v in blobs.values()
                                         for _, _, h in v])
    checked = 0
    for family in ('C', 'K', 'M'):
        for _, path, h in blobs[family]:
            assert_equivalent(path, 'linux', raw[h])
            checked += 1
    # a real sample across all three families
    assert checked >= 100, checked

@pytest.mark.skipif(not MUSL_REPO.exists(), reason='no musl clone')
def test_musl_corpus_equivalence():
    seen = set()
    blobs = {}
    for tag in ('v1.2.6', 'v0.9.15', 'v0.5.0'):
        for family, entries in _corpus_blobs(MUSL_REPO, tag, ('C', 'M'),
                                             8, seen).items():
            blobs.setdefault(family, []).extend(entries)
    raw = _cat_blobs(MUSL_REPO, [h for v in blobs.values() for _, _, h in v])
    checked = 0
    for family in ('C', 'M'):
        for _, path, h in blobs[family]:
            assert_equivalent(path, 'musl', raw[h])
            checked += 1
    assert checked >= 100, checked
