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
processing (sed/sort/awk pipelines) that script.sh ran for update.py.

Everything here works on bytes: tags, paths and blobs are byte
strings, exactly what git prints and what the databases store.

The version comparator is a faithful port of GNU sort's -V comparison
(gnulib's filevercmp.c); it is pinned against the real sort -V in
t/test_repo.py, over every clone's tag list.'''

import functools
import subprocess
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

# Tag pipelines: project name -> function(tags) -> ordered tags, where
# tags is `git tag` output as a bytes list. Projects without an entry
# use the default sort -V pipeline. Populated by project-specific
# ports (linux etc.); only the default exists so far.
TAG_PIPELINES = {}


def git(repo_dir, *args):
    '''Run git in repo_dir, return stdout as bytes'''
    p = subprocess.run(('git',) + args, cwd=repo_dir, stdout=subprocess.PIPE,
                       check=True)
    return p.stdout


def git_lines(repo_dir, *args):
    '''git() output split into lines, like lib.scriptLines'''
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

def list_tags(repo_dir, project=None):
    '''All tags of the repository, oldest first, in the project's
    version order (the default sort -V pipeline unless a project
    entry in TAG_PIPELINES overrides it)'''
    tags = git_lines(repo_dir, 'tag')
    pipeline = TAG_PIPELINES.get(project, default_tag_pipeline)
    return pipeline(tags)

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
    '''get_blob() split into lines with lib.scriptLines semantics:
    split(b'\\n') with the last element dropped, so a blob not ending
    in a newline loses its final (partial) line, as it always has'''
    lines = get_blob(hash).split(b'\n')
    del lines[-1]
    return lines
