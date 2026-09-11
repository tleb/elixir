#!/usr/bin/env python3

#  Tests for elixir/index.py (the utils/index port): project bootstrap,
#  idempotent remotes, the folded index pipeline and the CLI subcommands.
#  Everything runs against tiny local git repositories — no network.
#
#  This file is part of Elixir, a source code cross-referencer.
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
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import subprocess

import pytest

from elixir import cli, index, update
from elixir import data_duckdb as dd


def build_source_repo(path):
    """A tiny repository to fetch from: one commit, one file, one
    lightweight tag — the shape of conftest's build_repo, standalone.
    The -c flags keep the run independent of the user's gitconfig."""
    path.mkdir(parents=True)
    git = ['git', '-C', str(path), '-c', 'init.defaultBranch=main',
           '-c', 'user.name=test', '-c', 'user.email=test@example.com']

    def run(*args):
        subprocess.run(git + list(args), check=True)

    run('init', '.')
    (path / 'foo.c').write_text('int foo(void) { return 0; }\n')
    run('add', '.')
    run('commit', '-m', 'Initial commit')
    run('tag', 'v1')
    return str(path)


def remotes(proj_dir):
    out = subprocess.run(['git', '-C', str(proj_dir / 'repo'), 'remote'],
                         check=True, capture_output=True, text=True)
    return out.stdout.split()


def is_bare(proj_dir):
    out = subprocess.run(
        ['git', '-C', str(proj_dir / 'repo'), 'rev-parse', '--is-bare-repository'],
        check=True, capture_output=True, text=True)
    return out.stdout.strip() == 'true'


def test_project_init(tmp_path):
    proj = tmp_path / 'proj'
    index.project_init(proj)

    assert (proj / 'data').is_dir()
    assert is_bare(proj)
    head = (proj / 'repo' / 'HEAD').read_text().strip()
    assert head == 'ref: refs/heads/main'

    # Re-init is a no-op: the layout and the repository are untouched
    (proj / 'data' / 'marker').touch()
    index.project_init(proj)
    assert is_bare(proj)
    assert (proj / 'repo' / 'HEAD').read_text().strip() == head
    assert (proj / 'data' / 'marker').exists()


def test_add_remote_idempotent_and_numbered(tmp_path):
    src1 = build_source_repo(tmp_path / 'src1')
    src2 = build_source_repo(tmp_path / 'src2')

    proj = tmp_path / 'proj'
    index.project_init(proj)

    index.project_add_remote(proj, src1)
    assert remotes(proj) == ['remote0']

    # Same URL again: no-op, whichever remote already has it
    index.project_add_remote(proj, src1)
    assert remotes(proj) == ['remote0']

    # A different URL gets the next index
    index.project_add_remote(proj, src2)
    assert remotes(proj) == ['remote0', 'remote1']


def test_index_pipeline(tmp_path, monkeypatch, capsys):
    src = build_source_repo(tmp_path / 'src')
    root = tmp_path / 'root'
    proj = root / 'testproj'
    index.project_init(proj)
    index.project_add_remote(proj, src)

    # Keep the update pool small: the pipeline below is real update.run()
    monkeypatch.setattr(update, 'num_threads', 2)

    monkeypatch.chdir(root)
    cli.main(['index', 'testproj'])  # no SystemExit on success

    out = capsys.readouterr().out
    # The empty data directory means from scratch: fetch + update ran twice
    assert out.count('testproj: fetching') == 2
    assert out.count('SUMMARY ') == 2

    conn = dd.connect_ro(proj / 'data' / 'data.duckdb')
    try:
        tags = [row[0] for row in conn.execute('SELECT tag FROM versions').fetchall()]
    finally:
        conn.close()
    assert tags == ['v1']


def test_index_all_and_names_exclusive(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        cli.main(['index'])  # neither names nor --all
    with pytest.raises(SystemExit):
        cli.main(['index', '--all', 'foo'])  # both


def test_remote_add_cli(tmp_path, monkeypatch):
    src = build_source_repo(tmp_path / 'src')
    monkeypatch.chdir(tmp_path)

    cli.main(['remote', 'add', 'testproj', src])

    assert remotes(tmp_path / 'testproj') == ['remote0']
    assert (tmp_path / 'testproj' / 'data').is_dir()  # init ran too


def test_index_continues_past_failed_projects(tmp_path, capsys):
    # Both projects point at a nonexistent path: every fetch fails, so
    # no update runs, and the loop must still reach the second project
    root = tmp_path / 'root'
    for name in ('bad1', 'bad2'):
        proj = root / name
        index.project_init(proj)
        index.project_add_remote(proj, str(tmp_path / 'no-such-repo'))

    failed = index.run(root, ['bad1', 'bad2'])
    assert failed == ['bad1', 'bad2']
    assert capsys.readouterr().err.count('elixir: index') == 2


# The 25 projects utils/index gave default remotes to, verbatim —
# guards typos in the port
DEFAULT_PROJECTS = frozenset({
    'amazon-freertos', 'arm-trusted-firmware', 'barebox', 'busybox',
    'coreboot', 'dpdk', 'glibc', 'llvm', 'mesa', 'musl', 'ofono',
    'op-tee', 'qemu', 'u-boot', 'uclibc-ng', 'zephyr', 'toybox', 'grub',
    'bluez', 'linux', 'xen', 'freebsd', 'opensbi', 'iproute2', 'vpp',
})


def test_default_remotes_table():
    assert set(index.DEFAULT_REMOTES) == DEFAULT_PROJECTS
    assert all(urls for urls in index.DEFAULT_REMOTES.values())
    # The multi-URL entries survived the port
    assert len(index.DEFAULT_REMOTES['dpdk']) == 2
    assert len(index.DEFAULT_REMOTES['linux']) == 3
