# Fast scanners for the refs phase: single-pass (or few-pass) regex
# scans that emit exactly what the simple_lexer-based lexers emit for
# identifier tokens — same (identifier, line) pairs, same error count,
# same error samples — at several times the speed.
#
# Equivalence is by construction: every regex below is the production
# rule string itself (imported from lexers.py/shared.py), and the
# alternatives appear in the production rule order. simple_lexer picks,
# at each position, the first rule whose regex matches there; a
# combined alternation under re.finditer picks the first alternative
# that matches at the leftmost position — the same walk, without the
# per-rule Python dispatch (which the refs-phase profiling put at ~85%
# of worker time). Positions no alternative matches are exactly the
# positions where simple_lexer emits a one-character ERROR token, so
# error accounting falls out of the gaps between matches.
#
# Scope: only the refs worker path uses these (update.py). The web UI
# and the docs phase keep the lexers, which stay the reference.
#
# Families kept on the lexers: DTS and gas (.S) — their token rules are
# context-dependent enough that a regex port diverges (see the r3c
# analysis), and they are 4% of the bytes.
#
# This file is part of Elixir, a source code cross-referencer.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import re

from ..project_utils import get_lexer_class
from . import shared
from .lexers import CLexer, KconfigLexer, MakefileLexer

# Replaces masked-out characters in the Kconfig pre-passes. It matches
# no token rule of any family, so it can never take part in a match,
# and it is not whitespace, so it cannot satisfy a `\s` in later checks.
_FILLER = '\x00'

# FirstInLine equivalent as plain regex: the token preceded on its line
# by nothing but whitespace. `\n` is excluded from the class so the
# match cannot leave the line. Merged in front of the C preprocessor
# directives, where it also absorbs the leading whitespace that
# simple_lexer would first emit as a whitespace token: the union of the
# two spans is the same, and neither contains an identifier.
_first_in_line = r'[^\S\n]*'

# Whitespace for the C and makefile scanners: runs without newlines,
# plus one newline at a time. The split keeps every whitespace match
# inside one line, which the anchored preprocessor alternative needs
# (see _c_find), while still covering every whitespace character so
# that gaps hold only punctuation and ERROR tokens.
_ws_runs = r'[^\S\n]+|\n'

# C, in CLexer rule order. The directive alternative comes first as
# the merged FirstInLine rule: it also absorbs the leading whitespace
# simple_lexer would emit separately (same span union, no identifiers
# in either). The whitespace alternative is deliberately split into
# runs without newlines plus single newlines: a `\s+` that could
# cross a line start would swallow the indentation and demote an
# anchored `#directive` to `#` punctuation plus an identifier. With
# this form no match ever crosses a line start except comments,
# strings and directives, and no gap can hold a newline.
_c_find = re.compile(
    r'(?:^' + _first_in_line + shared.c_preproc_ignore
    + '|' + _ws_runs
    + '|' + shared.common_slash_comment
    + '|' + shared.common_string_and_char
    + '|' + shared.c_number + ')'
    r'|(?P<id>' + CLexer.c_identifier + ')',
    re.MULTILINE)

# Makefiles, in MakefileLexer rule order. The identifier rule
# precedes the minor one, so CONFIG_FOO is an identifier, not a
# minor token that swallows it. The whitespace alternative stays
# newline-safe like the C one (a makefile comment ends at the newline
# but consumes it, which is not a line-start crossing).
_m_find = re.compile(
    r'(?:' + _ws_runs
    + '|' + MakefileLexer.make_escape
    + '|' + MakefileLexer.make_comment
    + '|' + MakefileLexer.make_string + ')'
    r'|(?P<id>' + MakefileLexer.make_identifier + ')'
    r'|(?:' + MakefileLexer.make_minor_identifier
    + '|' + MakefileLexer.make_punctuation + ')',
    re.MULTILINE)

# A gap character is an ERROR token exactly when it starts no rule.
# Comments, strings, numbers and identifiers always match when their
# start character appears, so what is left is: not whitespace (the
# whitespace rule), not a punctuation character (the two punctuation
# rules) — everything else, unterminated quotes included, is an error.
# The classes below are the punctuation sets of the two lexers; the
# equivalence tests pin them to the lexer rules.
_c_gap_error = re.compile(r'[^\s!#%&`()*+,./:;<=>?\[\]\\^_{|}~$@-]')
_m_gap_error = re.compile(r'[^\s~\\`\[\](){}<>.,:;|%$^@&?!+*/=-]')

# Kconfig needs the help-block parser, which is stateful (minimum
# indentation), so it cannot fold into one alternation. Three passes:
#
# 1. mask what produces no identifiers and no errors: hash comments,
#    strings, and the catch-all runs of non-newline characters. The
#    catch-all fires at any character that starts no other rule and
#    swallows the rest of
#    the line — including uppercase words the identifier rule would
#    otherwise find. Quotes belong to it too: an unterminated quote
#    fails the string rule first (the string alternative runs before
#    the weird one, like the lexer's rule order) and then falls to the
#    catch-all, swallowing the line's tail. The character class is the
#    complement of every other Kconfig token start (whitespace, `#`,
#    the punctuation class, and the identifier/number/minor starts).
# 2. find help keywords and blank them plus their indented block,
#    exactly like KconfigLexer.parse_kconfig_help_text: keyword
#    detection and the block scan run on the original text (the lexer
#    scans ctx.code, not masked text), while candidates inside the
#    pass-1 spans are suppressed — those are inside comments, strings
#    or catch-all runs and never fire as keywords in the lexer.
# 3. one combined alternation in lexer rule order for the identifiers.
#
# Kconfig never produces ERROR tokens: `\s` covers newlines and the
# catch-all covers every other character, so errors are always 0.
_k_weird_run = r"[^\s#/|&!=$()+<>,_a-zA-Z0-9.\-][^\n]*"
_k_mask = re.compile(
    '(' + KconfigLexer.hash_comment
    + '|' + shared.common_string_and_char
    + '|' + _k_weird_run + ')',
    re.MULTILINE)

_k_help_kw = re.compile(r'(?m)^[^\S\n]*(?=-+help-+|help)')
_k_kw = re.compile(r'-+help-+|help') # rules[3]/[5] of KconfigLexer
_k_after_kw = re.compile(r'\s*?\n') # the lazy regex match_token uses
_k_line = re.compile(r'[^\n]*\n')
_k_lead_ws = re.compile(r'\s*')

_k_find = re.compile(
    r'(?:' + shared.whitespace
    + '|' + KconfigLexer.kconfig_punctuation + ')'
    r'|(?P<id>' + KconfigLexer.kconfig_identifier + ')'
    r'|(?:' + KconfigLexer.kconfig_number
    + '|' + KconfigLexer.kconfig_minor_identifier + ')',
    re.MULTILINE)


def _emit(idents, key, line):
    lines = idents.get(key)
    if lines is None:
        idents[key] = [line]
    else:
        lines.append(line)


def _sample_errors(gap_error, code, start, end, line, errors_out, samples,
                  max_samples):
    '''Account the ERROR tokens of one gap: every character starting no
    rule is one error token; sample the first ones with their line.'''
    gap = code[start:end]
    found = gap_error.findall(gap)
    if found:
        errors_out[0] += len(found)
    if samples is not None and len(samples) < max_samples:
        gline = line
        gprev = 0
        for m in gap_error.finditer(gap):
            gline += gap.count('\n', gprev, m.start())
            gprev = m.start()
            samples.append((m.group(), gline))
            if len(samples) >= max_samples:
                break


def _scan_c_or_m(code, regex, gap_error, prefix, config_only, max_samples):
    '''Shared body of the C and Makefile scanners: one finditer in rule
    order, identifiers from the named group, errors from the gaps.'''
    if not code:
        return {}, 0, []

    # simple_lexer appends a newline so line comments can always match
    if code[-1] != '\n':
        code += '\n'

    idents = {}
    errors = [0]
    samples = [] if max_samples > 0 else None
    line = 1
    pos = 0
    for m in regex.finditer(code):
        start = m.start()
        if start > pos:
            _sample_errors(gap_error, code, pos, start, line, errors,
                           samples, max_samples)
        if m['id'] is None:
            # whitespace is a skip alternative, so a gap holds no
            # newline: the line advances over the skipped match only
            line += code.count('\n', pos, m.end())
        else:
            token = prefix + m.group().encode()
            if not config_only or token.startswith(b'CONFIG_'):
                _emit(idents, token, line)
        pos = m.end()
    if pos < len(code):
        _sample_errors(gap_error, code, pos, len(code), line, errors,
                       samples, max_samples)

    return idents, errors[0], samples or []


def scan_c(code, max_samples=0):
    '''References of a C family file (.c/.h/...): CLexer's identifiers,
    error count and samples for refs, without token objects.'''
    return _scan_c_or_m(code, _c_find, _c_gap_error, b'', False, max_samples)


def scan_m(code, max_samples=0):
    '''References of a makefile: MakefileLexer's CONFIG_* identifiers,
    with the lexer's error accounting.'''
    return _scan_c_or_m(code, _m_find, _m_gap_error, b'', True, max_samples)


def scan_k(code, max_samples=0):
    '''References of a Kconfig file: KconfigLexer's CONFIG_-prefixed
    identifiers. The lexer cannot produce ERROR tokens, so neither does
    the scanner.'''
    if not code:
        return {}, 0, []

    if code[-1] != '\n':
        code += '\n'

    # Pass 1: mask comments, strings and catch-all runs. Newlines stay
    # so positions and lines survive every later pass.
    buf = list(code)
    spans = []
    for m in _k_mask.finditer(code):
        spans.append(m.span())
        for i in range(m.start(), m.end()):
            if code[i] != '\n':
                buf[i] = _FILLER

    # Pass 2: help keywords and their blocks, a port of
    # KconfigLexer.parse_kconfig_help_text on the original text.
    def blank(start, end):
        for i in range(start, end):
            if buf[i] != '\n':
                buf[i] = _FILLER

    si = 0
    nspans = len(spans)
    for m in _k_help_kw.finditer(code):
        kw = _k_kw.match(code, m.end())
        kw_start, kw_end = m.end(), kw.end()

        # A keyword inside a masked span is inside a comment, string or
        # catch-all run: the lexer never sees it as a keyword
        while si < nspans and spans[si][1] <= kw_start:
            si += 1
        if si < nspans and spans[si][0] < kw_end:
            continue

        blank(kw_start, kw_end)

        # `\s*?\n` right after the keyword, or this was not a help
        # block: the lexer yields only the keyword token
        after = _k_after_kw.match(code, kw_end)
        if after is None:
            continue

        # The block: lines until one is indented less than the first
        # non-empty one; empty lines never end it; a whitespace-only
        # line counts its newline as one character, like the lexer's
        # re.match(r'\s*', token) does
        pos = after.end()
        min_ws = None
        while pos < len(code):
            lm = _k_line.match(code, pos)
            if lm is None: # cannot happen: the code ends in \n
                break
            token = lm.group()
            if token == '\n':
                pos = lm.end()
                continue
            ws = _k_lead_ws.match(token).group()
            width = 8 * ws.count('\t') + (len(ws) - ws.count('\t'))
            if min_ws is None:
                min_ws = width
            elif width < min_ws:
                break
            pos = lm.end()
        blank(after.end(), pos)

    # Pass 3: identifiers, in lexer rule order
    masked = ''.join(buf)
    idents = {}
    line = 1
    pos = 0
    for m in _k_find.finditer(masked):
        line += masked.count('\n', pos, m.end())
        pos = m.end()
        if m['id'] is not None:
            _emit(idents, b'CONFIG_' + m.group().encode(), line)
    return idents, 0, []


_scanners = {
    CLexer: scan_c,
    KconfigLexer: scan_k,
    MakefileLexer: scan_m,
}

def get_scanner(path, project_name):
    '''The fast scanner for a path's refs-phase lexing, or None when
    the file keeps its simple_lexer path (DTS, gas). Resolves through
    the same per-project tables as project_utils.get_lexer, so any
    lexer the tables map a file to is the lexer the scanner must
    match.'''
    resolved = get_lexer_class(path, project_name)
    if type(resolved) == tuple:
        resolved = resolved[0]
    return _scanners.get(resolved)
