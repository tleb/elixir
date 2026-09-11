# Unit tests for elixir/repo.py, the git plumbing replacing script.sh's
# shell pipelines. Like t/goldens, these pin the shell behavior being
# replaced: the version comparator is checked against real GNU sort -V
# over every clone's tag list, and list_tags/list_blobs against the
# pipelines they replace, so the port cannot drift from what
# `git tag | sed 's/$/.0/' | sort -V | sed 's/\.0$//'` produced.
#
# This file is part of Elixir, a source code cross-referencer.
#
#  Copyright (C) 2017--2020 Mikaël Bouillot <mikael.bouillot@bootlin.com>
#                           and contributors
#
# Elixir is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Elixir is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with Elixir.  If not, see <http://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import os
import subprocess
from pathlib import Path

import pytest

from elixir import repo

# The clones of t/goldens/capture.sh: one tags-only repo per project
DATA_DIR = Path(os.environ.get(
    'ELIXIR_DATA_DIR',
    Path(__file__).resolve().parents[2] / 'elixir-data'))

HAVE_CLONES = DATA_DIR.is_dir()


def sort_V(lines):
    '''Real GNU sort -V, the command the comparator replaces'''
    assert lines, 'sort -V test input must be non-empty'
    p = subprocess.run(['sort', '-V'], input=b'\n'.join(lines) + b'\n',
                       stdout=subprocess.PIPE, env={**os.environ, 'LC_ALL': 'C'},
                       check=True)
    return p.stdout.split(b'\n')[:-1]


def sh_lines(cwd, pipeline):
    '''Run a shell pipeline (the script.sh code being replaced) and
    return its output lines'''
    p = subprocess.run(['sh', '-c', pipeline], cwd=cwd,
                       stdout=subprocess.PIPE,
                       env={**os.environ, 'LC_ALL': 'C'},
                       check=True)
    return p.stdout.split(b'\n')[:-1]


def repos():
    return sorted(p for p in DATA_DIR.glob('*/repo') if p.is_dir())


@pytest.mark.skipif(not HAVE_CLONES,
                    reason=f'no elixir-data clones under {DATA_DIR}')
def test_comparator_matches_sort_V_on_all_clones():
    '''The comparator's order must equal sort -V's for every tag of
    every clone, on the .0-appended keys the pipeline sorts'''
    for repo_dir in repos():
        tags = repo.git_lines(repo_dir, 'tag')
        if not tags:
            continue
        keys = [t + b'.0' for t in tags]
        assert repo.version_sort(keys) == sort_V(keys), repo_dir


def test_comparator_matches_sort_V_edge_cases():
    '''Same property on synthetic shapes: skipped zeros, tilde before
    end-of-string, letters between digits and the rest, file suffixes
    (filevercmp's second pass), leading dots, release/ and fedora/
    tag shapes'''
    shapes = [
        b'v1.0 v1.00 v1.000 v1.2 v1.02 v1',
        b'v1~beta v1~ v1 v1. v1.0',
        b'v3.0-rc1 v3.0 v3 v3.1-rc2 v3.1',
        b'v1a v1 v1.1a v1.1 v1b v10 v9 v100 v99',
        b'8.2 8.2.gz 8.2a 8.2.tar 8.2.1 8.2a.1',
        b'file file.c file.h file.tar file.~1~',
        b'v1+1 v1-1 v1_1 v1.1 v1a v1A',
        b'. .. .a a ..a',
        b'0 00 0a a',
        b'release/14.5.0 release/14.50.0 release/14.5 release/5.0',
        b'fedora/glibc-2.10.1-3 fedora/glibc-2.10-30 fedora/glibc-2.9',
        b'V1 v1 A1 a1 v11 V11',
    ]
    for shape in shapes:
        keys = shape.split(b' ')
        assert repo.version_sort(keys) == sort_V(keys), shape


@pytest.mark.skipif(not HAVE_CLONES,
                    reason=f'no elixir-data clones under {DATA_DIR}')
def test_list_tags_matches_default_pipeline():
    '''list_tags on any repo (the default pipeline) equals the shell
    pipeline it replaces, from script.sh get_tags'''
    pipeline = ("git tag | sed 's/$/.0/' | sort -V | sed 's/\\.0$//'")
    for repo_dir in repos():
        assert repo.list_tags(repo_dir) == sh_lines(repo_dir, pipeline), repo_dir


# Submodule (gitlink) entries must be dropped by list_blobs: this tag
# of the arm-trusted-firmware clone has four (checked when picked)
SUBMODULE_REPO = 'arm-trusted-firmware'
SUBMODULE_TAG = 'v2.15.0'

# Blob-path pipelines from script.sh list_blobs: -f gives (hash,
# filename), -p gives (hash, path). The tab in the sed script is the
# literal ls-tree field separator
LIST_BLOBS_F = ("git ls-tree -r $tag | "
                "sed -r \"s/^\\S* blob (\\S*)\t(([^/]*\\/)*(.*))$/\\1 \\4/; "
                "/^\\S* commit .*$/d\"")
LIST_BLOBS_P = ("git ls-tree -r $tag | "
                "sed -r \"s/^\\S* blob (\\S*)\t(([^/]*\\/)*(.*))$/\\1 \\2/; "
                "/^\\S* commit .*$/d\"")


@pytest.mark.skipif(not HAVE_CLONES,
                    reason=f'no elixir-data clones under {DATA_DIR}')
def test_list_blobs_matches_sed_pipeline():
    '''list_blobs equals both list-blobs pipelines it replaces, on a
    plain project and on one with submodule entries'''
    cases = []
    musl = DATA_DIR / 'musl' / 'repo'
    if musl.is_dir():
        cases.append((musl, repo.git_lines(musl, 'tag')[-1]))
    atf = DATA_DIR / SUBMODULE_REPO / 'repo'
    if atf.is_dir():
        cases.append((atf, SUBMODULE_TAG.encode()))

    for repo_dir, tag in cases:
        blobs = repo.list_blobs(repo_dir, tag)
        # The script.sh output lines, reproduced from the triples
        by_file = [h + b' ' + f for h, f, p in blobs]
        by_path = [h + b' ' + p for h, f, p in blobs]
        for pipeline, expected in ((LIST_BLOBS_F, by_file),
                                   (LIST_BLOBS_P, by_path)):
            env = {**os.environ, 'LC_ALL': 'C', 'tag': tag.decode()}
            p = subprocess.run(['sh', '-c', pipeline], cwd=repo_dir,
                               stdout=subprocess.PIPE, env=env, check=True)
            assert expected == p.stdout.split(b'\n')[:-1], (repo_dir, pipeline)


@pytest.mark.skipif(not HAVE_CLONES,
                    reason=f'no elixir-data clones under {DATA_DIR}')
def test_list_blobs_drops_submodule_entries():
    repo_dir = DATA_DIR / SUBMODULE_REPO / 'repo'
    raw = repo.git(repo_dir, 'ls-tree', '-r', SUBMODULE_TAG)
    assert any(b' commit ' in line for line in raw.split(b'\n')), \
        'expected submodule entries; pick another tag'
    blobs = repo.list_blobs(repo_dir, SUBMODULE_TAG)
    assert len(blobs) == sum(1 for line in raw.split(b'\n')
                             if b' blob ' in line)


@pytest.mark.skipif(not HAVE_CLONES,
                    reason=f'no elixir-data clones under {DATA_DIR}')
def test_blob_lists_round_trip_equals_list_blobs(tmp_path):
    '''BlobLists, the upfront walk's packed scratch file: add() every
    tag of a real clone, then get() each one back — the triples and
    their order must equal a direct list_blobs() exactly. Indexing
    consumes the scratch file instead of the git listing, so the
    dump's byte-identity hangs on this equality.'''
    repo_dir = DATA_DIR / 'musl' / 'repo'
    tags = repo.list_tags(repo_dir)
    assert len(tags) > 1

    lists = repo.BlobLists(str(tmp_path / 'blobwalk-lists'))
    for tag in tags:
        lists.add(tag, repo.list_blobs(repo_dir, tag))
    for tag in tags:
        assert lists.get(tag) == repo.list_blobs(repo_dir, tag), tag
    lists.close()

    # The scratch file itself is plaintext "hash path" lines
    # (greppable, one blob per line: hashes contain no space)
    with open(tmp_path / 'blobwalk-lists', 'rb') as f:
        lines = f.read().split(b'\n')[:-1]
    assert lines and all(len(ln.split(b' ', 1)) == 2 for ln in lines)


def test_dts_comp_support_table():
    '''The table matches projects/*.sh: exactly the projects whose
    plugin sets dts_comp_support=1, plus testproj'''
    expected = {'arm-trusted-firmware', 'barebox', 'linux', 'u-boot',
                'zephyr', 'testproj'}
    assert repo.DTS_COMP_SUPPORT == expected
    assert 'musl' not in repo.DTS_COMP_SUPPORT


def test_tag_pipelines_dispatch(monkeypatch):
    '''Unlisted projects get the default pipeline; a per-project entry
    overrides the pieces it sets (the shape the projects/*.sh ports
    plug into). The default itself is pinned against the shell
    pipeline over all clones, and every entry against t/goldens.'''
    monkeypatch.setattr(repo, 'git_lines', lambda repo_dir, *args: [b'v1', b'v2'])
    assert repo.list_tags('r') == repo.default_tag_pipeline([b'v1', b'v2'])
    monkeypatch.setattr(repo, 'TAG_PIPELINES',
                        {'dummy': repo.TagConfig(
                            list_tags=lambda tags: list(reversed(tags)))})
    assert repo.list_tags('r', 'dummy') == [b'v2', b'v1']


def test_get_blob_lines_scriptlines_semantics(monkeypatch):
    '''Exactly scriptLines semantics: split(b'\\n') minus the last
    element, even when that drops a final partial line'''
    cases = [
        (b'', []),
        (b'\n', [b'']),
        (b'a\n', [b'a']),
        (b'a\nb\n', [b'a', b'b']),
        (b'a\nb', [b'a']), # no trailing newline: last line lost
        (b'a\n\nb\n', [b'a', b'', b'b']),
    ]
    for blob, expected in cases:
        monkeypatch.setattr(repo, 'get_blob', lambda hash, _b=blob: _b)
        assert repo.get_blob_lines(b'0' * 40) == expected, blob


@pytest.mark.skipif(not HAVE_CLONES,
                    reason=f'no elixir-data clones under {DATA_DIR}')
def test_get_blob_matches_git_cat_file(monkeypatch):
    '''The persistent batch reader returns git cat-file blob bytes'''
    repo_dir = DATA_DIR / 'musl' / 'repo'
    hashes = [line.split()[2] for line in
              repo.git(repo_dir, 'ls-tree', '-r', 'v1.2.6').split(b'\n')
              if b' blob ' in line][:3]
    monkeypatch.setenv('LXR_REPO_DIR', str(repo_dir))
    try:
        for h in hashes:
            expected = repo.git(repo_dir, 'cat-file', 'blob', h)
            assert repo.get_blob(h) == expected
    finally:
        # Drop the TLS batch process before another test sees it
        p = getattr(repo._batch_tls, 'batch', None)
        if p is not None:
            p.stdin.close()
            p.wait()
            del repo._batch_tls.batch
