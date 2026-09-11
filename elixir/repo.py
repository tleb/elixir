#!/usr/bin/env python3

#  This file is part of Elixir, a source code cross-referencer.
#
#  Copyright (C) 2017--2020 Mikaël Bouillot <mikael.bouillot@bootlin.com>
#                           and contributors
#
#  Elixir is free software: you can redistribute it and/or modify
#  it under the terms of the GNU Affero General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Elixir is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU Affero General Public License for more details.
#
#  You should have received a copy of the GNU Affero General Public License
#  along with Elixir.  If not, see <http://www.gnu.org/licenses/>.

'''Git plumbing, replacing the git subprocesses and shell text
processing (sed/sort/awk pipelines) that script.sh ran for update.py
and query.py.

Everything here works on bytes: tags, paths and blobs are byte
strings, exactly what git prints and what the databases store.

The version comparator is a faithful port of GNU sort's -V comparison
(gnulib's filevercmp.c); it is pinned against the real sort -V in
t/test_repo.py, over every clone's tag list. The per-project tag
pipelines (TAG_PIPELINES, the projects/*.sh plugins) are pinned
against script.sh's own output in t/test_goldens.py.'''

import functools
import os
import re
import subprocess
from dataclasses import dataclass
from threading import local

from elixir.lib import getRepoDir

# Projects with DT bindings compatible strings support (script.sh's
# dts_comp_support=1)
DTS_COMP_SUPPORT = frozenset((
    'arm-trusted-firmware',
    'barebox',
    'linux',
    'u-boot',
    'zephyr',
    'testproj', # pytest tree project (projects/testproj.sh)
))

# Tag pipelines: the ports of the projects/<project>.sh plugins,
# below, one TagConfig per project that overrides anything. musl,
# uclibc-ng, vpp, iproute2 and opensbi define nothing and have no
# entry; testproj only sets dts_comp_support, which lives in
# DTS_COMP_SUPPORT above. t/test_goldens.py pins every entry against
# script.sh's output.


def git(repo_dir, *args):
    '''Run git in repo_dir, return stdout as bytes'''
    p = subprocess.run(('git',) + args, cwd=repo_dir, stdout=subprocess.PIPE,
                       check=True)
    return p.stdout


def git_lines(repo_dir, *args):
    '''git() output split into lines, like scriptLines'''
    lines = git(repo_dir, *args).split(b'\n')
    del lines[-1]
    return lines


# Version order, in GNU sort -V's steps. Numbers compare numerically
# (v9 < v10), and characters group as digits < letters < the rest,
# with '~' sorting before everything, even a shorter string's end.
# This is gnulib's filevercmp.c (which sort -V calls per line),
# including its file-suffix handling, ported to bytes.

def _is_digit(c):
    return 0x30 <= c <= 0x39

def _is_alpha(c):
    return 0x41 <= c <= 0x5A or 0x61 <= c <= 0x7A

def _order(s, pos):
    '''Sort weight of s[pos], or of the end of s when pos == len(s)'''
    if pos == len(s):
        return -1
    c = s[pos]
    if _is_digit(c):
        return 0
    elif _is_alpha(c):
        return c
    elif c == 0x7E: # '~'
        return -2
    else:
        return c + 256

def _verrevcmp(s1, s2):
    pos1 = 0
    pos2 = 0
    len1 = len(s1)
    len2 = len(s2)
    while pos1 < len1 or pos2 < len2:
        first_diff = 0

        # Compare the non-digit prefixes
        while ((pos1 < len1 and not _is_digit(s1[pos1])) or
               (pos2 < len2 and not _is_digit(s2[pos2]))):
            o1 = _order(s1, pos1)
            o2 = _order(s2, pos2)
            if o1 != o2:
                return o1 - o2
            pos1 += 1
            pos2 += 1

        # Skip leading zeros: v1.02 == v1.2 (the first difference
        # between the remaining digits still breaks the tie)
        while pos1 < len1 and s1[pos1] == 0x30:
            pos1 += 1
        while pos2 < len2 and s2[pos2] == 0x30:
            pos2 += 1

        # Compare the digit runs
        while (pos1 < len1 and pos2 < len2 and
               _is_digit(s1[pos1]) and _is_digit(s2[pos2])):
            if not first_diff:
                first_diff = s1[pos1] - s2[pos2]
            pos1 += 1
            pos2 += 1

        # The run that continues with a digit is the bigger number
        if pos1 < len1 and _is_digit(s1[pos1]):
            return 1
        if pos2 < len2 and _is_digit(s2[pos2]):
            return -1
        if first_diff:
            return first_diff
    return 0

def _prefixlen(s):
    '''Length of the prefix of s left after stripping the longest
    suffix matching (\\.[A-Za-z~][A-Za-z0-9~]*)*$ ("file.c" -> 4)'''
    n = len(s)
    prefixlen = 0
    i = 0
    while True:
        if i == n:
            return prefixlen
        i += 1
        prefixlen = i
        while (i + 1 < n and s[i] == 0x2E and
               (_is_alpha(s[i + 1]) or s[i + 1] == 0x7E)):
            i += 2
            while i < n and (_is_digit(s[i]) or _is_alpha(s[i]) or s[i] == 0x7E):
                i += 1

def filevercmp(a, b):
    '''Version-compare two byte strings; negative/zero/positive like
    C's filevercmp()'''
    if not a:
        return -1 if b else 0
    if not b:
        return 1

    # Leading-dot names ("." < ".." < ".foo" < "foo") never occur in
    # tags, but this is the reference algorithm
    if a[0] == 0x2E:
        if b[0] != 0x2E:
            return -1
        if len(a) == 1:
            return -1 if len(b) > 1 else 0
        if len(b) == 1:
            return 1
        a_dotdot = a[1] == 0x2E and len(a) == 2
        b_dotdot = b[1] == 0x2E and len(b) == 2
        if a_dotdot:
            return -1 if not b_dotdot else 0
        if b_dotdot:
            return 1
    elif b[0] == 0x2E:
        return 1

    # Compare without the suffixes first, then with them if that
    # was a tie (so "8.2" < "8.2.gz" but "8.2" < "8.2a")
    aprefix = _prefixlen(a)
    bprefix = _prefixlen(b)
    one_pass_only = aprefix == len(a) and bprefix == len(b)

    result = _verrevcmp(a[:aprefix], b[:bprefix])
    if result or one_pass_only:
        return result
    return _verrevcmp(a, b)

def versioncmp(a, b):
    '''filevercmp with sort's C-locale tie-break: lines the version
    comparison considers equal order by their raw bytes'''
    result = filevercmp(a, b)
    if result:
        return result
    return (a > b) - (a < b)

def version_sort(items):
    '''Sort byte strings like LC_ALL=C sort -V'''
    return sorted(items, key=functools.cmp_to_key(versioncmp))


def default_tag_pipeline(tags):
    '''The script.sh get_tags default:
    git tag | sed 's/$/.0/' | sort -V | sed 's/\\.0$//'
    The appended .0 makes bare releases (v3) compare against
    point releases (v3.5) as v3.0; the tags themselves come back,
    version-ordered, so the strip is the identity and only the
    comparison uses the padded keys.'''
    pairs = [(tag + b'.0', tag) for tag in tags]
    pairs.sort(key=functools.cmp_to_key(lambda x, y: versioncmp(x[0], y[0])))
    return [tag for _, tag in pairs]

# Shell text-processing primitives, for the pipelines below. Each
# behaves exactly like the command it replaces on bytes.

def _tac(lines):
    return lines[::-1]

def _grep(pattern, lines):
    rx = re.compile(pattern)
    return [line for line in lines if rx.search(line)]

def _grep_v(pattern, lines):
    rx = re.compile(pattern)
    return [line for line in lines if not rx.search(line)]

def _sed(pattern, repl, lines):
    '''One s/pattern/repl/ per line, like sed without the g flag;
    non-matching lines pass through unchanged. repl is a sed-style
    replacement (b'\\1' backreferences) or a match -> bytes function
    (for GNU sed's \\\'60'-style references, which re rejects)'''
    rx = re.compile(pattern)
    return [rx.sub(repl, line, count=1) for line in lines]

def _sort_vr(lines):
    '''sort -Vr: sort -V order reversed (newest first)'''
    return list(reversed(version_sort(lines)))


def _cat(lines):
    '''script.sh's default version_dir()/version_rev()/list_tags():
    cat (and `echo "$tags"`); identity on one line or a list'''
    return lines

# script.sh list_tags_h:
#   echo "$tags" | tac | sed -r 's/^(v[0-9]*)\.([0-9]*)(.*)$/\1 \1.\2 \1.\2\3/'
def default_list_tags_h(tags):
    return _sed(rb'^(v[0-9]*)\.([0-9]*)(.*)$', rb'\1 \1.\2 \1.\2\3',
                _tac(tags))


@dataclass(frozen=True)
class TagConfig:
    '''The tag functions one projects/<project>.sh plugin overrides;
    every field is the shell function of the same name, ported. None
    means script.sh's default, which is not always a plain function:
    the default get_tags and get_latest_tags compose version_dir, so
    they are resolved at call time instead.'''
    version_dir: callable = _cat   # tag names -> display versions
    version_rev: callable = _cat   # display version -> tag name
    get_tags: callable = None      # full pipeline: `git tag` -> ordered
    list_tags: callable = _cat     # filter over get_tags output
    list_tags_h: callable = default_list_tags_h
    latest_tags: callable = None   # full pipeline: `git tag` -> newest first


# ---- Ports of the projects/<project>.sh plugins ----
# One function per shell function, one Python line per pipeline
# stage, with the original above it. GNU sed's .*? is plain greedy
# (the ? is inert), so the ports use .*; and BRE's [\.] bracket
# matches backslash or dot, hence [\\.] — no real tag has a backslash.

# busybox.sh:
#   version_dir() { tr '_.' '._'; }   version_rev() { tr '._' '_.'; }
_BUSYBOX_TO_DISPLAY = bytes.maketrans(b'_.', b'._')
_BUSYBOX_TO_TAG = bytes.maketrans(b'._', b'_.')

def busybox_version_dir(tags):
    return [tag.translate(_BUSYBOX_TO_DISPLAY) for tag in tags]

def busybox_version_rev(v):
    return v.translate(_BUSYBOX_TO_TAG)

# busybox.sh list_tags_h:
#   tac | sed -r 's/^([0-9]*)\.([0-9]*)(.*)$/v\1 \1.\2 \1.\2\3/'
def busybox_list_tags_h(tags):
    return _sed(rb'^([0-9]*)\.([0-9]*)(.*)$', rb'v\1 \1.\2 \1.\2\3',
                _tac(tags))

# coreboot.sh / ofono.sh list_tags_h (same but with a v on the second
# column, unlike busybox's):
#   tac | sed -r 's/^([0-9]*)\.([0-9]*)(.*)$/v\1 v\1.\2 \1.\2\3/'
def _num_tags_h(tags):
    return _sed(rb'^([0-9]*)\.([0-9]*)(.*)$', rb'v\1 v\1.\2 \1.\2\3',
                _tac(tags))

# freebsd.sh:
#   version_dir() { grep "^release/[0-9]*\.[0-9]*\.[0-9]*$" |
#                   sed -e 's,^release/,v,' -e 's,\.0$,,'; }
#   version_rev() { grep "^v" |
#                   sed -e 's,v[0-9]*\.[0-9]*$,&\.0,' -e 's,^v,release/,'; }
def freebsd_version_dir(tags):
    tags = _grep(rb'^release/[0-9]*\.[0-9]*\.[0-9]*$', tags)
    tags = _sed(rb'^release/', b'v', tags)
    return _sed(rb'\.0$', b'', tags)

def freebsd_version_rev(v):
    if not v.startswith(b'v'): # grep dropped the line: empty output
        return b''
    v = _sed(rb'v[0-9]*\.[0-9]*$', lambda m: m.group(0) + b'.0', [v])[0]
    return _sed(rb'^v', b'release/', [v])[0]

# xen.sh:
#   version_dir() { grep "^RELEASE" | sed 's/^RELEASE-/v/'; }
#   version_rev() { grep "^v" | sed 's/^v/RELEASE-/'; }
def xen_version_dir(tags):
    return _sed(rb'^RELEASE-', b'v', _grep(rb'^RELEASE', tags))

def xen_version_rev(v):
    if not v.startswith(b'v'): # grep dropped the line: empty output
        return b''
    return _sed(rb'^v', b'RELEASE-', [v])[0]

# amazon-freertos.sh list_tags_h (YYYYMM tags first, then v tags) and
# get_latest_tags:
#   grep -v '^v' | tac | sed -r 's/^(\d\d\d\d)(\d\d)(.*)$/\1 \1\2 \1\2\3/'
#   grep '^v' | tac | <the default sed>
#   git tag | grep '^20' | sort -Vr
def amazon_freertos_list_tags_h(tags):
    b1 = _sed(rb'^([0-9][0-9][0-9][0-9])([0-9][0-9])(.*)$',
              rb'\1 \1\2 \1\2\3', _tac(_grep_v(rb'^v', tags)))
    return b1 + default_list_tags_h(_grep(rb'^v', tags))

def amazon_freertos_latest_tags(raw):
    return _sort_vr(_grep(rb'^20', raw))

# arm-trusted-firmware.sh list_tags_h:
#   grep -v 'for-v0\.4' | tac | <the default sed>
#   grep 'for-v0\.4' | tac | sed -r 's/^/custom for-v0.4 /'
def atf_list_tags_h(tags):
    b1 = default_list_tags_h(_grep_v(rb'for-v0\.4', tags))
    b2 = _sed(rb'^', b'custom for-v0.4 ', _tac(_grep(rb'for-v0\.4', tags)))
    return b1 + b2

# barebox.sh list_tags_h, three blocks:
#   grep '^v20' | tac | sed -r 's/^(v20..)\.([0-9][0-9])\.(.*)$/\1 \1.\2 \1.\2.\3/'
#   grep '^v2\.0' | tac | sed -r 's/^(v2\.0)(.*)$/old \1 \1\2/'
#   grep '^freescale' | tac | sed -r 's/^(freescale)(.*)$/old \1 \1\2/'
def barebox_list_tags_h(tags):
    b1 = _sed(rb'^(v20..)\.([0-9][0-9])\.(.*)$', rb'\1 \1.\2 \1.\2.\3',
              _tac(_grep(rb'^v20', tags)))
    b2 = _sed(rb'^(v2\.0)(.*)$', rb'old \1 \1\2',
              _tac(_grep(rb'^v2\.0', tags)))
    b3 = _sed(rb'^(freescale)(.*)$', rb'old \1 \1\2',
              _tac(_grep(rb'^freescale', tags)))
    return b1 + b2 + b3

# bluez.sh list_tags/list_tags_h/get_latest_tags:
#   grep '^[0-9]'
#   grep '^[0-9]' | sort -rV | sed -E 's/^([0-9]*)\.([0-9]*)$/v\1 v\1.\2 \1.\2/'
#   git tag | grep '^[0-9]\.' | sort -Vr
def bluez_list_tags(tags):
    return _grep(rb'^[0-9]', tags)

def bluez_list_tags_h(tags):
    return _sed(rb'^([0-9]*)\.([0-9]*)$', rb'v\1 v\1.\2 \1.\2',
                _sort_vr(_grep(rb'^[0-9]', tags)))

def bluez_latest_tags(raw):
    return _sort_vr(_grep(rb'^[0-9]\.', raw))

# dpdk.sh list_tags_h, two blocks:
#   grep -vE '^v1\.|^v2\.' | tac | sed -r 's/^v([0-9]*)\.([0-9]*)(.*)$/v\1 v\1.\2 v\1.\2\3/'
#   grep -E '^v1\.|^v2\.' | tac | sed -r 's/^v(1|2)\.([0-9])(.*)$/old v\1.\2 v\1.\2\3/'
def dpdk_list_tags_h(tags):
    b1 = _sed(rb'^v([0-9]*)\.([0-9]*)(.*)$', rb'v\1 v\1.\2 v\1.\2\3',
              _tac(_grep_v(rb'^v1\.|^v2\.', tags)))
    b2 = _sed(rb'^v(1|2)\.([0-9])(.*)$', rb'old v\1.\2 v\1.\2\3',
              _tac(_grep(rb'^v1\.|^v2\.', tags)))
    return b1 + b2

# glibc.sh list_tags/list_tags_h:
#   grep -v 'cvs'
#   grep "glibc" | grep -v "fedora" | grep -v "cvs" | tac |
#     sed -r 's/^glibc-([0-9]*)(\.[0-9]*)(.*)$/v\1 v\1\2 glibc-\1\2\3/'
#   grep -v "cvs" | grep "fedora" | tac |
#     sed -r 's/^fedora\/glibc-([0-9]*)(\.[0-9]*)(.*)$/fedora v\1\2 fedora\/glibc-\1\2\3/'
def glibc_list_tags(tags):
    return _grep_v(rb'cvs', tags)

def glibc_list_tags_h(tags):
    b1 = _tac(_grep_v(rb'cvs', _grep_v(rb'fedora', _grep(rb'glibc', tags))))
    b1 = _sed(rb'^glibc-([0-9]*)(\.[0-9]*)(.*)$',
              rb'v\1 v\1\2 glibc-\1\2\3', b1)
    b2 = _tac(_grep(rb'fedora', _grep_v(rb'cvs', tags)))
    b2 = _sed(rb'^fedora/glibc-([0-9]*)(\.[0-9]*)(.*)$',
              rb'fedora v\1\2 fedora/glibc-\1\2\3', b2)
    return b1 + b2

# grub.sh list_tags_h (the first '.' in the pattern is any character,
# unescaped in the shell):
#   tac | sed -r 's/^(grub-)?([0-9]+).([0-9]+)([A-Za-z0-9.-]*)$/\2 \2.\3 \1\2.\3\4/'
def grub_list_tags_h(tags):
    return _sed(rb'^(grub-)?([0-9]+).([0-9]+)([A-Za-z0-9.-]*)$',
                rb'\2 \2.\3 \1\2.\3\4', _tac(tags))

# linux.sh get_tags, the capture-group shuffle. The first sed rewrites
# each tag so that sort -V orders it by the pieces that matter (the
# numbered part up front, pre/alpha suffixes at the end); the second
# sed reassembles it. \60 in the replacement is group 6 then a
# literal 0 (GNU sed falls back from the nonexistent group 60).
def _linux_shuffle(m):
    g = m.groups()
    return (g[1] + b'#' + g[2] + b'@' + g[3] + b'@' + g[4] + b'@' +
            g[5] + b'0@' + g[0] + b'.0')

def _linux_unshuffle(m):
    g = m.groups()
    return g[5] + b''.join(g[:5])

_LINUX_SHUFFLE = re.compile(
    rb'^(pre|lia64-|)(v?[0-9\.]*)(pre|-[^pf].*|)(alpha|-[pf].*|)([0-9]*)(.*)$')
_LINUX_UNSPLIT = re.compile(rb'^(.*)#(.*)@(.*)@(.*)@(.*)0@(.*)\.0$')

def linux_get_tags(raw):
    keys = [_LINUX_SHUFFLE.sub(_linux_shuffle, tag, count=1) for tag in raw]
    return [_LINUX_UNSPLIT.sub(_linux_unshuffle, key, count=1)
            for key in version_sort(keys)]

# linux.sh list_tags_h:
#   tac | sed -r 's/^(pre|lia64-|)(v?)([0-9]*)\.([0-9]*)(.*)$/v\3 v\3.\4 \1\2\3.\4\5/'
def linux_list_tags_h(tags):
    return _sed(rb'^(pre|lia64-|)(v?)([0-9]*)\.([0-9]*)(.*)$',
                rb'v\3 v\3.\4 \1\2\3.\4\5', _tac(tags))

# llvm.sh (note the tac before the grep in list_tags: llvmorg tags are
# listed newest first — the live behavior):
#   tac | grep ^llvmorg-[0-9]*[\.][0-9]*
#   grep ^llvmorg | grep -v init | tac |
#     sed -r 's/^llvmorg-([0-9]*)\.([0-9]*)(.*)$/v\1 v\1.\2 llvmorg-\1.\2\3/'
#   git tag | grep 'llvmorg' | grep -v init | sort -Vr
_LLVM_ANN = rb'^llvmorg-[0-9]*[\\.][0-9]*'

def llvm_list_tags(tags):
    return _grep(_LLVM_ANN, _tac(tags))

def llvm_list_tags_h(tags):
    tags = _tac(_grep_v(rb'init', _grep(rb'^llvmorg', tags)))
    return _sed(rb'^llvmorg-([0-9]*)\.([0-9]*)(.*)$',
                rb'v\1 v\1.\2 llvmorg-\1.\2\3', tags)

def llvm_latest_tags(raw):
    return _sort_vr(_grep_v(rb'init', _grep(rb'llvmorg', raw)))

# mesa.sh, same shapes as llvm's:
#   tac | grep ^mesa-[0-9]*[\.][0-9]*
#   grep ^mesa-[0-9]*[\.][0-9]* | tac |
#     sed -r 's/^mesa-([0-9]*)(\.[0-9]*)(.*)$/v\1 v\1\2 mesa-\1\2\3/'
#   git tag | version_dir | grep ^mesa-[0-9]*[\.][0-9]* | grep -v '\-rc' | sort -Vr
#     (version_dir is the identity: mesa does not override it)
_MESA_ANN = rb'^mesa-[0-9]*[\\.][0-9]*'

def mesa_list_tags(tags):
    return _grep(_MESA_ANN, _tac(tags))

def mesa_list_tags_h(tags):
    return _sed(rb'^mesa-([0-9]*)(\.[0-9]*)(.*)$',
                rb'v\1 v\1\2 mesa-\1\2\3', _tac(_grep(_MESA_ANN, tags)))

def mesa_latest_tags(raw):
    return _sort_vr(_grep_v(rb'-rc', _grep(_MESA_ANN, raw)))

# op-tee.sh:
#   grep '^[0-9]\.'
#   grep '^[0-9]\.' | tac | <the busybox sed>
#   git tag | grep '^[0-9]\.' | grep -v '\-rc' | sort -Vr
def op_tee_list_tags(tags):
    return _grep(rb'^[0-9]\.', tags)

def op_tee_list_tags_h(tags):
    return busybox_list_tags_h(_grep(rb'^[0-9]\.', tags))

def op_tee_latest_tags(raw):
    return _sort_vr(_grep_v(rb'-rc', _grep(rb'^[0-9]\.', raw)))

# qemu.sh list_tags_h, three blocks (the last one a literal line):
#   grep -E "^v[0-9].*" | tac | sed -r 's/^(v[0-9])\.([0-9]*)(.*)$/\1 \1.\2 \1.\2\3/'
#   grep "release" | tac | sed -r 's/^(release)_([0-9_]*)$/old \1 \1_\2/'
#   echo "old initial initial"
def qemu_list_tags_h(tags):
    b1 = _sed(rb'^(v[0-9])\.([0-9]*)(.*)$', rb'\1 \1.\2 \1.\2\3',
              _tac(_grep(rb'^v[0-9].*', tags)))
    b2 = _sed(rb'^(release)_([0-9_]*)$', rb'old \1 \1_\2',
              _tac(_grep(rb'release', tags)))
    return b1 + b2 + [b'old initial initial']

# toybox.sh list_tags_h (like the busybox one, but no v prefixes):
#   tac | sed -r 's/^([0-9]*)\.([0-9]*)(.*)$/\1 \1.\2 \1.\2\3/'
def toybox_list_tags_h(tags):
    return _sed(rb'^([0-9]*)\.([0-9]*)(.*)$', rb'\1 \1.\2 \1.\2\3',
                _tac(tags))

# u-boot.sh list_tags_h, three blocks:
#   grep '^v20' | tac | sed -r 's/^(v20..)\.([0-9][0-9])(.*)$/\1 \1.\2 \1.\2\3/'
#   grep -E '^(v1|U)' | tac | sed -r 's/^/old by-version /'
#   grep -E '^(LABEL|DENX)' | tac | sed -r 's/^/old by-date /'
def u_boot_list_tags_h(tags):
    b1 = _sed(rb'^(v20..)\.([0-9][0-9])(.*)$', rb'\1 \1.\2 \1.\2\3',
              _tac(_grep(rb'^v20', tags)))
    b2 = _sed(rb'^', b'old by-version ', _tac(_grep(rb'^(v1|U)', tags)))
    b3 = _sed(rb'^', b'old by-date ', _tac(_grep(rb'^(LABEL|DENX)', tags)))
    return b1 + b2 + b3

# zephyr.sh:
#   grep -v '^zephyr-v'
#   grep -v '^zephyr-v' | tac |
#     sed -r 's/^(v[0-9]*)\.([0-9]*)(.*)$/\1 \1.\2 \1.\2\3/'
#   git tag | grep -v '^zephyr-v' | version_dir | grep -v '\-rc' | sort -Vr
#     (version_dir is the identity: zephyr does not override it)
def zephyr_list_tags(tags):
    return _grep_v(rb'^zephyr-v', tags)

def zephyr_list_tags_h(tags):
    return _sed(rb'^(v[0-9]*)\.([0-9]*)(.*)$', rb'\1 \1.\2 \1.\2\3',
                _tac(_grep_v(rb'^zephyr-v', tags)))

def zephyr_latest_tags(raw):
    return _sort_vr(_grep_v(rb'-rc', _grep_v(rb'^zephyr-v', raw)))


TAG_PIPELINES = {
    'amazon-freertos': TagConfig(list_tags_h=amazon_freertos_list_tags_h,
                                 latest_tags=amazon_freertos_latest_tags),
    'arm-trusted-firmware': TagConfig(list_tags_h=atf_list_tags_h),
    'barebox': TagConfig(list_tags_h=barebox_list_tags_h),
    'bluez': TagConfig(list_tags=bluez_list_tags, list_tags_h=bluez_list_tags_h,
                       latest_tags=bluez_latest_tags),
    'busybox': TagConfig(version_dir=busybox_version_dir,
                         version_rev=busybox_version_rev,
                         list_tags_h=busybox_list_tags_h),
    'coreboot': TagConfig(list_tags_h=_num_tags_h),
    'dpdk': TagConfig(list_tags_h=dpdk_list_tags_h),
    'freebsd': TagConfig(version_dir=freebsd_version_dir,
                         version_rev=freebsd_version_rev),
    'glibc': TagConfig(list_tags=glibc_list_tags, list_tags_h=glibc_list_tags_h),
    'grub': TagConfig(list_tags_h=grub_list_tags_h),
    'linux': TagConfig(get_tags=linux_get_tags, list_tags_h=linux_list_tags_h),
    'llvm': TagConfig(list_tags=llvm_list_tags, list_tags_h=llvm_list_tags_h,
                      latest_tags=llvm_latest_tags),
    'mesa': TagConfig(list_tags=mesa_list_tags, list_tags_h=mesa_list_tags_h,
                      latest_tags=mesa_latest_tags),
    'ofono': TagConfig(list_tags_h=_num_tags_h),
    'op-tee': TagConfig(list_tags=op_tee_list_tags, list_tags_h=op_tee_list_tags_h,
                        latest_tags=op_tee_latest_tags),
    'qemu': TagConfig(list_tags_h=qemu_list_tags_h),
    'toybox': TagConfig(list_tags_h=toybox_list_tags_h),
    'u-boot': TagConfig(list_tags_h=u_boot_list_tags_h),
    'xen': TagConfig(version_dir=xen_version_dir, version_rev=xen_version_rev),
    'zephyr': TagConfig(list_tags=zephyr_list_tags, list_tags_h=zephyr_list_tags_h,
                        latest_tags=zephyr_latest_tags),
}

_DEFAULT_TAGS = TagConfig()

def _tag_config(project=None):
    return TAG_PIPELINES.get(project, _DEFAULT_TAGS)

def _get_tags(repo_dir, cfg):
    '''script.sh get_tags: the project's pipeline over `git tag`'''
    raw = git_lines(repo_dir, 'tag')
    if cfg.get_tags is not None:
        return cfg.get_tags(raw)
    return default_tag_pipeline(cfg.version_dir(raw))


def version_dir(tags, project=None):
    '''Tag names -> display versions (script.sh version_dir)'''
    return _tag_config(project).version_dir(tags)

def version_rev(v, project=None):
    '''Display version -> tag name; b'' when the name does not start
    the way the project's grep wants, like `echo v | version_rev`
    whose output the command substitution reduces to nothing'''
    return _tag_config(project).version_rev(v)

def list_tags(repo_dir, project=None):
    '''All tags of the repository, oldest first in the project's
    version order (script.sh list-tags; llvm and mesa list newest
    first — their plugin reverses before filtering)'''
    cfg = _tag_config(project)
    return cfg.list_tags(_get_tags(repo_dir, cfg))

def list_tags_h(repo_dir, project=None):
    '''Tag menu lines "topmenu submenu tag", newest first (script.sh
    list-tags -h); lines the project's sed does not match pass
    through, so one-field lines occur and query.py tolerates them'''
    cfg = _tag_config(project)
    return cfg.list_tags_h(_get_tags(repo_dir, cfg))

def latest_tags(repo_dir, project=None):
    '''Non-rc tags, newest first (script.sh get-latest-tags)'''
    cfg = _tag_config(project)
    if cfg.latest_tags is not None:
        return cfg.latest_tags(git_lines(repo_dir, 'tag'))
    return _sort_vr(_grep_v(rb'-rc', cfg.version_dir(git_lines(repo_dir, 'tag'))))

def list_blobs(repo_dir, tag):
    '''Every blob of the tag as (hash, filename, path) triples, in
    ls-tree path order, replacing:
    git ls-tree -r <tag> |
    sed -r "s/^\\S* blob (\\S*)\\t(([^/]*\\/)*(.*))$/\\1 \\4/" (-f)
    ... "\\1 \\2" (-p), dropping commit entries (submodules)
    Paths stay as git prints them: space-separated fields come from
    the tab-separated prefix, and quoted paths stay quoted.'''
    blobs = []
    for line in git_lines(repo_dir, 'ls-tree', '-r', tag):
        # <mode> <type> <hash>\t<path>; mode/type/hash have no spaces
        meta, _, path = line.partition(b'\t')
        fields = meta.split(b' ')
        if len(fields) != 3 or fields[1] != b'blob':
            continue # submodule (commit) entries, like the old sed's d
        hash = fields[2]
        filename = path.rsplit(b'/', 1)[-1]
        blobs.append((hash, filename, path))
    return blobs


class BlobLists:
    '''Per-tag blob lists packed into one plaintext scratch file,
    "hash path" lines in ls-tree order: written once by an upfront
    walk of every tag, read back per tag during indexing, so the
    lists live on disk, not in memory. The filename is not stored;
    get() recomputes it, and returns exactly list_blobs()'s triples
    in exactly list_blobs()'s order — the reconstruction the dump's
    byte-identity hangs on. A hash contains no space and git quotes
    paths with control characters (newlines included), so one line
    is always one blob.'''
    def __init__(self, filename):
        self.f = open(filename, 'w+b')
        self.slices = {} # tag -> (offset, length) in the file

    def add(self, tag, blobs):
        '''Append one tag's (hash, filename, path) triples; the
        filename is dropped here and recomputed by get()'''
        start = self.f.tell()
        for hash, _, path in blobs:
            self.f.write(hash + b' ' + path + b'\n')
        self.slices[tag] = (start, self.f.tell() - start)

    def get(self, tag):
        offset, length = self.slices[tag]
        self.f.seek(offset)
        blobs = []
        for line in self.f.read(length).split(b'\n')[:-1]:
            hash, _, path = line.partition(b' ')
            blobs.append((hash, os.path.basename(path), path))
        return blobs

    def close(self):
        self.f.close()


_batch_tls = local()

def get_blob(hash):
    '''Blob content from a persistent per-thread `git cat-file --batch`
    in the repository given by LXR_REPO_DIR (lib.getRepoDir).

    Same bytes as script('get-blob', hash) without one fork+exec of
    script.sh and git per blob. Responses arrive in request order; one
    process per thread needs no locking.'''
    p = getattr(_batch_tls, 'batch', None)
    if p is None or p.poll() is not None:
        p = subprocess.Popen(['git', 'cat-file', '--batch'],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             cwd=getRepoDir())
        _batch_tls.batch = p

    p.stdin.write(hash + b'\n')
    p.stdin.flush()

    header = p.stdout.readline().split()
    assert header[1] == b'blob', header
    size = int(header[2])
    data = p.stdout.read(size)
    p.stdout.read(1) # newline after the payload
    return data

def get_blob_lines(hash):
    '''get_blob() split into lines with scriptLines semantics:
    split(b'\\n') with the last element dropped, so a blob not ending
    in a newline loses its final (partial) line, as it always has'''
    lines = get_blob(hash).split(b'\n')
    del lines[-1]
    return lines


# ---- script.sh's version-addressed queries (get-file/get-dir/
# get-type), ported with their quirks ----

def _b(arg):
    '''The argument as bytes, as the shell and git pass it around'''
    return arg if isinstance(arg, bytes) else os.fsencode(arg)

def denormalize(path):
    '''script.sh denormalize(): `echo $1 | cut -c 2-` — drop the
    leading character of the /-prefixed web path. $1 is unquoted, so
    word splitting cuts the path at its first whitespace and an
    empty or all-whitespace path gives b'' (no argument reached the
    function) — the live behavior'''
    words = path.split()
    return words[0][1:] if words else b''

def _rev_path(project, version, path):
    '''"<tag>:<path>": the rev:prefix git address script.sh builds
    from the display version through version_rev'''
    return version_rev(_b(version), project) + b':' + denormalize(_b(path))

def _field(fields, i):
    '''awk's default field splitting: missing fields read as empty'''
    return fields[i] if i < len(fields) else b''

def _git_or_empty(repo_dir, *args):
    '''git's stdout, or b'' when git fails (script.sh's 2>/dev/null)'''
    p = subprocess.run(('git',) + args, cwd=repo_dir, stdout=subprocess.PIPE,
                       stderr=subprocess.DEVNULL)
    return p.stdout

def get_file(repo_dir, project, version, path):
    '''Blob content at a display version (script.sh get_file):
    git cat-file blob "<rev>:<path minus the leading marker>"'''
    return _git_or_empty(repo_dir, 'cat-file', 'blob',
                         _rev_path(project, version, path))

def get_type(repo_dir, project, version, path):
    '''blob/tree/... of a path at a display version (script.sh
    get_type), stdout bytes as script.sh printed them'''
    return _git_or_empty(repo_dir, 'cat-file', '-t',
                         _rev_path(project, version, path))

def get_dir(repo_dir, project, version, path):
    '''Directory listing at a display version (script.sh get_dir),
    quirks included:
    - awk's field reorder "$2 $5 $4 $1" keeps only the first word of
      a path containing whitespace (ls-tree quotes such paths, so the
      quotes are part of that word)
    - grep -v " \\." drops dotfile entries: it runs on the reordered
      line, so an unquoted .name drops ("type .name ...") while a
      quoted one survives ("type \".name ...")
    - sort -t ' ' -k 1,1r -k 2,2: type reversed, then path word, the
      whole line as sort's last resort
    Output is the sort pipeline's bytes, newline-terminated lines'''
    out = _git_or_empty(repo_dir, 'ls-tree', '-l',
                        _rev_path(project, version, path))
    lines = []
    for line in out.split(b'\n')[:-1]:
        fields = line.split() # awk's default field splitting
        lines.append(b' '.join((_field(fields, 1), _field(fields, 4),
                                _field(fields, 3), _field(fields, 0))))
    lines = [line for line in lines if b' .' not in line]

    def compare(a, b):
        fa, fb = a.split(b' '), b.split(b' ')
        if fa[0] != fb[0]: # -k 1,1r
            return (fb[0] > fa[0]) - (fb[0] < fa[0])
        ka = fa[1] if len(fa) > 1 else b''
        kb = fb[1] if len(fb) > 1 else b''
        if ka != kb: # -k 2,2
            return (ka > kb) - (ka < kb)
        return (a > b) - (a < b) # last resort: the whole line

    lines.sort(key=functools.cmp_to_key(compare))
    return b''.join(line + b'\n' for line in lines)
