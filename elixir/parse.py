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
