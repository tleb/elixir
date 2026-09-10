#!/usr/bin/env python3

#  This file is part of Elixir, a source code cross-referencer.
#
#  Copyright (C) 2017--2020 Mikaël Bouillot <mikael.bouillot@bootlin.com>
#                           Maxime Chretien <maxime.chretien@bootlin.com>
#                           and contributors
#
#  Elixir is free software: you can redistribute it and/or modify
#  it under the terms of the GNU Affero General Public License as
#  published by the Free Software Foundation, either version 3 of the
#  License, or (at your option) any later version.
#
#  Elixir is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU Affero General Public License for more details.
#
#  You should have received a copy of the GNU Affero General Public
#  License along with Elixir (see the file COPYING); if not, see
#  <http://www.gnu.org/licenses/>.

'''Port of script.sh's parse-defs: the definitions of one blob, as
the list of b"ident type line" lines the shell pipeline printed,
byte for byte and in the same order (per family: the ctags lines
first, then the ENTRY scan, then the SYSCALL_DEFINE scan).

ctags stays a subprocess, one invocation per (chunk, family) instead
of one per blob: with --_xformat the -x output carries the input
file, so one run can cover a whole chunk, and the per-file output
comes out in the same order a per-file run printed (name-sorted,
ties kept in ctags' own order). ctags needs real files on disk and
keys its parser off the filename, so each blob is written to a fresh
temp directory under a UNIQUE name that keeps the original
EXTENSION (the C family has no --language-force; its language comes
from the name) and reduces everything else to plain ASCII, so ctags'
%{input} always attributes a line back to its blob. This is why
script.sh copied to $tmp/$opt2, just shared.

A nonzero ctags exit is tolerated and its stdout used anyway: the
shell pipelines' exit status came from the last stage, so a failing
ctags still delivered whatever it had printed (u-ctags even exits 0
after only warning that the input file cannot be opened).'''

import os
import re
import shutil
import subprocess
import tempfile

def _shell_lines(data):
    '''data as the line list a shell tool saw: split on newlines, a
    partial final line kept, a trailing empty line dropped'''
    lines = data.split(b'\n')
    if not data or data.endswith(b'\n'):
        del lines[-1]
    return lines

# One ctags -x run for a whole chunk. The plain -x line
# "name kind line input ..." carries the input file as its fourth
# column, printed exactly as passed, so one run can cover a whole
# chunk and every line still attributes back to its blob (--_xformat
# and --output-format=json are out: both sort equal names by the line
# number as a STRING, where the -x path keeps them in parser-creation
# order; json under -x even drops the line number). Every temp name is
# reduced to plain ASCII without whitespace, so no column can ever
# hold a separator or a shifted field can look like a path.
_TEMP_SAFE = re.compile(rb'[^A-Za-z0-9._-]')

def _temp_name(i, filename):
    '''Unique temp basename for blob #i of the chunk: the original
    extension kept (ctags keys the C family's language off it), the
    rest reduced to safe ASCII'''
    stem, dot, suffix = filename.rpartition(b'.')
    if not dot:
        stem, suffix = filename, b''
    stem = _TEMP_SAFE.sub(b'_', stem)[:200]
    if dot:
        dot += _TEMP_SAFE.sub(b'_', suffix)[:48]
    return b'%d-%s%s' % (i, stem, dot)

def _chunk_ctags(flags, entries):
    '''One ctags invocation for a chunk of (key, blob, filename)
    entries: every blob written under its temp name, ctags run once,
    the output attributed by the input column. Returns {key: [-x
    lines]}, in ctags' output order; a blob ctags said nothing about
    is simply missing from the map, and a line whose fourth column is
    not one of the paths (an empty name shifts the columns) belongs
    to no blob and is dropped'''
    tmp = tempfile.mkdtemp()
    try:
        keys_by_path = {}
        paths = []
        for key, blob, filename in entries:
            path = os.path.join(os.fsencode(tmp), _temp_name(key, filename))
            with open(path, 'wb') as f:
                f.write(blob)
            keys_by_path[path] = key
            paths.append(path)
        p = subprocess.run((b'ctags', b'-x') + flags + tuple(paths),
                           stdout=subprocess.PIPE)
        # The exit status is ignored: the pipelines swallowed it too
        lines = {}
        for line in _shell_lines(p.stdout):
            fields = line.split(None, 4)
            if len(fields) > 3:
                key = keys_by_path.get(fields[3])
                if key is not None:
                    lines.setdefault(key, []).append(line)
        return lines
    finally:
        shutil.rmtree(tmp)

def _awk123(lines, prefix=b''):
    '''awk '{print "<prefix>"$1" "$2" "$3}': the first three
    whitespace-separated fields, single spaces between, missing
    fields empty (awk prints the separators regardless)'''
    out = []
    for line in lines:
        fields = line.split()[:3]
        fields += [b''] * (3 - len(fields))
        out.append(prefix + b' '.join(fields))
    return out

# parse_defs_C's perl one-liners for .S files; ^ anchors like the
# shell's, \w and \W are [A-Za-z0-9_] and its complement on bytes.
# parse_defs_C's grep -avE -e '^operator ' -e '^CONFIG_' drops the
# operator overloads (name "operator ...") and config macros
_OPERATOR_OR_CONFIG = re.compile(rb'^operator |^CONFIG_')

_ENTRY = re.compile(rb'\s*ENTRY\((\w+)\)')
_SYSCALL_DEFINE = re.compile(rb'SYSCALL_DEFINE[0-9]\(\s*(\w+)\W')

def _scan(blob, pattern, prefix=b''):
    '''One perl -ne scan over the blob: pattern matched at the start
    of every line, printing "<prefix>$1 function $." per match'''
    out = []
    for lineno, line in enumerate(_shell_lines(blob), 1):
        m = pattern.match(line)
        if m:
            out.append(prefix + m.group(1) + b' function ' +
                       str(lineno).encode('ascii'))
    return out

def _defs_C(blob, lines):
    #   ctags -x --kinds-c=+p+x --extras='-{anonymous}' "$full_path" |
    #   grep -avE -e '^operator ' -e '^CONFIG_' |
    #   awk '{print $1" "$2" "$3}'
    lines = [l for l in lines if not _OPERATOR_OR_CONFIG.search(l)]
    return (_awk123(lines)
            + _scan(blob, _ENTRY)
            + _scan(blob, _SYSCALL_DEFINE, b'sys_'))

def _defs_K(blob, lines):
    #   ctags -x --language-force=kconfig --kinds-kconfig=c
    #        --extras-kconfig=-{configPrefixed} "$full_path" |
    #   awk '{print "CONFIG_"$1" "$2" "$3}'
    return _awk123(lines, b'CONFIG_')

def _defs_D(blob, lines):
    #   ctags -x --language-force=dts "$full_path" |
    #   awk '{print $1" "$2" "$3}'
    return _awk123(lines)

# The shell case's per-family ctags flags, keyed as update.py hands
# the families over (as str)
_PARSERS = {'C': _defs_C, 'K': _defs_K, 'D': _defs_D}
_DEFS_FLAGS = {
    'C': (b'--kinds-c=+p+x', b'--extras=-{anonymous}'),
    'K': (b'--language-force=kconfig', b'--kinds-kconfig=c',
          b'--extras-kconfig=-{configPrefixed}'),
    'D': (b'--language-force=dts',),
}

def parse_defs_chunk(items):
    '''parse_defs for a chunk of (blob, filename, family) triples: one
    ctags per (chunk, family) instead of one per blob, everything
    else per blob as before. Returns the per-blob line lists, in the
    input order'''
    out = [[] for _ in items]
    groups = {}
    for i, (blob, filename, family) in enumerate(items):
        if family in _PARSERS:
            if not isinstance(filename, bytes):
                filename = os.fsencode(filename)
            groups.setdefault(family, []).append((i, blob, filename))
    for family, entries in groups.items():
        lines = _chunk_ctags(_DEFS_FLAGS[family], entries)
        for i, blob, filename in entries:
            out[i] = _PARSERS[family](blob, lines.get(i, ()))
    return out

def parse_defs(blob, filename, family):
    '''The b"ident type line" lines script.sh parse-defs printed for
    the blob: same bytes, same order. filename is the ORIGINAL
    basename (ctags keys its language off it; update.py hands it over
    as str, script.sh passed it through argv unchanged), family one
    of C, K, D; other families printed nothing, like the shell case'''
    return parse_defs_chunk([(blob, filename, family)])[0]

'''Port of find-file-doc-comments.pl (script.sh parse-docs): the
b"ident line" lines the perl printed for the blob, associating
kernel-doc "/** ... */" comments with the ctags definitions above
them. Byte for byte, and in the same per-ident order; the perl's
order ACROSS idents was its hash order, i.e. unspecified, and
update.py stores per ident anyway.

ctags stays a subprocess with the perl's own flags, different from
parse_defs' (--c-kinds=+p-m --language-force=C): it builds
line -> (name, type) maps, so one name can have several definitions
(#186) instead of the name-keyed view db.defs has. The maps key on
the line AS PRINTED, and are looked up with the counter
stringified, so a ctags line number that is not a plain decimal just
never matches - as in the perl.'''

# Perl \h at byte level: the single-byte members of its horizontal
# whitespace class, for patterns matched on non-decoded source lines
_H = rb'[\t \xa0]'

# A multiline macro: walk back to its #define
_DOC_DEFINE = re.compile(rb'^' + _H + rb'*#' + _H + rb'*define')
# A line a function's return type could start on (the "int\nfoo()"
# walk-back)
_DOC_STARTS_IDENT = re.compile(rb'^[a-z_]', re.IGNORECASE)
# First line of a doc comment. Source lines keep their \n, and these
# $'s, like perl's, also match just before it
_DOC_OPENER = re.compile(rb'^' + _H + rb'*/\*\*(?:' + _H + rb'|$)')

# The three name-free skip shapes (an empty line, the end of a
# comment, a comment continuation) and the header's shape, all
# compiled once. The original patterns embedded every definition's
# NAME as a literal, and compiling those was nearly the whole scan
# cost; matching the shape once and comparing the captured name
# instead (a C identifier is exactly \w+, so the capture can only be
# the name the literal version matched - the terminator the patterns
# require after it rules out a prefix) is the same match at a
# fraction of the work. A name that is not a plain \w+ never comes
# out of ctags for these languages, but the per-name compile below
# still covers one if it ever does. The bare/qualified pair is the
# pattern's optional struct/enum/union/typedef prefix made explicit:
# the named pattern could match with the prefix left unmatched (" *
# struct foo" IS a header for struct), and only trying both shapes
# compares against the same positions
_SKIP_EMPTY = re.compile(rb'^' + _H + rb'*$')
_SKIP_END = re.compile(rb'^' + _H + rb'+\*/')
_SKIP_CONT = re.compile(rb'^' + _H + rb'+\*(?:' + _H + rb'|$)')
_HEADER_BARE = re.compile(rb'^' + _H + rb'+\*' + _H + rb'+(\w+)'
                          rb'(?:' + _H + rb'|\(|:|$)')
_HEADER_QUAL = re.compile(rb'^' + _H + rb'+\*' + _H + rb'+'
                          rb'(?:(?:struct|enum|union|typedef)'
                          + _H + rb'+)?(\w+)'
                          rb'(?:' + _H + rb'|\(|:|$)')
# The function walk-back's "name at line start" check, same trick
_NAME_START = re.compile(rb'^' + _H + rb'*(\w+)\b')
_WORD = re.compile(rb'\w+\Z')

def _def_patterns(name):
    '''The per-definition matches as (header, skip, starts_name)
    callables: shape matches plus captured-name comparison for a
    plain \w+ name (the whole cost used to be compiling these), the
    perl's own per-name patterns otherwise'''
    if _WORD.fullmatch(name):
        def header(line, bare=_HEADER_BARE, qual=_HEADER_QUAL, n=name):
            m = bare.match(line)
            if m is not None and m.group(1) == n:
                return True
            m = qual.match(line)
            return m is not None and m.group(1) == n
        def skip(line, empty=_SKIP_EMPTY, end=_SKIP_END, cont=_SKIP_CONT,
                 header=header):
            return bool(empty.match(line) or end.match(line)
                        or cont.match(line) or header(line))
        def starts_name(line, start=_NAME_START, n=name):
            m = start.match(line)
            return m is not None and m.group(1) == n
        return header, skip, starts_name

    header_src = (_H + rb'+\*' + _H + rb'+(?:(?:struct|enum|union|typedef)'
                  + _H + rb'+)?' + re.escape(name)
                  + rb'(?:' + _H + rb'|\(|:|$)')
    header_rx = re.compile(rb'^' + header_src)
    skip_rx = re.compile(rb'^(?:' + _H + rb'*$|' + _H + rb'+\*/|'
                         + _H + rb'+\*(?:' + _H + rb'|$)|'
                         + header_src + rb')')
    starts_rx = re.compile(rb'^' + _H + rb'*' + re.escape(name) + rb'\b')
    return header_rx.match, skip_rx.match, starts_rx.match

def _doc_comments(blob, lines):
    # lines: the blob's awk123 b"name kind line" lines
    definition_lines = {}
    definition_types = {}
    for line in lines:
        fields = line.split()
        if len(fields) < 3:
            # The perl then keyed these maps with an undef, which
            # warned - fatal, its $SIG{__WARN__} handler - so it died
            raise ValueError('ctags line without a line number: '
                             + repr(line))
        definition_lines[fields[2]] = fields[0]
        definition_types[fields[2]] = fields[1]

    # Indices match ctags's 1-based linenos
    source_lines = [None] + _shell_lines(blob)

    doc_comments = {}

    # The perl walked every line of the file and looked each one up
    # in the maps; walking only the definition lines visits the same
    # definitions in the same newest-first order and costs work
    # proportional to the definitions, not to the file. A key the
    # walk could never have looked up - a line number ctags did not
    # print as a plain decimal, or one beyond the file's last line -
    # is dead and skipped, as the lookups always skipped it
    last_lineno = len(source_lines) - 1
    linenos = sorted((int(key) for key in definition_lines
                      if key.isdigit() and key != b'0'
                      and key == b'%d' % int(key)
                      and int(key) <= last_lineno), reverse=True)

    # The per-definition patterns only depend on the definition's
    # name, so one name builds them once however often it is defined
    patterns = {}

    for lineno in linenos:
        key = b'%d' % lineno
        definition_name = definition_lines[key]
        definition_type = definition_types[key]

        this = patterns.get(definition_name)
        if this is None:
            this = patterns[definition_name] = _def_patterns(definition_name)
        header, skip, starts_name = this

        # Make sure we get back past the first line of multiline
        # definitions
        if definition_type == b'macro':
            while lineno and not _DOC_DEFINE.match(source_lines[lineno]):
                lineno -= 1
        elif definition_type == b'function':
            # Try to handle the case of "int\nfoo()"
            if starts_name(source_lines[lineno]):
                while lineno and _DOC_STARTS_IDENT.match(source_lines[lineno]):
                    lineno -= 1

        # Move to the first line that might be a doc comment
        lineno -= 1
        if lineno <= 0:
            continue

        # Find the last line that could be a doc-comment header for
        # this function
        while lineno and skip(source_lines[lineno]):
            lineno -= 1
        lineno += 1  # We may have just skipped past the header itself

        # Is it actually a header for this function?
        if not header(source_lines[lineno]):
            continue

        # We have found a header. Confirm it's a doc comment.
        lineno -= 1
        if not (lineno > 0 and _DOC_OPENER.match(source_lines[lineno])):
            continue

        # We have found a doc comment for this function! The lines are
        # pushed while walking the file backwards, so for one name they
        # come out in descending order of definition line
        doc_comments.setdefault(definition_name, []).append(lineno)

    out = []
    for name, linenos in doc_comments.items():
        out += [name + b' ' + b'%d' % lineno for lineno in linenos]
    return out

# The perl's ctags flags, chunked like parse_defs'. --language-force=C
# makes the temp names' extensions irrelevant
_DOCS_FLAGS = (b'--c-kinds=+p-m', b'--language-force=C')

def parse_doc_comments_chunk(blobs):
    '''parse_doc_comments for a chunk of blobs: the b"/\*\*" gate first
    (a doc comment needs an opener, and 80% of kernel C/H files have
    none at all, so most blobs never reach ctags), then ONE ctags for
    the chunk's survivors. The ^operator grep happened before the
    maps, as in the perl. Returns the per-blob line lists, in the
    input order'''
    out = [[] for _ in blobs]
    entries = [(i, blob, b'') for i, blob in enumerate(blobs)
               if b'/**' in blob]
    if not entries:
        return out
    lines = _chunk_ctags(_DOCS_FLAGS, entries)
    for i, blob, _ in entries:
        blob_lines = [l for l in lines.get(i, ())
                      if not l.startswith(b'operator ')]
        out[i] = _doc_comments(blob, _awk123(blob_lines))
    return out

def parse_doc_comments(blob):
    '''The b"ident line" lines find-file-doc-comments.pl printed for
    the blob: same bytes. The ident is the documented definition's
    name, the line the /** opener's line. One ctags subprocess for the
    chunk, run on temp copies of the blobs as the perl was (through
    script.sh's mktemp); --language-force=C made the temp names
    irrelevant, so any unique name serves'''
    return parse_doc_comments_chunk([blob])[0]
