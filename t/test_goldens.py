# Golden tests: the per-project tag pipelines (elixir/repo.py
# TAG_PIPELINES) and the version-addressed queries must reproduce,
# byte for byte, what script.sh printed over the frozen clones — the
# outputs captured by t/goldens/capture.sh. Each projects/<p>.sh port
# is pinned here against the shell it replaces.
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
from pathlib import Path

import pytest

from elixir import repo

GOLDEN_DIR = Path(__file__).resolve().parent / 'goldens'
DATA_DIR = Path(os.environ.get(
    'ELIXIR_DATA_DIR',
    Path(__file__).resolve().parents[2] / 'elixir-data'))

HAVE_CLONES = DATA_DIR.is_dir()

# busybox, freebsd and xen translate display versions to tag names
# (version_rev); only they have a get-type golden
VERSION_REV_PROJECTS = ('busybox', 'freebsd', 'xen')


def projects():
    return sorted(p.name for p in GOLDEN_DIR.iterdir() if p.is_dir())


def golden_lines(project, name):
    return (GOLDEN_DIR / project / name).read_bytes().split(b'\n')[:-1]


@pytest.mark.skipif(not HAVE_CLONES,
                    reason=f'no elixir-data clones under {DATA_DIR}')
@pytest.mark.parametrize('project', projects())
def test_golden_tags(project):
    '''list-tags, list-tags -h, get-latest-tags and dts-comp match the
    captured script.sh output byte for byte'''
    repo_dir = DATA_DIR / project / 'repo'
    assert repo.list_tags(repo_dir, project) == \
        golden_lines(project, 'list-tags.out')
    assert repo.list_tags_h(repo_dir, project) == \
        golden_lines(project, 'list-tags-h.out')
    assert repo.latest_tags(repo_dir, project) == \
        golden_lines(project, 'get-latest-tags.out')
    assert (GOLDEN_DIR / project / 'dts-comp.out').read_bytes() == \
        f'{int(project in repo.DTS_COMP_SUPPORT)}\n'.encode()


@pytest.mark.skipif(not HAVE_CLONES,
                    reason=f'no elixir-data clones under {DATA_DIR}')
@pytest.mark.parametrize('project', VERSION_REV_PROJECTS)
def test_golden_get_type(project):
    '''get-type through each project's version_rev translation, on the
    (display version, path) recorded in the golden's "# args:" header'''
    repo_dir = DATA_DIR / project / 'repo'
    lines = golden_lines(project, 'get-type.out')
    version, path = lines[0][len('# args: '):].split(b' ', 1)
    assert repo.get_type(repo_dir, project, version, path) == \
        lines[1] + b'\n'


@pytest.mark.skipif(not HAVE_CLONES,
                    reason=f'no elixir-data clones under {DATA_DIR}')
@pytest.mark.parametrize('project', projects())
def test_golden_version_rev_roundtrip(project):
    '''Every display version of list-tags, run through version_rev,
    is a real tag of the clone — the inverse-mapping property
    script.sh's get-file/get-type relied on'''
    repo_dir = DATA_DIR / project / 'repo'
    real_tags = set(repo.git_lines(repo_dir, 'tag'))
    for version in golden_lines(project, 'list-tags.out'):
        assert repo.version_rev(version, project) in real_tags, version
