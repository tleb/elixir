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

ctags stays a subprocess, one per blob as in script.sh; the grep,
awk and perl around it became Python string ops on bytes. ctags
needs a real file on disk and keys its parser off the filename, so
the blob is written to a fresh temp directory under its ORIGINAL
basename — which is why script.sh copied to $tmp/$opt2.

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

def _ctags_lines(flags, path):
    '''ctags -x cross-reference lines of path; the flags come as the
    subprocess argument list (the shell's quotes never reached ctags)'''
    p = subprocess.run((b'ctags', b'-x') + flags + (path,),
                       stdout=subprocess.PIPE)
    # The exit status is ignored: the pipelines swallowed it too
    return _shell_lines(p.stdout)

# parse_defs_C's grep -avE -e '^operator ' -e '^CONFIG_': drop the
# operator overloads (name "operator ...") and config macros
_OPERATOR_OR_CONFIG = re.compile(rb'^operator |^CONFIG_')

# parse_defs_C's perl one-liners for .S files; ^ anchors like the
# shell's, \w and \W are [A-Za-z0-9_] and its complement on bytes
_ENTRY = re.compile(rb'\s*ENTRY\((\w+)\)')
_SYSCALL_DEFINE = re.compile(rb'SYSCALL_DEFINE[0-9]\(\s*(\w+)\W')

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

def _defs_C(blob, path):
    #   ctags -x --kinds-c=+p+x --extras='-{anonymous}' "$full_path" |
    #   grep -avE -e '^operator ' -e '^CONFIG_' |
    #   awk '{print $1" "$2" "$3}'
    lines = _ctags_lines((b'--kinds-c=+p+x', b'--extras=-{anonymous}'), path)
    lines = [l for l in lines if not _OPERATOR_OR_CONFIG.search(l)]
    return (_awk123(lines)
            + _scan(blob, _ENTRY)
            + _scan(blob, _SYSCALL_DEFINE, b'sys_'))

def _defs_K(blob, path):
    #   ctags -x --language-force=kconfig --kinds-kconfig=c
    #        --extras-kconfig=-{configPrefixed} "$full_path" |
    #   awk '{print "CONFIG_"$1" "$2" "$3}'
    return _awk123(_ctags_lines(
        (b'--language-force=kconfig', b'--kinds-kconfig=c',
         b'--extras-kconfig=-{configPrefixed}'), path), b'CONFIG_')

def _defs_D(blob, path):
    #   ctags -x --language-force=dts "$full_path" |
    #   awk '{print $1" "$2" "$3}'
    return _awk123(_ctags_lines((b'--language-force=dts',), path))

_PARSERS = {'C': _defs_C, 'K': _defs_K, 'D': _defs_D}

def parse_defs(blob, filename, family):
    '''The b"ident type line" lines script.sh parse-defs printed for
    the blob: same bytes, same order. filename is the ORIGINAL
    basename (ctags keys its language off it; update.py hands it over
    as str, script.sh passed it through argv unchanged), family one
    of C, K, D; other families printed nothing, like the shell case'''
    defs = _PARSERS.get(family)
    if defs is None:
        return []
    if not isinstance(filename, bytes):
        filename = os.fsencode(filename)

    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(os.fsencode(tmp), filename)
        with open(path, 'wb') as f:
            f.write(blob)
        return defs(blob, path)
    finally:
        shutil.rmtree(tmp)

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

def _doc_comments(blob, path):
    lines = _ctags_lines((b'--c-kinds=+p-m', b'--language-force=C'), path)
    lines = [l for l in lines if not l.startswith(b'operator ')]
    lines = _awk123(lines)

    # Index definitions by line, not by name: multiple definitions can
    # share a name (#186), and the last one ctags reported on a line
    # wins
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

    for lineno in range(len(source_lines) - 1, 0, -1):
        key = b'%d' % lineno
        if key not in definition_lines:
            continue
        definition_name = definition_lines[key]
        definition_type = definition_types[key]

        # Comment header: be liberal in what we accept. For example,
        # do not check the type of the definition/declaration against
        # the type in the comment header.
        #   ^\h+\*\h+(?:(?:struct|enum|union|typedef)\h+)?NAME(?:\h|\(|:|$)
        header_src = (_H + rb'+\*' + _H + rb'+(?:(?:struct|enum|union|typedef)'
                      + _H + rb'+)?' + re.escape(definition_name)
                      + rb'(?:' + _H + rb'|\(|:|$)')
        header = re.compile(rb'^' + header_src)
        skip = re.compile(rb'^(?:' + _H + rb'*$|' + _H + rb'+\*/|'
                          + _H + rb'+\*(?:' + _H + rb'|$)|'
                          + header_src + rb')')

        # Make sure we get back past the first line of multiline
        # definitions
        if definition_type == b'macro':
            while lineno and not _DOC_DEFINE.match(source_lines[lineno]):
                lineno -= 1
        elif definition_type == b'function':
            # Try to handle the case of "int\nfoo()"
            if re.match(rb'^' + _H + rb'*' + re.escape(definition_name)
                        + rb'\b', source_lines[lineno]):
                while lineno and _DOC_STARTS_IDENT.match(source_lines[lineno]):
                    lineno -= 1

        # Move to the first line that might be a doc comment
        lineno -= 1
        if lineno <= 0:
            continue

        # Find the last line that could be a doc-comment header for
        # this function
        while lineno and skip.match(source_lines[lineno]):
            lineno -= 1
        lineno += 1  # We may have just skipped past the header itself

        # Is it actually a header for this function?
        if not header.match(source_lines[lineno]):
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

def parse_doc_comments(blob):
    '''The b"ident line" lines find-file-doc-comments.pl printed for
    the blob: same bytes. The ident is the documented definition's
    name, the line the /** opener's line. One ctags subprocess, run on
    a temp copy of the blob as the perl was (through script.sh's
    mktemp); --language-force=C made the temp name irrelevant, so any
    mkstemp file serves'''
    fd, path = tempfile.mkstemp()
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(blob)
        return _doc_comments(blob, os.fsencode(path))
    finally:
        os.unlink(path)
