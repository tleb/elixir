# Regression tests for the comment/string regex rework in
# elixir/lexers/shared.py, which replaced the overlapping `(.|\s)`
# bodies (exponential backtracking on unterminated comments/strings —
# the linux 2.1.25 drivers/net/cs89x0.c indexer wedge) with
# disjointified ones.
#
#  - every C/K/M-family file under t/tree and every C-family file of
#    the linux 2.1.25 checkout must scan identically (identifiers with
#    lines, error count) through the pre-fix and post-fix scanner
#    alternations — the fix must not change what the scanners report;
#  - the synthetic shape of the cs89x0.c trailer must stay fast.
#
# The pre-fix scanners are rebuilt by swapping the three old pattern
# strings back into the compiled scanner patterns, so the differential
# follows any future edit of the surrounding construction.
#
# This file is part of Elixir, a source code cross-referencer.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import re
import signal
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from elixir import lib
from elixir.lexers import fastscan, shared

TREE = REPO_ROOT / 't' / 'tree'
LINUX21 = Path('/var/tmp/linux21')  # the incident tree

# Per-file budget for the old scanner: the slowest non-hanging file of
# the linux21 corpus needs well under a second, so this only fires on
# the exponential cases.
OLD_SCAN_BUDGET = 10.0

# The pre-fix bodies, verbatim from the diff (git show HEAD^ if the fix
# is already committed): same languages, same lazy matches, but with the
# overlapping alternatives that made failure paths exponential.
_OLD_SUBSTITUTIONS = (
    (shared.slash_star_multline_comment, r'/\*(.|\s)*?\*/'),
    (shared.double_quote_string_with_escapes,
     r'"(\\\s*\n|[^\\"\n]|\\(.|\s))*?"'),
    (shared.single_quote_string_with_escapes,
     r"'(\\\s*\n|[^\\'\n]|\\(.|\s))*?'"),
)

_SCANS = {'C': fastscan.scan_c, 'K': fastscan.scan_k, 'M': fastscan.scan_m}


def _old_regex(regex):
    '''A compiled scanner regex with the pre-fix comment/string bodies
    swapped back in. The fix only rewrote the three pattern strings, so
    this reproduces the old scanner exactly.'''
    pattern = regex.pattern
    for new, old in _OLD_SUBSTITUTIONS:
        pattern = pattern.replace(new, old)
    return re.compile(pattern, re.MULTILINE)


@contextmanager
def _old_scanners():
    '''The fastscan module with its scanner regexes swapped for the
    pre-fix ones.'''
    names = ('_c_find', '_m_find', '_k_mask')
    saved = [(n, getattr(fastscan, n)) for n in names]
    try:
        for n, _rx in saved:
            setattr(fastscan, n, _old_regex(getattr(fastscan, n)))
        yield
    finally:
        for n, rx in saved:
            setattr(fastscan, n, rx)


class _OldScanTimeout(Exception):
    pass


@contextmanager
def _alarm(seconds):
    def fired(_signum, _frame):
        raise _OldScanTimeout()
    previous = signal.signal(signal.SIGALRM, fired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _family_files(root, families):
    '''Every file under root of one of the families, routed like the
    refs worker: gas (.s) keeps its lexer, never the fast scanner.'''
    for path in sorted(root.rglob('*')):
        if not path.is_file():
            continue
        family = lib.getFileFamily(path.name)
        if family not in families:
            continue
        if family == 'C' and path.suffix.lower() == '.s':
            continue
        yield family, path


def _decode(raw):
    '''The worker's blob decode: utf-8 with the escape fallback'''
    try:
        return raw.decode()
    except UnicodeDecodeError:
        return raw.decode('raw_unicode_escape')


def test_old_patterns_actually_differ():
    # The differential is only meaningful while these substitutions
    # change the scanner patterns (make_string embeds the shared double
    # quote string, so all three scanners move with the fix).
    for name in ('_c_find', '_m_find', '_k_mask'):
        regex = getattr(fastscan, name)
        assert _old_regex(regex).pattern != regex.pattern, name


@pytest.mark.skipif(not hasattr(signal, 'SIGALRM'), reason='needs SIGALRM')
def test_tree_old_vs_new_scanners():
    files = list(_family_files(TREE, ('C', 'K', 'M')))
    assert len(files) >= 10, files
    for family, path in files:
        code = _decode(path.read_bytes())
        new = _SCANS[family](code)
        with _old_scanners(), _alarm(OLD_SCAN_BUDGET):
            old = _SCANS[family](code)
        assert old[:2] == new[:2], (path, old[1], new[1])


@pytest.mark.skipif(not hasattr(signal, 'SIGALRM'), reason='needs SIGALRM')
@pytest.mark.skipif(not LINUX21.is_dir(), reason='no linux 2.1.25 checkout')
def test_linux21_old_vs_new_scanners():
    files = list(_family_files(LINUX21, ('C',)))
    assert len(files) > 1500, len(files)
    hangs = []
    for _family, path in files:
        code = _decode(path.read_bytes())
        new = fastscan.scan_c(code)
        try:
            with _old_scanners(), _alarm(OLD_SCAN_BUDGET):
                old = fastscan.scan_c(code)
        except _OldScanTimeout:
            hangs.append(path.relative_to(LINUX21))
            continue
        assert old[:2] == new[:2], (path, old[1], new[1])
    # The incident file is the only one whose old scan never finishes:
    # its ~30-space unterminated /* trailer is 2^30 decompositions of
    # the same span. Equivalence for it rests on the pattern-level
    # analysis (same language, same leftmost match), not on this run.
    assert hangs == [Path('drivers/net/cs89x0.c')], hangs


def test_pathological_comment_scan_stays_fast():
    '''The cs89x0.c trailer shape: rows of "* text" / "   or text"
    inside an unterminated /* comment. The old body needed >10 s at 6
    rows of it (verified in scratch with SIGALRM, exponential in the
    row count); the disjointified scanner must stay linear.'''
    fixture = '/*' + ' * text\n   or text' * 8
    start = time.perf_counter()
    idents, errors, _samples = fastscan.scan_c(fixture)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, elapsed
    # the comment match fails, so the body words surface as identifiers
    # and nothing is an error
    assert idents == {
        b'text': [1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8, 9],
        b'or': [2, 3, 4, 5, 6, 7, 8, 9],
    }
    assert errors == 0

    # with the terminator present the whole block is one comment: no
    # identifiers, no errors — the first `*/` still ends it
    terminated = '/*' + ' * text\n   or text' * 8 + '*/\n'
    assert fastscan.scan_c(terminated)[:2] == ({}, 0)
